"""
Background jobs:

1. Monthly leave balance renewal (no accumulation). The check runs at
   start-up and once a day, and the renewal itself is idempotent per month,
   so a server that sleeps through the 1st of the month catches up on the
   next start.
2. Daily reminder to managers about requests waiting more than 24 hours.
3. Keep-alive self ping so free hosting (Render) does not put the service
   to sleep. Enabled automatically when RENDER_EXTERNAL_URL (set by Render)
   or KEEP_ALIVE_URL is present.
"""
import logging
import os
import time
from datetime import datetime, timedelta

import schedule

from database import session
from leave_logic import renew_monthly_balances, pending_requests_for_review, leave_details, balance_hint
import notifier

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
# httpx logs every Telegram API URL (which embeds the bot token) at INFO level. Never log that.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

RENEWAL_CHECK_TIME = os.getenv("RENEWAL_CHECK_TIME", "00:05")
REMINDER_TIME = os.getenv("PENDING_REMINDER_TIME", "06:00")  # server time (UTC on most hosts)
REMINDER_AGE_HOURS = int(os.getenv("PENDING_REMINDER_AGE_HOURS", "24"))
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL")
KEEP_ALIVE_MINUTES = int(os.getenv("KEEP_ALIVE_MINUTES", "10"))


def renew_monthly_leave_balance():
    """Scheduler entry point for the monthly renewal."""
    try:
        renewed = renew_monthly_balances()
        if renewed:
            for emp in renewed:
                logger.info(
                    "Renewed %s: days=%s hours=%s (unused note: %s / %s)",
                    emp.full_name, emp.daily_leave_balance, emp.hourly_leave_balance,
                    emp.unused_daily_carryover, emp.unused_hourly_carryover,
                )
            logger.info("Monthly renewal applied to %d employee(s).", len(renewed))
    except Exception as e:
        logger.error("Error renewing monthly leave balance: %s", e)
    finally:
        session.remove()


def remind_pending_requests():
    """Send managers one summary of requests that have waited too long."""
    try:
        cutoff = datetime.utcnow() - timedelta(hours=REMINDER_AGE_HOURS)
        stale = [r for r in pending_requests_for_review() if r.created_at is None or r.created_at <= cutoff]
        if not stale:
            return
        lines = [f"⏰ تذكير: {len(stale)} طلب إجازة بانتظار قرارك منذ أكثر من {REMINDER_AGE_HOURS} ساعة:"]
        for r in stale:
            lines.append(f"\n• #{r.id} {r.employee.full_name} ({r.leave_type}) {leave_details(r)}")
            lines.append(balance_hint(r.employee))
        lines.append("\nاستخدم قائمة المدير ← مراجعة الطلبات، أو لوحة الويب.")
        notifier.notify_managers("\n".join(lines))
    except Exception as e:
        logger.error("Error sending pending reminder: %s", e)
    finally:
        session.remove()


def keep_alive_ping():
    """Hit our own /health so the hosting platform keeps the instance awake."""
    if not KEEP_ALIVE_URL:
        return
    import urllib.request
    url = KEEP_ALIVE_URL.rstrip('/') + '/health'
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            logger.debug("Keep-alive ping %s -> %s", url, resp.status)
    except Exception as e:
        logger.warning("Keep-alive ping failed: %s", e)


def schedule_jobs():
    schedule.every().day.at(RENEWAL_CHECK_TIME).do(renew_monthly_leave_balance)
    schedule.every().day.at(REMINDER_TIME).do(remind_pending_requests)
    logger.info("Scheduler: renewal check daily at %s, pending reminder daily at %s", RENEWAL_CHECK_TIME, REMINDER_TIME)
    if KEEP_ALIVE_URL and KEEP_ALIVE_MINUTES > 0:
        schedule.every(KEEP_ALIVE_MINUTES).minutes.do(keep_alive_ping)
        logger.info("Keep-alive: pinging %s/health every %d minutes", KEEP_ALIVE_URL.rstrip('/'), KEEP_ALIVE_MINUTES)


def run_scheduler():
    """Run forever. Call from a dedicated thread."""
    # Catch up immediately in case the server was asleep on the 1st of the month
    renew_monthly_leave_balance()
    schedule_jobs()
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    logger.info("Running monthly renewal check now...")
    renew_monthly_leave_balance()

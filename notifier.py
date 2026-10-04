"""
Synchronous Telegram helpers for code that runs outside the bot's event loop
(Flask request handlers and the scheduler thread).

A fresh Bot instance is created per call so that no httpx client is shared
across event loops.
"""
import asyncio
import logging
import os

import telegram

from database import session, NotificationLog, Employee

logger = logging.getLogger(__name__)


def _token():
    return os.getenv("TELEGRAM_BOT_TOKEN")


def _run(coro_factory):
    """Run an async bot operation from a synchronous context."""
    token = _token()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set; notification skipped.")
        return None

    async def runner():
        bot = telegram.Bot(token=token)
        async with bot:
            return await coro_factory(bot)

    try:
        return asyncio.run(runner())
    except RuntimeError:
        # Already inside a running loop (rare): use a dedicated loop.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(runner())
        finally:
            loop.close()


def send_message(chat_id, text) -> bool:
    if not chat_id:
        return False
    try:
        _run(lambda bot: bot.send_message(chat_id=chat_id, text=text))
        logger.info("Notification sent to %s: %s", chat_id, text[:60])
        return True
    except Exception as e:
        logger.error("Failed to send notification to %s: %s", chat_id, e)
        return False


def notify_managers(text, exclude_telegram_id=None) -> int:
    managers = session.query(Employee).filter_by(is_manager=True, status='approved').all()
    sent = 0
    for m in managers:
        if exclude_telegram_id and m.telegram_id == exclude_telegram_id:
            continue
        if send_message(m.telegram_id, text):
            sent += 1
    return sent


def finalize_leave_notifications(req_id: int, text: str) -> None:
    """
    Replace the manager messages (with approve/reject buttons) logged for a
    leave request by a final text, then forget the logs. Falls back to a
    plain broadcast to managers when no logs exist.
    """
    logs = session.query(NotificationLog).filter_by(request_type='leave', target_id=req_id).all()
    if not logs:
        notify_managers(text)
        return

    async def edit_all(bot):
        for log in logs:
            try:
                await bot.edit_message_text(
                    chat_id=log.manager_telegram_id, message_id=log.message_id, text=text
                )
            except Exception as e:
                logger.warning("Could not edit manager message %s: %s", log.message_id, e)
                try:
                    await bot.send_message(chat_id=log.manager_telegram_id, text=text)
                except Exception as e2:
                    logger.error("Could not notify manager %s: %s", log.manager_telegram_id, e2)

    try:
        _run(edit_all)
    except Exception as e:
        logger.error("finalize_leave_notifications failed: %s", e)
    finally:
        try:
            session.query(NotificationLog).filter_by(request_type='leave', target_id=req_id).delete()
            session.commit()
        except Exception:
            session.rollback()

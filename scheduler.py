"""
Monthly Leave Balance Renewal Scheduler
This module handles automatic renewal of employee leave balances monthly.
"""
import schedule
import time
import logging
from datetime import datetime
from database import session, Employee

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def renew_monthly_leave_balance():
    """
    Renews the leave balance for all approved employees based on their monthly quota.
    This runs at the start of each month.
    """
    # Check if today is the 1st day of the month
    if datetime.now().day != 1:
        return

    try:
        logger.info("Starting monthly leave balance renewal...")
        
        # Get all approved employees
        employees = session.query(Employee).filter_by(status='approved').all()
        
        renewed_count = 0
        for employee in employees:
            # Add monthly quota to current balance
            employee.daily_leave_balance += employee.monthly_daily_leave_quota
            employee.hourly_leave_balance += employee.monthly_hourly_leave_quota
            
            logger.info(
                f"Renewed balance for {employee.full_name}: "
                f"Days={employee.daily_leave_balance}, Hours={employee.hourly_leave_balance}"
            )
            renewed_count += 1
        
        session.commit()
        logger.info(f"Successfully renewed leave balance for {renewed_count} employees.")
        
    except Exception as e:
        logger.error(f"Error renewing monthly leave balance: {e}")
        session.rollback()


def schedule_monthly_renewal():
    """
    Schedules the check to run every day at 00:01.
    The renewal function itself checks if it's the 1st of the month.
    """
    # Run every day at 00:01, but the function will only execute logic on the 1st
    schedule.every().day.at("00:01").do(renew_monthly_leave_balance)
    logger.info("Monthly leave balance renewal scheduler started (checks daily at 00:01)")


def run_scheduler():
    """
    Runs the scheduler in a loop. This should be called in a separate thread.
    """
    schedule_monthly_renewal()
    
    logger.info("Scheduler started. Running pending tasks...")
    
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute


if __name__ == "__main__":
    # For testing: run renewal immediately
    # Note: This will run the logic regardless of the date if executed directly
    logger.info("Running manual leave balance renewal for testing...")
    
    # Temporarily bypass the date check for manual run
    try:
        logger.info("Starting MANUAL monthly leave balance renewal...")
        employees = session.query(Employee).filter_by(status='approved').all()
        renewed_count = 0
        for employee in employees:
            employee.daily_leave_balance += employee.monthly_daily_leave_quota
            employee.hourly_leave_balance += employee.monthly_hourly_leave_quota
            renewed_count += 1
        session.commit()
        logger.info(f"Successfully renewed leave balance for {renewed_count} employees.")
    except Exception as e:
        logger.error(f"Error renewing monthly leave balance: {e}")
        session.rollback()

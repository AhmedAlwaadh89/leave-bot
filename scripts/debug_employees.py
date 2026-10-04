"""Print all employees with their balances. Usage: python scripts/debug_employees.py"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from database import session, Employee  # noqa: E402

if __name__ == "__main__":
    print("ID | Telegram ID | Name | Dept | Manager | Status | Days | Hours | Unused D/H | Last renewal")
    print("-" * 100)
    for e in session.query(Employee).order_by(Employee.id).all():
        print(f"{e.id} | {e.telegram_id} | {e.full_name} | {e.department} | {e.is_manager} | {e.status} | "
              f"{e.daily_leave_balance}/{e.monthly_daily_leave_quota} | {e.hourly_leave_balance}/{e.monthly_hourly_leave_quota} | "
              f"{e.unused_daily_carryover}/{e.unused_hourly_carryover} | {e.last_renewal_date}")

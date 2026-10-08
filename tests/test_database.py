import unittest
from datetime import date

from helpers import new_session
from database import Employee, LeaveRequest


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.session = new_session()

    def tearDown(self):
        self.session.remove()

    def test_create_employee(self):
        employee = Employee(telegram_id=12345, full_name="Test User", status='approved')
        self.session.add(employee)
        self.session.commit()
        retrieved = self.session.query(Employee).filter_by(telegram_id=12345).first()
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.full_name, "Test User")
        self.assertEqual(retrieved.monthly_daily_leave_quota, 2.0)
        self.assertEqual(retrieved.monthly_hourly_leave_quota, 4.0)
        self.assertEqual(retrieved.unused_daily_carryover, 0.0)

    def test_create_leave_request(self):
        employee = Employee(telegram_id=12345, full_name="Test User", status='approved')
        self.session.add(employee)
        self.session.commit()
        leave_request = LeaveRequest(
            employee_id=employee.id, leave_type='يومية',
            start_date=date(2024, 1, 1), end_date=date(2024, 1, 5), reason="Vacation",
        )
        self.session.add(leave_request)
        self.session.commit()
        retrieved = self.session.query(LeaveRequest).filter_by(employee_id=employee.id).first()
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.status, 'pending')
        self.assertIsNotNone(retrieved.created_at)
        self.assertEqual(retrieved.employee.full_name, "Test User")


if __name__ == '__main__':
    unittest.main()

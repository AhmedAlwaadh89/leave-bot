import unittest
import os
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import Base, Employee, LeaveRequest

class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        self.session = Session()

    def tearDown(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)

    def test_create_employee(self):
        employee = Employee(telegram_id=12345, full_name="Test User", status='approved')
        self.session.add(employee)
        self.session.commit()
        queried_employee = self.session.query(Employee).filter_by(telegram_id=12345).first()
        self.assertIsNotNone(queried_employee)
        self.assertEqual(queried_employee.full_name, "Test User")

    def test_create_leave_request(self):
        employee = Employee(telegram_id=54321, full_name="Another User", status='approved')
        self.session.add(employee)
        self.session.commit()

        leave_request = LeaveRequest(
            employee_id=employee.id,
            leave_type='يومية',  # Added required field
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 5),
            reason="Vacation"
        )
        self.session.add(leave_request)
        self.session.commit()

        queried_request = self.session.query(LeaveRequest).first()
        self.assertIsNotNone(queried_request)
        self.assertEqual(queried_request.reason, "Vacation")

if __name__ == '__main__':
    unittest.main()

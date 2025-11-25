import unittest
from unittest.mock import patch
import os
import base64
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as flask_app
from database import Base, Employee, LeaveRequest

original_session = flask_app.session

class TestApp(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        self.session = Session()

        flask_app.session = self.session
        flask_app.app.config['TESTING'] = True
        self.app = flask_app.app.test_client()
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(b"admin:secret").decode('ascii')}

    def tearDown(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)
        flask_app.session = original_session

    def test_index_page_unauthorized(self):
        response = self.app.get('/')
        self.assertEqual(response.status_code, 401)

    def test_index_page_authorized(self):
        employee = Employee(telegram_id=111, full_name="Test Employee")
        leave_request = LeaveRequest(
            employee=employee, 
            status="pending", 
            leave_type='يومية',
            start_date=date.today(), 
            end_date=date.today()
        )
        self.session.add_all([employee, leave_request])
        self.session.commit()
        
        response = self.app.get('/', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Test Employee', response.data)

    @patch('app.send_notification')
    def test_approve_request(self, mock_send_notification):
        employee = Employee(telegram_id=222, full_name="Another Employee", daily_leave_balance=10) # Add balance
        leave_request = LeaveRequest(
            employee=employee, 
            status="pending", 
            leave_type='يومية',
            start_date=date.today(), 
            end_date=date.today()
        )
        self.session.add_all([employee, leave_request])
        self.session.commit()
            
        response = self.app.get(f'/approve/{leave_request.id}', headers=self.auth)
        self.assertEqual(response.status_code, 302)

        updated_request = self.session.get(LeaveRequest, leave_request.id)
        self.assertEqual(updated_request.status, 'approved')
        mock_send_notification.assert_called_once()
        
    @patch('app.send_notification')
    def test_reject_request(self, mock_send_notification):
        employee = Employee(telegram_id=333, full_name="Third Employee")
        leave_request = LeaveRequest(
            employee=employee, 
            status="pending", 
            leave_type='يومية',
            start_date=date.today(), 
            end_date=date.today()
        )
        self.session.add_all([employee, leave_request])
        self.session.commit()

        response = self.app.get(f'/reject/{leave_request.id}', headers=self.auth)
        self.assertEqual(response.status_code, 302)

        updated_request = self.session.get(LeaveRequest, leave_request.id)
        self.assertEqual(updated_request.status, 'rejected')
        mock_send_notification.assert_called_once()

if __name__ == '__main__':
    unittest.main()

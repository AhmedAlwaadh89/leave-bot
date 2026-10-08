import base64
import unittest
from unittest.mock import patch
from datetime import date

from helpers import new_session
import app as flask_app
from database import Employee, LeaveRequest, Holiday

CSRF = 'test-token'


class TestApp(unittest.TestCase):
    def setUp(self):
        self.session = new_session()
        flask_app.app.config['TESTING'] = True
        self.client = flask_app.app.test_client()
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(b"admin:secret").decode('ascii')}
        with self.client.session_transaction() as s:
            s['_csrf_token'] = CSRF

    def tearDown(self):
        self.session.remove()

    def post(self, url, data=None):
        data = dict(data or {})
        data.setdefault('_csrf_token', CSRF)
        return self.client.post(url, data=data, headers=self.auth)

    def _pending(self, tid, days=2.0, start=date(2026, 10, 4), end=date(2026, 10, 4)):
        employee = Employee(telegram_id=tid, full_name=f"Employee {tid}", status='approved', daily_leave_balance=days, hourly_leave_balance=4.0)
        req = LeaveRequest(employee=employee, status="pending", leave_type='يومية', start_date=start, end_date=end, replacement_approval_status='not_required')
        self.session.add_all([employee, req])
        self.session.commit()
        return employee, req

    def test_index_page_unauthorized(self):
        self.assertEqual(self.client.get('/').status_code, 401)

    def test_index_page_authorized(self):
        self._pending(111)
        response = self.client.get('/', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Employee 111'.encode(), response.data)

    def test_get_on_mutating_route_is_not_allowed(self):
        _, req = self._pending(112)
        self.assertEqual(self.client.get(f'/approve/{req.id}', headers=self.auth).status_code, 405)
        self.assertEqual(self.client.get(f'/delete/{req.id}', headers=self.auth).status_code, 405)

    def test_post_without_csrf_is_rejected(self):
        _, req = self._pending(113)
        response = self.client.post(f'/approve/{req.id}', data={}, headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.session.get(LeaveRequest, req.id).status, 'pending')

    @patch('app.finalize_leave_notifications')
    @patch('app.send_notification')
    def test_approve_request(self, mock_send, mock_finalize):
        employee, req = self._pending(222, days=2.0)
        req_id, emp_id = req.id, employee.id
        response = self.post(f'/approve/{req_id}')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.session.get(LeaveRequest, req.id).status, 'approved')
        self.assertEqual(self.session.get(Employee, employee.id).daily_leave_balance, 1.0)
        mock_send.assert_called_once()
        mock_finalize.assert_called_once()

    @patch('app.finalize_leave_notifications')
    @patch('app.send_notification')
    def test_approve_insufficient_then_convert_unpaid(self, mock_send, mock_finalize):
        employee, req = self._pending(223, days=0.0)
        req_id, emp_id = req.id, employee.id
        self.post(f'/approve/{req_id}')
        self.assertEqual(self.session.get(LeaveRequest, req_id).status, 'pending')
        mock_send.assert_not_called()

        self.post(f'/approve/{req_id}', {'convert_unpaid': '1'})
        req = self.session.get(LeaveRequest, req_id)
        self.assertEqual(req.status, 'approved')
        self.assertEqual(req.leave_type, 'بدون راتب')
        self.assertEqual(self.session.get(Employee, emp_id).daily_leave_balance, 0.0)

    def test_admin_add_leave_blocks_without_balance_but_allows_unpaid(self):
        emp = Employee(telegram_id=224, full_name="Zero", status='approved', daily_leave_balance=0.0, hourly_leave_balance=0.0)
        self.session.add(emp)
        self.session.commit()
        emp_id = emp.id
        form = {'employee_id': str(emp_id), 'leave_type': 'يومية', 'start_date': '2026-10-04', 'end_date': '2026-10-04', 'reason': 'x'}
        response = self.post('/admin/add_leave', form)
        self.assertEqual(response.status_code, 200)  # re-rendered with warning
        self.assertEqual(self.session.query(LeaveRequest).count(), 0)
        with patch('app.send_notification'):
            response = self.post('/admin/add_leave', dict(form, leave_type='بدون راتب'))
        self.assertEqual(response.status_code, 302)
        req = self.session.query(LeaveRequest).one()
        self.assertEqual(req.leave_type, 'بدون راتب')
        self.assertEqual(req.status, 'approved')
        self.assertEqual(self.session.get(Employee, emp_id).daily_leave_balance, 0.0)

    @patch('app.finalize_leave_notifications')
    @patch('app.send_notification')
    def test_reject_request(self, mock_send, mock_finalize):
        _, req = self._pending(333)
        response = self.post(f'/reject/{req.id}')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.session.get(LeaveRequest, req.id).status, 'rejected')
        mock_send.assert_called_once()
        mock_finalize.assert_called_once()

    @patch('app.send_notification')
    def test_delete_approved_restores_balance(self, mock_send):
        employee, req = self._pending(444, days=2.0)
        with patch('app.finalize_leave_notifications'):
            self.post(f'/approve/{req.id}')
        self.assertEqual(self.session.get(Employee, employee.id).daily_leave_balance, 1.0)
        self.post(f'/delete/{req.id}')
        self.assertIsNone(self.session.get(LeaveRequest, req.id))
        self.assertEqual(self.session.get(Employee, employee.id).daily_leave_balance, 2.0)

    def test_edit_request_validates_input(self):
        _, req = self._pending(555)
        response = self.post(f'/edit_request/{req.id}', {'leave_type': 'يومية', 'start_date': 'bad', 'end_date': '', 'reason': ''})
        self.assertEqual(response.status_code, 200)  # re-rendered form, no crash
        self.assertEqual(self.session.get(LeaveRequest, req.id).start_date, date(2026, 10, 4))

    def test_update_user_rejects_balance_above_quota(self):
        emp = Employee(telegram_id=666, full_name="Q", status='approved', daily_leave_balance=2.0, hourly_leave_balance=4.0)
        self.session.add(emp)
        self.session.commit()
        self.post(f'/update_user/{emp.id}', {
            'full_name': 'Q', 'department': 'IT', 'daily_quota': '2', 'hourly_quota': '4',
            'daily_balance': '5', 'hourly_balance': '4', 'unused_daily': '0', 'unused_hourly': '0',
        })
        self.assertEqual(self.session.get(Employee, emp.id).daily_leave_balance, 2.0)

    @patch('app.send_notification')
    def test_approve_user_grants_quota(self, mock_send):
        emp = Employee(telegram_id=777, full_name="New", status='pending')
        self.session.add(emp)
        self.session.commit()
        emp_id = emp.id
        self.post(f'/approve_user/{emp_id}')
        emp = self.session.get(Employee, emp_id)
        self.assertEqual(emp.status, 'approved')
        self.assertEqual(emp.daily_leave_balance, 2.0)
        self.assertEqual(emp.hourly_leave_balance, 4.0)
        self.assertIsNotNone(emp.last_renewal_date)

    def test_add_holiday_and_export(self):
        self.post('/add_holiday', {'name': 'Eid', 'date': '2026-10-05'})
        self.assertEqual(self.session.query(Holiday).count(), 1)
        self._pending(888)
        response = self.client.get('/export_reports', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Employee 888', response.data.decode('utf-8-sig'))

    @patch('app.send_notification')
    def test_suspend_activate_delete_and_reset(self, mock_send):
        boss = Employee(telegram_id=1, full_name="Boss", status='approved', is_manager=True)
        emp = Employee(telegram_id=999, full_name="S", status='approved', daily_leave_balance=2.0, hourly_leave_balance=4.0)
        self.session.add_all([boss, emp])
        self.session.commit()
        emp_id = emp.id
        self.post(f'/suspend_user/{emp_id}')
        self.assertEqual(self.session.get(Employee, emp_id).status, 'suspended')
        self.post(f'/activate_user/{emp_id}')
        self.assertEqual(self.session.get(Employee, emp_id).status, 'approved')
        self.post('/reset_all', {'confirm': 'wrong'})
        self.assertIsNotNone(self.session.get(Employee, emp_id))
        self.post(f'/reject_user/{emp_id}')
        self.assertIsNone(self.session.get(Employee, emp_id))
        response = self.post('/reset_all', {'confirm': 'RESET'})
        self.assertEqual(response.status_code, 302)

    def test_health(self):
        self.assertEqual(self.client.get('/health').status_code, 200)


if __name__ == '__main__':
    unittest.main()


class TestPagesRender(unittest.TestCase):
    def setUp(self):
        self.session = new_session()
        flask_app.app.config['TESTING'] = True
        self.client = flask_app.app.test_client()
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(b"admin:secret").decode('ascii')}
        emp = Employee(telegram_id=1, full_name="Render Emp", status='approved', department="IT",
                       daily_leave_balance=1.0, hourly_leave_balance=2.0, unused_daily_carryover=1.0)
        pend = Employee(telegram_id=2, full_name="Pending Emp", status='pending')
        self.session.add_all([emp, pend, Holiday(name="H", date=date(2026, 10, 5))])
        self.session.commit()
        from datetime import time
        self.session.add_all([
            LeaveRequest(employee_id=emp.id, leave_type='يومية', start_date=date(2026, 10, 4), end_date=date(2026, 10, 6), status='pending', replacement_approval_status='not_required'),
            LeaveRequest(employee_id=emp.id, leave_type='بالساعة', start_date=date(2026, 10, 7), end_date=date(2026, 10, 7), start_time=time(9), end_time=time(11), status='approved', approved_by='X'),
            LeaveRequest(employee_id=emp.id, leave_type='يومية', start_date=date(2026, 10, 8), end_date=date(2026, 10, 8), status='cancelled'),
        ])
        self.session.commit()
        self.req_id = self.session.query(LeaveRequest).first().id

    def tearDown(self):
        self.session.remove()

    def test_all_pages_render(self):
        for url in ['/', '/employees', '/holidays', '/reports', '/reports?status=approved', '/admin/add_leave', f'/edit_request/{self.req_id}']:
            with self.subTest(url=url):
                response = self.client.get(url, headers=self.auth)
                self.assertEqual(response.status_code, 200, url)
                if not url.startswith('/reports'):
                    self.assertIn(b'_csrf_token', response.data)

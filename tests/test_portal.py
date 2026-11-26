import unittest
from datetime import date, timedelta
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from helpers import new_session
import app as flask_app
import portal
from database import Employee, LeaveRequest
from form_utils import normalize_phone

CSRF = 'portal-token'


def next_working_day(start=None):
    d = (start or date.today()) + timedelta(days=1)
    while d.weekday() == 4:  # Friday
        d += timedelta(days=1)
    return d


class PortalTestCase(unittest.TestCase):
    def setUp(self):
        self.session = new_session()
        portal._failed_logins.clear()
        flask_app.app.config['TESTING'] = True
        self.client = flask_app.app.test_client()
        with self.client.session_transaction() as s:
            s['_csrf_token'] = CSRF

    def tearDown(self):
        self.session.remove()

    def post(self, url, data=None, client=None):
        data = dict(data or {})
        data.setdefault('_csrf_token', CSRF)
        return (client or self.client).post(url, data=data)

    def make_emp(self, name, phone, password='pass1234', manager=False, status='approved', days=2.0, hours=4.0):
        emp = Employee(full_name=name, phone=phone, password_hash=generate_password_hash(password),
                       is_manager=manager, status=status, daily_leave_balance=days, hourly_leave_balance=hours)
        self.session.add(emp)
        self.session.commit()
        return emp.id

    def login(self, phone, password='pass1234', client=None):
        return self.post('/login', {'phone': phone, 'password': password}, client=client)

    def other_client(self):
        c = flask_app.app.test_client()
        with c.session_transaction() as s:
            s['_csrf_token'] = CSRF
        return c


class TestPhone(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_phone('0770 123 4567'), '07701234567')
        self.assertEqual(normalize_phone('+964 770 123 4567'), '07701234567')
        self.assertEqual(normalize_phone('009647701234567'), '07701234567')
        self.assertEqual(normalize_phone('7701234567'), '07701234567')
        self.assertEqual(normalize_phone('٠٧٧٠١٢٣٤٥٦٧'), '07701234567')
        self.assertIsNone(normalize_phone('12'))
        self.assertIsNone(normalize_phone(''))


class TestRegistrationAndLogin(PortalTestCase):
    def test_root_redirects_to_login(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])
        self.assertEqual(self.client.get('/login').status_code, 200)
        self.assertEqual(self.client.get('/register').status_code, 200)

    def test_first_registration_becomes_manager(self):
        r = self.post('/register', {'full_name': 'المدير الأول', 'phone': '07700000001', 'department': 'الإدارة',
                                    'password': 'secret12', 'password2': 'secret12'})
        self.assertEqual(r.status_code, 302)
        emp = self.session.query(Employee).filter_by(phone='07700000001').one()
        self.assertTrue(emp.is_manager)
        self.assertEqual(emp.status, 'approved')
        self.assertEqual(emp.daily_leave_balance, 2.0)
        self.assertEqual(self.client.get('/me').status_code, 200)

    def test_registration_pending_until_approved(self):
        self.make_emp('Boss', '07700000001', manager=True)
        r = self.post('/register', {'full_name': 'Ali Hassan', 'phone': '+964 771 000 0002', 'password': 'abc123', 'password2': 'abc123'})
        self.assertEqual(r.status_code, 302)
        emp = self.session.query(Employee).filter_by(phone='07710000002').one()
        self.assertEqual(emp.status, 'pending')
        emp_id = emp.id
        r = self.login('07710000002', 'abc123')
        self.assertIn('بانتظار موافقة'.encode(), r.data)
        self.assertEqual(self.client.get('/me').status_code, 302)

        # Manager approves from the portal
        boss = self.other_client()
        self.login('07700000001', client=boss)
        self.post(f'/me/manage/user/{emp_id}/approve', client=boss)
        emp = self.session.get(Employee, emp_id)
        self.assertEqual(emp.status, 'approved')
        self.assertEqual(emp.daily_leave_balance, 2.0)

        r = self.login('07710000002', 'abc123')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get('/me').status_code, 200)

    def test_duplicate_phone_and_validation(self):
        self.make_emp('A', '07700000003')
        r = self.post('/register', {'full_name': 'B Person', 'phone': '07700000003', 'password': 'abc123', 'password2': 'abc123'})
        self.assertIn('مسجل مسبقاً'.encode(), r.data)
        r = self.post('/register', {'full_name': 'B Person', 'phone': '07700000004', 'password': 'abc', 'password2': 'abc'})
        self.assertEqual(self.session.query(Employee).filter_by(phone='07700000004').count(), 0)

    def test_wrong_password_and_throttle(self):
        self.make_emp('A', '07700000005')
        for _ in range(5):
            r = self.login('07700000005', 'wrong')
            self.assertEqual(r.status_code, 401)
        r = self.login('07700000005', 'pass1234')
        self.assertEqual(r.status_code, 429)

    def test_suspended_cannot_login_and_is_logged_out(self):
        emp_id = self.make_emp('A', '07700000006')
        self.login('07700000006')
        self.assertEqual(self.client.get('/me').status_code, 200)
        emp = self.session.get(Employee, emp_id)
        emp.status = 'suspended'
        self.session.commit()
        self.assertEqual(self.client.get('/me').status_code, 302)
        r = self.login('07700000006')
        self.assertIn('إيقاف'.encode(), r.data)

    def test_password_change_signs_out_other_devices(self):
        self.make_emp('A', '07700000007')
        other = self.other_client()
        self.login('07700000007')
        self.login('07700000007', client=other)
        r = self.post('/me/password', {'current_password': 'pass1234', 'new_password': 'newpass9', 'new_password2': 'newpass9'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get('/me').status_code, 200)
        self.assertEqual(other.get('/me').status_code, 302)
        self.assertEqual(self.login('07700000007', 'newpass9', client=other).status_code, 302)

    def test_open_redirect_is_blocked(self):
        self.make_emp('A', '07700000008')
        r = self.post('/login?next=//evil.example.com', {'phone': '07700000008', 'password': 'pass1234'})
        self.assertNotIn('evil', r.headers['Location'])


class TestRequests(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.boss_id = self.make_emp('Boss', '07700000001', manager=True)
        self.emp_id = self.make_emp('Employee', '07700000002', days=2.0, hours=4.0)
        self.login('07700000002')
        self.boss = self.other_client()
        self.login('07700000001', client=self.boss)

    def test_daily_request_and_manager_approval(self):
        d = next_working_day()
        r = self.post('/me/new', {'leave_type': 'يومية', 'start_date': d.isoformat(), 'end_date': d.isoformat(), 'reason': 'شخصي'})
        self.assertEqual(r.status_code, 302)
        req = self.session.query(LeaveRequest).one()
        self.assertEqual(req.status, 'pending')
        self.assertEqual(req.replacement_approval_status, 'not_required')
        page = self.boss.get('/me/manage')
        self.assertIn(f'#{req.id}'.encode(), page.data)
        self.post(f'/me/manage/request/{req.id}/approve', client=self.boss)
        req = self.session.get(LeaveRequest, req.id)
        self.assertEqual(req.status, 'approved')
        self.assertEqual(req.approved_by, 'Boss')
        self.assertEqual(self.session.get(Employee, self.emp_id).daily_leave_balance, 1.0)

    def test_over_balance_blocked_then_converted_to_unpaid(self):
        # Three working days against a balance of two
        start = next_working_day()
        end = next_working_day(next_working_day(start))
        form = {'leave_type': 'يومية', 'start_date': start.isoformat(), 'end_date': end.isoformat(), 'reason': 'سفر'}
        r = self.post('/me/new', form)
        self.assertEqual(r.status_code, 200)
        self.assertIn('رصيدك لا يكفي'.encode(), r.data)
        self.assertEqual(self.session.query(LeaveRequest).count(), 0)
        r = self.post('/me/new', dict(form, convert_unpaid='1'))
        self.assertEqual(r.status_code, 302)
        req = self.session.query(LeaveRequest).one()
        self.assertEqual(req.leave_type, 'بدون راتب')

    def test_work_duty_hours_never_deducted(self):
        d = next_working_day()
        r = self.post('/me/new', {'leave_type': 'مهمة عمل', 'start_date': d.isoformat(), 'start_time': '09:00',
                                  'end_time': '14:00', 'reason': 'زيارة موقع'})
        self.assertEqual(r.status_code, 302)
        req = self.session.query(LeaveRequest).one()
        self.assertEqual(req.leave_type, 'مهمة عمل')
        self.post(f'/me/manage/request/{req.id}/approve', client=self.boss)
        emp = self.session.get(Employee, self.emp_id)
        self.assertEqual(emp.hourly_leave_balance, 4.0)
        self.assertEqual(self.session.get(LeaveRequest, req.id).status, 'approved')

    def test_replacement_flow(self):
        rep_id = self.make_emp('Replacement', '07700000009')
        d = next_working_day()
        self.post('/me/new', {'leave_type': 'بالساعة', 'start_date': d.isoformat(), 'start_time': '10:00',
                              'end_time': '12:00', 'reason': 'مراجعة', 'replacement_id': str(rep_id)})
        req = self.session.query(LeaveRequest).one()
        self.assertEqual(req.replacement_approval_status, 'pending')
        # Not visible to the manager until the replacement accepts
        self.assertNotIn(f'#{req.id}'.encode(), self.boss.get('/me/manage').data)
        rep = self.other_client()
        self.login('07700000009', client=rep)
        self.assertIn('طلبات بديل'.encode(), rep.get('/me').data)
        self.post(f'/me/replacement/{req.id}/accept', client=rep)
        self.assertEqual(self.session.get(LeaveRequest, req.id).replacement_approval_status, 'accepted')
        self.assertIn(f'#{req.id}'.encode(), self.boss.get('/me/manage').data)
        # The requester cannot answer for the replacement
        self.post(f'/me/replacement/{req.id}/reject')
        self.assertEqual(self.session.get(LeaveRequest, req.id).status, 'pending')

    def test_cancel_and_overlap(self):
        d = next_working_day()
        form = {'leave_type': 'يومية', 'start_date': d.isoformat(), 'end_date': d.isoformat(), 'reason': 'x'}
        self.post('/me/new', form)
        r = self.post('/me/new', form)
        self.assertIn('يتداخل'.encode(), r.data)
        req = self.session.query(LeaveRequest).one()
        self.post(f'/me/cancel/{req.id}')
        self.assertEqual(self.session.get(LeaveRequest, req.id).status, 'cancelled')

    def test_past_date_rejected(self):
        y = (date.today() - timedelta(days=2)).isoformat()
        r = self.post('/me/new', {'leave_type': 'يومية', 'start_date': y, 'end_date': y, 'reason': 'x'})
        self.assertIn('الماضي'.encode(), r.data)
        self.assertEqual(self.session.query(LeaveRequest).count(), 0)

    def test_employee_cannot_access_manage_and_manager_cannot_self_approve(self):
        self.assertEqual(self.client.get('/me/manage').status_code, 302)
        d = next_working_day()
        self.post('/me/new', {'leave_type': 'يومية', 'start_date': d.isoformat(), 'end_date': d.isoformat(), 'reason': 'x'},
                  client=self.boss)
        req = self.session.query(LeaveRequest).filter_by(employee_id=self.boss_id).one()
        self.post(f'/me/manage/request/{req.id}/approve', client=self.boss)
        self.assertEqual(self.session.get(LeaveRequest, req.id).status, 'pending')

    def test_pages_render(self):
        for url in ['/me', '/me/new', '/me/new?type=مهمة عمل', '/me/password']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.boss.get('/me/manage').status_code, 200)


class TestAdminCredentials(PortalTestCase):
    def test_admin_enables_web_login_for_telegram_employee(self):
        import base64
        auth = {'Authorization': 'Basic ' + base64.b64encode(b"admin:secret").decode('ascii')}
        emp = Employee(telegram_id=555, full_name='Old Telegram User', status='approved',
                       daily_leave_balance=2.0, hourly_leave_balance=4.0)
        self.session.add(emp)
        self.session.commit()
        emp_id = emp.id
        r = self.client.post(f'/set_credentials/{emp_id}', data={'_csrf_token': CSRF, 'phone': '0771 222 3333', 'password': 'start123'}, headers=auth)
        self.assertEqual(r.status_code, 302)
        emp = self.session.get(Employee, emp_id)
        self.assertEqual(emp.phone, '07712223333')
        self.assertEqual(self.login('07712223333', 'start123').status_code, 302)
        self.assertEqual(self.client.get('/me').status_code, 200)

    def test_admin_add_web_only_employee(self):
        import base64
        auth = {'Authorization': 'Basic ' + base64.b64encode(b"admin:secret").decode('ascii')}
        r = self.client.post('/add_user', data={'_csrf_token': CSRF, 'full_name': 'Web Only', 'phone': '07713334444',
                                                'password': 'start123'}, headers=auth)
        self.assertEqual(r.status_code, 302)
        emp = self.session.query(Employee).filter_by(phone='07713334444').one()
        self.assertIsNone(emp.telegram_id)
        self.assertEqual(emp.status, 'approved')
        self.assertEqual(emp.daily_leave_balance, 2.0)  # new employees get the full quota
        self.assertEqual(emp.hourly_leave_balance, 4.0)
        self.assertEqual(self.client.get('/employees', headers=auth).status_code, 200)


if __name__ == '__main__':
    unittest.main()

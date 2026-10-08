import unittest
from datetime import date, time

from helpers import new_session
from database import Employee, LeaveRequest, Holiday
import leave_logic as logic


def make_employee(session, tid=1, days=2.0, hours=4.0, unused_d=0.0, unused_h=0.0, last_renewal=None):
    emp = Employee(
        telegram_id=tid, full_name=f"Emp {tid}", status='approved',
        daily_leave_balance=days, hourly_leave_balance=hours,
        unused_daily_carryover=unused_d, unused_hourly_carryover=unused_h,
        last_renewal_date=last_renewal,
    )
    session.add(emp)
    session.commit()
    return emp


def make_request(session, emp, start, end, leave_type=logic.DAILY, start_time=None, end_time=None):
    req = LeaveRequest(employee_id=emp.id, leave_type=leave_type, start_date=start, end_date=end,
                       start_time=start_time, end_time=end_time, reason="r", replacement_approval_status='not_required')
    session.add(req)
    session.commit()
    return req


class TestWorkingDays(unittest.TestCase):
    def setUp(self):
        self.session = new_session()

    def tearDown(self):
        self.session.remove()

    def test_saturday_is_a_working_day_and_friday_is_not(self):
        # 2026-10-02 is a Friday, 2026-10-03 is a Saturday
        self.assertFalse(logic.is_working_day(date(2026, 10, 2), set()))
        self.assertTrue(logic.is_working_day(date(2026, 10, 3), set()))

    def test_week_counts_six_working_days(self):
        # Sat 2026-10-03 .. Fri 2026-10-09 -> 6 working days
        self.assertEqual(logic.calculate_leave_days(date(2026, 10, 3), date(2026, 10, 9), set()), 6)

    def test_holidays_are_excluded(self):
        self.session.add(Holiday(name="Eid", date=date(2026, 10, 5)))
        self.session.commit()
        # Sun 4 .. Tue 6 -> 3 days minus holiday on the 5th
        self.assertEqual(logic.calculate_leave_days(date(2026, 10, 4), date(2026, 10, 6)), 2)

    def test_hours(self):
        self.assertEqual(logic.calculate_leave_hours(time(9, 0), time(11, 30)), 2.5)


class TestApproval(unittest.TestCase):
    def setUp(self):
        self.session = new_session()

    def tearDown(self):
        self.session.remove()

    def test_approve_deducts_balance(self):
        emp = make_employee(self.session, days=2.0)
        req = make_request(self.session, emp, date(2026, 10, 4), date(2026, 10, 4))  # Sunday
        res = logic.approve_request(req.id, "Boss")
        self.assertTrue(res.ok)
        self.assertFalse(res.converted_to_unpaid)
        self.assertEqual(emp.daily_leave_balance, 1.0)
        self.assertEqual(req.status, 'approved')
        self.assertEqual(req.approved_by, "Boss")

    def test_insufficient_balance_blocks_unless_converted_to_unpaid(self):
        emp = make_employee(self.session, days=1.0, unused_d=3.0)
        req = make_request(self.session, emp, date(2026, 10, 4), date(2026, 10, 6))  # 3 working days
        res = logic.approve_request(req.id, "Boss")
        self.assertFalse(res.ok)
        self.assertTrue(res.needs_unpaid)
        self.assertEqual(res.shortage, 2.0)
        self.assertEqual(req.status, 'pending')
        self.assertEqual(emp.daily_leave_balance, 1.0)

        res = logic.approve_request(req.id, "Boss", convert_to_unpaid=True)
        self.assertTrue(res.ok)
        self.assertTrue(res.converted_to_unpaid)
        self.assertEqual(req.leave_type, logic.UNPAID)
        self.assertEqual(req.status, 'approved')
        self.assertEqual(emp.daily_leave_balance, 1.0)  # untouched
        self.assertEqual(emp.unused_daily_carryover, 3.0)  # note untouched
        self.assertIn("بدون راتب", req.approved_by)

    def test_unpaid_request_never_touches_balance(self):
        emp = make_employee(self.session, days=0.0, hours=0.0)
        req = make_request(self.session, emp, date(2026, 10, 4), date(2026, 10, 8), logic.UNPAID)
        amount, shortage, unit = logic.shortage_for(emp, req.leave_type, req.start_date, req.end_date)
        self.assertEqual((amount, shortage, unit), (5.0, 0.0, 'يوم'))
        res = logic.approve_request(req.id, "Boss")
        self.assertTrue(res.ok)
        self.assertEqual(emp.daily_leave_balance, 0.0)
        tid, restored = logic.delete_request(req.id)
        self.assertFalse(restored)
        self.assertEqual(emp.daily_leave_balance, 0.0)

    def test_shortage_for_hourly(self):
        emp = make_employee(self.session, hours=1.0)
        amount, shortage, unit = logic.shortage_for(emp, logic.HOURLY, date(2026, 10, 4), date(2026, 10, 4), time(9, 0), time(12, 0))
        self.assertEqual((amount, shortage, unit), (3.0, 2.0, 'ساعة'))

    def test_second_approval_is_rejected(self):
        emp = make_employee(self.session)
        req = make_request(self.session, emp, date(2026, 10, 4), date(2026, 10, 4))
        self.assertTrue(logic.approve_request(req.id, "A").ok)
        res = logic.approve_request(req.id, "B")
        self.assertFalse(res.ok)
        self.assertEqual(emp.daily_leave_balance, 1.0)

    def test_reject_and_cancel(self):
        emp = make_employee(self.session)
        req = make_request(self.session, emp, date(2026, 10, 4), date(2026, 10, 4))
        self.assertTrue(logic.reject_request(req.id, "Boss").ok)
        self.assertEqual(req.status, 'rejected')
        self.assertEqual(emp.daily_leave_balance, 2.0)

        req2 = make_request(self.session, emp, date(2026, 10, 5), date(2026, 10, 5))
        self.assertFalse(logic.cancel_request(req2.id, emp.id + 99).ok)
        self.assertTrue(logic.cancel_request(req2.id, emp.id).ok)
        self.assertEqual(req2.status, 'cancelled')

    def test_delete_approved_restores_balance_capped_at_quota(self):
        emp = make_employee(self.session, days=2.0)
        req = make_request(self.session, emp, date(2026, 10, 4), date(2026, 10, 5))
        logic.approve_request(req.id, "Boss")
        self.assertEqual(emp.daily_leave_balance, 0.0)
        tid, restored = logic.delete_request(req.id)
        self.assertEqual(tid, emp.telegram_id)
        self.assertTrue(restored)
        self.assertEqual(emp.daily_leave_balance, 2.0)
        self.assertIsNone(self.session.get(LeaveRequest, req.id))


class TestMonthlyRenewal(unittest.TestCase):
    def setUp(self):
        self.session = new_session()

    def tearDown(self):
        self.session.remove()

    def test_renewal_resets_without_accumulation_and_records_unused(self):
        emp = make_employee(self.session, days=1.5, hours=4.0, last_renewal=date(2026, 9, 1))
        renewed = logic.renew_monthly_balances(today=date(2026, 10, 1))
        self.assertEqual(len(renewed), 1)
        self.assertEqual(emp.daily_leave_balance, 2.0)
        self.assertEqual(emp.hourly_leave_balance, 4.0)
        self.assertEqual(emp.unused_daily_carryover, 1.5)
        self.assertEqual(emp.unused_hourly_carryover, 4.0)
        self.assertEqual(emp.last_renewal_date, date(2026, 10, 1))

    def test_renewal_is_idempotent_within_month(self):
        emp = make_employee(self.session, days=0.0, last_renewal=date(2026, 10, 1))
        self.assertEqual(logic.renew_monthly_balances(today=date(2026, 10, 15)), [])
        self.assertEqual(emp.daily_leave_balance, 0.0)

    def test_catch_up_after_missed_months(self):
        emp = make_employee(self.session, days=0.0, hours=0.0, last_renewal=date(2026, 7, 1))
        logic.renew_monthly_balances(today=date(2026, 10, 20))
        self.assertEqual(emp.daily_leave_balance, 2.0)
        self.assertEqual(emp.last_renewal_date, date(2026, 10, 20))

    def test_first_run_caps_old_accumulated_balance(self):
        emp = make_employee(self.session, days=7.0, hours=1.0, last_renewal=None)
        logic.renew_monthly_balances(today=date(2026, 10, 4))
        self.assertEqual(emp.daily_leave_balance, 2.0)
        self.assertEqual(emp.unused_daily_carryover, 5.0)
        self.assertEqual(emp.hourly_leave_balance, 1.0)  # kept, under quota
        self.assertEqual(emp.unused_hourly_carryover, 0.0)

    def test_pending_employees_are_not_renewed(self):
        emp = Employee(telegram_id=9, full_name="P", status='pending', daily_leave_balance=0.0, hourly_leave_balance=0.0)
        self.session.add(emp)
        self.session.commit()
        logic.renew_monthly_balances(today=date(2026, 10, 1))
        self.assertEqual(emp.daily_leave_balance, 0.0)


if __name__ == '__main__':
    unittest.main()

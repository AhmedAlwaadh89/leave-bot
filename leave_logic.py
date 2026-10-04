"""
Shared business logic for leave management.

Used by the Telegram bot, the Flask dashboard and the scheduler so that
all entry points apply exactly the same rules:

* Work week: Saturday to Thursday. Only **Friday** is a weekly day off.
  Official holidays (``Holiday`` table) are also excluded.
* Every employee has a monthly quota (default 2 days / 4 hours).
* The quota is renewed at the start of each month **without accumulation**.
  Unused balance is recorded in ``unused_*_carryover`` as a *note* for
  management; it is never usable balance.
* A manager may grant an *exceptional* approval when the balance is
  insufficient. The usable balance then drops to zero and the shortage is
  subtracted from the informational carryover note (if any).
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from database import session, Employee, LeaveRequest, Holiday

# Friday only. Saturday is a normal working day.
WEEKEND_DAYS = {4}

DAILY = 'يومية'
HOURLY = 'بالساعة'

STATUS_PENDING = 'pending'
STATUS_APPROVED = 'approved'
STATUS_REJECTED = 'rejected'
STATUS_CANCELLED = 'cancelled'

STATUS_LABELS = {
    STATUS_PENDING: 'قيد الانتظار',
    STATUS_APPROVED: 'مقبولة',
    STATUS_REJECTED: 'مرفوضة',
    STATUS_CANCELLED: 'ملغاة',
}

EXCEPTIONAL_SUFFIX = ' (استثنائي)'


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def fmt(n) -> str:
    """Format a number without a trailing .0 (2.0 -> '2', 1.5 -> '1.5')."""
    if n is None:
        return '0'
    n = float(n)
    return str(int(n)) if n == int(n) else f"{n:g}"


def leave_details(req: LeaveRequest) -> str:
    if req.leave_type == HOURLY and req.start_time and req.end_time:
        return f"يوم {req.start_date} من {req.start_time.strftime('%H:%M')} إلى {req.end_time.strftime('%H:%M')}"
    return f"من {req.start_date} إلى {req.end_date}"


def balance_hint(emp: Employee) -> str:
    """Current balance plus the informational note about unused past balance."""
    text = (
        f"💰 الرصيد الحالي: {fmt(emp.daily_leave_balance)} يوم | {fmt(emp.hourly_leave_balance)} ساعة"
        f" (الحصة الشهرية: {fmt(emp.monthly_daily_leave_quota)} يوم | {fmt(emp.monthly_hourly_leave_quota)} ساعة)"
    )
    unused_d = emp.unused_daily_carryover or 0
    unused_h = emp.unused_hourly_carryover or 0
    if unused_d > 0 or unused_h > 0:
        text += (
            f"\n📝 ملاحظة للإدارة: لم يستخدم الموظف {fmt(unused_d)} يوم و {fmt(unused_h)} ساعة"
            f" من أشهر سابقة (للتقدير فقط، غير قابلة للاستخدام تلقائياً)."
        )
    return text


# --------------------------------------------------------------------------
# Duration calculation
# --------------------------------------------------------------------------
def get_holidays() -> set:
    return {h.date for h in session.query(Holiday).all()}


def is_working_day(d: date, holidays: Optional[set] = None) -> bool:
    if holidays is None:
        holidays = get_holidays()
    return d.weekday() not in WEEKEND_DAYS and d not in holidays


def calculate_leave_days(start_date: date, end_date: date, holidays: Optional[set] = None) -> int:
    """Working days between start and end inclusive (Friday + holidays excluded)."""
    if holidays is None:
        holidays = get_holidays()
    days = 0
    current = start_date
    while current <= end_date:
        if is_working_day(current, holidays):
            days += 1
        current += timedelta(days=1)
    return days


def calculate_leave_hours(start_time, end_time) -> float:
    if not start_time or not end_time:
        return 0.0
    duration = datetime.combine(date.today(), end_time) - datetime.combine(date.today(), start_time)
    return round(duration.total_seconds() / 3600, 2)


def requested_amount(leave_type, start_date, end_date, start_time=None, end_time=None) -> Tuple[float, str]:
    """Return (amount, unit_label) for the given leave parameters."""
    if leave_type == DAILY:
        return float(calculate_leave_days(start_date, end_date)), 'يوم'
    return calculate_leave_hours(start_time, end_time), 'ساعة'


def request_amount(req: LeaveRequest) -> Tuple[float, str]:
    return requested_amount(req.leave_type, req.start_date, req.end_date, req.start_time, req.end_time)


def current_balance(emp: Employee, leave_type: str) -> float:
    return float(emp.daily_leave_balance or 0) if leave_type == DAILY else float(emp.hourly_leave_balance or 0)


def shortage_for(emp: Employee, leave_type, start_date, end_date, start_time=None, end_time=None) -> Tuple[float, float, str]:
    """Return (amount, shortage, unit). shortage is 0 when the balance suffices."""
    amount, unit = requested_amount(leave_type, start_date, end_date, start_time, end_time)
    balance = current_balance(emp, leave_type)
    return amount, max(0.0, amount - balance), unit


# --------------------------------------------------------------------------
# Balance mutation
# --------------------------------------------------------------------------
def deduct_balance(emp: Employee, leave_type: str, amount: float, force: bool = False) -> float:
    """
    Deduct ``amount`` from the employee's usable balance.

    Returns the shortage that could not be covered by the usable balance.
    With ``force=False`` a shortage raises ValueError and nothing changes.
    With ``force=True`` the usable balance drops to 0 and the shortage is
    subtracted from the informational carryover note (never below 0).
    """
    is_daily = leave_type == DAILY
    balance = current_balance(emp, leave_type)
    shortage = max(0.0, amount - balance)
    if shortage > 0 and not force:
        raise ValueError("insufficient balance")

    new_balance = max(0.0, balance - amount)
    if is_daily:
        emp.daily_leave_balance = new_balance
        if shortage > 0:
            emp.unused_daily_carryover = max(0.0, float(emp.unused_daily_carryover or 0) - shortage)
    else:
        emp.hourly_leave_balance = new_balance
        if shortage > 0:
            emp.unused_hourly_carryover = max(0.0, float(emp.unused_hourly_carryover or 0) - shortage)
    return shortage


def restore_balance(emp: Employee, leave_type: str, amount: float) -> None:
    """Give back a previously deducted amount, capped at the monthly quota."""
    if leave_type == DAILY:
        quota = float(emp.monthly_daily_leave_quota or 0)
        emp.daily_leave_balance = min(quota, float(emp.daily_leave_balance or 0) + amount)
    else:
        quota = float(emp.monthly_hourly_leave_quota or 0)
        emp.hourly_leave_balance = min(quota, float(emp.hourly_leave_balance or 0) + amount)


def grant_initial_balance(emp: Employee, today: Optional[date] = None) -> None:
    """Give a newly approved employee the full quota for the current month."""
    emp.daily_leave_balance = float(emp.monthly_daily_leave_quota or 0)
    emp.hourly_leave_balance = float(emp.monthly_hourly_leave_quota or 0)
    emp.last_renewal_date = today or date.today()


# --------------------------------------------------------------------------
# Request state transitions
# --------------------------------------------------------------------------
@dataclass
class ActionResult:
    ok: bool
    request: Optional[LeaveRequest] = None
    error: str = ''
    needs_force: bool = False
    amount: float = 0.0
    shortage: float = 0.0
    unit: str = ''
    exceptional: bool = False


def _claim(req_id: int, new_status: str, approved_by: Optional[str]) -> bool:
    """Atomically move a pending request to ``new_status``. Returns False if someone else already did."""
    values = {LeaveRequest.status: new_status}
    if approved_by is not None:
        values[LeaveRequest.approved_by] = approved_by
    updated = (
        session.query(LeaveRequest)
        .filter(LeaveRequest.id == req_id, LeaveRequest.status == STATUS_PENDING)
        .update(values, synchronize_session=False)
    )
    return updated == 1


def approve_request(req_id: int, approver_name: str, force: bool = False) -> ActionResult:
    req = session.get(LeaveRequest, req_id)
    if not req or req.status != STATUS_PENDING:
        return ActionResult(ok=False, request=req, error="الطلب غير موجود أو تمت معالجته مسبقاً.")

    emp = req.employee
    amount, unit = request_amount(req)
    shortage = max(0.0, amount - current_balance(emp, req.leave_type))

    if shortage > 0 and not force:
        return ActionResult(
            ok=False, request=req, needs_force=True, amount=amount, shortage=shortage, unit=unit,
            error=(
                f"رصيد الموظف غير كافٍ (المطلوب {fmt(amount)} {unit}، النقص {fmt(shortage)} {unit}).\n"
                f"{balance_hint(emp)}\n"
                "يمكنك استخدام الموافقة الاستثنائية إذا رأيت ذلك مناسباً."
            ),
        )

    exceptional = shortage > 0
    approved_by = approver_name + (EXCEPTIONAL_SUFFIX if exceptional else '')
    try:
        if not _claim(req_id, STATUS_APPROVED, approved_by):
            session.rollback()
            return ActionResult(ok=False, request=req, error="تمت معالجة الطلب مسبقاً من قبل مدير آخر.")
        deduct_balance(emp, req.leave_type, amount, force=True)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(req)
    return ActionResult(ok=True, request=req, amount=amount, shortage=shortage, unit=unit, exceptional=exceptional)


def reject_request(req_id: int, approver_name: str) -> ActionResult:
    req = session.get(LeaveRequest, req_id)
    if not req or req.status != STATUS_PENDING:
        return ActionResult(ok=False, request=req, error="الطلب غير موجود أو تمت معالجته مسبقاً.")
    try:
        if not _claim(req_id, STATUS_REJECTED, approver_name):
            session.rollback()
            return ActionResult(ok=False, request=req, error="تمت معالجة الطلب مسبقاً من قبل مدير آخر.")
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(req)
    return ActionResult(ok=True, request=req)


def cancel_request(req_id: int, employee_id: int) -> ActionResult:
    """Employee cancels their own pending request."""
    req = session.get(LeaveRequest, req_id)
    if not req or req.employee_id != employee_id:
        return ActionResult(ok=False, request=req, error="الطلب غير موجود.")
    if req.status != STATUS_PENDING:
        return ActionResult(ok=False, request=req, error="لا يمكن إلغاء طلب تمت معالجته.")
    try:
        if not _claim(req_id, STATUS_CANCELLED, None):
            session.rollback()
            return ActionResult(ok=False, request=req, error="تمت معالجة الطلب مسبقاً.")
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(req)
    return ActionResult(ok=True, request=req)


def delete_request(req_id: int) -> Tuple[Optional[int], bool]:
    """
    Delete a request. If it was approved, the deducted amount is restored
    (capped at the monthly quota). Returns (employee_telegram_id, restored).
    """
    req = session.get(LeaveRequest, req_id)
    if not req:
        return None, False
    telegram_id = req.employee.telegram_id
    restored = False
    try:
        if req.status == STATUS_APPROVED:
            amount, _ = request_amount(req)
            restore_balance(req.employee, req.leave_type, amount)
            restored = True
        session.delete(req)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return telegram_id, restored


def create_approved_request(emp: Employee, leave_type, start_date, end_date, start_time, end_time,
                            reason, approver_name, force=False) -> ActionResult:
    """Admin adds a leave directly (already approved)."""
    amount, unit = requested_amount(leave_type, start_date, end_date, start_time, end_time)
    shortage = max(0.0, amount - current_balance(emp, leave_type))
    if shortage > 0 and not force:
        return ActionResult(
            ok=False, needs_force=True, amount=amount, shortage=shortage, unit=unit,
            error=f"رصيد الموظف غير كافٍ (المطلوب {fmt(amount)} {unit}، المتوفر {fmt(current_balance(emp, leave_type))} {unit}).\n{balance_hint(emp)}",
        )
    exceptional = shortage > 0
    try:
        deduct_balance(emp, leave_type, amount, force=True)
        req = LeaveRequest(
            employee_id=emp.id,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            reason=reason,
            status=STATUS_APPROVED,
            replacement_approval_status='not_required',
            approved_by=approver_name + (EXCEPTIONAL_SUFFIX if exceptional else ''),
        )
        session.add(req)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ActionResult(ok=True, request=req, amount=amount, shortage=shortage, unit=unit, exceptional=exceptional)


# --------------------------------------------------------------------------
# Monthly renewal (no accumulation)
# --------------------------------------------------------------------------
def _month_key(d: date):
    return (d.year, d.month)


def renew_monthly_balances(today: Optional[date] = None):
    """
    Reset the usable balance of every approved employee to the monthly quota
    once per calendar month. Unused balance is added to the informational
    carryover note. Idempotent: running it twice in one month is a no-op.

    Employees that have never been renewed under this policy (``last_renewal_date``
    is NULL) are capped at their quota; any excess from the old accumulating
    policy is moved to the note so nothing is silently lost.

    Returns the list of employees whose balance was changed.
    """
    today = today or date.today()
    renewed = []
    try:
        employees = session.query(Employee).filter_by(status='approved').all()
        for emp in employees:
            quota_d = float(emp.monthly_daily_leave_quota or 0)
            quota_h = float(emp.monthly_hourly_leave_quota or 0)
            bal_d = float(emp.daily_leave_balance or 0)
            bal_h = float(emp.hourly_leave_balance or 0)

            if emp.last_renewal_date is None:
                emp.unused_daily_carryover = float(emp.unused_daily_carryover or 0) + max(0.0, bal_d - quota_d)
                emp.unused_hourly_carryover = float(emp.unused_hourly_carryover or 0) + max(0.0, bal_h - quota_h)
                emp.daily_leave_balance = min(bal_d, quota_d) if bal_d > 0 else quota_d
                emp.hourly_leave_balance = min(bal_h, quota_h) if bal_h > 0 else quota_h
                emp.last_renewal_date = today
                renewed.append(emp)
            elif _month_key(emp.last_renewal_date) < _month_key(today):
                emp.unused_daily_carryover = float(emp.unused_daily_carryover or 0) + max(0.0, bal_d)
                emp.unused_hourly_carryover = float(emp.unused_hourly_carryover or 0) + max(0.0, bal_h)
                emp.daily_leave_balance = quota_d
                emp.hourly_leave_balance = quota_h
                emp.last_renewal_date = today
                renewed.append(emp)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return renewed


def pending_requests_for_review():
    """Pending requests that are ready for a manager decision."""
    from sqlalchemy import or_
    return (
        session.query(LeaveRequest)
        .filter(
            LeaveRequest.status == STATUS_PENDING,
            or_(
                LeaveRequest.replacement_approval_status == 'accepted',
                LeaveRequest.replacement_approval_status == 'not_required',
            ),
        )
        .order_by(LeaveRequest.id.asc())
        .all()
    )

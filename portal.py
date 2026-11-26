"""
Employee web portal.

A plain web link that works on any phone browser, so the system does not
depend on Telegram (which is blocked in some countries):

* employees register with their phone number and a password, then wait
  for a manager's approval;
* they see their balance, submit leave requests and work duties, choose a
  replacement, answer replacement requests and cancel pending requests;
* managers get a "manage" page to approve / reject requests, convert an
  over-balance request to unpaid leave, approve registrations and
  suspend / re-activate employees.

Business rules come from leave_logic, exactly as for the bot and the
admin dashboard. Telegram messages are still sent in the background to
anyone who has Telegram linked, but nothing depends on them.
"""
import hashlib
import logging
import threading
import time
from datetime import date
from functools import wraps

from flask import Blueprint, flash, g, redirect, render_template, request, session as web_session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import leave_logic as logic
import notifier
from database import Employee, LeaveRequest, session
from form_utils import MIN_PASSWORD_LENGTH, normalize_phone, parse_leave_period

logger = logging.getLogger(__name__)

bp = Blueprint('portal', __name__)


# --------------------------------------------------------------------------
# Login throttling (per IP + phone, in memory)
# --------------------------------------------------------------------------
MAX_FAILED_LOGINS = 5
FAILED_LOGIN_WINDOW = 15 * 60  # seconds
_failed_logins = {}
_failed_lock = threading.Lock()


def _client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    return (forwarded.split(',')[0].strip() if forwarded else request.remote_addr) or '-'


def _throttle_key(phone):
    return f"{_client_ip()}|{phone or '-'}"


def _is_throttled(key):
    now = time.time()
    with _failed_lock:
        recent = [t for t in _failed_logins.get(key, []) if now - t < FAILED_LOGIN_WINDOW]
        _failed_logins[key] = recent
        return len(recent) >= MAX_FAILED_LOGINS


def _record_failure(key):
    with _failed_lock:
        _failed_logins.setdefault(key, []).append(time.time())


def _clear_failures(key):
    with _failed_lock:
        _failed_logins.pop(key, None)


# --------------------------------------------------------------------------
# Session helpers
# --------------------------------------------------------------------------
def _password_marker(emp):
    """Changes whenever the password changes, so old sessions stop working."""
    return hashlib.sha256((emp.password_hash or '').encode()).hexdigest()[:16]


def _log_in(emp):
    web_session.permanent = True
    web_session['employee_id'] = emp.id
    web_session['pw'] = _password_marker(emp)


def _log_out():
    web_session.pop('employee_id', None)
    web_session.pop('pw', None)


def current_employee():
    emp_id = web_session.get('employee_id')
    if not emp_id:
        return None
    emp = session.get(Employee, emp_id)
    if not emp or emp.status != logic.EMP_APPROVED or web_session.get('pw') != _password_marker(emp):
        _log_out()
        return None
    return emp


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        emp = current_employee()
        if emp is None:
            return redirect(url_for('portal.login', next=request.path))
        g.employee = emp
        return view(*args, **kwargs)
    return wrapped


def manager_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not g.employee.is_manager:
            flash("هذه الصفحة للمدراء فقط.", "error")
            return redirect(url_for('portal.home'))
        return view(*args, **kwargs)
    return wrapped


def _safe_next(target):
    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return url_for('portal.home')


# --------------------------------------------------------------------------
# Notification helpers (Telegram is optional; failures never block the web flow)
# --------------------------------------------------------------------------
def _manager_telegram_ids(exclude_employee_id=None):
    managers = session.query(Employee).filter_by(is_manager=True, status=logic.EMP_APPROVED).all()
    return [m.telegram_id for m in managers if m.telegram_id and m.id != exclude_employee_id]


def _notify(telegram_ids, text):
    ids = [t for t in telegram_ids if t]
    if ids:
        notifier.run_in_background(notifier.send_many, ids, text)


def _finalize(req_id, text):
    notifier.run_in_background(notifier.finalize_leave_notifications, req_id, text)


@bp.app_context_processor
def inject_portal_globals():
    return {'portal_employee': getattr(g, 'employee', None)}


# --------------------------------------------------------------------------
# Public pages
# --------------------------------------------------------------------------
@bp.route('/')
def root():
    if web_session.get('employee_id') and current_employee():
        return redirect(url_for('portal.home'))
    return redirect(url_for('portal.login'))


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if web_session.get('employee_id') and current_employee():
            return redirect(url_for('portal.home'))
        return render_template('portal/login.html', phone='')

    raw_phone = request.form.get('phone', '')
    password = request.form.get('password', '')
    phone = normalize_phone(raw_phone)
    key = _throttle_key(phone)
    if _is_throttled(key):
        flash("محاولات كثيرة غير صحيحة. حاول مرة أخرى بعد 15 دقيقة.", "error")
        return render_template('portal/login.html', phone=raw_phone), 429

    emp = session.query(Employee).filter_by(phone=phone).first() if phone else None
    if not emp or not emp.password_hash or not check_password_hash(emp.password_hash, password):
        _record_failure(key)
        flash("رقم الهاتف أو كلمة المرور غير صحيحة.", "error")
        return render_template('portal/login.html', phone=raw_phone), 401

    _clear_failures(key)
    if emp.status == logic.EMP_PENDING:
        flash("حسابك بانتظار موافقة الإدارة. سيُفعَّل قريباً.", "warning")
        return render_template('portal/login.html', phone=raw_phone)
    if emp.status == logic.EMP_SUSPENDED:
        flash("تم إيقاف حسابك من قبل الإدارة. تواصل مع مديرك.", "error")
        return render_template('portal/login.html', phone=raw_phone)

    _log_in(emp)
    return redirect(_safe_next(request.args.get('next')))


@bp.route('/register', methods=['GET', 'POST'])
def register():
    form = request.form.to_dict() if request.method == 'POST' else {}
    if request.method == 'GET':
        return render_template('portal/register.html', form=form)

    full_name = (form.get('full_name') or '').strip()
    department = (form.get('department') or '').strip() or None
    phone = normalize_phone(form.get('phone'))
    password = form.get('password') or ''

    error = None
    if len(full_name) < 3:
        error = "يرجى إدخال الاسم الكامل."
    elif not phone:
        error = "رقم الهاتف غير صحيح. مثال: 07701234567"
    elif len(password) < MIN_PASSWORD_LENGTH:
        error = f"كلمة المرور يجب أن تكون {MIN_PASSWORD_LENGTH} أحرف أو أرقام على الأقل."
    elif password != (form.get('password2') or ''):
        error = "كلمتا المرور غير متطابقتين."
    elif session.query(Employee).filter_by(phone=phone).first():
        error = "رقم الهاتف مسجل مسبقاً. استخدم صفحة الدخول، أو تواصل مع الإدارة إذا نسيت كلمة المرور."
    if error:
        flash(error, "error")
        return render_template('portal/register.html', form=form)

    is_first = session.query(Employee).count() == 0
    emp = Employee(
        full_name=full_name,
        department=department,
        phone=phone,
        password_hash=generate_password_hash(password),
        is_manager=is_first,
        status=logic.EMP_APPROVED if is_first else logic.EMP_PENDING,
    )
    if is_first:
        logic.grant_initial_balance(emp)
    try:
        session.add(emp)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error("Registration failed: %s", e)
        flash("تعذّر إكمال التسجيل. حاول مرة أخرى.", "error")
        return render_template('portal/register.html', form=form)

    if is_first:
        _log_in(emp)
        flash("تم تسجيلك كأول مستخدم وتعيينك مديراً للنظام.", "success")
        return redirect(url_for('portal.home'))

    _notify(_manager_telegram_ids(),
            f"👤 تسجيل جديد من بوابة الويب ينتظر الموافقة:\nالاسم: {full_name}\nالقسم: {department or '-'}\nالهاتف: {phone}")
    flash("تم إرسال طلب التسجيل للإدارة. ستتمكن من الدخول بعد الموافقة.", "success")
    return redirect(url_for('portal.login'))


@bp.route('/logout', methods=['POST'])
def logout():
    _log_out()
    flash("تم تسجيل الخروج.", "success")
    return redirect(url_for('portal.login'))


# --------------------------------------------------------------------------
# Employee pages
# --------------------------------------------------------------------------
@bp.route('/me')
@login_required
def home():
    me = g.employee
    my_requests = (
        session.query(LeaveRequest).filter_by(employee_id=me.id)
        .order_by(LeaveRequest.id.desc()).limit(15).all()
    )
    replacement_asks = (
        session.query(LeaveRequest)
        .filter(
            LeaveRequest.replacement_employee_id == me.id,
            LeaveRequest.status == logic.STATUS_PENDING,
            LeaveRequest.replacement_approval_status == 'pending',
        )
        .order_by(LeaveRequest.start_date).all()
    )
    pending_reviews = 0
    if me.is_manager:
        pending_reviews = sum(1 for r in logic.pending_requests_for_review() if r.employee_id != me.id)
        pending_reviews += session.query(Employee).filter_by(status=logic.EMP_PENDING).count()
    return render_template(
        'portal/home.html', me=me, my_requests=my_requests,
        replacement_asks=replacement_asks, pending_reviews=pending_reviews,
    )


@bp.route('/me/new', methods=['GET', 'POST'])
@login_required
def new_request():
    me = g.employee
    others = (
        session.query(Employee)
        .filter(Employee.status == logic.EMP_APPROVED, Employee.id != me.id)
        .order_by(Employee.full_name).all()
    )
    form = request.form.to_dict() if request.method == 'POST' else {'leave_type': request.args.get('type', logic.DAILY)}

    def again(message=None, shortage=None, status=200):
        if message:
            flash(message, "error")
        return render_template('portal/new.html', me=me, others=others, form=form, shortage=shortage), status

    if request.method == 'GET':
        return again()

    try:
        leave_type, start_date, end_date, start_time, end_time = parse_leave_period(request.form)
    except ValueError as e:
        return again(str(e))

    if start_date < date.today():
        return again("لا يمكن أن يكون التاريخ في الماضي.")
    if leave_type in (logic.DAILY, logic.UNPAID) and not start_time and logic.calculate_leave_days(start_date, end_date) == 0:
        return again("الفترة المختارة لا تحتوي أي يوم عمل.")
    if leave_type == logic.HOURLY and not logic.is_working_day(start_date):
        return again("هذا اليوم عطلة. اختر يوم عمل آخر.")
    reason = (form.get('reason') or '').strip()
    if not reason:
        return again("يرجى كتابة السبب أو وصف المهمة.")
    if logic.has_overlapping_leave(me.id, start_date, end_date):
        return again("لديك طلب آخر (قيد الانتظار أو معتمد) في نفس الفترة أو يتداخل معها.")

    amount, shortage, unit = logic.shortage_for(me, leave_type, start_date, end_date, start_time, end_time)
    if shortage > 0:
        if form.get('convert_unpaid') != '1':
            return again(shortage={
                'amount': amount, 'shortage': shortage, 'unit': unit,
                'balance': logic.current_balance(me, leave_type),
            })
        leave_type = logic.UNPAID

    replacement = None
    if leave_type != logic.WORK_DUTY and form.get('replacement_id'):
        try:
            replacement = session.get(Employee, int(form['replacement_id']))
        except ValueError:
            replacement = None
        if not replacement or replacement.status != logic.EMP_APPROVED or replacement.id == me.id:
            return again("الموظف البديل غير متاح. اختر موظفاً آخر.")

    req = LeaveRequest(
        employee_id=me.id,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        reason=reason,
        status=logic.STATUS_PENDING,
        replacement_employee_id=replacement.id if replacement else None,
        replacement_approval_status='pending' if replacement else 'not_required',
    )
    try:
        session.add(req)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error("Could not save request: %s", e)
        return again("تعذّر حفظ الطلب. حاول مرة أخرى.")

    details = logic.leave_details(req)
    if replacement:
        _notify([replacement.telegram_id],
                f"طلب بديل: {me.full_name} يطلب أن تكون بديلاً له في إجازته {details}. افتح بوابة الإجازات للرد.")
        flash(f"تم إرسال الطلب #{req.id} إلى {replacement.full_name} للموافقة كبديل، ثم يُرفع للإدارة.", "success")
    else:
        _notify(_manager_telegram_ids(exclude_employee_id=me.id),
                f"📩 طلب جديد #{req.id} من بوابة الويب\nالموظف: {me.full_name}\nالنوع: {req.leave_type}\nالتفاصيل: {details}\nالسبب: {reason}")
        if leave_type == logic.WORK_DUTY:
            flash(f"تم تسجيل مهمة العمل #{req.id} وإرسالها للإدارة. لا تُخصم من رصيدك.", "success")
        else:
            flash(f"تم تقديم الطلب #{req.id} وهو الآن قيد المراجعة.", "success")
    return redirect(url_for('portal.home'))


@bp.route('/me/cancel/<int:req_id>', methods=['POST'])
@login_required
def cancel(req_id):
    me = g.employee
    result = logic.cancel_request(req_id, me.id)
    if not result.ok:
        flash(result.error, "error")
        return redirect(url_for('portal.home'))
    flash(f"تم إلغاء الطلب #{req_id}.", "success")
    _finalize(req_id, f"↩️ ألغى {me.full_name} الطلب #{req_id} ({logic.leave_details(result.request)}).")
    return redirect(url_for('portal.home'))


@bp.route('/me/replacement/<int:req_id>/<action>', methods=['POST'])
@login_required
def replacement_answer(req_id, action):
    me = g.employee
    req = session.get(LeaveRequest, req_id)
    if (not req or req.replacement_employee_id != me.id or req.status != logic.STATUS_PENDING
            or req.replacement_approval_status != 'pending' or action not in ('accept', 'reject')):
        flash("هذا الطلب غير متاح أو تمت معالجته.", "error")
        return redirect(url_for('portal.home'))

    requester = req.employee
    if action == 'accept':
        req.replacement_approval_status = 'accepted'
        session.commit()
        flash(f"شكراً، تم قبولك كبديل لـ {requester.full_name}. رُفع الطلب للإدارة.", "success")
        _notify([requester.telegram_id], f"وافق {me.full_name} على أن يكون بديلاً لك. تم رفع الطلب #{req.id} للإدارة.")
        _notify(_manager_telegram_ids(exclude_employee_id=requester.id),
                f"📩 طلب جديد #{req.id}\nالموظف: {requester.full_name}\nالنوع: {req.leave_type}\n"
                f"التفاصيل: {logic.leave_details(req)}\nالبديل: {me.full_name} (وافق)")
    else:
        req.replacement_approval_status = 'rejected'
        req.status = logic.STATUS_REJECTED
        req.approved_by = f"البديل {me.full_name} رفض"
        session.commit()
        flash("تم رفض طلب البديل.", "success")
        _notify([requester.telegram_id], f"رفض {me.full_name} طلب البديل. أُلغي الطلب #{req.id}، يمكنك تقديم طلب جديد ببديل آخر.")
    return redirect(url_for('portal.home'))


@bp.route('/me/password', methods=['GET', 'POST'])
@login_required
def change_password():
    me = g.employee
    if request.method == 'POST':
        current = request.form.get('current_password', '')
        new = request.form.get('new_password', '')
        if not me.password_hash or not check_password_hash(me.password_hash, current):
            flash("كلمة المرور الحالية غير صحيحة.", "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(f"كلمة المرور الجديدة يجب أن تكون {MIN_PASSWORD_LENGTH} أحرف أو أرقام على الأقل.", "error")
        elif new != request.form.get('new_password2', ''):
            flash("كلمتا المرور الجديدتان غير متطابقتين.", "error")
        else:
            me.password_hash = generate_password_hash(new)
            session.commit()
            _log_in(me)  # refresh the marker so this device stays signed in, others are signed out
            flash("تم تغيير كلمة المرور.", "success")
            return redirect(url_for('portal.home'))
    return render_template('portal/password.html', me=me)


# --------------------------------------------------------------------------
# Manager pages
# --------------------------------------------------------------------------
@bp.route('/me/manage')
@manager_required
def manage():
    me = g.employee
    reviews = []
    for req in logic.pending_requests_for_review():
        if req.employee_id == me.id:
            continue
        amount, shortage, unit = logic.shortage_for(
            req.employee, req.leave_type, req.start_date, req.end_date, req.start_time, req.end_time)
        reviews.append({'req': req, 'amount': amount, 'shortage': shortage, 'unit': unit})
    pending_users = session.query(Employee).filter_by(status=logic.EMP_PENDING).order_by(Employee.id).all()
    employees = (
        session.query(Employee).filter(Employee.status != logic.EMP_PENDING)
        .order_by(Employee.status, Employee.department, Employee.full_name).all()
    )
    own_pending = session.query(LeaveRequest).filter_by(employee_id=me.id, status=logic.STATUS_PENDING).count()
    return render_template('portal/manage.html', me=me, reviews=reviews, pending_users=pending_users,
                           employees=employees, own_pending=own_pending)


@bp.route('/me/manage/request/<int:req_id>/<action>', methods=['POST'])
@manager_required
def decide(req_id, action):
    me = g.employee
    req = session.get(LeaveRequest, req_id)
    if not req:
        flash("الطلب غير موجود.", "error")
        return redirect(url_for('portal.manage'))
    if req.employee_id == me.id:
        flash("لا يمكنك اتخاذ قرار على طلبك أنت. يقرره مدير آخر أو لوحة الإدارة.", "error")
        return redirect(url_for('portal.manage'))

    if action in ('approve', 'unpaid'):
        result = logic.approve_request(req_id, me.full_name, convert_to_unpaid=(action == 'unpaid'))
        if not result.ok:
            flash(result.error, "warning" if result.needs_unpaid else "error")
            return redirect(url_for('portal.manage'))
        req = result.request
        emp = req.employee
        if result.converted_to_unpaid:
            note = " (تم تحويلها إلى إجازة بدون راتب)"
        elif req.leave_type == logic.WORK_DUTY:
            note = " (مهمة عمل)"
        elif req.leave_type == logic.UNPAID:
            note = " (بدون راتب)"
        else:
            note = ""
        flash(f"تمت الموافقة على الطلب #{req_id} للموظف {emp.full_name}{note}.", "success")
        _notify([emp.telegram_id], f"✅ تمت الموافقة على طلبك #{req_id} من قبل {me.full_name}{note}.\nالتفاصيل: {logic.leave_details(req)}")
        _finalize(req_id, f"✅ وافق {me.full_name} على الطلب #{req_id} للموظف {emp.full_name}{note}.\nالتفاصيل: {logic.leave_details(req)}")
    elif action == 'reject':
        result = logic.reject_request(req_id, me.full_name)
        if not result.ok:
            flash(result.error, "error")
            return redirect(url_for('portal.manage'))
        emp = result.request.employee
        flash(f"تم رفض الطلب #{req_id} للموظف {emp.full_name}.", "success")
        _notify([emp.telegram_id], f"❌ تم رفض طلبك #{req_id} من قبل {me.full_name}.")
        _finalize(req_id, f"❌ رفض {me.full_name} الطلب #{req_id} للموظف {emp.full_name}.")
    else:
        flash("إجراء غير صالح.", "error")
    return redirect(url_for('portal.manage'))


@bp.route('/me/manage/user/<int:user_id>/<action>', methods=['POST'])
@manager_required
def manage_user(user_id, action):
    me = g.employee
    target = session.get(Employee, user_id)
    if not target:
        flash("الموظف غير موجود.", "error")
        return redirect(url_for('portal.manage'))

    if action == 'approve':
        if target.status != logic.EMP_PENDING:
            flash("هذا الحساب ليس بانتظار الموافقة.", "error")
        else:
            res = logic.set_employee_status(user_id, logic.EMP_APPROVED, acting_emp_id=me.id)
            flash(f"تمت الموافقة على {target.full_name}." if res.ok else res.error, "success" if res.ok else "error")
    elif action == 'reject':
        ok, msg, _tid = logic.delete_employee(user_id, acting_emp_id=me.id)
        flash(msg if ok else msg, "success" if ok else "error")
    elif action in ('suspend', 'activate'):
        new_status = logic.EMP_SUSPENDED if action == 'suspend' else logic.EMP_APPROVED
        res = logic.set_employee_status(user_id, new_status, acting_emp_id=me.id)
        label = "تم إيقاف الحساب." if action == 'suspend' else "تم تفعيل الحساب."
        flash(label if res.ok else res.error, "success" if res.ok else "error")
    else:
        flash("إجراء غير صالح.", "error")
    return redirect(url_for('portal.manage'))

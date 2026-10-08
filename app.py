import logging
import os
import secrets
import threading
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

from datetime import datetime  # noqa: E402
from flask import (  # noqa: E402
    Flask, render_template, redirect, url_for, request, Response, flash, abort,
    session as flask_session,
)

from database import session, LeaveRequest, Employee, Holiday, NotificationLog  # noqa: E402
import leave_logic as logic  # noqa: E402
import notifier  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("FLASK_SECRET_KEY"):
    logger.warning("FLASK_SECRET_KEY not set: a random key is used, web sessions reset on restart.")

WEB_ADMIN_LABEL = "الإدارة (Web)"


@app.teardown_appcontext
def shutdown_session(exception=None):
    session.remove()


# --- Background threads (bot + scheduler) ---
token = os.getenv("TELEGRAM_BOT_TOKEN")
RUN_BACKGROUND = os.getenv("RUN_BOT_IN_WEB", "1") not in ("0", "false", "False")

if token and RUN_BACKGROUND:
    def start_bot():
        import asyncio
        import time
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        time.sleep(2)  # let a previous instance release polling
        import bot as bot_module
        try:
            bot_module.main()
        except Exception as e:
            logger.error("Error running bot: %s", e)

    def start_scheduler():
        from scheduler import run_scheduler
        run_scheduler()

    # Note: Procfile runs gunicorn with a single worker so that polling is not duplicated.
    threading.Thread(target=start_bot, daemon=True, name="telegram-bot").start()
    logger.info("Telegram bot started in background thread.")
    threading.Thread(target=start_scheduler, daemon=True, name="scheduler").start()
    logger.info("Scheduler started in background thread.")


# --- Basic Authentication ---
def check_auth(username, password):
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "secret")
    return secrets.compare_digest(username or "", admin_user) and secrets.compare_digest(password or "", admin_pass)


if not os.getenv("ADMIN_PASSWORD"):
    logger.warning("ADMIN_PASSWORD not set: the dashboard uses the default password. Set it in the environment!")


def authenticate():
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


# --- CSRF protection for all POST forms ---
def generate_csrf_token():
    if '_csrf_token' not in flask_session:
        flask_session['_csrf_token'] = secrets.token_hex(16)
    return flask_session['_csrf_token']


app.jinja_env.globals['csrf_token'] = generate_csrf_token
app.jinja_env.globals['fmt'] = logic.fmt
app.jinja_env.globals['STATUS_LABELS'] = logic.STATUS_LABELS
app.jinja_env.globals['LEAVE_TYPES'] = logic.LEAVE_TYPES
app.jinja_env.globals['UNPAID'] = logic.UNPAID
app.jinja_env.globals['EMP_STATUS_LABELS'] = logic.EMP_STATUS_LABELS


@app.before_request
def csrf_protect():
    if request.method == 'POST':
        token_in_session = flask_session.get('_csrf_token')
        token_in_form = request.form.get('_csrf_token')
        if not token_in_session or not token_in_form or not secrets.compare_digest(token_in_session, token_in_form):
            abort(400, description="CSRF token missing or invalid.")


# --- Notification helpers (kept as thin wrappers so tests can patch them) ---
def send_notification(telegram_id, message):
    return notifier.send_message(telegram_id, message)


def finalize_leave_notifications(req_id, text):
    return notifier.finalize_leave_notifications(req_id, text)


# --- Form parsing helpers ---
def parse_date(value, field_label):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValueError(f"صيغة {field_label} غير صالحة (YYYY-MM-DD).")


def parse_time(value, field_label):
    try:
        return datetime.strptime(value, '%H:%M').time()
    except (TypeError, ValueError):
        raise ValueError(f"صيغة {field_label} غير صالحة (HH:MM).")


def parse_float(value, field_label, minimum=0.0):
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_label} يجب أن يكون رقماً.")
    if f < minimum:
        raise ValueError(f"{field_label} لا يمكن أن يكون أقل من {logic.fmt(minimum)}.")
    return f


def parse_leave_period(form):
    """Returns (leave_type, start_date, end_date, start_time, end_time) or raises ValueError."""
    leave_type = form.get('leave_type')
    if leave_type not in logic.LEAVE_TYPES:
        raise ValueError("نوع الإجازة غير صالح.")
    start_date = parse_date(form.get('start_date'), "تاريخ البدء")
    if leave_type in (logic.DAILY, logic.UNPAID):
        end_date = parse_date(form.get('end_date') or form.get('start_date'), "تاريخ الانتهاء")
        if end_date < start_date:
            raise ValueError("تاريخ الانتهاء لا يمكن أن يكون قبل تاريخ البدء.")
        return leave_type, start_date, end_date, None, None
    start_time = parse_time(form.get('start_time'), "وقت البدء")
    end_time = parse_time(form.get('end_time'), "وقت الانتهاء")
    if end_time <= start_time:
        raise ValueError("وقت الانتهاء يجب أن يكون بعد وقت البدء.")
    return leave_type, start_date, start_date, start_time, end_time


# Backwards-compatible aliases used by older code/tests
calculate_leave_days = logic.calculate_leave_days
calculate_leave_hours = logic.calculate_leave_hours


# --- Health Check (for UptimeRobot) ---
@app.route('/health')
def health():
    return {"status": "ok", "bot": "running" if token else "disabled"}, 200


# --- Main Routes ---
@app.route('/')
@requires_auth
def index():
    all_requests = session.query(LeaveRequest).order_by(LeaveRequest.id.desc()).all()
    return render_template('index.html', requests=all_requests, balance_hint=logic.balance_hint)


@app.route('/approve/<int:request_id>', methods=['POST'])
@requires_auth
def approve_request(request_id):
    convert = request.form.get('convert_unpaid') == '1'
    result = logic.approve_request(request_id, WEB_ADMIN_LABEL, convert_to_unpaid=convert)
    if not result.ok:
        flash(result.error, "warning" if result.needs_unpaid else "error")
        return redirect(url_for('index'))

    req = result.request
    employee = req.employee
    if result.converted_to_unpaid:
        note = " (تم تحويلها إلى إجازة بدون راتب)"
    elif req.leave_type == logic.UNPAID:
        note = " (إجازة بدون راتب)"
    else:
        note = ""
    flash(f"تمت الموافقة على الطلب #{request_id}{note}.", "success")
    send_notification(
        employee.telegram_id,
        f"✅ تمت الموافقة على طلب الإجازة الخاص بك (ID: {request_id}) من قبل {WEB_ADMIN_LABEL}{note}.\n"
        f"التفاصيل: {logic.leave_details(req)}\nرصيدك المتبقي: {logic.fmt(employee.daily_leave_balance)} يوم | {logic.fmt(employee.hourly_leave_balance)} ساعة",
    )
    finalize_leave_notifications(
        request_id,
        f"✅ تمت الموافقة على طلب الإجازة (ID: {request_id}) للموظف {employee.full_name} من قبل {WEB_ADMIN_LABEL}{note}.\n"
        f"التفاصيل: {logic.leave_details(req)}",
    )
    return redirect(url_for('index'))


@app.route('/reject/<int:request_id>', methods=['POST'])
@requires_auth
def reject_request(request_id):
    result = logic.reject_request(request_id, WEB_ADMIN_LABEL)
    if not result.ok:
        flash(result.error, "error")
        return redirect(url_for('index'))
    req = result.request
    flash(f"تم رفض الطلب #{request_id}.", "success")
    send_notification(req.employee.telegram_id, f"❌ تم رفض طلب الإجازة الخاص بك (ID: {request_id}) من قبل {WEB_ADMIN_LABEL}.")
    finalize_leave_notifications(
        request_id,
        f"❌ تم رفض طلب الإجازة (ID: {request_id}) للموظف {req.employee.full_name} من قبل {WEB_ADMIN_LABEL}.\n"
        f"التفاصيل: {logic.leave_details(req)}",
    )
    return redirect(url_for('index'))


@app.route('/delete/<int:request_id>', methods=['POST'])
@requires_auth
def delete_request(request_id):
    telegram_id, restored = logic.delete_request(request_id)
    if telegram_id is None:
        flash("الطلب غير موجود.", "error")
        return redirect(url_for('index'))
    flash("تم حذف الطلب بنجاح." + (" وتمت استعادة الرصيد المخصوم." if restored else ""), "success")
    send_notification(telegram_id, f"تم حذف طلب الإجازة رقم {request_id} من قبل الإدارة." + (" تمت استعادة الرصيد المخصوم." if restored else ""))
    session.query(NotificationLog).filter_by(request_type='leave', target_id=request_id).delete()
    session.commit()
    return redirect(url_for('index'))


@app.route('/bulk_delete_requests', methods=['POST'])
@requires_auth
def bulk_delete_requests():
    request_ids = request.form.getlist('request_ids')
    if not request_ids:
        flash("لم يتم تحديد أي طلبات للحذف.", "error")
        return redirect(url_for('index'))
    deleted = 0
    restored_count = 0
    for req_id in request_ids:
        try:
            telegram_id, restored = logic.delete_request(int(req_id))
        except ValueError:
            continue
        if telegram_id is not None:
            deleted += 1
            restored_count += 1 if restored else 0
            session.query(NotificationLog).filter_by(request_type='leave', target_id=int(req_id)).delete()
    session.commit()
    msg = f"تم حذف {deleted} طلب(ات) بنجاح."
    if restored_count:
        msg += f" تمت استعادة الرصيد لـ {restored_count} طلب(ات) معتمدة."
    flash(msg, "success")
    return redirect(url_for('index'))


@app.route('/edit_request/<int:request_id>', methods=['GET', 'POST'])
@requires_auth
def edit_request(request_id):
    leave_request = session.get(LeaveRequest, request_id)
    if not leave_request:
        flash("الطلب غير موجود.", "error")
        return redirect(url_for('index'))

    if request.method == 'POST':
        if leave_request.status != logic.STATUS_PENDING:
            flash("يمكن تعديل الطلبات قيد الانتظار فقط. احذف الطلب وأضفه من جديد إذا لزم.", "error")
            return redirect(url_for('index'))
        try:
            leave_type, start_date, end_date, start_time, end_time = parse_leave_period(request.form)
        except ValueError as e:
            flash(str(e), "error")
            return render_template('edit_request.html', request=leave_request)

        leave_request.leave_type = leave_type
        leave_request.start_date = start_date
        leave_request.end_date = end_date
        leave_request.start_time = start_time
        leave_request.end_time = end_time
        leave_request.reason = (request.form.get('reason') or '').strip()
        session.commit()

        flash("تم تعديل الطلب بنجاح.", "success")
        send_notification(
            leave_request.employee.telegram_id,
            f"تنبيه: قام المسؤول بتعديل طلب الإجازة الخاص بك (ID: {request_id}).\nالتفاصيل الجديدة: {logic.leave_details(leave_request)}",
        )
        return redirect(url_for('index'))

    return render_template('edit_request.html', request=leave_request)


@app.route('/employees')
@requires_auth
def manage_employees():
    all_employees = session.query(Employee).order_by(Employee.id).all()
    return render_template('employees.html', employees=all_employees)


@app.route('/update_user/<int:user_id>', methods=['POST'])
@requires_auth
def update_user(user_id):
    user = session.get(Employee, user_id)
    if not user:
        flash("الموظف غير موجود.", "error")
        return redirect(url_for('manage_employees'))
    try:
        full_name = (request.form.get('full_name') or '').strip()
        if not full_name:
            raise ValueError("الاسم الكامل مطلوب.")
        daily_quota = parse_float(request.form.get('daily_quota', user.monthly_daily_leave_quota), "الحصة الشهرية (أيام)")
        hourly_quota = parse_float(request.form.get('hourly_quota', user.monthly_hourly_leave_quota), "الحصة الشهرية (ساعات)")
        daily_balance = parse_float(request.form.get('daily_balance', user.daily_leave_balance), "رصيد الأيام")
        hourly_balance = parse_float(request.form.get('hourly_balance', user.hourly_leave_balance), "رصيد الساعات")
        unused_d = parse_float(request.form.get('unused_daily', user.unused_daily_carryover or 0), "غير المستخدم (أيام)")
        unused_h = parse_float(request.form.get('unused_hourly', user.unused_hourly_carryover or 0), "غير المستخدم (ساعات)")
        if daily_balance > daily_quota or hourly_balance > hourly_quota:
            raise ValueError("الرصيد الحالي لا يمكن أن يتجاوز الحصة الشهرية (لا يوجد تراكم).")
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for('manage_employees'))

    user.full_name = full_name
    user.department = (request.form.get('department') or '').strip() or None
    user.is_manager = 'is_manager' in request.form
    user.monthly_daily_leave_quota = daily_quota
    user.monthly_hourly_leave_quota = hourly_quota
    user.daily_leave_balance = daily_balance
    user.hourly_leave_balance = hourly_balance
    user.unused_daily_carryover = unused_d
    user.unused_hourly_carryover = unused_h
    session.commit()
    flash(f"تم تحديث بيانات الموظف {user.full_name} بنجاح.", "success")
    return redirect(url_for('manage_employees'))


@app.route('/approve_user/<int:user_id>', methods=['POST'])
@requires_auth
def approve_user_web(user_id):
    user = session.get(Employee, user_id)
    if user and user.status == 'pending':
        user.status = 'approved'
        logic.grant_initial_balance(user)
        session.commit()
        flash(f"تمت الموافقة على الموظف {user.full_name}.", "success")
        send_notification(user.telegram_id, "تهانينا! تمت الموافقة على حسابك. يمكنك الآن استخدام الأمر /start للبدء.")
        session.query(NotificationLog).filter_by(request_type='user', target_id=user_id).delete()
        session.commit()
    return redirect(url_for('manage_employees'))


@app.route('/reject_user/<int:user_id>', methods=['POST'])
@requires_auth
def reject_user_web(user_id):
    user = session.get(Employee, user_id)
    if not user:
        flash("الموظف غير موجود.", "error")
        return redirect(url_for('manage_employees'))
    was_pending = user.status == logic.EMP_PENDING
    ok, msg, tid = logic.delete_employee(user_id)
    flash(msg, "success" if ok else "error")
    if ok and tid:
        send_notification(tid, "نأسف، تم رفض طلب تسجيلك." if was_pending else "تم حذف حسابك من نظام الإجازات من قبل الإدارة.")
    return redirect(url_for('manage_employees'))


@app.route('/suspend_user/<int:user_id>', methods=['POST'])
@requires_auth
def suspend_user_web(user_id):
    res = logic.set_employee_status(user_id, logic.EMP_SUSPENDED)
    flash("تم إيقاف حساب الموظف." if res.ok else res.error, "success" if res.ok else "error")
    if res.ok:
        user = session.get(Employee, user_id)
        send_notification(user.telegram_id, "تم إيقاف حسابك في نظام الإجازات من قبل الإدارة.")
    return redirect(url_for('manage_employees'))


@app.route('/activate_user/<int:user_id>', methods=['POST'])
@requires_auth
def activate_user_web(user_id):
    res = logic.set_employee_status(user_id, logic.EMP_APPROVED)
    flash("تم تفعيل حساب الموظف." if res.ok else res.error, "success" if res.ok else "error")
    if res.ok:
        user = session.get(Employee, user_id)
        send_notification(user.telegram_id, "تم تفعيل حسابك في نظام الإجازات. اضغط /start للبدء.")
    return redirect(url_for('manage_employees'))


@app.route('/reset_all', methods=['POST'])
@requires_auth
def reset_all_web():
    if request.form.get('confirm') != 'RESET':
        flash("لم يتم التأكيد. اكتب RESET في حقل التأكيد.", "error")
        return redirect(url_for('manage_employees'))
    deleted = logic.reset_everything()
    flash(f"تم حذف {deleted} طلب(ات) وإعادة كل الأرصدة إلى الحصة الشهرية.", "success")
    return redirect(url_for('manage_employees'))


@app.route('/add_user', methods=['POST'])
@requires_auth
def add_user_web():
    telegram_id = (request.form.get('telegram_id') or '').strip()
    full_name = (request.form.get('full_name') or '').strip()
    department = (request.form.get('department') or '').strip() or None
    is_manager = 'is_manager' in request.form

    if not telegram_id or not full_name:
        flash("يرجى ملء جميع الحقول المطلوبة (Telegram ID والاسم).", "error")
        return redirect(url_for('manage_employees'))

    try:
        telegram_id = int(telegram_id)
    except ValueError:
        flash("Telegram ID يجب أن يكون رقماً.", "error")
        return redirect(url_for('manage_employees'))

    existing = session.query(Employee).filter_by(telegram_id=telegram_id).first()
    if existing:
        flash(f"الموظف بـ ID {telegram_id} موجود مسبقاً باسم {existing.full_name}.", "error")
        return redirect(url_for('manage_employees'))

    try:
        new_emp = Employee(
            telegram_id=telegram_id,
            full_name=full_name,
            department=department,
            status='approved',
            is_manager=is_manager,
        )
        logic.grant_initial_balance(new_emp)
        session.add(new_emp)
        session.commit()
        flash(f"تم إضافة الموظف {full_name} بنجاح.", "success")
    except Exception as e:
        session.rollback()
        flash(f"حدث خطأ أثناء الإضافة: {e}", "error")
    return redirect(url_for('manage_employees'))


@app.route('/holidays')
@requires_auth
def manage_holidays():
    all_holidays = session.query(Holiday).order_by(Holiday.date.asc()).all()
    return render_template('holidays.html', holidays=all_holidays)


@app.route('/add_holiday', methods=['POST'])
@requires_auth
def add_holiday():
    name = (request.form.get('name') or '').strip()
    try:
        if not name:
            raise ValueError("اسم العطلة مطلوب.")
        date_obj = parse_date(request.form.get('date'), "تاريخ العطلة")
        if session.query(Holiday).filter_by(date=date_obj).first():
            raise ValueError("يوجد عطلة مسجلة بهذا التاريخ مسبقاً.")
        session.add(Holiday(name=name, date=date_obj))
        session.commit()
        flash(f"تمت إضافة '{name}' إلى قائمة العطلات.", "success")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        session.rollback()
        flash(f"فشل في إضافة العطلة: {e}", "error")
    return redirect(url_for('manage_holidays'))


@app.route('/delete_holiday/<int:holiday_id>', methods=['POST'])
@requires_auth
def delete_holiday(holiday_id):
    holiday = session.get(Holiday, holiday_id)
    if holiday:
        session.delete(holiday)
        session.commit()
        flash("تم حذف العطلة بنجاح.", "success")
    return redirect(url_for('manage_holidays'))


def _filtered_requests(args):
    query = session.query(LeaveRequest)
    employee_id = args.get('employee_id')
    start_date = args.get('start_date')
    end_date = args.get('end_date')
    leave_status = args.get('status')

    if employee_id:
        try:
            query = query.filter(LeaveRequest.employee_id == int(employee_id))
        except ValueError:
            pass
    if start_date:
        try:
            query = query.filter(LeaveRequest.start_date >= parse_date(start_date, "تاريخ البدء"))
        except ValueError:
            pass
    if end_date:
        try:
            query = query.filter(LeaveRequest.end_date <= parse_date(end_date, "تاريخ الانتهاء"))
        except ValueError:
            pass
    if leave_status:
        query = query.filter(LeaveRequest.status == leave_status)
    return query.order_by(LeaveRequest.start_date.desc()).all()


@app.route('/reports', methods=['GET', 'POST'])
@requires_auth
def reports():
    employees = session.query(Employee).order_by(Employee.full_name).all()
    source = request.form if request.method == 'POST' else request.args
    results = _filtered_requests(source)
    return render_template('reports.html', results=results, employees=employees, filters=source)


@app.route('/export_reports', methods=['GET'])
@requires_auth
def export_reports():
    import csv
    import io
    from flask import make_response

    results = _filtered_requests(request.args)

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'الموظف', 'القسم', 'النوع', 'تاريخ البدء', 'تاريخ الانتهاء', 'وقت البدء', 'وقت الانتهاء',
                 'المدة', 'السبب', 'الحالة', 'البديل', 'تمت المعالجة بواسطة'])
    holidays = logic.get_holidays()
    for req in results:
        replacement_name = req.replacement_employee.full_name if req.replacement_employee else "لا يوجد"
        if req.start_time and req.end_time:
            duration = f"{logic.fmt(logic.calculate_leave_hours(req.start_time, req.end_time))} ساعة"
        else:
            duration = f"{logic.calculate_leave_days(req.start_date, req.end_date, holidays)} يوم"
        cw.writerow([
            req.id,
            req.employee.full_name,
            req.employee.department or '',
            req.leave_type,
            req.start_date,
            req.end_date,
            req.start_time.strftime('%H:%M') if req.start_time else "",
            req.end_time.strftime('%H:%M') if req.end_time else "",
            duration,
            req.reason,
            logic.STATUS_LABELS.get(req.status, req.status),
            replacement_name,
            req.approved_by or '',
        ])

    output = make_response(si.getvalue().encode('utf-8-sig'))  # utf-8-sig for Excel compatibility
    output.headers["Content-Disposition"] = "attachment; filename=leave_reports.csv"
    output.headers["Content-type"] = "text/csv; charset=utf-8"
    return output


@app.route('/admin/add_leave', methods=['GET', 'POST'])
@requires_auth
def admin_add_leave():
    employees = session.query(Employee).filter_by(status='approved').order_by(Employee.full_name).all()

    if request.method == 'POST':
        try:
            employee = session.get(Employee, int(request.form.get('employee_id') or 0))
            if not employee:
                raise ValueError("يرجى اختيار الموظف.")
            leave_type, start_date, end_date, start_time, end_time = parse_leave_period(request.form)
        except ValueError as e:
            flash(str(e), "error")
            return render_template('admin_add_leave.html', employees=employees)

        reason = (request.form.get('reason') or '').strip()

        result = logic.create_approved_request(
            employee, leave_type, start_date, end_date, start_time, end_time,
            reason=f"{reason} [تمت الإضافة بواسطة الإدارة]", approver_name=WEB_ADMIN_LABEL,
        )
        if not result.ok:
            flash(result.error, "warning")
            return render_template('admin_add_leave.html', employees=employees)

        note = " (بدون راتب)" if leave_type == logic.UNPAID else ""
        flash(f"تم إضافة الإجازة والموافقة عليها بنجاح{note}.", "success")
        send_notification(
            employee.telegram_id,
            f"تم إضافة إجازة لك بواسطة الإدارة{note}.\nالتفاصيل: {logic.leave_details(result.request)}\nالسبب: {reason}",
        )
        return redirect(url_for('index'))

    return render_template('admin_add_leave.html', employees=employees)


if __name__ == '__main__':
    if not token:
        print("Warning: TELEGRAM_BOT_TOKEN not set. Dashboard runs without the bot.")
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")

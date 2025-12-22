from functools import wraps
import os
import asyncio
import threading
from dotenv import load_dotenv

load_dotenv()
from datetime import date, datetime, timedelta
from flask import Flask, render_template, redirect, url_for, request, Response, flash
import telegram
from database import session, LeaveRequest, Employee, Holiday

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "a_default_secret_key")

@app.teardown_appcontext
def shutdown_session(exception=None):
    session.remove()

# --- Telegram Bot Setup ---
bot = None
token = os.getenv("TELEGRAM_BOT_TOKEN")
if token:
    bot = telegram.Bot(token=token)
    
    # Start the Telegram bot in a background thread
    def start_bot():
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Add a small delay to allow previous instances to close
        import time
        time.sleep(2)
        
        import bot as bot_module
        try:
            bot_module.main()
        except Exception as e:
            print(f"Error running bot: {e}")

    # Start Scheduler
    def start_scheduler():
        from scheduler import run_scheduler
        run_scheduler()

    # Only start threads if not already running (basic check)
    # Note: In gunicorn with workers=1, this runs once per worker.
    bot_thread = threading.Thread(target=start_bot, daemon=True)
    bot_thread.start()
    print("Telegram bot started in background thread.")

    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()
    print("Scheduler started in background thread.")
# -------------------------

# --- Basic Authentication ---
def check_auth(username, password):
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "secret")
    return username == admin_user and password == admin_pass

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
# --------------------------

# --- Notification Logic ---
async def send_notification_async(telegram_id, message):
    if bot:
        try:
            await bot.send_message(chat_id=telegram_id, text=message)
        except Exception as e:
            app.logger.error(f"Failed to send notification to {telegram_id}: {e}")

def send_notification(telegram_id, message):
    asyncio.run(send_notification_async(telegram_id, message))

# --- Business Logic for Leave Calculation ---
def calculate_leave_days(start_date, end_date):
    holidays = {h.date for h in session.query(Holiday).all()}
    days = 0
    current_date = start_date
    current_date = start_date
    while current_date <= end_date:
        if current_date.weekday() not in [4, 5] and current_date not in holidays: # Fri=4, Sat=5
            days += 1
        current_date += timedelta(days=1)
    return days

def calculate_leave_hours(start_time, end_time):
    if not start_time or not end_time:
        return 0
    # Assuming start and end times are on the same day for hourly leave
    duration = datetime.combine(date.today(), end_time) - datetime.combine(date.today(), start_time)
    return duration.total_seconds() / 3600
    
# --- Health Check (for UptimeRobot) ---
@app.route('/health')
def health():
    return {"status": "ok", "bot": "running"}, 200

# --- Main Routes ---
@app.route('/')
@requires_auth
def index():
    all_requests = session.query(LeaveRequest).order_by(LeaveRequest.id.desc()).all()
    return render_template('index.html', requests=all_requests)

@app.route('/approve/<int:request_id>')
@requires_auth
def approve_request(request_id):
    leave_request = session.get(LeaveRequest, request_id)
    if not leave_request or leave_request.status != 'pending':
        return redirect(url_for('index'))

    employee = leave_request.employee
    
    if leave_request.leave_type == 'يومية':
        days_to_deduct = calculate_leave_days(leave_request.start_date, leave_request.end_date)
        if employee.daily_leave_balance < days_to_deduct:
            flash("رصيد أيام الموظف غير كافٍ!", "error")
            return redirect(url_for('index'))
        employee.daily_leave_balance -= days_to_deduct

    elif leave_request.leave_type == 'بالساعة':
        hours_to_deduct = calculate_leave_hours(leave_request.start_time, leave_request.end_time)
        if employee.hourly_leave_balance < hours_to_deduct:
            flash("رصيد ساعات الموظف غير كافٍ!", "error")
            return redirect(url_for('index'))
        employee.hourly_leave_balance -= hours_to_deduct
        
    leave_request.status = 'approved'
    leave_request.approved_by = "Admin (Web)"
    session.commit()
    flash("تمت الموافقة على الطلب بنجاح.", "success")
    send_notification(employee.telegram_id, f"تمت الموافقة على طلب الإجازة الخاص بك (ID: {request_id}) من قبل الإدارة (Web).")
    
    # Notify all managers about the web approval
    managers = session.query(Employee).filter_by(is_manager=True).all()
    for mgr in managers:
        send_notification(mgr.telegram_id, f"✅ تم الموافقة على طلب الإجازة (ID: {request_id}) للموظف {employee.full_name} من قبل الإدارة (Web).")
        
    return redirect(url_for('index'))

@app.route('/reject/<int:request_id>')
@requires_auth
def reject_request(request_id):
    leave_request = session.get(LeaveRequest, request_id)
    if leave_request and leave_request.status == 'pending':
        leave_request.status = 'rejected'
        session.commit()
        flash("تم رفض الطلب.", "success")
        send_notification(leave_request.employee.telegram_id, f"تم رفض طلب الإجازة الخاص بك (ID: {request_id}).")
    return redirect(url_for('index'))

@app.route('/delete/<int:request_id>')
@requires_auth
def delete_request(request_id):
    leave_request = session.get(LeaveRequest, request_id)
    if leave_request:
        employee_id = leave_request.employee.telegram_id
        session.delete(leave_request)
        session.commit()
        flash("تم حذف الطلب بنجاح.", "success")
        send_notification(employee_id, f"تم حذف طلب الإجازة رقم {request_id}.")
    return redirect(url_for('index'))

@app.route('/bulk_delete_requests', methods=['POST'])
@requires_auth
def bulk_delete_requests():
    request_ids = request.form.getlist('request_ids')
    if request_ids:
        deleted_count = 0
        for req_id in request_ids:
            leave_req = session.get(LeaveRequest, int(req_id))
            if leave_req:
                session.delete(leave_req)
                deleted_count += 1
        session.commit()
        flash(f"تم حذف {deleted_count} طلب(ات) بنجاح.", "success")
    else:
        flash("لم يتم تحديد أي طلبات للحذف.", "error")
    return redirect(url_for('index'))

@app.route('/edit_request/<int:request_id>', methods=['GET', 'POST'])
@requires_auth
def edit_request(request_id):
    leave_request = session.get(LeaveRequest, request_id)
    if not leave_request:
        return redirect(url_for('index'))

    if request.method == 'POST':
        leave_request.leave_type = request.form['leave_type']
        leave_request.start_date = datetime.strptime(request.form['start_date'], '%Y-%m-%d').date()
        if leave_request.leave_type == 'يومية':
             leave_request.end_date = datetime.strptime(request.form['end_date'], '%Y-%m-%d').date()
             leave_request.start_time = None
             leave_request.end_time = None
        else:
             leave_request.end_date = leave_request.start_date
             leave_request.start_time = datetime.strptime(request.form['start_time'], '%H:%M').time()
             leave_request.end_time = datetime.strptime(request.form['end_time'], '%H:%M').time()
        
        leave_request.reason = request.form['reason']
        session.commit()
        
        flash("تم تعديل الطلب بنجاح.", "success")
        send_notification(leave_request.employee.telegram_id, f"تنبيه: قام المسؤول بتعديل طلب الإجازة الخاص بك (ID: {request_id}).")
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
    if user:
        user.full_name = request.form['full_name']
        user.department = request.form['department']
        user.is_manager = 'is_manager' in request.form
        user.daily_leave_balance = float(request.form['daily_balance'])
        user.hourly_leave_balance = float(request.form['hourly_balance'])
        session.commit()
        flash(f"تم تحديث بيانات الموظف {user.full_name} بنجاح.", "success")
    return redirect(url_for('manage_employees'))

@app.route('/approve_user/<int:user_id>')
@requires_auth
def approve_user_web(user_id):
    user = session.get(Employee, user_id)
    if user and user.status == 'pending':
        user.status = 'approved'
        # Grant initial leave balance from monthly quotas
        user.daily_leave_balance = user.monthly_daily_leave_quota
        user.hourly_leave_balance = user.monthly_hourly_leave_quota
        session.commit()
        flash(f"تمت الموافقة على الموظف {user.full_name}.", "success")
        send_notification(user.telegram_id, "تهانينا! تمت الموافقة على حسابك. يمكنك الآن استخدام الأمر /start للبدء.")
    return redirect(url_for('manage_employees'))

@app.route('/reject_user/<int:user_id>')
@requires_auth
def reject_user_web(user_id):
    user = session.get(Employee, user_id)
    if user:
        if user.status == 'pending':
             send_notification(user.telegram_id, "نأسف، تم رفض طلب تسجيلك.")
        session.delete(user)
        session.commit()
        flash(f"تم حذف الموظف {user.full_name}.", "success")
    return redirect(url_for('manage_employees'))

@app.route('/add_user', methods=['POST'])
@requires_auth
def add_user_web():
    telegram_id = request.form.get('telegram_id')
    full_name = request.form.get('full_name')
    department = request.form.get('department')
    is_manager = 'is_manager' in request.form
    
    if not telegram_id or not full_name:
        flash("يرجى ملء جميع الحقول المطلوبة (Telegram ID والاسم).", "error")
        return redirect(url_for('manage_employees'))
        
    try:
        telegram_id = int(telegram_id)
        # Check if already exists
        existing = session.query(Employee).filter_by(telegram_id=telegram_id).first()
        if existing:
            flash(f"الموظف بـ ID {telegram_id} موجود مسبقاً باسم {existing.full_name}.", "error")
            return redirect(url_for('manage_employees'))
            
        new_emp = Employee(
            telegram_id=telegram_id,
            full_name=full_name,
            department=department,
            status='approved',
            is_manager=is_manager,
            daily_leave_balance=2.0,
            hourly_leave_balance=4.0
        )
        session.add(new_emp)
        session.commit()
        flash(f"تم إضافة الموظف {full_name} بنجاح.", "success")
    except ValueError:
        flash("Telegram ID يجب أن يكون رقماً.", "error")
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
    name = request.form['name']
    date_str = request.form['date']
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        new_holiday = Holiday(name=name, date=date_obj)
        session.add(new_holiday)
        session.commit()
        flash(f"تمت إضافة '{name}' إلى قائمة العطلات.", "success")
    except Exception as e:
        session.rollback()
        flash(f"فشل في إضافة العطلة: {e}", "error")
    return redirect(url_for('manage_holidays'))

@app.route('/delete_holiday/<int:holiday_id>')
@requires_auth
def delete_holiday(holiday_id):
    holiday = session.get(Holiday, holiday_id)
    if holiday:
        session.delete(holiday)
        session.commit()
        flash("تم حذف العطلة بنجاح.", "success")
    return redirect(url_for('manage_holidays'))

@app.route('/reports', methods=['GET', 'POST'])
@requires_auth
def reports():
    query = session.query(LeaveRequest)
    employees = session.query(Employee).order_by(Employee.full_name).all()

    if request.method == 'POST':
        employee_id = request.form.get('employee_id')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        leave_status = request.form.get('status')

        if employee_id:
            query = query.filter(LeaveRequest.employee_id == employee_id)
        if start_date:
            query = query.filter(LeaveRequest.start_date >= datetime.strptime(start_date, '%Y-%m-%d').date())
        if end_date:
            query = query.filter(LeaveRequest.end_date <= datetime.strptime(end_date, '%Y-%m-%d').date())
        if leave_status:
            query = query.filter(LeaveRequest.status == leave_status)

    results = query.order_by(LeaveRequest.start_date.desc()).all()
    return render_template('reports.html', results=results, employees=employees)

@app.route('/export_reports', methods=['GET'])
@requires_auth
def export_reports():
    import csv
    import io
    from flask import make_response

    # Re-implement filtering logic (or refactor to share it, but for now copying is safer/faster)
    query = session.query(LeaveRequest)
    
    employee_id = request.args.get('employee_id')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    leave_status = request.args.get('status')

    if employee_id:
        query = query.filter(LeaveRequest.employee_id == employee_id)
    if start_date:
        query = query.filter(LeaveRequest.start_date >= datetime.strptime(start_date, '%Y-%m-%d').date())
    if end_date:
        query = query.filter(LeaveRequest.end_date <= datetime.strptime(end_date, '%Y-%m-%d').date())
    if leave_status:
        query = query.filter(LeaveRequest.status == leave_status)

    results = query.order_by(LeaveRequest.start_date.desc()).all()

    # Create CSV
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['الموظف', 'النوع', 'تاريخ البدء', 'تاريخ الانتهاء', 'وقت البدء', 'وقت الانتهاء', 'السبب', 'الحالة', 'البديل'])

    for req in results:
        replacement_name = req.replacement_employee.full_name if req.replacement_employee else "لا يوجد"
        cw.writerow([
            req.employee.full_name,
            req.leave_type,
            req.start_date,
            req.end_date,
            req.start_time if req.start_time else "",
            req.end_time if req.end_time else "",
            req.reason,
            req.status,
            replacement_name
        ])

    output = make_response(si.getvalue().encode('utf-8-sig')) # utf-8-sig for Excel compatibility
    output.headers["Content-Disposition"] = "attachment; filename=leave_reports.csv"
    output.headers["Content-type"] = "text/csv"
    return output

@app.route('/admin/add_leave', methods=['GET', 'POST'])
@requires_auth
def admin_add_leave():
    employees = session.query(Employee).order_by(Employee.full_name).all()
    
    if request.method == 'POST':
        employee_id = request.form['employee_id']
        leave_type = request.form['leave_type']
        start_date = datetime.strptime(request.form['start_date'], '%Y-%m-%d').date()
        reason = request.form['reason']
        ignore_balance = 'ignore_balance' in request.form
        
        employee = session.get(Employee, employee_id)
        
        # Determine end date/time
        end_date = start_date
        start_time = None
        end_time = None
        
        if leave_type == 'يومية':
            if request.form.get('end_date'):
                end_date = datetime.strptime(request.form['end_date'], '%Y-%m-%d').date()
            
            # Balance check
            days = calculate_leave_days(start_date, end_date)
            if not ignore_balance and employee.daily_leave_balance < days:
                flash(f"رصيد الموظف غير كافٍ! (مطلوب: {days}, متوفر: {employee.daily_leave_balance})", "error")
                return render_template('admin_add_leave.html', employees=employees)
            
            employee.daily_leave_balance -= days
            
        else: # Hourly
            start_time = datetime.strptime(request.form['start_time'], '%H:%M').time()
            end_time = datetime.strptime(request.form['end_time'], '%H:%M').time()
            
            # Balance check
            hours = calculate_leave_hours(start_time, end_time)
            if not ignore_balance and employee.hourly_leave_balance < hours:
                flash(f"رصيد الموظف غير كافٍ! (مطلوب: {hours}, متوفر: {employee.hourly_leave_balance})", "error")
                return render_template('admin_add_leave.html', employees=employees)
            
            employee.hourly_leave_balance -= hours

        # Create approved request
        new_request = LeaveRequest(
            employee_id=employee_id,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            reason=f"{reason} [تمت الإضافة بواسطة الإدارة]",
            status='approved',
            replacement_approval_status='not_required'
        )
        session.add(new_request)
        session.commit()
        
        flash("تم إضافة الإجازة والموافقة عليها بنجاح.", "success")
        send_notification(employee.telegram_id, f"تم إضافة إجازة لك بواسطة الإدارة.\nالسبب: {reason}")
        return redirect(url_for('index'))

    return render_template('admin_add_leave.html', employees=employees)


if __name__ == '__main__':
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set.")
    else:
        app.run(debug=True)

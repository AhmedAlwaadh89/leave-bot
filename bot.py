import os
import logging
from dotenv import load_dotenv
from datetime import datetime, date, timedelta

load_dotenv()
from sqlalchemy import or_
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)
import dateparser
from database import session, Employee, LeaveRequest

# Setup logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
(
    FULL_NAME, DEPARTMENT, LEAVE_TYPE, LEAVE_START_DATE, LEAVE_END_DATE,
    LEAVE_START_TIME, LEAVE_END_TIME, LEAVE_REASON, REPLACEMENT_EMPLOYEE,
    MAIN_MENU
) = range(10)

# --- Helper Functions & Keyboards ---

def is_manager(telegram_id: int) -> bool:
    employee = session.query(Employee).filter_by(telegram_id=telegram_id, status='approved').first()
    return employee and employee.is_manager

def check_conflicts(employee_id: int, start_date: date, end_date: date) -> bool:
    """Checks if there are overlapping approved leaves in the same department."""
    employee = session.get(Employee, employee_id)
    if not employee or not employee.department:
        return False
        
    conflicts = session.query(LeaveRequest).join(Employee).filter(
        Employee.department == employee.department,
        Employee.id != employee_id,
        LeaveRequest.status == 'approved',
        or_(
            (LeaveRequest.start_date <= end_date) & (LeaveRequest.end_date >= start_date)
        )
    ).count()
    
    return conflicts > 0

def get_main_menu_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Builds the main menu keyboard based on user role."""
    keyboard = [
        [InlineKeyboardButton("➕ طلب إجازة جديد", callback_data='new_leave')],
        [
            InlineKeyboardButton("📂 طلباتي", callback_data='my_requests'),
            InlineKeyboardButton("📊 رصيدي", callback_data='my_balance')
        ],
    ]
    if is_manager(telegram_id):
        keyboard.append([InlineKeyboardButton("👑 قائمة المدير", callback_data='admin_menu')])
    return InlineKeyboardMarkup(keyboard)

def get_admin_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("⏳ مراجعة طلبات الإجازة", callback_data='admin_review_leaves')],
        [InlineKeyboardButton("👥 إدارة تسجيل الموظفين", callback_data='admin_manage_employees')],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data='main_menu')],
    ]
    return InlineKeyboardMarkup(keyboard)

async def notify_managers(context: ContextTypes.DEFAULT_TYPE, message: str, reply_markup: InlineKeyboardMarkup = None):
    logger.info("Attempting to notify managers...")
    managers = session.query(Employee).filter_by(is_manager=True, status='approved').all()
    
    if not managers:
        logger.warning("No approved managers found to send notifications to.")
        return
        
    logger.info(f"Found {len(managers)} manager(s) to notify.")
    logger.info(f"Found {len(managers)} manager(s) to notify.")
    
    # Create tasks for all notifications to run in parallel
    tasks = []
    for manager in managers:
        tasks.append(context.bot.send_message(chat_id=manager.telegram_id, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup))
    
    # Run all tasks concurrently
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Failed to send notification to manager {managers[i].telegram_id}: {result}")
            else:
                logger.info(f"Sent notification to manager {managers[i].telegram_id}.")

# --- Main Commands & Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles /start command, showing main menu or starting registration."""
    user = update.effective_user
    employee = session.query(Employee).filter_by(telegram_id=user.id).first()

    if employee:
        if employee.status == 'approved':
            keyboard = get_main_menu_keyboard(user.id)
            await update.message.reply_text(
                f"أهلاً بعودتك، {employee.full_name}! اختر أحد الخيارات:",
                reply_markup=keyboard
            )
        elif employee.status == 'pending':
            await update.message.reply_text("حسابك لا يزال قيد المراجعة من قبل الإدارة. سيتم إعلامك عند الموافقة.")
        # For both approved and pending, we go to the main menu state
        return MAIN_MENU
    else:
        # New user, start registration
        await update.message.reply_text("أهلاً بك في نظام إدارة الإجازات. يرجى إدخال اسمك الكامل للتسجيل:")
        return FULL_NAME


async def full_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles name input and asks for department."""
    context.user_data['full_name'] = update.message.text
    await update.message.reply_text("يرجى إدخال القسم الذي تعمل به:")
    return DEPARTMENT

async def department_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles department input and completes registration."""
    user = update.effective_user
    full_name = context.user_data['full_name']
    department = update.message.text

    # Check if user already exists to avoid duplicates from race conditions/restarts
    if session.query(Employee).filter_by(telegram_id=user.id).first():
        await update.message.reply_text("أنت مسجل بالفعل. سيتم نقلك للقائمة الرئيسية.")
        return await start(update, context)

    is_first = session.query(Employee).count() == 0

    new_employee = Employee(
        telegram_id=user.id,
        full_name=full_name,
        department=department,
        is_manager=is_first,
        status='approved' if is_first else 'pending'
    )
    session.add(new_employee)
    session.commit()

    if is_first:
        await update.message.reply_text("تم تسجيلك كأول مستخدم وتعيينك كمدير. أهلاً بك!")
        keyboard = get_main_menu_keyboard(user.id)
        await update.message.reply_text("اختر أحد الخيارات للبدء:", reply_markup=keyboard)
    else:
        await update.message.reply_text("شكراً لتسجيلك. تم إرسال طلبك للإدارة للموافقة. سيتم إعلامك عند الموافقة.")
        
        keyboard = [
            [
                InlineKeyboardButton("✅ موافقة", callback_data=f"approve_user_{new_employee.id}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"reject_user_{new_employee.id}")
            ]
        ]
        await notify_managers(
            context, 
            f"الموظف الجديد *{full_name}* (ID: `{new_employee.id}`) من قسم *{department}* ينتظر الموافقة.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return MAIN_MENU # Transition to main menu state for all new users

# --- New Leave Conversation Handlers ---

async def new_leave_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the new leave request conversation by asking for leave type."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("إجازة يومية", callback_data='leave_daily'),
            InlineKeyboardButton("إجازة بالساعة", callback_data='leave_hourly')
        ],
        [InlineKeyboardButton("🔙 إلغاء والعودة", callback_data='cancel_leave')]
    ]
    await query.edit_message_text(
        text="اختر نوع الإجازة:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LEAVE_TYPE

async def cancel_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the leave conversation and shows the main menu."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    keyboard = get_main_menu_keyboard(user_id)
    await query.edit_message_text(text="تم إلغاء العملية. القائمة الرئيسية:", reply_markup=keyboard)
    context.user_data.clear()
    return ConversationHandler.END

async def leave_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the leave type selection."""
    query = update.callback_query
    await query.answer()
    leave_type = query.data.split('_')[1]
    context.user_data['leave_type'] = 'يومية' if leave_type == 'daily' else 'بالساعة'

    prompt = "أدخل تاريخ البدء (YYYY-MM-DD):"
    if context.user_data['leave_type'] == 'بالساعة':
        prompt = "أدخل تاريخ الإجازة (YYYY-MM-DD):"

    await query.edit_message_text(text=prompt)
    return LEAVE_START_DATE

async def leave_start_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the leave start date input and branches for hourly leave."""
    try:
        date_text = update.message.text
        leave_date = dateparser.parse(date_text, settings={'DATE_ORDER': 'YMD'})
        
        if not leave_date:
             await update.message.reply_text("لم أتمكن من فهم التاريخ. يرجى المحاولة مرة أخرى (مثال: 2023-10-25 أو 'غداً'):")
             return LEAVE_START_DATE
             
        leave_date = leave_date.date()
        
        if leave_date < date.today():
             await update.message.reply_text("لا يمكن أن يكون التاريخ في الماضي. حاول مرة أخرى:")
             return LEAVE_START_DATE
        
        context.user_data['start_date'] = leave_date

        if context.user_data['leave_type'] == 'بالساعة':
            context.user_data['end_date'] = leave_date # For hourly, end_date is same as start_date
            await update.message.reply_text("أدخل وقت البدء (HH:MM بصيغة 24 ساعة):")
            return LEAVE_START_TIME
        else: # Daily leave
            await update.message.reply_text("أدخل تاريخ الانتهاء (YYYY-MM-DD):")
            return LEAVE_END_DATE

    except ValueError:
        await update.message.reply_text("صيغة التاريخ غير صالحة. يرجى استخدام YYYY-MM-DD:")
        return LEAVE_START_DATE

async def leave_end_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the leave end date input."""
    try:
        date_text = update.message.text
        end_date = dateparser.parse(date_text, settings={'DATE_ORDER': 'YMD'})
        
        if not end_date:
             await update.message.reply_text("لم أتمكن من فهم التاريخ. يرجى المحاولة مرة أخرى:")
             return LEAVE_END_DATE
             
        end_date = end_date.date()

        if end_date < context.user_data['start_date']:
            await update.message.reply_text("تاريخ الانتهاء لا يمكن أن يكون قبل تاريخ البدء. حاول مرة أخرى:")
            return LEAVE_END_DATE
        context.user_data['end_date'] = end_date
        await update.message.reply_text("أدخل سبب الإجازة:")
        return LEAVE_REASON
    except ValueError:
        await update.message.reply_text("صيغة التاريخ غير صالحة. يرجى استخدام YYYY-MM-DD:")
        return LEAVE_END_DATE

async def leave_start_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the leave start time input for hourly leave."""
    try:
        start_time = datetime.strptime(update.message.text, '%H:%M').time()
        context.user_data['start_time'] = start_time
        await update.message.reply_text("أدخل وقت الانتهاء (HH:MM بصيغة 24 ساعة):")
        return LEAVE_END_TIME
    except ValueError:
        await update.message.reply_text("صيغة الوقت غير صالحة. يرجى استخدام HH:MM:")
        return LEAVE_START_TIME

async def leave_end_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the leave end time input and moves to the reason."""
    try:
        end_time = datetime.strptime(update.message.text, '%H:%M').time()
        if end_time <= context.user_data['start_time']:
            await update.message.reply_text("وقت الانتهاء يجب أن يكون بعد وقت البدء. حاول مرة أخرى:")
            return LEAVE_END_TIME
        context.user_data['end_time'] = end_time
        await update.message.reply_text("أدخل سبب الإجازة:")
        return LEAVE_REASON
    except ValueError:
        await update.message.reply_text("صيغة الوقت غير صالحة. يرجى استخدام HH:MM:")
        return LEAVE_END_TIME

async def leave_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the leave reason input and asks for a replacement."""
    context.user_data['reason'] = update.message.text
    
    # Fetch other employees to be potential replacements
    current_user_id = update.effective_user.id
    employees = session.query(Employee).filter(Employee.telegram_id != current_user_id, Employee.status == 'approved').all()
    
    if not employees:
        await update.message.reply_text("لا يوجد موظفون آخرون متاحون لتحديدهم كبديل. سيتم المتابعة بدون بديل.")
        return await submit_leave_request(update, context) # Skip to submission

    # Check for conflicts
    try:
        employee = session.query(Employee).filter_by(telegram_id=update.effective_user.id).first()
        if check_conflicts(employee.id, context.user_data['start_date'], context.user_data['end_date']):
            await update.message.reply_text("⚠️ تنبيه: يوجد موظفون آخرون في قسمك لديهم إجازات معتمدة في نفس الفترة.")
    except Exception as e:
        logger.error(f"Error checking conflicts: {e}")

    keyboard = [[InlineKeyboardButton(emp.full_name, callback_data=f"rep_{emp.id}")] for emp in employees]
    keyboard.append([InlineKeyboardButton("لا يوجد بديل", callback_data="rep_0")])
    await update.message.reply_text("اختر الموظف البديل:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REPLACEMENT_EMPLOYEE

async def replacement_employee_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the replacement employee selection and starts the approval flow."""
    query = update.callback_query
    await query.answer()
    
    replacement_id = int(query.data.split('_')[1])
    context.user_data['replacement_id'] = replacement_id if replacement_id != 0 else None
    
    if replacement_id == 0:
        # No replacement needed, submit directly
        # We need to ensure we pass the query as the update object for the helper function
        return await submit_leave_request(query, context)
    
    # Notify replacement employee
    replacement = session.get(Employee, replacement_id)
    requester = session.query(Employee).filter_by(telegram_id=query.from_user.id).first()
    
    # Create the request first with 'pending_replacement' status (or just pending with flag)
    # Actually, let's create it now so we have an ID to reference in the callback
    new_request = create_leave_request_record(context, requester.id, 'pending', 'pending')
    
    keyboard = [
        [
            InlineKeyboardButton("✅ موافق", callback_data=f"rep_accept_{new_request.id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"rep_reject_{new_request.id}")
        ]
    ]
    
    try:
        await context.bot.send_message(
            chat_id=replacement.telegram_id,
            text=f"طلب بديل: الموظف {requester.full_name} يطلب منك أن تكون بديلاً له في إجازته من {new_request.start_date} إلى {new_request.end_date}.\nالسبب: {new_request.reason}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        await query.edit_message_text(f"تم إرسال الطلب للموظف البديل {replacement.full_name}. بانتظار موافقته...")
    except Exception as e:
        logger.error(f"Failed to send message to replacement: {e}")
        await query.edit_message_text("فشل إرسال الإشعار للموظف البديل. تأكد من أنه مسجل في البوت.")
        
    context.user_data.clear()
    return ConversationHandler.END

async def replacement_response_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the replacement employee's acceptance or rejection."""
    query = update.callback_query
    await query.answer()
    
    try:
        action, request_id = query.data.split('_')[1], int(query.data.split('_')[2])
        leave_request = session.get(LeaveRequest, request_id)
        
        if not leave_request:
            await query.edit_message_text("عذراً، لم يتم العثور على الطلب.")
            return 0
            
        if action == 'accept':
            leave_request.replacement_approval_status = 'accepted'
            session.commit()
            await query.edit_message_text("شكراً لك. تم قبول طلب البديل.")
            
            # Notify Requester
            requester = leave_request.employee
            await context.bot.send_message(requester.telegram_id, f"وافق {leave_request.replacement_employee.full_name} على أن يكون بديلاً لك. تم رفع الطلب للإدارة.")
            
            # Notify Managers
            await notify_managers_new_request(context, leave_request)
            
        elif action == 'reject':
            leave_request.replacement_approval_status = 'rejected'
            leave_request.status = 'rejected' # Auto reject if replacement refuses? Or just cancel?
            session.commit()
            await query.edit_message_text("تم رفض طلب البديل.")
            
            # Notify Requester
            requester = leave_request.employee
            await context.bot.send_message(requester.telegram_id, f"عذراً، رفض {leave_request.replacement_employee.full_name} طلب البديل. تم إلغاء طلب الإجازة.")
    
    except Exception as e:
        logger.error(f"Error in replacement_response_handler: {e}")
        await query.edit_message_text("حدث خطأ أثناء معالجة الطلب. يرجى المحاولة مرة أخرى.")
        session.rollback()

    return 0

def create_leave_request_record(context, employee_id, status, rep_status):
    new_request = LeaveRequest(
        employee_id=employee_id,
        leave_type=context.user_data['leave_type'],
        start_date=context.user_data['start_date'],
        end_date=context.user_data['end_date'],
        start_time=context.user_data.get('start_time'),
        end_time=context.user_data.get('end_time'),
        reason=context.user_data['reason'],
        replacement_employee_id=context.user_data.get('replacement_id'),
        status=status,
        replacement_approval_status=rep_status
    )
    session.add(new_request)
    session.commit()
    return new_request

async def submit_leave_request(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Helper to save the leave request to the DB and notify managers (No replacement case)."""
    user_id = update_or_query.effective_user.id
    employee = session.query(Employee).filter_by(telegram_id=user_id).first()

    # Create and save the leave request
    new_request = create_leave_request_record(context, employee.id, 'pending', 'not_required')
    
    # Notify managers
    await notify_managers_new_request(context, new_request)

    # Respond to the user
    text = "تم تقديم طلب الإجازة الخاص بك بنجاح وهو الآن قيد المراجعة."
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text)
    else: # It's a CallbackQuery
        await update_or_query.edit_message_text(text)
        
    context.user_data.clear()
    return ConversationHandler.END

async def notify_managers_new_request(context, new_request):
    employee = new_request.employee
    message = (
        f"طلب إجازة جديد من *{employee.full_name}* (ID: `{new_request.id}`)\n"
        f"القسم: {employee.department}\n"
        f"النوع: {new_request.leave_type}\n"
    )
    if new_request.leave_type == 'يومية':
        message += f"من: {new_request.start_date.strftime('%Y-%m-%d')} إلى: {new_request.end_date.strftime('%Y-%m-%d')}\n"
    else: # بالساعة
        message += (
            f"التاريخ: {new_request.start_date.strftime('%Y-%m-%d')}\n"
            f"من الساعة: {new_request.start_time.strftime('%H:%M')} إلى الساعة: {new_request.end_time.strftime('%H:%M')}\n"
        )
    message += f"السبب: {new_request.reason}\n"
    message += f"💰 *الرصيد الحالي:* أيام: {employee.daily_leave_balance} | ساعات: {employee.hourly_leave_balance}"

    if new_request.replacement_employee_id:
        replacement = session.get(Employee, new_request.replacement_employee_id)
        message += f"\nالموظف البديل: {replacement.full_name} (✅ وافق)"

    await notify_managers(context, message)


# --- Callback Query (Button) Handler ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parses the CallbackQuery and shows the appropriate menu or action."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    employee = session.query(Employee).filter_by(telegram_id=user_id).first()
    
    # If user is not approved, stop them from proceeding.
    if not employee or employee.status != 'approved':
        await query.edit_message_text("حسابك قيد المراجعة. لا يمكنك القيام بأي إجراء حالياً.")
        return MAIN_MENU

    # Main Menu navigation
    if query.data == 'main_menu':
        keyboard = get_main_menu_keyboard(user_id)
        await query.edit_message_text(text="القائمة الرئيسية:", reply_markup=keyboard)
        return MAIN_MENU
    
    # Admin Menu navigation
    elif query.data == 'admin_menu':
        if not is_manager(user_id):
            await query.edit_message_text("ليس لديك صلاحيات المدير.")
            return MAIN_MENU
        keyboard = get_admin_menu_keyboard()
        await query.edit_message_text(text="قائمة المدير:", reply_markup=keyboard)
        return MAIN_MENU
        
    elif query.data == 'new_leave':
        return await new_leave_start(update, context)

    # Placeholder for other actions - will be filled out
    elif query.data == 'my_requests':
        requests = session.query(LeaveRequest).filter_by(employee_id=employee.id).order_by(LeaveRequest.id.desc()).limit(5).all()
        if not requests:
            await query.edit_message_text("لا يوجد لديك طلبات إجازة سابقة.")
            return

        message = "*آخر 5 طلبات إجازة:*\n"
        for req in requests:
            status_icon = "⏳" if req.status == 'pending' else ("✅" if req.status == 'approved' else "❌")
            rep_status = ""
            if req.replacement_employee_id:
                rep_status = f" (البديل: {req.replacement_approval_status})"
            
            message += f"{status_icon} ID: `{req.id}` | {req.leave_type} | {req.status}{rep_status}\n"
            
        await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN)

    elif query.data == 'my_balance':
        await query.edit_message_text(
            f"*رصيدك الحالي:*\n- أيام: {employee.daily_leave_balance}\n- ساعات: {employee.hourly_leave_balance}",
            parse_mode=ParseMode.MARKDOWN
        )
    elif query.data == 'admin_review_leaves':
        # Filter for requests that are pending AND (replacement is accepted OR not required)
        pending_requests = session.query(LeaveRequest).filter(
            LeaveRequest.status == 'pending',
            or_(
                LeaveRequest.replacement_approval_status == 'accepted',
                LeaveRequest.replacement_approval_status == 'not_required'
            )
        ).all()
        
        if not pending_requests:
            await query.edit_message_text("لا يوجد طلبات إجازة بانتظار المراجعة.")
            return

        message = "*طلبات الإجازة بانتظار المراجعة:*\n"
        keyboard = []
        for req in pending_requests:
            emp = req.employee
            message += f"- {emp.full_name} ({req.leave_type}) ID: `{req.id}`\n"
            message += f"  💰 رصيد: {emp.daily_leave_balance} يوم | {emp.hourly_leave_balance} ساعة\n"
            keyboard.append([
                InlineKeyboardButton(f"✅ موافقة {req.id}", callback_data=f"admin_approve_{req.id}"),
                InlineKeyboardButton(f"❌ رفض {req.id}", callback_data=f"admin_reject_{req.id}")
            ])
        
        keyboard.append([InlineKeyboardButton("🔙 العودة لقائمة المدير", callback_data='admin_menu')])
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

    elif query.data.startswith('admin_approve_'):
        req_id = int(query.data.split('_')[2])
        req = session.get(LeaveRequest, req_id)
        if req and req.status == 'pending':
            # Deduct balance logic should ideally be shared, but for now simplistic:
            # We should probably call the logic in app.py or duplicate it here.
            # For safety, let's just mark approved and let the admin handle balance manually if needed, 
            # OR implement the deduction logic here.
            # Let's implement deduction here to match app.py logic.
            
            emp = req.employee
            if req.leave_type == 'يومية':
                # Calculate days (excluding Fri/Sat)
                days = 0
                curr = req.start_date
                while curr <= req.end_date:
                    if curr.weekday() not in [4, 5]: # Fri=4, Sat=5
                        days += 1
                    curr += timedelta(days=1)
                
                if emp.daily_leave_balance < days:
                    await query.answer("رصيد الموظف غير كافٍ!", show_alert=True)
                    return
                emp.daily_leave_balance -= days
            
            elif req.leave_type == 'بالساعة':
                # Calculate hours
                duration = datetime.combine(date.today(), req.end_time) - datetime.combine(date.today(), req.start_time)
                hours = duration.total_seconds() / 3600
                if emp.hourly_leave_balance < hours:
                    await query.answer("رصيد الموظف غير كافٍ!", show_alert=True)
                    return
                emp.hourly_leave_balance -= hours

            req.status = 'approved'
            session.commit()
            await query.edit_message_text(f"تمت الموافقة على طلب الإجازة {req_id}.", reply_markup=get_admin_menu_keyboard())
            await context.bot.send_message(emp.telegram_id, f"تمت الموافقة على طلب الإجازة الخاص بك (ID: {req_id}).")
        else:
             await query.edit_message_text("الطلب غير موجود أو تمت معالجته مسبقاً.", reply_markup=get_admin_menu_keyboard())

    elif query.data.startswith('admin_reject_'):
        req_id = int(query.data.split('_')[2])
        req = session.get(LeaveRequest, req_id)
        if req and req.status == 'pending':
            req.status = 'rejected'
            session.commit()
            await query.edit_message_text(f"تم رفض طلب الإجازة {req_id}.", reply_markup=get_admin_menu_keyboard())
            await context.bot.send_message(req.employee.telegram_id, f"تم رفض طلب الإجازة الخاص بك (ID: {req_id}).")
    elif query.data == 'admin_manage_employees':
        pending = session.query(Employee).filter_by(status='pending').all()
        if not pending:
            await query.edit_message_text("لا يوجد موظفون في انتظار الموافقة.", reply_markup=get_admin_menu_keyboard())
            return
        
        message = "*الموظفون في انتظار الموافقة:*\n"
        keyboard_buttons = []
        for emp in pending:
            message += f"- {emp.full_name} (ID: `{emp.id}`)\n"
            keyboard_buttons.append([
                InlineKeyboardButton(f"✅ موافقة {emp.full_name}", callback_data=f"approve_user_{emp.id}"),
                InlineKeyboardButton(f"❌ رفض {emp.full_name}", callback_data=f"reject_user_{emp.id}")
            ])
        keyboard_buttons.append([InlineKeyboardButton("🔙 العودة لقائمة المدير", callback_data='admin_menu')])
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=ParseMode.MARKDOWN)

    # Handling user approval/rejection
    # Handling user approval/rejection - MOVED TO GLOBAL HANDLER
    elif query.data.startswith('approve_user_') or query.data.startswith('reject_user_'):
         # This part is now handled by global_admin_handler, but we keep it here just in case
         # to avoid "query not answered" if it falls through.
         # Actually, better to remove it from here to avoid double handling if we register the global one correctly.
         pass

# --- Global Admin Handlers ---

async def global_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin actions (approve/reject user/leave) globally."""
    query = update.callback_query
    await query.answer()
    
    try:
        # Check if user is manager
        if not is_manager(query.from_user.id):
            await query.edit_message_text("ليس لديك صلاحيات المدير.")
            return

        if query.data.startswith('approve_user_'):
            user_to_approve_id = int(query.data.split('_')[2])
            user = session.get(Employee, user_to_approve_id)
            if user and user.status == 'pending':
                user.status = 'approved'
                session.commit()
                await query.edit_message_text(f"تمت الموافقة على {user.full_name}.")
                await context.bot.send_message(user.telegram_id, "تهانينا! تمت الموافقة على حسابك. اضغط /start للبدء.")
            else:
                await query.edit_message_text("المستخدم غير موجود أو تمت الموافقة عليه مسبقاً.")
        
        elif query.data.startswith('reject_user_'):
            user_to_reject_id = int(query.data.split('_')[2])
            user = session.get(Employee, user_to_reject_id)
            if user:
                await context.bot.send_message(user.telegram_id, "نأسف، تم رفض طلب تسجيلك.")
                session.delete(user)
                session.commit()
                await query.edit_message_text(f"تم رفض وحذف {user.full_name}.")
            else:
                await query.edit_message_text("المستخدم غير موجود.")

        elif query.data.startswith('admin_approve_'):
            req_id = int(query.data.split('_')[2])
            req = session.get(LeaveRequest, req_id)
            if req and req.status == 'pending':
                emp = req.employee
                if req.leave_type == 'يومية':
                    days = 0
                    curr = req.start_date
                    while curr <= req.end_date:
                        if curr.weekday() not in [4, 5]: 
                            days += 1
                        curr += timedelta(days=1)
                    
                    if emp.daily_leave_balance < days:
                        await query.answer("رصيد الموظف غير كافٍ!", show_alert=True)
                        return
                    emp.daily_leave_balance -= days
                
                elif req.leave_type == 'بالساعة':
                    duration = datetime.combine(date.today(), req.end_time) - datetime.combine(date.today(), req.start_time)
                    hours = duration.total_seconds() / 3600
                    if emp.hourly_leave_balance < hours:
                        await query.answer("رصيد الموظف غير كافٍ!", show_alert=True)
                        return
                    emp.hourly_leave_balance -= hours

                req.status = 'approved'
                session.commit()
                await query.edit_message_text(f"تمت الموافقة على طلب الإجازة {req_id}.")
                await context.bot.send_message(emp.telegram_id, f"تمت الموافقة على طلب الإجازة الخاص بك (ID: {req_id}).")
            else:
                 await query.edit_message_text("الطلب غير موجود أو تمت معالجته مسبقاً.")

        elif query.data.startswith('admin_reject_'):
            req_id = int(query.data.split('_')[2])
            req = session.get(LeaveRequest, req_id)
            if req and req.status == 'pending':
                req.status = 'rejected'
                session.commit()
                await query.edit_message_text(f"تم رفض طلب الإجازة {req_id}.")
                await context.bot.send_message(req.employee.telegram_id, f"تم رفض طلب الإجازة الخاص بك (ID: {req_id}).")
            else:
                await query.edit_message_text("الطلب غير موجود أو تمت معالجته مسبقاً.")
                
    except Exception as e:
        logger.error(f"Error in global_admin_handler: {e}")
        await query.edit_message_text("حدث خطأ أثناء معالجة الطلب. يرجى المحاولة مرة أخرى.")
        session.rollback()  # Rollback on error

# --- Cancel ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("تم إلغاء العملية.")
    return ConversationHandler.END

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set.")
        return

    application = Application.builder().token(token).build()

    # A single conversation handler to manage everything
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            # Add a text handler to allow starting without /start
            MessageHandler(filters.TEXT & ~filters.COMMAND, start)
        ],
        states={
            # State for user registration
            FULL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, full_name_handler)],
            DEPARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, department_handler)],
            
            # State for main menu and general button handling
            MAIN_MENU: [CallbackQueryHandler(button_handler)],

            # States for the leave request conversation
            LEAVE_TYPE: [CallbackQueryHandler(leave_type_handler, pattern='^leave_(daily|hourly)$')],
            LEAVE_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_start_date_handler)],
            LEAVE_END_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_end_date_handler)],
            LEAVE_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_start_time_handler)],
            LEAVE_END_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_end_time_handler)],
            LEAVE_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_reason_handler)],
            REPLACEMENT_EMPLOYEE: [CallbackQueryHandler(replacement_employee_handler, pattern='^rep_')]
        },
        fallbacks=[
            CallbackQueryHandler(replacement_response_handler, pattern='^rep_(accept|reject)_'),
            CallbackQueryHandler(cancel_leave, pattern='^cancel_leave$'),
            CommandHandler("cancel", cancel),
            CommandHandler("start", start) # Allow restarting
        ],
        per_message=False
    )
    
    application.add_handler(conv_handler)
    
    # Register global admin handler for inline buttons (approve/reject)
    # This ensures they work even if the manager is not in a specific state
    application.add_handler(CallbackQueryHandler(global_admin_handler, pattern='^(approve_user_|reject_user_|admin_approve_|admin_reject_)'))

    # Register global handler for replacement responses
    # The replacement employee is not in the conversation, so this must be global
    application.add_handler(CallbackQueryHandler(replacement_response_handler, pattern='^rep_(accept|reject)_'))

    application.run_polling(stop_signals=None, close_loop=False)

if __name__ == "__main__":
    main()

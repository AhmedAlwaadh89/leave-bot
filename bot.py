import os
import io
import logging
from datetime import datetime, date

import dateparser
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import or_  # noqa: E402
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402
from telegram.constants import ParseMode  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

from database import session, Employee, LeaveRequest, NotificationLog  # noqa: E402
import leave_logic as logic  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
(
    FULL_NAME, DEPARTMENT, LEAVE_TYPE, LEAVE_START_DATE, LEAVE_END_DATE,
    LEAVE_START_TIME, LEAVE_END_TIME, LEAVE_REASON, REPLACEMENT_EMPLOYEE,
    MAIN_MENU,
    ADMIN_ADD_EMP_ID, ADMIN_ADD_EMP_NAME, ADMIN_ADD_EMP_DEPT
) = range(13)

ADMIN_CALLBACK_PATTERN = r'^(approve_user_|reject_user_|admin_approve_|admin_reject_|admin_force_)'


# --------------------------------------------------------------------------
# Helpers & keyboards
# --------------------------------------------------------------------------
def get_employee(telegram_id: int):
    return session.query(Employee).filter_by(telegram_id=telegram_id).first()


def is_manager(telegram_id: int) -> bool:
    employee = session.query(Employee).filter_by(telegram_id=telegram_id, status='approved').first()
    return bool(employee and employee.is_manager)


def manager_name(telegram_id: int) -> str:
    emp = get_employee(telegram_id)
    return emp.full_name if emp else f"مدير {telegram_id}"


def check_conflicts(employee_id: int, start_date: date, end_date: date) -> bool:
    """True if someone else in the same department has approved leave overlapping the period."""
    employee = session.get(Employee, employee_id)
    if not employee or not employee.department:
        return False
    conflicts = session.query(LeaveRequest).join(Employee, LeaveRequest.employee_id == Employee.id).filter(
        Employee.department == employee.department,
        Employee.id != employee_id,
        LeaveRequest.status == logic.STATUS_APPROVED,
        LeaveRequest.start_date <= end_date,
        LeaveRequest.end_date >= start_date,
    ).count()
    return conflicts > 0


def has_overlapping_leave(employee_id, start_date, end_date) -> bool:
    """True if the employee already has a pending/approved leave overlapping the period."""
    overlap = session.query(LeaveRequest).filter(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.status.in_([logic.STATUS_PENDING, logic.STATUS_APPROVED]),
        LeaveRequest.start_date <= end_date,
        LeaveRequest.end_date >= start_date,
    ).first()
    return overlap is not None


def get_main_menu_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ طلب إجازة جديد", callback_data='new_leave')],
        [
            InlineKeyboardButton("📂 طلباتي", callback_data='my_requests'),
            InlineKeyboardButton("📊 رصيدي", callback_data='my_balance'),
        ],
    ]
    if is_manager(telegram_id):
        keyboard.append([InlineKeyboardButton("👑 قائمة المدير", callback_data='admin_menu')])
    return InlineKeyboardMarkup(keyboard)


def get_admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("مراجعة الطلبات", callback_data='admin_review_leaves')],
        [InlineKeyboardButton("إدارة الموظفين", callback_data='admin_manage_employees')],
        [InlineKeyboardButton("➕ إضافة موظف جديد", callback_data='admin_add_employee')],
        [InlineKeyboardButton("📊 أرصدة الموظفين", callback_data='admin_balances')],
        [InlineKeyboardButton("📥 تقرير الإجازات (Excel)", callback_data='admin_export_report')],
        [InlineKeyboardButton("القائمة الرئيسية", callback_data='main_menu')],
    ])


def leave_decision_keyboard(req_id: int, with_force: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(f"✅ موافقة {req_id}", callback_data=f"admin_approve_{req_id}"),
        InlineKeyboardButton(f"❌ رفض {req_id}", callback_data=f"admin_reject_{req_id}"),
    ]]
    if with_force:
        rows.append([InlineKeyboardButton(f"⚠️ موافقة استثنائية {req_id} (تجاوز الرصيد)", callback_data=f"admin_force_{req_id}")])
    return InlineKeyboardMarkup(rows)


async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    """Edit the callback message; fall back to a new message if editing fails."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.debug("edit_message_text failed (%s); sending new message", e)
        try:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e2:
            logger.error("Could not reply to user: %s", e2)


async def notify_managers(context, message, reply_markup=None, request_type=None, target_id=None, exclude_telegram_id=None):
    managers = session.query(Employee).filter_by(is_manager=True, status='approved').all()
    if not managers:
        logger.warning("No approved managers found to notify.")
        return
    for manager in managers:
        if exclude_telegram_id and manager.telegram_id == exclude_telegram_id:
            continue
        try:
            sent_msg = await context.bot.send_message(chat_id=manager.telegram_id, text=message, reply_markup=reply_markup)
            if request_type and target_id:
                session.add(NotificationLog(
                    request_type=request_type, target_id=target_id,
                    manager_telegram_id=manager.telegram_id, message_id=sent_msg.message_id,
                ))
        except Exception as e:
            logger.error("Failed to notify manager %s: %s", manager.telegram_id, e)
    session.commit()


async def finalize_notifications(context, request_type: str, target_id: int, text: str, acting_telegram_id=None):
    """Replace the buttons in every manager's notification by the final outcome and drop the logs."""
    logs = session.query(NotificationLog).filter_by(request_type=request_type, target_id=target_id).all()
    for log in logs:
        if acting_telegram_id and log.manager_telegram_id == acting_telegram_id:
            continue
        try:
            await context.bot.edit_message_text(chat_id=log.manager_telegram_id, message_id=log.message_id, text=text)
        except Exception:
            # Message may be too old/edited; send a fresh one so the manager still knows.
            try:
                await context.bot.send_message(chat_id=log.manager_telegram_id, text=text)
            except Exception as e:
                logger.error("Failed to update manager %s: %s", log.manager_telegram_id, e)
    session.query(NotificationLog).filter_by(request_type=request_type, target_id=target_id).delete()
    session.commit()


def new_request_text(req: LeaveRequest) -> str:
    emp = req.employee
    replacement_text = "بدون بديل" if not req.replacement_employee_id else f"البديل: {req.replacement_employee.full_name}"
    amount, shortage, unit = logic.shortage_for(emp, req.leave_type, req.start_date, req.end_date, req.start_time, req.end_time)
    text = (
        f"📩 طلب إجازة جديد #{req.id}\n"
        f"الموظف: {emp.full_name} ({emp.department or 'بدون قسم'})\n"
        f"النوع: {req.leave_type}\n"
        f"التفاصيل: {logic.leave_details(req)}\n"
        f"المدة: {logic.fmt(amount)} {unit}\n"
        f"السبب: {req.reason}\n"
        f"{replacement_text}\n\n"
        f"{logic.balance_hint(emp)}"
    )
    if shortage > 0:
        text += f"\n\n⚠️ الطلب يتجاوز الرصيد الحالي بمقدار {logic.fmt(shortage)} {unit}. تحتاج الموافقة إلى قرار استثنائي."
    return text


async def notify_managers_new_request(context, req: LeaveRequest):
    _, shortage, _ = logic.shortage_for(req.employee, req.leave_type, req.start_date, req.end_date, req.start_time, req.end_time)
    await notify_managers(
        context, new_request_text(req),
        reply_markup=leave_decision_keyboard(req.id, with_force=shortage > 0),
        request_type='leave', target_id=req.id,
        exclude_telegram_id=req.employee.telegram_id,
    )


# --------------------------------------------------------------------------
# /start & registration
# --------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session.rollback()
    user = update.effective_user
    employee = get_employee(user.id)

    if employee:
        if employee.status == 'approved':
            await update.message.reply_text(
                f"أهلاً بعودتك، {employee.full_name}! اختر أحد الخيارات:",
                reply_markup=get_main_menu_keyboard(user.id),
            )
        else:
            await update.message.reply_text("حسابك لا يزال قيد المراجعة من قبل الإدارة. سيتم إعلامك عند الموافقة.")
        return MAIN_MENU

    await update.message.reply_text("أهلاً بك في نظام إدارة الإجازات. يرجى إدخال اسمك الكامل للتسجيل:")
    return FULL_NAME


async def full_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or '').strip()
    if len(name) < 3:
        await update.message.reply_text("يرجى إدخال اسم كامل صحيح (3 أحرف على الأقل):")
        return FULL_NAME
    context.user_data['full_name'] = name
    await update.message.reply_text("يرجى إدخال القسم الذي تعمل به:")
    return DEPARTMENT


async def department_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    full_name = context.user_data.get('full_name')
    department = (update.message.text or '').strip()

    if get_employee(user.id):
        await update.message.reply_text("أنت مسجل بالفعل. سيتم نقلك للقائمة الرئيسية.")
        return await start(update, context)

    is_first = session.query(Employee).count() == 0
    try:
        new_employee = Employee(
            telegram_id=user.id,
            full_name=full_name,
            department=department,
            is_manager=is_first,
            status='approved' if is_first else 'pending',
        )
        if is_first:
            logic.grant_initial_balance(new_employee)
        session.add(new_employee)
        session.commit()
    except Exception as e:
        logger.error("Error saving new employee: %s", e)
        session.rollback()
        await update.message.reply_text("حدث خطأ أثناء حفظ بياناتك. يرجى المحاولة لاحقاً.")
        return ConversationHandler.END

    context.user_data.clear()
    if is_first:
        await update.message.reply_text("تم تسجيلك كأول مستخدم وتعيينك كمدير. أهلاً بك!")
        await update.message.reply_text("اختر أحد الخيارات للبدء:", reply_markup=get_main_menu_keyboard(user.id))
        return MAIN_MENU

    await update.message.reply_text("شكراً لتسجيلك. تم إرسال طلبك للإدارة للموافقة. سيتم إعلامك عند الموافقة.")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ موافقة", callback_data=f"approve_user_{new_employee.id}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"reject_user_{new_employee.id}"),
    ]])
    await notify_managers(
        context,
        f"👤 موظف جديد ينتظر الموافقة:\nالاسم: {full_name}\nالقسم: {department}\nID: {new_employee.id}",
        reply_markup=keyboard, request_type='user', target_id=new_employee.id,
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------
# New leave request flow
# --------------------------------------------------------------------------
async def new_leave_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    keyboard = [
        [
            InlineKeyboardButton("إجازة يومية", callback_data='leave_daily'),
            InlineKeyboardButton("إجازة بالساعة", callback_data='leave_hourly'),
        ],
        [InlineKeyboardButton("🔙 إلغاء والعودة", callback_data='cancel_leave')],
    ]
    await safe_edit(query, "اختر نوع الإجازة:", InlineKeyboardMarkup(keyboard))
    return LEAVE_TYPE


async def cancel_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await safe_edit(query, "تم إلغاء العملية. القائمة الرئيسية:", get_main_menu_keyboard(query.from_user.id))
    return MAIN_MENU


async def leave_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['leave_type'] = logic.DAILY if query.data == 'leave_daily' else logic.HOURLY
    if context.user_data['leave_type'] == logic.HOURLY:
        prompt = "أدخل تاريخ الإجازة (YYYY-MM-DD أو مثلاً 'غداً'):"
    else:
        prompt = "أدخل تاريخ البدء (YYYY-MM-DD أو مثلاً 'غداً'):"
    await safe_edit(query, prompt)
    return LEAVE_START_DATE


def _parse_date(text):
    parsed = dateparser.parse(text, settings={'DATE_ORDER': 'YMD', 'PREFER_DATES_FROM': 'future'})
    return parsed.date() if parsed else None


async def leave_start_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    leave_date = _parse_date(update.message.text)
    if not leave_date:
        await update.message.reply_text("لم أتمكن من فهم التاريخ. يرجى المحاولة مرة أخرى (مثال: 2025-10-25 أو 'غداً'):")
        return LEAVE_START_DATE
    if leave_date < date.today():
        await update.message.reply_text("لا يمكن أن يكون التاريخ في الماضي. حاول مرة أخرى:")
        return LEAVE_START_DATE

    context.user_data['start_date'] = leave_date
    if context.user_data['leave_type'] == logic.HOURLY:
        if not logic.is_working_day(leave_date):
            await update.message.reply_text("هذا اليوم عطلة (جمعة أو عطلة رسمية). اختر يوم عمل آخر:")
            return LEAVE_START_DATE
        context.user_data['end_date'] = leave_date
        await update.message.reply_text("أدخل وقت البدء (HH:MM بصيغة 24 ساعة):")
        return LEAVE_START_TIME

    await update.message.reply_text("أدخل تاريخ الانتهاء (YYYY-MM-DD):")
    return LEAVE_END_DATE


async def leave_end_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    end_date = _parse_date(update.message.text)
    if not end_date:
        await update.message.reply_text("لم أتمكن من فهم التاريخ. يرجى المحاولة مرة أخرى:")
        return LEAVE_END_DATE
    if end_date < context.user_data['start_date']:
        await update.message.reply_text("تاريخ الانتهاء لا يمكن أن يكون قبل تاريخ البدء. حاول مرة أخرى:")
        return LEAVE_END_DATE
    context.user_data['end_date'] = end_date

    days = logic.calculate_leave_days(context.user_data['start_date'], end_date)
    if days == 0:
        await update.message.reply_text("الفترة المختارة لا تحتوي أي يوم عمل (الجمعة والعطلات لا تُحسب). أدخل تاريخ انتهاء آخر:")
        return LEAVE_END_DATE
    await update.message.reply_text(f"عدد أيام العمل المطلوبة: {days} يوم (الجمعة والعطلات الرسمية لا تُحسب).\nأدخل سبب الإجازة:")
    return LEAVE_REASON


async def leave_start_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['start_time'] = datetime.strptime(update.message.text.strip(), '%H:%M').time()
    except ValueError:
        await update.message.reply_text("صيغة الوقت غير صالحة. يرجى استخدام HH:MM:")
        return LEAVE_START_TIME
    await update.message.reply_text("أدخل وقت الانتهاء (HH:MM بصيغة 24 ساعة):")
    return LEAVE_END_TIME


async def leave_end_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        end_time = datetime.strptime(update.message.text.strip(), '%H:%M').time()
    except ValueError:
        await update.message.reply_text("صيغة الوقت غير صالحة. يرجى استخدام HH:MM:")
        return LEAVE_END_TIME
    if end_time <= context.user_data['start_time']:
        await update.message.reply_text("وقت الانتهاء يجب أن يكون بعد وقت البدء. حاول مرة أخرى:")
        return LEAVE_END_TIME
    context.user_data['end_time'] = end_time
    hours = logic.calculate_leave_hours(context.user_data['start_time'], end_time)
    await update.message.reply_text(f"المدة المطلوبة: {logic.fmt(hours)} ساعة.\nأدخل سبب الإجازة:")
    return LEAVE_REASON


async def leave_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['reason'] = (update.message.text or '').strip()
    employee = get_employee(update.effective_user.id)
    if not employee:
        context.user_data.clear()
        return ConversationHandler.END

    ud = context.user_data
    if has_overlapping_leave(employee.id, ud['start_date'], ud['end_date']):
        await update.message.reply_text("عذراً، لديك طلب إجازة آخر (قيد الانتظار أو معتمد) في نفس الفترة أو يتداخل معها.")
        context.user_data.clear()
        return ConversationHandler.END

    # Balance check: a shortage does not block the request, it is flagged to the manager.
    amount, shortage, unit = logic.shortage_for(
        employee, ud['leave_type'], ud['start_date'], ud['end_date'], ud.get('start_time'), ud.get('end_time')
    )
    if shortage > 0:
        unused = employee.unused_daily_carryover if ud['leave_type'] == logic.DAILY else employee.unused_hourly_carryover
        msg = (
            f"⚠️ تنبيه: الطلب ({logic.fmt(amount)} {unit}) يتجاوز رصيدك الحالي "
            f"({logic.fmt(logic.current_balance(employee, ud['leave_type']))} {unit}) بمقدار {logic.fmt(shortage)} {unit}.\n"
            "سيُرفع الطلب للإدارة كطلب استثنائي وقرار الموافقة يعود للمدير."
        )
        if unused and unused > 0:
            msg += f"\nملاحظة: لديك {logic.fmt(unused)} {unit} غير مستخدمة من أشهر سابقة ستُعرض على المدير للتقدير."
        await update.message.reply_text(msg)

    try:
        if check_conflicts(employee.id, ud['start_date'], ud['end_date']):
            await update.message.reply_text("⚠️ تنبيه: يوجد موظفون آخرون في قسمك لديهم إجازات معتمدة في نفس الفترة.")
    except Exception as e:
        logger.error("Error checking conflicts: %s", e)

    others = session.query(Employee).filter(
        Employee.telegram_id != employee.telegram_id, Employee.status == 'approved'
    ).order_by(Employee.full_name).all()
    if not others:
        await update.message.reply_text("لا يوجد موظفون آخرون متاحون لتحديدهم كبديل. سيتم المتابعة بدون بديل.")
        context.user_data['replacement_id'] = None
        return await submit_leave_request(update, context)

    keyboard = [[InlineKeyboardButton(emp.full_name, callback_data=f"rep_{emp.id}")] for emp in others]
    keyboard.append([InlineKeyboardButton("لا يوجد بديل", callback_data="rep_0")])
    keyboard.append([InlineKeyboardButton("🔙 إلغاء", callback_data="cancel_leave")])
    await update.message.reply_text("اختر الموظف البديل:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REPLACEMENT_EMPLOYEE


def create_leave_request_record(context, employee_id, status, rep_status):
    ud = context.user_data
    new_request = LeaveRequest(
        employee_id=employee_id,
        leave_type=ud['leave_type'],
        start_date=ud['start_date'],
        end_date=ud['end_date'],
        start_time=ud.get('start_time'),
        end_time=ud.get('end_time'),
        reason=ud.get('reason', ''),
        replacement_employee_id=ud.get('replacement_id'),
        status=status,
        replacement_approval_status=rep_status,
    )
    session.add(new_request)
    session.commit()
    return new_request


async def replacement_employee_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session.rollback()
    query = update.callback_query
    await query.answer()

    replacement_id = int(query.data.split('_')[1])
    context.user_data['replacement_id'] = replacement_id or None
    requester = get_employee(query.from_user.id)
    if not requester or 'start_date' not in context.user_data:
        await safe_edit(query, "انتهت صلاحية الطلب. ابدأ من جديد بـ /start.")
        context.user_data.clear()
        return ConversationHandler.END

    if has_overlapping_leave(requester.id, context.user_data['start_date'], context.user_data['end_date']):
        await safe_edit(query, "عذراً، لديك طلب إجازة آخر في نفس الفترة أو يتداخل معها.")
        context.user_data.clear()
        return ConversationHandler.END

    if not replacement_id:
        return await submit_leave_request(query, context)

    replacement = session.get(Employee, replacement_id)
    if not replacement or replacement.status != 'approved':
        await safe_edit(query, "الموظف البديل غير متاح. ابدأ من جديد بـ /start.")
        context.user_data.clear()
        return ConversationHandler.END

    new_request = create_leave_request_record(context, requester.id, logic.STATUS_PENDING, 'pending')
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ موافق", callback_data=f"rep_accept_{new_request.id}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"rep_reject_{new_request.id}"),
    ]])
    msg_text = (
        f"طلب بديل: الموظف {requester.full_name} يطلب منك أن تكون بديلاً له في إجازته "
        f"{logic.leave_details(new_request)}."
    )
    try:
        await context.bot.send_message(chat_id=replacement.telegram_id, text=msg_text, reply_markup=keyboard)
        await safe_edit(query, f"تم إرسال الطلب للموظف البديل {replacement.full_name}. بانتظار موافقته...")
    except Exception as e:
        logger.error("Failed to send message to replacement: %s", e)
        await safe_edit(query, "فشل إرسال الإشعار للموظف البديل. تأكد من أنه بدأ محادثة مع البوت. تم حفظ الطلب ويمكنك إلغاؤه من 'طلباتي'.")

    context.user_data.clear()
    return ConversationHandler.END


async def replacement_response_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session.rollback()
    query = update.callback_query
    await query.answer()
    try:
        _, action, req_id = query.data.split('_')
        leave_request = session.get(LeaveRequest, int(req_id))
        if not leave_request:
            await safe_edit(query, "عذراً، لم يتم العثور على الطلب.")
            return
        if leave_request.status != logic.STATUS_PENDING or leave_request.replacement_approval_status != 'pending':
            await safe_edit(query, "تمت معالجة هذا الطلب مسبقاً.")
            return
        if leave_request.replacement_employee_id != (get_employee(query.from_user.id) or Employee()).id:
            await query.answer("هذا الطلب ليس موجهاً لك.", show_alert=True)
            return

        requester = leave_request.employee
        if action == 'accept':
            leave_request.replacement_approval_status = 'accepted'
            session.commit()
            await safe_edit(query, "شكراً لك. تم قبول طلب البديل.")
            await context.bot.send_message(
                requester.telegram_id,
                f"وافق {leave_request.replacement_employee.full_name} على أن يكون بديلاً لك. تم رفع الطلب للإدارة.",
            )
            await notify_managers_new_request(context, leave_request)
        else:
            leave_request.replacement_approval_status = 'rejected'
            leave_request.status = logic.STATUS_REJECTED
            leave_request.approved_by = f"البديل {leave_request.replacement_employee.full_name} رفض"
            session.commit()
            await safe_edit(query, "تم رفض طلب البديل.")
            await context.bot.send_message(
                requester.telegram_id,
                f"عذراً، رفض {leave_request.replacement_employee.full_name} طلب البديل. تم إلغاء طلب الإجازة #{leave_request.id}. يمكنك تقديم طلب جديد ببديل آخر.",
            )
    except Exception as e:
        logger.exception("Error in replacement_response_handler: %s", e)
        session.rollback()
        await safe_edit(query, "حدث خطأ أثناء معالجة الطلب. يرجى المحاولة مرة أخرى.")


async def submit_leave_request(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> int:
    session.rollback()
    user_id = update_or_query.effective_user.id if hasattr(update_or_query, 'effective_user') else update_or_query.from_user.id
    employee = get_employee(user_id)

    async def reply(text):
        if isinstance(update_or_query, Update):
            await update_or_query.message.reply_text(text)
        else:
            await safe_edit(update_or_query, text)

    try:
        new_request = create_leave_request_record(context, employee.id, logic.STATUS_PENDING, 'not_required')
        await notify_managers_new_request(context, new_request)
        await reply(f"تم تقديم طلب الإجازة #{new_request.id} بنجاح وهو الآن قيد المراجعة.")
    except Exception as e:
        logger.exception("Error submitting leave request: %s", e)
        session.rollback()
        await reply("حدث خطأ أثناء تقديم طلب الإجازة. يرجى المحاولة مرة أخرى.")
    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Manager decisions (shared by conversation + global handlers)
# --------------------------------------------------------------------------
async def approve_leave_logic(context, query, req_id: int, admin_id: int, force: bool = False):
    admin_name = manager_name(admin_id)
    result = logic.approve_request(req_id, admin_name, force=force)

    if not result.ok:
        if result.needs_force:
            await query.answer(f"رصيد الموظف غير كافٍ (نقص {logic.fmt(result.shortage)} {result.unit}).", show_alert=True)
            req = result.request
            try:
                await query.edit_message_text(
                    new_request_text(req) + "\n\nاختر: موافقة استثنائية (يصبح الرصيد صفراً ويُخصم الفرق من ملاحظة غير المستخدم) أو رفض.",
                    reply_markup=leave_decision_keyboard(req_id, with_force=True),
                )
            except Exception:
                await query.message.reply_text(result.error, reply_markup=leave_decision_keyboard(req_id, with_force=True))
        else:
            await safe_edit(query, result.error, get_admin_menu_keyboard())
        return

    req = result.request
    emp = req.employee
    note = " (موافقة استثنائية)" if result.exceptional else ""
    details = logic.leave_details(req)

    await safe_edit(
        query,
        f"✅ تمت الموافقة على طلب الإجازة #{req.id} للموظف {emp.full_name}{note}.\nالتفاصيل: {details}\n{logic.balance_hint(emp)}",
        get_admin_menu_keyboard(),
    )
    try:
        await context.bot.send_message(
            emp.telegram_id,
            f"✅ تمت الموافقة على طلب الإجازة الخاص بك (ID: {req.id}) من قبل {admin_name}{note}.\n"
            f"التفاصيل: {details}\n"
            f"رصيدك المتبقي هذا الشهر: {logic.fmt(emp.daily_leave_balance)} يوم | {logic.fmt(emp.hourly_leave_balance)} ساعة",
        )
    except Exception as e:
        logger.error("Failed to notify employee %s of approval: %s", emp.telegram_id, e)

    await finalize_notifications(
        context, 'leave', req.id,
        f"✅ تمت الموافقة على طلب الإجازة (ID: {req.id}) للموظف {emp.full_name} من قبل {admin_name}{note}.\nالتفاصيل: {details}",
        acting_telegram_id=admin_id,
    )


async def reject_leave_logic(context, query, req_id: int, admin_id: int):
    admin_name = manager_name(admin_id)
    result = logic.reject_request(req_id, admin_name)
    if not result.ok:
        await safe_edit(query, result.error, get_admin_menu_keyboard())
        return
    req = result.request
    emp = req.employee
    details = logic.leave_details(req)
    await safe_edit(query, f"❌ تم رفض طلب الإجازة #{req.id} للموظف {emp.full_name} بواسطة {admin_name}.", get_admin_menu_keyboard())
    try:
        await context.bot.send_message(emp.telegram_id, f"❌ نأسف، تم رفض طلب إجازتك رقم {req.id} من قبل {admin_name}.\nالتفاصيل: {details}")
    except Exception as e:
        logger.error("Failed to notify employee %s of rejection: %s", emp.telegram_id, e)
    await finalize_notifications(
        context, 'leave', req.id,
        f"❌ تم رفض طلب الإجازة (ID: {req.id}) للموظف {emp.full_name} من قبل {admin_name}.\nالتفاصيل: {details}",
        acting_telegram_id=admin_id,
    )


async def approve_user_logic(context, query, user_id: int, admin_id: int):
    user = session.get(Employee, user_id)
    if not user or user.status != 'pending':
        await safe_edit(query, "المستخدم غير موجود أو تمت الموافقة عليه مسبقاً.")
        return
    user.status = 'approved'
    logic.grant_initial_balance(user)
    session.commit()
    await safe_edit(query, f"تمت الموافقة على {user.full_name}. تم منحه الحصة الشهرية ({logic.fmt(user.daily_leave_balance)} يوم | {logic.fmt(user.hourly_leave_balance)} ساعة).")
    try:
        await context.bot.send_message(user.telegram_id, "تهانينا! تمت الموافقة على حسابك. اضغط /start للبدء.")
    except Exception as e:
        logger.error("Failed to notify new user %s: %s", user.telegram_id, e)
    await finalize_notifications(
        context, 'user', user_id, f"✅ تمت الموافقة على الموظف {user.full_name} من قبل {manager_name(admin_id)}.", acting_telegram_id=admin_id,
    )


async def reject_user_logic(context, query, user_id: int, admin_id: int):
    user = session.get(Employee, user_id)
    if not user:
        await safe_edit(query, "المستخدم غير موجود.")
        return
    if user.status != 'pending':
        await safe_edit(query, "هذا الموظف معتمد بالفعل. استخدم لوحة الويب لحذفه.")
        return
    user_name, user_tid = user.full_name, user.telegram_id
    session.query(LeaveRequest).filter(LeaveRequest.employee_id == user.id).delete()
    session.delete(user)
    session.commit()
    await safe_edit(query, f"تم رفض وحذف {user_name}.")
    try:
        await context.bot.send_message(user_tid, "نأسف، تم رفض طلب تسجيلك.")
    except Exception as e:
        logger.error("Failed to notify rejected user %s: %s", user_tid, e)
    await finalize_notifications(
        context, 'user', user_id, f"❌ تم رفض الموظف {user_name} من قبل {manager_name(admin_id)}.", acting_telegram_id=admin_id,
    )


async def dispatch_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Route approve/reject callbacks. Returns True if the callback was an admin action."""
    query = update.callback_query
    data = query.data
    admin_id = query.from_user.id
    prefixes = ('approve_user_', 'reject_user_', 'admin_approve_', 'admin_reject_', 'admin_force_')
    if not data.startswith(prefixes):
        return False
    if not is_manager(admin_id):
        await query.answer("ليس لديك صلاحيات المدير.", show_alert=True)
        return True
    target_id = int(data.rsplit('_', 1)[1])
    if data.startswith('approve_user_'):
        await approve_user_logic(context, query, target_id, admin_id)
    elif data.startswith('reject_user_'):
        await reject_user_logic(context, query, target_id, admin_id)
    elif data.startswith('admin_approve_'):
        await approve_leave_logic(context, query, target_id, admin_id)
    elif data.startswith('admin_force_'):
        await approve_leave_logic(context, query, target_id, admin_id, force=True)
    elif data.startswith('admin_reject_'):
        await reject_leave_logic(context, query, target_id, admin_id)
    return True


async def global_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve/reject buttons must work even when the manager is not inside the conversation."""
    session.rollback()
    query = update.callback_query
    await query.answer()
    try:
        await dispatch_admin_action(update, context)
    except Exception as e:
        logger.exception("Error in global_admin_handler: %s", e)
        session.rollback()
        await safe_edit(query, "حدث خطأ أثناء معالجة الطلب. يرجى المحاولة مرة أخرى.")


# --------------------------------------------------------------------------
# Employee: cancel own pending request
# --------------------------------------------------------------------------
async def cancel_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session.rollback()
    query = update.callback_query
    await query.answer()
    employee = get_employee(query.from_user.id)
    if not employee:
        return
    req_id = int(query.data.rsplit('_', 1)[1])
    result = logic.cancel_request(req_id, employee.id)
    if not result.ok:
        await query.answer(result.error, show_alert=True)
        return
    await safe_edit(query, f"تم إلغاء طلب الإجازة #{req_id}.", get_main_menu_keyboard(query.from_user.id))
    await finalize_notifications(
        context, 'leave', req_id,
        f"↩️ ألغى الموظف {employee.full_name} طلب الإجازة (ID: {req_id}) {logic.leave_details(result.request)}.",
    )
    if result.request.replacement_employee_id and result.request.replacement_approval_status == 'pending':
        try:
            await context.bot.send_message(
                result.request.replacement_employee.telegram_id,
                f"ألغى {employee.full_name} طلب الإجازة الذي طُلب منك أن تكون بديلاً فيه. لا حاجة للرد.",
            )
        except Exception:
            pass


# --------------------------------------------------------------------------
# Main menu button handler (inside the conversation)
# --------------------------------------------------------------------------
def my_requests_view(employee):
    requests = session.query(LeaveRequest).filter_by(employee_id=employee.id).order_by(LeaveRequest.id.desc()).limit(5).all()
    if not requests:
        return "لا يوجد لديك طلبات إجازة سابقة.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data='main_menu')]])
    icons = {logic.STATUS_PENDING: "⏳", logic.STATUS_APPROVED: "✅", logic.STATUS_REJECTED: "❌", logic.STATUS_CANCELLED: "↩️"}
    lines = ["آخر 5 طلبات إجازة:"]
    buttons = []
    for req in requests:
        rep = ""
        if req.replacement_employee_id and req.replacement_employee:
            rep_label = {'accepted': 'وافق', 'rejected': 'رفض', 'pending': 'بانتظار الرد'}.get(req.replacement_approval_status, req.replacement_approval_status)
            rep = f" | البديل {req.replacement_employee.full_name}: {rep_label}"
        lines.append(f"{icons.get(req.status, '•')} #{req.id} | {req.leave_type} | {logic.leave_details(req)} | {logic.STATUS_LABELS.get(req.status, req.status)}{rep}")
        if req.status == logic.STATUS_PENDING:
            buttons.append([InlineKeyboardButton(f"↩️ إلغاء الطلب #{req.id}", callback_data=f"cancel_req_{req.id}")])
    buttons.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data='main_menu')])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session.rollback()
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    employee = get_employee(user_id)
    if not employee or employee.status != 'approved':
        await safe_edit(query, "حسابك قيد المراجعة. لا يمكنك القيام بأي إجراء حالياً.")
        return MAIN_MENU

    # Buttons that also have global handlers
    if data.startswith(('rep_accept_', 'rep_reject_')):
        await replacement_response_handler(update, context)
        return MAIN_MENU
    if data.startswith('cancel_req_'):
        await cancel_request_handler(update, context)
        return MAIN_MENU
    if await dispatch_admin_action(update, context):
        return MAIN_MENU

    if data == 'main_menu':
        await safe_edit(query, "القائمة الرئيسية:", get_main_menu_keyboard(user_id))
        return MAIN_MENU

    if data == 'new_leave':
        return await new_leave_start(update, context)

    if data == 'my_requests':
        text, kb = my_requests_view(employee)
        await safe_edit(query, text, kb)
        return MAIN_MENU

    if data == 'my_balance':
        unused_d = employee.unused_daily_carryover or 0
        unused_h = employee.unused_hourly_carryover or 0
        text = (
            f"رصيدك لهذا الشهر:\n"
            f"• أيام: {logic.fmt(employee.daily_leave_balance)} من {logic.fmt(employee.monthly_daily_leave_quota)}\n"
            f"• ساعات: {logic.fmt(employee.hourly_leave_balance)} من {logic.fmt(employee.monthly_hourly_leave_quota)}\n\n"
            "ℹ️ يتجدد الرصيد أول كل شهر بدون تراكم. أيام العمل من السبت إلى الخميس."
        )
        if unused_d > 0 or unused_h > 0:
            text += f"\n📝 غير مستخدم من أشهر سابقة (مسجل كملاحظة لدى الإدارة): {logic.fmt(unused_d)} يوم | {logic.fmt(unused_h)} ساعة"
        await safe_edit(query, text, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data='main_menu')]]))
        return MAIN_MENU

    # ---- Manager area ----
    if data.startswith('admin_') and not is_manager(user_id):
        await safe_edit(query, "ليس لديك صلاحيات المدير.", get_main_menu_keyboard(user_id))
        return MAIN_MENU

    if data == 'admin_menu':
        await safe_edit(query, "قائمة المدير:", get_admin_menu_keyboard())
        return MAIN_MENU

    if data == 'admin_review_leaves':
        pending = logic.pending_requests_for_review()
        if not pending:
            await safe_edit(query, "لا يوجد طلبات إجازة بانتظار المراجعة.", get_admin_menu_keyboard())
            return MAIN_MENU
        lines = ["طلبات الإجازة بانتظار المراجعة:"]
        keyboard = []
        for req in pending:
            emp = req.employee
            amount, shortage, unit = logic.shortage_for(emp, req.leave_type, req.start_date, req.end_date, req.start_time, req.end_time)
            lines.append(f"\n#{req.id} {emp.full_name} ({emp.department or '-'}) | {req.leave_type} | {logic.fmt(amount)} {unit}")
            lines.append(f"📅 {logic.leave_details(req)}")
            lines.append(f"💰 رصيد: {logic.fmt(emp.daily_leave_balance)} يوم | {logic.fmt(emp.hourly_leave_balance)} ساعة")
            if (emp.unused_daily_carryover or 0) > 0 or (emp.unused_hourly_carryover or 0) > 0:
                lines.append(f"📝 غير مستخدم سابقاً: {logic.fmt(emp.unused_daily_carryover)} يوم | {logic.fmt(emp.unused_hourly_carryover)} ساعة (للتقدير)")
            if shortage > 0:
                lines.append(f"⚠️ يتجاوز الرصيد بمقدار {logic.fmt(shortage)} {unit}")
            keyboard.extend(leave_decision_keyboard(req.id, with_force=shortage > 0).inline_keyboard)
        keyboard.append([InlineKeyboardButton("🔙 العودة لقائمة المدير", callback_data='admin_menu')])
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))
        return MAIN_MENU

    if data == 'admin_balances':
        employees = session.query(Employee).filter_by(status='approved').order_by(Employee.department, Employee.full_name).all()
        lines = ["📊 أرصدة الموظفين لهذا الشهر:"]
        for emp in employees:
            line = f"• {emp.full_name}: {logic.fmt(emp.daily_leave_balance)}/{logic.fmt(emp.monthly_daily_leave_quota)} يوم | {logic.fmt(emp.hourly_leave_balance)}/{logic.fmt(emp.monthly_hourly_leave_quota)} ساعة"
            if (emp.unused_daily_carryover or 0) > 0 or (emp.unused_hourly_carryover or 0) > 0:
                line += f" | 📝 غير مستخدم سابقاً {logic.fmt(emp.unused_daily_carryover)} ي / {logic.fmt(emp.unused_hourly_carryover)} س"
            lines.append(line)
        text = "\n".join(lines)
        await safe_edit(query, text[:4000], InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة لقائمة المدير", callback_data='admin_menu')]]))
        return MAIN_MENU

    if data == 'admin_export_report':
        await export_report(query, context)
        return MAIN_MENU

    if data == 'admin_manage_employees':
        pending = session.query(Employee).filter_by(status='pending').all()
        if not pending:
            await safe_edit(query, "لا يوجد موظفون في انتظار الموافقة.", get_admin_menu_keyboard())
            return MAIN_MENU
        lines = ["الموظفون في انتظار الموافقة:"]
        keyboard = []
        for emp in pending:
            lines.append(f"- {emp.full_name} ({emp.department or '-'}) ID: {emp.id}")
            keyboard.append([
                InlineKeyboardButton(f"✅ موافقة {emp.full_name}", callback_data=f"approve_user_{emp.id}"),
                InlineKeyboardButton(f"❌ رفض {emp.full_name}", callback_data=f"reject_user_{emp.id}"),
            ])
        keyboard.append([InlineKeyboardButton("🔙 العودة لقائمة المدير", callback_data='admin_menu')])
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))
        return MAIN_MENU

    if data == 'admin_add_employee':
        await safe_edit(query, "يرجى إرسال Telegram ID الخاص بالموظف الجديد (يجب أن يكون رقماً)، أو /cancel للإلغاء:")
        return ADMIN_ADD_EMP_ID

    return MAIN_MENU


async def export_report(query, context):
    try:
        leaves = session.query(LeaveRequest).order_by(LeaveRequest.id.desc()).all()
        if not leaves:
            await safe_edit(query, "لا يوجد إجازات لتصديرها.", get_admin_menu_keyboard())
            return
        holidays = logic.get_holidays()
        data = []
        for leave in leaves:
            if leave.leave_type == logic.DAILY:
                duration = f"{logic.calculate_leave_days(leave.start_date, leave.end_date, holidays)} يوم"
            else:
                duration = f"{logic.fmt(logic.calculate_leave_hours(leave.start_time, leave.end_time))} ساعة"
            data.append({
                'ID': leave.id,
                'الموظف': leave.employee.full_name,
                'القسم': leave.employee.department or '-',
                'النوع': leave.leave_type,
                'الحالة': logic.STATUS_LABELS.get(leave.status, leave.status),
                'من': leave.start_date,
                'إلى': leave.end_date,
                'من ساعة': leave.start_time.strftime('%H:%M') if leave.start_time else '-',
                'إلى ساعة': leave.end_time.strftime('%H:%M') if leave.end_time else '-',
                'المدة': duration,
                'السبب': leave.reason,
                'البديل': leave.replacement_employee.full_name if leave.replacement_employee else 'لا يوجد',
                'تمت المعالجة بواسطة': leave.approved_by or '-',
            })
        balances = [{
            'الموظف': e.full_name,
            'القسم': e.department or '-',
            'رصيد الأيام': e.daily_leave_balance,
            'حصة الأيام الشهرية': e.monthly_daily_leave_quota,
            'رصيد الساعات': e.hourly_leave_balance,
            'حصة الساعات الشهرية': e.monthly_hourly_leave_quota,
            'غير مستخدم سابقاً (أيام)': e.unused_daily_carryover or 0,
            'غير مستخدم سابقاً (ساعات)': e.unused_hourly_carryover or 0,
            'آخر تجديد': e.last_renewal_date,
        } for e in session.query(Employee).filter_by(status='approved').order_by(Employee.full_name).all()]

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            pd.DataFrame(data).to_excel(writer, index=False, sheet_name='الإجازات')
            pd.DataFrame(balances).to_excel(writer, index=False, sheet_name='الأرصدة')
        output.seek(0)
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=output,
            filename=f"leaves_report_{datetime.now().strftime('%Y-%m-%d')}.xlsx",
            caption="📊 تقرير جميع الإجازات وأرصدة الموظفين",
        )
    except Exception as e:
        logger.exception("Error exporting report: %s", e)
        await query.answer("حدث خطأ في تصدير التقرير", show_alert=True)


# --------------------------------------------------------------------------
# Admin: add employee manually
# --------------------------------------------------------------------------
async def admin_add_employee_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        telegram_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("خطأ: يرجى إدخال Telegram ID صحيح (أرقام فقط)، أو /cancel للإلغاء:")
        return ADMIN_ADD_EMP_ID
    if get_employee(telegram_id):
        await update.message.reply_text("هذا Telegram ID موجود مسبقاً في النظام. أدخل ID آخر أو /cancel:")
        return ADMIN_ADD_EMP_ID
    context.user_data['add_emp_id'] = telegram_id
    await update.message.reply_text("يرجى إدخال اسم الموظف الكامل:")
    return ADMIN_ADD_EMP_NAME


async def admin_add_employee_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['add_emp_name'] = update.message.text.strip()
    await update.message.reply_text("يرجى إدخال قسم الموظف:")
    return ADMIN_ADD_EMP_DEPT


async def admin_add_employee_dept_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dept = update.message.text.strip()
    telegram_id = context.user_data.get('add_emp_id')
    full_name = context.user_data.get('add_emp_name')
    user_id = update.effective_user.id
    if not telegram_id or not full_name:
        await update.message.reply_text("حدث خطأ في البيانات. يرجى المحاولة من جديد.", reply_markup=get_main_menu_keyboard(user_id))
        context.user_data.clear()
        return MAIN_MENU
    try:
        new_emp = Employee(telegram_id=telegram_id, full_name=full_name, department=dept, status='approved', is_manager=False)
        logic.grant_initial_balance(new_emp)
        session.add(new_emp)
        session.commit()
        await update.message.reply_text(
            f"✅ تم إضافة الموظف {full_name} كموظف معتمد مع الحصة الشهرية "
            f"({logic.fmt(new_emp.daily_leave_balance)} يوم | {logic.fmt(new_emp.hourly_leave_balance)} ساعة)."
        )
    except Exception as e:
        logger.error("Error creating employee by admin: %s", e)
        session.rollback()
        await update.message.reply_text("❌ حدث خطأ أثناء حفظ البيانات.")
    context.user_data.clear()
    await update.message.reply_text("العودة للقائمة الرئيسية:", reply_markup=get_main_menu_keyboard(user_id))
    return MAIN_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية. اكتب /start للعودة للقائمة.")
    return ConversationHandler.END


async def error_handler(update, context):
    logger.error("Unhandled error while processing update: %s", context.error, exc_info=context.error)
    session.rollback()


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set.")
        return

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(button_handler, pattern='^(main_menu|new_leave|my_requests|my_balance|admin_)'),
        ],
        states={
            FULL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, full_name_handler)],
            DEPARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, department_handler)],
            MAIN_MENU: [CallbackQueryHandler(button_handler)],
            LEAVE_TYPE: [CallbackQueryHandler(leave_type_handler, pattern='^leave_(daily|hourly)$')],
            LEAVE_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_start_date_handler)],
            LEAVE_END_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_end_date_handler)],
            LEAVE_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_start_time_handler)],
            LEAVE_END_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_end_time_handler)],
            LEAVE_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_reason_handler)],
            REPLACEMENT_EMPLOYEE: [CallbackQueryHandler(replacement_employee_handler, pattern=r'^rep_\d+$')],
            ADMIN_ADD_EMP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_employee_id_handler)],
            ADMIN_ADD_EMP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_employee_name_handler)],
            ADMIN_ADD_EMP_DEPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_employee_dept_handler)],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_leave, pattern='^cancel_leave$'),
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
        per_message=False,
    )
    application.add_handler(conv_handler)

    # Global handlers: these buttons arrive outside of any conversation state.
    application.add_handler(CallbackQueryHandler(global_admin_handler, pattern=ADMIN_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(replacement_response_handler, pattern=r'^rep_(accept|reject)_'))
    application.add_handler(CallbackQueryHandler(cancel_request_handler, pattern=r'^cancel_req_'))
    application.add_error_handler(error_handler)

    application.run_polling(stop_signals=None, close_loop=False)


if __name__ == "__main__":
    main()

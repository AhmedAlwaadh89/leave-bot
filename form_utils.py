"""Form parsing and validation helpers shared by the admin dashboard and the employee portal."""
import re
from datetime import datetime

import leave_logic as logic

MIN_PASSWORD_LENGTH = 6

# Arabic-Indic and Persian digits -> ASCII
_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')


def normalize_phone(raw):
    """
    Normalize a phone number used as the employee login.
    Accepts Arabic digits, spaces, dashes and the +964 / 00964 prefixes and
    returns the local form (e.g. 07701234567), or None when it is not a phone number.
    """
    if not raw:
        return None
    s = re.sub(r'\D', '', str(raw).translate(_DIGITS))
    if s.startswith('00964'):
        s = '0' + s[5:]
    elif s.startswith('964'):
        s = '0' + s[3:]
    elif len(s) == 10 and s.startswith('7'):
        s = '0' + s
    if not 8 <= len(s) <= 15:
        return None
    return s


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
    if leave_type == logic.WORK_DUTY and not (form.get('start_time') and form.get('end_time')):
        # Work duty for whole day(s)
        end_date = parse_date(form.get('end_date') or form.get('start_date'), "تاريخ الانتهاء")
        if end_date < start_date:
            raise ValueError("تاريخ الانتهاء لا يمكن أن يكون قبل تاريخ البدء.")
        return leave_type, start_date, end_date, None, None
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

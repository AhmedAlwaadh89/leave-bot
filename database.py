"""
Database models, engine setup and lightweight schema migrations.

Business rules reflected here:
- Every employee has a *monthly* quota (default 2 days and 4 hours).
- The quota is renewed at the start of every month WITHOUT accumulation:
  the usable balance is reset to the quota.
- Whatever was not used in the previous month is recorded in the
  ``unused_*_carryover`` columns.  Those values are informational only
  (a hint for management) and are never usable balance.
"""
import os
import logging
from datetime import datetime

from sqlalchemy import (
    create_engine, inspect, text,
    Column, Integer, String, Boolean, Date, DateTime, Time, Float, ForeignKey, BigInteger,
)
from sqlalchemy.orm import sessionmaker, relationship, scoped_session, declarative_base

logger = logging.getLogger(__name__)

Base = declarative_base()

DEFAULT_MONTHLY_DAYS = 2.0
DEFAULT_MONTHLY_HOURS = 4.0


class Employee(Base):
    __tablename__ = 'employees'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    full_name = Column(String, nullable=False)
    department = Column(String, nullable=True)
    is_manager = Column(Boolean, default=False)
    status = Column(String, default='pending')  # pending / approved

    # Usable balance for the current month
    daily_leave_balance = Column(Float, default=0.0)
    hourly_leave_balance = Column(Float, default=0.0)

    # Monthly quota (reset target at the start of each month)
    monthly_daily_leave_quota = Column(Float, default=DEFAULT_MONTHLY_DAYS)
    monthly_hourly_leave_quota = Column(Float, default=DEFAULT_MONTHLY_HOURS)

    # Informational only: balance that expired unused in previous months.
    unused_daily_carryover = Column(Float, default=0.0)
    unused_hourly_carryover = Column(Float, default=0.0)

    # Date of the last monthly renewal applied to this employee
    last_renewal_date = Column(Date, nullable=True)

    leave_requests = relationship(
        "LeaveRequest", back_populates="employee", foreign_keys="[LeaveRequest.employee_id]"
    )
    replacement_for = relationship(
        "LeaveRequest", back_populates="replacement_employee",
        foreign_keys="[LeaveRequest.replacement_employee_id]"
    )


class LeaveRequest(Base):
    __tablename__ = 'leave_requests'
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey('employees.id'), nullable=False)
    leave_type = Column(String, nullable=False)  # 'يومية' or 'بالساعة'
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    reason = Column(String)
    status = Column(String, default='pending')  # pending / approved / rejected / cancelled
    replacement_employee_id = Column(Integer, ForeignKey('employees.id'), nullable=True)
    replacement_approval_status = Column(String, default='pending')  # pending, accepted, rejected, not_required
    approved_by = Column(String, nullable=True)  # Name of the manager who approved/rejected
    created_at = Column(DateTime, default=datetime.utcnow)

    employee = relationship("Employee", back_populates="leave_requests", foreign_keys=[employee_id])
    replacement_employee = relationship(
        "Employee", back_populates="replacement_for", foreign_keys=[replacement_employee_id]
    )


class NotificationLog(Base):
    __tablename__ = 'notification_logs'
    id = Column(Integer, primary_key=True)
    request_type = Column(String, nullable=False)  # 'leave' or 'user'
    target_id = Column(Integer, nullable=False)  # leave_request_id or employee_id
    manager_telegram_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)


class Holiday(Base):
    """Official holidays (excluded from daily leave calculation)."""
    __tablename__ = 'holidays'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    date = Column(Date, nullable=False, unique=True)


# --------------------------------------------------------------------------
# Engine setup
# --------------------------------------------------------------------------
def mask_url(url):
    if not url:
        return "None"
    try:
        return url.split('@')[-1]  # host/db part only
    except Exception:
        return "Invalid URL format"


def build_engine():
    database_url = (os.getenv('DATABASE_URL') or '').strip()
    if not database_url:
        logger.info("DATABASE_URL not set. Using local SQLite database.")
        return create_engine('sqlite:///leave_management.db')

    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    logger.info("Connecting to database: %s", mask_url(database_url))
    try:
        if database_url.startswith("postgresql"):
            eng = create_engine(
                database_url,
                pool_pre_ping=True,
                pool_recycle=300,
                connect_args={
                    "connect_timeout": 10,
                    "keepalives": 1,
                    "keepalives_idle": 30,
                    "keepalives_interval": 10,
                    "keepalives_count": 5,
                },
            )
        else:
            eng = create_engine(database_url, pool_pre_ping=True)
        with eng.connect():
            pass
        return eng
    except Exception as e:
        logger.error("Error connecting to DATABASE_URL: %s. Falling back to SQLite.", e)
        return create_engine('sqlite:///leave_management.db')


engine = build_engine()
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
# scoped_session: thread-safe access (Flask + Telegram bot + scheduler threads)
session = scoped_session(Session)


# --------------------------------------------------------------------------
# Migrations (idempotent, dialect-aware)
# --------------------------------------------------------------------------
_COLUMN_MIGRATIONS = [
    # (table, column, sqlite DDL, postgresql DDL)
    ('leave_requests', 'approved_by', 'VARCHAR', 'VARCHAR'),
    ('leave_requests', 'created_at', 'TIMESTAMP', 'TIMESTAMP'),
    ('employees', 'department', 'TEXT', 'VARCHAR'),
    ('employees', 'monthly_daily_leave_quota', f'FLOAT DEFAULT {DEFAULT_MONTHLY_DAYS}', f'FLOAT DEFAULT {DEFAULT_MONTHLY_DAYS}'),
    ('employees', 'monthly_hourly_leave_quota', f'FLOAT DEFAULT {DEFAULT_MONTHLY_HOURS}', f'FLOAT DEFAULT {DEFAULT_MONTHLY_HOURS}'),
    ('employees', 'unused_daily_carryover', 'FLOAT DEFAULT 0', 'FLOAT DEFAULT 0'),
    ('employees', 'unused_hourly_carryover', 'FLOAT DEFAULT 0', 'FLOAT DEFAULT 0'),
    ('employees', 'last_renewal_date', 'DATE', 'DATE'),
]

_BIGINT_COLUMNS = [
    ('employees', 'telegram_id'),
    ('notification_logs', 'manager_telegram_id'),
    ('notification_logs', 'message_id'),
]


def run_migrations(target_engine=None):
    """Add missing columns and widen telegram id columns. Safe to run repeatedly."""
    eng = target_engine or engine
    db_type = eng.dialect.name
    try:
        inspector = inspect(eng)
        existing = {}
        for table in {t for t, *_ in _COLUMN_MIGRATIONS} | {t for t, _ in _BIGINT_COLUMNS}:
            if inspector.has_table(table):
                existing[table] = {c['name']: c for c in inspector.get_columns(table)}

        with eng.connect() as conn:
            with conn.begin():
                for table, column, sqlite_ddl, pg_ddl in _COLUMN_MIGRATIONS:
                    if table in existing and column not in existing[table]:
                        ddl = pg_ddl if db_type == 'postgresql' else sqlite_ddl
                        logger.info("Migrating: adding %s.%s", table, column)
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

                if db_type == 'postgresql':
                    for table, column in _BIGINT_COLUMNS:
                        col = existing.get(table, {}).get(column)
                        if col is not None and 'BIGINT' not in str(col['type']).upper():
                            logger.info("Migrating: %s.%s -> BIGINT", table, column)
                            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT"))

                # Null-safe defaults for rows that existed before the new columns
                conn.execute(text("UPDATE employees SET unused_daily_carryover = 0 WHERE unused_daily_carryover IS NULL"))
                conn.execute(text("UPDATE employees SET unused_hourly_carryover = 0 WHERE unused_hourly_carryover IS NULL"))
                conn.execute(text(f"UPDATE employees SET monthly_daily_leave_quota = {DEFAULT_MONTHLY_DAYS} WHERE monthly_daily_leave_quota IS NULL"))
                conn.execute(text(f"UPDATE employees SET monthly_hourly_leave_quota = {DEFAULT_MONTHLY_HOURS} WHERE monthly_hourly_leave_quota IS NULL"))
    except Exception as e:
        logger.error("Migration check failed: %s", e)


run_migrations()

if __name__ == "__main__":
    print("Database tables created/updated successfully.")

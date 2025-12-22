from sqlalchemy import create_engine, Column, Integer, String, Boolean, Date, Time, Float, ForeignKey
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Employee(Base):
    __tablename__ = 'employees'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    full_name = Column(String, nullable=False)
    department = Column(String, nullable=True) # New field
    is_manager = Column(Boolean, default=False)
    status = Column(String, default='pending')
    daily_leave_balance = Column(Float, default=0.0)
    hourly_leave_balance = Column(Float, default=0.0)
    # Monthly quota fields - default 2 days and 4 hours per month
    monthly_daily_leave_quota = Column(Float, default=2.0)
    monthly_hourly_leave_quota = Column(Float, default=4.0)
    leave_requests = relationship("LeaveRequest", back_populates="employee", foreign_keys="[LeaveRequest.employee_id]")
    replacement_for = relationship("LeaveRequest", back_populates="replacement_employee", foreign_keys="[LeaveRequest.replacement_employee_id]")

class LeaveRequest(Base):
    __tablename__ = 'leave_requests'
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey('employees.id'), nullable=False)
    leave_type = Column(String, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    reason = Column(String)
    status = Column(String, default='pending')
    replacement_employee_id = Column(Integer, ForeignKey('employees.id'), nullable=True)
    replacement_approval_status = Column(String, default='pending') # pending, accepted, rejected, not_required
    approved_by = Column(String, nullable=True) # Name of the manager who approved/rejected
    employee = relationship("Employee", back_populates="leave_requests", foreign_keys=[employee_id])
    replacement_employee = relationship("Employee", back_populates="replacement_for", foreign_keys=[replacement_employee_id])

class NotificationLog(Base):
    __tablename__ = 'notification_logs'
    id = Column(Integer, primary_key=True)
    request_type = Column(String, nullable=False) # 'leave' or 'user'
    target_id = Column(Integer, nullable=False)  # leave_request_id or employee_id
    manager_telegram_id = Column(Integer, nullable=False)
    message_id = Column(Integer, nullable=False)

class Holiday(Base):
    """New table to store official holidays."""
    __tablename__ = 'holidays'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    date = Column(Date, nullable=False, unique=True)

import os

# Setup database
# Setup database
# Use DATABASE_URL if available (for production), otherwise fallback to SQLite (for local dev)
database_url = os.getenv('DATABASE_URL')

# Helper to mask URL for logging
def mask_url(url):
    if not url: return "None"
    try:
        return url.split('@')[-1] # Show only host/db part
    except:
        return "Invalid URL format"

if database_url and database_url.strip():
    database_url = database_url.strip()
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    
    print(f"Attempting to connect to:{mask_url(database_url)}")
    
    try:
        # Connection pooling settings for Neon.tech
        engine = create_engine(
            database_url,
            pool_pre_ping=True,  # Test connections before using
            pool_recycle=300,     # Recycle connections after 5 minutes
            connect_args={
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5
            }
        )
        # Test connection
        with engine.connect() as conn:
            pass
    except Exception as e:
        print(f"Error connecting to DATABASE_URL: {e}")
        print("Falling back to SQLite...")
        engine = create_engine('sqlite:///leave_management.db')
else:
    print("DATABASE_URL not set. Using SQLite.")
    engine = create_engine('sqlite:///leave_management.db')
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# Use scoped_session for thread-safe access (Flask + Telegram bot in different threads)
from sqlalchemy.orm import scoped_session
session = scoped_session(Session)

if __name__ == "__main__":
    print("Database tables created/updated successfully.")

# Migration to add approved_by column if it doesn't exist
def run_migrations():
    from sqlalchemy import text
    
    # Detect database type
    db_type = engine.dialect.name
    
    try:
        with engine.connect() as conn:
            with conn.begin():
                if db_type == 'postgresql':
                    # PostgreSQL migrations
                    # Check if approved_by column exists
                    result = conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='leave_requests' AND column_name='approved_by'"
                    ))
                    if not result.fetchone():
                        print("Migrating: Adding approved_by column to leave_requests...")
                        conn.execute(text("ALTER TABLE leave_requests ADD COLUMN approved_by VARCHAR"))
                    
                    # Check if monthly quota columns exist
                    result = conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='employees' AND column_name='monthly_daily_leave_quota'"
                    ))
                    if not result.fetchone():
                        print("Migrating: Adding monthly_daily_leave_quota column to employees...")
                        conn.execute(text("ALTER TABLE employees ADD COLUMN monthly_daily_leave_quota FLOAT DEFAULT 2.0"))
                    
                    result = conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='employees' AND column_name='monthly_hourly_leave_quota'"
                    ))
                    if not result.fetchone():
                        print("Migrating: Adding monthly_hourly_leave_quota column to employees...")
                        conn.execute(text("ALTER TABLE employees ADD COLUMN monthly_hourly_leave_quota FLOAT DEFAULT 4.0"))

                    # Check for department column
                    result = conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='employees' AND column_name='department'"
                    ))
                    if not result.fetchone():
                        print("Migrating: Adding department column to employees...")
                        conn.execute(text("ALTER TABLE employees ADD COLUMN department VARCHAR"))
                        
                elif db_type == 'sqlite':
                    # SQLite migrations
                    # Check for approved_by column
                    result = conn.execute(text("PRAGMA table_info(leave_requests)"))
                    columns = [row[1] for row in result.fetchall()]
                    
                    if 'approved_by' not in columns:
                        print("Migrating: Adding approved_by column to leave_requests...")
                        conn.execute(text("ALTER TABLE leave_requests ADD COLUMN approved_by VARCHAR"))
                    
                    # Check for monthly quota columns
                    result = conn.execute(text("PRAGMA table_info(employees)"))
                    columns = [row[1] for row in result.fetchall()]
                    
                    if 'monthly_daily_leave_quota' not in columns:
                        print("Migrating: Adding monthly_daily_leave_quota column to employees...")
                        conn.execute(text("ALTER TABLE employees ADD COLUMN monthly_daily_leave_quota FLOAT DEFAULT 2.0"))
                    
                    if 'monthly_hourly_leave_quota' not in columns:
                        print("Migrating: Adding monthly_hourly_leave_quota column to employees...")
                        conn.execute(text("ALTER TABLE employees ADD COLUMN monthly_hourly_leave_quota FLOAT DEFAULT 4.0"))
                    
                    if 'department' not in columns:
                        print("Migrating: Adding department column to employees...")
                        conn.execute(text("ALTER TABLE employees ADD COLUMN department TEXT"))

    except Exception as e:
        print(f"Migration check failed: {e}")

run_migrations()


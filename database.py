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
    employee = relationship("Employee", back_populates="leave_requests", foreign_keys=[employee_id])
    replacement_employee = relationship("Employee", back_populates="replacement_for", foreign_keys=[replacement_employee_id])

class Holiday(Base):
    """New table to store official holidays."""
    __tablename__ = 'holidays'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    date = Column(Date, nullable=False, unique=True)

import os

# Setup database
# Use DATABASE_URL if available (for production), otherwise fallback to SQLite (for local dev)
database_url = os.getenv('DATABASE_URL', 'sqlite:///leave_management.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(database_url)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()

if __name__ == "__main__":
    print("Database tables created/updated successfully.")

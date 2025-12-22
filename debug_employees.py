from sqlalchemy import create_engine, text
import os

database_url = os.getenv('DATABASE_URL', 'sqlite:///leave_management.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(database_url)

with engine.connect() as conn:
    result = conn.execute(text("SELECT id, telegram_id, full_name, department, is_manager, status FROM employees"))
    print("ID | Telegram ID | Full Name | Department | Manager | Status")
    print("-" * 70)
    for row in result:
        print(f"{row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]}")

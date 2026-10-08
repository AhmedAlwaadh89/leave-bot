"""
Create all database tables and run the schema migrations.
Usage:  python scripts/create_tables.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import inspect  # noqa: E402
from database import engine, Base, run_migrations, mask_url  # noqa: E402


def create_all_tables():
    print("Creating database tables...")
    print(f"Database: {mask_url(os.getenv('DATABASE_URL')) if os.getenv('DATABASE_URL') else 'SQLite (local)'}")
    Base.metadata.create_all(engine)
    run_migrations()
    inspector = inspect(engine)
    for table in inspector.get_table_names():
        columns = inspector.get_columns(table)
        print(f"\n  {table} ({len(columns)} columns)")
        for col in columns:
            print(f"    - {col['name']} ({col['type']})")
    print("\nDone. Your database is ready.")


if __name__ == "__main__":
    create_all_tables()

"""
Run the idempotent schema migrations against the configured database.
(Migrations also run automatically whenever the app starts.)
Usage:  python scripts/migrate_db.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from database import run_migrations  # noqa: E402

if __name__ == "__main__":
    run_migrations()
    print("Migrations completed.")

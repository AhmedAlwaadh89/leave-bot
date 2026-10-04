"""Shared test setup: force an in-memory SQLite database before importing the app."""
import os
import sys

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ.pop('TELEGRAM_BOT_TOKEN', None)  # never start the bot thread in tests
os.environ['ADMIN_USERNAME'] = 'admin'
os.environ['ADMIN_PASSWORD'] = 'secret'

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import database  # noqa: E402


def reset_db():
    database.session.remove()
    database.Base.metadata.drop_all(database.engine)
    database.Base.metadata.create_all(database.engine)


def new_session():
    reset_db()
    return database.session

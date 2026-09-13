"""Persistent UI context for role-aware Telegram navigation.

The sales session phase and the screen the user is currently viewing are different
things.  A completed training must not keep its result keyboard visible while the
same user is reading the Academy.  This module stores the current navigation
surface separately from training sessions.
"""
import sqlite3
from datetime import datetime, timezone

HOME = 'home'
LEARNING = 'learning'
ASSESSMENT = 'assessment'
TRAINING = 'training'
RESULT = 'result'
MANAGEMENT = 'management'
VALID_CONTEXTS = {HOME, LEARNING, ASSESSMENT, TRAINING, RESULT, MANAGEMENT}


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_ui_schema(path):
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS ui_context(
                user_id INTEGER PRIMARY KEY,
                context TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')


def get_context(path, user_id):
    ensure_ui_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        row = db.execute(
            'SELECT context FROM ui_context WHERE user_id=?',
            (int(user_id),),
        ).fetchone()
    if not row or row[0] not in VALID_CONTEXTS:
        return HOME
    return row[0]


def set_context(path, user_id, context):
    if context not in VALID_CONTEXTS:
        raise ValueError('Unknown UI context')
    ensure_ui_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute('''
            INSERT INTO ui_context(user_id,context,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id)
            DO UPDATE SET context=excluded.context,updated_at=excluded.updated_at
        ''', (int(user_id), context, _now()))
    return context


def reset_context(path, user_id):
    """Return the user to the Academy/home surface."""
    return set_context(path, user_id, HOME)

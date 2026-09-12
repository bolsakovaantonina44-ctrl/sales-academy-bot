"""Persistent access roles for private company learning areas."""
import sqlite3
from datetime import datetime, timezone

PUBLIC = 'public'
AKENSO = 'akenso'
SUPERVISOR = 'supervisor'
ROLES = {PUBLIC, AKENSO, SUPERVISOR}


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_access_schema(path):
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS user_access(
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')


def get_role(path, user_id):
    ensure_access_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        row = db.execute('SELECT role FROM user_access WHERE user_id=?', (int(user_id),)).fetchone()
    role = row[0] if row else PUBLIC
    return role if role in ROLES else PUBLIC


def set_role(path, user_id, role):
    if role not in ROLES:
        raise ValueError('Unknown access role')
    ensure_access_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        if role == PUBLIC:
            db.execute('DELETE FROM user_access WHERE user_id=?', (int(user_id),))
        else:
            db.execute('''
                INSERT INTO user_access(user_id,role,updated_at) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, updated_at=excluded.updated_at
            ''', (int(user_id), role, _now()))
    return role


def has_company_access(path, user_id):
    return get_role(path, user_id) in {AKENSO, SUPERVISOR}


def is_supervisor(path, user_id):
    return get_role(path, user_id) == SUPERVISOR

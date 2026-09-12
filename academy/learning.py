"""Persistence foundation for company knowledge modules and deterministic attestations.

This module intentionally has no Telegram or AI dependencies. It can be wired into the
existing bot without changing sales-training sessions or their scoring.
"""
import json
import sqlite3
from datetime import datetime, timezone


MODULES = (
    {'id': 'product', 'title': 'Продукт'},
    {'id': 'sales', 'title': 'Техники продаж'},
    {'id': 'regulations', 'title': 'Регламенты'},
)
MODULE_IDS = {item['id'] for item in MODULES}
VALID_PROGRESS = {'not_started', 'in_progress', 'completed'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_learning_schema(path):
    """Create additive learning tables next to the existing Academy SQLite database."""
    with sqlite3.connect(str(path), timeout=30) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS learning_progress(
            user_id INTEGER NOT NULL,
            module_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'not_started',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id,module_id)
        );
        CREATE TABLE IF NOT EXISTS assessment_attempts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            module_id TEXT NOT NULL,
            correct INTEGER NOT NULL,
            total INTEGER NOT NULL,
            score INTEGER NOT NULL,
            passed INTEGER NOT NULL,
            answers_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_assessment_user_module
            ON assessment_attempts(user_id,module_id,id);
        ''')


def set_progress(path, user_id, module_id, status):
    if module_id not in MODULE_IDS:
        raise ValueError('Unknown learning module')
    if status not in VALID_PROGRESS:
        raise ValueError('Unknown learning progress status')
    ensure_learning_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute('''
            INSERT INTO learning_progress(user_id,module_id,status,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id,module_id)
            DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at
        ''', (int(user_id), module_id, status, _now()))


def record_assessment(path, user_id, module_id, answers, correct, total, pass_percent=80):
    """Persist one objectively graded attempt and return its result summary."""
    if module_id not in MODULE_IDS:
        raise ValueError('Unknown learning module')
    total = int(total)
    correct = int(correct)
    if total <= 0 or correct < 0 or correct > total:
        raise ValueError('Invalid assessment result')
    score = round(correct * 100 / total)
    passed = score >= int(pass_percent)
    ensure_learning_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        attempt_id = db.execute('''
            INSERT INTO assessment_attempts(
                user_id,module_id,correct,total,score,passed,answers_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
        ''', (int(user_id), module_id, correct, total, score, int(passed),
              json.dumps(answers, ensure_ascii=False), _now())).lastrowid
    if passed:
        set_progress(path, user_id, module_id, 'completed')
    return {
        'attempt_id': attempt_id,
        'module_id': module_id,
        'correct': correct,
        'total': total,
        'score': score,
        'passed': passed,
    }


def progress_snapshot(path, user_id):
    """Return all three modules plus the latest attestation result for each one."""
    ensure_learning_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        db.row_factory = sqlite3.Row
        progress = {
            row['module_id']: row['status']
            for row in db.execute(
                'SELECT module_id,status FROM learning_progress WHERE user_id=?',
                (int(user_id),),
            )
        }
        latest = {}
        for row in db.execute('''
            SELECT a.* FROM assessment_attempts a
            JOIN (
                SELECT module_id,MAX(id) AS max_id
                FROM assessment_attempts
                WHERE user_id=?
                GROUP BY module_id
            ) x ON x.max_id=a.id
            ORDER BY a.id
        ''', (int(user_id),)):
            latest[row['module_id']] = {
                'attempt_id': row['id'],
                'score': row['score'],
                'passed': bool(row['passed']),
                'correct': row['correct'],
                'total': row['total'],
            }
    return [
        {
            'id': module['id'],
            'title': module['title'],
            'status': progress.get(module['id'], 'not_started'),
            'latest_assessment': latest.get(module['id']),
        }
        for module in MODULES
    ]

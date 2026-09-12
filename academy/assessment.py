"""Durable state machine for one short knowledge attestation."""
import json
import sqlite3
from datetime import datetime, timezone

from .curriculum import QUESTION_BANK, PASS_PERCENT
from .learning import MODULE_IDS, ensure_learning_schema, record_assessment, set_progress


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure(path):
    ensure_learning_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS active_assessments(
                user_id INTEGER PRIMARY KEY,
                module_id TEXT NOT NULL,
                question_index INTEGER NOT NULL DEFAULT 0,
                answers_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )
        """)


def start(path, user_id, module_id):
    if module_id not in MODULE_IDS:
        raise ValueError("Unknown learning module")
    _ensure(path)
    set_progress(path, user_id, module_id, "in_progress")
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute("""
            INSERT INTO active_assessments(user_id,module_id,question_index,answers_json,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              module_id=excluded.module_id,question_index=0,answers_json='[]',updated_at=excluded.updated_at
        """, (int(user_id), module_id, 0, "[]", _now()))
    return question(path, user_id)


def question(path, user_id):
    _ensure(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM active_assessments WHERE user_id=?", (int(user_id),)).fetchone()
    if not row:
        return None
    questions = QUESTION_BANK[row["module_id"]]
    index = int(row["question_index"])
    if not 0 <= index < len(questions):
        return None
    data = dict(questions[index])
    data.update(module_id=row["module_id"], index=index, total=len(questions))
    return data


def answer(path, user_id, answer_index):
    current = question(path, user_id)
    if not current:
        raise ValueError("No active assessment")
    option_count = len(current["options"])
    if not 0 <= int(answer_index) < option_count:
        raise ValueError("Invalid option")

    _ensure(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        row = db.execute("SELECT answers_json FROM active_assessments WHERE user_id=?", (int(user_id),)).fetchone()
        answers = json.loads(row[0]) if row else []
        answers.append(int(answer_index))
        next_index = current["index"] + 1
        if next_index < current["total"]:
            db.execute("UPDATE active_assessments SET question_index=?,answers_json=?,updated_at=? WHERE user_id=?",
                       (next_index, json.dumps(answers), _now(), int(user_id)))
            return {"finished": False, "question": question(path, user_id)}

        correct = sum(
            int(value == item["correct"])
            for value, item in zip(answers, QUESTION_BANK[current["module_id"]])
        )
        result = record_assessment(path, user_id, current["module_id"], answers, correct, current["total"], PASS_PERCENT)
        db.execute("DELETE FROM active_assessments WHERE user_id=?", (int(user_id),))
        result.update(finished=True)
        return result

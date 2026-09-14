"""Durable state machine for one knowledge attestation module."""
import json
import sqlite3
from datetime import datetime, timezone

from .attestation_bank import QUESTION_BANK as BASE_QUESTION_BANK, PASS_PERCENT
from .attestation_extensions import EXTRA_QUESTIONS
from .learning import MODULE_IDS, ensure_learning_schema, record_assessment, set_progress


QUESTION_BANK = {
    module_id: list(BASE_QUESTION_BANK[module_id]) + list(EXTRA_QUESTIONS.get(module_id, ()))
    for module_id in BASE_QUESTION_BANK
}


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


def _question_for(user_id, module_id, index):
    """Return one question with stable rotation and mobile-readable answer choices."""
    source = QUESTION_BANK[module_id][int(index)]
    data = dict(source)
    options = list(source["options"])
    if options:
        module_salt = {"product": 0, "sales": 1, "regulations": 2}.get(module_id, 0)
        shift = (int(user_id) + int(index) + module_salt) % len(options)
        if shift:
            options = options[shift:] + options[:shift]
            data["correct"] = (int(source["correct"]) - shift) % len(options)

        # Telegram truncates long inline-button labels on phones. Keep the full
        # answer text inside the message and use only short letter buttons.
        letters = ("А", "Б", "В", "Г", "Д", "Е")
        answer_lines = [f"{letters[pos]}. {option}" for pos, option in enumerate(options)]
        data["question"] = source["question"] + "\n\nВарианты ответа:\n" + "\n".join(answer_lines)
        data["options"] = list(letters[:len(options)])
    return data


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
    data = _question_for(user_id, row["module_id"], index)
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
            finished = False
            result = None
        else:
            correct = sum(
                int(value == _question_for(user_id, current["module_id"], idx)["correct"])
                for idx, value in enumerate(answers)
            )
            result = record_assessment(path, user_id, current["module_id"], answers, correct, current["total"], PASS_PERCENT)
            db.execute("DELETE FROM active_assessments WHERE user_id=?", (int(user_id),))
            result.update(finished=True)
            finished = True
    if not finished:
        return {"finished": False, "question": question(path, user_id)}
    return result

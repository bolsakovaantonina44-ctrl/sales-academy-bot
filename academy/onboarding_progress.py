"""Persistent lesson-level progress for the mobile onboarding route."""
import sqlite3
from datetime import datetime, timezone

from academy.onboarding import MODULE_ORDER, ONBOARDING


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(path):
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS onboarding_progress(
                user_id INTEGER NOT NULL,
                lesson_id TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY(user_id, lesson_id)
            )
            """
        )


def ordered_lessons():
    result = []
    for module_id in MODULE_ORDER:
        for index, lesson in enumerate(ONBOARDING[module_id]["lessons"]):
            result.append((module_id, index, lesson))
    return result


def completed_ids(path, user_id):
    ensure_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        rows = db.execute(
            "SELECT lesson_id FROM onboarding_progress WHERE user_id=?",
            (int(user_id),),
        ).fetchall()
    return {row[0] for row in rows}


def mark_completed(path, user_id, lesson_id):
    valid_ids = {lesson["id"] for _, _, lesson in ordered_lessons()}
    if lesson_id not in valid_ids:
        raise ValueError("Unknown onboarding lesson")
    ensure_schema(path)
    with sqlite3.connect(str(path), timeout=30) as db:
        db.execute(
            """
            INSERT INTO onboarding_progress(user_id,lesson_id,completed_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id,lesson_id) DO UPDATE SET completed_at=excluded.completed_at
            """,
            (int(user_id), lesson_id, _now()),
        )


def progress(path, user_id):
    done = completed_ids(path, user_id)
    lessons = ordered_lessons()
    total = len(lessons)
    completed = sum(1 for _, _, lesson in lessons if lesson["id"] in done)
    modules = {}
    for module_id in MODULE_ORDER:
        items = ONBOARDING[module_id]["lessons"]
        module_done = sum(1 for lesson in items if lesson["id"] in done)
        modules[module_id] = {
            "title": ONBOARDING[module_id]["title"],
            "completed": module_done,
            "total": len(items),
        }
    return {"completed": completed, "total": total, "modules": modules}


def next_lesson(path, user_id):
    done = completed_ids(path, user_id)
    for module_id, index, lesson in ordered_lessons():
        if lesson["id"] not in done:
            return module_id, index, lesson
    return None


def next_after(module_id, index):
    lessons = ordered_lessons()
    for position, (candidate_module, candidate_index, _) in enumerate(lessons):
        if candidate_module == module_id and candidate_index == int(index):
            return lessons[position + 1] if position + 1 < len(lessons) else None
    raise ValueError("Unknown onboarding position")

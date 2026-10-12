"""Batch sales-call attestation with per-series conversion and historical dynamics.

The batch is deliberately stored outside ordinary training sessions. Each call is still
an ordinary Academy session, but carries call_attestation metadata so the existing
trainer/evaluator can be reused without duplicating the conversation engine.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .domain import session_empty, upgrade_session
from .reporting import total_score

ALLOWED_BATCH_SIZES = (10, 20, 30)
INTERRUPTION_SECONDS = 15 * 60
FOCUS_SEQUENCE = ("price", "no_need", "supplier", "send_info", "no_time")
SCENARIOS = (
    ("руководитель отдела закупок", "Холодный звонок в компанию, которая раньше с менеджером не общалась."),
    ("собственник небольшого бизнеса", "Клиент занят и быстро проверяет, есть ли смысл продолжать разговор."),
    ("руководитель проекта", "У клиента есть текущий поставщик, но впереди новый проект."),
    ("коммерческий директор", "Клиент сравнивает несколько вариантов и не хочет тратить время на общую презентацию."),
    ("специалист по закупкам", "Клиент просит информацию письмом и не обещает дальнейшего контакта."),
    ("руководитель подразделения", "Клиент уже сталкивался с похожими предложениями и относится к ним скептически."),
)


def _connect(path):
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def ensure_schema(path):
    with _connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS call_attestation_batches(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                total_calls INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                last_activity_at TEXT NOT NULL,
                interrupted INTEGER NOT NULL DEFAULT 0,
                base_product TEXT NOT NULL DEFAULT '',
                base_customer TEXT NOT NULL DEFAULT '',
                employee_name TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_call_attestation_user
                ON call_attestation_batches(user_id,status,id);
            CREATE TABLE IF NOT EXISTS call_attestation_results(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                call_no INTEGER NOT NULL,
                session_id INTEGER NOT NULL UNIQUE,
                focus TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                score INTEGER,
                converted INTEGER NOT NULL,
                duration_seconds INTEGER,
                completed_at TEXT NOT NULL,
                UNIQUE(batch_id,call_no)
            );
            CREATE INDEX IF NOT EXISTS ix_call_attestation_results_batch
                ON call_attestation_results(batch_id,call_no);
            """
        )


def _latest_useful_session(path, user_id):
    with _connect(path) as db:
        rows = db.execute(
            "SELECT payload FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (int(user_id),),
        ).fetchall()
    for row in rows:
        try:
            session = upgrade_session(json.loads(row["payload"]))
        except Exception:
            continue
        if session.get("source") == "call_attestation":
            continue
        fields = session.get("fields") or {}
        if fields.get("product") or fields.get("customer") or session.get("employee", {}).get("name"):
            return session
    return None


def active_batch(path, user_id):
    ensure_schema(path)
    with _connect(path) as db:
        row = db.execute(
            "SELECT * FROM call_attestation_batches WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
        if not row:
            return None
        count = db.execute(
            "SELECT COUNT(*) FROM call_attestation_results WHERE batch_id=?", (row["id"],)
        ).fetchone()[0]
    result = dict(row)
    result["completed_calls"] = int(count)
    result["next_call_no"] = int(count) + 1
    return result


def batch_by_id(path, batch_id):
    ensure_schema(path)
    with _connect(path) as db:
        row = db.execute("SELECT * FROM call_attestation_batches WHERE id=?", (int(batch_id),)).fetchone()
    return dict(row) if row else None


def start_batch(path, user_id, total_calls, employee_name=""):
    ensure_schema(path)
    total_calls = int(total_calls)
    if total_calls not in ALLOWED_BATCH_SIZES:
        raise ValueError("Batch size must be 10, 20 or 30 calls")
    current = active_batch(path, user_id)
    if current:
        return current

    latest = _latest_useful_session(path, user_id) or {}
    fields = latest.get("fields") or {}
    known_name = " ".join(str(employee_name or latest.get("employee", {}).get("name") or "").split())
    started = _now()
    with _connect(path) as db:
        cursor = db.execute(
            """INSERT INTO call_attestation_batches(
                   user_id,total_calls,status,started_at,last_activity_at,
                   base_product,base_customer,employee_name
               ) VALUES(?,?,'active',?,?,?,?,?)""",
            (
                int(user_id), total_calls, started, started,
                str(fields.get("product") or "продукт или услугу компании"),
                str(fields.get("customer") or "потенциальный корпоративный клиент"),
                known_name,
            ),
        )
        batch_id = cursor.lastrowid
    return active_batch(path, user_id) | {"id": batch_id}


def touch(path, user_id, now=None):
    """Track continuity. A long gap does not erase work, but marks the batch interrupted."""
    batch = active_batch(path, user_id)
    if not batch:
        return None
    current = now or datetime.now(timezone.utc)
    previous = _parse_ts(batch.get("last_activity_at"))
    interrupted = int(batch.get("interrupted") or 0)
    if previous is not None and (current - previous).total_seconds() > INTERRUPTION_SECONDS:
        interrupted = 1
    with _connect(path) as db:
        db.execute(
            "UPDATE call_attestation_batches SET last_activity_at=?, interrupted=? WHERE id=?",
            (current.isoformat(), interrupted, batch["id"]),
        )
    return active_batch(path, user_id)


def difficulty_for_call(call_no, total_calls):
    ratio = (int(call_no) - 1) / max(1, int(total_calls))
    if ratio < 1 / 3:
        return "easy"
    if ratio < 2 / 3:
        return "medium"
    return "hard"


def focus_for_call(batch_id, call_no):
    offset = (int(batch_id) - 1) % len(FOCUS_SEQUENCE)
    return FOCUS_SEQUENCE[(offset + int(call_no) - 1) % len(FOCUS_SEQUENCE)]


def _current_session(path, user_id):
    with _connect(path) as db:
        row = db.execute(
            "SELECT s.payload FROM users u JOIN sessions s ON s.id=u.current_id WHERE u.user_id=?",
            (int(user_id),),
        ).fetchone()
    if not row:
        return None
    try:
        return upgrade_session(json.loads(row["payload"]))
    except Exception:
        return None


def prepare_next_call(path, user_id):
    """Create the next ready training session and make it the user's current session."""
    batch = active_batch(path, user_id)
    if not batch:
        raise ValueError("No active call attestation")
    call_no = batch["next_call_no"]
    if call_no > batch["total_calls"]:
        return None

    current = _current_session(path, user_id)
    if current and current.get("call_attestation", {}).get("batch_id") == batch["id"]:
        if current.get("call_attestation", {}).get("call_no") == call_no and current.get("phase") in {"ready", "active", "closed"}:
            return current
    if current and current.get("phase") in {"active", "closed"}:
        raise ValueError("Finish the current training before starting attestation")

    focus = focus_for_call(batch["id"], call_no)
    difficulty = difficulty_for_call(call_no, batch["total_calls"])
    customer_role, situation = SCENARIOS[(call_no - 1) % len(SCENARIOS)]
    session = session_empty()
    session["source"] = "call_attestation"
    session["phase"] = "ready"
    session["employee"] = {"id": int(user_id), "name": batch.get("employee_name") or "Сотрудник"}
    session["fields"].update(
        product=batch.get("base_product") or "продукт или услугу компании",
        customer=f"{customer_role}; {batch.get('base_customer') or 'корпоративный клиент'}",
        goal="согласовать конкретный следующий шаг с клиентом",
        difficulty=difficulty,
    )
    session["training_focus"] = focus
    session["setup"] = [{"role": "user", "content": f"Экзаменационная ситуация {call_no}: {situation}"}]
    session["call_attestation"] = {
        "batch_id": int(batch["id"]),
        "call_no": int(call_no),
        "total_calls": int(batch["total_calls"]),
        "focus": focus,
        "difficulty": difficulty,
    }

    with _connect(path) as db:
        cursor = db.execute(
            "INSERT INTO sessions(user_id,payload,counted) VALUES(?,?,0)",
            (int(user_id), json.dumps(session, ensure_ascii=False)),
        )
        session["id"] = cursor.lastrowid
        db.execute(
            "UPDATE sessions SET payload=? WHERE id=?",
            (json.dumps(session, ensure_ascii=False), session["id"]),
        )
        db.execute(
            "INSERT INTO users(user_id,current_id) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET current_id=excluded.current_id",
            (int(user_id), session["id"]),
        )
    return session


def session_batch_meta(path, session_id):
    with _connect(path) as db:
        row = db.execute("SELECT payload FROM sessions WHERE id=?", (int(session_id),)).fetchone()
    if not row:
        return None
    try:
        return upgrade_session(json.loads(row["payload"])).get("call_attestation")
    except Exception:
        return None


def record_completed(path, user_id, session=None):
    """Persist one completed call. Idempotent by session_id."""
    ensure_schema(path)
    session = session or _current_session(path, user_id)
    if not session or session.get("phase") != "completed" or not session.get("id"):
        return None
    meta = session.get("call_attestation") or {}
    batch_id = meta.get("batch_id")
    call_no = meta.get("call_no")
    if not batch_id or not call_no:
        return None
    batch = batch_by_id(path, batch_id)
    if not batch or int(batch["user_id"]) != int(user_id):
        return None

    data = session.get("report_data") or {}
    state = session.get("state") or {}
    converted = int(
        state.get("close") == "success"
        or (data.get("goal") == "achieved" and data.get("next_step_status") == "agreed")
    )
    score = total_score(data)
    started_at = _parse_ts(session.get("started_at"))
    completed_at = _parse_ts(session.get("completed_at")) or datetime.now(timezone.utc)
    duration = None
    if started_at is not None:
        duration = max(0, int((completed_at - started_at).total_seconds()))

    with _connect(path) as db:
        existing = db.execute(
            "SELECT id FROM call_attestation_results WHERE session_id=?", (int(session["id"]),)
        ).fetchone()
        if existing:
            return {"recorded": False, "batch": batch_summary(path, batch_id)}
        db.execute(
            """INSERT INTO call_attestation_results(
                   batch_id,call_no,session_id,focus,difficulty,score,converted,duration_seconds,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                int(batch_id), int(call_no), int(session["id"]),
                str(meta.get("focus") or ""), str(meta.get("difficulty") or "medium"),
                score, converted, duration, completed_at.isoformat(),
            ),
        )
        count = db.execute(
            "SELECT COUNT(*) FROM call_attestation_results WHERE batch_id=?", (int(batch_id),)
        ).fetchone()[0]
        if count >= int(batch["total_calls"]):
            db.execute(
                "UPDATE call_attestation_batches SET status='completed',completed_at=?,last_activity_at=? WHERE id=?",
                (completed_at.isoformat(), completed_at.isoformat(), int(batch_id)),
            )
        else:
            db.execute(
                "UPDATE call_attestation_batches SET last_activity_at=? WHERE id=?",
                (completed_at.isoformat(), int(batch_id)),
            )
    return {"recorded": True, "batch": batch_summary(path, batch_id)}


def batch_summary(path, batch_id):
    ensure_schema(path)
    batch = batch_by_id(path, batch_id)
    if not batch:
        return None
    with _connect(path) as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM call_attestation_results WHERE batch_id=? ORDER BY call_no", (int(batch_id),)
        ).fetchall()]
    completed = len(rows)
    successes = sum(int(r["converted"]) for r in rows)
    conversion = round(successes * 100 / completed, 1) if completed else 0.0
    scores = [int(r["score"]) for r in rows if r["score"] is not None]
    average_score = round(sum(scores) / len(scores), 1) if scores else None
    stage_scores = {}
    for difficulty in ("easy", "medium", "hard"):
        values = [int(r["score"]) for r in rows if r["difficulty"] == difficulty and r["score"] is not None]
        stage_scores[difficulty] = round(sum(values) / len(values), 1) if values else None
    durations = [int(r["duration_seconds"]) for r in rows if r["duration_seconds"] is not None]
    average_duration = round(sum(durations) / len(durations)) if durations else None
    return {
        "id": int(batch["id"]),
        "user_id": int(batch["user_id"]),
        "status": batch["status"],
        "total_calls": int(batch["total_calls"]),
        "completed_calls": completed,
        "successes": successes,
        "conversion": conversion,
        "average_score": average_score,
        "stage_scores": stage_scores,
        "average_duration": average_duration,
        "interrupted": bool(batch["interrupted"]),
        "started_at": batch["started_at"],
        "completed_at": batch["completed_at"],
        "employee_name": batch.get("employee_name") if isinstance(batch, dict) else batch["employee_name"],
        "results": rows,
    }


def history(path, user_id, limit=5):
    ensure_schema(path)
    with _connect(path) as db:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM call_attestation_batches WHERE user_id=? AND status='completed' ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()]
    return [batch_summary(path, batch_id) for batch_id in ids]


def format_summary(summary, include_history=None):
    if not summary:
        return "Результат серии пока недоступен."
    avg = "недоступен" if summary["average_score"] is None else f"{summary['average_score']}/100"
    stage_labels = (("easy", "Этап 1 · базовые клиенты"), ("medium", "Этап 2 · рабочая нагрузка"), ("hard", "Этап 3 · сложные клиенты"))
    lines = [
        "АТТЕСТАЦИЯ ПО СЕРИИ ЗВОНКОВ",
        "",
        f"Завершено: {summary['completed_calls']} из {summary['total_calls']}",
        f"Результативных звонков: {summary['successes']}",
        f"Конверсия этой серии: {summary['conversion']}%",
        f"Средний балл навыков: {avg}",
        "",
        "Лесенка сложности:",
    ]
    for key, label in stage_labels:
        value = summary["stage_scores"].get(key)
        lines.append(f"• {label}: {'—' if value is None else str(value) + '/100'}")
    if summary.get("average_duration") is not None:
        minutes, seconds = divmod(int(summary["average_duration"]), 60)
        lines.append(f"• Среднее время звонка: {minutes}:{seconds:02d}")
    lines += ["", "Непрерывность: " + ("нарушена — была пауза более 15 минут" if summary["interrupted"] else "серия пройдена без зафиксированной длинной паузы")]
    if include_history:
        older = [item for item in include_history if item and item["id"] != summary["id"]]
        if older:
            lines += ["", "Динамика предыдущих серий:"]
            for item in older[:4]:
                lines.append(
                    f"• {item['total_calls']} звонков: {item['conversion']}% · средний балл "
                    + ("—" if item["average_score"] is None else str(item["average_score"]))
                )
    lines += ["", "Конверсия считается только внутри этой серии. История серий хранится отдельно и используется для динамики."]
    return "\n".join(lines)

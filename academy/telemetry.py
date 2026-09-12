"""Persistent, privacy-safe telemetry aggregation for the production trainer."""
import os
import sqlite3
from pathlib import Path


def db_path():
    return os.getenv("DB_PATH", "./data/academy.sqlite3")


def ensure_schema(path=None):
    target = path or db_path()
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target, timeout=30) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS telemetry_events("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "event TEXT NOT NULL,"
            "user_id INTEGER,"
            "session_id INTEGER,"
            "stage TEXT,"
            "model TEXT,"
            "status TEXT,"
            "duration_ms INTEGER,"
            "phase_before TEXT,"
            "phase_after TEXT,"
            "diagnostics INTEGER DEFAULT 0)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS ix_telemetry_event ON telemetry_events(event,id)")
        db.execute("CREATE INDEX IF NOT EXISTS ix_telemetry_session ON telemetry_events(session_id,id)")


def record(event, *, user_id=None, session_id=None, stage=None, model=None,
           status=None, duration_ms=None, phase_before=None, phase_after=None,
           diagnostics=0, path=None):
    """Best-effort telemetry write. Never stores prompts, messages, audio, or secrets."""
    target = path or db_path()
    try:
        ensure_schema(target)
        with sqlite3.connect(target, timeout=30) as db:
            db.execute(
                "INSERT INTO telemetry_events(event,user_id,session_id,stage,model,status,duration_ms,"
                "phase_before,phase_after,diagnostics) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event, user_id, session_id, stage, model, status, duration_ms,
                 phase_before, phase_after, int(diagnostics or 0)),
            )
    except Exception:
        # Observability must never break the trainer.
        return


def summary_text(path=None):
    target = path or db_path()
    try:
        ensure_schema(target)
        with sqlite3.connect(target, timeout=30) as db:
            db.row_factory = sqlite3.Row
            starts = db.execute("SELECT COUNT(*) FROM telemetry_events WHERE event='phase_change' AND phase_after='active'").fetchone()[0]
            completed = db.execute("SELECT COUNT(*) FROM telemetry_events WHERE event='phase_change' AND phase_after='completed'").fetchone()[0]
            abandoned = db.execute("SELECT COUNT(*) FROM telemetry_events WHERE event='phase_change' AND phase_after='abandoned'").fetchone()[0]
            ai = db.execute("SELECT COUNT(*) n, COALESCE(AVG(duration_ms),0) avg_ms, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errors FROM telemetry_events WHERE event='ai_request'").fetchone()
            ev = db.execute("SELECT COUNT(*) n, COALESCE(AVG(duration_ms),0) avg_ms, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errors FROM telemetry_events WHERE event='evaluation'").fetchone()
            eval_calls = db.execute("SELECT COUNT(*) FROM telemetry_events WHERE event='ai_request' AND stage='evaluation'").fetchone()[0]
            eval_retries = max(0, eval_calls - int(ev['n'] or 0))
            by_model = db.execute("SELECT COALESCE(model,'?') model, COUNT(*) n FROM telemetry_events WHERE event='ai_request' GROUP BY model ORDER BY n DESC").fetchall()
            try:
                voices = db.execute("SELECT COUNT(*) FROM voice_session_map").fetchone()[0]
            except sqlite3.OperationalError:
                voices = 0
            try:
                current_active = 0
                for row in db.execute("SELECT payload FROM sessions"):
                    payload = row[0] or "{}"
                    if '"phase": "active"' in payload or '"phase":"active"' in payload:
                        current_active += 1
            except sqlite3.OperationalError:
                current_active = 0
    except Exception:
        return "МЕТРИКИ\nНе удалось прочитать telemetry."

    calls_per_completed = (float(ai['n']) / completed) if completed else 0.0
    model_line = ", ".join(f"{r['model']}: {r['n']}" for r in by_model) or "пока нет данных"
    return "\n".join([
        "МЕТРИКИ ТРЕНАЖЁРА",
        "С момента включения telemetry:",
        f"• Начато тренировок: {starts}",
        f"• Завершено: {completed}",
        f"• Брошено/заменено: {abandoned}",
        f"• Сейчас активных сессий: {current_active}",
        f"• Сохранённых голосовых: {voices}",
        "",
        f"• AI-вызовов: {ai['n']} · ошибок: {ai['errors'] or 0} · среднее: {int(ai['avg_ms'] or 0)} мс",
        f"• Evaluation: {ev['n']} · ошибок: {ev['errors'] or 0} · повторных попыток: {eval_retries} · среднее: {int(ev['avg_ms'] or 0)} мс",
        f"• AI-вызовов на завершённую тренировку: {calls_per_completed:.1f}",
        f"• По моделям: {model_line}",
        "",
        "Токены и точная стоимость пока не считаются: сейчас фиксируются вызовы, модель и длительность без текста диалогов.",
    ])

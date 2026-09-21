from academy.store import Store
from academy.reporting import voice_self_correction_lines
from bot import (
    ensure_voice_attempt_schema,
    _save_voice_draft,
    _voice_attempts_for_session,
    _voice_self_correction_summary,
    _sync_voice_self_correction,
)


def _active_session(store, user_id=101):
    store.enqueue("setup", user_id, user_id, "text", "setup")
    event = store.claim()
    session = store.current(user_id)
    session["phase"] = "active"
    store.commit(event, session, [])
    return store.current(user_id)


def test_voice_attempt_summary_counts_statuses():
    attempts = [
        {"status": "replaced"},
        {"status": "confirmed"},
        {"status": "pending"},
        {"status": "replaced"},
    ]
    assert _voice_self_correction_summary(attempts) == {
        "total_attempts": 4,
        "replaced_attempts": 2,
        "confirmed_attempts": 1,
        "pending_attempts": 1,
    }


def test_voice_retry_is_persisted_and_visible_after_reopen(tmp_path):
    db_path = tmp_path / "academy.sqlite3"
    store = Store(db_path)
    ensure_voice_attempt_schema(db_path)
    session = _active_session(store)
    session_id = session["id"]

    store.enqueue("voice-1", 101, 101, "voice_draft", "file-1")
    first = store.claim()
    attempt_id_1, attempt_no_1 = _save_voice_draft(store, first, "Первый вариант")
    assert attempt_no_1 == 1

    store.enqueue("voice-2", 101, 101, "voice_draft", "file-2")
    second = store.claim()
    attempt_id_2, attempt_no_2 = _save_voice_draft(store, second, "Второй вариант")
    assert attempt_no_2 == 2
    assert attempt_id_2 != attempt_id_1

    attempts = _voice_attempts_for_session(store, session_id)
    assert [item["status"] for item in attempts] == ["replaced", "pending"]
    assert [item["transcript"] for item in attempts] == ["Первый вариант", "Второй вариант"]

    reopened = Store(db_path)
    persisted = _voice_attempts_for_session(reopened, session_id)
    assert [item["status"] for item in persisted] == ["replaced", "pending"]
    summary = reopened.current(101)["voice_self_correction"]
    assert summary["total_attempts"] == 2
    assert summary["replaced_attempts"] == 1
    assert summary["pending_attempts"] == 1


def test_confirmed_voice_summary_is_written_to_session(tmp_path):
    db_path = tmp_path / "academy.sqlite3"
    store = Store(db_path)
    ensure_voice_attempt_schema(db_path)
    session = _active_session(store)
    session_id = session["id"]

    store.enqueue("voice-1", 101, 101, "voice_draft", "file-1")
    event = store.claim()
    attempt_id, _ = _save_voice_draft(store, event, "Финальный вариант")

    with store.db() as db:
        db.execute(
            "UPDATE voice_attempts SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP WHERE id=?",
            (attempt_id,),
        )
    summary = _sync_voice_self_correction(store, 101, session_id)

    assert summary["confirmed_attempts"] == 1
    assert summary["pending_attempts"] == 0
    assert store.current(101)["voice_self_correction"] == summary


def test_report_describes_self_correction_without_claiming_improvement():
    session = {
        "voice_self_correction": {
            "total_attempts": 3,
            "replaced_attempts": 2,
            "confirmed_attempts": 1,
            "pending_attempts": 0,
        }
    }
    lines = voice_self_correction_lines(session)
    text = " ".join(lines)
    assert "Голосовых версий: 3" in text
    assert "Перезаписано сотрудником: 2" in text
    assert "Подтверждено и отправлено клиенту: 1" in text
    assert "улучш" not in text.lower()

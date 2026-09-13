import sqlite3

from academy.ui_state import (
    HOME,
    LEARNING,
    ASSESSMENT,
    TRAINING,
    RESULT,
    MANAGEMENT,
    get_context,
    set_context,
    reset_context,
)


def test_default_context_is_home(tmp_path):
    db = tmp_path / 'academy.sqlite3'
    assert get_context(db, 101) == HOME


def test_context_persists_per_user(tmp_path):
    db = tmp_path / 'academy.sqlite3'
    set_context(db, 101, LEARNING)
    set_context(db, 202, TRAINING)
    assert get_context(db, 101) == LEARNING
    assert get_context(db, 202) == TRAINING


def test_all_supported_contexts_round_trip(tmp_path):
    db = tmp_path / 'academy.sqlite3'
    contexts = [HOME, LEARNING, ASSESSMENT, TRAINING, RESULT, MANAGEMENT]
    for index, context in enumerate(contexts, 1):
        set_context(db, index, context)
        assert get_context(db, index) == context


def test_invalid_context_is_rejected(tmp_path):
    db = tmp_path / 'academy.sqlite3'
    try:
        set_context(db, 101, 'admin_everything')
    except ValueError:
        pass
    else:
        raise AssertionError('invalid UI context must raise ValueError')


def test_reset_returns_to_home(tmp_path):
    db = tmp_path / 'academy.sqlite3'
    set_context(db, 101, RESULT)
    reset_context(db, 101)
    assert get_context(db, 101) == HOME


def test_corrupt_context_falls_back_to_home(tmp_path):
    db = tmp_path / 'academy.sqlite3'
    get_context(db, 101)  # create schema
    with sqlite3.connect(db) as conn:
        conn.execute(
            'INSERT OR REPLACE INTO ui_context(user_id,context,updated_at) VALUES(?,?,?)',
            (101, 'legacy_bad_value', '2026-09-13T00:00:00+00:00'),
        )
    assert get_context(db, 101) == HOME

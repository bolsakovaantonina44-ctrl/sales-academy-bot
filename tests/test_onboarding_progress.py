import tempfile
from pathlib import Path

from academy.onboarding_progress import (
    ordered_lessons,
    progress,
    next_lesson,
    next_after,
    mark_completed,
)


def _db():
    return Path(tempfile.mkdtemp()) / "academy.sqlite3"


def test_onboarding_has_fourteen_lessons_in_required_order():
    lessons = ordered_lessons()
    assert len(lessons) == 14
    assert [module_id for module_id, _, _ in lessons[:6]] == ["product"] * 6
    assert [module_id for module_id, _, _ in lessons[6:12]] == ["sales"] * 6
    assert [module_id for module_id, _, _ in lessons[12:]] == ["regulations"] * 2


def test_progress_starts_empty_and_resumes_first_unfinished_lesson():
    path = _db()
    user_id = 101
    snapshot = progress(path, user_id)
    assert snapshot["completed"] == 0
    assert snapshot["total"] == 14

    module_id, index, lesson = next_lesson(path, user_id)
    assert (module_id, index, lesson["id"]) == ("product", 0, "product-1")

    mark_completed(path, user_id, "product-1")
    module_id, index, lesson = next_lesson(path, user_id)
    assert (module_id, index, lesson["id"]) == ("product", 1, "product-2")


def test_progress_is_isolated_between_employees():
    path = _db()
    mark_completed(path, 1, "product-1")
    assert progress(path, 1)["completed"] == 1
    assert progress(path, 2)["completed"] == 0


def test_next_after_crosses_module_boundaries():
    module_id, index, lesson = next_after("product", 5)
    assert (module_id, index, lesson["id"]) == ("sales", 0, "sales-1")

    module_id, index, lesson = next_after("sales", 5)
    assert (module_id, index, lesson["id"]) == ("regulations", 0, "regulations-1")

    assert next_after("regulations", 1) is None


def test_completion_is_idempotent():
    path = _db()
    mark_completed(path, 7, "product-1")
    mark_completed(path, 7, "product-1")
    assert progress(path, 7)["completed"] == 1

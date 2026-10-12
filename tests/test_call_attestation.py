import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from academy.call_attestation import (
    active_batch,
    batch_summary,
    difficulty_for_call,
    format_summary,
    history,
    prepare_next_call,
    record_completed,
    start_batch,
    touch,
)
from academy.domain import SKILLS
from academy.store import Store


class CallAttestationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "academy.sqlite3")
        Store(self.path)
        self.user_id = 101

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def report(score=80, goal="achieved", next_step_status="agreed"):
        maximums = {key: maximum for key, _, maximum in SKILLS}
        remaining = int(score)
        skills = []
        for key, _, maximum in SKILLS:
            value = min(maximum, remaining)
            remaining -= value
            skills.append({"id": key, "score": value, "reason": "ok", "evidence": []})
        return {
            "simulation_valid": True,
            "simulation_issues": [],
            "skills": skills,
            "goal": goal,
            "outcome": 3 if goal == "achieved" else 1,
            "next_step": "созвон",
            "next_step_status": next_step_status,
            "strengths": [],
            "mistakes": [],
            "findings": [],
            "recommendations": [],
            "revealed": [],
            "missed": [],
        }

    def complete(self, session, converted=True, score=80):
        session["phase"] = "completed"
        session["started_at"] = datetime.now(timezone.utc).isoformat()
        session["completed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        session["state"]["close"] = "success" if converted else "refusal"
        session["report_data"] = self.report(
            score=score,
            goal="achieved" if converted else "not_achieved",
            next_step_status="agreed" if converted else "absent",
        )
        return record_completed(self.path, self.user_id, session=session)

    def test_batch_uses_ladder_and_counts_conversion_inside_series(self):
        batch = start_batch(self.path, self.user_id, 10, "Иван Иванов")
        self.assertEqual(batch["total_calls"], 10)
        self.assertEqual(difficulty_for_call(1, 10), "easy")
        self.assertEqual(difficulty_for_call(5, 10), "medium")
        self.assertEqual(difficulty_for_call(10, 10), "hard")

        for index in range(1, 11):
            session = prepare_next_call(self.path, self.user_id)
            self.assertEqual(session["call_attestation"]["call_no"], index)
            result = self.complete(session, converted=index <= 4, score=70 + index)
            self.assertTrue(result["recorded"])

        summary = batch_summary(self.path, batch["id"])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["successes"], 4)
        self.assertEqual(summary["conversion"], 40.0)
        self.assertEqual(summary["completed_calls"], 10)
        self.assertIsNotNone(summary["stage_scores"]["easy"])
        self.assertIsNotNone(summary["stage_scores"]["medium"])
        self.assertIsNotNone(summary["stage_scores"]["hard"])

    def test_duplicate_session_is_not_counted_twice(self):
        batch = start_batch(self.path, self.user_id, 10, "Иван Иванов")
        session = prepare_next_call(self.path, self.user_id)
        first = self.complete(session, converted=True, score=80)
        second = record_completed(self.path, self.user_id, session=session)
        self.assertTrue(first["recorded"])
        self.assertFalse(second["recorded"])
        self.assertEqual(batch_summary(self.path, batch["id"])["completed_calls"], 1)

    def test_long_gap_marks_batch_interrupted_without_erasing_progress(self):
        start = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        batch = start_batch(self.path, self.user_id, 10, "Иван Иванов")
        with Store(self.path).db() as db:
            db.execute(
                "UPDATE call_attestation_batches SET last_activity_at=? WHERE id=?",
                (start.isoformat(), batch["id"]),
            )
        updated = touch(self.path, self.user_id, now=start + timedelta(minutes=16))
        self.assertEqual(updated["interrupted"], 1)
        self.assertEqual(updated["completed_calls"], 0)

    def test_history_is_separate_from_current_batch_conversion(self):
        first = start_batch(self.path, self.user_id, 10, "Иван Иванов")
        for index in range(10):
            session = prepare_next_call(self.path, self.user_id)
            self.complete(session, converted=index < 5, score=75)
        self.assertEqual(batch_summary(self.path, first["id"])["conversion"], 50.0)

        second = start_batch(self.path, self.user_id, 10, "Иван Иванов")
        for index in range(10):
            session = prepare_next_call(self.path, self.user_id)
            self.complete(session, converted=index < 7, score=82)
        current = batch_summary(self.path, second["id"])
        self.assertEqual(current["conversion"], 70.0)
        previous = history(self.path, self.user_id, limit=5)
        text = format_summary(current, include_history=previous)
        self.assertIn("Конверсия этой серии: 70.0%", text)
        self.assertIn("10 звонков: 50.0%", text)

    def test_active_batch_prevents_starting_a_second_series(self):
        first = start_batch(self.path, self.user_id, 20, "Иван Иванов")
        second = start_batch(self.path, self.user_id, 30, "Иван Иванов")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(active_batch(self.path, self.user_id)["total_calls"], 20)


if __name__ == "__main__":
    unittest.main()

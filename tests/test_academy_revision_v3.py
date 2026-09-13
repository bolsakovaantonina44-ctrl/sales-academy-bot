import json
import os
import sqlite3
import tempfile
import unittest

from academy import admission
from academy.curriculum import MODULE_CONTENT, QUESTION_BANK
from academy.domain import session_empty
from academy.learning import record_assessment
from academy.onboarding import ONBOARDING, total_lessons
from academy.store import Store


class AcademyRevisionV3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "academy.sqlite3")
        self.store = Store(self.path)
        self.user_id = 101

    def tearDown(self):
        self.tmp.cleanup()

    def _pass_theory(self, score=100):
        total = 10
        correct = round(total * score / 100)
        for module_id in ("product", "sales", "regulations"):
            record_assessment(self.path, self.user_id, module_id, [0] * total, correct, total, 80)

    def _practice(self, score):
        session = session_empty()
        session["phase"] = "completed"
        # total_score only accepts a complete, valid eight-skill report.
        values = [score // 8] * 8
        for i in range(score % 8):
            values[i] += 1
        session["report_data"] = {
            "simulation_valid": True,
            "technical_partial": False,
            "skills": [
                {"id": f"s{i}", "score": value, "reason": "test", "evidence": []}
                for i, value in enumerate(values)
            ],
        }
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO sessions(user_id,payload,counted) VALUES(?,?,1)",
                (self.user_id, json.dumps(session, ensure_ascii=False)),
            )

    def test_mobile_route_is_expanded(self):
        self.assertEqual(total_lessons(), 14)
        self.assertEqual(len(ONBOARDING["product"]["lessons"]), 6)
        self.assertEqual(len(ONBOARDING["sales"]["lessons"]), 6)

    def test_forbidden_or_outdated_product_names_are_absent(self):
        text = repr(MODULE_CONTENT) + repr(ONBOARDING)
        for forbidden in ("ITALON", "Arlequino", "Aqueastrelle", "Mirabo", "Мирабо", "Карловская"):
            self.assertNotIn(forbidden, text)

    def test_questions_do_not_test_caliber_or_batches(self):
        questions = " ".join(
            item["question"].lower()
            for group in QUESTION_BANK.values()
            for item in group
        )
        self.assertNotIn("калибр", questions)
        self.assertNotIn("парт", questions)

    def test_theory_alone_never_grants_admission(self):
        self._pass_theory()
        result = admission.assess(self.path, self.user_id)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["practice"])
        self.assertIn("практический", result["decision"].lower())

    def test_weak_practice_blocks_independent_admission(self):
        self._pass_theory()
        self._practice(50)
        result = admission.assess(self.path, self.user_id)
        self.assertTrue(result["complete"])
        self.assertIn("не рекомендован", result["decision"].lower())
        self.assertEqual(result["practice"]["score"], 50)

    def test_strong_practice_is_main_verdict(self):
        self._pass_theory()
        self._practice(80)
        result = admission.assess(self.path, self.user_id)
        self.assertTrue(result["complete"])
        self.assertEqual(result["decision"], "Допуск к самостоятельным переговорам")
        self.assertGreater(result["grade"], 80)


if __name__ == "__main__":
    unittest.main()

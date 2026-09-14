import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academy import admission, assessment
from academy.learning import record_assessment
from academy.onboarding import ONBOARDING
from academy.onboarding_progress import ordered_lessons


class AcademyRevisionTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "academy.sqlite3"
        self.user_id = 7001

    def _record_scores(self, scores):
        modules = ("product", "sales", "regulations")
        for module_id, score in zip(modules, scores):
            total = 10
            correct = round(score / 10)
            record_assessment(self.path, self.user_id, module_id, [0] * total, correct, total, 80)

    def test_onboarding_revision_has_fourteen_lessons(self):
        lessons = ordered_lessons()
        self.assertEqual(len(lessons), 14)
        self.assertEqual([m for m, _, _ in lessons[:6]], ["product"] * 6)
        self.assertEqual([m for m, _, _ in lessons[6:12]], ["sales"] * 6)
        self.assertEqual([m for m, _, _ in lessons[12:]], ["regulations"] * 2)

    def test_current_product_knowledge_matches_manager_revision(self):
        text = repr(ONBOARDING["product"]).lower()
        self.assertIn("600×600", text)
        self.assertIn("600×1200", text)
        self.assertIn("300×600", text)
        self.assertIn("белое солнце", text)
        self.assertIn("соль-перец", text)
        self.assertIn("самарский стройфарфор", text)
        self.assertIn("35 дней", text)
        self.assertIn("128,16", text)
        self.assertNotIn("equipe", text)
        self.assertNotIn("italon", text)

    def test_sales_teaches_open_and_closed_questions(self):
        text = repr(ONBOARDING["sales"]).lower()
        self.assertIn("открытые", text)
        self.assertIn("закрытые", text)
        self.assertIn("кто согласует", text)
        self.assertIn("бюджет", text)
        self.assertIn("следующий шаг", text)

    def test_attestation_keeps_calibre_tone_and_adds_real_calculation(self):
        text = repr(assessment.QUESTION_BANK).lower()
        self.assertIn("калибр", text)
        self.assertIn("тон", text)
        self.assertIn("356", text)
        self.assertIn("128,16", text)
        self.assertIn("самарском стройфарфоре", text)
        self.assertIn("открытых и закрытых", text)
        self.assertNotIn("italon", text)
        self.assertNotIn("arlequino", text)
        self.assertNotIn("aqueastrelle", text)
        self.assertNotIn("equipe", text)
        self.assertGreaterEqual(len(assessment.QUESTION_BANK["product"]), 12)
        self.assertGreaterEqual(len(assessment.QUESTION_BANK["sales"]), 10)
        self.assertGreaterEqual(len(assessment.QUESTION_BANK["regulations"]), 5)

    def test_theory_alone_never_grants_admission(self):
        self._record_scores((100, 100, 100))
        with patch("academy.admission._latest_practice", return_value=None):
            result = admission.assess(self.path, self.user_id)
        self.assertFalse(result["complete"])
        self.assertFalse(result["passed"])
        self.assertIn("Практический экзамен", result["missing"][0])

    def test_practice_has_main_weight_and_low_practice_blocks_pass(self):
        self._record_scores((100, 100, 100))
        fake = {"session_id": 99, "score": 60, "focus": "project", "difficulty": "3"}
        with patch("academy.admission._latest_practice", return_value=fake):
            result = admission.assess(self.path, self.user_id)
        self.assertEqual(result["final_score"], 76.0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["decision"], "Допуск пока не рекомендован")

    def test_strong_practice_passes_and_invites_to_company_chat(self):
        self._record_scores((90, 90, 90))
        fake = {"session_id": 100, "score": 90, "focus": "project", "difficulty": "3"}
        with patch("academy.admission._latest_practice", return_value=fake):
            result = admission.assess(self.path, self.user_id)
        self.assertTrue(result["passed"])
        self.assertEqual(result["final_score"], 90.0)
        text = admission.employee_text(result)
        self.assertIn("https://t.me/+F-D9K-WkUEwxNjli", text)

    def test_start_practical_exam_creates_new_boundary(self):
        baseline = admission.start_practical_exam(self.path, self.user_id)
        self.assertEqual(baseline, 0)
        self.assertIsNone(admission._latest_practice(self.path, self.user_id))


if __name__ == "__main__":
    unittest.main()

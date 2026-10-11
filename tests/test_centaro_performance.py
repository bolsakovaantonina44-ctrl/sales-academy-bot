import tempfile
import unittest
from pathlib import Path

from academy.centaro_performance_runtime import build_projection, combined_report, series_metrics
from academy.domain import SKILLS, session_empty
from academy.store import Store


class CentaroPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "academy.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def report_data(score=70):
        # Preserve the production eight-skill/100-point contract.
        remaining = score
        skills = []
        for index, (key, _, maximum) in enumerate(SKILLS):
            slots = len(SKILLS) - index
            value = min(maximum, max(0, round(remaining / slots)))
            skills.append({"id": key, "score": value, "reason": "test", "evidence": []})
            remaining -= value
        # Adjust exact total without exceeding skill maxima.
        delta = score - sum(item["score"] for item in skills)
        for item, (_, _, maximum) in zip(skills, SKILLS):
            if not delta:
                break
            capacity = maximum - item["score"] if delta > 0 else item["score"]
            change = min(abs(delta), capacity) * (1 if delta > 0 else -1)
            item["score"] += change
            delta -= change
        return {
            "simulation_valid": True,
            "technical_partial": False,
            "skills": skills,
        }

    def result(self, achieved, outcome="no_agreement", score=70):
        session = session_empty()
        session["report_data"] = self.report_data(score)
        return {
            "status": "completed",
            "assessment_status": "verified",
            "outcome_status": "verified",
            "goal_achieved": achieved,
            "outcome": outcome,
            "session": session,
        }

    def series(self, goals=None):
        return {
            "id": 1,
            "user_id": 10,
            "status": "completed",
            "mode": "practice",
            "config": {
                "count": 10,
                "product": "CRM",
                "customer": "Собственник бизнеса",
                "goals": goals or ["purchase"],
            },
        }

    def test_training_conversion_and_plan_forecast(self):
        results = [self.result(i < 3, "purchase" if i < 3 else "no_agreement", 70) for i in range(10)]
        series = self.series(["purchase"])
        metrics = series_metrics(series, results)
        self.assertAlmostEqual(metrics["training_conversion"], 0.3)
        self.assertEqual(metrics["goal_achieved"], 3)
        self.assertEqual(metrics["quality_calls"], 10)

        projection, _ = build_projection(series, results, {
            "average_check": 100000,
            "monthly_contacts": 100,
            "monthly_plan": 4000000,
            "downstream_percent": 100,
        })
        self.assertAlmostEqual(projection["expected_sales"], 30)
        self.assertAlmostEqual(projection["projected_revenue"], 3000000)
        self.assertAlmostEqual(projection["plan_percent"], 75)

    def test_non_purchase_goal_uses_downstream_company_conversion(self):
        results = [self.result(i < 5, "meeting" if i < 5 else "no_agreement", 65) for i in range(10)]
        projection, _ = build_projection(self.series(["meeting"]), results, {
            "average_check": 200000,
            "monthly_contacts": 80,
            "monthly_plan": 2000000,
            "downstream_percent": 25,
        })
        # 80 contacts * 50% training conversion * 25% meeting->sale = 10 sales.
        self.assertAlmostEqual(projection["expected_sales"], 10)
        self.assertAlmostEqual(projection["projected_revenue"], 2000000)
        self.assertAlmostEqual(projection["plan_percent"], 100)

    def test_combined_report_labels_forecast_as_model(self):
        results = [self.result(i < 2, "purchase" if i < 2 else "no_agreement", 60) for i in range(10)]
        series = self.series(["purchase"])
        projection, _ = build_projection(series, results, {
            "average_check": 100000,
            "monthly_contacts": 100,
            "monthly_plan": 2500000,
            "downstream_percent": 100,
        })
        series["business_projection"] = projection
        series["comparison_projection"] = None
        text = combined_report(self.store, series, results)
        self.assertIn("CENTARO · ИТОГ ЭФФЕКТИВНОСТИ", text)
        self.assertIn("Модельное выполнение плана", text)
        self.assertIn("не гарантия реальных продаж", text)
        self.assertIn("CENTARO · НАВИГАТОР ПО ТЕХНИКЕ", text)


if __name__ == "__main__":
    unittest.main()

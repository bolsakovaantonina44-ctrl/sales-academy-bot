import unittest

from academy import call_series
from academy.centaro_role_reports_runtime import CONVERSION_GOALS, _prompt, install
from academy.domain import validate_card


class CentaroConversionGoalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install()

    def test_supervisor_conversion_prompt_has_business_goals(self):
        text = _prompt({"mode": "attestation", "step": "conversion_goal", "data": {}})
        self.assertIn("Встреча", text)
        self.assertIn("Продажа", text)
        self.assertIn("Получение сделки", text)
        self.assertIn("Приглашение / визит в салон", text)
        self.assertEqual(CONVERSION_GOALS["deal"]["engine_goal"], "deal")
        self.assertEqual(CONVERSION_GOALS["salon"]["engine_goal"], "showroom")

    def test_showroom_is_not_generic_meeting(self):
        cfg, calls = call_series.build_manifest({
            "count": 10,
            "product": "Керамогранит",
            "customer": "Дизайнер",
            "goals": ["showroom"],
        }, seed=12)
        self.assertEqual(cfg["goals"], ["showroom"])
        self.assertTrue(all(call["goal"] == "showroom" for call in calls))
        self.assertTrue(all("салон" in call["card"]["success_condition"].lower() or "шоурум" in call["card"]["success_condition"].lower() for call in calls))
        for call in calls:
            validate_card(call["card"])

    def test_deal_is_distinct_from_any_next_step(self):
        cfg, calls = call_series.build_manifest({
            "count": 10,
            "product": "Услуга",
            "customer": "Собственник",
            "goals": ["deal"],
        }, seed=3)
        self.assertEqual(cfg["goals"], ["deal"])
        self.assertTrue(all(call["goal"] == "deal" for call in calls))
        self.assertTrue(all("оформлен" in call["card"]["success_condition"].lower() for call in calls))
        for call in calls:
            validate_card(call["card"])


if __name__ == "__main__":
    unittest.main()

import pathlib
import unittest

from academy.centaro_role_reports_runtime import _management_conclusion


class ReportWordingTests(unittest.TestCase):
    def test_high_plan_with_weak_skills_is_flagged(self):
        series = {"business_projection": {"plan_percent": 81}}
        text = _management_conclusion(series, {"skill_score": 49, "training_conversion": 0.3})
        self.assertIn("81% плана", text)
        self.assertIn("навыки — 49/100", text)

    def test_strong_skills_have_no_warning(self):
        series = {"business_projection": {"plan_percent": 81}}
        self.assertNotIn("Внимание", _management_conclusion(series, {"skill_score": 72, "training_conversion": 0.3}))

    def test_low_plan_has_no_extra_warning(self):
        series = {"business_projection": {"plan_percent": 40}}
        self.assertNotIn("Внимание", _management_conclusion(series, {"skill_score": 40, "training_conversion": 0.1}))

    def test_user_facing_texts_have_no_english_conversion_labels(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "academy"
        for path in root.glob("centaro_*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("#", "-")) or '"' not in stripped:
                    continue
                for label in ("Training Conversion", "Business Conversion", "CRM Conversion", "CALIBRATED FORECAST"):
                    self.assertNotIn(label, stripped, f"{path.name}: {stripped}")


if __name__ == "__main__":
    unittest.main()

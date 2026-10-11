import unittest

from academy.centaro_crm_calibration_runtime import build_crm_fact
from academy.centaro_calibrated_forecast_runtime import build_calibrated_projection


class CentaroCrmCalibrationTests(unittest.TestCase):
    def test_builds_training_vs_crm_conversion_and_coefficient(self):
        fact = build_crm_fact(0.40, 120, 30, 1_800_000)
        self.assertEqual(fact["real_contacts"], 120)
        self.assertEqual(fact["real_outcomes"], 30)
        self.assertAlmostEqual(fact["crm_conversion"], 0.25)
        self.assertAlmostEqual(fact["training_conversion"], 0.40)
        self.assertAlmostEqual(fact["calibration_coefficient"], 0.625)
        self.assertEqual(fact["confidence"], "устойчивая CRM-калибровка")
        self.assertEqual(fact["actual_revenue"], 1_800_000)

    def test_marks_small_real_sample_preliminary(self):
        fact = build_crm_fact(0.30, 20, 5)
        self.assertEqual(fact["confidence"], "предварительная CRM-калибровка")

    def test_rejects_invalid_real_sample(self):
        with self.assertRaises(ValueError):
            build_crm_fact(0.30, 9, 3)
        with self.assertRaises(ValueError):
            build_crm_fact(0.30, 20, 21)

    def test_zero_training_conversion_does_not_fake_coefficient(self):
        fact = build_crm_fact(0.0, 100, 10)
        self.assertIsNone(fact["calibration_coefficient"])
        self.assertAlmostEqual(fact["crm_conversion"], 0.10)

    def test_prior_crm_coefficient_calibrates_future_forecast(self):
        projection = {
            "training_conversion": 0.40,
            "monthly_contacts": 100,
            "downstream_percent": 25,
            "average_check": 150_000,
            "monthly_plan": 2_000_000,
        }
        calibrated = build_calibrated_projection(projection, 0.625)
        self.assertAlmostEqual(calibrated["calibrated_conversion"], 0.25)
        self.assertAlmostEqual(calibrated["expected_sales"], 6.25)
        self.assertAlmostEqual(calibrated["projected_revenue"], 937_500)
        self.assertAlmostEqual(calibrated["plan_percent"], 46.875)

    def test_calibrated_conversion_is_capped_at_one(self):
        projection = {
            "training_conversion": 0.80,
            "monthly_contacts": 10,
            "downstream_percent": 100,
            "average_check": 100,
            "monthly_plan": 1000,
        }
        calibrated = build_calibrated_projection(projection, 2.0)
        self.assertEqual(calibrated["calibrated_conversion"], 1.0)


if __name__ == "__main__":
    unittest.main()

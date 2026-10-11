import unittest

from academy.centaro_crm_calibration_runtime import build_crm_fact


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


if __name__ == "__main__":
    unittest.main()

import unittest

from academy import centaro_rop_runtime as rop


class CentaroRopTests(unittest.TestCase):
    def test_flow_parser_and_labels(self):
        self.assertEqual(rop._parse_flow("❄️ Холодный контакт"), "cold")
        self.assertEqual(rop._parse_flow("Входящий лид"), "inbound")
        self.assertEqual(rop._parse_flow("Возврат клиента"), "reactivation")
        self.assertEqual(rop._sample_label(10), "экспресс-диагностика")
        self.assertEqual(rop._sample_label(20), "рабочая оценка")
        self.assertEqual(rop._sample_label(30), "расширенная аттестация")

    def test_target_conversion_validation(self):
        self.assertEqual(rop._parse_percent("25%"), 25.0)
        self.assertEqual(rop._parse_percent("12,5"), 12.5)
        with self.assertRaises(ValueError):
            rop._parse_percent("0")
        with self.assertRaises(ValueError):
            rop._parse_percent("101")

    def test_wilson_interval_contains_observed_rate(self):
        low, high = rop._wilson_interval(8, 20)
        self.assertLess(low, 0.4)
        self.assertGreater(high, 0.4)
        self.assertGreater(high - low, 0.2)

    def test_flow_is_applied_to_manifest(self):
        series = {
            "manifest": [
                {
                    "difficulty": "hard",
                    "card": {
                        "opening": "old",
                        "company_context": "Учебная компания",
                        "background": {"situation": "Новый проект"},
                    },
                }
            ]
        }
        rop._apply_flow_to_manifest(series, "inbound")
        card = series["manifest"][0]["card"]
        self.assertIn("оставлял заявку", card["opening"])
        self.assertIn("мало времени", card["opening"])
        self.assertIn("Входящий лид", card["company_context"])
        self.assertIn("Входящий лид", card["background"]["situation"])

    def test_critical_skill_flags_do_not_hide_red_zone(self):
        series = {"conversion_goal": {"code": "sale"}}
        metrics = {
            "skill_normalized": {
                "needs": 78,
                "objections": 42,
                "next_step": 61,
            }
        }
        flags = rop._critical_flags(series, metrics)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0][0], "Работа с возражениями")
        self.assertEqual(flags[0][1], 42)


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from pathlib import Path


class GoldenFixtureContractTests(unittest.TestCase):
    def test_golden_set_has_20_well_formed_cases(self):
        path = Path(__file__).with_name('golden_cases.json')
        cases = json.loads(path.read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(cases), 20)
        ids = [case['id'] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        levels = {case['level'] for case in cases}
        self.assertEqual(levels, {'weak', 'medium', 'strong'})
        for case in cases:
            self.assertIn('fields', case)
            self.assertIn('dialogue', case)
            self.assertGreaterEqual(len(case['dialogue']), 5)
            for role, text in case['dialogue']:
                self.assertIn(role, ('user', 'assistant'))
                self.assertTrue(text.strip())
            expected = case['expected']
            self.assertLessEqual(expected['score_min'], expected['score_max'])
            self.assertGreaterEqual(expected['score_min'], 0)
            self.assertLessEqual(expected['score_max'], 100)
            self.assertTrue(expected['goal'])
            self.assertTrue(expected['next_step_status'])


if __name__ == '__main__':
    unittest.main()

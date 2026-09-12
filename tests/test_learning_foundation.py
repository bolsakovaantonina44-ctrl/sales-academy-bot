import tempfile
import unittest
from pathlib import Path

from academy.learning import progress_snapshot, record_assessment, set_progress


class LearningFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / 'academy.sqlite3'

    def tearDown(self):
        self.tempdir.cleanup()

    def test_snapshot_starts_with_three_modules(self):
        snapshot = progress_snapshot(self.db_path, 101)
        self.assertEqual([x['id'] for x in snapshot], ['product', 'sales', 'regulations'])
        self.assertTrue(all(x['status'] == 'not_started' for x in snapshot))
        self.assertTrue(all(x['latest_assessment'] is None for x in snapshot))

    def test_progress_is_independent_from_sales_sessions(self):
        set_progress(self.db_path, 101, 'product', 'in_progress')
        snapshot = {x['id']: x for x in progress_snapshot(self.db_path, 101)}
        self.assertEqual(snapshot['product']['status'], 'in_progress')
        self.assertEqual(snapshot['sales']['status'], 'not_started')

    def test_passing_attestation_completes_module(self):
        result = record_assessment(
            self.db_path, 101, 'product',
            answers={'q1': 'A', 'q2': 'C', 'q3': 'B', 'q4': 'D', 'q5': 'A'},
            correct=4, total=5, pass_percent=80,
        )
        self.assertEqual(result['score'], 80)
        self.assertTrue(result['passed'])
        snapshot = {x['id']: x for x in progress_snapshot(self.db_path, 101)}
        self.assertEqual(snapshot['product']['status'], 'completed')
        self.assertEqual(snapshot['product']['latest_assessment']['score'], 80)

    def test_failed_attestation_is_saved_but_does_not_complete_module(self):
        set_progress(self.db_path, 101, 'regulations', 'in_progress')
        result = record_assessment(
            self.db_path, 101, 'regulations',
            answers={'q1': 'B', 'q2': 'A'}, correct=1, total=2, pass_percent=80,
        )
        self.assertEqual(result['score'], 50)
        self.assertFalse(result['passed'])
        snapshot = {x['id']: x for x in progress_snapshot(self.db_path, 101)}
        self.assertEqual(snapshot['regulations']['status'], 'in_progress')
        self.assertFalse(snapshot['regulations']['latest_assessment']['passed'])

    def test_unknown_module_is_rejected(self):
        with self.assertRaises(ValueError):
            set_progress(self.db_path, 101, 'unknown', 'in_progress')


if __name__ == '__main__':
    unittest.main()

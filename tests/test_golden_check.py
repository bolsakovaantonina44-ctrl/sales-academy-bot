import json
import tempfile
import time
import unittest
from pathlib import Path

from academy import golden_check
from academy.access import AKENSO, set_role
from academy.domain import EvaluationError
from academy.engine import Engine, deliver
from academy.store import Store
from test_call_series import SeriesAI


class JudgeStub:
    """Answers like an outcome judge: achieved when the last client line confirms."""
    def __init__(self, wrong=()):
        self.wrong = set(wrong)

    def series_outcome(self, session, goal):
        last = session['history'][-1]['content'].lower()
        achieved = any(w in last for w in ('жду', 'договорились', 'подходит'))
        if session['history'][0]['content'] in self.wrong:
            achieved = not achieved
        if 'не мне решать' in last:
            raise EvaluationError('review rejected')
        return {'goal_achieved': achieved, 'reason': 'stub', 'outcome': goal if achieved else 'no_agreement'}


class GoldenCheckTests(unittest.TestCase):
    def test_cases_are_valid_and_cover_both_verdicts(self):
        cases = golden_check.load_cases()
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len({c['id'] for c in cases}), len(cases))
        self.assertTrue(any(c['expected'] for c in cases) and any(not c['expected'] for c in cases))
        from academy.call_series import GOALS
        self.assertTrue(all(c['goal'] in GOALS for c in cases))
        self.assertIn('demo_yes_with_caveat', {c['id'] for c in cases})

    def test_report_counts_matches_and_lists_mismatches(self):
        cases = golden_check.load_cases()
        results = golden_check.run_all(JudgeStub(), cases)
        by_id = {r['id']: r for r in results}
        self.assertIs(by_id['demo_send_info_only']['got'], False)  # review refusal = not achieved
        text = golden_check.format_report(results, 12)
        self.assertIn('Совпало с эталоном:', text)
        wrong = [r for r in results if r['got'] != r['expected']]
        for r in wrong:
            self.assertIn(r['id'], text)

    def test_ai_failure_is_reported_not_counted(self):
        class Broken:
            def series_outcome(self, session, goal):
                raise RuntimeError('network')
        results = golden_check.run_all(Broken(), golden_check.load_cases()[:2])
        text = golden_check.format_report(results)
        self.assertIn('Совпало с эталоном: 0 из 0', text)
        self.assertIn('Не удалось проверить', text)


class GoldenCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.ai = SeriesAI()
        self.engine = Engine(self.store, self.ai, admin_ids=[1])
        self.seq = 0
        set_role(self.path, 10, AKENSO)
        for user in (1, 10):
            self.send('/start', user); self.send(f'Тест {user}', user)

    def tearDown(self):
        self.temp.cleanup()

    def send(self, text, user):
        self.seq += 1
        self.store.enqueue(f'g-{self.seq}', user, user, 'text', text)
        self.engine.handle(self.store.claim())
        out = []
        deliver(self.store, user, lambda c, t: out.append(t))
        return '\n'.join(out)

    def test_admin_runs_check_in_background_employee_refused(self):
        self.assertIn('только администратору', self.send('/series_check', 10))
        self.ai.series_outcome = JudgeStub().series_outcome
        self.assertIn('Запустила проверку', self.send('/series_check', 1))
        for _ in range(50):
            out = []
            deliver(self.store, 1, lambda c, t: out.append(t))
            if out:
                break
            time.sleep(0.1)
        self.assertTrue(out and 'ПРОВЕРКА ОЦЕНКИ ЦЕЛЕЙ' in out[0], out)


if __name__ == '__main__':
    unittest.main()

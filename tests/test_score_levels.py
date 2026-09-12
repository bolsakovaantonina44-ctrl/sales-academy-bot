import unittest

from academy.domain import SKILLS, score_level, session_empty
from academy.report_text import render_report as render_telegram_report


class ScoreLevelTests(unittest.TestCase):
    def test_score_level_boundaries(self):
        cases = {
            0: 'требует системной отработки',
            39: 'требует системной отработки',
            40: 'рабочая база',
            59: 'рабочая база',
            60: 'уверенный уровень',
            74: 'уверенный уровень',
            75: 'сильный уровень',
            89: 'сильный уровень',
            90: 'очень сильный уровень',
            100: 'очень сильный уровень',
        }
        for score, expected in cases.items():
            with self.subTest(score=score):
                self.assertEqual(score_level(score), expected)

    def test_telegram_report_shows_human_readable_level(self):
        scores = dict(contact=4, questions=6, needs=8, listening=7,
                      control=4, arguments=4, objections=5, next_step=6)
        data = dict(
            simulation_valid=True,
            skills=[dict(id=key, score=scores[key], reason='Основание') for key, _, _ in SKILLS],
            goal='partial', outcome=1, next_step='Продолжить обсуждение', next_step_status='proposed',
            strengths=[], mistakes=[], recommendations=[], findings=[],
        )
        session = session_empty()
        session['id'] = 1
        report = render_telegram_report(data, session)
        self.assertIn('Навыки: 44/100', report)
        self.assertIn('Уровень: рабочая база', report)


if __name__ == '__main__':
    unittest.main()

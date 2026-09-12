import unittest

from academy.domain import SKILLS
from academy.engine import Engine


class FakeStore:
    def attempts(self, user_id):
        return 2

    def recent(self, user_id):
        report_data = {
            'simulation_valid': True,
            'skills': [
                {'id': key, 'score': maximum // 2, 'reason': '', 'evidence': []}
                for key, _, maximum in SKILLS
            ],
        }
        return [
            {
                'id': 8,
                'card': {'role': 'client'},
                'phase': 'active',
                'fields': {'customer': 'Директор компании'},
                'training_focus': 'no_need',
                'report_data': None,
            },
            {
                'id': 7,
                'card': {'role': 'client'},
                'phase': 'completed',
                'fields': {'customer': 'Собственник компании'},
                'training_focus': 'price',
                'report_data': report_data,
            },
        ]


class HistorySummaryTests(unittest.TestCase):
    def test_history_shows_usage_score_level_and_focus(self):
        engine = Engine(FakeStore(), ai=object(), limit=3)
        text = engine.history_summary(10)

        self.assertIn('Бесплатные тренировки: использовано 2 из 3, осталось 1.', text)
        self.assertIn('№7', text)
        self.assertIn('Дорого', text)
        self.assertIn('49/100', text)
        self.assertIn('рабочая база', text)
        self.assertIn('№8', text)
        self.assertIn('Нам не надо', text)
        self.assertIn('в процессе', text)
        self.assertNotIn('completed', text)
        self.assertIn('/report НОМЕР', text)

    def test_admin_does_not_get_misleading_free_limit(self):
        engine = Engine(FakeStore(), ai=object(), limit=3, admin_ids=[10])
        text = engine.history_summary(10)
        self.assertIn('для администратора не применяется', text)
        self.assertNotIn('осталось 1', text)


if __name__ == '__main__':
    unittest.main()

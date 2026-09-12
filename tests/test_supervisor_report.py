import unittest

from academy.domain import SKILLS, session_empty
from academy.reporting import manager_summary


class SupervisorReportTests(unittest.TestCase):
    def test_manager_summary_is_training_observation_not_staffing_verdict(self):
        scores = dict(contact=4, questions=6, needs=8, listening=7,
                      control=4, arguments=4, objections=5, next_step=6)
        data = dict(
            simulation_valid=True,
            skills=[dict(id=key, score=scores[key], reason='Основание', evidence=[]) for key, _, _ in SKILLS],
            goal='partial', outcome=1, next_step='Продолжить обсуждение', next_step_status='proposed',
            strengths=[], mistakes=[dict(text='Уточнить следующий шаг', evidence=[])],
            recommendations=[], findings=[], revealed=[], missed=[],
        )
        session = session_empty()
        session['id'] = 1
        session['employee'] = {'name': 'Тестовый Менеджер'}
        summary = manager_summary(data, session)
        self.assertIn('Уровень: рабочая база', summary)
        self.assertIn('Вывод по этой тренировке:', summary)
        self.assertIn('Динамика обучения:', summary)
        self.assertNotIn('Допуск', summary)
        self.assertNotIn('Обучаемость', summary)


if __name__ == '__main__':
    unittest.main()

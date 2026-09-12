import inspect
import unittest

from academy.domain import SKILLS, session_empty
from academy.pdf_report import render_pdf


class PDFReportConsistencyTests(unittest.TestCase):
    def _session(self):
        scores = dict(contact=4, questions=6, needs=8, listening=7,
                      control=4, arguments=4, objections=5, next_step=6)
        data = dict(
            simulation_valid=True,
            technical_partial=False,
            skills=[dict(id=key, score=scores[key], reason='Основание', evidence=[])
                    for key, _, _ in SKILLS],
            goal='partial', outcome=1,
            next_step='Продолжить обсуждение', next_step_status='proposed',
            strengths=[], mistakes=[], recommendations=[], findings=[],
            revealed=[], missed=[],
        )
        session = session_empty()
        session['id'] = 1
        session['employee'] = {'name': 'Тестовый Менеджер', 'id': 10}
        session['report_data'] = data
        return session

    def test_both_pdf_audiences_render(self):
        session = self._session()
        for audience in ('employee', 'supervisor'):
            with self.subTest(audience=audience):
                stream = render_pdf(session, audience)
                self.assertTrue(stream.getvalue().startswith(b'%PDF'))

    def test_pdf_copy_uses_training_language_and_score_level(self):
        source = inspect.getsource(render_pdf)
        self.assertIn("'Уровень: ' + score_level(score)", source)
        self.assertNotIn('Что делать с сотрудником', source)
        self.assertNotIn('Решение о найме', source)
        self.assertNotIn('Обучаемость оцениваем', source)
        self.assertIn('Динамику оцениваем после повторной попытки', source)


if __name__ == '__main__':
    unittest.main()

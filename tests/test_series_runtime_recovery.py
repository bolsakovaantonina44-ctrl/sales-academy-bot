import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from academy.ai import AI
from academy.domain import reduce_plan
from academy.pacing import apply_behavior
from academy.series_report import deliver_series_pdf, render_series_pdf, summary
import test_call_series as series_fixture
from test_core import plan


class SeriesRecoveryTests(unittest.TestCase):
    setUp = series_fixture.CallSeriesTests.setUp
    tearDown = series_fixture.CallSeriesTests.tearDown
    send = series_fixture.CallSeriesTests.send
    event = series_fixture.CallSeriesTests.event
    cfg = series_fixture.CallSeriesTests.cfg
    start = series_fixture.CallSeriesTests.start
    def test_progress_is_delivered_before_slow_evaluation(self):
        self.start()
        self.send('Какова ваша задача?')
        delivered = []
        from academy.engine import deliver
        self.engine.series_progress = lambda user: deliver(self.store, user, lambda c, t: delivered.append(t))
        original = self.ai.evaluate
        def evaluate(s):
            self.assertTrue(any('Сохраняю результат' in text for text in delivered))
            self.assertEqual(self.store.series_results(s['series_id'])[0]['status'], 'pending')
            return original(s)
        self.ai.evaluate = evaluate
        self.send('Завершить звонок')

    def test_double_finish_is_coalesced_during_processing(self):
        self.start()
        self.assertTrue(self.store.enqueue('finish-one', 10, 10, 'text', 'Завершить звонок'))
        self.store.claim()
        self.assertFalse(self.store.enqueue('finish-two', 10, 10, 'text', 'Завершить звонок'))

    def test_resume_recovers_natural_end_and_advances_same_fixed_series(self):
        series = self.start()
        self.ai.close = True
        original = self.ai.evaluate
        self.ai.evaluate = Mock(side_effect=KeyboardInterrupt())
        event = self.event('Спасибо, до свидания')
        with self.assertRaises(KeyboardInterrupt):
            self.engine.handle(event)
        self.ai.evaluate = original
        self.ai.close = False
        self.send('/series_resume')
        self.assertEqual(self.store.current(10)['series_index'], 2)
        self.assertEqual(self.store.get_series(series['id'])['manifest_hash'], series['manifest_hash'])
        self.assertEqual(self.store.series_results(series['id'])[0]['status'], 'completed')

    def test_pdf_partial_report_download_and_cross_user_denial(self):
        series = self.start()
        self.send('Какова ваша задача?')
        self.send('/series_abort')
        series = self.store.get_series(series['id'])
        rows = self.store.series_results(series['id'])
        text = summary(series, rows)
        self.assertIn('частичный результат', text)
        self.assertIn('1/10', text)
        pdf = render_series_pdf(series, rows)
        import pdfplumber
        with pdfplumber.open(pdf) as doc:
            text = '\n'.join(page.extract_text() or '' for page in doc.pages)
        self.assertIn('Отчёт сотруднику', text)
        self.assertIn('Какова ваша задача?', text)
        self.assertIn('Учебная конверсия', text)
        bot = Mock()
        with self.assertRaises(PermissionError):
            deliver_series_pdf(bot, self.store, 11, f"__academy_series_pdf__:{series['id']}")
        marker = self.send('Скачать отчёт серии')
        self.assertIn('__academy_series_pdf__:', marker)

    def test_name_questions_do_not_depend_on_stale_planner_intent(self):
        self.start()
        s = self.store.current(10)
        ai = AI(None, 'fake', 'fake')
        ai.request = Mock()
        state = copy.deepcopy(s['state'])
        state['last_intent'] = 'other'
        for text in ('Как могу к вам обращаться?', 'Меня зовут Светлана, а вас?'):
            self.assertEqual(ai.reply(s, text, state), s['card']['identity']['name'] + '.')
        ai.request.assert_not_called()

    def test_greeting_does_not_end_call_but_later_refusal_is_possible(self):
        self.start()
        s = self.store.current(10)
        p = plan(intent='name', action='question', close='refusal', reveal_ids=[])
        value = apply_behavior(s, p, reduce_plan(s['state'], p, s['card']))
        self.assertEqual(value['close'], 'continue')
        s['state']['substantive_turns'] = 4
        p = plan(intent='objection', action='pressure', close='refusal', reveal_ids=[])
        value = apply_behavior(s, p, reduce_plan(s['state'], p, s['card']))
        self.assertEqual(value['close'], 'refusal')


class ProductionRuntimeProbe(unittest.TestCase):
    def test_full_launcher_real_telegram_handlers_10_20_30_and_restart(self):
        probe = Path(__file__).with_name('series_runtime_probe.py')
        result = subprocess.run([sys.executable, str(probe)], cwd=probe.parent.parent,
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
        self.assertIn('PASS 10/20/30', result.stdout)

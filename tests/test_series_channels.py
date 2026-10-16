import json
import unittest
from types import SimpleNamespace

from academy import call_series
from academy.ai import AI
from academy.call_series import build_manifest, channel_breakdown, config, intro, new_call
from academy.series_ux_runtime import _channel_summary, _parse_channel

BASE = dict(count=10, product='Керамогранит', customer='Директор')


def result(index, achieved, assessment='verified'):
    return {'status': 'completed', 'outcome_status': 'verified', 'assessment_status': assessment,
            'goal_achieved': achieved,
            'session': {'series_index': index, 'report_data': {'simulation_valid': True}}}


class SeriesChannelTests(unittest.TestCase):
    def test_default_is_calls_only_and_randomness_unchanged(self):
        cfg, calls = build_manifest(BASE, seed=7)
        self.assertEqual(cfg['chat_percent'], 0)
        self.assertTrue(all(c['channel'] == 'call' for c in calls))
        _, explicit = build_manifest(dict(BASE, chat_percent=0), seed=7)
        self.assertEqual(calls, explicit)

    def test_exact_chat_count_is_fixed_before_start(self):
        for count, percent, expected in ((10, 30, 3), (20, 30, 6), (30, 100, 30), (10, 25, 2)):
            _, calls = build_manifest(dict(BASE, count=count, chat_percent=percent), seed=1)
            self.assertEqual(sum(c['channel'] == 'chat' for c in calls), expected)
        _, a = build_manifest(dict(BASE, chat_percent=50), seed=3)
        _, b = build_manifest(dict(BASE, chat_percent=50), seed=3)
        self.assertEqual(call_series.manifest_hash(a), call_series.manifest_hash(b))

    def test_chat_percent_validated(self):
        for bad in (-1, 101, '30', 3.5):
            with self.assertRaises(ValueError):
                config(dict(BASE, chat_percent=bad))

    def test_chat_call_opening_session_and_intro(self):
        _, calls = build_manifest(dict(BASE, chat_percent=100), seed=2)
        self.assertNotIn('Слушаю', calls[0]['card']['opening'])
        series = {'id': 5, 'index': 0, 'config': dict(BASE, chat_percent=100), 'manifest': calls}
        s = new_call(series, {'id': 1, 'name': 'Иван'})
        self.assertEqual(s['series_channel'], 'chat')
        text = intro(series, s)[0]
        self.assertIn('переписка 1/10', text)
        self.assertIn('мессенджере', text)

    def test_breakdown_only_for_mixed_series(self):
        _, calls = build_manifest(dict(BASE, chat_percent=30), seed=4)
        series = {'manifest': calls}
        chat_idx = [c['ordinal'] for c in calls if c['channel'] == 'chat']
        call_idx = [c['ordinal'] for c in calls if c['channel'] == 'call']
        rows = [result(i, i in chat_idx[:2]) for i in chat_idx] + [result(i, i == call_idx[0]) for i in call_idx]
        rows.append(dict(result(call_idx[1], True), assessment_status='unavailable'))  # not in denominator
        line = channel_breakdown(series, rows)[0]
        self.assertIn('Звонки: 1/7 (14%)', line)
        self.assertIn('Переписка: 2/3 (67%)', line)
        _, only_calls = build_manifest(BASE, seed=4)
        self.assertEqual(channel_breakdown({'manifest': only_calls}, rows), [])

    def test_wizard_helpers(self):
        self.assertEqual(_parse_channel('📞 звонки'), 'calls')
        self.assertEqual(_parse_channel('💬 переписка'), 'chats')
        self.assertEqual(_parse_channel('🔀 звонки и переписка'), 'mixed')
        self.assertIsNone(_parse_channel('что-то'))
        self.assertEqual(_channel_summary({'count': 10, 'chat_percent': 30}), 'Формат: звонков 7, переписок 3')
        self.assertEqual(_channel_summary({'count': 10, 'chat_percent': 100}), 'Формат: только переписка')
        self.assertEqual(_channel_summary({'count': 10}), 'Формат: только звонки')

    def test_ai_receives_channel_for_reply_and_evaluation(self):
        sent = []

        class Responses:
            def create(self, **kwargs):
                sent.append(json.loads(kwargs['input']))
                raise RuntimeError('stop after capture')

        ai = AI(SimpleNamespace(responses=Responses()), 'm', 't')
        _, calls = build_manifest(dict(BASE, chat_percent=100), seed=2)
        s = new_call({'id': 1, 'index': 0, 'config': dict(BASE, chat_percent=100), 'manifest': calls}, {'id': 1, 'name': 'И'})
        with self.assertRaises(RuntimeError):
            ai.evaluate(s)
        self.assertEqual(sent[-1]['channel'], 'chat')


if __name__ == '__main__':
    unittest.main()


class FreeDemoCriterionTests(unittest.TestCase):
    def test_outcome_judge_gets_demo_acceptance_rule(self):
        sent = []

        class Responses:
            def create(self, **kwargs):
                sent.append((kwargs['instructions'], json.loads(kwargs['input'])))
                raise RuntimeError('stop after capture')

        ai = AI(SimpleNamespace(responses=Responses()), 'm', 't')
        session = {'history': [
            {'role': 'user', 'content': 'Предлагаем подарить вам демо тренировку'},
            {'role': 'assistant', 'content': 'Демо посмотреть можно. Пришлите, что входит в демо.'},
            {'role': 'user', 'content': 'Пришлю ссылку. Перезвоню завтра в 2?'},
            {'role': 'assistant', 'content': 'Хорошо, ориентируемся на два часа. Ссылку и описание демо жду сюда.'}]}
        with self.assertRaises(RuntimeError):
            ai.series_outcome(session, 'free_demo')
        instructions, payload = sent[-1]
        self.assertIn('посмотреть или попробовать бесплатное демо', payload['criterion'])
        self.assertIn('получение ссылки на демо', payload['criterion'])
        self.assertIn('только просит прислать информацию, КП или прайс', payload['criterion'])
        self.assertIn('Для цели free_demo', instructions)
        self.assertIn('Сомнение клиента не равно отказу', instructions)

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from academy.access import AKENSO, SUPERVISOR, PUBLIC, set_role
from academy.ai import AI
from academy.call_series import build_manifest, manifest_hash, config, OBJECTIONS, report
from academy.domain import validate_card, check_evaluation, reduce_plan, EvaluationError
from academy.engine import Engine, deliver
from academy.store import Store
from bot import keyboard_rows
from test_core import FakeAI, evaluation, plan


class SeriesAI(FakeAI):
    def turn(self, s, text):
        self.calls += 1
        if self.fail:
            raise ValueError('API unavailable')
        p = plan(reveal_ids=['task'], close='refusal' if self.close else 'continue')
        return 'Расскажите, что предлагаете для нашей задачи.', reduce_plan(s['state'], p, s['card']), p

    def evaluate(self, s):
        if self.fail:
            raise ValueError('API unavailable')
        value = evaluation(s)
        value.update(revealed=['task'], missed=['cause'])
        return check_evaluation(value, s)

    def series_outcome(self, s, goal):
        return dict(outcome='no_agreement', goal_achieved=False, evidence=[], reason='Нет договорённости')


class CallSeriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.ai = SeriesAI()
        self.engine = Engine(self.store, self.ai, admin_ids=[1])
        self.seq = 0
        set_role(self.path, 10, AKENSO)
        set_role(self.path, 11, AKENSO)
        set_role(self.path, 12, SUPERVISOR)
        for user in (10, 11, 12):
            self.send('/start', user)
            self.send(f'Тестовый Менеджер {user}', user)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, text, user=10):
        self.seq += 1
        self.store.enqueue(f'test-{self.seq}', user, user, 'text', text)
        return self.store.claim()

    def send(self, text, user=10, received_at=None):
        event = self.event(text, user)
        if received_at is not None:
            event['received_at'] = received_at
        self.engine.handle(event)
        replies = []
        deliver(self.store, user, lambda c, t: replies.append(t))
        return '\n'.join(replies)

    def cfg(self, count=10):
        return dict(count=count, product='Бухгалтерское сопровождение', customer='Собственник компании')

    def start(self, count=10, user=10):
        self.send('/series_config ' + json.dumps(self.cfg(count), ensure_ascii=False), user)
        self.send('/series_begin', user)
        return self.store.open_series(user)

    def test_10_20_30_complete_save_reopen_and_restore_every_call(self):
        for count in (10, 20, 30):
            with self.subTest(count=count):
                series = self.start(count)
                original = series['manifest_hash']
                for i in range(count):
                    self.send('Для какой задачи рассматриваете решение?')
                    text = self.send('Завершить звонок')
                    rows = Store(self.path).series_results(series['id'])
                    self.assertEqual(len(rows), i+1)
                    self.assertEqual(rows[-1]['status'], 'completed')
                    self.assertEqual(rows[-1]['assessment_status'], 'verified')
                    self.assertIn('Для какой задачи', rows[-1]['session']['history'][1]['content'])
                    self.assertEqual(self.store.get_series(series['id'])['manifest_hash'], original)
                    if i < count - 1:
                        self.assertNotIn('ИТОГ СЕРИИ', text)
                        self.assertNotIn('contact:', text)
                        self.send('/series_next')
                text = self.send(f"/series_report {series['id']}")
                self.assertIn(f'Сохранено звонков: {count}/{count}', text)
                self.assertIn('Для какой задачи', text)
                self.assertIn('Начало:', text)
                self.assertIn('Середина:', text)
                self.assertIn('Конец:', text)
                self.assertIn('не является прогнозом', text)
                self.assertIn('не доказательство выносливости', text)
                self.assertEqual(self.store.get_series(series['id'])['status'], 'completed')
                self.assertEqual(len({r['session']['id'] for r in rows}), count)
                self.assertEqual(Store(self.path).series_results(series['id']), rows)

    def test_fixed_manifest_weights_custom_interested_and_progressive_difficulty(self):
        raw = self.cfg(30)
        raw.update(objections={'expensive': 2, 'custom_0': 1}, interested_percent=20,
                   custom=[dict(label='Проверка безопасности', objection='Нужен аудит.',
                                causes=['Требуется согласование службы безопасности.', 'Нужны документы по доступу.'], overcome=False)])
        cfg, calls = build_manifest(raw, seed=4)
        self.assertEqual(sum(c['objection'] == 'interested' for c in calls), 6)
        self.assertEqual(sum(c['objection'] == 'expensive' for c in calls), 16)
        self.assertEqual(sum(c['objection'] == 'custom_0' for c in calls), 8)
        self.assertEqual([c['difficulty'] for c in calls], ['easy']*10 + ['medium']*10 + ['hard']*10)
        self.assertEqual(len({c['card']['identity']['company'] for c in calls}), 30)
        for call in calls:
            validate_card(call['card'])
            if call['objection'] == 'interested':
                self.assertEqual(call['card']['barriers'], [])
            if call['objection'] == 'custom_0':
                self.assertFalse(call['overcome'])
        self.assertEqual(manifest_hash(calls), manifest_hash(build_manifest(raw, seed=4)[1]))
        self.assertEqual(len(OBJECTIONS), 14)
        self.assertTrue(all(len(causes) >= 2 for _, causes in OBJECTIONS.values()))

    def test_supervisor_assigns_fixed_conditions_and_report_permissions(self):
        self.send('/series_assign 10 ' + json.dumps(self.cfg(20)), 12)
        before = self.store.open_series(10)
        self.send('/series_config ' + json.dumps(self.cfg(10)), 10)
        self.assertEqual(self.store.open_series(10), before)
        self.send('/series_begin', 10)
        self.send('/series_abort', 10)
        self.assertIn('ИТОГ СЕРИИ', self.send(f"/series_report {before['id']}", 12))
        self.assertIn('недоступна', self.send(f"/series_report {before['id']}", 11))
        self.assertIn('Назначение доступно руководителю', self.send('/series_assign 11 ' + json.dumps(self.cfg()), 10))

    def test_no_new_scenario_hints_report_pause_or_retake(self):
        series = self.start()
        before = self.store.current(10)
        for cmd in ('/start', '/new', '/scenario', '/report', '/pdf', '/recheck', '/pause', '/series 30'):
            self.send(cmd)
            after = self.store.current(10)
            self.assertEqual(after['id'], before['id'])
            self.assertEqual(after['card'], before['card'])
            self.assertEqual(after['history'], before['history'])
            self.assertEqual(self.store.open_series(10)['manifest_hash'], series['manifest_hash'])
        self.assertEqual(self.ai.calls, 0)
        self.assertEqual(keyboard_rows(self.store.current(10)), [['Завершить звонок'], ['/series_abort']])

    def test_technical_retry_and_restart_are_separate_from_employee_gaps(self):
        series = self.start()
        before = self.store.current(10)
        self.ai.fail = True
        event = self.event('Уточняющий вопрос')
        with self.assertRaises(ValueError):
            self.engine.handle(event)
        self.store.fail(event, 'APIUnavailable')
        self.ai.fail = False
        self.store.recover()
        self.send('/retry')
        s = self.store.current(10)
        self.assertEqual(s['card'], before['card'])
        self.assertEqual(sum(m['role'] == 'user' for m in s['history']), 1)
        self.assertIsNone(s['response_times'][0]['seconds'])
        delivered = s['last_delivered_at']
        self.send('Следующий вопрос', received_at=delivered+400)
        series = self.store.open_series(10)
        kinds = [i['kind'] for i in series['interruptions']]
        self.assertIn('technical_APIUnavailable', kinds)
        self.assertIn('technical_restart', kinds)
        self.assertIn('unexplained_gap', kinds)
        self.assertEqual(self.store.current(10)['response_times'][-1]['seconds'], 400)

    def test_crash_during_evaluation_keeps_call_and_requeues_exact_finish(self):
        series = self.start()
        self.send('Какова текущая задача?')
        saved_evaluate = self.ai.evaluate
        self.ai.evaluate = Mock(side_effect=KeyboardInterrupt())
        event = self.event('Завершить звонок')
        with self.assertRaises(KeyboardInterrupt):
            self.engine.handle(event)
        pending = self.store.series_results(series['id'])[0]
        self.assertEqual(pending['status'], 'pending')
        self.assertIn('Какова текущая задача?', pending['session']['history'][1]['content'])
        self.store = Store(self.path)
        self.store.recover()
        self.engine = Engine(self.store, self.ai, admin_ids=[1])
        self.ai.evaluate = saved_evaluate
        restored = self.store.claim()
        self.assertEqual(restored['id'], event['id'])
        self.engine.handle(restored)
        self.assertEqual(len(self.store.series_results(series['id'])), 1)
        self.assertEqual(self.store.series_results(series['id'])[0]['status'], 'completed')

    def test_natural_end_crash_resumes_evaluation_without_replaying_call(self):
        series = self.start()
        self.ai.close = True
        saved_evaluate = self.ai.evaluate
        self.ai.evaluate = Mock(side_effect=KeyboardInterrupt())
        event = self.event('Спасибо, до свидания')
        with self.assertRaises(KeyboardInterrupt):
            self.engine.handle(event)
        self.ai.evaluate = saved_evaluate
        self.store.recover()
        self.send('/start')
        self.assertEqual(len(self.store.series_results(series['id'])), 1)
        self.assertEqual(self.store.current(10)['phase'], 'completed')
        self.assertEqual(self.ai.calls, 1)

    def test_abort_records_interruption_and_keeps_evidence(self):
        series = self.start(30)
        self.send('Какова задача?')
        text = self.send('/series_abort')
        self.assertIn('Сохранено звонков: 1/30', text)
        value = self.store.get_series(series['id'])
        self.assertEqual(value['status'], 'aborted')
        self.assertEqual(value['interruptions'][-1]['kind'], 'employee_abort')
        self.assertEqual(self.store.series_results(series['id'])[0]['status'], 'interrupted')

    def test_paid_series_consumes_full_package_once_and_restart_does_not_recharge(self):
        set_role(self.path, 10, PUBLIC)
        self.assertIn('нужно 10', self.send('/series_config ' + json.dumps(self.cfg())))
        self.store.grant_paid(10, 10, 30, 'series-payment', 1)
        series = self.start()
        self.assertEqual(self.store.paid_remaining(10), 0)
        self.store.recover()
        self.send('/start')
        self.assertEqual(self.store.paid_remaining(10), 0)
        self.assertEqual(self.store.open_series(10)['id'], series['id'])

    def test_invalid_settings_and_manifest_tampering_are_rejected(self):
        for change in (dict(count=11), dict(interested_percent=0), dict(objections={'unknown': 1}), dict(custom=[{}])):
            with self.assertRaises(ValueError):
                config({**self.cfg(), **change})
        series = self.start()
        series['manifest'][0]['difficulty'] = 'hard'
        with self.store.db() as db:
            db.execute('UPDATE call_series SET payload=? WHERE id=?', (json.dumps(series), series['id']))
        with self.assertRaises(ValueError):
            self.engine.handle(self.event('Добрый день'))

    def test_outcome_validation_copies_exact_quotes_and_rejects_wrong_speaker(self):
        self.start()
        self.send('Предлагаю встречу завтра для обсуждения задачи.')
        s = self.store.current(10)
        s['series_call_unreachable'] = False
        s['history'][-1]['content'] = 'Да, завтра в десять обсудим задачу.'
        ai = AI(Mock(), 'fake', 'fake')
        result = dict(outcome='meeting', goal_achieved=True, reason='Обе стороны согласовали встречу',
                      evidence=[dict(message_id=2, speaker='manager'), dict(message_id=3, speaker='client')])
        ai.request = Mock(side_effect=[copy.deepcopy(result), dict(passed=True, issues=[])])
        checked = ai.series_outcome(s, 'meeting')
        self.assertEqual(checked['evidence'][0]['quote'], s['history'][1]['content'])
        result['evidence'][0]['speaker'] = 'client'
        ai.request = Mock(return_value=result)
        with self.assertRaises(EvaluationError):
            ai.series_outcome(s, 'meeting')

    def test_revoked_attestation_access_preserves_call(self):
        self.send('/series_assign 10 ' + json.dumps(self.cfg()), 12)
        self.send('/series_begin')
        before = self.store.current(10)
        set_role(self.path, 10, PUBLIC)
        self.assertIn('отозван', self.send('Добрый день'))
        self.assertEqual(self.store.current(10)['history'], before['history'])

    def test_wizard_practice_and_assignment_persist_every_step(self):
        for user, command in [(10, '/series'), (12, '/series_assign')]:
            self.send(command, user)
            if user == 12:
                self.send('11', user)
            for answer in ('20', 'Бухгалтерское сопровождение', 'Собственник компании',
                           '1,2', '2:2,5:1', '20', 'Высокая'):
                self.send(answer, user)
                self.assertIsNotNone(Store(self.path).current(user).get('series_setup'))
            self.send('Аудит | Нужен аудит. | Нужны документы / Проверяет служба безопасности | нельзя | 1', user)
            self.send('Готово', user)
            prompt = self.send('Назначить серию' if user == 12 else 'Подготовить серию', user)
            target = 11 if user == 12 else 10
            series = self.store.open_series(target)
            self.assertEqual(series['config']['count'], 20)
            self.assertEqual(series['config']['objections']['expensive'], 2)
            self.assertEqual(series['config']['custom'][0]['overcome'], False)
            self.assertEqual(series['assigned_by'], 12 if user == 12 else None)
            self.assertIn('зафиксирована', prompt)
            self.assertNotIn('series_setup', self.store.current(user))

    def test_intercall_gap_is_recorded_and_no_pause_button_is_present(self):
        self.start()
        self.send('Какова задача?')
        self.send('Завершить звонок')
        s = self.store.current(10)
        self.assertEqual(keyboard_rows(s), [['Следующий звонок'], ['/series_abort']])
        self.send('/series_next', received_at=s['next_call_delivered_at']+400)
        series = self.store.open_series(10)
        self.assertEqual(series['between_call_times'][0]['seconds'], 400)
        self.assertEqual(series['interruptions'][-1]['kind'], 'unexplained_between_call_gap')

    def test_outcome_review_rejects_positive_result_even_with_valid_quotes(self):
        self.start()
        self.send('Я предлагаю встречу.')
        s = self.store.current(10)
        s['series_call_unreachable'] = False
        ai = AI(Mock(), 'fake', 'fake')
        ai.request = Mock(side_effect=[dict(outcome='meeting', goal_achieved=True, reason='Встреча',
            evidence=[dict(message_id=2, speaker='manager'), dict(message_id=3, speaker='client')]),
            dict(passed=False, issues=['Клиент не согласился на встречу.'])])
        with self.assertRaises(EvaluationError):
            ai.series_outcome(s, 'meeting')

    def test_technical_abort_is_excluded_from_conversion_and_employee_interruptions(self):
        series = self.start()
        self.send('Какова задача?')
        self.ai.fail = True
        event = self.event('Уточните детали')
        with self.assertRaises(ValueError):
            self.engine.handle(event)
        self.store.fail(event, 'APIUnavailable')
        self.ai.fail = False
        text = self.send('/series_abort')
        self.assertIn('Учебная конверсия: 0/0', text)
        row = self.store.series_results(series['id'])[0]
        self.assertEqual(row['reason'], 'technical_abort')
        self.assertEqual(row['assessment_status'], 'technical_partial')
        self.assertNotIn('employee_abort', [i['kind'] for i in self.store.get_series(series['id'])['interruptions']])

    def test_interested_client_has_no_artificial_barrier_and_unreachable_cannot_succeed(self):
        from academy.domain import initial_state
        from academy.pacing import apply_behavior
        self.start()
        base = self.store.current(10)
        base['card']['barriers'] = []
        base['state'] = initial_state()
        base['series_call_unreachable'] = False
        p = plan(reveal_ids=['task'], close='success', action='next_step',
                 next_step_requested=True, agreement='Встреча завтра')
        state = apply_behavior(base, p, reduce_plan(base['state'], p, base['card']))
        self.assertEqual(state['close'], 'success')
        self.assertEqual(state['required_objection'], '')
        base['series_call_unreachable'] = True
        state = apply_behavior(base, p, reduce_plan(base['state'], p, base['card']))
        self.assertEqual(state['close'], 'qualified_refusal')
        self.assertEqual(state['agreement'], '')

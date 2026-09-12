import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from academy.ai import AI
from academy.domain import SKILLS, initial_state, reduce_plan, session_empty, render_report, EvaluationError
from academy.pacing import prepare_card, apply_behavior
from academy.reporting import fallback_data, recommended_training_cases
from academy.scenarios import template
from bot import receive_text, keyboard_rows, deliver_pdf
import test_core as core


class LiveRegressions(unittest.TestCase):
    setUp = core.CoreTests.setUp
    tearDown = core.CoreTests.tearDown
    event = core.CoreTests.event
    send = core.CoreTests.send
    start = core.CoreTests.start
    talk = core.CoreTests.talk

    def test_duplicate_begin_never_reaches_ai_or_charges(self):
        before = self.start()
        self.ai.turn = Mock(side_effect=AssertionError('button reached model'))
        for text in ('Начать тренировку', '/begin', 'НАЧАТЬ ТРЕНИРОВКУ!'):
            after = self.send(text)
            self.assertEqual(after['history'], before['history'])
        self.assertEqual(self.store.attempts(10), 0)

    def test_receipt_while_worker_is_blocked_and_two_queued_starts(self):
        self.send('Продаю плитку'); self.send('Закупщику'); self.send('Расчёт')
        self.send('Дорого')
        entered, release = threading.Event(), threading.Event()
        def slow_card(fields):
            entered.set(); release.wait(3); return template('1')['card']
        self.ai.card = slow_card
        received = []
        receive_text(self.store, 'begin1', 10, 10, 'text', 'Начать тренировку', lambda c,t: received.append(t))
        first = self.store.claim()
        thread = threading.Thread(target=self.engine.handle, args=(first,)); thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(received[0], 'Запускаю тренировку…')
            receive_text(self.store, 'begin2', 10, 10, 'text', 'Начать тренировку', lambda c,t: received.append(t))
        finally:
            release.set(); thread.join(3)
        self.engine.handle(self.store.claim())
        self.assertEqual(len(self.store.current(10)['history']), 0)
        self.assertEqual(self.ai.calls, 0)
        self.assertEqual(self.store.attempts(10), 0)

    def test_repeated_finish_taps_are_coalesced_while_report_is_working(self):
        self.talk()
        received = []
        self.assertTrue(receive_text(self.store, 'finish1', 10, 10, 'text', 'Завершить тренировку',
                                     lambda c, t: received.append(t)))
        event = self.store.claim()
        self.assertFalse(receive_text(self.store, 'finish2', 10, 10, 'text', 'Завершить тренировку',
                                      lambda c, t: received.append(t)))
        self.assertFalse(receive_text(self.store, 'finish3', 10, 10, 'text', '/finish',
                                      lambda c, t: received.append(t)))
        self.assertEqual(received[0], 'Завершаю тренировку. Готовлю разбор — это может занять около 1 минуты…')
        self.assertEqual(received[1:], [
            'Разбор уже формируется. Повторно нажимать «Завершить тренировку» не нужно.',
            'Разбор уже формируется. Повторно нажимать «Завершить тренировку» не нужно.',
        ])
        self.engine.handle(event)
        bodies = [item['body'] for item in self.store.outgoing(10)]
        self.assertEqual(sum(body.startswith('РЕЗУЛЬТАТ ТРЕНИРОВКИ') for body in bodies), 1)
        self.assertEqual(sum(body.startswith('__academy_pdf__:') for body in bodies), 1)

    def test_levels_have_required_barriers_and_natural_end(self):
        for level, count in [('easy',1),('medium',2),('hard',3)]:
            s = session_empty(); s['fields']['difficulty'] = level
            s['card'] = prepare_card(template('1')['card'], level)
            self.assertEqual(len(s['card']['barriers']), count)
            spoken=[]
            for _ in range(16):
                p=core.plan(intent='need')
                s['state']=apply_behavior(s,p,reduce_plan(s['state'],p,s['card']))
                if s['state']['required_objection']: spoken.append(s['state']['required_objection'])
            self.assertEqual(len(spoken),count)
            self.assertEqual(s['state']['close'],'refusal')
            self.assertTrue(s['state']['wrap_up'])
            self.assertEqual(s['state']['agreement'],'')

    def test_focused_training_cannot_refuse_before_objection_is_spoken(self):
        s=self.start()
        p=core.plan(close='refusal',intent='need')
        state=apply_behavior(s,p,reduce_plan(s['state'],p,s['card']))
        self.assertEqual(state['close'],'continue')
        self.assertTrue(state['required_objection'])

    def test_resolved_or_deferred_objection_is_not_repeated_without_new_cause(self):
        s=self.start()
        barrier=s['card']['barriers'][0]['id']
        s['state']['issues']={b['id']:'unknown' for b in s['card']['barriers']}
        s['state']['issues'][barrier]='resolved'
        s['state']['resolved']=[barrier]
        s['state']['presented_barriers']=[barrier]
        s['state']['issue_mentions']={barrier:2}
        p=core.plan(intent='objection',focus_issue_id=barrier,
                    issue_updates=[dict(id=barrier,status='resolved',reopen_reason='none')])
        state=apply_behavior(s,p,reduce_plan(s['state'],p,s['card']))
        self.assertTrue(state['suppress_issue_repeat'])
        self.assertNotEqual(state.get('required_objection_id'),barrier)

    def test_success_cannot_be_manufactured_before_all_barriers_are_seen_and_resolved(self):
        s=self.start()
        p=core.plan(intent='next_step',action='next_step',close='success',next_step_requested=True,
                    agreement='Встреча завтра в 10:00')
        state=apply_behavior(s,p,reduce_plan(s['state'],p,s['card']))
        self.assertEqual(state['close'],'continue')
        self.assertEqual(state.get('agreement',''),'')

    def test_hidden_fact_is_revealed_at_most_one_per_turn_and_only_for_earning_actions(self):
        s=self.start(); ids=[f['id'] for f in s['card']['facts'][:2]]
        p=core.plan(intent='need',action='question',reveal_ids=ids)
        state=apply_behavior(s,p,reduce_plan(s['state'],p,s['card']))
        self.assertLessEqual(len(set(state['revealed'])-set(s['state'].get('revealed',[]))),1)
        s2=self.start(); p2=core.plan(intent='need',action='monologue',reveal_ids=ids)
        state2=apply_behavior(s2,p2,reduce_plan(s2['state'],p2,s2['card']))
        self.assertEqual(state2['revealed'],[])

    def test_objection_is_spoken_and_hidden_information_not_given_to_writer(self):
        s=self.talk(); state=copy.deepcopy(s['state'])
        state.update(required_objection='Я подумаю.',focus_issue_id='comparable')
        ai=AI(None,'fake','fake');ai.request=Mock(return_value=dict(reply='Для входной зоны.',used_fact_ids=['need']))
        answer=ai.reply(s,'Какую задачу решаете?',state)
        self.assertIn('Я подумаю.',answer)
        payload=ai.request.call_args.args[2]
        self.assertEqual(payload['active_barrier'],'У других дешевле.')
        self.assertNotIn('card',payload)

    def test_monologue_loses_engagement_and_cannot_open_facts(self):
        s=self.start(); p=core.plan(action='monologue',interest_delta=1,trust_delta=1)
        state=apply_behavior(s,p,reduce_plan(s['state'],p,s['card']))
        self.assertLess(state['trust'],s['state']['trust'])
        self.assertEqual(state['revealed'],[])

    def test_evaluator_never_receives_hidden_card_and_saves_rejections(self):
        s=self.talk(); d=core.evaluation(s)
        ai=AI(None,'fake','fake');ai.request=Mock(side_effect=[
            d,dict(passed=False,issues=['next_step: contradiction']),
            d,dict(passed=False,issues=['next_step: contradiction']),
            d,dict(passed=False,issues=['next_step: contradiction'])])
        with self.assertRaises(EvaluationError): ai.evaluate(s)
        self.assertEqual(len(s['evaluation_diagnostics']),3)
        self.assertNotIn('card',ai.request.call_args_list[0].args[2])
        self.assertEqual(ai.request.call_args_list[2].args[2]['review_feedback'],['next_step: contradiction'])

    def test_fallback_keeps_all_sections_and_null_scores(self):
        s=self.talk(); d=fallback_data(s); report=render_report(d,s)
        for key,title,maximum in SKILLS:
            self.assertIn(title,report)
        for title in ('РЕЗУЛЬТАТ ТРЕНИРОВКИ','ОЦЕНКА ПО НАВЫКАМ','ЧТО ОТРАБОТАТЬ'):
            self.assertIn(title,report)
        self.assertTrue(all(x['score'] is None for x in d['skills']))
        self.assertIsNone(d['outcome'])
        self.assertEqual(len(d['recommendations']),2)

    def test_pdf_preserves_cyrillic_and_excludes_hidden_card(self):
        import pdfplumber
        from academy.pdf_report import render_pdf
        s=self.talk();s['report_data']=core.evaluation(s)
        s['card']['hidden_motive']='SECRET-HIDDEN-MOTIVE'
        s['card']['facts'].append(dict(id='private',text='SECRET-HIDDEN-FACT',reveal_when='never'))
        for data in (s['report_data'],fallback_data(s)):
            s['report_data']=data
            for audience in ('employee','supervisor'):
                with pdfplumber.open(render_pdf(s,audience)) as doc:
                    text='\n'.join(p.extract_text() or '' for p in doc.pages)
                self.assertIn('Академия продаж',text)
                self.assertNotIn('SECRET-HIDDEN',text)
                for _,title,_ in SKILLS:
                    self.assertIn(title,text)

    def test_pdf_authorization_and_durable_outbox(self):
        self.talk(); self.send('/finish'); self.send('/pdf')
        self.assertTrue(any(x['body'].startswith('__academy_pdf__:') for x in self.store.outgoing(10)))
        self.assertIsNone(self.store.session_for_user(20,self.store.current(10)['id']))
        rows=keyboard_rows(self.store.current(10))
        self.assertFalse(any('Показать скрытый сценарий' in r or 'Повторить обработку' in r for r in rows))
        self.send('Отчёт руководителю')
        self.assertFalse(any(x['body'].endswith(':supervisor') for x in self.store.outgoing(10)))

    def test_finish_queues_pdf_automatically_and_report_prose_is_not_amputated(self):
        self.talk()
        long_reason = ('Менеджер уточнил задачу клиента и получил содержательный ответ. '
                       'Затем предложил продолжить обсуждение на демонстрации.')
        self.ai.evaluate = Mock(return_value=core.evaluation(self.store.current(10)))
        data = self.ai.evaluate.return_value
        data['skills'][0]['reason'] = long_reason
        self.send('/finish')
        outgoing = self.store.outgoing(10)
        self.assertTrue(any(x['body'].startswith('__academy_pdf__:') for x in outgoing))
        self.assertIn(long_reason, ''.join(x['body'] for x in outgoing))
        self.assertIn('КАК ИСПОЛЬЗОВАТЬ ТРЕНАЖЁР ДАЛЬШЕ', ''.join(x['body'] for x in outgoing))

    def test_report_prescribes_two_concrete_reuses_of_trainer(self):
        s=self.talk();data=core.evaluation(s)
        cases=recommended_training_cases(data,s)
        self.assertEqual(len(cases),2)
        self.assertTrue(all(case.endswith('.') for case in cases))

    def test_pdf_marker_sends_real_document_and_enforces_audience(self):
        done=self.talk();done=self.send('/finish')
        telegram=Mock()
        deliver_pdf(telegram,self.store,10,f"__academy_pdf__:{done['id']}:employee")
        args,kwargs=telegram.send_document.call_args
        self.assertEqual(args[0],10)
        self.assertTrue(kwargs['visible_file_name'].endswith('.pdf'))
        self.assertTrue(args[1].getvalue().startswith(b'%PDF-'))
        with self.assertRaises(PermissionError):
            deliver_pdf(telegram,self.store,10,f"__academy_pdf__:{done['id']}:supervisor")

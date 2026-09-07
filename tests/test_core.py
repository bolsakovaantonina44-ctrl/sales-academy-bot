import tempfile
import unittest
from pathlib import Path
from academy.domain import chunks, validate_card, reduce_plan, initial_state, check_evaluation, render_report, SKILLS
from academy.scenarios import template
from academy.store import Store
from academy.engine import Engine, deliver


def plan(**changes):
    p = dict(action='question', reveal_ids=['need'], resolved_ids=[], trust_delta=1,
             interest_delta=0, close='continue', next_step_requested=False, agreement='', reason='Уместный вопрос')
    p.update(changes)
    return p


def evaluation(s):
    mid = next(i+1 for i,m in enumerate(s['history']) if m['role']=='user')
    quote = s['history'][mid-1]['content']
    return dict(simulation_valid=True, simulation_issues=[],
        skills=[dict(id=k, score=m//2, reason='Основание по диалогу', evidence=[dict(message_id=mid, quote=quote)]) for k,_,m in SKILLS],
        goal='partial', outcome=1, next_step='', strengths=['Вопрос'], mistakes=['Не уточнил задачу'],
        recommendations=['Уточнить задачу'], revealed=['need'], missed=[])


class FakeAI:
    model=eval_model='fake'
    transcribe_model='fake-transcription'
    def __init__(self):
        self.calls=0; self.extractions=0; self.fail=False; self.close=False
    def extract(self,s,t):
        self.extractions+=1
        return dict(product='Плитка',customer='',goal='',difficulty='medium')
    def card(self,f):return template('1')['card']
    def turn(self,s,t):
        self.calls+=1
        if self.fail: raise ValueError('AI unavailable')
        p=plan(close='refusal' if self.close else 'continue')
        return 'Нужен материал для входной зоны.',reduce_plan(s['state'],p,s['card']),p
    def evaluate(self,s):
        if self.fail: raise ValueError('AI unavailable')
        return evaluation(s)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'db.sqlite3'
        self.store=Store(self.path); self.ai=FakeAI(); self.engine=Engine(self.store,self.ai); self.seq=0
    def tearDown(self):self.tmp.cleanup()
    def event(self,text,user=10,kind='text'):
        self.seq+=1; self.store.enqueue(str(self.seq),user,user,kind,text)
        return self.store.claim()
    def send(self,text,user=10):
        e=self.event(text,user); self.engine.handle(e)
        return self.store.current(user)
    def start(self,user=10):
        self.send('1',user); return self.send('Начать тренировку',user)
    def talk(self):
        self.start(); return self.send('Для какой задачи нужен материал?')
    def test_all_templates_valid(self):
        for k in ['1','2','3']:validate_card(template(k)['card'])
    def test_restore_after_restart(self):
        before=self.talk(); self.assertEqual(Store(self.path).current(10),before)
        self.assertEqual(Store(self.path).attempts(10),1)
    def test_duplicate_event_does_not_append(self):
        self.store.enqueue('same',10,10,'text','1')
        self.assertFalse(self.store.enqueue('same',10,10,'text','1'))
        self.engine.handle(self.store.claim()); self.assertIsNone(self.store.claim())
    def test_hidden_card_stable(self):
        old=self.start()['card']; self.send('Вопрос один'); s=self.send('Вопрос два')
        self.assertEqual(old,s['card']); self.assertEqual(self.store.attempts(10),1)
    def test_new_archives_and_start_resumes(self):
        old=self.talk(); self.assertEqual(self.send('/start')['history'],old['history'])
        self.send('/new'); archived=self.store.recent(10)[1]
        self.assertEqual(archived['phase'],'abandoned'); self.assertEqual(archived['history'],old['history'])
    def test_setup_asks_each_missing_field_once(self):
        self.send('Я продаю плитку'); self.send('Закупщику'); ready=self.send('Получить спецификацию')
        self.assertEqual(ready['phase'],'ready'); self.assertEqual(self.ai.extractions,1)
        self.assertEqual(ready['fields']['customer'],'Закупщику')
    def test_closed_client_cannot_reopen(self):
        self.start(); self.ai.close=True; s=self.send('Вопрос')
        self.assertEqual(s['phase'],'closed'); self.send('А всё-таки?'); self.assertEqual(self.ai.calls,1)
    def test_no_turn_no_charge(self):
        self.start(); self.send('/finish'); self.assertEqual(self.store.attempts(10),0)
    def test_usage_limit_survives_new_sessions(self):
        for _ in range(3):self.send('/new'); self.start(); self.send('Вопрос')
        self.send('/new'); self.start()
        self.assertEqual(self.store.current(10)['phase'],'ready'); self.assertEqual(self.store.attempts(10),3)
    def test_owner_exemption(self):
        self.engine=Engine(self.store,self.ai,limit=0,admin_ids=[10]); self.assertEqual(self.start()['phase'],'active')
    def test_failure_rolls_back_history_and_charge(self):
        before=self.start(); self.ai.fail=True; e=self.event('Вопрос')
        with self.assertRaises(ValueError):self.engine.handle(e)
        self.store.fail(e,'ValueError')
        self.assertEqual(self.store.current(10)['history'],before['history']); self.assertEqual(self.store.attempts(10),0)
        self.ai.fail=False; self.send('/retry')
        self.assertEqual(len(self.store.current(10)['history']),3); self.assertIsNone(self.store.failed(10))
    def test_repeated_failure_then_retry(self):
        self.start(); self.ai.fail=True
        for text in ('Вопрос','/retry','/retry'):
            e=self.event(text)
            try:self.engine.handle(e)
            except ValueError:self.store.fail(e,'ValueError')
        self.ai.fail=False; self.send('/retry')
        self.assertIsNone(self.store.failed(10)); self.assertEqual(len(self.store.current(10)['history']),3)
    def test_delivery_retry_does_not_call_model(self):
        self.talk(); called=self.ai.calls
        def bad(chat,text):raise OSError('timeout')
        with self.assertRaises(OSError):deliver(self.store,10,bad)
        self.assertTrue(self.store.outgoing(10)); sent=[]; deliver(self.store,10,lambda c,t:sent.append(t))
        self.assertTrue(sent); self.assertFalse(self.store.outgoing(10)); self.assertEqual(self.ai.calls,called)
    def test_report_and_card_survive_finish(self):
        self.talk(); done=self.send('/finish'); self.assertTrue(done['report']); self.assertIsNotNone(done['card'])
        sid=done['id']; self.send('/new'); self.send('/report '+str(sid))
        self.assertIn(done['report'], ''.join(x['body'] for x in self.store.outgoing(10)))
    def test_hidden_not_exposed_by_command_while_active(self):
        self.start(); deliver(self.store,10,lambda c,t:None); self.send('Показать скрытый сценарий')
        self.assertNotIn('удорожание',''.join(x['body'] for x in self.store.outgoing(10)))
    def test_users_are_isolated(self):
        self.talk(); self.start(20)
        self.assertEqual(len(self.store.current(20)['history']),1); self.assertEqual(self.store.attempts(20),0)
    def test_crash_requeues_uncommitted_event(self):
        e=self.event('1'); self.store.recover(); self.assertEqual(self.store.claim()['id'],e['id'])
    def test_split_utf16_long_report(self):
        text='Разбор😀\n'*2000; parts=chunks(text)
        self.assertEqual(''.join(parts),text); self.assertTrue(all(len(x.encode('utf-16-le'))//2<=3500 for x in parts))
    def test_no_invented_barrier_ids(self):
        with self.assertRaises(ValueError):reduce_plan(initial_state(),plan(resolved_ids=['invented']),template('1')['card'])
    def test_success_without_resolved_barriers_denied(self):
        s=reduce_plan(initial_state(),plan(close='success',next_step_requested=True,agreement='Встреча'),template('1')['card'])
        self.assertEqual(s['close'],'continue')
    def test_resolved_barriers_do_not_reappear(self):
        card=template('1')['card'];s=reduce_plan(initial_state(),plan(resolved_ids=['comparable']),card)
        s=reduce_plan(s,plan(),card);self.assertEqual(s['resolved'],['comparable'])
    def test_pressure_cannot_increase_trust_or_interest(self):
        state=initial_state()
        changed=reduce_plan(state,plan(action='pressure',trust_delta=1,interest_delta=1),template('1')['card'])
        self.assertEqual(changed['trust'],state['trust'])
        self.assertEqual(changed['interest'],state['interest'])
    def test_fake_quote_rejected(self):
        s=self.talk();d=evaluation(s);d['skills'][0]['evidence'][0]['quote']='Этого не было'
        with self.assertRaises(ValueError):check_evaluation(d,s)
    def test_invalid_or_duplicate_score_rejected(self):
        s=self.talk();d=evaluation(s);d['skills'][0]['score']=100
        with self.assertRaises(ValueError):check_evaluation(d,s)
        d=evaluation(s);d['skills'][0]['id']='questions'
        with self.assertRaises(ValueError):check_evaluation(d,s)
    def test_unobserved_not_zero_or_fake_100(self):
        s=self.talk();d=evaluation(s);d['skills'][0]['score']=None;check_evaluation(d,s)
        report=render_report(d,s)
        self.assertIn('недостаточно данных',report);self.assertIn('Общий балл из 100 не рассчитан',report)
    def test_outcome_does_not_change_skill_score(self):
        s=self.talk();d=evaluation(s);a=render_report(d,s).splitlines()[2];d['outcome']=3
        self.assertEqual(a,render_report(d,s).splitlines()[2])
    def test_invalid_simulation_suppresses_total(self):
        s=self.talk();d=evaluation(s);d['simulation_valid']=False
        self.assertIn('Итоговый балл не выставлен',render_report(d,s))

if __name__=='__main__':unittest.main()

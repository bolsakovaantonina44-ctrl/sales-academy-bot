import copy
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock
import test_core as core
from academy.ai import AI
from academy.domain import initial_state, reduce_plan, session_empty, validate_card
from academy.scenarios import menu, template
from academy.store import Store


class MVPTests(unittest.TestCase):
    setUp=core.CoreTests.setUp
    tearDown=core.CoreTests.tearDown
    event=core.CoreTests.event
    send=core.CoreTests.send
    start=core.CoreTests.start
    talk=core.CoreTests.talk

    def fail_turn(self):
        event=self.event('Необработанная реплика');self.ai.fail=True
        try:self.engine.handle(event)
        except ValueError:self.store.fail(event,'ValueError')
        return event

    def test_finish_bypasses_failed_turn_and_never_loses_report(self):
        before=self.talk(); event=self.fail_turn()
        done=self.send('/finish')
        self.assertEqual(done['phase'],'completed')
        self.assertEqual(done['history'],before['history'])
        self.assertEqual(done['technical_errors'],1)
        self.assertEqual(done['report_status'],'technical_partial')
        self.assertIn('Баллы не выставлены',done['report'])
        self.assertIsNone(self.store.failed(10))
        with self.store.db() as db:
            row=db.execute('SELECT text,raw_text,status FROM inbox WHERE id=?',(event['id'],)).fetchone()
            self.assertEqual(row['raw_text'],'Необработанная реплика')
            self.assertEqual(row['status'],'discarded')

    def test_second_retry_is_not_another_model_call(self):
        self.talk();self.fail_turn()
        retry=self.event('/retry')
        try:self.engine.handle(retry)
        except ValueError:self.store.fail(retry,'ValueError')
        failed=self.store.failed(10)
        self.assertEqual(failed['attempts'],2)
        called=self.ai.calls;self.send('/retry')
        self.assertEqual(self.ai.calls,called)
        self.send('/skip'); self.ai.fail=False
        self.send('Продолжим разговор')
        self.assertEqual(self.store.current(10)['technical_errors'],1)

    def test_state_and_limit_survive_new_process(self):
        before=self.talk()
        code='from academy.store import Store;import sys,json;s=Store(sys.argv[1]);print(json.dumps([s.current(10),s.attempts(10)]))'
        result=subprocess.check_output([sys.executable,'-c',code,str(self.path)],text=True)
        after,count=json.loads(result)
        self.assertEqual(after,before);self.assertEqual(count,1)

    def test_legacy_sessions_gain_stable_identity_without_reset(self):
        s=self.talk();s.pop('scenario_id');s.pop('knowledge');s['card'].pop('identity')
        self.store.commit(self.event('fixture'),s,[])
        a=Store(self.path).current(10);b=Store(self.path).current(10)
        self.assertEqual(a['scenario_id'],b['scenario_id'])
        self.assertEqual(a['card']['identity'],b['card']['identity'])
        self.assertEqual(a['history'],s['history'])

    def test_new_custom_session_has_no_product_default(self):
        s=session_empty();self.assertEqual(s['fields']['product'],'')
        self.assertEqual(s['knowledge']['product_knowledge'],{})
        self.assertIn('Керамогранит',menu())  # Explicit demo label, never a hidden choice.

    def test_generated_card_ids_are_owned_by_engine(self):
        card=copy.deepcopy(template('1')['card'])
        for field in ('facts','barriers'):
            for item in card[field]:item['id']='Факт с пробелами / duplicate'
        ai=AI(None,'fake','fake');ai.request=Mock(return_value=card)
        result=ai.card({})
        self.assertEqual([x['id'] for x in result['facts']],
                         [f'f{i}' for i in range(1,len(result['facts'])+1)])
        self.assertEqual([x['id'] for x in result['barriers']],
                         [f'b{i}' for i in range(1,len(result['barriers'])+1)])
        validate_card(result)

    def test_semantic_name_intent_returns_frozen_name(self):
        s=self.talk();ai=AI(None,'fake','fake'); ai.request=Mock(side_effect=AssertionError('No writer needed'))
        state=initial_state();state['last_intent']='name'
        for question in ('Как к вам обращаться?','Представьтесь, пожалуйста.','Как вас зовут?'):
            self.assertEqual(ai.reply(s,question,state),s['card']['identity']['name']+'.')

    def test_issue_deferred_and_resolved_cannot_reopen_without_cause(self):
        card=template('1')['card'];p=core.plan(issue_updates=[dict(id='comparable',status='deferred',reopen_reason='none')])
        state=reduce_plan(initial_state(),p,card)
        with self.assertRaisesRegex(ValueError,'without cause'):
            reduce_plan(state,core.plan(issue_updates=[dict(id='comparable',status='open',reopen_reason='none')]),card)
        state=reduce_plan(state,core.plan(focus_issue_id='comparable'),card)
        self.assertTrue(state['suppress_issue_repeat'])
        state=reduce_plan(state,core.plan(issue_updates=[dict(id='comparable',status='open',reopen_reason='contradiction')]),card)
        self.assertEqual(state['issues']['comparable'],'open')

    def test_repeated_issue_suppressed_after_two_mentions(self):
        card=template('1')['card'];state=initial_state()
        for _ in range(3):state=reduce_plan(state,core.plan(focus_issue_id='comparable'),card)
        self.assertTrue(state['suppress_issue_repeat'])

    def test_fourth_training_has_offer_without_erasing_reports(self):
        for _ in range(3):
            self.send('/new');self.start();self.send('Для какой задачи?');self.send('/finish')
        self.send('/new');s=self.start()
        self.assertEqual(s['phase'],'ready');self.assertEqual(self.store.attempts(10),3)
        self.assertIn('корпоративного тренажёра',''.join(x['body'] for x in self.store.outgoing(10)))


if __name__=='__main__':unittest.main()

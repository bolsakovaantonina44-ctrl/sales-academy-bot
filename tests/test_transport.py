import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from academy.ai import AI
from academy.store import Store
from academy.engine import Engine, deliver
from academy.domain import initial_state
from test_core import FakeAI
from bot import connect_telegram, worker, keyboard_rows


class TransportTests(unittest.TestCase):
    def test_telegram_connection_falls_back_to_recovery_token(self):
        bots = []

        class FakeBot:
            def __init__(self, token, threaded=False):
                self.token = token
                bots.append(self)

            def get_me(self):
                if self.token == 'bad-primary':
                    raise RuntimeError('unauthorized')
                return SimpleNamespace(username='Academy_sales_trainer_bot')

            def get_webhook_info(self, timeout):
                return SimpleNamespace(url='', pending_update_count=0)

        module = SimpleNamespace(TeleBot=FakeBot)
        bot, identity, webhook, source = connect_telegram(
            module, [('TELEGRAM_TOKEN', 'bad-primary'), ('LEGACY_BOT_TOKEN', 'working-recovery')]
        )
        self.assertEqual([item.token for item in bots], ['bad-primary', 'working-recovery'])
        self.assertEqual(bot.token, 'working-recovery')
        self.assertEqual(identity.username, 'Academy_sales_trainer_bot')
        self.assertEqual(source, 'LEGACY_BOT_TOKEN')

    def test_public_setup_has_no_product_specific_demo_buttons(self):
        rows = keyboard_rows({'phase': 'setup'}, admin=False)
        self.assertEqual(rows, [['Мои тренировки']])
        self.assertFalse(any(value in ('1', '2', '3') for row in rows for value in row))

    def test_waiting_earlier_message_cannot_be_overtaken(self):
        with tempfile.TemporaryDirectory() as temp:
            s=Store(Path(temp)/'db')
            s.enqueue('1',1,1,'text','first'); first=s.claim();s.defer(first)
            s.enqueue('2',1,1,'text','second');s.enqueue('3',2,2,'text','other user')
            self.assertEqual(s.claim()['text'],'other user')
            self.assertIsNone(s.claim())
            s.release_waiting(1)
            self.assertEqual(s.claim()['text'],'first')
    def test_voice_retry_transcribes_once_and_commits_one_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            s=Store(Path(temp)/'db');a=FakeAI();e=Engine(s,a)
            for i,t in enumerate(['/start','Тестовый Менеджер','1','Дорого','Начать тренировку']):
                s.enqueue(str(i),1,1,'text',t);e.handle(s.claim())
            session=s.current(1);session['lpr_gate_passed']=True
            s.enqueue('gate-state',1,1,'text','test setup');s.commit(s.claim(),session,[])
            transcript=Mock(return_value=SimpleNamespace(text='Для чего нужен материал?'))
            a.client=SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=transcript)))
            b=SimpleNamespace(send_chat_action=Mock(),get_file=Mock(return_value=SimpleNamespace(file_path='voice')),
                              download_file=Mock(return_value=b'fake audio'))
            sent=[];stop=threading.Event();a.fail=True
            thread=threading.Thread(target=worker,args=(s,e,a,b,stop,lambda c,t:sent.append(t)))
            s.enqueue('voice',1,1,'voice','file123');thread.start()
            try:
                deadline=time.monotonic()+3
                while not s.failed(1) and time.monotonic()<deadline:time.sleep(.01)
                self.assertIsNotNone(s.failed(1));a.fail=False
                s.enqueue('retry',1,1,'text','/retry')
                deadline=time.monotonic()+3
                while len(s.current(1)['history'])<2 and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(len(s.current(1)['history']),2)
                self.assertEqual(transcript.call_count,1)
                self.assertEqual(s.current(1)['history'][0]['content'],'Для чего нужен материал?')
                self.assertIsNone(s.failed(1))
            finally:stop.set();thread.join(3)
    def test_writer_receives_no_hidden_card(self):
        from academy.scenarios import template
        s=dict(card=template('1')['card'],fields=dict(product='Плитка',difficulty='medium'),history=[])
        a=AI(None,'fake','fake');a.request=Mock(return_value=dict(reply='Для входной зоны.',used_fact_ids=['need']))
        state=initial_state();state['revealed']=['need']
        a.reply(s,'Для чего нужен материал?',state)
        payload=a.request.call_args.args[2]
        encoded=json.dumps(payload,ensure_ascii=False)
        self.assertNotIn(s['card']['hidden_motive'],encoded)
        self.assertNotIn('success_condition',encoded)
        self.assertEqual([f['id'] for f in payload['allowed_facts']],['need'])
    def test_api_contract_and_incomplete_response(self):
        from academy.domain import FIELDS_SCHEMA
        payload=dict(product='Плитка',customer='Закупщик',goal='Расчёт',difficulty='medium')
        create=Mock(return_value=SimpleNamespace(status='completed',output_text=json.dumps(payload)))
        a=AI(SimpleNamespace(responses=SimpleNamespace(create=create)),'fake','fake')
        self.assertEqual(a.request('test','extract',{},FIELDS_SCHEMA),payload)
        kwargs=create.call_args.kwargs
        self.assertEqual(kwargs['text']['format']['type'],'json_schema')
        self.assertFalse(kwargs['store'])
        create.return_value.status='incomplete'
        with self.assertRaises(ValueError):a.request('test','extract',{},FIELDS_SCHEMA)

if __name__=='__main__':unittest.main()

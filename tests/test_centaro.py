import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from centaro.funnel import Funnel, PRODUCTS, phone_number
from centaro.runtime import register


class CentaroTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'centaro.sqlite3'
        self.app = Funnel(self.path, 418136392)
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def act(self, user=42, **kwargs):
        self.counter += 1
        self.app.process(str(self.counter), user, 'Ирина', 'irina', **kwargs)
        return str(self.counter)

    def rows(self, table):
        with self.app.db() as db:
            return list(db.execute('SELECT * FROM ' + table))

    def apply(self, product='course', user=42):
        self.act(user, callback='centaro:product:' + product)
        with self.app.db() as db:
            nonce = db.execute('SELECT nonce FROM drafts WHERE user_id=?', (user,)).fetchone()[0]
        self.act(user, callback='centaro:apply:' + nonce)
        return nonce

    def test_all_products_contacts_saved_and_reopened(self):
        for index, product in enumerate(PRODUCTS):
            user = 42 + index
            self.apply(product, user)
            self.act(user, contact={'user_id': user, 'phone_number': '89991234567'})
        reopened = Funnel(self.path, 418136392)
        with reopened.db() as db:
            leads = list(db.execute('SELECT * FROM leads'))
        self.assertEqual(len(leads), 5)
        self.assertEqual({r['product'] for r in leads}, set(PRODUCTS))
        self.assertTrue(all(r['phone'] == '+79991234567' for r in leads))
        owner_messages = [json.loads(r['payload'])['text'] for r in self.rows('outbox') if r['recipient'] == 418136392]
        self.assertEqual(len(owner_messages), 5)
        self.assertTrue(all('Ирина' in text and '@irina' in text and 'Telegram ID:' in text for text in owner_messages))

    def test_duplicate_contact_event_and_double_submit(self):
        self.apply()
        key = self.act(text='+79991234567')
        self.app.process(key, 42, 'Ирина', 'irina', text='+79991234567')
        self.act(text='+79991234567')
        self.assertEqual(len(self.rows('leads')), 1)

    def test_no_username_telegram_only_and_owner_reply(self):
        self.apply()
        self.app.process('only', 42, 'Ирина', '', text='Только Telegram')
        lead = self.rows('leads')[0]
        self.assertIsNone(lead['phone'])
        self.assertFalse(self.app.reply('bad', 77, lead['id'], 'секрет'))
        self.assertTrue(self.app.reply('reply', 418136392, lead['id'], 'Здравствуйте'))
        self.app.reply('reply', 418136392, lead['id'], 'Здравствуйте')
        responses = [json.loads(r['payload'])['text'] for r in self.rows('outbox') if 'Здравствуйте' in r['payload']]
        self.assertEqual(len(responses), 1)

    def test_restart_keeps_pending_draft(self):
        nonce = self.apply()
        self.app = Funnel(self.path, 418136392)
        self.act(text='Только Telegram')
        self.assertEqual(len(self.rows('leads')), 1)
        self.act(callback='centaro:apply:' + nonce)
        self.assertEqual(len(self.rows('leads')), 1)

    def test_wrong_contact_invalid_phone_and_stale_buttons(self):
        nonce = self.apply()
        self.act(contact={'user_id': 99, 'phone_number': '89991234567'})
        self.act(text='это не номер')
        self.assertEqual(len(self.rows('leads')), 0)
        self.act(callback='centaro:product:demo')
        self.act(callback='centaro:apply:' + nonce)
        self.assertFalse(self.rows('drafts')[0]['waiting'])
        self.act(text='/start')
        self.assertEqual(len(self.rows('drafts')), 0)
        self.assertEqual(json.loads(self.rows('outbox')[-1]['payload'])['markup']['kind'], 'menu')

    def test_notification_failure_recovers_without_lost_lead(self):
        self.apply()
        self.act(text='Только Telegram')
        def failed_owner(recipient, payload):
            if recipient == 418136392:
                raise RuntimeError('network failed')
        for _ in range(6):
            self.app.deliver(failed_owner, now=100)
        self.assertEqual(len(self.rows('leads')), 1)
        pending = [r for r in self.rows('outbox') if not r['sent']]
        self.assertEqual(len(pending), 1)
        self.app = Funnel(self.path, 418136392)
        delivered = []
        self.app.deliver(lambda recipient, payload: delivered.append((recipient, payload)), now=104)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], 418136392)
        self.assertFalse(any(not r['sent'] for r in self.rows('outbox')))

    def test_failed_message_does_not_reorder_recipient(self):
        self.act(text='/start')
        self.act(text='/start')
        self.app.deliver(Mock(side_effect=RuntimeError()), now=100)
        successes = []
        self.app.deliver(lambda recipient, payload: successes.append(payload), now=101)
        self.assertEqual(successes, [])

    def test_phone_validation(self):
        self.assertEqual(phone_number('+7 (999) 123-45-67'), '+79991234567')
        for value in ('123', '99999999999999999', 'abc9991234567'):
            with self.assertRaises(ValueError):
                phone_number(value)

    def test_telebot_registration_and_menu_send(self):
        bot = Mock()
        bot.message_handler.return_value = lambda fn: fn
        bot.callback_query_handler.return_value = lambda fn: fn
        send = register(bot, self.app)
        self.act(text='/start')
        self.app.deliver(send)
        self.assertEqual(bot.send_message.call_count, 2)
        self.assertFalse(any(not r['sent'] for r in self.rows('outbox')))

    def test_actual_telegram_update_dispatch(self):
        import telebot
        from unittest.mock import patch
        bot = telebot.TeleBot('123456:FAKE', threaded=False)
        send = register(bot, self.app)
        user = {'id': 42, 'is_bot': False, 'first_name': 'Ирина', 'username': 'irina'}
        def message(number, text=None, contact=None):
            result = {'message_id': number, 'date': 1, 'chat': {'id': 42, 'type': 'private'}, 'from': user}
            if text is not None:
                result['text'] = text
            if contact:
                result['contact'] = contact
            return result
        def dispatch(update):
            bot.process_new_updates([telebot.types.Update.de_json(json.dumps(update))])
        dispatch({'update_id': 1, 'message': message(1, '/start')})
        with patch.object(bot, 'answer_callback_query'):
            dispatch({'update_id': 2, 'callback_query': {'id': 'p', 'chat_instance': 'x', 'from': user,
                     'message': message(2, 'menu'), 'data': 'centaro:product:demo'}})
            nonce = self.rows('drafts')[0]['nonce']
            dispatch({'update_id': 3, 'callback_query': {'id': 'a', 'chat_instance': 'x', 'from': user,
                     'message': message(3, 'apply'), 'data': 'centaro:apply:' + nonce}})
        dispatch({'update_id': 4, 'message': message(4, contact={'user_id': 42,
                 'phone_number': '+79991234567', 'first_name': 'Ирина'})})
        self.assertEqual(len(self.rows('leads')), 1)
        with patch.object(bot, 'send_message') as sent:
            for _ in range(8):
                self.app.deliver(send)
        owner = [call for call in sent.call_args_list if call.args[0] == 418136392]
        self.assertEqual(len(owner), 1)
        self.assertIn('@irina', owner[0].args[1])
        self.assertIn('+79991234567', owner[0].args[1])

    def test_centaro_database_snapshot_and_restore(self):
        import sqlite3
        self.apply()
        self.act(text='Только Telegram')
        snapshot = Path(self.tmp.name) / 'snapshot.sqlite3'
        with self.app.db() as src, sqlite3.connect(snapshot) as dst:
            src.backup(dst)
        restored = Funnel(snapshot, 418136392)
        with restored.db() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leads').fetchone()[0], 1)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM outbox WHERE sent=0').fetchone()[0], 0)

"""Consent gate at the real Telegram entry point: nothing is queued before acceptance."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class ConsentGateTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'academy.sqlite3')
        self.env = patch.dict(os.environ, {'DB_PATH': self.path, 'ADMIN_IDS': '999', 'OPENAI_API_KEY': 'test-key',
                                           'TELEGRAM_TOKEN': '12345:test-token',
                                           'CONSENT_OPERATOR': 'ИП Тестов Т. Т., ИНН 000000000000',
                                           'CONSENT_DOC_URL': 'https://example.com/consent',
                                           'PRIVACY_POLICY_URL': 'https://example.com/privacy'}, clear=True)
        self.env.start()
        import bot as app
        import telebot
        self.telebot = telebot
        client = telebot.TeleBot('12345:test-token', threaded=False)
        for name in ('send_message', 'send_document', 'send_chat_action', 'answer_callback_query', 'infinity_polling'):
            setattr(client, name, Mock())
        with patch.object(app, 'connect_telegram', return_value=(client, SimpleNamespace(username='test'),
                SimpleNamespace(url='', pending_update_count=0, allowed_updates=[]), 'TELEGRAM_TOKEN')), \
                patch('openai.OpenAI'), patch.object(app.threading, 'Thread'):
            app.main()
        from academy.store import Store
        self.client, self.store, self.seq = client, Store(self.path), 0

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def queued(self):
        with self.store.db() as db:
            return db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]

    def send(self, text=None, voice=False, user=10):
        self.seq += 1
        body = {'message_id': self.seq, 'date': 1, 'chat': {'id': user, 'type': 'private'},
                'from': {'id': user, 'is_bot': False, 'first_name': 'Test'}}
        if voice:
            body['voice'] = {'file_id': 'v', 'file_unique_id': str(self.seq), 'duration': 5, 'file_size': 10}
        else:
            body['text'] = text
        before = self.client.send_message.call_count
        self.client.process_new_updates([self.telebot.types.Update.de_json({'update_id': self.seq, 'message': body})])
        calls = self.client.send_message.call_args_list[before:]
        return [(c.args[1], [b['text'] for r in getattr(c.kwargs.get('reply_markup'), 'keyboard', []) or [] for b in r])
                for c in calls]

    def test_first_message_shows_consent_and_stores_nothing(self):
        from academy import consent
        out = self.send('/start')
        self.assertEqual(len(out), 1)
        text, buttons = out[0]
        self.assertIn('Добро пожаловать в CENTARO', text)
        self.assertIn('согласие на обработку персональных данных: https://example.com/consent', text)
        self.assertIn('https://example.com/privacy', text)
        self.assertIn('/privacy_revoke', text)
        self.assertNotIn('Не называйте', text)
        self.assertLess(len(text), 400)
        self.assertEqual(buttons, [consent.ACCEPT])
        self.assertEqual(self.queued(), 0)
        self.send(voice=True)
        self.assertEqual(self.queued(), 0)
        self.assertFalse(consent.has_consent(self.path, 10))

    def test_accept_then_messages_flow_and_revoke_blocks_again(self):
        from academy import consent
        out = self.send(consent.ACCEPT)
        self.assertIn('согласие сохранено', out[0][0])
        self.assertTrue(consent.has_consent(self.path, 10))
        # Acceptance continues straight into /start: no second tap needed.
        self.assertEqual(self.queued(), 1)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT text FROM inbox').fetchone()[0], '/start')
        out = self.send('/privacy_revoke')
        self.assertIn('Согласие отозвано', out[0][0])
        self.send('/start')
        self.assertEqual(self.queued(), 1)
        self.assertFalse(consent.has_consent(self.path, 10))

    def test_decline_support_and_policy_before_consent(self):
        from academy import consent
        self.assertIn('Без согласия', self.send(consent.DECLINE)[0][0])
        self.assertIn('согласие', self.send('/privacy')[0][0])
        self.send('/support')
        self.assertFalse(consent.has_consent(self.path, 10))

    def test_old_button_label_still_accepted_and_fallback_text(self):
        from academy import consent
        self.send('✅ Принимаю условия')
        self.assertTrue(consent.has_consent(self.path, 10))
        with patch.dict(os.environ, {'CONSENT_DOC_URL': ''}):
            text = consent.consent_text()
        self.assertIn('ИП Тестов Т. Т., ИНН 000000000000', text)
        self.assertIn('OpenAI', text)
        self.assertNotIn('Не называйте', text)

    def test_new_version_requires_new_acceptance_and_switch_off(self):
        from academy import consent
        consent.record(self.path, 10, 'accepted')
        with patch.object(consent, 'CONSENT_VERSION', '2099-01-01'):
            self.assertIn('Добро пожаловать', self.send('/start')[0][0])
            self.assertEqual(self.queued(), 0)
        with patch.dict(os.environ, {'CONSENT_REQUIRED': 'false'}):
            self.send('/start', user=11)
        self.assertEqual(self.queued(), 1)


if __name__ == '__main__':
    unittest.main()

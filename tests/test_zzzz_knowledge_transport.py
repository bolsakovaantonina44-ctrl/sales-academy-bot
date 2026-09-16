"""Exercise real Telegram handlers, with no network or production data."""
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import telebot

from academy import admission, assessment
from academy.access import AKENSO, set_role
from academy.curriculum import MODULE_CONTENT, QUESTION_BANK


class KnowledgeTransportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = str(Path(temp) / 'academy.sqlite3')
        self.stack.enter_context(patch.dict(os.environ, {
            'DB_PATH': self.path, 'ADMIN_IDS': '999',
            'OPENAI_API_KEY': 'test-not-a-real-key',
            'TELEGRAM_TOKEN': '12345:test-not-a-real-token',
        }, clear=True))
        import telemetry_launcher
        import bot
        self.app = bot
        self.bot = telebot.TeleBot('12345:test-not-a-real-token', threaded=False)
        for method in ('send_message', 'edit_message_text', 'answer_callback_query', 'send_document'):
            setattr(self.bot, method, Mock())
        self.bot.infinity_polling = Mock()
        self.stack.enter_context(patch.object(bot, 'connect_telegram', return_value=(
            self.bot, SimpleNamespace(username='test_bot'),
            SimpleNamespace(url='', pending_update_count=0, allowed_updates=['message']),
            'TELEGRAM_TOKEN',
        )))
        self.stack.enter_context(patch('openai.OpenAI'))
        self.stack.enter_context(patch.object(bot.threading, 'Thread'))
        bot.main()
        set_role(self.path, 10, AKENSO)
        self.update_id = 0

    def message(self, text, user_id=10):
        self.update_id += 1
        self.bot.process_new_updates([telebot.types.Update.de_json({
            'update_id': self.update_id,
            'message': {'message_id': self.update_id, 'date': 1, 'text': text,
                        'chat': {'id': user_id, 'type': 'private'},
                        'from': {'id': user_id, 'is_bot': False, 'first_name': 'Test'}},
        })])

    def callback(self, data, user_id=10):
        self.update_id += 1
        self.bot.process_new_updates([telebot.types.Update.de_json({
            'update_id': self.update_id,
            'callback_query': {'id': str(self.update_id), 'data': data, 'chat_instance': 'test',
                'from': {'id': user_id, 'is_bot': False, 'first_name': 'Test'},
                'message': {'message_id': 100, 'date': 1, 'text': 'Test menu',
                            'chat': {'id': user_id, 'type': 'private'}}},
        })])

    def test_polling_replaces_a_previous_messages_only_filter(self):
        options = self.bot.infinity_polling.call_args.kwargs
        self.assertEqual(options.get('allowed_updates'), ['message', 'callback_query'])
        with patch.object(telebot.apihelper, '_make_request', return_value=[]) as request:
            self.bot.get_updates(allowed_updates=options['allowed_updates'])
        params = request.call_args.kwargs['params']
        self.assertEqual(json.loads(params['allowed_updates']), ['message', 'callback_query'])

    def _assert_reference_opened(self, body):
        if self.bot.edit_message_text.called:
            self.assertEqual(self.bot.edit_message_text.call_args.args[0], body)
            return
        sent = ''.join(call.args[1] for call in self.bot.send_message.call_args_list if len(call.args) > 1)
        self.assertIn(body[:200], sent)
        self.assertIn(body[-200:], sent)

    def test_every_module_opens_and_theory_requires_practical_exam(self):
        self.message('База знаний')
        self.assertIn('АКАДЕМИЯ АКЕНСО', self.bot.send_message.call_args.args[1])
        for module_id, item in MODULE_CONTENT.items():
            with self.subTest(module_id=module_id):
                self.bot.send_message.reset_mock()
                self.bot.edit_message_text.reset_mock()
                self.callback(f'learn:read:{module_id}')
                self._assert_reference_opened(item['body'])
                self.callback(f'learn:start:{module_id}')
                for index in range(len(QUESTION_BANK[module_id])):
                    current = assessment.question(self.path, 10)
                    self.assertEqual(current['index'], index)
                    self.callback(f'learn:answer:{module_id}:{index}:{current["correct"]}')
                self.assertIsNone(assessment.question(self.path, 10))
                self.assertIn('100%', self.bot.edit_message_text.call_args.args[0])
        result = admission.assess(self.path, 10)
        self.assertFalse(result['complete'])
        self.assertFalse(result['passed'])
        self.assertEqual(result['theory_score'], 100.0)
        self.assertIn('Практический экзамен', result['missing'][0])
        self.callback('learn:admission')
        self.assertIn('Практический экзамен', self.bot.edit_message_text.call_args.args[0])
        self.callback('team:user:10', user_id=999)
        self.assertIn('Практический экзамен', self.bot.edit_message_text.call_args.args[0])

    def test_answer_positions_are_not_fixed_to_first_option(self):
        self.callback('learn:start:product')
        positions = []
        for index in range(min(4, len(QUESTION_BANK['product']))):
            current = assessment.question(self.path, 10)
            positions.append(current['correct'])
            self.callback(f'learn:answer:product:{index}:{current["correct"]}')
        self.assertGreater(len(set(positions)), 1)

    def test_stale_answer_cannot_consume_the_next_question(self):
        self.callback('learn:start:product')
        first = assessment.question(self.path, 10)
        self.callback(f'learn:answer:product:0:{first["correct"]}')
        self.callback(f'learn:answer:product:0:{first["correct"]}')
        self.assertEqual(assessment.question(self.path, 10)['index'], 1)
        self.assertTrue(self.bot.answer_callback_query.call_args.kwargs.get('show_alert'))

    def test_public_user_cannot_read_or_start_by_text_or_callback(self):
        for text in ('База знаний', 'Продукт', 'Тест: Продукт'):
            self.message(text, user_id=20)
        self.callback('learn:read:product', user_id=20)
        self.callback('learn:start:product', user_id=20)
        self.bot.edit_message_text.assert_not_called()
        self.assertIsNone(assessment.question(self.path, 20))
        for call in self.bot.send_message.call_args_list:
            self.assertNotIn(MODULE_CONTENT['product']['body'], call.args[1])

    def test_granting_company_access_notifies_employee_once(self):
        self.bot.send_message.reset_mock()
        self.callback('acc:set:20:akenso', user_id=999)
        employee_messages = [
            call.args[1] for call in self.bot.send_message.call_args_list
            if call.args and call.args[0] == 20
        ]
        self.assertEqual(len(employee_messages), 1)
        self.assertIn('корпоративный доступ АКЕНСО', employee_messages[0])
        self.assertIn('без ограничения', employee_messages[0])
        self.assertIn('история и разборы сохранены', employee_messages[0])

        self.bot.send_message.reset_mock()
        self.callback('acc:set:20:akenso', user_id=999)
        self.assertFalse(any(call.args and call.args[0] == 20 for call in self.bot.send_message.call_args_list))

    def test_module_text_route_offers_working_test_and_back_buttons(self):
        self.message('Продукт')
        call = self.bot.send_message.call_args
        markup = call.kwargs['reply_markup'].to_dict()
        commands = [button['callback_data'] for row in markup['inline_keyboard'] for button in row]
        self.assertIn('learn:start:product', commands)
        self.assertIn('learn:home:knowledge', commands)

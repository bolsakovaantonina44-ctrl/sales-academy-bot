import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import telebot


class CallbackBootstrapTests(unittest.TestCase):
    def test_hotfix_updates_filter_preserves_polling_and_reads_confirmation_once(self):
        install = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                    'ops/callback_polling_hotfix.py'))['install']
        client = SimpleNamespace(get_webhook_info=Mock(side_effect=[
            SimpleNamespace(allowed_updates=['message']),
            SimpleNamespace(allowed_updates=['message', 'callback_query']),
        ]))
        with patch.object(telebot.TeleBot, 'get_updates', return_value=['update']) as original, \
                patch.object(telebot.TeleBot, 'process_new_callback_query') as callback:
            install()
            result = telebot.TeleBot.get_updates(client, offset=-1)
            self.assertEqual(result, ['update'])
            original.assert_called_once_with(client, offset=-1,
                                             allowed_updates=['message', 'callback_query'])
            self.assertEqual(client.get_webhook_info.call_count, 2)
            telebot.TeleBot.get_updates(client, offset=42, timeout=30,
                                       long_polling_timeout=30, allowed_updates=None)
            original.assert_called_with(client, offset=42, timeout=30, long_polling_timeout=30,
                                        allowed_updates=['message', 'callback_query'])
            self.assertEqual(client.get_webhook_info.call_count, 2)
            telebot.TeleBot.process_new_callback_query(client, ['callback'])
            callback.assert_called_once_with(client, ['callback'])


if __name__ == '__main__':
    unittest.main()

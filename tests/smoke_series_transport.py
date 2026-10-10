"""Exercise the exact production entrypoint and Telegram dispatch without network.

Uses real TeleBot update/handler registration and application Store/Engine;
Telegram sends, OpenAI and polling are replaced. No production credentials/data.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import telebot
from academy.access import AKENSO, set_role
from academy.engine import deliver
from test_call_series import SeriesAI
from test_core import FakeAI


def main():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
        'DB_PATH': str(Path(tmp) / 'db'), 'ADMIN_IDS': '999',
        'OPENAI_API_KEY': 'test-only', 'TELEGRAM_TOKEN': '12345:test-only',
    }, clear=True):
        import academy_v2_launcher  # exact Railway entrypoint + all wrappers
        import bot as app
        client = telebot.TeleBot('12345:test-only', threaded=False)
        for method in ('send_message', 'edit_message_text', 'answer_callback_query', 'send_document'):
            setattr(client, method, Mock())
        client.infinity_polling = Mock()
        with patch.object(app, 'connect_telegram', return_value=(client, SimpleNamespace(username='test'),
            SimpleNamespace(url='', pending_update_count=0, allowed_updates=['message']), 'TELEGRAM_TOKEN')), \
                patch('openai.OpenAI'), patch.object(app.threading, 'Thread') as thread:
            app.main()
            store, engine, _, _, _, send = thread.call_args.kwargs['args']
            engine.ai = SeriesAI()
            set_role(store.path, 10, AKENSO)
            sequence = 0

            def drain():
                while True:
                    event = store.claim()
                    if not event:
                        break
                    engine.handle(event)
                    deliver(store, event['user_id'], send)

            def message(text, user=10):
                nonlocal sequence
                sequence += 1
                client.process_new_updates([telebot.types.Update.de_json(dict(update_id=sequence,
                    message=dict(message_id=sequence, date=1, text=text,
                                 chat=dict(id=user, type='private' if user > 0 else 'group'),
                                 **{'from': dict(id=abs(user), is_bot=False, first_name='Test')})))])
                drain()

            def callback(data):
                nonlocal sequence
                sequence += 1
                client.process_new_updates([telebot.types.Update.de_json(dict(update_id=sequence,
                    callback_query=dict(id=str(sequence), data=data, chat_instance='test',
                        **{'from': dict(id=10, is_bot=False, first_name='Test')},
                        message=dict(message_id=100, date=1, text='Menu', chat=dict(id=10, type='private')))))])
                drain()

            message('/name')
            message('Тестовый Менеджер')
            callback('academyv2:series')
            assert store.current(10)['series_setup']['step'] == 'count'
            for answer in ('10', 'Бухгалтерское сопровождение', 'Собственник компании', '1,2',
                           'Все возражения', '20', 'Высокая', 'Готово', 'Подготовить серию', '/series_begin'):
                message(answer)
            series = store.open_series(10)
            assert series['status'] == 'active'
            before = store.current(10)
            callback('academyv2:pause')
            callback('learn:home:assessment')
            assert store.current(10)['id'] == before['id']
            assert store.current(10)['history'] == before['history']
            assert client.answer_callback_query.call_args.kwargs.get('show_alert')
            for i in range(10):
                message('Для какой задачи выбираете решение?')
                message('Завершить звонок')
                assert len(store.series_results(series['id'])) == i+1
                if i < 9:
                    message('Следующий звонок')
            assert store.get_series(series['id'])['status'] == 'completed'
            message('/series_report ' + str(series['id']))
            assert any('ИТОГ СЕРИИ' in str(call.args) for call in client.send_message.call_args_list)
            assert client.infinity_polling.call_args.kwargs['skip_pending'] is False
            message('/series', user=-20)
            assert store.current(-20)['id'] is None
            # Voice ingress rejects a long clip before it reaches the queue/API.
            sequence += 1
            client.process_new_updates([telebot.types.Update.de_json(dict(update_id=sequence,
                message=dict(message_id=sequence, date=1, chat=dict(id=10, type='private'),
                    **{'from': dict(id=10, is_bot=False, first_name='Test')},
                    voice=dict(file_id='fake', file_unique_id='fake', duration=181, file_size=100))))])
            assert store.claim() is None
            assert 'не длиннее 3 минут' in str(client.send_message.call_args.args)
            # A fully consumed paid package still permits resuming its active call.
            engine.ai = FakeAI()
            engine.limit = 0
            store.grant_paid(20, 1, 30, 'smoke-paid', 999)
            for text in ('/name', 'Платный Тестовый Менеджер', '1', 'Дорого', '/begin', 'Для какой задачи нужен материал?'):
                message(text, user=20)
            assert store.paid_remaining(20) == 0
            before = store.current(20)
            message('/start', user=20)
            assert store.current(20)['history'] == before['history']
            assert store.current(20)['phase'] == 'active'
            assert 'Тренировка продолжается' in str(client.send_message.call_args.args)
            print(json.dumps(dict(production_entrypoint='academy_v2_launcher.py',
                simulated_calls=10, saved=10, callback_pause_blocked=True, report_reopened=True,
                group_blocked=True, oversized_voice_blocked=True, paid_call_resumed=True, external_api_calls=0)))


if __name__ == '__main__':
    main()

"""Full production bootstrap, real Telegram dispatch; isolated DB and mocked external APIs."""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_call_series import SeriesAI


def main():
    with tempfile.TemporaryDirectory() as temp:
        path = str(Path(temp) / 'academy.sqlite3')
        with patch.dict(os.environ, {'DB_PATH': path, 'ADMIN_IDS': '999',
                                     'OPENAI_API_KEY': 'test-key', 'TELEGRAM_TOKEN': '12345:test-token'}, clear=True):
            import academy_v2_launcher
            import bot as app
            import telebot
            from academy.access import set_role, AKENSO, SUPERVISOR
            from academy.call_series import manifest_hash
            from academy.store import Store
            from academy.engine import Engine, deliver
            from academy.centaro_rop_runtime import _persist_management_context
            from academy.series_report import render_series_pdf
            client = telebot.TeleBot('12345:test-token', threaded=False)
            client.send_message = Mock()
            client.send_document = Mock()
            client.send_chat_action = Mock()
            client.get_file = Mock(return_value=SimpleNamespace(file_path='test.ogg'))
            client.download_file = Mock(return_value=b'test-audio')
            client.answer_callback_query = Mock()
            client.infinity_polling = Mock()
            with patch.object(app, 'connect_telegram', return_value=(client, SimpleNamespace(username='test'), SimpleNamespace(url='', pending_update_count=0, allowed_updates=[]), 'TELEGRAM_TOKEN')), patch('openai.OpenAI'), patch.object(app.threading, 'Thread') as thread:
                app.main()
            send = thread.call_args.kwargs['args'][-1]
            store = Store(path)
            ai = SeriesAI()
            ai.client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(
                create=Mock(return_value=SimpleNamespace(text='Какую задачу вы хотите решить?')))))
            engine = Engine(store, ai, admin_ids=[999])
            engine.series_progress = lambda user: deliver(store, user, send)
            set_role(path, 10, AKENSO)
            set_role(path, 12, SUPERVISOR)
            seq = 0
            class OneEvent:
                def __init__(self): self.calls = 0
                def is_set(self):
                    self.calls += 1
                    return self.calls > 1
            def message(text, voice=False):
                nonlocal seq
                seq += 1
                body = {
                    'message_id': seq, 'date': 1, 'text': text, 'chat': {'id': 10, 'type': 'private'},
                    'from': {'id': 10, 'is_bot': False, 'first_name': 'Test'}}
                if voice:
                    body.pop('text')
                    body['voice'] = {'file_id': f'test-voice-{seq}', 'file_unique_id': str(seq), 'duration': 6, 'file_size': 10}
                client.process_new_updates([telebot.types.Update.de_json({'update_id': seq, 'message': body})])
                with store.db() as db:
                    assert db.execute("SELECT COUNT(*) FROM inbox WHERE status='queued'").fetchone()[0], f'Handler swallowed: {text}'
                app.worker(store, engine, ai, client, OneEvent(), send)
                assert not store.failed(10), f'Worker failed: {text}'
            message('/start')
            message('Тестовый Менеджер')
            for count in (10, 20, 30):
                message('/series')
                for text in (str(count), 'Тренажёр продаж', 'Собственник бизнеса', '7', 'Все типовые', 'Средняя', 'Готово', '▶️ Начать серию'):
                    message(text)
                series = store.open_series(10)
                assert series['config']['count'] == count and series['status'] == 'active'
                fixed = series['manifest_hash']
                for i in range(count):
                    message('Какую задачу вы хотите решить?', voice=i == 0)
                    if i == 1:
                        store.recover()
                        message('/series_resume')
                        assert store.current(10)['series_index'] == i+1
                    ai.close = i % 2 == 0
                    if ai.close:
                        message('Спасибо, до свидания')
                    else:
                        message('Завершить звонок')
                    ai.close = False
                    rows = Store(path).series_results(series['id'])
                    assert len(rows) == i+1 and rows[-1]['status'] == 'completed'
                    if i < count-1:
                        rows = client.send_message.call_args.kwargs['reply_markup'].keyboard
                        assert any(button['text'] == 'Следующий звонок' for row in rows for button in row), rows
                        message('Следующий звонок')
                        assert store.current(10)['series_index'] == i+2
                saved = store.get_series(series['id'])
                assert saved['status'] == 'completed' and saved['manifest_hash'] == fixed
                assert client.send_document.called
                assert render_series_pdf(saved, store.series_results(series['id'])).read(4) == b'%PDF'
                message('Посмотреть итог серии')
                assert 'ИТОГ СЕРИИ' in client.send_message.call_args.args[1]
                before_pdf = client.send_document.call_count
                message('Скачать отчёт серии')
                assert client.send_document.call_count == before_pdf + 1
                message(f"/series_report {series['id']}")
            # A pending business questionnaire on an older series cannot swallow
            # actual conversation or the next-call button of the current series.
            message('/series_config ' + json.dumps(dict(count=10, product='CRM', customer='Собственник')))
            message('/series_begin')
            active = store.open_series(10)
            message('Уточняющий вопрос')
            assert len(store.current(10)['history']) == 3
            message('/series_abort')
            assert store.get_series(active['id'])['status'] == 'aborted'
            # Supervisor flow metadata is fixed before activation with a matching hash.
            from academy.call_series import build_manifest, SeriesEngine
            cfg, calls = build_manifest(dict(count=10, product='CRM', customer='Собственник', goals=['showroom']))
            assigned = SeriesEngine(engine).create(10, cfg, calls, 'attestation', 12)
            _persist_management_context(store, 10, None, 'inbound', 25)
            fixed = store.open_series(10)
            assert fixed['manifest_hash'] == manifest_hash(fixed['manifest'])
            message('/series_begin')
            message('Как к вам обращаться?')
            assert store.current(10)['series_id'] == assigned['id']
            assert ai.client.audio.transcriptions.create.call_count == 3
            print('PASS 10/20/30; real Telegram handlers and worker, mocked voice transcription, durable results, natural/manual endings, restart, PDF, pending forecast isolation, fixed supervisor manifest. External API calls: 0.')


if __name__ == '__main__':
    main()

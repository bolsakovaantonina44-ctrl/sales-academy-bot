import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from academy.domain import session_empty
from academy.store import Store


class AdminRegressionTests(unittest.TestCase):
    """Regression coverage for the production admin/session layer.

    This module is intentionally named `zz` so launcher monkey-patches are imported
    after the core tests during normal unittest discovery.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'academy.sqlite3')
        self.old_db_path = os.environ.get('DB_PATH')
        self.old_admin_ids = os.environ.get('ADMIN_IDS')
        os.environ['DB_PATH'] = self.db_path
        os.environ['ADMIN_IDS'] = '999'

        # Import only after DB_PATH is isolated for this test module. launcher is
        # side-effect safe with respect to bot startup (bot.main is guarded).
        import launcher
        self.launcher = launcher
        self.store = Store(self.db_path)

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop('DB_PATH', None)
        else:
            os.environ['DB_PATH'] = self.old_db_path
        if self.old_admin_ids is None:
            os.environ.pop('ADMIN_IDS', None)
        else:
            os.environ['ADMIN_IDS'] = self.old_admin_ids
        self.tmp.cleanup()

    def _session(self, phase='active', user_id=10, history=None, current=True, counted=0):
        session = session_empty()
        session['phase'] = phase
        session['employee'] = {'id': user_id, 'name': 'Тест Менеджер'}
        session['history'] = history or []
        session['fields'].update(
            product='Тестовый продукт', customer='Собственник', goal='Назначить встречу', difficulty='medium'
        )
        session['training_focus'] = 'expensive'
        with self.store.db() as db:
            sid = db.execute(
                'INSERT INTO sessions(user_id,payload,counted) VALUES(?,?,?)',
                (user_id, '{}', counted),
            ).lastrowid
            session['id'] = sid
            db.execute('UPDATE sessions SET payload=? WHERE id=?', (json.dumps(session, ensure_ascii=False), sid))
            if current:
                db.execute(
                    'INSERT INTO users(user_id,current_id) VALUES(?,?) '
                    'ON CONFLICT(user_id) DO UPDATE SET current_id=excluded.current_id',
                    (user_id, sid),
                )
        return sid, session

    def test_exact_voice_mapping_is_isolated_between_sessions(self):
        first_id, first = self._session(history=[{'role': 'user', 'content': 'Одинаковая фраза'}])
        self.assertTrue(self.store.enqueue('voice-1', 10, 10, 'voice', 'FILE_A'))
        with self.store.db() as db:
            event_id = db.execute("SELECT id FROM inbox WHERE event_key='voice-1'").fetchone()[0]
        self.store.cache_text(event_id, 'Одинаковая фраза')

        second_id, second = self._session(history=[{'role': 'user', 'content': 'Одинаковая фраза'}])
        self.assertTrue(self.store.enqueue('voice-2', 10, 10, 'voice', 'FILE_B'))
        with self.store.db() as db:
            event_id = db.execute("SELECT id FROM inbox WHERE event_key='voice-2'").fetchone()[0]
        self.store.cache_text(event_id, 'Одинаковая фраза')

        first_rows = self.launcher._voice_rows_for_session(first_id)
        second_rows = self.launcher._voice_rows_for_session(second_id)
        self.assertEqual([row['raw_text'] for row in first_rows], ['FILE_A'])
        self.assertEqual([row['raw_text'] for row in second_rows], ['FILE_B'])

    def test_voice_replay_sends_exact_saved_file_id(self):
        sid, session = self._session(history=[{'role': 'user', 'content': 'Голос менеджера'}])
        self.assertTrue(self.store.enqueue('voice-replay', 10, 10, 'voice', 'FILE_REPLAY'))
        with self.store.db() as db:
            event_id = db.execute("SELECT id FROM inbox WHERE event_key='voice-replay'").fetchone()[0]
        self.store.cache_text(event_id, 'Голос менеджера')

        bot = Mock()
        self.launcher._send_voices(bot, 999, sid)
        bot.send_voice.assert_called_once()
        args, kwargs = bot.send_voice.call_args
        self.assertEqual(args[0], 999)
        self.assertEqual(args[1], 'FILE_REPLAY')
        self.assertIn('Расшифровка: Голос менеджера', kwargs['caption'])

    def test_legacy_voice_fallback_still_recovers_old_sessions(self):
        sid, session = self._session(history=[{'role': 'user', 'content': 'Старая запись'}])
        with self.store.db() as db:
            db.execute(
                "INSERT INTO inbox(event_key,user_id,chat_id,kind,text,raw_text,status) "
                "VALUES(?,?,?,?,?,?,?)",
                ('legacy-voice', 10, 10, 'text', 'Старая запись', 'LEGACY_FILE', 'done'),
            )
        rows = self.launcher._voice_rows_for_session(sid)
        self.assertEqual([row['raw_text'] for row in rows], ['LEGACY_FILE'])

    def test_session_card_handles_unfinished_session(self):
        sid, session = self._session(phase='active', history=[])
        card = self.launcher._session_card(sid)
        self.assertIn(f'СЕССИЯ #{sid}', card)
        self.assertIn('Статус: active', card)
        self.assertIn('Реплик в диалоге: 0', card)
        self.assertIn('Сохранённых голосовых менеджера: 0', card)

    def test_full_dialogue_is_available_to_admin(self):
        sid, session = self._session(history=[
            {'role': 'user', 'content': 'Добрый день'},
            {'role': 'assistant', 'content': 'Здравствуйте'},
            {'role': 'user', 'content': 'Удобно говорить?'},
        ])
        bot = Mock()
        self.launcher._send_dialog(bot, 999, sid)
        combined = '\n'.join(call.args[1] for call in bot.send_message.call_args_list)
        self.assertIn(f'ДИАЛОГ СЕССИИ #{sid}', combined)
        self.assertIn('👤 Менеджер: Добрый день', combined)
        self.assertIn('🤖 Клиент: Здравствуйте', combined)
        self.assertIn('👤 Менеджер: Удобно говорить?', combined)

    def test_session_pagination_does_not_drop_rows(self):
        for index in range(10):
            self._session(phase='completed', user_id=100 + index, current=False)
        first, total = self.launcher._session_rows(0, 8)
        second, total2 = self.launcher._session_rows(1, 8)
        self.assertEqual(total, 10)
        self.assertEqual(total2, 10)
        self.assertEqual(len(first), 8)
        self.assertEqual(len(second), 2)
        self.assertEqual(len({row['id'] for row in first + second}), 10)

    def test_free_training_limit_blocks_fourth_start_for_non_admin(self):
        sid, session = self._session(phase='ready', user_id=10)
        session['training_focus'] = 'expensive'
        with self.store.db() as db:
            db.execute('UPDATE sessions SET payload=? WHERE id=?', (json.dumps(session, ensure_ascii=False), sid))
            for _ in range(3):
                db.execute('INSERT INTO sessions(user_id,payload,counted) VALUES(?,?,1)', (10, json.dumps(session, ensure_ascii=False)))

        from academy.engine import Engine
        ai = Mock()
        ai.model = 'fake-model'
        ai.eval_model = 'fake-eval'
        ai.transcribe_model = 'fake-transcribe'
        ai.card = Mock(side_effect=AssertionError('AI card generation must not run after limit'))
        engine = Engine(self.store, ai, limit=3, admin_ids=())

        self.assertTrue(self.store.enqueue('begin-after-limit', 10, 10, 'text', 'Начать тренировку'))
        event = self.store.claim()
        engine.handle(event)
        bodies = [item['body'] for item in self.store.outgoing(10)]
        self.assertTrue(any('Использованы все 3 бесплатные тренировки' in body for body in bodies))
        self.assertEqual(self.store.current(10)['phase'], 'ready')
        ai.card.assert_not_called()


if __name__ == '__main__':
    unittest.main()

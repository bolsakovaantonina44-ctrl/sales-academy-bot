import tempfile
import unittest
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from academy.store import Store
from academy.engine import Engine
from academy.usage import UsageBudget, BudgetExceeded
from ops.backup import snapshot, verify
from test_core import FakeAI
from academy.domain import session_empty


class CommercialReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_paid_grant_idempotent_and_isolated(self):
        self.assertTrue(self.store.grant_paid(10, 10, 30, 'receipt', 1))
        self.assertFalse(self.store.grant_paid(10, 10, 30, 'receipt', 1))
        with self.assertRaises(ValueError):
            self.store.grant_paid(11, 10, 30, 'receipt', 1)
        self.assertEqual(Store(self.path).paid_remaining(10), 10)
        self.assertEqual(self.store.paid_remaining(11), 0)

    def test_paid_credit_consumed_once_per_training(self):
        self.store.grant_paid(10, 1, 30, 'receipt', 1)
        engine = Engine(self.store, FakeAI(), limit=0)
        for i, text in enumerate(['/start', 'Тестовый Менеджер', '1', 'Дорого', '/begin', 'Для какой задачи?', 'Какие сроки?']):
            self.store.enqueue(str(i), 10, 10, 'text', text)
            engine.handle(self.store.claim())
        self.assertEqual(self.store.attempts(10), 1)
        self.assertEqual(self.store.paid_remaining(10), 0)
        self.assertFalse(engine.can_start_training(10))

    def test_snapshot_restore_preserves_inflight_state_and_outbox(self):
        engine = Engine(self.store, FakeAI())
        self.store.enqueue('start', 10, 10, 'text', '/start')
        engine.handle(self.store.claim())
        self.store.enqueue('pending', 10, 10, 'text', 'Тестовый Менеджер')
        before = self.store.current(10)
        backup, restored = Path(self.temp.name) / 'backup', Path(self.temp.name) / 'restored'
        snapshot(self.path, backup)
        verify(backup)
        snapshot(backup, restored)
        store = Store(restored)
        self.assertEqual(store.current(10), before)
        self.assertEqual(store.claim()['event_key'], 'pending')
        self.assertEqual(store.outgoing(10), self.store.outgoing(10))
        with self.assertRaises(FileExistsError):
            snapshot(backup, restored)

    def test_failed_reservation_survives_restart_and_limits_users(self):
        budget = UsageBudget(self.store, daily_calls=3, user_calls=1)
        with budget.scope(10):
            budget.reserve('turn', 'fake')
            with self.assertRaises(BudgetExceeded):
                budget.reserve('turn', 'fake')
        with budget.scope(11):
            budget.reserve('turn', 'fake')
        restarted = UsageBudget(Store(self.path), daily_calls=2, user_calls=2)
        with restarted.scope(12), self.assertRaises(BudgetExceeded):
            restarted.reserve('turn', 'fake')

    def test_usd_budget_and_tokens(self):
        budget = UsageBudget(self.store, daily_usd=.01,
                             prices={'fake': {'input_per_million': 1, 'output_per_million': 2}})
        with budget.scope(10):
            req = budget.reserve('turn', 'fake', 1000, 2000)
            budget.finish(req, SimpleNamespace(input_tokens=100, output_tokens=200))
            with self.assertRaises(BudgetExceeded):
                budget.reserve('turn', 'fake', 10000, 20000)
        self.assertEqual(budget.summary()['input_tokens'], 100)
        self.assertAlmostEqual(budget.summary()['usd'], .0005)

    def test_unpriced_and_partial_prices_fail_closed_with_usd_limit(self):
        budget = UsageBudget(self.store, daily_usd=1, prices={'fake': {'input_per_million': 1}})
        with budget.scope(10), self.assertRaises(BudgetExceeded):
            budget.reserve('turn', 'fake', 1000, 2000)
        with self.assertRaises(ValueError):
            UsageBudget(self.store, daily_usd=float('nan'))

    def test_legacy_database_upgrade_preserves_users_results_and_pending_queues(self):
        legacy = Path(self.temp.name) / 'legacy'
        s = session_empty()
        s.update(id=1, phase='completed', report='Сохранённый старый разбор',
                 employee=dict(id=10, name='Старый Пользователь'))
        with sqlite3.connect(legacy) as db:
            db.executescript('''
                CREATE TABLE users(user_id INTEGER PRIMARY KEY,current_id INTEGER);
                CREATE TABLE sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,payload TEXT NOT NULL,counted INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE inbox(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE NOT NULL,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,error_kind TEXT,retry_of INTEGER);
                CREATE TABLE outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,body TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0);
            ''')
            db.execute('INSERT INTO sessions VALUES(1,10,?,1)', (json.dumps(s, ensure_ascii=False),))
            db.execute('INSERT INTO users VALUES(10,1)')
            db.execute("INSERT INTO inbox(event_key,user_id,chat_id,kind,text) VALUES('legacy-voice',10,10,'voice','old-file-id')")
            db.execute("INSERT INTO outbox(user_id,chat_id,body) VALUES(10,10,'Старый ожидающий ответ')")
        upgraded = Store(legacy)
        self.assertEqual(upgraded.current(10), s)
        self.assertEqual(upgraded.attempts(10), 1)
        self.assertIsNone(upgraded.session_for_user(11, 1))
        pending = upgraded.claim()
        self.assertEqual(pending['raw_text'], 'old-file-id')
        self.assertEqual(upgraded.outgoing(10)[0]['body'], 'Старый ожидающий ответ')
        self.assertEqual(upgraded.paid_remaining(10), 0)

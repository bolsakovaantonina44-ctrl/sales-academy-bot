import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from academy.access import AKENSO, set_role
from academy.engine import Engine, deliver
from academy.store import Store
from academy.usage import UsageBudget
from test_call_series import SeriesAI

PRICES = {
    'chat-model': {'input_per_million': 2.0, 'output_per_million': 8.0},
    'voice-model': {'audio_per_minute': 0.003},
}


class SeriesUsageCommandTests(unittest.TestCase):
    ADMIN, EMP = 1, 10

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.store = Store(self.path)
        self.ai = SeriesAI()
        self.engine = Engine(self.store, self.ai, admin_ids=[self.ADMIN])
        self.seq = 0
        set_role(self.path, self.EMP, AKENSO)
        for user in (self.ADMIN, self.EMP):
            self.send('/start', user)
            self.send(f'Тестовый {user}', user)

    def tearDown(self):
        self.temp.cleanup()

    def send(self, text, user=EMP):
        self.seq += 1
        self.store.enqueue(f'u-{self.seq}', user, user, 'text', text)
        self.engine.handle(self.store.claim())
        replies = []
        deliver(self.store, user, lambda c, t: replies.append(t))
        return '\n'.join(replies)

    def run_series(self, count=10):
        cfg = dict(count=count, product='Керамогранит', customer='Директор')
        self.send('/series_config ' + json.dumps(cfg, ensure_ascii=False))
        self.send('/series_begin')
        series = self.store.open_series(self.EMP)
        for i in range(count):
            self.send('Добрый день, удобно говорить?')
            self.send('Завершить звонок')
            if i < count - 1:
                self.send('/series_next')
        return self.store.get_series(series['id'])

    def add_usage(self, series_id):
        sessions = [r['session']['id'] for r in self.store.series_results(series_id)]
        rows = []
        for sid in sessions:
            rows += [(sid, 'transcription', 'voice-model', None, None, 30),
                     (sid, 'client_reply', 'chat-model', 3000, 200, 0),
                     (sid, 'evaluation', 'chat-model', 8000, 1500, 0)]
        rows.append((sessions[0], 'evaluation_review', 'chat-model', None, None, 0))  # crash: no usage data
        with self.store.db() as db:
            UsageBudget(self.store)  # ensure table
            for sid, stage, model, inp, out, audio in rows:
                status = 'reserved' if inp is None and not audio else 'completed'
                db.execute('INSERT INTO ai_usage(day,user_id,session_id,stage,model,created_at,input_tokens,'
                           'output_tokens,audio_seconds,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           ('2026-10-16', self.EMP, sid, stage, model, time.time(), inp, out, audio, status))
        return len(sessions)

    def test_admin_sees_priced_breakdown_in_rubles(self):
        series = self.run_series(10)
        self.add_usage(series['id'])
        env = {'AI_PRICES_JSON': json.dumps(PRICES), 'USD_RUB_RATE': '90'}
        with patch.dict(os.environ, env):
            self.ai.budget = UsageBudget(self.store)
            text = self.send(f"/series_usage {series['id']}", self.ADMIN)
        self.assertIn(f"СЕРИЯ №{series['id']}", text)
        self.assertIn('Звонков сохранено: 10/10', text)
        self.assertIn('Обращений к ИИ: 31', text)
        self.assertIn('Голос: 5.0 мин', text)
        # 10 calls: chat = (110000*2 + 17000*8)/1e6 = 0.356; voice = 300s*0.003/60 = 0.015
        self.assertIn('Итого: $0.37 ≈ 33 ₽', text)
        self.assertIn('На один звонок: $0.04 ≈ 3 ₽', text)
        self.assertIn('Распознавание голоса', text)
        self.assertIn('без данных о расходе (сбой или обрыв): 1', text)
        self.assertNotIn('нет цены в AI_PRICES_JSON', text)

    def test_latest_series_without_number_and_unpriced_is_not_zero(self):
        series = self.run_series(10)
        self.add_usage(series['id'])
        with patch.dict(os.environ, {'AI_PRICES_JSON': json.dumps({'chat-model': PRICES['chat-model']})}):
            os.environ.pop('USD_RUB_RATE', None)
            self.ai.budget = UsageBudget(self.store)
            text = self.send('/series_usage', self.ADMIN)
        self.assertIn(f"СЕРИЯ №{series['id']}", text)
        self.assertIn('Итого по этапам с ценой: $0.36', text)
        self.assertIn('нет цены в AI_PRICES_JSON для voice-model', text)
        self.assertIn('задайте USD_RUB_RATE', text)

    def test_employee_cannot_see_costs_and_bad_input(self):
        series = self.run_series(10)
        self.assertIn('только администратору', self.send(f"/series_usage {series['id']}"))
        self.assertIn('Формат', self.send('/series_usage abc', self.ADMIN))
        self.assertIn('не найдена', self.send('/series_usage 999', self.ADMIN))
        self.assertIn('не записано', self.send(f"/series_usage {series['id']}", self.ADMIN))


if __name__ == '__main__':
    unittest.main()

"""Durable reservations bound API attempts, including crashes and unknown billing.

Prices are deployment configuration, never guessed. Failed/unknown requests keep
their reservation; a crash must not reset the spending guard.
"""
import contextvars
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone


class BudgetExceeded(ValueError):
    pass


class UsageBudget:
    def __init__(self, store, *, daily_calls=None, user_calls=None, daily_usd=None, prices=None):
        self.store = store
        self.daily_calls = int(daily_calls or os.getenv('AI_DAILY_CALLS', '3000'))
        self.user_calls = int(user_calls or os.getenv('AI_USER_DAILY_CALLS', '1000'))
        self.daily_usd = float(daily_usd if daily_usd is not None else os.getenv('AI_DAILY_USD', '0'))
        self.prices = prices if prices is not None else json.loads(os.getenv('AI_PRICES_JSON', '{}'))
        self.context = contextvars.ContextVar('usage_context', default=(None, None))
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ai_usage(
                id INTEGER PRIMARY KEY, day TEXT NOT NULL, user_id INTEGER, session_id INTEGER,
                stage TEXT NOT NULL, model TEXT NOT NULL, created_at REAL NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, audio_seconds INTEGER,
                reserved_usd REAL, charged_usd REAL, status TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS ix_usage_day ON ai_usage(day,user_id)')

    @contextmanager
    def scope(self, user, session=None):
        token = self.context.set((user, session))
        try:
            yield
        finally:
            self.context.reset(token)

    def reserve(self, stage, model, input_bound=0, output_bound=0, audio_seconds=0):
        user, session = self.context.get()
        day = datetime.now(timezone.utc).date().isoformat()
        price = self.prices.get(model)
        estimate = None
        if price:
            estimate = (input_bound * float(price.get('input_per_million', 0)) / 1_000_000
                        + output_bound * float(price.get('output_per_million', 0)) / 1_000_000
                        + audio_seconds * float(price.get('audio_per_minute', 0)) / 60)
        if self.daily_usd and (not price or estimate is None or estimate <= 0):
            raise BudgetExceeded('Configure prices for every enabled model before USD budget use')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            total = db.execute('SELECT COUNT(*), COALESCE(SUM(COALESCE(charged_usd,reserved_usd)),0) FROM ai_usage WHERE day=?', (day,)).fetchone()
            own = db.execute('SELECT COUNT(*) FROM ai_usage WHERE day=? AND user_id IS ?', (day, user)).fetchone()[0]
            if total[0] >= self.daily_calls or own >= self.user_calls:
                raise BudgetExceeded('Daily AI request limit reached')
            if self.daily_usd and total[1] + estimate > self.daily_usd:
                raise BudgetExceeded('Daily AI spending limit reached')
            return db.execute('INSERT INTO ai_usage(day,user_id,session_id,stage,model,created_at,audio_seconds,reserved_usd,status) VALUES(?,?,?,?,?,?,?,?,?)',
                              (day, user, session, stage, model, time.time(), audio_seconds, estimate, 'reserved')).lastrowid

    def finish(self, request_id, usage=None, *, audio_seconds=0):
        inp = getattr(usage, 'input_tokens', None)
        out = getattr(usage, 'output_tokens', None)
        with self.store.db() as db:
            row = db.execute('SELECT model FROM ai_usage WHERE id=?', (request_id,)).fetchone()
            price = self.prices.get(row[0])
            actual = None
            if price and (inp is not None and out is not None):
                actual = (inp * float(price.get('input_per_million', 0))
                          + out * float(price.get('output_per_million', 0))) / 1_000_000
            if price and audio_seconds and 'audio_per_minute' in price:
                actual = audio_seconds * float(price['audio_per_minute']) / 60
            db.execute("UPDATE ai_usage SET input_tokens=?,output_tokens=?,charged_usd=?,status='completed' WHERE id=?",
                       (inp, out, actual, request_id))

    def summary(self):
        with self.store.db() as db:
            row = db.execute('SELECT COUNT(*) calls, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, SUM(audio_seconds) audio_seconds, SUM(COALESCE(charged_usd,reserved_usd)) usd, SUM(reserved_usd IS NULL) unpriced FROM ai_usage').fetchone()
            return dict(row)

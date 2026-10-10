"""Durable reservations bound API attempts, including crashes and unknown billing.

Prices are deployment configuration, never guessed. Failed/unknown requests keep
their reservation; a crash must not reset the spending guard.
"""
import contextvars
import json
import os
import math
import time
from contextlib import contextmanager
from datetime import datetime, timezone


class BudgetExceeded(ValueError):
    pass


class UsageBudget:
    def __init__(self, store, *, daily_calls=None, user_calls=None, daily_usd=None, prices=None):
        self.store = store
        self.daily_calls = int(daily_calls or os.getenv('AI_DAILY_CALLS', '5000'))
        self.user_calls = int(user_calls or os.getenv('AI_USER_DAILY_CALLS', '3500'))
        self.daily_usd = float(daily_usd if daily_usd is not None else os.getenv('AI_DAILY_USD', '0'))
        self.prices = prices if prices is not None else json.loads(os.getenv('AI_PRICES_JSON', '{}'))
        if (self.daily_calls <= 0 or self.user_calls <= 0
                or not math.isfinite(self.daily_usd) or self.daily_usd < 0 or not isinstance(self.prices, dict)):
            raise ValueError('Invalid AI budget configuration')
        for rate in self.prices.values():
            if (not isinstance(rate, dict) or not rate or any(key not in
                ('input_per_million', 'output_per_million', 'audio_per_minute') for key in rate)
                or any(not math.isfinite(float(v)) or float(v) < 0 for v in rate.values())):
                raise ValueError('Invalid model price configuration')
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
            if self.daily_usd and not audio_seconds and not {'input_per_million', 'output_per_million'} <= set(price):
                raise BudgetExceeded('Configure both input and output model prices')
            if self.daily_usd and audio_seconds and 'audio_per_minute' not in price:
                raise BudgetExceeded('Configure audio model price')
            if audio_seconds and 'audio_per_minute' in price:
                estimate = audio_seconds * float(price['audio_per_minute']) / 60
            elif not audio_seconds and {'input_per_million', 'output_per_million'} <= set(price):
                estimate = (input_bound * float(price['input_per_million'])
                            + output_bound * float(price['output_per_million'])) / 1_000_000
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
        if type(inp) is not int or type(out) is not int or inp < 0 or out < 0:
            inp = out = None
        with self.store.db() as db:
            row = db.execute('SELECT model FROM ai_usage WHERE id=?', (request_id,)).fetchone()
            price = self.prices.get(row[0])
            actual = None
            if price and {'input_per_million', 'output_per_million'} <= set(price) and inp is not None and out is not None:
                actual = (inp * float(price.get('input_per_million', 0))
                          + out * float(price.get('output_per_million', 0))) / 1_000_000
            if price and audio_seconds and 'audio_per_minute' in price:
                actual = audio_seconds * float(price['audio_per_minute']) / 60
            db.execute("UPDATE ai_usage SET input_tokens=?,output_tokens=?,charged_usd=?,status='completed' WHERE id=?",
                       (inp, out, actual, request_id))

    def summary(self, series_id=None):
        with self.store.db() as db:
            query = 'SELECT COUNT(*) calls, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, SUM(audio_seconds) audio_seconds, SUM(COALESCE(charged_usd,reserved_usd)) usd, SUM(reserved_usd IS NULL) unpriced FROM ai_usage'
            args = ()
            if series_id is not None:
                query += " WHERE session_id IN (SELECT id FROM sessions WHERE json_extract(payload,'$.series_id')=?)"
                args = (series_id,)
            row = db.execute(query, args).fetchone()
            return dict(row)

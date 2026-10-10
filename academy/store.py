"""Single-worker SQLite inbox + atomic state/outbox. Mount DB_PATH on a volume."""
import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from .domain import chunks, session_empty, upgrade_session, is_finish_command, normalize_command


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, current_id INTEGER);
            CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, payload TEXT NOT NULL, counted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0, error_kind TEXT, retry_of INTEGER);
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS ix_inbox_status ON inbox(status,id);
            CREATE INDEX IF NOT EXISTS ix_outbox_user ON outbox(user_id,sent,id);
            CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id,id);
            CREATE TABLE IF NOT EXISTS paid_grants(
                reference TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                credits INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL, granted_by INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS call_series(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                assigned_by INTEGER, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS ix_one_open_series ON call_series(user_id)
                WHERE status IN ('assigned','active');
            CREATE TABLE IF NOT EXISTS series_calls(
                series_id INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                session_id INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(series_id,ordinal));
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(inbox)')}
            if 'raw_text' not in columns:
                db.execute('ALTER TABLE inbox ADD COLUMN raw_text TEXT')
                db.execute('UPDATE inbox SET raw_text=text')
            if 'received_at' not in columns:
                db.execute('ALTER TABLE inbox ADD COLUMN received_at REAL')
            if 'voice_seconds' not in columns:
                db.execute('ALTER TABLE inbox ADD COLUMN voice_seconds INTEGER')
            out_columns = {row[1] for row in db.execute('PRAGMA table_info(outbox)')}
            if 'session_id' not in out_columns:
                db.execute('ALTER TABLE outbox ADD COLUMN session_id INTEGER')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        # No state commit occurred for these events. The model call may repeat, not the turn.
        with self.db() as db:
            db.execute("UPDATE inbox SET status='queued' WHERE status IN ('working','waiting')")
            for row in db.execute("SELECT * FROM call_series WHERE status='active'").fetchall():
                value = json.loads(row['payload'])
                value.setdefault('interruptions', []).append(dict(kind='technical_restart', at=self.now()))
                value['timing_uncertain'] = True
                db.execute('UPDATE call_series SET payload=? WHERE id=?', (dump(value), row['id']))

    def enqueue(self, event_key, user_id, chat_id, kind, text):
        with self.db() as db:
            # Telegram reply-keyboard taps have different message IDs. While a slow
            # evaluation is running, coalesce repeated finish taps into one event.
            if kind == 'text' and is_finish_command(text):
                pending = db.execute(
                    "SELECT text FROM inbox WHERE user_id=? AND status IN ('queued','working','waiting')",
                    (user_id,),
                ).fetchall()
                if any(is_finish_command(row['text']) for row in pending):
                    return False
            import time
            return db.execute('INSERT OR IGNORE INTO inbox(event_key,user_id,chat_id,kind,text,raw_text,received_at) VALUES(?,?,?,?,?,?,?)',
                              (event_key, user_id, chat_id, kind, text, text, time.time())).rowcount == 1

    def claim(self):
        with self.db() as db:
            row = db.execute("SELECT q.* FROM inbox q WHERE q.status='queued' AND NOT EXISTS (SELECT 1 FROM inbox earlier WHERE earlier.user_id=q.user_id AND earlier.id<q.id AND earlier.status IN ('queued','working','waiting')) ORDER BY q.id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE inbox SET status='working',attempts=attempts+1 WHERE id=?", (row['id'],))
                return dict(row)

    def current(self, user_id):
        with self.db() as db:
            row = db.execute('SELECT s.payload FROM users u JOIN sessions s ON s.id=u.current_id WHERE u.user_id=?',
                             (user_id,)).fetchone()
            return upgrade_session(json.loads(row[0])) if row else session_empty()

    def session_for_user(self, user_id, session_id):
        with self.db() as db:
            row = db.execute('SELECT payload FROM sessions WHERE id=? AND user_id=?', (session_id, user_id)).fetchone()
        return upgrade_session(json.loads(row[0])) if row else None

    def attempts(self, user_id):
        with self.db() as db:
            return db.execute('SELECT COALESCE(SUM(counted),0) FROM sessions WHERE user_id=?', (user_id,)).fetchone()[0]

    def recent(self, user_id):
        with self.db() as db:
            rows = db.execute('SELECT payload FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 10', (user_id,)).fetchall()
        return [upgrade_session(json.loads(r[0])) for r in rows]

    def failed(self, user_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM inbox WHERE user_id=? AND status='failed' ORDER BY id DESC LIMIT 1",
                             (user_id,)).fetchone()
            return dict(row) if row else None

    def retry_event(self, event, failed):
        # Resume exact persisted input; a voice transcription is cached separately.
        restored = dict(failed)
        restored['id'] = event['id']
        restored['retry_of'] = failed.get('retry_of') or failed['id']
        restored['attempts'] = failed['attempts'] + 1
        with self.db() as db:
            db.execute('UPDATE inbox SET kind=?,text=?,raw_text=?,retry_of=?,attempts=? WHERE id=?',
                       (failed['kind'], failed['text'], failed.get('raw_text', failed['text']),
                        restored['retry_of'], restored['attempts'], event['id']))
        return restored

    def cache_text(self, event_id, text):
        with self.db() as db:
            db.execute("UPDATE inbox SET kind='text',text=? WHERE id=?", (text, event_id))

    def commit(self, event, session, replies, counted=False, series=None, call_result=None):
        """Session, usage, input acknowledgement and outgoing messages are one transaction."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            sid = session['id']
            if sid is None:
                old = db.execute('SELECT s.id,s.payload FROM sessions s JOIN users u ON u.current_id=s.id WHERE u.user_id=?', (event['user_id'],)).fetchone()
                if old:
                    archived = json.loads(old['payload'])
                    if archived['phase'] != 'completed':
                        archived['phase'] = 'abandoned'
                        db.execute('UPDATE sessions SET payload=? WHERE id=?', (dump(archived), old['id']))
                sid = db.execute('INSERT INTO sessions(user_id,payload) VALUES(?,?)',
                                 (event['user_id'], '{}')).lastrowid
                session['id'] = sid
            previous = db.execute('SELECT counted FROM sessions WHERE id=? AND user_id=?',
                                  (sid, event['user_id'])).fetchone()
            if previous is None:
                raise ValueError('Session owner mismatch')
            if counted and not previous['counted'] and session.get('paid_training'):
                grant = db.execute("SELECT reference FROM paid_grants WHERE user_id=? AND used<credits AND expires_at>? ORDER BY expires_at LIMIT 1",
                                   (event['user_id'], self.now())).fetchone()
                if grant is None:
                    raise ValueError('Paid access expired or exhausted')
                db.execute('UPDATE paid_grants SET used=used+1 WHERE reference=?', (grant[0],))
                session['paid_consumed'] = True
            db.execute('UPDATE sessions SET payload=?, counted=MAX(counted,?) WHERE id=? AND user_id=?',
                       (dump(session), int(counted), sid, event['user_id']))
            db.execute('INSERT INTO users(user_id,current_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET current_id=excluded.current_id',
                       (event['user_id'], sid))
            db.execute("UPDATE inbox SET status='done' WHERE id=?", (event['id'],))
            if event.get('retry_of'):
                db.execute("UPDATE inbox SET status='done' WHERE user_id=? AND status='failed'", (event['user_id'],))
            if event.get('discard_failed'):
                db.execute("UPDATE inbox SET status='discarded' WHERE user_id=? AND status='failed'", (event['user_id'],))
            for reply in replies:
                for part in chunks(reply):
                    db.execute('INSERT INTO outbox(user_id,chat_id,body,session_id) VALUES(?,?,?,?)',
                               (event['user_id'], event['chat_id'], part, sid))
            if series is not None:
                stored = db.execute('SELECT status FROM call_series WHERE id=? AND user_id=?',
                                    (series['id'], event['user_id'])).fetchone()
                if stored is None:
                    raise ValueError('Series owner mismatch')
                if stored['status'] == 'assigned' and series['status'] == 'active' and series.get('credits_required'):
                    credits = series['credits_required']
                    grants = db.execute('SELECT reference,credits-used remaining FROM paid_grants WHERE user_id=? AND expires_at>? ORDER BY expires_at',
                                        (event['user_id'], self.now())).fetchall()
                    if sum(g['remaining'] for g in grants) < credits:
                        raise ValueError('Paid credits expired or insufficient for series')
                    for grant in grants:
                        used = min(credits, grant['remaining'])
                        db.execute('UPDATE paid_grants SET used=used+? WHERE reference=?', (used, grant['reference']))
                        credits -= used
                        if not credits:
                            break
                    series['credits_reserved'] = series['credits_required']
                db.execute('UPDATE call_series SET status=?,payload=? WHERE id=? AND user_id=?',
                           (series['status'], dump(series), series['id'], event['user_id']))
            if call_result is not None:
                db.execute('INSERT INTO series_calls(series_id,ordinal,session_id,status,payload) VALUES(?,?,?,?,?) '
                           'ON CONFLICT(series_id,ordinal) DO UPDATE SET status=excluded.status,payload=excluded.payload',
                           (series['id'], session['series_index'], sid, call_result['status'], dump(call_result)))

    def fail(self, event, kind):
        report_failure = (is_finish_command(event['text']) or
                          normalize_command(event['text']) in ('/recheck', 'обновить разбор'))
        message = ('Не удалось подготовить проверенный разбор. Разговор сохранён; дополнительная попытка не списана. '
                   'Нажми «Повторить обработку» — повторять разговор не нужно.' if report_failure else
                   'Не удалось обработать реплику. Она сохранена; дополнительная попытка не списана. '
                   'Нажми «Повторить обработку»; повторять текст или голосовое не нужно.')
        if event.get('attempts', 1) >= 2:
            message = ('Эту реплику не удалось обработать повторно. Она сохранена. '
                       'Нажми «Пропустить эту реплику» или «Завершить тренировку» — разбор будет по доступному разговору.')
        series = self.open_series(event['user_id'])
        if series and series['status'] == 'active':
            message = ('Технический сбой: исходная реплика и сценарий сохранены. /retry — повторить её обработку. '
                       'Новый звонок и пересдача не создаются.' if event.get('attempts', 1) < 2 else
                       'Повторная обработка не удалась. Диалог сохранён; обратитесь в поддержку. '
                       '/series_abort — завершить серию с прерыванием. Сбой учитывается отдельно.')
        with self.db() as db:
            db.execute("UPDATE inbox SET status='failed',error_kind=? WHERE id=?", (kind, event['id']))
            db.execute('INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)',
                       (event['user_id'], event['chat_id'], message))
        self.series_technical(event['user_id'], 'technical_' + kind, event['id'])

    def discard_failed(self, user_id):
        with self.db() as db:
            db.execute("UPDATE inbox SET status='discarded' WHERE user_id=? AND status='failed'", (user_id,))

    def outgoing(self, user_id):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM outbox WHERE user_id=? AND sent=0 ORDER BY id', (user_id,))]

    def pending_users(self):
        with self.db() as db:
            return [r[0] for r in db.execute('SELECT DISTINCT user_id FROM outbox WHERE sent=0')]

    def sent(self, message_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM outbox WHERE id=? AND sent=0', (message_id,)).fetchone()
            if row and row['session_id']:
                saved = db.execute('SELECT payload FROM sessions WHERE id=?', (row['session_id'],)).fetchone()
                if saved:
                    s = json.loads(saved[0])
                    if (s.get('series_active') and s.get('phase') == 'active' and s.get('history')
                            and s['history'][-1]['role'] == 'assistant'
                            and s['history'][-1]['content'] == row['body']):
                        import time
                        s['last_delivered_at'] = time.time()
                        db.execute('UPDATE sessions SET payload=? WHERE id=?', (dump(s), row['session_id']))
                    elif s.get('series_active') and s.get('phase') == 'completed' and row['body'].startswith('Результат звонка сохранён'):
                        import time
                        s['next_call_delivered_at'] = time.time()
                        db.execute('UPDATE sessions SET payload=? WHERE id=?', (dump(s), row['session_id']))
            db.execute('UPDATE outbox SET sent=1 WHERE id=?', (message_id,))

    def defer(self, event):
        with self.db() as db:
            # A temporarily unavailable chat must not starve other users.
            db.execute("UPDATE inbox SET status='waiting' WHERE id=?", (event['id'],))
        self.series_technical(event['user_id'], 'technical_delivery', event['id'])

    def release_waiting(self, user_id):
        with self.db() as db:
            db.execute("UPDATE inbox SET status='queued' WHERE user_id=? AND status='waiting'", (user_id,))

    @staticmethod
    def now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def grant_paid(self, user, credits, days, reference, actor):
        """Manual, auditable provisioning after payment; reference makes retries safe."""
        from datetime import datetime, timezone, timedelta
        if not 1 <= credits <= 1000 or not 1 <= days <= 366 or not 1 <= len(reference) <= 100:
            raise ValueError('Invalid grant')
        with self.db() as db:
            previous = db.execute('SELECT user_id,credits,granted_by FROM paid_grants WHERE reference=?', (reference,)).fetchone()
            if previous and tuple(previous) != (user, credits, actor):
                raise ValueError('Payment reference already used')
            return db.execute('INSERT OR IGNORE INTO paid_grants(reference,user_id,credits,expires_at,granted_by) VALUES(?,?,?,?,?)',
                              (reference, user, credits, (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(), actor)).rowcount == 1

    def paid_remaining(self, user):
        with self.db() as db:
            return db.execute('SELECT COALESCE(SUM(credits-used),0) FROM paid_grants WHERE user_id=? AND expires_at>?',
                              (user, self.now())).fetchone()[0]

    def create_series(self, user, payload, assigned_by=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            status = 'assigned'
            ident = db.execute('INSERT INTO call_series(user_id,assigned_by,status,payload) VALUES(?,?,?,?)',
                               (user, assigned_by, status, '{}')).lastrowid
            payload.update(id=ident, user_id=user, assigned_by=assigned_by, status=status)
            db.execute('UPDATE call_series SET payload=? WHERE id=?', (dump(payload), ident))
            return payload

    def open_series(self, user):
        with self.db() as db:
            row = db.execute("SELECT payload FROM call_series WHERE user_id=? AND status IN ('assigned','active') ORDER BY id DESC LIMIT 1", (user,)).fetchone()
            return json.loads(row[0]) if row else None

    def get_series(self, ident):
        with self.db() as db:
            row = db.execute('SELECT payload FROM call_series WHERE id=?', (ident,)).fetchone()
            return json.loads(row[0]) if row else None

    def recent_series(self, user):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM call_series WHERE user_id=? ORDER BY id DESC LIMIT 10', (user,))]

    def series_results(self, ident):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM series_calls WHERE series_id=? ORDER BY ordinal', (ident,))]

    def checkpoint_series_call(self, series, session, reason):
        """Persist the actual dialogue BEFORE asking the evaluator; restart can resume it."""
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO series_calls(series_id,ordinal,session_id,status,payload) VALUES(?,?,?,?,?)',
                       (series['id'], session['series_index'], session['id'], 'pending',
                        dump(dict(status='pending', session=session, reason=reason))))

    def series_technical(self, user, kind, event_id):
        with self.db() as db:
            row = db.execute("SELECT id,payload FROM call_series WHERE user_id=? AND status='active'", (user,)).fetchone()
            if row:
                value = json.loads(row['payload'])
                record = dict(kind=kind, event_id=event_id)
                if record not in value.setdefault('interruptions', []):
                    value['interruptions'].append(record)
                value['timing_uncertain'] = True
                db.execute('UPDATE call_series SET payload=? WHERE id=?', (dump(value), row['id']))

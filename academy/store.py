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
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(inbox)')}
            if 'raw_text' not in columns:
                db.execute('ALTER TABLE inbox ADD COLUMN raw_text TEXT')
                db.execute('UPDATE inbox SET raw_text=text')

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

    def enqueue(self, event_key, user_id, chat_id, kind, text):
        with self.db() as db:
            return db.execute('INSERT OR IGNORE INTO inbox(event_key,user_id,chat_id,kind,text,raw_text) VALUES(?,?,?,?,?,?)',
                              (event_key, user_id, chat_id, kind, text, text)).rowcount == 1

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

    def commit(self, event, session, replies, counted=False):
        """Session, usage, input acknowledgement and outgoing messages are one transaction."""
        with self.db() as db:
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
                    db.execute('INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)',
                               (event['user_id'], event['chat_id'], part))

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
        with self.db() as db:
            db.execute("UPDATE inbox SET status='failed',error_kind=? WHERE id=?", (kind, event['id']))
            db.execute('INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)',
                       (event['user_id'], event['chat_id'], message))

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
            db.execute('UPDATE outbox SET sent=1 WHERE id=?', (message_id,))

    def defer(self, event):
        with self.db() as db:
            # A temporarily unavailable chat must not starve other users.
            db.execute("UPDATE inbox SET status='waiting' WHERE id=?", (event['id'],))

    def release_waiting(self, user_id):
        with self.db() as db:
            db.execute("UPDATE inbox SET status='queued' WHERE user_id=? AND status='waiting'", (user_id,))

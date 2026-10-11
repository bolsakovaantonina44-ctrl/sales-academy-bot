"""Transactional lead collection and durable notification outbox."""
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

PRODUCTS = {
    'course': ('🎬 Видеокурс — 9 900 ₽', 'Видеокурс из 6 уроков — 9 900 ₽.'),
    'demo': ('🤖 Бесплатное демо ИИ-тренажёра', 'Демо ИИ-тренажёра продаж — бесплатно.'),
    'consultation': ('💬 Консультация — 30 000 ₽', 'Личная консультация — 30 000 ₽.'),
    'company': ('🏢 Корпоративное обучение', 'Корпоративное обучение — условия по запросу.'),
    'mastermind': ('Мастер-майнд', 'Оставьте заявку, чтобы узнать условия участия.'),
}
MENU = {'kind': 'menu'}
CONTACT = {'kind': 'contact'}


def phone_number(value):
    value = str(value).strip()
    if not re.fullmatch(r'\+?[\d\s()\-]+', value):
        raise ValueError('Некорректный телефон')
    digits = re.sub(r'\D', '', value)
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if not 10 <= len(digits) <= 15:
        raise ValueError('Некорректный телефон')
    return '+' + digits


class Funnel:
    def __init__(self, path, owner):
        self.path = str(path)
        self.owner = int(owner)
        if self.owner <= 0:
            raise ValueError('Owner must be a personal Telegram ID')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS drafts(
                  user_id INTEGER PRIMARY KEY, product TEXT, nonce TEXT, waiting INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events(key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS leads(
                  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, product TEXT NOT NULL,
                  full_name TEXT NOT NULL, username TEXT NOT NULL, phone TEXT,
                  consent TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                  id INTEGER PRIMARY KEY, recipient INTEGER NOT NULL, payload TEXT NOT NULL,
                  sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt REAL NOT NULL DEFAULT 0, error TEXT);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def queue(self, db, recipient, text, markup=None):
        db.execute('INSERT INTO outbox(recipient,payload) VALUES(?,?)',
                   (recipient, json.dumps({'text': text, 'markup': markup}, ensure_ascii=False)))

    def process(self, key, user_id, full_name, username, text='', callback='', contact=None):
        """Save the lead and BOTH messages atomically; replays never duplicate leads."""
        user_id = int(user_id)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM events WHERE key=?', (key,)).fetchone():
                return
            db.execute('INSERT INTO events VALUES(?)', (key,))
            draft = db.execute('SELECT * FROM drafts WHERE user_id=?', (user_id,)).fetchone()
            if (text.split()[0].split('@')[0] if text.split() else '') in ('/start', '/cancel') or text == '← Главное меню' or callback == 'centaro:menu':
                db.execute('DELETE FROM drafts WHERE user_id=?', (user_id,))
                self.queue(db, user_id, 'Centaro — новый уровень продаж. Выберите формат:', MENU)
                return
            if callback.startswith('centaro:product:'):
                product = callback.removeprefix('centaro:product:')
                if product not in PRODUCTS:
                    self.queue(db, user_id, 'Выберите формат из меню.', MENU)
                    return
                nonce = uuid.uuid4().hex[:12]
                db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?,0)', (user_id, product, nonce))
                self.queue(db, user_id, PRODUCTS[product][1], {'kind': 'apply', 'nonce': nonce})
                return
            if callback.startswith('centaro:apply:'):
                if not draft or callback.removeprefix('centaro:apply:') != draft['nonce']:
                    self.queue(db, user_id, 'Эта кнопка устарела. Выберите формат заново.', MENU)
                    return
                db.execute('UPDATE drafts SET waiting=1 WHERE user_id=?', (user_id,))
                self.queue(db, user_id,
                           'Как с вами связаться? Поделитесь своим телефоном кнопкой или введите номер с кодом страны. '
                           'Можно выбрать «Только Telegram» и не передавать номер.\n\n'
                           'Нажимая «Поделиться контактом» или отправляя номер, вы разрешаете использовать его '
                           'для связи по этой заявке. /cancel — отменить.', CONTACT)
                return
            if not draft or not draft['waiting']:
                self.queue(db, user_id, 'Выберите формат из меню. /start — главное меню.', MENU)
                return
            phone = None
            if contact:
                if contact.get('user_id') != user_id:
                    self.queue(db, user_id, 'Поделитесь именно своим контактом или введите свой номер.', CONTACT)
                    return
                raw_phone = contact.get('phone_number', '')
            elif text == 'Только Telegram':
                raw_phone = None
            else:
                raw_phone = text
            if raw_phone is not None:
                try:
                    phone = phone_number(raw_phone)
                except ValueError:
                    self.queue(db, user_id, 'Не удалось распознать номер. Например: +7 999 123-45-67.', CONTACT)
                    return
            full_name = str(full_name or 'Без имени')[:200]
            username = str(username or '')[:64]
            consent = 'phone_for_application_contact_v1' if phone else 'telegram_only_v1'
            cursor = db.execute('INSERT INTO leads(user_id,product,full_name,username,phone,consent,created_at) '
                                'VALUES(?,?,?,?,?,?,?)',
                                (user_id, draft['product'], full_name, username, phone, consent, time.time()))
            lead_id = cursor.lastrowid
            product_name = PRODUCTS[draft['product']][0]
            account = '@' + username if username else 'не задан; отвечайте через бота'
            owner_text = (f'Новая заявка Centaro #{lead_id}\nФормат: {product_name}\nИмя: {full_name}\n'
                          f'Telegram: {account}\nTelegram ID: {user_id}\nТелефон: {phone or "только Telegram"}\n\n'
                          f'Ответ через Centaro: /reply {lead_id} текст сообщения')
            self.queue(db, self.owner, owner_text)
            self.queue(db, user_id, f'Ваша заявка #{lead_id} принята ✅\nФормат: {product_name}\n'
                       f'Способ связи: {phone or "Telegram"}.\nМы свяжемся с вами, чтобы уточнить детали.', MENU)
            db.execute('DELETE FROM drafts WHERE user_id=?', (user_id,))

    def reply(self, key, sender, lead_id, text):
        if int(sender) != self.owner:
            return False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM events WHERE key=?', (key,)).fetchone():
                return True
            db.execute('INSERT INTO events VALUES(?)', (key,))
            lead = db.execute('SELECT user_id FROM leads WHERE id=?', (lead_id,)).fetchone()
            if not lead or not text.strip() or len(text) > 3000:
                self.queue(db, sender, 'Формат: /reply НОМЕР_ЗАЯВКИ текст (до 3000 символов).')
                return True
            self.queue(db, lead['user_id'], 'Centaro, сообщение менеджера:\n' + text)
            self.queue(db, sender, f'Ответ по заявке #{lead_id} сохранён в очереди отправки.')
            return True

    def deliver(self, send, now=None):
        """At-least-once: timeout after Telegram accepted a message can cause a duplicate."""
        now = time.time() if now is None else now
        with self.db() as db:
            rows = db.execute('SELECT * FROM outbox o WHERE sent=0 AND next_attempt<=? AND NOT EXISTS (SELECT 1 FROM outbox older WHERE older.recipient=o.recipient AND older.sent=0 AND older.id<o.id) ORDER BY id LIMIT 20', (now,)).fetchall()
        failed = set()
        for row in rows:
            if row['recipient'] in failed:
                continue
            try:
                send(row['recipient'], json.loads(row['payload']))
            except Exception as exc:
                failed.add(row['recipient'])
                with self.db() as db:
                    db.execute('UPDATE outbox SET attempts=attempts+1,error=?,next_attempt=? WHERE id=?',
                               (type(exc).__name__, now + min(3600, 2 ** min(row['attempts'] + 1, 12)), row['id']))
            else:
                with self.db() as db:
                    db.execute('UPDATE outbox SET sent=1,error=NULL WHERE id=?', (row['id'],))

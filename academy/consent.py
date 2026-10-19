"""Consent to personal-data processing, required before the first training message.

The gate runs at the Telegram entry point, before anything is queued, stored as a
session or sent to the AI provider. The consent document and policy links are deployment
configuration (CONSENT_DOC_URL, PRIVACY_POLICY_URL; CONSENT_OPERATOR for the fallback text); CONSENT_REQUIRED=false
switches the gate off. Bumping CONSENT_VERSION asks everyone to accept the new text again.
"""
import os
import sqlite3
import time

CONSENT_VERSION = '2026-10-16'
ACCEPT = '✅ Согласен'
# Keyboards sent before the short notice still carry the old label.
ACCEPT_ALIASES = (ACCEPT, '✅ Принимаю условия', 'Согласен', 'Принимаю условия')
DECLINE = 'Не принимаю'
REVOKE_COMMAND = '/privacy_revoke'
POLICY_COMMAND = '/privacy'
# Commands that must work before consent: they process nothing beyond the command itself.
OPEN_COMMANDS = ('/support', POLICY_COMMAND)
# Sent on acceptance; the entry point then continues straight to /start.
ACCEPTED_REPLY = 'Спасибо, согласие сохранено.'


def required():
    return os.getenv('CONSENT_REQUIRED', 'true').strip().lower() not in ('false', '0', 'no', 'off')


def _connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.execute('''CREATE TABLE IF NOT EXISTS pd_consent(
        user_id INTEGER NOT NULL, version TEXT NOT NULL, action TEXT NOT NULL, created_at REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS ix_pd_consent_user ON pd_consent(user_id, created_at)')
    return db


def _last(path, user_id):
    with _connect(path) as db:
        return db.execute('SELECT version, action FROM pd_consent WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1',
                          (int(user_id),)).fetchone()


def has_consent(path, user_id):
    row = _last(path, user_id)
    return bool(row and row[1] == 'accepted' and row[0] == CONSENT_VERSION)


def record(path, user_id, action):
    with _connect(path) as db:
        db.execute('INSERT INTO pd_consent(user_id, version, action, created_at) VALUES(?,?,?,?)',
                   (int(user_id), CONSENT_VERSION, action, time.time()))


def consent_text():
    """Short in-bot notice; the full consent is a separate document (152-FZ since 01.09.2025)."""
    doc = os.getenv('CONSENT_DOC_URL', '').strip()
    policy = os.getenv('PRIVACY_POLICY_URL', '').strip()
    lines = ['Добро пожаловать в CENTARO!', '']
    if doc:
        lines.append(f'Чтобы начать тренировки, подтвердите согласие на обработку персональных данных: {doc}')
    else:
        operator = os.getenv('CONSENT_OPERATOR', '').strip() or 'владелец сервиса CENTARO'
        lines += [
            'Чтобы начать тренировки, подтвердите согласие на обработку персональных данных.',
            '',
            f'Оператор — {operator}. Обрабатываются Telegram ID, имя, которое вы укажете, и материалы тренировок '
            '(реплики, расшифровки голосовых, оценки и отчёты) — для проведения тренировок и отчётов вам и вашему '
            'руководителю. Для ответов учебного клиента и оценки тексты разговоров без имени и Telegram ID '
            'обрабатывает сервис ИИ OpenAI.',
        ]
    if policy:
        lines.append(f'Подробнее — в политике конфиденциальности: {policy}')
    lines += ['', f'Отозвать согласие можно в любой момент: {REVOKE_COMMAND}']
    return '\n'.join(lines)


def gate(path, user_id, text):
    """Return None to let the message through, or (reply, keyboard_rows) to send instead."""
    if not required():
        return None
    command = (text or '').strip()
    lowered = command.lower()
    if lowered == POLICY_COMMAND:
        rows = [] if has_consent(path, user_id) else [[ACCEPT]]
        return consent_text(), rows
    if lowered == REVOKE_COMMAND:
        if has_consent(path, user_id):
            record(path, user_id, 'revoked')
        return ('Согласие отозвано. Тренировки приостановлены. Чтобы удалить сохранённые данные, '
                'напишите в поддержку: /support.', [[ACCEPT]])
    if has_consent(path, user_id):
        return None
    if lowered.startswith(OPEN_COMMANDS):
        return None
    if command in ACCEPT_ALIASES:
        record(path, user_id, 'accepted')
        return ACCEPTED_REPLY, []
    if command == DECLINE:
        record(path, user_id, 'declined')
        return ('Без согласия тренажёр не может сохранять тренировки и отчёты. '
                f'Если передумаете — нажмите «{ACCEPT}».', [[ACCEPT]])
    return consent_text(), [[ACCEPT]]

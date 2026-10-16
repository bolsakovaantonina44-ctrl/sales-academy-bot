"""Consent to personal-data processing, required before the first training message.

The gate runs at the Telegram entry point, before anything is queued, stored as a
session or sent to the AI provider. Operator details and the policy link are deployment
configuration (CONSENT_OPERATOR, PRIVACY_POLICY_URL, CONSENT_STORAGE); CONSENT_REQUIRED=false
switches the gate off. Bumping CONSENT_VERSION asks everyone to accept the new text again.
"""
import os
import sqlite3
import time

CONSENT_VERSION = '2026-10-16'
ACCEPT = '✅ Принимаю условия'
DECLINE = 'Не принимаю'
REVOKE_COMMAND = '/privacy_revoke'
POLICY_COMMAND = '/privacy'
# Commands that must work before consent: they process nothing beyond the command itself.
OPEN_COMMANDS = ('/support', POLICY_COMMAND)


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
    operator = os.getenv('CONSENT_OPERATOR', '').strip() or 'владелец сервиса CENTARO'
    storage = os.getenv('CONSENT_STORAGE', '').strip() or 'сервер хостинг-провайдера Railway'
    policy = os.getenv('PRIVACY_POLICY_URL', '').strip()
    lines = [
        'СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ',
        '',
        f'Оператор: {operator}.',
        '',
        'Что обрабатывается: ваш Telegram ID, имя, которое вы укажете, тексты реплик и расшифровки '
        'голосовых сообщений на тренировках, оценки и отчёты.',
        '',
        'Зачем: проведение тренировок и аттестаций по продажам, отчёты для вас и — если доступ выдан '
        'работодателем — для назначившего вас руководителя.',
        '',
        f'Где хранится: {storage}. Голосовые сообщения расшифровываются автоматически и не сохраняются.',
        '',
        'Для ответов вымышленного клиента и оценки тексты разговоров без вашего имени и Telegram ID '
        'передаются сервису ИИ OpenAI, в том числе за пределы РФ.',
        '',
        'Не называйте на тренировках реальных клиентов, свою фамилию, телефоны и другие личные данные — '
        'используйте вымышленные.',
        '',
        f'Срок: до отзыва согласия. Отозвать: {REVOKE_COMMAND}. Вопросы и удаление данных: /support.',
    ]
    if policy:
        lines += ['', f'Политика обработки персональных данных: {policy}']
    lines += ['', f'Нажмите «{ACCEPT}», чтобы продолжить.']
    return '\n'.join(lines)


def gate(path, user_id, text):
    """Return None to let the message through, or (reply, keyboard_rows) to send instead."""
    if not required():
        return None
    command = (text or '').strip()
    lowered = command.lower()
    if lowered == POLICY_COMMAND:
        rows = [] if has_consent(path, user_id) else [[ACCEPT], [DECLINE]]
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
    if command == ACCEPT:
        record(path, user_id, 'accepted')
        return 'Спасибо, согласие сохранено. Нажмите /start, чтобы начать.', [['/start']]
    if command == DECLINE:
        record(path, user_id, 'declined')
        return ('Без согласия тренажёр не может сохранять тренировки и отчёты. '
                f'Если передумаете — нажмите «{ACCEPT}».', [[ACCEPT]])
    return consent_text(), [[ACCEPT], [DECLINE]]

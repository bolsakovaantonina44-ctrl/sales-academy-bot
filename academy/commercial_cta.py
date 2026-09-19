"""Commercial funnel shown to PUBLIC users after the free training limit."""
import json
import os
import sqlite3
from datetime import datetime, timezone

from .access import AKENSO, SUPERVISOR, ensure_access_schema
from .store import Store

CTA_KEY = 'commercial_cta_v2'
CTA_HEADING = 'Тестовый доступ завершён.'
INDIVIDUAL_10_PRICE = 1490
INDIVIDUAL_MONTH_PRICE = 3990

COMPANY_QUESTIONS = (
    {'key': 'company_name', 'prompt': 'Как называется ваша компания?'},
    {'key': 'industry', 'prompt': 'Чем занимается компания?'},
    {'key': 'team_size', 'prompt': 'Сколько сотрудников планируете обучать?',
     'options': (('1–5', '1-5'), ('6–15', '6-15'), ('16–50', '16-50'), ('50+', '50+'))},
    {'key': 'goal', 'prompt': 'Какая задача сейчас приоритетна?',
     'options': (
         ('Обучение новичков', 'onboarding'),
         ('Тренировка продаж', 'training'),
         ('Аттестация', 'assessment'),
         ('База знаний и регламенты', 'knowledge'),
         ('Всё вместе', 'all'),
     )},
    {'key': 'knowledge', 'prompt': 'Есть ли сейчас база знаний и регламенты?',
     'options': (('Да', 'yes'), ('Частично', 'partial'), ('Нет', 'no'))},
    {'key': 'contact_name', 'prompt': 'Как к вам обращаться?'},
    {'key': 'contact', 'prompt': 'Оставьте телефон или Telegram для связи.'},
)

OPTION_LABELS = {
    '1-5': '1–5',
    '6-15': '6–15',
    '16-50': '16–50',
    '50+': '50+',
    'onboarding': 'Обучение новичков',
    'training': 'Тренировка продаж',
    'assessment': 'Аттестация',
    'knowledge': 'База знаний и регламенты',
    'all': 'Всё вместе',
    'yes': 'Да',
    'partial': 'Частично',
    'no': 'Нет',
}

_original_init = Store.__init__


def _now():
    return datetime.now(timezone.utc).isoformat()


def _contact():
    value = os.getenv('COMMERCIAL_CONTACT', '').strip()
    return value or 'специалист Академии'


def cta_text():
    return (
        f'{CTA_HEADING}\n\n'
        'Вы прошли 3 бесплатные тренировки и получили разбор своих навыков.\n\n'
        'Выберите, как хотите продолжить: тренироваться самостоятельно '
        'или подключить Академию для своей компании.'
    )


def is_cta_text(value):
    return CTA_HEADING in str(value or '')


def individual_text():
    return (
        'ПРОДОЛЖИТЬ ДЛЯ СЕБЯ\n\n'
        'Выберите вариант доступа:\n\n'
        f'• 10 тренировок — {INDIVIDUAL_10_PRICE:,} ₽\n'
        f'• Безлимит на 30 дней — {INDIVIDUAL_MONTH_PRICE:,} ₽\n\n'
        'В доступ входят AI-тренировки, разбор каждого диалога, оценка навыков '
        'и сохранение истории прогресса.'
    ).replace(',', ' ')


def payment_url(plan):
    env_name = 'PAYMENT_10_URL' if plan == '10' else 'PAYMENT_MONTH_URL'
    value = os.getenv(env_name, '').strip()
    return value or None


def _connect(path):
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def ensure_funnel_schema(path):
    with _connect(path) as db:
        db.executescript(
            '''
            CREATE TABLE IF NOT EXISTS sales_funnel_state(
                user_id INTEGER PRIMARY KEY,
                step INTEGER NOT NULL,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sales_leads(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_sales_leads_user ON sales_leads(user_id,id);
            '''
        )


def start_company(path, user_id, telegram=None):
    ensure_funnel_schema(path)
    data = {'telegram': telegram or {}}
    with _connect(path) as db:
        db.execute(
            '''
            INSERT INTO sales_funnel_state(user_id,step,data,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                step=excluded.step,data=excluded.data,updated_at=excluded.updated_at
            ''',
            (int(user_id), 0, json.dumps(data, ensure_ascii=False), _now()),
        )
    return company_state(path, user_id)


def company_state(path, user_id):
    ensure_funnel_schema(path)
    with _connect(path) as db:
        row = db.execute(
            'SELECT step,data FROM sales_funnel_state WHERE user_id=?',
            (int(user_id),),
        ).fetchone()
    if not row:
        return None
    step = int(row['step'])
    if step >= len(COMPANY_QUESTIONS):
        return None
    return {
        'step': step,
        'question': COMPANY_QUESTIONS[step],
        'data': json.loads(row['data'] or '{}'),
    }


def cancel_company(path, user_id):
    ensure_funnel_schema(path)
    with _connect(path) as db:
        db.execute('DELETE FROM sales_funnel_state WHERE user_id=?', (int(user_id),))


def _normalize_answer(question, value):
    value = ' '.join(str(value or '').split()).strip()
    if question.get('options'):
        allowed = {item[1] for item in question['options']}
        if value not in allowed:
            raise ValueError('Choose one of the offered options')
        return value
    if len(value) < 2:
        raise ValueError('Answer is too short')
    return value[:500]


def answer_company(path, user_id, value):
    state = company_state(path, user_id)
    if not state:
        raise LookupError('Company questionnaire is not active')
    question = state['question']
    answer = _normalize_answer(question, value)
    data = dict(state['data'])
    data[question['key']] = answer
    next_step = state['step'] + 1
    ensure_funnel_schema(path)
    if next_step < len(COMPANY_QUESTIONS):
        with _connect(path) as db:
            db.execute(
                'UPDATE sales_funnel_state SET step=?,data=?,updated_at=? WHERE user_id=?',
                (next_step, json.dumps(data, ensure_ascii=False), _now(), int(user_id)),
            )
        return {'finished': False, 'state': company_state(path, user_id), 'data': data}

    with _connect(path) as db:
        cursor = db.execute(
            'INSERT INTO sales_leads(user_id,kind,payload,created_at) VALUES(?,?,?,?)',
            (int(user_id), 'company', json.dumps(data, ensure_ascii=False), _now()),
        )
        lead_id = cursor.lastrowid
        db.execute('DELETE FROM sales_funnel_state WHERE user_id=?', (int(user_id),))
    return {'finished': True, 'lead_id': lead_id, 'data': data}


def record_individual_interest(path, user_id, plan, telegram=None):
    if plan not in {'10', 'month'}:
        raise ValueError('Unknown individual plan')
    ensure_funnel_schema(path)
    payload = {
        'plan': plan,
        'price': INDIVIDUAL_10_PRICE if plan == '10' else INDIVIDUAL_MONTH_PRICE,
        'telegram': telegram or {},
    }
    with _connect(path) as db:
        cursor = db.execute(
            'INSERT INTO sales_leads(user_id,kind,payload,created_at) VALUES(?,?,?,?)',
            (int(user_id), 'individual', json.dumps(payload, ensure_ascii=False), _now()),
        )
        return cursor.lastrowid


def company_lead_text(lead_id, user_id, data, training_summary=''):
    telegram = data.get('telegram') or {}
    username = telegram.get('username')
    tg_line = f"@{username}" if username else f'Telegram ID {user_id}'
    lines = [
        '🔥 НОВАЯ КОРПОРАТИВНАЯ ЗАЯВКА',
        '',
        f'Лид: #{lead_id}',
        f"Компания: {data.get('company_name', '—')}",
        f"Сфера: {data.get('industry', '—')}",
        f"Сотрудников: {OPTION_LABELS.get(data.get('team_size'), data.get('team_size', '—'))}",
        f"Задача: {OPTION_LABELS.get(data.get('goal'), data.get('goal', '—'))}",
        f"База знаний: {OPTION_LABELS.get(data.get('knowledge'), data.get('knowledge', '—'))}",
        f"Имя: {data.get('contact_name', '—')}",
        f"Контакт: {data.get('contact', '—')}",
        f'Telegram: {tg_line}',
    ]
    if training_summary:
        lines += ['', 'Результаты демо:', training_summary]
    return '\n'.join(lines)


def individual_lead_text(lead_id, user_id, plan, telegram=None):
    telegram = telegram or {}
    username = telegram.get('username')
    tg_line = f"@{username}" if username else f'Telegram ID {user_id}'
    label = (
        f'10 тренировок — {INDIVIDUAL_10_PRICE:,} ₽'
        if plan == '10'
        else f'Безлимит на 30 дней — {INDIVIDUAL_MONTH_PRICE:,} ₽'
    ).replace(',', ' ')
    return (
        '💳 НОВЫЙ ИНТЕРЕС К ТАРИФУ\n\n'
        f'Лид: #{lead_id}\n'
        f'Тариф: {label}\n'
        f'Пользователь: {tg_line}'
    )


def _admin_ids():
    result = set()
    for value in os.getenv('ADMIN_IDS', '').split(','):
        try:
            if value.strip():
                result.add(int(value.strip()))
        except ValueError:
            pass
    return result


def _completed_count(rows):
    total = 0
    for row in rows:
        if not row['counted']:
            continue
        try:
            payload = json.loads(row['payload'])
        except Exception:
            continue
        if payload.get('phase') == 'completed':
            total += 1
    return total


def _queue_existing_completed_users(store):
    limit = int(os.getenv('FREE_TRAININGS', '3'))
    admins = _admin_ids()
    ensure_access_schema(store.path)
    with store.db() as db:
        db.execute(
            'CREATE TABLE IF NOT EXISTS notifications('
            'user_id INTEGER NOT NULL, key TEXT NOT NULL, '
            'PRIMARY KEY(user_id,key))'
        )
        corporate_users = {
            row[0]
            for row in db.execute(
                'SELECT user_id FROM user_access WHERE role IN (?,?)',
                (AKENSO, SUPERVISOR),
            )
        }
        users = [row[0] for row in db.execute('SELECT DISTINCT user_id FROM sessions')]
        for user_id in users:
            if user_id in admins or user_id in corporate_users:
                continue
            rows = db.execute(
                'SELECT counted,payload FROM sessions WHERE user_id=? ORDER BY id',
                (user_id,),
            ).fetchall()
            if _completed_count(rows) < limit:
                continue
            inserted = db.execute(
                'INSERT OR IGNORE INTO notifications(user_id,key) VALUES(?,?)',
                (user_id, CTA_KEY),
            ).rowcount
            if inserted:
                db.execute(
                    'INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)',
                    (user_id, user_id, cta_text()),
                )


def _patched_init(self, path):
    _original_init(self, path)
    ensure_funnel_schema(path)
    _queue_existing_completed_users(self)


if not getattr(Store, '_commercial_cta_patched', False):
    Store.__init__ = _patched_init
    Store._commercial_cta_patched = True

# Future PUBLIC users receive this offer through the normal Engine limit flow.
from . import knowledge as _knowledge  # noqa: E402
_original_profile = _knowledge.profile


def _profile_with_cta():
    value = _original_profile()
    if str(value.get('offer', '')).startswith('Демо завершено.'):
        value['offer'] = cta_text()
    return value


if not getattr(_knowledge, '_commercial_cta_patched', False):
    _knowledge.profile = _profile_with_cta
    _knowledge._commercial_cta_patched = True

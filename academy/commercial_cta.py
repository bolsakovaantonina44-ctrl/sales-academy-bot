"""One-time commercial CTA for users who already completed the free limit."""
import json
import os

from .store import Store

CTA_KEY = 'commercial_cta_v1'

_original_init = Store.__init__


def _contact():
    value = os.getenv('COMMERCIAL_CONTACT', '').strip()
    return value or 'Telegram автора проекта'


def cta_text():
    return (
        'Тестовый доступ завершён.\n\n'
        'Вы прошли 3 бесплатные тренировки и получили разбор своих навыков.\n\n'
        'Полная версия «Академии продаж» настраивается под конкретную компанию: '
        'продукт, реальные клиентские ситуации, возражения, стандарты продаж и отчётность для руководителя.\n\n'
        'Хотите внедрить тренажёр в свой отдел продаж? '
        f'Напишите в Telegram: {_contact()}'
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
    with store.db() as db:
        db.execute(
            'CREATE TABLE IF NOT EXISTS notifications('
            'user_id INTEGER NOT NULL, key TEXT NOT NULL, '
            'PRIMARY KEY(user_id,key))'
        )
        users = [row[0] for row in db.execute('SELECT DISTINCT user_id FROM sessions')]
        for user_id in users:
            if user_id in admins:
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
    _queue_existing_completed_users(self)


if not getattr(Store, '_commercial_cta_patched', False):
    Store.__init__ = _patched_init
    Store._commercial_cta_patched = True

# Future users already receive the profile offer when their free limit is reached.
# Replace only the built-in generic demo offer; preserve any company-specific offer.
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

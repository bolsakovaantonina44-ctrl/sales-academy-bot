"""Telegram transport. Run one replica with persistent DB_PATH. Import is side-effect free."""
import io
import json
import logging
import os
import threading
import time
from pathlib import Path

from academy.ai import AI
from academy.engine import Engine, deliver
from academy.store import Store
from academy.domain import normalize_command, upgrade_session, chunks, is_finish_command
from academy.diagnostics import log_failure
from academy.pacing import FOCUS_OBJECTIONS
from academy.reporting import total_score
from academy.access import PUBLIC, AKENSO, SUPERVISOR, get_role, set_role, has_company_access, ensure_access_schema
from academy.curriculum import MODULE_CONTENT
from academy.learning import progress_snapshot
from academy.ui_policy import (
    CONTEXT_HOME,
    CONTEXT_LEARNING,
    CONTEXT_ASSESSMENT,
    CONTEXT_TRAINING,
    CONTEXT_RESULT,
    CONTEXT_MANAGEMENT,
    rows_for_context,
)
from academy import assessment
from academy import admission
from academy import commercial_cta

LOG = logging.getLogger('academy')
TELEGRAM_ALLOWED_UPDATES = ['message', 'callback_query']
FOCUS_LABELS = [v['label'] for v in FOCUS_OBJECTIONS.values()]
ROLE_LABELS = {PUBLIC: 'Публичный', AKENSO: 'АКЕНСО', SUPERVISOR: 'Руководитель'}

CONTROL_COMMANDS = {
    'начать тренировку', 'новая тренировка', 'завершить тренировку', 'заверши тренировку',
    'закончить тренировку', 'закончи тренировку', 'завершить тест', 'закончить тест',
    'повторить обработку', 'пропустить эту реплику', 'обновить разбор', 'посмотреть разбор',
    'скачать результат', 'сформировать отчет', 'отчет сотруднику', 'отчет руководителю',
    'показать скрытый сценарий', 'мои тренировки', 'сессии пользователей',
    'доступ сотрудников', 'прогресс команды', 'база знаний', 'аттестация',
    'продолжить обучение', 'тренировка', 'мой прогресс', 'команда', 'к академии',
    'управление доступом', 'технические сессии',
    'изменить имя', 'сменить имя',
    'легкий', 'лёгкий', 'средний', 'сложный', '1', '2', '3',
} | {normalize_command(label) for label in FOCUS_LABELS}


def _is_control_text(text):
    """UI/system commands are never manager speech and must bypass every simulation layer."""
    if not text:
        return False
    cmd = normalize_command(text)
    return is_finish_command(text) or cmd.startswith('/') or cmd in CONTROL_COMMANDS


def _focus_rows():
    labels = FOCUS_LABELS
    return [[labels[0], labels[1]], [labels[2], labels[3]], [labels[4]]]


def connect_telegram(telebot_module, token_sources):
    """Use the primary token, with a separately stored known token as recovery."""
    attempted = set()
    for source, token in token_sources:
        token = (token or '').strip()
        if not token or token in attempted:
            continue
        attempted.add(token)
        candidate = telebot_module.TeleBot(token, threaded=False)
        try:
            identity = candidate.get_me()
            webhook = candidate.get_webhook_info(timeout=15)
        except Exception as exc:
            LOG.warning('Startup: Telegram token rejected source=%s kind=%s', source, type(exc).__name__)
            continue
        return candidate, identity, webhook, source
    raise RuntimeError('Telegram startup check failed; verify configured tokens and network')


def keyboard_rows(session, failed=False, admin=False):
    """Training-only keyboard. Management actions intentionally live elsewhere."""
    phase = session['phase']
    if failed:
        return [['Повторить обработку', 'Пропустить эту реплику'], ['Завершить тренировку']]
    if phase == 'active':
        return [['Завершить тренировку']]
    if phase == 'closed':
        return [['Завершить тренировку', 'Новая тренировка']]
    if phase == 'completed':
        rows = [['Посмотреть разбор', 'Скачать результат'], ['Новая тренировка', 'Мои тренировки']]
        if session.get('report_status') == 'technical_partial':
            rows.append(['Обновить разбор'])
        return rows
    if phase == 'ready':
        rows = []
        if session.get('training_focus'):
            rows.append(['Начать тренировку'])
            rows.append(['1', '2', '3'])
        rows.extend(_focus_rows())
        rows.append(['Новая тренировка'])
        return rows
    return [['Мои тренировки']]


def receive_text(store, event_key, user_id, chat_id, kind, text, send):
    accepted = store.enqueue(event_key, user_id, chat_id, kind, text)
    if kind == 'text':
        cmd = normalize_command(text)
        if is_finish_command(text) and not accepted:
            try:
                send(chat_id, 'Разбор уже формируется. Повторно нажимать «Завершить тренировку» не нужно.')
            except Exception:
                pass
        elif accepted and cmd in ('начать тренировку', '/begin'):
            try:
                send(chat_id, 'Запускаю тренировку…' if store.current(user_id)['phase'] == 'ready'
                     else 'Запрос принят. Проверяю состояние тренировки…')
            except Exception:
                pass
        elif accepted and is_finish_command(text):
            try:
                send(chat_id, 'Завершаю тренировку. Готовлю разбор — это может занять около 1 минуты…')
            except Exception:
                pass
        elif cmd in ('/pdf', 'сформировать отчет', 'скачать результат', 'отчет сотруднику', 'отчет руководителю'):
            try:
                send(chat_id, 'Готовлю файл. Это может занять около 1 минуты…')
            except Exception:
                pass
    return accepted


def _direct_lpr_known(session):
    """Skip the gate only when setup explicitly says the manager already has the target contact."""
    setup = ' '.join(str(x.get('content', '')) for x in session.get('setup', []) if isinstance(x, dict)).lower()
    markers = ('разговариваю с ', 'уже разговариваю с ', 'говорю с ', 'уже общаюсь с ',
               'на связи с ', 'встречаюсь с ', 'пишу напрямую ', 'звоню напрямую ')
    return any(marker in setup for marker in markers)


def _lpr_search_attempt(text):
    value = normalize_command(text)
    markers = ('кто отвечает', 'кто занимается', 'кто принимает решение', 'кто принимает решения',
               'с кем можно', 'с кем поговорить', 'с кем обсудить', 'кому можно', 'кому обратиться',
               'соедините', 'соедините с', 'переключите', 'руководитель', 'директор', 'ответственный', 'лпр')
    return any(marker in value for marker in markers)


def _first_name(value):
    """Office staff usually give a first name, not a full fictional passport-style name."""
    return (value or '').strip().split()[0] if (value or '').strip() else ''


def deliver_pdf(bot, store, chat_id, text, admins=()):
    """Turn a durable outbox marker into an authorized Telegram document."""
    marker, raw_id, audience = str(text).split(':', 2)
    if marker != '__academy_pdf__' or audience not in ('employee', 'supervisor'):
        raise ValueError('Invalid PDF delivery marker')
    session_id = int(raw_id)
    with store.db() as db:
        row = db.execute('SELECT user_id,payload FROM sessions WHERE id=?', (session_id,)).fetchone()
    if not row:
        raise LookupError('Report session not found')
    owner_id, report_session = row['user_id'], upgrade_session(json.loads(row['payload']))
    admin_ids = set(admins)
    if owner_id != chat_id and chat_id not in admin_ids:
        raise PermissionError('Report session is unavailable for this chat')
    if audience == 'supervisor' and chat_id not in admin_ids:
        raise PermissionError('Supervisor report requires admin access')
    from academy.pdf_report import render_pdf
    title = 'Отчёт_руководителю' if audience == 'supervisor' else 'Разбор_тренировки'
    fileobj = render_pdf(report_session, audience)
    fileobj.seek(0)
    bot.send_document(chat_id, fileobj, visible_file_name=f'{title}_{session_id}.pdf')


def queue_training_reports(store, user_id, session, admin_ids):
    """Durably queue one supervisor notice and PDF per completed corporate training."""
    if get_role(store.path, user_id) not in {AKENSO, SUPERVISOR}:
        return 0
    if session.get('phase') != 'completed' or not session.get('report_data') or not session.get('id'):
        return 0

    employee = session.get('employee', {})
    employee_name = ' '.join(str(employee.get('name') or '').split()) or f'Telegram ID {user_id}'
    score = total_score(session.get('report_data'))
    result = f'{score}/100' if score is not None else 'оценка требует проверки'
    fields = session.get('fields', {})
    notice = (
        'НОВАЯ ТРЕНИРОВКА СОТРУДНИКА\n\n'
        f'Сотрудник: {employee_name}\n'
        f'Тренировка №{session["id"]}\n'
        f'Клиент: {fields.get("customer") or "не указан"}\n'
        f'Цель: {fields.get("goal") or "не указана"}\n'
        f'Результат: {result}\n\n'
        'Руководительский PDF-отчёт отправлен следующим сообщением.'
    )

    queued = 0
    with store.db() as db:
        db.execute(
            'CREATE TABLE IF NOT EXISTS notifications('
            'user_id INTEGER NOT NULL, key TEXT NOT NULL, '
            'PRIMARY KEY(user_id,key))'
        )
        for admin_id in {int(value) for value in admin_ids}:
            if admin_id == int(user_id):
                continue
            key = f'training_report_v1:{session["id"]}'
            inserted = db.execute(
                'INSERT OR IGNORE INTO notifications(user_id,key) VALUES(?,?)',
                (admin_id, key),
            ).rowcount
            if not inserted:
                continue
            db.execute(
                'INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)',
                (admin_id, admin_id, notice),
            )
            db.execute(
                'INSERT INTO outbox(user_id,chat_id,body) VALUES(?,?,?)',
                (admin_id, admin_id, f'__academy_pdf__:{session["id"]}:supervisor'),
            )
            queued += 1
    return queued


def _handle_lpr_gate(store, event):
    """One or two realistic discovery steps before the target LPR; never loop indefinitely."""
    if event.get('kind') != 'text':
        return False
    text = event.get('text', '').strip()
    if _is_control_text(text):
        return False
    s = store.current(event['user_id'])
    if s.get('phase') != 'active' or not s.get('card') or s.get('lpr_gate_passed') or _direct_lpr_known(s):
        return False
    if not text:
        return False

    identity = s['card'].get('identity', {})
    target = identity.get('job_title') or s['fields'].get('customer') or 'ответственный сотрудник'
    name = _first_name(identity.get('name', ''))
    connect = (f'Да, этим занимается {name}, {target}. Сейчас соединю.' if name
               else f'Да, этим занимается {target}. Сейчас соединю.')
    stage = s.get('lpr_gate_turns', 0)

    if _lpr_search_attempt(text):
        reply = connect
        s['lpr_gate_passed'] = True
    elif stage == 0:
        reply = 'Добрый день. Подскажите, по какому вопросу?'
        s['lpr_gate_turns'] = 1
    elif len(text.split()) >= 3 or stage >= 2:
        reply = connect
        s['lpr_gate_passed'] = True
    else:
        reply = 'Уточните, пожалуйста, вопрос чуть конкретнее — я подскажу, кто за это отвечает.'
        s['lpr_gate_turns'] = 2

    s['history'] += [dict(role='user', content=text), dict(role='assistant', content=reply)]
    s['state']['turns'] = s['state'].get('turns', 0) + 1
    store.commit(event, s, [reply], counted=True)
    return True


def worker(store, engine, ai, bot, stop, send):
    next_delivery = 0
    while not stop.is_set():
        if time.monotonic() >= next_delivery:
            for user in store.pending_users():
                try:
                    deliver(store, user, send)
                except Exception as exc:
                    LOG.warning('delivery error user=%s kind=%s', user, type(exc).__name__)
            next_delivery = time.monotonic() + 10
        event = store.claim()
        if event is None:
            stop.wait(.3)
            continue
        try:
            deliver(store, event['user_id'], send)
        except Exception:
            store.defer(event)
            continue
        try:
            previous_phase = store.current(event['user_id']).get('phase')
            if event['kind'] == 'text' and normalize_command(event['text']) in ('/retry', 'повторить обработку'):
                failed = store.failed(event['user_id'])
                if failed and failed['attempts'] < 2:
                    event = store.retry_event(event, failed)
            try:
                bot.send_chat_action(event['chat_id'], 'typing')
            except Exception:
                pass
            stage = 'transcription' if event['kind'] == 'voice' else 'conversation'
            if event['kind'] == 'voice':
                info = bot.get_file(event['text'])
                raw = bot.download_file(info.file_path)
                if len(raw) > 10 * 1024 * 1024:
                    raise ValueError('Audio too large')
                audio = io.BytesIO(raw)
                audio.name = 'voice.ogg'
                transcript = ai.client.audio.transcriptions.create(model=ai.transcribe_model, file=audio, language='ru')
                text = transcript.text.strip()
                if not text or len(text) > 5000:
                    raise ValueError('Transcription empty or too long')
                store.cache_text(event['id'], text)
                event.update(kind='text', text=text)
                stage = 'conversation'
            if not _handle_lpr_gate(store, event):
                engine.handle(event)
            completed = store.current(event['user_id'])
            if previous_phase != 'completed' and completed.get('phase') == 'completed':
                queue_training_reports(store, event['user_id'], completed, engine.admin_ids)
        except Exception as exc:
            log_failure(event, store.current(event['user_id']), locals().get('stage', 'transport'), exc)
            store.fail(event, type(exc).__name__)
        try:
            deliver(store, event['user_id'], send)
        except Exception:
            pass


def main():
    import faulthandler
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', force=True)
    LOG.info('Startup: entering application')
    faulthandler.dump_traceback_later(60, repeat=False)
    import fcntl
    import telebot
    from openai import OpenAI
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not os.getenv('OPENAI_API_KEY'):
        raise RuntimeError('OPENAI_API_KEY is not set')
    if not (os.getenv('TELEGRAM_TOKEN') or os.getenv('LEGACY_BOT_TOKEN')):
        raise RuntimeError('TELEGRAM_TOKEN or LEGACY_BOT_TOKEN is not set')
    path = os.getenv('DB_PATH', './data/academy.sqlite3')
    mount = os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
    if os.getenv('RAILWAY_ENVIRONMENT_ID'):
        if not mount or not Path(path).resolve().is_relative_to(Path(mount).resolve()):
            raise RuntimeError('Attach a Railway volume and set DB_PATH inside its mount path')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lock = open(path + '.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('Another worker is using this database') from None
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], timeout=90, max_retries=1)
    model = os.getenv('OPENAI_MODEL', 'gpt-5.6-luna')
    ai = AI(client, model, os.getenv('OPENAI_TRANSCRIBE_MODEL', 'gpt-4o-mini-transcribe'),
            os.getenv('OPENAI_EVAL_MODEL', model))
    LOG.info('Startup: opening persistent database at %s', path)
    store = Store(path)
    store.recover()
    with store.db() as db:
        persisted = db.execute('SELECT COUNT(*), COALESCE(SUM(counted),0) FROM sessions').fetchone()
        pending = db.execute("SELECT COUNT(*) FROM inbox WHERE status IN ('queued','working','waiting')").fetchone()[0]
    LOG.info('Startup: database ready sessions=%s counted_trainings=%s pending_events=%s',
             persisted[0], persisted[1], pending)
    bot, identity, webhook, token_source = connect_telegram(telebot, (
        ('TELEGRAM_TOKEN', os.getenv('TELEGRAM_TOKEN')),
        ('LEGACY_BOT_TOKEN', os.getenv('LEGACY_BOT_TOKEN')),
    ))
    LOG.info('Startup: Telegram bot=@%s token_source=%s webhook_active=%s pending_updates=%s previous_allowed_updates=%s',
             identity.username, token_source, bool(webhook.url), webhook.pending_update_count,
             getattr(webhook, 'allowed_updates', None))
    admins = [int(v.strip()) for v in os.getenv('ADMIN_IDS', '').split(',') if v.strip()]
    engine = Engine(store, ai, limit=int(os.getenv('FREE_TRAININGS', '3')), admin_ids=admins)

    # UI context is deliberately independent from the training session phase.
    # This prevents a completed training keyboard from leaking into Academy screens.
    ui_context = {}

    def current_context(user_id):
        return ui_context.get(int(user_id), CONTEXT_TRAINING)

    def set_context(user_id, context):
        ui_context[int(user_id)] = context
        return context

    def _reply_markup(rows):
        if not rows:
            return telebot.types.ReplyKeyboardRemove()
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        for row in rows:
            markup.row(*[telebot.types.KeyboardButton(value) for value in row])
        return markup

    def _telegram_profile(user):
        return {
            'username': getattr(user, 'username', None),
            'first_name': getattr(user, 'first_name', None),
            'last_name': getattr(user, 'last_name', None),
        }

    def _sales_main_markup():
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            telebot.types.InlineKeyboardButton('👤 Продолжить для себя', callback_data='sales:self'),
            telebot.types.InlineKeyboardButton('👥 Подключить для компании', callback_data='sales:company'),
        )
        return markup

    def _individual_markup():
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        plan_10 = f'10 тренировок — {commercial_cta.INDIVIDUAL_10_PRICE:,} ₽'.replace(',', ' ')
        plan_month = f'Безлимит на 30 дней — {commercial_cta.INDIVIDUAL_MONTH_PRICE:,} ₽'.replace(',', ' ')
        url_10 = commercial_cta.payment_url('10')
        url_month = commercial_cta.payment_url('month')
        markup.add(telebot.types.InlineKeyboardButton(
            plan_10, url=url_10) if url_10 else telebot.types.InlineKeyboardButton(
            plan_10, callback_data='sales:plan:10'))
        markup.add(telebot.types.InlineKeyboardButton(
            plan_month, url=url_month) if url_month else telebot.types.InlineKeyboardButton(
            plan_month, callback_data='sales:plan:month'))
        markup.add(telebot.types.InlineKeyboardButton('← Назад', callback_data='sales:back'))
        return markup

    def _company_question_markup(state):
        question = state['question']
        options = question.get('options') or ()
        if not options:
            return None
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for label, value in options:
            markup.add(telebot.types.InlineKeyboardButton(
                label, callback_data=f"sales:answer:{state['step']}:{value}"
            ))
        markup.add(telebot.types.InlineKeyboardButton('Отменить', callback_data='sales:cancel'))
        return markup

    def _send_company_question(chat_id, state, edit_message=None):
        question = state['question']
        prefix = f"Заявка для компании · шаг {state['step'] + 1} из {len(commercial_cta.COMPANY_QUESTIONS)}\n\n"
        text = prefix + question['prompt']
        markup = _company_question_markup(state)
        if edit_message is not None:
            try:
                bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)
                return
            except Exception:
                pass
        bot.send_message(chat_id, text, reply_markup=markup)

    def _training_summary(user_id):
        rows = []
        for item in store.recent(user_id):
            if item.get('phase') != 'completed':
                continue
            score = total_score(item.get('report_data'))
            if score is None:
                continue
            focus = FOCUS_OBJECTIONS.get(item.get('training_focus'), {}).get('label', 'Общий разговор')
            rows.append(f"• #{item.get('id')}: {score}/100 · {focus}")
            if len(rows) >= 3:
                break
        attempts = store.attempts(user_id)
        head = f'Завершено бесплатных тренировок: {min(attempts, engine.limit)} из {engine.limit}'
        return '\n'.join([head] + rows)

    def _notify_sales_admins(text):
        for admin_id in set(admins):
            try:
                bot.send_message(admin_id, text)
            except Exception:
                LOG.exception('Could not deliver sales lead admin=%s', admin_id)

    def context_rows(user_id, context=None):
        context = context or current_context(user_id)
        role = get_role(path, user_id)
        is_admin = int(user_id) in admins
        try:
            session = store.current(user_id)
            phase = session.get('phase')
        except Exception:
            session = None
            phase = None
        if context == CONTEXT_TRAINING:
            rows = keyboard_rows(session or {'phase': 'setup'}, bool(store.failed(user_id)), False)
            if role in {AKENSO, SUPERVISOR} and phase not in ('active', 'closed'):
                rows.append(['К Академии'])
            return rows
        return rows_for_context(role, context, phase=phase, is_admin=is_admin)

    def activate_context(chat_id, context, label=None):
        previous = current_context(chat_id)
        set_context(chat_id, context)
        if label is not None or previous != context:
            text = label or 'Раздел открыт.'
            bot.send_message(chat_id, text, reply_markup=_reply_markup(context_rows(chat_id, context)))

    def load_admin_session(session_id):
        with store.db() as db:
            row = db.execute('SELECT id,user_id,payload,counted FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            return None, None
        try:
            return row, upgrade_session(json.loads(row['payload']))
        except Exception:
            return row, None

    def session_score(session):
        if not session or not session.get('report_data'):
            return None
        from academy.reporting import total_score
        return total_score(session['report_data'])

    def admin_sessions_page(page=0, page_size=8):
        page = max(0, int(page))
        with store.db() as db:
            total = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
            rows = db.execute(
                'SELECT id,user_id,payload,counted FROM sessions ORDER BY id DESC LIMIT ? OFFSET ?',
                (page_size, page * page_size),
            ).fetchall()
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            try:
                s = upgrade_session(json.loads(row['payload']))
                employee = s.get('employee', {}).get('name') or f"TG {row['user_id']}"
                score = session_score(s)
                suffix = f'{score}/100' if score is not None else s.get('phase', '')
                label = f"#{row['id']} · {employee[:24]} · {suffix}"
            except Exception:
                label = f"#{row['id']} · TG {row['user_id']} · ошибка чтения"
            markup.add(telebot.types.InlineKeyboardButton(label, callback_data=f"adm:session:{row['id']}:{page}"))
        nav = []
        if page > 0:
            nav.append(telebot.types.InlineKeyboardButton('◀️ Назад', callback_data=f'adm:sessions:{page - 1}'))
        if (page + 1) * page_size < total:
            nav.append(telebot.types.InlineKeyboardButton('Вперёд ▶️', callback_data=f'adm:sessions:{page + 1}'))
        if nav:
            markup.row(*nav)
        start = page * page_size + 1 if total else 0
        end = min((page + 1) * page_size, total)
        text = f'СЕССИИ ПОЛЬЗОВАТЕЛЕЙ\nПоказаны {start}–{end} из {total}. Нажмите на сессию, чтобы открыть её.'
        return text, markup

    def admin_session_card(session_id):
        row, s = load_admin_session(session_id)
        if not row:
            return 'Сессия не найдена.'
        if not s:
            return f"Сессия #{session_id}\nНе удалось прочитать сохранённые данные."
        fields = s.get('fields', {})
        employee = s.get('employee', {}).get('name') or 'ФИО не указано'
        score = session_score(s)
        identity = (s.get('card') or {}).get('identity', {})
        focus = s.get('training_focus') or 'не указан'
        lines = [
            f'СЕССИЯ #{session_id}',
            f"Пользователь: {employee}",
            f"Telegram ID: {row['user_id']}",
            f"Статус: {s.get('phase', 'неизвестно')}",
            f"Итог: {score}/100" if score is not None else 'Итог: разбор ещё не сформирован',
            f"Навык/фокус: {focus}",
            f"Клиент/сценарий: {fields.get('customer') or 'не указан'}",
            f"Продукт: {fields.get('product') or 'не указан'}",
            f"Цель: {fields.get('goal') or 'не указана'}",
            f"Сложность: {fields.get('difficulty') or 'не указана'}",
        ]
        if identity:
            client_bits = [identity.get('name'), identity.get('job_title'), identity.get('company')]
            client_bits = [str(v).strip() for v in client_bits if str(v or '').strip()]
            if client_bits:
                lines.append('Карточка клиента: ' + ' · '.join(client_bits))
        lines.append(f"Реплик в диалоге: {len(s.get('history') or [])}")
        return '\n'.join(lines)

    def admin_session_markup(session_id, page=0):
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        markup.row(
            telebot.types.InlineKeyboardButton('💬 Показать диалог', callback_data=f'adm:dialog:{session_id}:{page}'),
            telebot.types.InlineKeyboardButton('📄 Отчёт PDF', callback_data=f'adm:pdf:{session_id}:{page}'),
        )
        markup.row(telebot.types.InlineKeyboardButton('← К списку сессий', callback_data=f'adm:sessions:{page}'))
        return markup

    def admin_dialogue_text(session_id):
        row, s = load_admin_session(session_id)
        if not row:
            return ['Сессия не найдена.']
        if not s:
            return [f'Сессия #{session_id}: не удалось прочитать данные.']
        history = s.get('history') or []
        if not history:
            return [f'Сессия #{session_id}: диалог пуст.']
        lines = [f'DИАЛОГ СЕССИИ #{session_id}'.replace('D', 'Д')]
        for index, item in enumerate(history, 1):
            role = item.get('role')
            if role == 'user':
                speaker = '👤 Менеджер'
            elif role == 'assistant':
                speaker = '🤖 Клиент'
            else:
                continue
            content = str(item.get('content', '')).strip()
            if content:
                lines.append(f'{index}. {speaker}: {content}')
        return chunks('\n\n'.join(lines), limit=3800)

    def send_admin_sessions(chat_id, page=0, edit_message=None):
        text, markup = admin_sessions_page(page)
        if edit_message is not None:
            try:
                bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)
                return
            except Exception:
                pass
        bot.send_message(chat_id, text, reply_markup=markup)

    def latest_user_name(user_id):
        with store.db() as db:
            row = db.execute('SELECT payload FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1', (int(user_id),)).fetchone()
        if not row:
            return f'TG {user_id}'
        try:
            s = upgrade_session(json.loads(row['payload']))
            return s.get('employee', {}).get('name') or f'TG {user_id}'
        except Exception:
            return f'TG {user_id}'

    def admin_access_page():
        with store.db() as db:
            rows = db.execute('''
                SELECT user_id, MAX(id) AS last_id
                FROM sessions
                GROUP BY user_id
                ORDER BY last_id DESC
                LIMIT 30
            ''').fetchall()
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            user_id = int(row['user_id'])
            role = get_role(path, user_id)
            name = latest_user_name(user_id)
            markup.add(telebot.types.InlineKeyboardButton(
                f"{name[:24]} · {ROLE_LABELS[role]}", callback_data=f'acc:user:{user_id}'
            ))
        text = ('ДОСТУП СОТРУДНИКОВ\n\n'
                'Публичный — только тестовый тренажёр.\n'
                'АКЕНСО — обучение, аттестация и тренажёр.\n'
                'Руководитель — корпоративный доступ и прогресс команды.\n\n'
                'Выберите пользователя:')
        return text, markup

    def access_user_card(user_id):
        role = get_role(path, user_id)
        name = latest_user_name(user_id)
        text = f'ДОСТУП ПОЛЬЗОВАТЕЛЯ\n\n{name}\nTelegram ID: {user_id}\nТекущая роль: {ROLE_LABELS[role]}'
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(telebot.types.InlineKeyboardButton('Публичный', callback_data=f'acc:set:{user_id}:{PUBLIC}'))
        markup.add(telebot.types.InlineKeyboardButton('Сотрудник АКЕНСО', callback_data=f'acc:set:{user_id}:{AKENSO}'))
        markup.add(telebot.types.InlineKeyboardButton('Руководитель', callback_data=f'acc:set:{user_id}:{SUPERVISOR}'))
        markup.add(telebot.types.InlineKeyboardButton('← К списку', callback_data='acc:list'))
        return text, markup

    def send_admin_access(chat_id, edit_message=None):
        text, markup = admin_access_page()
        if edit_message is not None:
            try:
                bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)
                return
            except Exception:
                pass
        bot.send_message(chat_id, text, reply_markup=markup)

    def send_access_user_card(chat_id, user_id, edit_message=None):
        ensure_access_schema(path)
        text, markup = access_user_card(user_id)
        if edit_message is not None:
            try:
                bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)
                return
            except Exception:
                LOG.exception('Could not edit access user card; sending a new one')
        bot.send_message(chat_id, text, reply_markup=markup)

    def team_progress_page(chat_id, edit_message=None):
        ensure_access_schema(path)
        with store.db() as db:
            rows = db.execute("""
                SELECT user_id,role FROM user_access
                WHERE role IN (?,?)
                ORDER BY updated_at DESC
            """, (AKENSO, SUPERVISOR)).fetchall()
        lines = ['ПРОГРЕСС КОМАНДЫ', '']
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        if not rows:
            lines.append('Пока нет сотрудников с корпоративным доступом.')
        for row in rows:
            user_id = int(row['user_id'])
            name = latest_user_name(user_id)
            result = admission.assess(path, user_id)
            if result['complete']:
                status = f"{result['grade']:.1f}/10 · {result['decision']}"
            elif result['scores']:
                status = f"{len(result['scores'])}/3 теста"
            else:
                status = 'не начал'
            lines.append(f"• {name}: {status}")
            markup.add(telebot.types.InlineKeyboardButton(
                f"{name[:24]} · {status[:32]}", callback_data=f"team:user:{user_id}"
            ))
        markup.add(telebot.types.InlineKeyboardButton('↻ Обновить', callback_data='team:list'))
        text = '\n'.join(lines)
        if edit_message is None:
            bot.send_message(chat_id, text, reply_markup=markup)
        else:
            bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)

    def team_member_card(chat_id, user_id, edit_message):
        result = admission.assess(path, user_id)
        name = latest_user_name(user_id)
        text = admission.supervisor_text(name, result)
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(telebot.types.InlineKeyboardButton('📨 Отправить сотруднику маршрут', callback_data=f'team:send:{user_id}'))
        markup.add(telebot.types.InlineKeyboardButton('← К прогрессу команды', callback_data='team:list'))
        bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)

    def send_employee_route(user_id):
        result = admission.assess(path, user_id)
        text = ('ВАШ УЧЕБНЫЙ МАРШРУТ\n\n' + admission.employee_text(result) +
                '\n\nСледующий шаг: откройте «Аттестация» или перейдите к тренировке.')
        bot.send_message(user_id, text)

    def learning_message(chat_id, text, markup, edit_message=None):
        """Keep navigation usable if Telegram cannot edit an older menu."""
        parts = chunks(text, limit=3800)
        if edit_message is not None and len(parts) == 1:
            try:
                return bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)
            except Exception as exc:
                LOG.warning('Learning edit failed; sending new message kind=%s', type(exc).__name__)
        for index, part in enumerate(parts):
            bot.send_message(chat_id, part, reply_markup=markup if index == len(parts) - 1 else None)

    def learning_home(chat_id, section='knowledge', edit_message=None):
        snapshot = {item['id']: item for item in progress_snapshot(path, chat_id)}
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        if section == 'knowledge':
            header = 'АКАДЕМИЯ АКЕНСО'
            lines = [header, '', 'Ваш учебный маршрут. Выберите раздел:']
        else:
            header = 'АТТЕСТАЦИЯ АКЕНСО'
            lines = [header, '', 'Выберите раздел для короткой проверки знаний:']
        for module_id, item in MODULE_CONTENT.items():
            latest = snapshot[module_id].get('latest_assessment')
            status = 'не начат'
            if latest:
                status = f"результат: {latest['score']}%"
            elif snapshot[module_id]['status'] == 'in_progress':
                status = 'в процессе'
            prefix = '📘' if section == 'knowledge' else '📝'
            action = 'read' if section == 'knowledge' else 'start'
            markup.add(telebot.types.InlineKeyboardButton(
                f"{prefix} {item['title']} · {status}", callback_data=f"learn:{action}:{module_id}"
            ))
        if section == 'assessment':
            markup.add(telebot.types.InlineKeyboardButton('📊 Итоговый допуск', callback_data='learn:admission'))
            lines += ['', 'Проходной результат каждого теста — 80%. Итоговый допуск — от 7,0 по шкале 0–10.']
        else:
            lines += ['', 'Можно остановиться после любого раздела и вернуться к обучению позже.']
        learning_message(chat_id, '\n'.join(lines), markup, edit_message)

    def learning_module(chat_id, module_id, edit_message=None):
        item = MODULE_CONTENT[module_id]
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(telebot.types.InlineKeyboardButton('📝 Пройти аттестацию', callback_data=f'learn:start:{module_id}'))
        markup.add(telebot.types.InlineKeyboardButton('← К разделам', callback_data='learn:home:knowledge'))
        learning_message(chat_id, item['body'], markup, edit_message)

    def learning_admission(chat_id, edit_message=None):
        result = admission.assess(path, chat_id)
        text = admission.employee_text(result)
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(telebot.types.InlineKeyboardButton('← К аттестациям', callback_data='learn:home:assessment'))
        learning_message(chat_id, text, markup, edit_message)
        return result

    def own_progress_text(user_id):
        rows = progress_snapshot(path, user_id)
        labels = {'not_started': 'не начат', 'in_progress': 'в процессе', 'completed': 'пройден'}
        lines = ['МОЙ ПРОГРЕСС', '']
        for item in rows:
            latest = item.get('latest_assessment')
            suffix = labels.get(item.get('status'), item.get('status', 'не начат'))
            if latest:
                suffix += f" · тест {latest['score']}%"
            lines.append(f"• {item['title']}: {suffix}")
        return '\n'.join(lines)

    def notify_supervisors(user_id, result):
        if not result['complete']:
            return
        employee_name = latest_user_name(user_id)
        text = admission.supervisor_text(employee_name, result)
        supervisor_ids = set(admins)
        try:
            with store.db() as db:
                for row in db.execute('SELECT user_id FROM user_access WHERE role=?', (SUPERVISOR,)).fetchall():
                    supervisor_ids.add(int(row['user_id']))
        except Exception:
            pass
        for supervisor_id in supervisor_ids:
            if int(supervisor_id) == int(user_id):
                continue
            try:
                bot.send_message(supervisor_id, text)
            except Exception:
                LOG.exception('Could not deliver admission report supervisor=%s user=%s', supervisor_id, user_id)

    def assessment_question(chat_id, question, edit_message):
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for index, option in enumerate(question['options']):
            markup.add(telebot.types.InlineKeyboardButton(
                option, callback_data=f"learn:answer:{question['module_id']}:{question['index']}:{index}"
            ))
        text = (f"АТТЕСТАЦИЯ · {MODULE_CONTENT[question['module_id']]['title']}\n"
                f"Вопрос {question['index'] + 1} из {question['total']}\n\n{question['question']}")
        learning_message(chat_id, text, markup, edit_message)

    def send(chat_id, text):
        if str(text).startswith('__academy_pdf__:'):
            deliver_pdf(bot, store, chat_id, text, admins)
            return
        if commercial_cta.is_cta_text(text):
            bot.send_message(chat_id, str(text), reply_markup=_sales_main_markup())
            return
        user_id = chat_id
        try:
            if str(text).startswith(('Завершаю тренировку.', 'Разбор уже формируется.')):
                markup = telebot.types.ReplyKeyboardRemove()
            else:
                markup = _reply_markup(context_rows(user_id))
        except Exception:
            markup = None
        for part in chunks(str(text)):
            bot.send_message(chat_id, part, reply_markup=markup)

    stop = threading.Event()
    thread = threading.Thread(target=worker, args=(store, engine, ai, bot, stop, send), daemon=True)
    thread.start()

    @bot.message_handler(content_types=['text'])
    def on_text(message):
        chat_id = message.chat.id
        cmd = normalize_command(message.text or '')
        role = get_role(path, chat_id)

        funnel_state = commercial_cta.company_state(path, chat_id)
        if funnel_state:
            if cmd in ('/cancel', 'отмена'):
                commercial_cta.cancel_company(path, chat_id)
                bot.send_message(chat_id, 'Заявка отменена.', reply_markup=_sales_main_markup())
                return
            if funnel_state['question'].get('options'):
                _send_company_question(chat_id, funnel_state)
                return
            try:
                result = commercial_cta.answer_company(path, chat_id, message.text or '')
            except ValueError:
                bot.send_message(chat_id, 'Пожалуйста, напишите ответ чуть подробнее.')
                return
            if result['finished']:
                lead_text = commercial_cta.company_lead_text(
                    result['lead_id'], chat_id, result['data'], _training_summary(chat_id)
                )
                _notify_sales_admins(lead_text)
                bot.send_message(
                    chat_id,
                    'Спасибо! Заявка получена.\n\n'
                    'Специалист Академии свяжется с вами, уточнит задачи и покажет, '
                    'как настроить систему под вашу команду.',
                )
            else:
                _send_company_question(chat_id, result['state'])
            return

        if cmd in ('/sessions', 'сессии пользователей', 'технические сессии'):
            if chat_id in admins:
                set_context(chat_id, CONTEXT_MANAGEMENT)
                send_admin_sessions(chat_id, 0)
            else:
                send(chat_id, 'Сессии других пользователей доступны только администратору системы.')
            return
        if cmd in ('/access', 'доступ сотрудников', 'управление доступом'):
            if chat_id in admins:
                set_context(chat_id, CONTEXT_MANAGEMENT)
                send_admin_access(chat_id)
            else:
                send(chat_id, 'Управление доступом доступно только администратору системы.')
            return
        if cmd in ('/akenso', 'выдать себе доступ акенсо'):
            if chat_id not in admins:
                send(chat_id, 'Эта команда доступна только администратору.')
                return
            set_role(path, chat_id, SUPERVISOR)
            activate_context(chat_id, CONTEXT_HOME, 'Корпоративный доступ руководителя АКЕНСО включён.')
            return
        if cmd in ('/team', 'прогресс команды', 'команда'):
            if role == SUPERVISOR or chat_id in admins:
                activate_context(chat_id, CONTEXT_MANAGEMENT, '👥 Раздел руководителя')
                team_progress_page(chat_id)
            else:
                send(chat_id, 'Прогресс команды доступен только руководителю.')
            return
        if cmd in ('база знаний', 'продолжить обучение'):
            if has_company_access(path, chat_id):
                activate_context(chat_id, CONTEXT_LEARNING, '📚 Обучение')
                learning_home(chat_id, 'knowledge')
            else:
                send(chat_id, 'Обучение доступно только сотрудникам подключённой компании.')
            return
        if cmd == 'аттестация':
            if has_company_access(path, chat_id):
                activate_context(chat_id, CONTEXT_ASSESSMENT, '🎓 Аттестация')
                learning_home(chat_id, 'assessment')
            else:
                send(chat_id, 'Аттестация доступна только сотрудникам подключённой компании.')
            return
        if cmd == 'мой прогресс':
            if has_company_access(path, chat_id):
                bot.send_message(chat_id, own_progress_text(chat_id), reply_markup=_reply_markup(context_rows(chat_id)))
            else:
                send(chat_id, 'Прогресс обучения доступен сотрудникам подключённой компании.')
            return
        if cmd == 'к академии':
            if has_company_access(path, chat_id):
                activate_context(chat_id, CONTEXT_HOME, '🏠 Академия АКЕНСО')
            else:
                set_context(chat_id, CONTEXT_TRAINING)
                send(chat_id, 'Открываю тренажёр.')
            return
        if cmd == 'тренировка':
            set_context(chat_id, CONTEXT_TRAINING)
            send(chat_id, '🎭 Режим тренировки. Выберите действие ниже.')
            return
        module_commands = {
            'продукт': 'product',
            'техники продаж': 'sales',
            'регламенты': 'regulations',
        }
        if cmd in module_commands:
            if has_company_access(path, chat_id):
                activate_context(chat_id, CONTEXT_LEARNING)
                learning_module(chat_id, module_commands[cmd])
            else:
                send(chat_id, 'Обучение доступно только сотрудникам подключённой компании.')
            return

        # Text entered while browsing Academy must never accidentally become a manager
        # replica in the sales simulation. The user explicitly enters Training first.
        if has_company_access(path, chat_id) and current_context(chat_id) != CONTEXT_TRAINING:
            bot.send_message(
                chat_id,
                'Вы сейчас в Академии. Выберите действие кнопкой ниже или перейдите в «Тренировка».',
                reply_markup=_reply_markup(context_rows(chat_id)),
            )
            return

        receive_text(store, f'tg:{chat_id}:{message.message_id}', chat_id, chat_id,
                     'text', message.text or '', send)

    @bot.callback_query_handler(func=lambda call: str(call.data or '').startswith('sales:'))
    def on_sales_callback(call):
        chat_id = call.message.chat.id
        try:
            parts = str(call.data).split(':')
            action = parts[1]
            if action == 'self':
                bot.edit_message_text(
                    commercial_cta.individual_text(),
                    chat_id,
                    call.message.message_id,
                    reply_markup=_individual_markup(),
                )
                bot.answer_callback_query(call.id)
                return
            if action == 'company':
                state = commercial_cta.start_company(
                    path, chat_id, _telegram_profile(call.from_user)
                )
                _send_company_question(chat_id, state, call.message.message_id)
                bot.answer_callback_query(call.id)
                return
            if action == 'back':
                commercial_cta.cancel_company(path, chat_id)
                bot.edit_message_text(
                    commercial_cta.cta_text(),
                    chat_id,
                    call.message.message_id,
                    reply_markup=_sales_main_markup(),
                )
                bot.answer_callback_query(call.id)
                return
            if action == 'cancel':
                commercial_cta.cancel_company(path, chat_id)
                bot.edit_message_text(
                    commercial_cta.cta_text(),
                    chat_id,
                    call.message.message_id,
                    reply_markup=_sales_main_markup(),
                )
                bot.answer_callback_query(call.id, 'Заявка отменена.')
                return
            if action == 'plan':
                plan = parts[2]
                lead_id = commercial_cta.record_individual_interest(
                    path, chat_id, plan, _telegram_profile(call.from_user)
                )
                _notify_sales_admins(commercial_cta.individual_lead_text(
                    lead_id, chat_id, plan, _telegram_profile(call.from_user)
                ))
                label = (
                    f'{commercial_cta.INDIVIDUAL_10_PRICE:,} ₽ за 10 тренировок'
                    if plan == '10'
                    else f'{commercial_cta.INDIVIDUAL_MONTH_PRICE:,} ₽ за 30 дней безлимита'
                ).replace(',', ' ')
                bot.answer_callback_query(call.id, 'Тариф выбран.')
                bot.send_message(
                    chat_id,
                    f'Вы выбрали тариф: {label}.\n\n'
                    'Заявка на подключение отправлена. Специалист Академии свяжется с вами '
                    'по Telegram. После подключения платёжной ссылки эта кнопка будет вести '
                    'сразу на оплату.',
                )
                return
            if action == 'answer':
                state = commercial_cta.company_state(path, chat_id)
                if not state or str(state['step']) != parts[2]:
                    bot.answer_callback_query(call.id, 'Этот шаг уже обновлён.', show_alert=True)
                    return
                result = commercial_cta.answer_company(path, chat_id, parts[3])
                if result['finished']:
                    _notify_sales_admins(commercial_cta.company_lead_text(
                        result['lead_id'], chat_id, result['data'], _training_summary(chat_id)
                    ))
                    bot.edit_message_text(
                        'Спасибо! Заявка получена.\n\n'
                        'Специалист Академии свяжется с вами, уточнит задачи и покажет, '
                        'как настроить систему под вашу команду.',
                        chat_id,
                        call.message.message_id,
                    )
                else:
                    _send_company_question(chat_id, result['state'], call.message.message_id)
                bot.answer_callback_query(call.id)
                return
            bot.answer_callback_query(call.id, 'Неизвестная команда.', show_alert=True)
        except Exception:
            LOG.exception('Sales funnel callback failed')
            try:
                bot.answer_callback_query(call.id, 'Не удалось обработать действие. Попробуйте ещё раз.', show_alert=True)
            except Exception:
                pass

    @bot.callback_query_handler(func=lambda call: str(call.data or '').startswith('team:'))
    def on_team_callback(call):
        chat_id = call.message.chat.id
        if get_role(path, chat_id) != SUPERVISOR and chat_id not in admins:
            bot.answer_callback_query(call.id, 'Доступно только руководителю.', show_alert=True)
            return
        try:
            set_context(chat_id, CONTEXT_MANAGEMENT)
            parts = str(call.data).split(':')
            action = parts[1]
            if action == 'list':
                team_progress_page(chat_id, call.message.message_id)
            elif action == 'user':
                team_member_card(chat_id, int(parts[2]), call.message.message_id)
            elif action == 'send':
                send_employee_route(int(parts[2]))
                bot.answer_callback_query(call.id, 'Маршрут отправлен сотруднику.')
                return
            else:
                bot.answer_callback_query(call.id, 'Неизвестная команда.', show_alert=True)
                return
            bot.answer_callback_query(call.id)
        except Exception:
            LOG.exception('Team progress callback failed')
            try:
                bot.answer_callback_query(call.id, 'Не удалось открыть прогресс.', show_alert=True)
            except Exception:
                pass

    @bot.callback_query_handler(func=lambda call: str(call.data or '').startswith('learn:'))
    def on_learning_callback(call):
        chat_id = call.message.chat.id
        if not has_company_access(path, chat_id):
            bot.answer_callback_query(call.id, 'Раздел доступен только сотрудникам компании.', show_alert=True)
            return
        try:
            parts = str(call.data).split(':')
            action = parts[1]
            if action in ('start', 'answer', 'admission') or (action == 'home' and len(parts) > 2 and parts[2] == 'assessment'):
                if current_context(chat_id) != CONTEXT_ASSESSMENT:
                    activate_context(chat_id, CONTEXT_ASSESSMENT, '🎓 Аттестация')
                else:
                    set_context(chat_id, CONTEXT_ASSESSMENT)
            else:
                if current_context(chat_id) != CONTEXT_LEARNING:
                    activate_context(chat_id, CONTEXT_LEARNING, '📚 Обучение')
                else:
                    set_context(chat_id, CONTEXT_LEARNING)

            if action == 'home':
                learning_home(chat_id, parts[2], call.message.message_id)
            elif action == 'read':
                learning_module(chat_id, parts[2], call.message.message_id)
            elif action == 'start':
                question = assessment.start(path, chat_id, parts[2])
                assessment_question(chat_id, question, call.message.message_id)
            elif action == 'admission':
                learning_admission(chat_id, call.message.message_id)
            elif action == 'answer':
                current = assessment.question(path, chat_id)
                if not current or current['module_id'] != parts[2] or str(current['index']) != parts[3]:
                    bot.answer_callback_query(call.id, 'Этот вопрос уже обновлён. Откройте аттестацию снова.', show_alert=True)
                    return
                result = assessment.answer(path, chat_id, int(parts[4]))
                if result['finished']:
                    verdict = 'пройдена' if result['passed'] else 'пока не пройдена'
                    text = (f"АТТЕСТАЦИЯ {verdict.upper()}\n\n"
                            f"Результат: {result['correct']} из {result['total']} · {result['score']}%\n"
                            f"Проходной порог: 80%.\n\n"
                            + ('Модуль отмечен как пройденный.' if result['passed'] else 'Повторите раздел и попробуйте ещё раз.'))
                    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
                    markup.add(telebot.types.InlineKeyboardButton('← К аттестациям', callback_data='learn:home:assessment'))
                    learning_message(chat_id, text, markup, call.message.message_id)
                    overall = admission.assess(path, chat_id)
                    if overall['complete']:
                        bot.send_message(chat_id, admission.employee_text(overall))
                        notify_supervisors(chat_id, overall)
                else:
                    assessment_question(chat_id, result['question'], call.message.message_id)
            else:
                bot.answer_callback_query(call.id, 'Неизвестная команда.', show_alert=True)
                return
            bot.answer_callback_query(call.id)
        except Exception:
            LOG.exception('Learning callback failed')
            try:
                bot.answer_callback_query(call.id, 'Не удалось открыть учебный раздел. Попробуйте ещё раз.', show_alert=True)
            except Exception:
                pass

    @bot.callback_query_handler(func=lambda call: str(call.data or '').startswith('adm:'))
    def on_admin_callback(call):
        chat_id = call.message.chat.id
        if chat_id not in admins:
            bot.answer_callback_query(call.id, 'Доступно только администратору.', show_alert=True)
            return
        try:
            set_context(chat_id, CONTEXT_MANAGEMENT)
            parts = str(call.data).split(':')
            action = parts[1]
            if action == 'sessions':
                page = int(parts[2]) if len(parts) > 2 else 0
                send_admin_sessions(chat_id, page, call.message.message_id)
                bot.answer_callback_query(call.id)
                return
            session_id = int(parts[2])
            page = int(parts[3]) if len(parts) > 3 else 0
            if action == 'session':
                bot.edit_message_text(
                    admin_session_card(session_id), chat_id, call.message.message_id,
                    reply_markup=admin_session_markup(session_id, page),
                )
                bot.answer_callback_query(call.id)
                return
            if action == 'dialog':
                bot.answer_callback_query(call.id)
                for part in admin_dialogue_text(session_id):
                    bot.send_message(chat_id, part)
                bot.send_message(chat_id, 'Действия с сессией:', reply_markup=admin_session_markup(session_id, page))
                return
            if action == 'pdf':
                bot.answer_callback_query(call.id, 'Готовлю отчёт…')
                deliver_pdf(bot, store, chat_id, f'__academy_pdf__:{session_id}:supervisor', admins)
                return
            bot.answer_callback_query(call.id, 'Неизвестная команда.')
        except Exception:
            LOG.exception('Admin session callback failed')
            try:
                bot.answer_callback_query(call.id, 'Не удалось открыть сессию.', show_alert=True)
            except Exception:
                pass

    @bot.callback_query_handler(func=lambda call: str(call.data or '').startswith('acc:'))
    def on_access_callback(call):
        chat_id = call.message.chat.id
        if chat_id not in admins:
            bot.answer_callback_query(call.id, 'Доступно только администратору.', show_alert=True)
            return
        try:
            set_context(chat_id, CONTEXT_MANAGEMENT)
            parts = str(call.data).split(':')
            action = parts[1]
            if action == 'list':
                send_admin_access(chat_id, call.message.message_id)
                bot.answer_callback_query(call.id)
                return
            if action == 'user':
                user_id = int(parts[2])
                bot.answer_callback_query(call.id, 'Открываю карточку…')
                send_access_user_card(chat_id, user_id, call.message.message_id)
                return
            if action == 'set':
                user_id = int(parts[2])
                role = parts[3]
                previous_role = get_role(path, user_id)
                set_role(path, user_id, role)
                send_access_user_card(chat_id, user_id, call.message.message_id)
                bot.answer_callback_query(call.id, f'Роль: {ROLE_LABELS[role]}')
                if role in {AKENSO, SUPERVISOR} and role != previous_role:
                    access_label = ('корпоративный доступ АКЕНСО' if role == AKENSO
                                    else 'доступ руководителя АКЕНСО')
                    notification = (
                        f'Вам подключён {access_label}.\n\n'
                        'Вы можете продолжать тренировки без ограничения. '
                        'Предыдущая история и разборы сохранены.\n\n'
                        'Нажмите «Новая тренировка» или «К Академии».'
                    )
                    try:
                        bot.send_message(
                            user_id,
                            notification,
                            reply_markup=_reply_markup(context_rows(user_id)),
                        )
                    except Exception:
                        LOG.exception('Could not deliver access notification user=%s role=%s', user_id, role)
                return
            bot.answer_callback_query(call.id, 'Неизвестная команда.')
        except Exception:
            LOG.exception('Access callback failed')
            try:
                bot.answer_callback_query(call.id, 'Не удалось изменить доступ.', show_alert=True)
            except Exception:
                pass

    @bot.message_handler(content_types=['voice'])
    def on_voice(message):
        chat_id = message.chat.id
        if has_company_access(path, chat_id) and current_context(chat_id) != CONTEXT_TRAINING:
            bot.send_message(
                chat_id,
                'Голосовое не отправлено в тренажёр: вы сейчас в Академии. Сначала откройте «Тренировка».',
                reply_markup=_reply_markup(context_rows(chat_id)),
            )
            return
        receive_text(store, f'tg:{chat_id}:{message.message_id}', chat_id, chat_id,
                     'voice', message.voice.file_id, send)

    try:
        LOG.info('Startup: polling allowed_updates=%s callback_handlers=%s',
                 TELEGRAM_ALLOWED_UPDATES, len(bot.callback_query_handlers))
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30,
                             allowed_updates=TELEGRAM_ALLOWED_UPDATES)
    finally:
        stop.set()
        thread.join(timeout=2)
        lock.close()


if __name__ == '__main__':
    main()

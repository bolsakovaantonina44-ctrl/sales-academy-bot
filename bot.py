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
from academy.access import PUBLIC, AKENSO, SUPERVISOR, get_role, set_role, has_company_access
from academy.curriculum import MODULE_CONTENT
from academy.learning import progress_snapshot
from academy import assessment
from academy import admission

LOG = logging.getLogger('academy')
FOCUS_LABELS = [v['label'] for v in FOCUS_OBJECTIONS.values()]
ROLE_LABELS = {PUBLIC: 'Публичный', AKENSO: 'АКЕНСО', SUPERVISOR: 'Руководитель'}

CONTROL_COMMANDS = {
    'начать тренировку', 'новая тренировка', 'завершить тренировку', 'заверши тренировку',
    'закончить тренировку', 'закончи тренировку', 'завершить тест', 'закончить тест',
    'повторить обработку', 'пропустить эту реплику', 'обновить разбор', 'посмотреть разбор',
    'скачать результат', 'сформировать отчет', 'отчет сотруднику', 'отчет руководителю',
    'показать скрытый сценарий', 'мои тренировки', 'сессии пользователей',
    'доступ сотрудников', 'база знаний', 'аттестация',
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
    phase = session['phase']
    if failed:
        rows = [['Повторить обработку', 'Пропустить эту реплику'], ['Завершить тренировку']]
        if admin:
            rows.append(['Сессии пользователей'])
        return rows
    if phase == 'active':
        rows = [['Завершить тренировку']]
        if admin:
            rows.append(['Сессии пользователей'])
        return rows
    if phase == 'closed':
        rows = [['Завершить тренировку', 'Новая тренировка']]
        if admin:
            rows.append(['Сессии пользователей'])
        return rows
    if phase == 'completed':
        rows = [['Посмотреть разбор', 'Скачать результат'], ['Новая тренировка', 'Мои тренировки']]
        if session.get('report_status') == 'technical_partial':
            rows.append(['Обновить разбор'])
        if admin:
            rows.append(['Отчёт руководителю', 'Показать скрытый сценарий'])
            rows.append(['Сессии пользователей'])
        return rows
    if phase == 'ready':
        rows = []
        if session.get('training_focus'):
            rows.append(['Начать тренировку'])
            rows.append(['1', '2', '3'])
        rows.extend(_focus_rows())
        rows.append(['Новая тренировка'])
        if admin:
            rows.append(['Сессии пользователей'])
        return rows
    # Product-specific demo scenarios remain internal fixtures, not public choices.
    rows = [['Мои тренировки']]
    if admin:
        rows.append(['Сессии пользователей'])
    return rows


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
    LOG.info('Startup: Telegram bot=@%s token_source=%s webhook_active=%s pending_updates=%s',
             identity.username, token_source, bool(webhook.url), webhook.pending_update_count)
    admins = [int(v.strip()) for v in os.getenv('ADMIN_IDS', '').split(',') if v.strip()]
    engine = Engine(store, ai, limit=int(os.getenv('FREE_TRAININGS', '3')), admin_ids=admins)

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
        lines = [f'ДИАЛОГ СЕССИИ #{session_id}']
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
                'АКЕНСО — база знаний, аттестация и тренажёр.\n'
                'Руководитель — корпоративный доступ руководителя.\n\n'
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

    def learning_home(chat_id, section='knowledge', edit_message=None):
        snapshot = {item['id']: item for item in progress_snapshot(path, chat_id)}
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        header = 'БАЗА ЗНАНИЙ АКЕНСО' if section == 'knowledge' else 'АТТЕСТАЦИЯ АКЕНСО'
        lines = [header, '', 'Выберите модуль:' if section == 'knowledge' else 'Выберите модуль для короткой проверки знаний:']
        for module_id, item in MODULE_CONTENT.items():
            latest = snapshot[module_id].get('latest_assessment')
            status = 'не начат'
            if latest:
                status = f"последняя попытка: {latest['score']}%"
            elif snapshot[module_id]['status'] == 'in_progress':
                status = 'в процессе'
            prefix = '📘' if section == 'knowledge' else '📝'
            action = 'read' if section == 'knowledge' else 'start'
            markup.add(telebot.types.InlineKeyboardButton(
                f"{prefix} {item['title']} · {status}", callback_data=f"learn:{action}:{module_id}"
            ))
        if section == 'assessment':
            markup.add(telebot.types.InlineKeyboardButton('📊 Итоговый допуск', callback_data='learn:admission'))
        lines += ['', 'Проходной результат каждого теста — 80%. Итоговый балл допуска формируется после трёх модулей по шкале 0–10; минимальный допуск — 7,0.']
        text = '\n'.join(lines)
        if edit_message is not None:
            bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)
        else:
            bot.send_message(chat_id, text, reply_markup=markup)

    def learning_module(chat_id, module_id, edit_message):
        item = MODULE_CONTENT[module_id]
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(telebot.types.InlineKeyboardButton('📝 Пройти аттестацию', callback_data=f'learn:start:{module_id}'))
        markup.add(telebot.types.InlineKeyboardButton('← К модулям', callback_data='learn:home:knowledge'))
        bot.edit_message_text(item['body'], chat_id, edit_message, reply_markup=markup)

    def learning_admission(chat_id, edit_message=None):
        result = admission.assess(path, chat_id)
        text = admission.employee_text(result)
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(telebot.types.InlineKeyboardButton('← К аттестациям', callback_data='learn:home:assessment'))
        if edit_message is not None:
            bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)
        else:
            bot.send_message(chat_id, text, reply_markup=markup)
        return result

    def notify_supervisors(user_id, result):
        if not result['complete']:
            return
        employee_name = latest_user_name(user_id)
        text = admission.supervisor_text(employee_name, result)
        for admin_id in admins:
            if int(admin_id) == int(user_id):
                continue
            try:
                bot.send_message(admin_id, text)
            except Exception:
                LOG.exception('Could not deliver admission report admin=%s user=%s', admin_id, user_id)

    def assessment_question(chat_id, question, edit_message):
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        for index, option in enumerate(question['options']):
            markup.add(telebot.types.InlineKeyboardButton(
                option, callback_data=f"learn:answer:{question['module_id']}:{question['index']}:{index}"
            ))
        text = (f"АТТЕСТАЦИЯ · {MODULE_CONTENT[question['module_id']]['title']}\n"
                f"Вопрос {question['index'] + 1} из {question['total']}\n\n{question['question']}")
        bot.edit_message_text(text, chat_id, edit_message, reply_markup=markup)

    def send(chat_id, text):
        if str(text).startswith('__academy_pdf__:'):
            deliver_pdf(bot, store, chat_id, text, admins)
            return
        user_id = chat_id
        try:
            if str(text).startswith(('Завершаю тренировку.', 'Разбор уже формируется.')):
                markup = telebot.types.ReplyKeyboardRemove()
            else:
                s = store.current(user_id)
                markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
                rows = keyboard_rows(s, bool(store.failed(user_id)), user_id in admins)
                if s.get('phase') not in ('active', 'closed') and has_company_access(path, user_id):
                    rows.append(['База знаний', 'Аттестация'])
                if s.get('phase') not in ('active', 'closed') and user_id in admins:
                    rows.append(['Доступ сотрудников'])
                for row in rows:
                    markup.row(*[telebot.types.KeyboardButton(v) for v in row])
        except Exception:
            markup = None
        for part in chunks(str(text)):
            bot.send_message(chat_id, part, reply_markup=markup)

    stop = threading.Event()
    thread = threading.Thread(target=worker, args=(store, engine, ai, bot, stop, send), daemon=True)
    thread.start()

    @bot.message_handler(content_types=['text'])
    def on_text(message):
        cmd = normalize_command(message.text or '')
        if cmd in ('/sessions', 'сессии пользователей'):
            if message.chat.id in admins:
                send_admin_sessions(message.chat.id, 0)
            else:
                send(message.chat.id, 'Сессии пользователей доступны только администратору.')
            return
        if cmd in ('/access', 'доступ сотрудников'):
            if message.chat.id in admins:
                send_admin_access(message.chat.id)
            else:
                send(message.chat.id, 'Управление доступом доступно только администратору.')
            return
        if cmd == 'база знаний':
            if has_company_access(path, message.chat.id):
                learning_home(message.chat.id, 'knowledge')
            else:
                send(message.chat.id, 'База знаний доступна только сотрудникам подключённой компании.')
            return
        if cmd == 'аттестация':
            if has_company_access(path, message.chat.id):
                learning_home(message.chat.id, 'assessment')
            else:
                send(message.chat.id, 'Аттестация доступна только сотрудникам подключённой компании.')
            return
        receive_text(store, f'tg:{message.chat.id}:{message.message_id}', message.chat.id, message.chat.id,
                     'text', message.text or '', send)

    @bot.callback_query_handler(func=lambda call: str(call.data or '').startswith('learn:'))
    def on_learning_callback(call):
        chat_id = call.message.chat.id
        if not has_company_access(path, chat_id):
            bot.answer_callback_query(call.id, 'Раздел доступен только сотрудникам компании.', show_alert=True)
            return
        try:
            parts = str(call.data).split(':')
            action = parts[1]
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
                            + ('Модуль отмечен как пройденный.' if result['passed'] else 'Повторите модуль и попробуйте ещё раз.'))
                    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
                    markup.add(telebot.types.InlineKeyboardButton('← К аттестациям', callback_data='learn:home:assessment'))
                    bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)
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
                bot.answer_callback_query(call.id, 'Не удалось открыть учебный модуль. Попробуйте ещё раз.', show_alert=True)
            except Exception:
                pass

    @bot.callback_query_handler(func=lambda call: str(call.data or '').startswith('adm:'))
    def on_admin_callback(call):
        chat_id = call.message.chat.id
        if chat_id not in admins:
            bot.answer_callback_query(call.id, 'Доступно только администратору.', show_alert=True)
            return
        try:
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
            parts = str(call.data).split(':')
            action = parts[1]
            if action == 'list':
                send_admin_access(chat_id, call.message.message_id)
                bot.answer_callback_query(call.id)
                return
            if action == 'user':
                user_id = int(parts[2])
                text, markup = access_user_card(user_id)
                bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)
                bot.answer_callback_query(call.id)
                return
            if action == 'set':
                user_id = int(parts[2])
                role = parts[3]
                set_role(path, user_id, role)
                text, markup = access_user_card(user_id)
                bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)
                bot.answer_callback_query(call.id, f'Роль: {ROLE_LABELS[role]}')
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
        receive_text(store, f'tg:{message.chat.id}:{message.message_id}', message.chat.id, message.chat.id,
                     'voice', message.voice.file_id, send)

    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    finally:
        stop.set()
        thread.join(timeout=2)


if __name__ == '__main__':
    main()

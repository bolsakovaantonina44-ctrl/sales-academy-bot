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

LOG = logging.getLogger('academy')
FOCUS_LABELS = [v['label'] for v in FOCUS_OBJECTIONS.values()]

CONTROL_COMMANDS = {
    'начать тренировку', 'новая тренировка', 'завершить тренировку', 'заверши тренировку',
    'закончить тренировку', 'закончи тренировку', 'завершить тест', 'закончить тест',
    'повторить обработку', 'пропустить эту реплику', 'обновить разбор', 'посмотреть разбор',
    'скачать результат', 'сформировать отчет', 'отчет сотруднику', 'отчет руководителю',
    'показать скрытый сценарий', 'мои тренировки', 'сессии пользователей',
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
    rows = [['1', '2', '3'], ['Мои тренировки']]
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
        # One clarification is enough: do not trap the manager in a secretary loop.
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
    for secret in ('TELEGRAM_TOKEN', 'OPENAI_API_KEY'):
        if not os.getenv(secret):
            raise RuntimeError(secret + ' is not set')
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
    bot = telebot.TeleBot(os.environ['TELEGRAM_TOKEN'], threaded=False)
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
    try:
        identity = bot.get_me()
        webhook = bot.get_webhook_info(timeout=15)
    except Exception as exc:
        LOG.error('Startup: Telegram connection failed kind=%s', type(exc).__name__)
        raise RuntimeError('Telegram startup check failed; verify token and network') from None
    LOG.info('Startup: Telegram bot=@%s webhook_active=%s pending_updates=%s',
             identity.username, bool(webhook.url), webhook.pending_update_count)
    admins = [int(v.strip()) for v in os.getenv('ADMIN_IDS', '').split(',') if v.strip()]
    engine = Engine(store, ai, limit=int(os.getenv('FREE_TRAININGS', '3')), admin_ids=admins)

    def admin_sessions_text():
        with store.db() as db:
            rows = db.execute('SELECT id,user_id,payload,counted FROM sessions ORDER BY id DESC LIMIT 15').fetchall()
        lines = ['ПОСЛЕДНИЕ СЕССИИ ПОЛЬЗОВАТЕЛЕЙ']
        for row in rows:
            try:
                s = upgrade_session(json.loads(row['payload']))
                customer = s.get('fields', {}).get('customer', '') or 'сценарий не указан'
                employee = s.get('employee', {}).get('name') or 'ФИО не указано'
                score = None
                if s.get('report_data'):
                    from academy.reporting import total_score
                    score = total_score(s['report_data'])
                lines.append(f"#{row['id']} · TG {row['user_id']} · {employee} · {customer} · "
                             + (f'{score}/100' if score is not None else s.get('phase', ''))) 
            except Exception:
                lines.append(f"#{row['id']} · TG {row['user_id']} · не удалось прочитать")
        return '\n'.join(lines)

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
                for row in keyboard_rows(s, bool(store.failed(user_id)), user_id in admins):
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
        receive_text(store, f'tg:{message.chat.id}:{message.message_id}', message.chat.id, message.chat.id,
                     'text', message.text or '', send)

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

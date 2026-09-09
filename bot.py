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
from academy.domain import normalize_command, upgrade_session, chunks
from academy.diagnostics import log_failure

LOG = logging.getLogger('academy')


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
        rows = [['Начать тренировку'], ['Лёгкий', 'Средний', 'Сложный'], ['Новая тренировка']]
        if admin:
            rows.append(['Сессии пользователей'])
        return rows
    rows = [['1', '2', '3'], ['Мои тренировки']]
    if admin:
        rows.append(['Сессии пользователей'])
    return rows


def receive_text(store, event_key, user_id, chat_id, kind, text, send):
    accepted = store.enqueue(event_key, user_id, chat_id, kind, text)
    if accepted and kind == 'text':
        cmd = normalize_command(text)
        if cmd in ('начать тренировку', '/begin'):
            # Receipt is independent of the ordered worker and slow card/model requests.
            try:
                send(chat_id, 'Запускаю тренировку…' if store.current(user_id)['phase'] == 'ready'
                     else 'Запрос принят. Проверяю состояние тренировки…')
            except Exception:
                pass  # Input remains durable even if the cosmetic acknowledgement fails.
        elif cmd in ('/pdf', 'сформировать отчет', 'скачать результат', 'отчет сотруднику', 'отчет руководителю'):
            try:
                send(chat_id, 'Готовлю файл. Это может занять около 1 минуты…')
            except Exception:
                pass  # File request remains durable even if the acknowledgement fails.
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


def _handle_lpr_gate(store, event):
    """One realistic discovery step before the target LPR for outbound/cold scenarios."""
    if event.get('kind') != 'text':
        return False
    s = store.current(event['user_id'])
    if s.get('phase') != 'active' or not s.get('card') or s.get('lpr_gate_passed') or _direct_lpr_known(s):
        return False
    text = event['text'].strip()
    if not text or normalize_command(text) in ('начать тренировку', '/begin'):
        return False
    identity = s['card'].get('identity', {})
    if _lpr_search_attempt(text):
        target = identity.get('job_title') or s['fields'].get('customer') or 'ответственный сотрудник'
        name = identity.get('name', '').strip()
        reply = (f'Да, этим занимается {name}, {target}. Сейчас соединю.' if name
                 else f'Да, этим занимается {target}. Сейчас соединю.')
        s['lpr_gate_passed'] = True
    else:
        stage = s.get('lpr_gate_turns', 0)
        reply = ('Добрый день. Подскажите, по какому вопросу?' if stage == 0
                 else 'Понял. С кем именно вы хотите поговорить по этому вопросу?')
        s['lpr_gate_turns'] = stage + 1
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
                pass  # Cosmetic action must never abort a persisted turn.
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
            # Do not log API exception bodies, tokens, card contents or entire user messages.
            log_failure(event, store.current(event['user_id']), locals().get('stage', 'transport'), exc)
            store.fail(event, type(exc).__name__)
        try:
            deliver(store, event['user_id'], send)
        except Exception:
            pass  # Durable outbox retried independently, without another model call.


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
    # Synchronous handlers only persist arrivals. All slow work is in one ordered worker.
    bot = telebot.TeleBot(os.environ['TELEGRAM_TOKEN'], threaded=False)
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], timeout=90, max_retries=1)
    model = os.getenv('OPENAI_MODEL', 'gpt-5.6-luna')  # Original default, availability must be verified.
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

    def load_any_session(session_id):
        with store.db() as db:
            row = db.execute('SELECT user_id,payload FROM sessions WHERE id=?', (session_id,)).fetchone()
        return (row['user_id'], upgrade_session(json.loads(row['payload']))) if row else (None, None)

    def admin_sessions_text():
        with store.db() as db:
            rows = db.execute('SELECT id,user_id,payload,counted FROM sessions ORDER BY id DESC LIMIT 15').fetchall()
        lines = ['ПОСЛЕДНИЕ СЕССИИ ПОЛЬЗОВАТЕЛЕЙ']
        for row in rows:
            try:
                s = upgrade_session(json.loads(row['payload']))
                customer = s.get('fields', {}).get('customer', '') or 'сценарий не указан'
                score = None
                if s.get('report_data'):
                    scored = [x.get('score') for x in s['report_data'].get('skills', []) if x.get('score') is not None]
                    if scored:
                        score = sum(scored)
                suffix = f' · {score}/100' if score is not None else ''
                lines.append(f"№{row['id']} · user {row['user_id']} · {s.get('phase','?')}{suffix}\n{customer[:100]}")
            except Exception:
                lines.append(f"№{row['id']} · user {row['user_id']} · данные требуют проверки")
        lines += ['', 'Открыть разбор: /adminreport НОМЕР', 'Получить PDF: /adminpdf НОМЕР']
        return '\n'.join(lines)

    def send(chat_id, text):
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        for row in keyboard_rows(store.current(chat_id), bool(store.failed(chat_id)), chat_id in admins):
            markup.row(*row)
        if text.startswith('__academy_pdf__:'):
            _, sid, audience = text.split(':')
            if audience == 'supervisor' and chat_id not in admins:
                bot.send_message(chat_id, 'Расширенный отчёт доступен только администратору.', reply_markup=markup)
                return
            session = store.session_for_user(chat_id, int(sid))
            if not session:
                bot.send_message(chat_id, 'Сохранённый результат не найден.', reply_markup=markup)
                return
            from academy.pdf_report import render_pdf
            document = render_pdf(session, audience)
            bot.send_document(chat_id, document, caption='Результат тренировки · Академия продаж', reply_markup=markup)
        else:
            bot.send_message(chat_id, text, reply_markup=markup)

    @bot.message_handler(content_types=['text', 'voice'])
    def incoming(message):
        if message.chat.type != 'private':
            return  # No shared hidden cards in groups.
        kind = 'voice' if message.content_type == 'voice' else 'text'
        text = message.voice.file_id if kind == 'voice' else message.text

        # Admin-only cross-user review. Never expose these commands to normal users.
        if kind == 'text' and message.from_user.id in admins and text:
            cmd = normalize_command(text)
            if cmd in ('сессии пользователей', '/admin'):
                for part in chunks(admin_sessions_text()):
                    send(message.chat.id, part)
                return
            if cmd.startswith('/adminreport '):
                try:
                    sid = int(cmd.split()[1])
                except (ValueError, IndexError):
                    send(message.chat.id, 'Формат: /adminreport НОМЕР')
                    return
                owner, session = load_any_session(sid)
                if not session:
                    send(message.chat.id, 'Сессия не найдена.')
                    return
                header = f'Сессия №{sid} · Telegram ID {owner}\n'
                report = session.get('report') or 'Разбор ещё не сформирован.'
                for part in chunks(header + report):
                    send(message.chat.id, part)
                return
            if cmd.startswith('/adminpdf '):
                try:
                    sid = int(cmd.split()[1])
                except (ValueError, IndexError):
                    send(message.chat.id, 'Формат: /adminpdf НОМЕР')
                    return
                owner, session = load_any_session(sid)
                if not session or not session.get('report_data'):
                    send(message.chat.id, 'Для этой сессии пока нет готового отчёта.')
                    return
                send(message.chat.id, 'Готовлю файл. Это может занять около 1 минуты…')
                from academy.pdf_report import render_pdf
                document = render_pdf(session, 'supervisor')
                markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
                for row in keyboard_rows(store.current(message.chat.id), bool(store.failed(message.chat.id)), True):
                    markup.row(*row)
                bot.send_document(message.chat.id, document,
                                  caption=f'Сессия №{sid} · Telegram ID {owner}', reply_markup=markup)
                return

        if kind == 'voice' and (message.voice.duration > 180 or (message.voice.file_size or 0) > 10*1024*1024):
            send(message.chat.id, 'Отправь голосовое до 3 минут и 10 МБ.')
            return
        if kind == 'text' and (not text or len(text) > 5000):
            send(message.chat.id, 'Отправь реплику до 5000 символов.')
            return
        receive_text(store, f'{message.chat.id}:{message.message_id}', message.from_user.id, message.chat.id, kind, text, send)

    stop = threading.Event()
    thread = threading.Thread(target=worker, args=(store, engine, ai, bot, stop, send), daemon=True)
    thread.start()
    LOG.info('Sales Academy demo-4.1 started; persistent path=%s', path)
    faulthandler.cancel_dump_traceback_later()
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=False, allowed_updates=['message'])
    finally:
        stop.set()
        thread.join(timeout=5)
        client.close()
        lock.close()


if __name__ == '__main__':
    main()

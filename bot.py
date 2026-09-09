"""Telegram transport. Run one replica with persistent DB_PATH. Import is side-effect free."""
import io
import logging
import os
import threading
import time
from pathlib import Path

from academy.ai import AI
from academy.engine import Engine, deliver
from academy.store import Store
from academy.domain import normalize_command
from academy.diagnostics import log_failure

LOG = logging.getLogger('academy')


def keyboard_rows(session, failed=False, admin=False):
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
        if admin:
            rows.append(['Отчёт руководителю', 'Показать скрытый сценарий'])
        return rows
    if phase == 'ready':
        return [['Начать тренировку'], ['Лёгкий', 'Средний', 'Сложный'], ['Новая тренировка']]
    return [['1', '2', '3'], ['Мои тренировки']]


def receive_text(store, event_key, user_id, chat_id, kind, text, send):
    accepted = store.enqueue(event_key, user_id, chat_id, kind, text)
    if accepted and kind == 'text' and normalize_command(text) in ('начать тренировку', '/begin'):
        # Receipt is independent of the ordered worker and slow card/model requests.
        try:
            send(chat_id, 'Запускаю тренировку…' if store.current(user_id)['phase'] == 'ready'
                 else 'Запрос принят. Проверяю состояние тренировки…')
        except Exception:
            pass  # Input remains durable even if the cosmetic acknowledgement fails.
    return accepted


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
        if kind == 'voice' and (message.voice.duration > 180 or (message.voice.file_size or 0) > 10*1024*1024):
            send(message.chat.id, 'Отправь голосовое до 3 минут и 10 МБ.')
            return
        text = message.voice.file_id if kind == 'voice' else message.text
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

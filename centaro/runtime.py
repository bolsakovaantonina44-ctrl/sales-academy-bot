"""Independent polling process; never inherits Academy's TeleBot monkey patches."""
import fcntl
import logging
import os
import threading
from pathlib import Path

import telebot
from centaro.funnel import Funnel, PRODUCTS

LOG = logging.getLogger('centaro')


def markup(spec):
    if not spec:
        return None
    kind = spec['kind']
    if kind == 'contact':
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        keyboard.add(telebot.types.KeyboardButton('Поделиться контактом', request_contact=True))
        keyboard.add('Только Telegram', '← Главное меню')
        return keyboard
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
    if kind == 'menu':
        for key, (label, _) in PRODUCTS.items():
            keyboard.add(telebot.types.InlineKeyboardButton(label, callback_data='centaro:product:' + key))
    if kind == 'apply':
        keyboard.add(telebot.types.InlineKeyboardButton('Оставить заявку', callback_data='centaro:apply:' + spec['nonce']))
    keyboard.add(telebot.types.InlineKeyboardButton('← Главное меню', callback_data='centaro:menu'))
    return keyboard


def register(bot, funnel):
    def send(recipient, payload):
        # Remove a contact keyboard before showing the next inline menu.
        if (payload.get('markup') or {}).get('kind') == 'menu':
            bot.send_message(recipient, 'Меню Centaro', reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.send_message(recipient, payload['text'], reply_markup=markup(payload.get('markup')),
                         disable_web_page_preview=True)

    def private(message):
        return message.chat.type == 'private' and message.chat.id == message.from_user.id

    def identity(user):
        name = ' '.join(filter(None, [user.first_name, user.last_name]))
        return name, user.username or ''

    @bot.message_handler(content_types=['text', 'contact'])
    def handle(message):
        if not private(message):
            return
        text = str(message.text or '')
        key = f'm:{message.chat.id}:{message.message_id}'
        if text.startswith('/reply '):
            parts = text.split(maxsplit=2)
            if len(parts) == 3 and parts[1].isdigit():
                if funnel.reply(key, message.from_user.id, int(parts[1]), parts[2]):
                    return
        if len(text) > 500:
            bot.send_message(message.chat.id, 'Для заявки отправьте номер телефона или выберите пункт меню.')
            return
        name, username = identity(message.from_user)
        contact = None
        if message.contact:
            contact = {'user_id': message.contact.user_id, 'phone_number': message.contact.phone_number}
        funnel.process(key, message.from_user.id, name, username, text=text, contact=contact)

    @bot.callback_query_handler(func=lambda call: str(call.data or '').startswith('centaro:'))
    def callback(call):
        if not call.message or call.message.chat.type != 'private':
            return
        if call.message.chat.id != call.from_user.id:
            return
        name, username = identity(call.from_user)
        funnel.process('cb:' + call.id, call.from_user.id, name, username, callback=call.data)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            LOG.warning('Could not acknowledge callback')

    return send


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ['CENTARO_TELEGRAM_TOKEN']
    if token in (os.getenv('TELEGRAM_TOKEN'), os.getenv('LEGACY_BOT_TOKEN')):
        raise RuntimeError('Centaro and Academy must have separate tokens')
    path = Path(os.getenv('CENTARO_DB_PATH', '/data/centaro.sqlite3')).resolve()
    academy = Path(os.getenv('DB_PATH', '/data/academy.sqlite3')).resolve()
    if path == academy:
        raise RuntimeError('Centaro database must be separate from Academy')
    if os.getenv('RAILWAY_ENVIRONMENT_ID'):
        mount = os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
        if not mount or not path.is_relative_to(Path(mount).resolve()):
            raise RuntimeError('Centaro database must be inside the existing Volume')
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(path) + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    bot = telebot.TeleBot(token, threaded=False)
    if (bot.get_me().username or '').lower() != 'centaro_sales_bot':
        raise RuntimeError('Token is not for @centaro_sales_bot')
    if bot.get_webhook_info().url:
        raise RuntimeError('Centaro webhook still active: disconnect Botmother before switching')
    funnel = Funnel(path, os.getenv('CENTARO_OWNER_ID', '418136392'))
    send = register(bot, funnel)
    stop = threading.Event()

    def delivery():
        while not stop.is_set():
            try:
                funnel.deliver(send)
            except Exception:
                LOG.error('Centaro notification queue unavailable')
            stop.wait(1)

    worker = threading.Thread(target=delivery, daemon=True)
    worker.start()
    LOG.info('Centaro polling started; durable database ready')
    try:
        bot.infinity_polling(skip_pending=False, timeout=30, long_polling_timeout=30,
                             allowed_updates=['message', 'callback_query'])
    finally:
        stop.set()
        worker.join(3)
        lock.close()


if __name__ == '__main__':
    main()

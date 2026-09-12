"""Production launcher with a narrow Telegram edit fallback.

The admin session viewer normally edits the inline list message into a session card.
Some Telegram clients / message states can reject editMessageText. In that case,
keep the callback flow usable by sending the requested content as a fresh message.
"""
import logging

import telebot


LOG = logging.getLogger("academy")
_original_edit_message_text = telebot.TeleBot.edit_message_text


def _resilient_edit_message_text(self, text, chat_id=None, message_id=None,
                                 inline_message_id=None, parse_mode=None,
                                 entities=None, reply_markup=None,
                                 disable_web_page_preview=None, timeout=None,
                                 business_connection_id=None, **kwargs):
    try:
        return _original_edit_message_text(
            self,
            text,
            chat_id=chat_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
            parse_mode=parse_mode,
            entities=entities,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
            timeout=timeout,
            business_connection_id=business_connection_id,
            **kwargs,
        )
    except Exception as exc:
        if chat_id is None:
            raise
        LOG.warning(
            "Telegram edit failed; falling back to send_message chat_id=%s message_id=%s kind=%s",
            chat_id,
            message_id,
            type(exc).__name__,
        )
        return self.send_message(chat_id, text, reply_markup=reply_markup)


telebot.TeleBot.edit_message_text = _resilient_edit_message_text

import bot  # noqa: E402  (patch must be installed before bot.main creates the client)


if __name__ == "__main__":
    bot.main()

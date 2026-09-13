"""Startup-only callback fix for the existing Railway deployment image.

The service source is pinned to an older commit. This self-contained bootstrap
can also be passed to `python -c` as its start command, without downloading code,
changing credentials, creating services, or touching persistent database files.
Restore `python telemetry_launcher.py` after deploying the permanent bot.py fix.
"""
import logging
import runpy

import telebot


def install():
    original_get_updates = telebot.TeleBot.get_updates
    original_callbacks = telebot.TeleBot.process_new_callback_query
    log = logging.getLogger('academy')

    def get_updates_with_callbacks(self, *args, **kwargs):
        first = not getattr(self, '_callback_filter_verified', False)
        if first:
            info = self.get_webhook_info(timeout=15)
            log.info('Callback repair: previous_allowed_updates=%s', info.allowed_updates)
        kwargs['allowed_updates'] = ['message', 'callback_query']
        result = original_get_updates(self, *args, **kwargs)
        if first:
            info = self.get_webhook_info(timeout=15)
            log.info('Callback repair: confirmed_allowed_updates=%s', info.allowed_updates)
            self._callback_filter_verified = True
        return result

    def callbacks_received(self, updates):
        log.info('Telegram callback delivery: count=%s', len(updates))
        return original_callbacks(self, updates)

    telebot.TeleBot.get_updates = get_updates_with_callbacks
    telebot.TeleBot.process_new_callback_query = callbacks_received


if __name__ == '__main__':
    install()
    runpy.run_module('telemetry_launcher', run_name='__main__')

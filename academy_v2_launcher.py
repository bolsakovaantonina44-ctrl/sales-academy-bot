"""Production entrypoint for the Academy v2 mobile learning route.

The Academy runtimes are installed before the existing telemetry/launcher stack.
Academy v2 and call-attestation callbacks are explicitly included in Telegram callback
handler filters so inline buttons remain active alongside legacy admin/learning handlers.
"""
import telebot

from academy.mobile_learning_runtime import install
from academy.call_attestation_runtime import install as install_call_attestation

install()
install_call_attestation()

# The runtimes wrap callback handlers, but the legacy bot registers handlers with
# narrow prefixes (team:, learn:, adm:, acc:). Without widening those filters,
# Telegram never invokes the runtime wrappers for Academy callbacks.
_runtime_callback_handler = telebot.TeleBot.callback_query_handler


def _academy_v2_callback_handler(self, *args, **kwargs):
    original_func = kwargs.get("func")
    if original_func is not None:
        def combined_func(call):
            data = str(getattr(call, "data", "") or "")
            if data.startswith(("academyv2:", "academycalls:")):
                return True
            return original_func(call)

        kwargs["func"] = combined_func
    return _runtime_callback_handler(self, *args, **kwargs)


telebot.TeleBot.callback_query_handler = _academy_v2_callback_handler

import telemetry_launcher  # noqa: E402


if __name__ == "__main__":
    telemetry_launcher.launcher.bot.main()

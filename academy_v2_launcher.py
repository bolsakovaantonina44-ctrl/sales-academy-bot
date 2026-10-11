"""Production entrypoint for the Academy v2 mobile learning route.

The Academy runtime is installed before the existing telemetry/launcher stack.
Academy v2 callbacks are explicitly included in Telegram callback handler filters so
inline learning buttons remain active alongside legacy admin/learning handlers.
"""
import telebot

from academy.mobile_learning_runtime import install as install_mobile_learning
from academy.series_ux_runtime import install as install_series_ux
from academy.centaro_performance_runtime import install as install_centaro_performance
from academy.centaro_role_reports_runtime import install as install_centaro_role_reports
from academy.centaro_rop_runtime import install as install_centaro_rop
from academy.centaro_crm_calibration_runtime import install as install_centaro_crm_calibration
from academy.centaro_calibrated_forecast_runtime import install as install_centaro_calibrated_forecast

install_mobile_learning()
install_series_ux()
install_centaro_performance()
install_centaro_role_reports()
install_centaro_rop()
install_centaro_crm_calibration()
install_centaro_calibrated_forecast()

# mobile_learning_runtime wraps callback handlers, but the legacy bot registers
# handlers with narrow prefixes (team:, learn:, adm:, acc:). Without widening those
# filters, Telegram never invokes the wrapper for academyv2:* callbacks. Extend each
# registered filter so the runtime gets first chance to handle Academy v2 buttons.
_runtime_callback_handler = telebot.TeleBot.callback_query_handler


def _academy_v2_callback_handler(self, *args, **kwargs):
    original_func = kwargs.get("func")
    if original_func is not None:
        def combined_func(call):
            data = str(getattr(call, "data", "") or "")
            if data.startswith("academyv2:"):
                return True
            return original_func(call)

        kwargs["func"] = combined_func
    return _runtime_callback_handler(self, *args, **kwargs)


telebot.TeleBot.callback_query_handler = _academy_v2_callback_handler

import telemetry_launcher  # noqa: E402


if __name__ == "__main__":
    from centaro.supervisor import start
    stop_centaro = start()
    try:
        telemetry_launcher.launcher.bot.main()
    finally:
        stop_centaro()

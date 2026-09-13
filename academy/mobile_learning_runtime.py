"""Runtime patch for the mobile-first Academy route.

Installs before bot.main() registers handlers. Academy navigation is isolated from
trainer controls and uses inline buttons so stale training reply keyboards disappear.
"""
import os
import functools

import telebot

from academy.access import has_company_access, get_role, SUPERVISOR
from academy.onboarding import MODULE_ORDER, ONBOARDING
from academy.onboarding_progress import progress, next_lesson, next_after, mark_completed

_INSTALLED = False
_ORIGINAL_MESSAGE_HANDLER = None
_ORIGINAL_CALLBACK_HANDLER = None


def _db_path():
    return os.getenv("DB_PATH", "./data/academy.sqlite3")


def _admins():
    result = set()
    for value in os.getenv("ADMIN_IDS", "").split(","):
        value = value.strip()
        if value:
            try:
                result.add(int(value))
            except ValueError:
                pass
    return result


def _remove_reply_keyboard(bot, chat_id):
    # One short transition message removes the old trainer keyboard reliably.
    bot.send_message(
        chat_id,
        "Открываю Академию…",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )


def _home_markup(user_id):
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(telebot.types.InlineKeyboardButton("▶️ Продолжить обучение", callback_data="academyv2:continue"))
    markup.add(telebot.types.InlineKeyboardButton("📚 Все разделы", callback_data="academyv2:modules"))
    markup.add(telebot.types.InlineKeyboardButton("📝 Аттестация", callback_data="learn:home:assessment"))
    markup.add(telebot.types.InlineKeyboardButton("📊 Мой прогресс", callback_data="academyv2:progress"))
    if get_role(_db_path(), user_id) == SUPERVISOR or user_id in _admins():
        markup.add(telebot.types.InlineKeyboardButton("👥 Команда", callback_data="team:list"))
    return markup


def _send_home(bot, chat_id, remove_keyboard=False):
    if remove_keyboard:
        _remove_reply_keyboard(bot, chat_id)
    p = progress(_db_path(), chat_id)
    if p["completed"] >= p["total"]:
        status = "Базовый маршрут завершён. Можно переходить к аттестации и тренировкам."
    else:
        status = f"Пройдено: {p['completed']} из {p['total']} уроков."
    bot.send_message(
        chat_id,
        "АКАДЕМИЯ АКЕНСО\n\n"
        + status
        + "\n\nУчитесь короткими блоками. После любого урока можно остановиться — прогресс сохранится.",
        reply_markup=_home_markup(chat_id),
    )


def _modules_markup(user_id):
    p = progress(_db_path(), user_id)
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for module_id in MODULE_ORDER:
        item = p["modules"][module_id]
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"{item['title']} · {item['completed']}/{item['total']}",
                callback_data=f"academyv2:module:{module_id}",
            )
        )
    markup.add(telebot.types.InlineKeyboardButton("← В Академию", callback_data="academyv2:home"))
    return markup


def _send_modules(bot, chat_id):
    bot.send_message(
        chat_id,
        "РАЗДЕЛЫ ОБУЧЕНИЯ\n\nПроходите по порядку или возвращайтесь к нужному разделу.",
        reply_markup=_modules_markup(chat_id),
    )


def _module_markup(module_id):
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for index, lesson in enumerate(ONBOARDING[module_id]["lessons"]):
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"{index + 1}. {lesson['title']} · {lesson['minutes']} мин",
                callback_data=f"academyv2:lesson:{module_id}:{index}",
            )
        )
    markup.add(telebot.types.InlineKeyboardButton("← К разделам", callback_data="academyv2:modules"))
    return markup


def _send_module(bot, chat_id, module_id):
    item = ONBOARDING[module_id]
    bot.send_message(
        chat_id,
        f"{item['title'].upper()}\n\n{item['intro']}",
        reply_markup=_module_markup(module_id),
    )


def _lesson_markup(module_id, index):
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        telebot.types.InlineKeyboardButton(
            "✅ Проверить себя",
            callback_data=f"academyv2:check:{module_id}:{index}",
        )
    )
    markup.add(telebot.types.InlineKeyboardButton("⏸ Сделать паузу", callback_data="academyv2:pause"))
    markup.add(telebot.types.InlineKeyboardButton("← К урокам раздела", callback_data=f"academyv2:module:{module_id}"))
    return markup


def _send_lesson(bot, chat_id, module_id, index):
    lesson = ONBOARDING[module_id]["lessons"][int(index)]
    bot.send_message(
        chat_id,
        f"{ONBOARDING[module_id]['title']} · урок {int(index) + 1}/{len(ONBOARDING[module_id]['lessons'])}\n"
        f"{lesson['title']} · {lesson['minutes']} мин\n\n{lesson['body']}",
        reply_markup=_lesson_markup(module_id, index),
        disable_web_page_preview=True,
    )


def _check_markup(module_id, index):
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        telebot.types.InlineKeyboardButton(
            "Показать подсказку из урока",
            callback_data=f"academyv2:lesson:{module_id}:{index}",
        )
    )
    markup.add(
        telebot.types.InlineKeyboardButton(
            "✅ Ответил(а) — идти дальше",
            callback_data=f"academyv2:complete:{module_id}:{index}",
        )
    )
    markup.add(telebot.types.InlineKeyboardButton("⏸ Сделать паузу", callback_data="academyv2:pause"))
    return markup


def _send_checkpoint(bot, chat_id, module_id, index):
    lesson = ONBOARDING[module_id]["lessons"][int(index)]
    bot.send_message(
        chat_id,
        "ПРОВЕРЬТЕ СЕБЯ\n\n"
        + lesson["checkpoint"]
        + "\n\nОтветьте себе вслух или одной-двумя фразами. Это не аттестация — здесь важно понять, можете ли вы объяснить материал своими словами.",
        reply_markup=_check_markup(module_id, index),
    )


def _send_progress(bot, chat_id):
    p = progress(_db_path(), chat_id)
    lines = [f"МОЙ ПРОГРЕСС\n\nПройдено: {p['completed']} из {p['total']} уроков."]
    for module_id in MODULE_ORDER:
        item = p["modules"][module_id]
        lines.append(f"• {item['title']}: {item['completed']}/{item['total']}")
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(telebot.types.InlineKeyboardButton("▶️ Продолжить обучение", callback_data="academyv2:continue"))
    markup.add(telebot.types.InlineKeyboardButton("← В Академию", callback_data="academyv2:home"))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)


def _continue(bot, chat_id):
    target = next_lesson(_db_path(), chat_id)
    if target is None:
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        markup.add(telebot.types.InlineKeyboardButton("📝 Перейти к аттестации", callback_data="learn:home:assessment"))
        markup.add(telebot.types.InlineKeyboardButton("← В Академию", callback_data="academyv2:home"))
        bot.send_message(
            chat_id,
            "Базовое обучение завершено. Все 11 уроков пройдены.",
            reply_markup=markup,
        )
        return
    module_id, index, _ = target
    _send_lesson(bot, chat_id, module_id, index)


def _complete(bot, chat_id, module_id, index):
    lesson = ONBOARDING[module_id]["lessons"][int(index)]
    mark_completed(_db_path(), chat_id, lesson["id"])
    upcoming = next_after(module_id, int(index))
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    if upcoming is None:
        markup.add(telebot.types.InlineKeyboardButton("📝 Перейти к аттестации", callback_data="learn:home:assessment"))
        markup.add(telebot.types.InlineKeyboardButton("📊 Мой прогресс", callback_data="academyv2:progress"))
        text = "Урок отмечен как пройденный. Базовый маршрут завершён."
    else:
        next_module, next_index, next_item = upcoming
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"Дальше: {next_item['title']}",
                callback_data=f"academyv2:lesson:{next_module}:{next_index}",
            )
        )
        markup.add(telebot.types.InlineKeyboardButton("⏸ Сделать паузу", callback_data="academyv2:pause"))
        text = "Урок отмечен как пройденный. Можно идти дальше или остановиться — прогресс уже сохранён."
    bot.send_message(chat_id, text, reply_markup=markup)


def _handle_text(bot, message):
    text = (getattr(message, "text", None) or "").strip().lower().replace("ё", "е")
    chat_id = message.chat.id
    if text not in {"база знаний", "продолжить обучение", "мой прогресс", "к академии"}:
        return False
    if not has_company_access(_db_path(), chat_id):
        bot.send_message(chat_id, "Академия доступна только сотрудникам подключённой компании.")
        return True
    if text in {"база знаний", "к академии"}:
        _send_home(bot, chat_id, remove_keyboard=True)
    elif text == "продолжить обучение":
        _remove_reply_keyboard(bot, chat_id)
        _continue(bot, chat_id)
    else:
        _remove_reply_keyboard(bot, chat_id)
        _send_progress(bot, chat_id)
    return True


def _handle_callback(bot, call):
    data = str(getattr(call, "data", "") or "")
    if not data.startswith("academyv2:"):
        return False
    chat_id = call.message.chat.id
    if not has_company_access(_db_path(), chat_id):
        bot.answer_callback_query(call.id, "Раздел доступен только сотрудникам компании.", show_alert=True)
        return True
    parts = data.split(":")
    action = parts[1]
    try:
        if action == "home":
            _send_home(bot, chat_id)
        elif action == "modules":
            _send_modules(bot, chat_id)
        elif action == "module":
            _send_module(bot, chat_id, parts[2])
        elif action == "lesson":
            _send_lesson(bot, chat_id, parts[2], int(parts[3]))
        elif action == "check":
            _send_checkpoint(bot, chat_id, parts[2], int(parts[3]))
        elif action == "complete":
            _complete(bot, chat_id, parts[2], int(parts[3]))
        elif action == "continue":
            _continue(bot, chat_id)
        elif action == "progress":
            _send_progress(bot, chat_id)
        elif action == "pause":
            p = progress(_db_path(), chat_id)
            bot.send_message(
                chat_id,
                f"Пауза сохранена. Пройдено {p['completed']} из {p['total']} уроков. Возвращайтесь через «Продолжить обучение».",
                reply_markup=_home_markup(chat_id),
            )
        else:
            bot.answer_callback_query(call.id, "Неизвестная команда.", show_alert=True)
            return True
        bot.answer_callback_query(call.id)
    except Exception:
        try:
            bot.answer_callback_query(call.id, "Не удалось открыть урок. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass
    return True


def install():
    global _INSTALLED, _ORIGINAL_MESSAGE_HANDLER, _ORIGINAL_CALLBACK_HANDLER
    if _INSTALLED:
        return
    _INSTALLED = True

    _ORIGINAL_MESSAGE_HANDLER = telebot.TeleBot.message_handler
    _ORIGINAL_CALLBACK_HANDLER = telebot.TeleBot.callback_query_handler

    def patched_message_handler(self, *args, **kwargs):
        original_decorator = _ORIGINAL_MESSAGE_HANDLER(self, *args, **kwargs)

        def decorator(handler):
            if handler.__name__ != "on_text":
                return original_decorator(handler)

            @functools.wraps(handler)
            def wrapped(message):
                if _handle_text(self, message):
                    return None
                return handler(message)

            return original_decorator(wrapped)

        return decorator

    def patched_callback_handler(self, *args, **kwargs):
        original_decorator = _ORIGINAL_CALLBACK_HANDLER(self, *args, **kwargs)

        def decorator(handler):
            @functools.wraps(handler)
            def wrapped(call):
                if _handle_callback(self, call):
                    return None
                return handler(call)

            return original_decorator(wrapped)

        return decorator

    telebot.TeleBot.message_handler = patched_message_handler
    telebot.TeleBot.callback_query_handler = patched_callback_handler

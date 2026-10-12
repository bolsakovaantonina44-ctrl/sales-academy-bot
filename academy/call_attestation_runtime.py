"""Telegram runtime for uninterrupted 10/20/30-call sales attestation.

Installed after mobile_learning_runtime and before bot.main() registers handlers.
The runtime reuses the existing trainer sessions and evaluator, but hides per-call
coaching during an exam batch and publishes one aggregate result at the end.
"""
from __future__ import annotations

import functools
import os
import re
import time
from types import SimpleNamespace

import telebot

from academy.access import has_company_access
from academy import call_attestation as calls

_INSTALLED = False
_PREVIOUS_MESSAGE_HANDLER = None
_PREVIOUS_CALLBACK_HANDLER = None
_ORIGINAL_SEND_MESSAGE = None
_ORIGINAL_SEND_DOCUMENT = None
_BOT_ON_TEXT_HANDLER = None
_SUPPRESS_REPORT_CHATS = set()


def _db_path():
    return os.getenv("DB_PATH", "./data/academy.sqlite3")


def _admins():
    result = set()
    for value in os.getenv("ADMIN_IDS", "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            result.add(int(value))
        except ValueError:
            pass
    return result


def _normalize(text):
    return " ".join((text or "").strip().lower().replace("ё", "е").split())


def _employee_name(user):
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    return " ".join(str(x).strip() for x in parts if x and str(x).strip())


def _current_call_session(user_id):
    try:
        session = calls._current_session(_db_path(), user_id)
    except Exception:
        return None
    if not session or not session.get("call_attestation"):
        return None
    return session


def _add_calls_button(text, markup):
    if not isinstance(markup, telebot.types.InlineKeyboardMarkup):
        return markup
    if not str(text).startswith(("АКАДЕМИЯ АКЕНСО", "АТТЕСТАЦИЯ АКЕНСО")):
        return markup
    existing = []
    for row in getattr(markup, "keyboard", []) or []:
        existing.extend(str(getattr(button, "callback_data", "") or "") for button in row)
    if "academycalls:menu" not in existing:
        markup.add(telebot.types.InlineKeyboardButton(
            "📞 Аттестация: серия звонков", callback_data="academycalls:menu"
        ))
    return markup


def _menu_markup():
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for total in calls.ALLOWED_BATCH_SIZES:
        markup.add(telebot.types.InlineKeyboardButton(
            f"Начать {total} звонков подряд", callback_data=f"academycalls:start:{total}"
        ))
    markup.add(telebot.types.InlineKeyboardButton(
        "📊 История серий", callback_data="academycalls:history"
    ))
    markup.add(telebot.types.InlineKeyboardButton(
        "← В Академию", callback_data="academyv2:home"
    ))
    return markup


def _send_menu(bot, chat_id):
    active = calls.active_batch(_db_path(), chat_id)
    if active:
        text = (
            "АТТЕСТАЦИЯ ПО ЗВОНКАМ\n\n"
            f"Сейчас идёт серия: {active['completed_calls']} из {active['total_calls']} звонков завершено.\n"
            "Пауза и переход в другие разделы во время серии отключены. Если между действиями будет "
            "пауза более 15 минут, это будет отмечено в итоговом результате."
        )
    else:
        text = (
            "АТТЕСТАЦИЯ ПО ЗВОНКАМ\n\n"
            "Выберите 10, 20 или 30 последовательных звонков. Ситуации, клиенты и возражения меняются, "
            "а сложность растёт от базовой к высокой. Во время серии подсказки и промежуточные оценки скрыты.\n\n"
            "В конце система покажет конверсию именно этой серии, средний балл, результат по трём ступеням "
            "сложности и динамику относительно прошлых серий."
        )
    _ORIGINAL_SEND_MESSAGE(bot, chat_id, text, reply_markup=_menu_markup())


def _history_text(user_id):
    history = calls.history(_db_path(), user_id, limit=5)
    if not history:
        return "ИСТОРИЯ СЕРИЙ\n\nЗавершённых серий пока нет."
    lines = ["ИСТОРИЯ СЕРИЙ", ""]
    for item in history:
        avg = "—" if item["average_score"] is None else str(item["average_score"])
        continuity = "с паузой" if item["interrupted"] else "непрерывно"
        lines.append(
            f"• {item['total_calls']} звонков: конверсия {item['conversion']}% · "
            f"средний балл {avg} · {continuity}"
        )
    return "\n".join(lines)


def _fake_text(chat_id, text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=int(chat_id)),
        text=text,
        message_id=int(time.time() * 1_000_000),
    )


def _start_next_call(bot, chat_id):
    session = calls.prepare_next_call(_db_path(), chat_id)
    if session is None:
        return
    meta = session["call_attestation"]
    _ORIGINAL_SEND_MESSAGE(
        bot,
        chat_id,
        f"ЗВОНОК {meta['call_no']} ИЗ {meta['total_calls']}\n"
        "Новый клиент готов. Возражение заранее не показывается. Начинайте как в реальном рабочем звонке.",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )
    if _BOT_ON_TEXT_HANDLER is None:
        _ORIGINAL_SEND_MESSAGE(
            bot, chat_id, "Не удалось автоматически запустить звонок. Нажмите «Начать тренировку»."
        )
        return
    _BOT_ON_TEXT_HANDLER(_fake_text(chat_id, "Начать тренировку"))


def _start_batch(bot, call, total):
    chat_id = call.message.chat.id
    if not has_company_access(_db_path(), chat_id):
        bot.answer_callback_query(call.id, "Доступно сотрудникам подключённой компании.", show_alert=True)
        return
    existing = calls.active_batch(_db_path(), chat_id)
    batch = calls.start_batch(
        _db_path(), chat_id, total, employee_name=_employee_name(call.from_user)
    )
    bot.answer_callback_query(call.id)
    if existing:
        current = _current_call_session(chat_id)
        if current and current.get("phase") in {"ready", "active", "closed"}:
            _ORIGINAL_SEND_MESSAGE(
                bot,
                chat_id,
                f"Серия уже идёт: {batch['completed_calls']} из {batch['total_calls']} завершено. "
                "Продолжайте текущий звонок.",
            )
            return
    _ORIGINAL_SEND_MESSAGE(
        bot,
        chat_id,
        f"СТАРТ АТТЕСТАЦИИ · {batch['total_calls']} ЗВОНКОВ\n\n"
        "Результаты отдельных звонков до конца серии не показываются. "
        "Конверсия считается только внутри выбранной серии. Длинная пауза будет отмечена как нарушение непрерывности.",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )
    _start_next_call(bot, chat_id)


def _handle_callback(bot, call):
    data = str(getattr(call, "data", "") or "")
    if not data.startswith("academycalls:"):
        return False
    chat_id = call.message.chat.id
    try:
        if not has_company_access(_db_path(), chat_id):
            bot.answer_callback_query(call.id, "Раздел доступен сотрудникам компании.", show_alert=True)
            return True
        calls.touch(_db_path(), chat_id)
        parts = data.split(":")
        action = parts[1]
        if action == "menu":
            _send_menu(bot, chat_id)
            bot.answer_callback_query(call.id)
        elif action == "start":
            _start_batch(bot, call, int(parts[2]))
        elif action == "history":
            markup = telebot.types.InlineKeyboardMarkup(row_width=1)
            markup.add(telebot.types.InlineKeyboardButton(
                "← К серии звонков", callback_data="academycalls:menu"
            ))
            _ORIGINAL_SEND_MESSAGE(bot, chat_id, _history_text(chat_id), reply_markup=markup)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Неизвестная команда.", show_alert=True)
        return True
    except Exception:
        try:
            bot.answer_callback_query(call.id, "Не удалось открыть аттестацию. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass
        return True


def _handle_text(bot, message):
    text = _normalize(getattr(message, "text", None))
    chat_id = message.chat.id
    if text in {"аттестация по звонкам", "серия звонков", "аттестация звонков"}:
        if not has_company_access(_db_path(), chat_id):
            _ORIGINAL_SEND_MESSAGE(bot, chat_id, "Раздел доступен сотрудникам подключённой компании.")
            return True
        _send_menu(bot, chat_id)
        return True

    active = calls.active_batch(_db_path(), chat_id)
    if not active:
        return False
    blocked = {
        "к академии", "база знаний", "продолжить обучение", "мой прогресс",
        "новая тренировка", "аттестация", "команда", "прогресс команды",
    }
    if text in blocked:
        _ORIGINAL_SEND_MESSAGE(
            bot,
            chat_id,
            f"Сейчас идёт аттестационная серия: {active['completed_calls']} из {active['total_calls']} звонков завершено. "
            "Поставить её на паузу или перейти в другой раздел нельзя. Завершите текущий звонок и серию.",
        )
        return True
    return False


def _notify_batch_complete(bot, summary):
    user_id = summary["user_id"]
    history = calls.history(_db_path(), user_id, limit=5)
    text = calls.format_summary(summary, include_history=history)
    _ORIGINAL_SEND_MESSAGE(bot, user_id, text, reply_markup=_menu_markup())
    employee = summary.get("employee_name") or f"TG {user_id}"
    supervisor_text = "РЕЗУЛЬТАТ АТТЕСТАЦИИ СОТРУДНИКА\n\n" + employee + "\n\n" + text
    for admin_id in _admins():
        if int(admin_id) == int(user_id):
            continue
        try:
            _ORIGINAL_SEND_MESSAGE(bot, admin_id, supervisor_text)
        except Exception:
            pass


def _session_id_from_notice(text):
    match = re.search(r"Тренировка №(\d+)", str(text))
    return int(match.group(1)) if match else None


def _session_id_from_filename(kwargs):
    name = str(kwargs.get("visible_file_name") or "")
    match = re.search(r"_(\d+)\.pdf$", name)
    return int(match.group(1)) if match else None


def install():
    global _INSTALLED, _PREVIOUS_MESSAGE_HANDLER, _PREVIOUS_CALLBACK_HANDLER
    global _ORIGINAL_SEND_MESSAGE, _ORIGINAL_SEND_DOCUMENT
    if _INSTALLED:
        return
    _INSTALLED = True
    calls.ensure_schema(_db_path())
    _PREVIOUS_MESSAGE_HANDLER = telebot.TeleBot.message_handler
    _PREVIOUS_CALLBACK_HANDLER = telebot.TeleBot.callback_query_handler
    _ORIGINAL_SEND_MESSAGE = telebot.TeleBot.send_message
    _ORIGINAL_SEND_DOCUMENT = telebot.TeleBot.send_document

    def patched_message_handler(self, *args, **kwargs):
        previous_decorator = _PREVIOUS_MESSAGE_HANDLER(self, *args, **kwargs)

        def decorator(handler):
            global _BOT_ON_TEXT_HANDLER
            if handler.__name__ == "on_text":
                _BOT_ON_TEXT_HANDLER = handler

            @functools.wraps(handler)
            def wrapped(message):
                try:
                    calls.touch(_db_path(), message.chat.id)
                except Exception:
                    pass
                if handler.__name__ == "on_text" and _handle_text(self, message):
                    return None
                return handler(message)

            return previous_decorator(wrapped)

        return decorator

    def patched_callback_handler(self, *args, **kwargs):
        original_func = kwargs.get("func")
        if original_func is not None:
            def combined_func(call):
                data = str(getattr(call, "data", "") or "")
                if data.startswith("academycalls:"):
                    return True
                return original_func(call)
            kwargs["func"] = combined_func
        previous_decorator = _PREVIOUS_CALLBACK_HANDLER(self, *args, **kwargs)

        def decorator(handler):
            @functools.wraps(handler)
            def wrapped(call):
                if _handle_callback(self, call):
                    return None
                return handler(call)
            return previous_decorator(wrapped)

        return decorator

    def patched_send_message(self, chat_id, text, *args, **kwargs):
        try:
            kwargs["reply_markup"] = _add_calls_button(text, kwargs.get("reply_markup"))
            if str(text).startswith("НОВАЯ ТРЕНИРОВКА СОТРУДНИКА"):
                sid = _session_id_from_notice(text)
                if sid and calls.session_batch_meta(_db_path(), sid):
                    return None

            session = _current_call_session(chat_id)
            if session:
                meta = session["call_attestation"]
                value = str(text)
                if value.startswith("Запускаю тренировку"):
                    return None
                if value.startswith("Тренировка началась. Фокус:"):
                    text = (
                        f"ЗВОНОК {meta['call_no']} ИЗ {meta['total_calls']} НАЧАЛСЯ\n"
                        "Клиент на линии. Возражение заранее не раскрывается. Ведите разговор до результата."
                    )
                elif value.startswith("РЕЗУЛЬТАТ ЭТОЙ ТРЕНИРОВКИ"):
                    _SUPPRESS_REPORT_CHATS.add(int(chat_id))
                    return None
                elif int(chat_id) in _SUPPRESS_REPORT_CHATS and not value.startswith("Разбор сохранён."):
                    return None
                elif value.startswith("Разбор сохранён. PDF-файл"):
                    _SUPPRESS_REPORT_CHATS.discard(int(chat_id))
                    recorded = calls.record_completed(_db_path(), chat_id, session=session)
                    if not recorded:
                        return None
                    summary = recorded["batch"]
                    if not recorded["recorded"]:
                        return None
                    if summary["status"] == "completed":
                        _notify_batch_complete(self, summary)
                    else:
                        _ORIGINAL_SEND_MESSAGE(
                            self,
                            chat_id,
                            f"Звонок {summary['completed_calls']} из {summary['total_calls']} завершён. "
                            "Результат зафиксирован. Следующий звонок запускается автоматически.",
                            reply_markup=telebot.types.ReplyKeyboardRemove(),
                        )
                        _start_next_call(self, chat_id)
                    return None
        except Exception:
            pass
        return _ORIGINAL_SEND_MESSAGE(self, chat_id, text, *args, **kwargs)

    def patched_send_document(self, chat_id, document, *args, **kwargs):
        try:
            sid = _session_id_from_filename(kwargs)
            if sid and calls.session_batch_meta(_db_path(), sid):
                return None
        except Exception:
            pass
        return _ORIGINAL_SEND_DOCUMENT(self, chat_id, document, *args, **kwargs)

    telebot.TeleBot.message_handler = patched_message_handler
    telebot.TeleBot.callback_query_handler = patched_callback_handler
    telebot.TeleBot.send_message = patched_send_message
    telebot.TeleBot.send_document = patched_send_document

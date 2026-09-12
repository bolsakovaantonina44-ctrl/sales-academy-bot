"""Production launcher with resilient Telegram admin-session navigation.

This module keeps the production bot compatible with Telegram client quirks while
providing an admin session viewer. It also persists an exact mapping between new
voice messages and the session in which they were sent, so supervisors can replay
the correct audio later.
"""
import faulthandler
import functools
import json
import logging
import os
import re
import sqlite3
from collections import Counter

import telebot

from academy.domain import chunks, upgrade_session
from academy.reporting import total_score
from academy.store import Store


LOG = logging.getLogger("academy")
_original_edit_message_text = telebot.TeleBot.edit_message_text
_original_message_handler = telebot.TeleBot.message_handler
_original_store_enqueue = Store.enqueue


def _db_path():
    return os.getenv("DB_PATH", "./data/academy.sqlite3")


def _ensure_voice_map(path=None):
    """Create the lightweight voice-to-session index if it is not present yet."""
    target = path or _db_path()
    try:
        with sqlite3.connect(target) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS voice_session_map("
                "inbox_id INTEGER PRIMARY KEY, "
                "session_id INTEGER NOT NULL, "
                "user_id INTEGER NOT NULL, "
                "file_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS ix_voice_session_map_session "
                "ON voice_session_map(session_id,inbox_id)"
            )
    except sqlite3.OperationalError:
        # The database directory may not exist yet at import time. The first
        # accepted voice message retries creation after Store has initialized it.
        pass


def _enqueue_with_voice_session(self, event_key, user_id, chat_id, kind, text):
    """Keep Store semantics, and index new voice messages to their active session."""
    accepted = _original_store_enqueue(self, event_key, user_id, chat_id, kind, text)
    if not accepted or kind != "voice":
        return accepted
    try:
        _ensure_voice_map(self.path)
        with self.db() as db:
            event = db.execute(
                "SELECT id FROM inbox WHERE event_key=?", (event_key,)
            ).fetchone()
            current = db.execute(
                "SELECT u.current_id,s.payload FROM users u "
                "JOIN sessions s ON s.id=u.current_id WHERE u.user_id=?",
                (user_id,),
            ).fetchone()
            if not event or not current or not current["current_id"]:
                return accepted
            try:
                phase = upgrade_session(json.loads(current["payload"])).get("phase")
            except Exception:
                phase = None
            # Voice replay belongs to the sales conversation itself. Do not attach
            # setup/profile voice input to an old or not-yet-started session.
            if phase not in ("active", "closed"):
                return accepted
            db.execute(
                "INSERT OR REPLACE INTO voice_session_map(inbox_id,session_id,user_id,file_id) "
                "VALUES(?,?,?,?)",
                (event["id"], current["current_id"], user_id, text),
            )
    except Exception:
        # Voice indexing is an admin convenience and must never block training.
        LOG.exception("Voice session indexing failed user_id=%s event_key=%s", user_id, event_key)
    return accepted


Store.enqueue = _enqueue_with_voice_session

# bot.main() intentionally used a one-shot 60-second faulthandler dump while the
# production worker was being diagnosed. It prints a scary "Timeout" traceback
# even when long polling is healthy. Disable only that scheduled dump; real Python
# exceptions and normal logging remain untouched.
faulthandler.dump_traceback_later = lambda *args, **kwargs: None


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


def _admin_ids():
    result = set()
    for value in os.getenv("ADMIN_IDS", "").split(","):
        value = value.strip()
        if value:
            try:
                result.add(int(value))
            except ValueError:
                pass
    return result


def _session_rows(page=0, page_size=8):
    page = max(0, int(page))
    with sqlite3.connect(_db_path()) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        rows = db.execute(
            "SELECT id,user_id,payload,counted FROM sessions ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, page * page_size),
        ).fetchall()
    return rows, total


def _load_session(session_id):
    with sqlite3.connect(_db_path()) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT id,user_id,payload,counted FROM sessions WHERE id=?", (int(session_id),)
        ).fetchone()
    if not row:
        return None, None
    try:
        return row, upgrade_session(json.loads(row["payload"]))
    except Exception:
        return row, None


def _score(session):
    if not session or not session.get("report_data"):
        return None
    try:
        return total_score(session["report_data"])
    except Exception:
        return None


def _session_label(row):
    try:
        session = upgrade_session(json.loads(row["payload"]))
        employee = session.get("employee", {}).get("name") or f"TG {row['user_id']}"
        score = _score(session)
        suffix = f"{score}/100" if score is not None else session.get("phase", "")
        return f"#{row['id']} · {employee[:24]} · {suffix}"
    except Exception:
        return f"#{row['id']} · TG {row['user_id']} · ошибка чтения"


def _sessions_markup(rows, page, total, page_size=8):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in rows:
        markup.row(telebot.types.KeyboardButton(f"Сессия {row['id']}"))
    nav = []
    if page > 0:
        nav.append(telebot.types.KeyboardButton(f"◀️ Страница {page}"))
    if (page + 1) * page_size < total:
        nav.append(telebot.types.KeyboardButton(f"Страница {page + 2} ▶️"))
    if nav:
        markup.row(*nav)
    markup.row(telebot.types.KeyboardButton("Мои тренировки"))
    return markup


def _send_sessions(bot_client, chat_id, page=0):
    rows, total = _session_rows(page)
    page_size = 8
    start = page * page_size + 1 if total else 0
    end = min((page + 1) * page_size, total)
    lines = [
        "СЕССИИ ПОЛЬЗОВАТЕЛЕЙ",
        f"Показаны {start}–{end} из {total}.",
        "Выберите сессию кнопкой ниже.",
        "",
    ]
    lines.extend(_session_label(row) for row in rows)
    bot_client.send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=_sessions_markup(rows, page, total, page_size),
    )


def _legacy_voice_rows(row, session):
    """Best-effort recovery for sessions created before exact voice indexing."""
    manager_lines = [
        str(item.get("content", "")).strip()
        for item in (session.get("history") or [])
        if item.get("role") == "user" and str(item.get("content", "")).strip()
    ]
    remaining = Counter(manager_lines)
    if not remaining:
        return []
    with sqlite3.connect(_db_path()) as db:
        db.row_factory = sqlite3.Row
        candidates = db.execute(
            "SELECT id,text,raw_text FROM inbox "
            "WHERE user_id=? AND raw_text IS NOT NULL AND text IS NOT NULL AND raw_text<>text "
            "ORDER BY id",
            (row["user_id"],),
        ).fetchall()
    matched = []
    for item in candidates:
        transcript = str(item["text"] or "").strip()
        if remaining[transcript] > 0:
            matched.append(item)
            remaining[transcript] -= 1
    return matched


def _voice_rows_for_session(session_id):
    """Return exact new voice messages; fall back to transcript matching for legacy data."""
    row, session = _load_session(session_id)
    if not row or not session:
        return []
    _ensure_voice_map()
    try:
        with sqlite3.connect(_db_path()) as db:
            db.row_factory = sqlite3.Row
            exact = db.execute(
                "SELECT i.id,i.text,m.file_id AS raw_text FROM voice_session_map m "
                "JOIN inbox i ON i.id=m.inbox_id "
                "WHERE m.session_id=? AND m.user_id=? ORDER BY i.id",
                (int(session_id), row["user_id"]),
            ).fetchall()
        if exact:
            return exact
    except sqlite3.OperationalError:
        pass
    return _legacy_voice_rows(row, session)


def _session_card(session_id):
    row, session = _load_session(session_id)
    if not row:
        return "Сессия не найдена."
    if not session:
        return f"СЕССИЯ #{session_id}\nНе удалось прочитать сохранённые данные."
    fields = session.get("fields", {})
    employee = session.get("employee", {}).get("name") or "ФИО не указано"
    score = _score(session)
    identity = (session.get("card") or {}).get("identity", {})
    focus = session.get("training_focus") or "не указан"
    voices = _voice_rows_for_session(session_id)
    lines = [
        f"СЕССИЯ #{session_id}",
        f"Пользователь: {employee}",
        f"Telegram ID: {row['user_id']}",
        f"Статус: {session.get('phase', 'неизвестно')}",
        f"Итог: {score}/100" if score is not None else "Итог: разбор ещё не сформирован",
        f"Навык/фокус: {focus}",
        f"Клиент/сценарий: {fields.get('customer') or 'не указан'}",
        f"Продукт: {fields.get('product') or 'не указан'}",
        f"Цель: {fields.get('goal') or 'не указана'}",
        f"Сложность: {fields.get('difficulty') or 'не указана'}",
    ]
    if identity:
        bits = [identity.get("name"), identity.get("job_title"), identity.get("company")]
        bits = [str(v).strip() for v in bits if str(v or "").strip()]
        if bits:
            lines.append("Карточка клиента: " + " · ".join(bits))
    lines.append(f"Реплик в диалоге: {len(session.get('history') or [])}")
    lines.append(f"Сохранённых голосовых менеджера: {len(voices)}")
    return "\n".join(lines)


def _session_markup(session_id):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        telebot.types.KeyboardButton(f"Диалог {session_id}"),
        telebot.types.KeyboardButton(f"Голосовые {session_id}"),
    )
    markup.row(telebot.types.KeyboardButton("Сессии пользователей"))
    markup.row(telebot.types.KeyboardButton("Мои тренировки"))
    return markup


def _send_session(bot_client, chat_id, session_id):
    bot_client.send_message(chat_id, _session_card(session_id), reply_markup=_session_markup(session_id))


def _send_dialog(bot_client, chat_id, session_id):
    row, session = _load_session(session_id)
    if not row:
        bot_client.send_message(chat_id, "Сессия не найдена.")
        return
    if not session:
        bot_client.send_message(chat_id, f"Сессия #{session_id}: не удалось прочитать данные.")
        return
    history = session.get("history") or []
    if not history:
        bot_client.send_message(chat_id, f"Сессия #{session_id}: диалог пуст.", reply_markup=_session_markup(session_id))
        return
    lines = [f"ДИАЛОГ СЕССИИ #{session_id}"]
    for index, item in enumerate(history, 1):
        role = item.get("role")
        if role == "user":
            speaker = "👤 Менеджер"
        elif role == "assistant":
            speaker = "🤖 Клиент"
        else:
            continue
        content = str(item.get("content", "")).strip()
        if content:
            lines.append(f"{index}. {speaker}: {content}")
    parts = chunks("\n\n".join(lines), limit=3800)
    for part in parts[:-1]:
        bot_client.send_message(chat_id, part)
    bot_client.send_message(chat_id, parts[-1], reply_markup=_session_markup(session_id))


def _send_voices(bot_client, chat_id, session_id):
    row, session = _load_session(session_id)
    if not row:
        bot_client.send_message(chat_id, "Сессия не найдена.")
        return
    voices = _voice_rows_for_session(session_id)
    if not voices:
        bot_client.send_message(
            chat_id,
            f"Сессия #{session_id}: сохранённых голосовых менеджера не найдено. "
            "Если менеджер писал текстом, весь разговор доступен через «Диалог».\n"
            "Для старых голосовых воспроизведение возможно только когда сохранился исходный Telegram file_id.",
            reply_markup=_session_markup(session_id),
        )
        return
    bot_client.send_message(chat_id, f"ГОЛОСОВЫЕ СЕССИИ #{session_id}: {len(voices)} шт.")
    sent = 0
    for index, voice in enumerate(voices, 1):
        transcript = str(voice["text"] or "").strip()
        caption = f"Голосовое {index}/{len(voices)}"
        if transcript:
            excerpt = transcript if len(transcript) <= 700 else transcript[:697] + "…"
            caption += f"\nРасшифровка: {excerpt}"
        try:
            bot_client.send_voice(chat_id, voice["raw_text"], caption=caption)
            sent += 1
        except Exception as exc:
            LOG.warning("Voice replay failed session=%s inbox_id=%s kind=%s", session_id, voice["id"], type(exc).__name__)
            bot_client.send_message(chat_id, caption + "\n⚠️ Оригинал аудио уже недоступен Telegram.")
    bot_client.send_message(
        chat_id,
        f"Готово. Воспроизведено голосовых: {sent} из {len(voices)}.",
        reply_markup=_session_markup(session_id),
    )


def _handle_admin_text(bot_client, message):
    if message.chat.id not in _admin_ids():
        return False
    raw = (message.text or "").strip()
    normalized = raw.lower().replace("ё", "е")

    if normalized in ("/sessions", "сессии пользователей"):
        _send_sessions(bot_client, message.chat.id, 0)
        return True

    match = re.fullmatch(r"#?\s*(\d+)", raw)
    if not match:
        match = re.fullmatch(r"сессия\s+#?\s*(\d+)", normalized)
    if match:
        _send_session(bot_client, message.chat.id, int(match.group(1)))
        return True

    match = re.fullmatch(r"диалог\s+#?\s*(\d+)", normalized)
    if match:
        _send_dialog(bot_client, message.chat.id, int(match.group(1)))
        return True

    match = re.fullmatch(r"голосов(?:ые|ая|ое)?\s+#?\s*(\d+)", normalized)
    if match:
        _send_voices(bot_client, message.chat.id, int(match.group(1)))
        return True

    match = re.fullmatch(r"страница\s+(\d+)\s*▶?️?", normalized)
    if match:
        _send_sessions(bot_client, message.chat.id, max(0, int(match.group(1)) - 1))
        return True

    match = re.fullmatch(r"◀?️?\s*страница\s+(\d+)", normalized)
    if match:
        _send_sessions(bot_client, message.chat.id, max(0, int(match.group(1)) - 1))
        return True

    return False


def _patched_message_handler(self, *args, **kwargs):
    original_decorator = _original_message_handler(self, *args, **kwargs)

    def decorator(handler):
        if handler.__name__ != "on_text":
            return original_decorator(handler)

        @functools.wraps(handler)
        def wrapped(message):
            try:
                if _handle_admin_text(self, message):
                    return None
            except Exception:
                LOG.exception("Text admin session viewer failed")
                try:
                    self.send_message(message.chat.id, "Не удалось открыть сессию. Попробуйте ещё раз.")
                except Exception:
                    pass
                return None
            return handler(message)

        return original_decorator(wrapped)

    return decorator


telebot.TeleBot.edit_message_text = _resilient_edit_message_text
telebot.TeleBot.message_handler = _patched_message_handler

import bot  # noqa: E402  (patches must be installed before bot.main registers handlers)


if __name__ == "__main__":
    bot.main()

"""Production observability wrapper.

Adds structured timing/lifecycle logs around the existing application without
changing simulation, scoring, persistence, Telegram, or OpenAI behavior.
No prompts, message text, audio, tokens, or secrets are written to telemetry.
"""
import contextvars
import logging
import re
import time

from academy.ai import AI
from academy.domain import score_level
from academy.engine import Engine
from academy.pacing import FOCUS_OBJECTIONS
from academy.telemetry import record, summary_text

LOG = logging.getLogger("academy.telemetry")
_INSTALLED = False
_CTX_USER = contextvars.ContextVar("telemetry_user", default=None)
_CTX_SESSION = contextvars.ContextVar("telemetry_session", default=None)


def _ms(start):
    return int((time.monotonic() - start) * 1000)


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_request = AI.request
    original_evaluate = AI.evaluate
    original_handle = Engine.handle

    def observed_request(self, name, instructions, payload, schema, model=None, max_output_tokens=6000):
        started = time.monotonic()
        selected_model = model or self.model
        try:
            result = original_request(
                self, name, instructions, payload, schema,
                model=model, max_output_tokens=max_output_tokens,
            )
            duration = _ms(started)
            LOG.info("telemetry event=ai_request stage=%s model=%s status=ok duration_ms=%s",
                     name, selected_model, duration)
            record("ai_request", user_id=_CTX_USER.get(), session_id=_CTX_SESSION.get(),
                   stage=name, model=selected_model, status="ok", duration_ms=duration)
            return result
        except Exception as exc:
            duration = _ms(started)
            LOG.warning("telemetry event=ai_request stage=%s model=%s status=error duration_ms=%s error_type=%s",
                        name, selected_model, duration, type(exc).__name__)
            record("ai_request", user_id=_CTX_USER.get(), session_id=_CTX_SESSION.get(),
                   stage=name, model=selected_model, status="error", duration_ms=duration)
            raise

    def observed_evaluate(self, session):
        started = time.monotonic()
        session_id = session.get("id") if isinstance(session, dict) else None
        token = _CTX_SESSION.set(session_id or _CTX_SESSION.get())
        try:
            result = original_evaluate(self, session)
            diagnostics = len(session.get("evaluation_diagnostics", [])) if isinstance(session, dict) else 0
            duration = _ms(started)
            LOG.info("telemetry event=evaluation session_id=%s model=%s status=ok duration_ms=%s diagnostics=%s",
                     session_id, self.eval_model, duration, diagnostics)
            record("evaluation", user_id=_CTX_USER.get(), session_id=session_id,
                   model=self.eval_model, status="ok", duration_ms=duration, diagnostics=diagnostics)
            return result
        except Exception as exc:
            diagnostics = len(session.get("evaluation_diagnostics", [])) if isinstance(session, dict) else 0
            duration = _ms(started)
            LOG.warning("telemetry event=evaluation session_id=%s model=%s status=error duration_ms=%s diagnostics=%s error_type=%s",
                        session_id, self.eval_model, duration, diagnostics, type(exc).__name__)
            record("evaluation", user_id=_CTX_USER.get(), session_id=session_id,
                   model=self.eval_model, status="error", duration_ms=duration, diagnostics=diagnostics)
            raise
        finally:
            _CTX_SESSION.reset(token)

    def observed_handle(self, event):
        started = time.monotonic()
        user_id = event.get("user_id")
        event_id = event.get("id")
        before = None
        try:
            before = self.store.current(user_id) if user_id is not None else None
        except Exception:
            pass
        before_phase = before.get("phase") if isinstance(before, dict) else None
        before_session = before.get("id") if isinstance(before, dict) else None
        user_token = _CTX_USER.set(user_id)
        session_token = _CTX_SESSION.set(before_session)
        try:
            result = original_handle(self, event)
            after = self.store.current(user_id) if user_id is not None else None
            after_phase = after.get("phase") if isinstance(after, dict) else None
            after_session = after.get("id") if isinstance(after, dict) else None
            turns = len(after.get("history") or []) // 2 if isinstance(after, dict) else 0
            duration = _ms(started)
            LOG.info("telemetry event=engine_handle event_id=%s user_id=%s session_id=%s phase_before=%s phase_after=%s turns=%s status=ok duration_ms=%s",
                     event_id, user_id, after_session or before_session, before_phase, after_phase, turns, duration)
            record("engine_handle", user_id=user_id, session_id=after_session or before_session,
                   status="ok", duration_ms=duration, phase_before=before_phase, phase_after=after_phase)
            if before_phase != after_phase:
                LOG.info("telemetry event=phase_change user_id=%s session_id=%s from=%s to=%s",
                         user_id, after_session or before_session, before_phase, after_phase)
                record("phase_change", user_id=user_id, session_id=after_session or before_session,
                       status="ok", phase_before=before_phase, phase_after=after_phase)
            return result
        except Exception as exc:
            duration = _ms(started)
            LOG.warning("telemetry event=engine_handle event_id=%s user_id=%s session_id=%s phase_before=%s status=error duration_ms=%s error_type=%s",
                        event_id, user_id, before_session, before_phase, duration, type(exc).__name__)
            record("engine_handle", user_id=user_id, session_id=before_session,
                   status="error", duration_ms=duration, phase_before=before_phase)
            raise
        finally:
            _CTX_SESSION.reset(session_token)
            _CTX_USER.reset(user_token)

    AI.request = observed_request
    AI.evaluate = observed_evaluate
    Engine.handle = observed_handle


install()

import launcher  # noqa: E402

_original_admin_text = launcher._handle_admin_text
_original_session_markup = launcher._session_markup
_original_session_card = launcher._session_card


def _session_markup_with_report(session_id):
    markup = _original_session_markup(session_id)
    markup.row(launcher.telebot.types.KeyboardButton(f"Отчёт руководителю {session_id}"))
    return markup


def _session_card_human(session_id):
    text = _original_session_card(session_id)
    row, session = launcher._load_session(session_id)
    if not row or not session:
        return text
    focus = session.get("training_focus")
    focus_label = FOCUS_OBJECTIONS.get(focus, {}).get("label")
    if focus_label and focus:
        text = text.replace(f"Навык/фокус: {focus}", f"Навык/фокус: {focus_label}")
    difficulty = (session.get("fields") or {}).get("difficulty")
    difficulty_label = {"easy": "1 — лёгкая", "medium": "2 — средняя", "hard": "3 — сложная"}.get(difficulty)
    if difficulty_label and difficulty:
        text = text.replace(f"Сложность: {difficulty}", f"Сложность: {difficulty_label}")
    score = launcher._score(session)
    if score is not None:
        text = text.replace(f"Итог: {score}/100", f"Итог: {score}/100 · {score_level(score)}")
    return text


def _send_supervisor_report(bot_client, chat_id, session_id):
    row, session = launcher._load_session(session_id)
    if not row:
        bot_client.send_message(chat_id, "Сессия не найдена.")
        return
    if not session:
        bot_client.send_message(chat_id, f"Сессия #{session_id}: не удалось прочитать данные.")
        return
    if not session.get("report_data"):
        bot_client.send_message(
            chat_id,
            f"Сессия #{session_id}: разбор ещё не сформирован, поэтому отчёт руководителю пока недоступен.",
            reply_markup=_session_markup_with_report(session_id),
        )
        return
    from academy.pdf_report import render_pdf
    fileobj = render_pdf(session, "supervisor")
    fileobj.seek(0)
    bot_client.send_document(
        chat_id,
        fileobj,
        visible_file_name=f"Отчёт_руководителю_{session_id}.pdf",
    )
    bot_client.send_message(
        chat_id,
        f"Отчёт руководителю по сессии #{session_id} готов.",
        reply_markup=_session_markup_with_report(session_id),
    )


def _safe_admin_text(bot_client, message):
    raw = (getattr(message, "text", None) or "").strip()
    normalized = raw.lower().replace("ё", "е")
    is_admin = message.chat.id in launcher._admin_ids()
    if normalized in ("метрики", "статистика", "/metrics"):
        if is_admin:
            bot_client.send_message(message.chat.id, summary_text())
            return True
    match = re.fullmatch(r"отчет руководителю\s+#?\s*(\d+)", normalized)
    if match and is_admin:
        _send_supervisor_report(bot_client, message.chat.id, int(match.group(1)))
        return True
    # Difficulty buttons must always reach the trainer, never admin session navigation.
    if re.fullmatch(r"\d+", raw):
        return False
    return _original_admin_text(bot_client, message)


launcher._session_markup = _session_markup_with_report
launcher._session_card = _session_card_human
launcher._handle_admin_text = _safe_admin_text


if __name__ == "__main__":
    launcher.bot.main()

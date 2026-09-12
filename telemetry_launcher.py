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
from academy.engine import Engine
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


def _safe_admin_text(bot_client, message):
    raw = (getattr(message, "text", None) or "").strip()
    normalized = raw.lower().replace("ё", "е")
    if normalized in ("метрики", "статистика", "/metrics"):
        if message.chat.id in launcher._admin_ids():
            bot_client.send_message(message.chat.id, summary_text())
            return True
    # Difficulty buttons must always reach the trainer, never admin session navigation.
    if re.fullmatch(r"\d+", raw):
        return False
    return _original_admin_text(bot_client, message)


launcher._handle_admin_text = _safe_admin_text


if __name__ == "__main__":
    launcher.bot.main()

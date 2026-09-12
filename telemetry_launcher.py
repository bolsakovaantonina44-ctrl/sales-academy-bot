"""Production observability wrapper.

Adds structured timing/lifecycle logs around the existing application without
changing simulation, scoring, persistence, Telegram, or OpenAI behavior.
No prompts, message text, tokens, or secrets are written to telemetry logs.
"""
import logging
import re
import time

from academy.ai import AI
from academy.engine import Engine

LOG = logging.getLogger("academy.telemetry")
_INSTALLED = False


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
            LOG.info(
                "telemetry event=ai_request stage=%s model=%s status=ok duration_ms=%s",
                name, selected_model, _ms(started),
            )
            return result
        except Exception as exc:
            LOG.warning(
                "telemetry event=ai_request stage=%s model=%s status=error duration_ms=%s error_type=%s",
                name, selected_model, _ms(started), type(exc).__name__,
            )
            raise

    def observed_evaluate(self, session):
        started = time.monotonic()
        session_id = session.get("id") if isinstance(session, dict) else None
        try:
            result = original_evaluate(self, session)
            diagnostics = len(session.get("evaluation_diagnostics", [])) if isinstance(session, dict) else 0
            LOG.info(
                "telemetry event=evaluation session_id=%s model=%s status=ok duration_ms=%s diagnostics=%s",
                session_id, self.eval_model, _ms(started), diagnostics,
            )
            return result
        except Exception as exc:
            diagnostics = len(session.get("evaluation_diagnostics", [])) if isinstance(session, dict) else 0
            LOG.warning(
                "telemetry event=evaluation session_id=%s model=%s status=error duration_ms=%s diagnostics=%s error_type=%s",
                session_id, self.eval_model, _ms(started), diagnostics, type(exc).__name__,
            )
            raise

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
        try:
            result = original_handle(self, event)
            after = self.store.current(user_id) if user_id is not None else None
            after_phase = after.get("phase") if isinstance(after, dict) else None
            after_session = after.get("id") if isinstance(after, dict) else None
            turns = 0
            if isinstance(after, dict):
                turns = len(after.get("history") or []) // 2
            LOG.info(
                "telemetry event=engine_handle event_id=%s user_id=%s session_id=%s phase_before=%s phase_after=%s turns=%s status=ok duration_ms=%s",
                event_id, user_id, after_session or before_session, before_phase, after_phase, turns, _ms(started),
            )
            if before_phase != after_phase:
                LOG.info(
                    "telemetry event=phase_change user_id=%s session_id=%s from=%s to=%s",
                    user_id, after_session or before_session, before_phase, after_phase,
                )
            return result
        except Exception as exc:
            LOG.warning(
                "telemetry event=engine_handle event_id=%s user_id=%s session_id=%s phase_before=%s status=error duration_ms=%s error_type=%s",
                event_id, user_id, before_session, before_phase, _ms(started), type(exc).__name__,
            )
            raise

    AI.request = observed_request
    AI.evaluate = observed_evaluate
    Engine.handle = observed_handle


install()

# launcher.py keeps the existing production patches/admin tools. Import it after
# telemetry patches are installed.
import launcher  # noqa: E402

# Admin session navigation historically accepted a bare number such as "22".
# That conflicts with the trainer's difficulty buttons 1/2/3 for admins: "2"
# was being interpreted as "open session #2". Bare numbers must always continue
# to the trainer; admin sessions remain available via "Сессия 22" or "#22".
_original_admin_text = launcher._handle_admin_text


def _safe_admin_text(bot_client, message):
    raw = (getattr(message, "text", None) or "").strip()
    if re.fullmatch(r"\d+", raw):
        return False
    return _original_admin_text(bot_client, message)


launcher._handle_admin_text = _safe_admin_text


if __name__ == "__main__":
    launcher.bot.main()

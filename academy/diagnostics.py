"""Structured error logging without secrets, prompts, or raw user input."""
import logging
import os
import re
import traceback


def log_failure(event, session, stage, exc):
    message = str(exc)
    # API exception bodies may contain prompts. Keep only a safe status, while
    # local validation errors contain paths/codes rather than supplied values.
    if not isinstance(exc, ValueError):
        message = 'external_or_runtime_error status=' + str(getattr(exc, 'status_code', 'unknown'))
    message = re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{20,}', '[REDACTED]', message)
    message = re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', message)
    for key in ('TELEGRAM_TOKEN', 'OPENAI_API_KEY'):
        secret = os.getenv(key)
        if secret:
            message = message.replace(secret, '[REDACTED]')
    frames = ''.join(traceback.format_list(traceback.extract_tb(exc.__traceback__)))
    frames = re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{20,}', '[REDACTED]', frames)
    frames = re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', frames)
    for key in ('TELEGRAM_TOKEN', 'OPENAI_API_KEY'):
        if os.getenv(key):
            frames = frames.replace(os.environ[key], '[REDACTED]')
    logging.getLogger('academy').error(
        'event_id=%s session_id=%s stage=%s exception=%s message=%s\nTraceback:\n%s',
        event.get('id'), session.get('id'), getattr(exc, 'stage', stage), type(exc).__name__, message[:500], frames)

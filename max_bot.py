"""Development/pilot MAX entrypoint.

Uses MAX long polling for development only. Production must switch to HTTPS Webhook.
Telegram production is not modified by this file.
"""
import logging
import os
import time
from pathlib import Path

from openai import OpenAI

from academy.ai import AI
from academy.engine import Engine, deliver
from academy.max_transport import MaxClient, normalize_updates
from academy.store import Store


LOG = logging.getLogger("academy.max")


def process_pending(store, engine, send):
    """Process all queued text events sequentially using the existing Academy engine."""
    while True:
        event = store.claim()
        if event is None:
            break
        try:
            engine.handle(event)
        except Exception as exc:
            LOG.exception("MAX event failed id=%s", event.get("id"))
            store.fail(event, type(exc).__name__)
        try:
            deliver(store, event["user_id"], send)
        except Exception:
            LOG.exception("MAX delivery failed user=%s", event["user_id"])
            store.defer(event)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("MAX_TOKEN")
    openai_key = os.getenv("OPENAI_API_KEY")
    if not token:
        raise RuntimeError("MAX_TOKEN is not set")
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    # Separate DB avoids Telegram/MAX numeric user ID collisions during the pilot.
    db_path = os.getenv("MAX_DB_PATH", "./data/max_academy.sqlite3")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    store = Store(db_path)
    store.recover()
    client = MaxClient(token)

    openai_client = OpenAI(api_key=openai_key, timeout=90, max_retries=1)
    model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    ai = AI(
        openai_client,
        model,
        os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
        os.getenv("OPENAI_EVAL_MODEL", model),
    )
    admins = [
        int(value.strip())
        for value in os.getenv("MAX_ADMIN_IDS", "").split(",")
        if value.strip()
    ]
    engine = Engine(store, ai, limit=int(os.getenv("FREE_TRAININGS", "3")), admin_ids=admins)

    def send(user_id, body):
        client.send_text(user_id, body)

    me = client.get_me()
    LOG.info("MAX Academy pilot started bot=%s id=%s", me.get("username"), me.get("user_id"))

    marker = None
    while True:
        try:
            payload = client.get_updates(marker=marker, timeout=30, limit=100)
            marker = payload.get("marker", marker)
            for inbound in normalize_updates(payload):
                store.enqueue(
                    inbound.event_key,
                    inbound.user_id,
                    inbound.chat_id,
                    "text",
                    inbound.text,
                )
            process_pending(store, engine, send)
            for user_id in store.pending_users():
                try:
                    deliver(store, user_id, send)
                except Exception:
                    LOG.exception("MAX pending delivery failed user=%s", user_id)
        except KeyboardInterrupt:
            break
        except Exception:
            LOG.exception("MAX polling cycle failed")
            time.sleep(2)


if __name__ == "__main__":
    main()

"""Minimal MAX Bot API transport for the Academy pilot.

The application core remains channel-agnostic. This module only:
- calls MAX HTTP API;
- normalizes message_created updates into Academy inbox events;
- keeps transport-specific identifiers out of the engine.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

API_BASE = "https://platform-api2.max.ru"


class MaxAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaxInboundText:
    event_key: str
    user_id: int
    chat_id: int
    text: str


class MaxClient:
    def __init__(self, token: str, base_url: str = API_BASE, timeout: int = 95):
        if not token:
            raise ValueError("MAX token is required")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, query=None, body=None):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = None
        headers = {"Authorization": self.token}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except Exception as exc:
            raise MaxAPIError(type(exc).__name__) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise MaxAPIError("Invalid JSON response") from exc

    def get_me(self):
        return self._request("GET", "/me")

    def get_updates(self, marker=None, timeout=30, limit=100):
        query = {
            "timeout": int(timeout),
            "limit": int(limit),
            "types": "message_created",
        }
        if marker is not None:
            query["marker"] = int(marker)
        return self._request("GET", "/updates", query=query)

    def send_text(self, user_id: int, text: str):
        text = str(text)
        # MAX accepts up to 4000 characters in a text message.
        results = []
        for part in split_text(text, 3900):
            results.append(
                self._request(
                    "POST",
                    "/messages",
                    query={"user_id": int(user_id)},
                    body={"text": part},
                )
            )
        return results


def split_text(text: str, limit: int = 3900):
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    parts = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [part for part in parts if part]


def normalize_message_created(update: dict[str, Any]) -> MaxInboundText | None:
    """Return a text event for a direct user message; ignore unsupported updates."""
    if not isinstance(update, dict) or update.get("update_type") != "message_created":
        return None
    message = update.get("message") or {}
    sender = message.get("sender") or {}
    body = message.get("body") or {}
    try:
        user_id = int(sender["user_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if sender.get("is_bot"):
        return None
    text = str(body.get("text") or "").strip()
    if not text:
        return None
    mid = str(body.get("mid") or "")
    if not mid:
        # timestamp is only a fallback for malformed/legacy payloads.
        stamp = update.get("timestamp") or message.get("timestamp")
        if stamp is None:
            return None
        mid = f"ts-{stamp}"
    return MaxInboundText(
        event_key=f"max:{user_id}:{mid}",
        user_id=user_id,
        chat_id=user_id,
        text=text,
    )


def normalize_updates(payload: dict[str, Any]) -> Iterable[MaxInboundText]:
    for update in payload.get("updates") or []:
        event = normalize_message_created(update)
        if event is not None:
            yield event

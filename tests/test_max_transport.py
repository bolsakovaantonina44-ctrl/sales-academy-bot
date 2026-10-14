from academy.max_transport import normalize_message_created, normalize_updates, split_text


def test_normalizes_direct_text_message():
    update = {
        "update_type": "message_created",
        "timestamp": 1770000000000,
        "message": {
            "sender": {"user_id": 555, "is_bot": False, "first_name": "Ирина"},
            "recipient": {"chat_id": 777},
            "body": {"mid": "mid.abc-1", "text": "  /start  ", "attachments": []},
        },
    }
    event = normalize_message_created(update)
    assert event.user_id == 555
    assert event.chat_id == 555
    assert event.text == "/start"
    assert event.event_key == "max:555:mid.abc-1"


def test_ignores_non_text_and_bot_messages():
    assert normalize_message_created({"update_type": "message_callback"}) is None
    assert normalize_message_created({
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": 1, "is_bot": True},
            "body": {"mid": "x", "text": "hello"},
        },
    }) is None
    assert normalize_message_created({
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": 2, "is_bot": False},
            "body": {"mid": "x", "text": "", "attachments": [{"type": "audio"}]},
        },
    }) is None


def test_normalize_updates_filters_unsupported_items():
    payload = {
        "updates": [
            {"update_type": "bot_started"},
            {
                "update_type": "message_created",
                "message": {
                    "sender": {"user_id": 9, "is_bot": False},
                    "body": {"mid": "m1", "text": "Новая тренировка"},
                },
            },
        ],
        "marker": 10,
    }
    result = list(normalize_updates(payload))
    assert len(result) == 1
    assert result[0].text == "Новая тренировка"


def test_split_text_respects_limit_and_preserves_content():
    original = ("слово " * 1000).strip()
    parts = split_text(original, 200)
    assert parts
    assert all(len(part) <= 200 for part in parts)
    assert " ".join(parts).split() == original.split()

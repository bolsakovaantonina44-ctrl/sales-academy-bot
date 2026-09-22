# MAX channel pilot

This branch adds a second transport for the Sales Academy without changing Telegram production.

## Architecture

```
Academy core (Engine / AI / reports / scenarios)
├── Telegram transport — existing bot.py
└── MAX transport — max_bot.py + academy/max_transport.py
```

During the pilot MAX uses a separate SQLite database (`MAX_DB_PATH`). This is intentional:
Telegram and MAX both use numeric user IDs and the current schema does not yet have a
`channel` namespace. Keeping the databases separate prevents accidental identity/history
collisions. A later platform migration should introduce canonical company/user identities
and explicit channel account links.

## Current pilot scope

Implemented:
- MAX Bot API client using the current `platform-api2.max.ru` endpoint;
- Authorization header;
- development long polling;
- `message_created` text normalization;
- existing Academy Engine for /start, setup, training dialogue, finish and text report;
- independent MAX admin IDs and database;
- deduplicated inbound events by MAX message id.

Not implemented yet:
- MAX voice/audio download and transcription;
- inline/reply keyboards;
- PDF/file upload delivery;
- supervisor/company navigation parity;
- production webhook endpoint.

## Environment

```
MAX_TOKEN=...
OPENAI_API_KEY=...
MAX_DB_PATH=/data/max_academy.sqlite3
MAX_ADMIN_IDS=12345
```

For a local development check:

```
python max_bot.py
```

Long polling is only for development/pilot verification. Production MAX deployment must
use an HTTPS webhook subscription and validate `X-Max-Bot-Api-Secret`.

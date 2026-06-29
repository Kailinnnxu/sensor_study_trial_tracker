# Participant Touchpoint Tracker

Tracks participant touchpoints across a multi-step study workflow and reminds the researcher at fixed intervals after a triggering anchor event.

## Architecture

Touchpoints and ingestion sources are **configuration**, not separate code paths:

| Concept | Where it lives |
|---------|----------------|
| Touchpoint definitions | `tracker/config.py` |
| Ingestion sources | `tracker/config.py` |
| Action handlers | `tracker/engine/actions.py` (register new handlers in `ACTION_HANDLERS`) |
| Anchor events, status, email idempotency | SQLite (`tracker/db.py`) |

### Phase 1 (fully implemented)

- **Anchor:** `assessment_complete` — manual entry or HAI email ingestion
- **Touchpoint:** `schedule_home_visit` — offsets 0, 3, 7 days — action `email_kailin`

### Phase 2 (structure + stub)

- **Anchor:** `sensor_collection_start` — sensor trigger email ingestion
- **Touchpoint:** `sensor_dropoff_reminder` — offset 9 days — action `webex_call` (stubbed)

## Setup

```bash
cd Tracker
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # edit with your values
```

### Gmail OAuth (one-time)

1. Create a Google Cloud project and enable the Gmail API.
2. Download OAuth client credentials JSON to `credentials/gmail_credentials.json`.
3. Run:

```bash
python scripts/setup_gmail_oauth.py
```

Token is saved to `credentials/gmail_token.json` (gitignored).

### Initialize database

The database is created automatically on first run. To initialize explicitly:

```python
from tracker.db import init_db
init_db()
```

## Running

### Web dashboard

```bash
flask --app tracker.web.app run
```

Open http://localhost:5000 — list participants, add anchor events, mark touchpoints done/undo.

### Deploy to Railway

See **[DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md)** for web + daily cron setup with a persistent volume.

### Daily jobs (cron / Task Scheduler)

Run **ingestion first**, then **reminders**:

```bash
python scripts/run_ingestion.py
python scripts/run_reminders.py
```

Or use the combined script:

```bash
python scripts/run_daily.py
```

Example Windows Task Scheduler: run `run_daily.py` once per day before business hours.

## Adding a new touchpoint (Phase 3+)

1. Add a `TouchpointDefinition` in `tracker/config.py`.
2. If needed, add an `IngestionSource` and a parser in `tracker/ingestion/parsers.py`.
3. If needed, add an action handler and register it in `ACTION_HANDLERS`.

No database migration, engine loop changes, or dashboard code changes required.

## Tests

```bash
pip install pytest
pytest
```

## Security / compliance

- **Study ID only** — no participant names, phone numbers, or other PHI.
- Credentials via environment variables (see `.env.example`).
- Gmail token and database are local files, not committed to source control.

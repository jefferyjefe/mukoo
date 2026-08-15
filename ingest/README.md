# mukoo-ingest

Flask ingestion API for Mukoo. Accepts batches of cellular-signal measurements
from the field logger and writes them to PostGIS. Ingestion is **idempotent**:
each measurement carries a client-generated `sample_id`, and re-sent samples are
skipped rather than duplicated or rejected.

## Endpoint

### `POST /v1/measurements`

Request body:

```json
{
  "session_id": "0e6f…",
  "carrier": "Verizon",
  "samples": [
    {
      "sample_id": "6b1e…",
      "recorded_at": "2026-07-07T09:41:12-04:00",
      "lat": 32.4488,
      "lon": -81.7832,
      "network_type": "LTE",
      "rsrp": -95.0,
      "rsrq": -10.5,
      "sinr": 4.0,
      "cell_id": "310-410-0001",
      "speed_mps": 12.5,
      "heading_deg": 270.0
    },
    {
      "sample_id": "9c2f…",
      "recorded_at": "2026-07-07T09:41:14-04:00",
      "lat": 32.4495,
      "lon": -81.7810,
      "network_type": "none",
      "rsrp": null,
      "rsrq": null,
      "sinr": null,
      "cell_id": null
    }
  ]
}
```

- `session_id` and `carrier` are batch-level and applied to every sample; either
  may be overridden per-sample.
- `rsrp` / `rsrq` / `sinr` / `cell_id` are optional. A sample with
  `network_type: "none"` and null metrics is **valid dead-zone data**.
- `modem_reported_at` is optional — the modem's own timestamp for the reading.
  Samples sharing it within a session are re-reads of one measurement. Omit it
  and the sample is still valid: older logger builds and modems that report no
  timestamp upload unchanged, and the column is nullable.
- Unknown keys are **rejected** (`400`), so a client typo surfaces instead of
  silently landing as a null.

Response (`200 OK`):

```json
{ "received": 2, "inserted": 2, "skipped": 0 }
```

- `received` — samples in the payload.
- `inserted` — newly written rows.
- `skipped` — samples already present (from this batch or an earlier one).

A partial-duplicate batch never fails as a whole: new samples are inserted and
duplicates are skipped, via `INSERT … ON CONFLICT (sample_id) DO NOTHING`.

Invalid payloads (bad UUID, unknown `network_type`, out-of-range coordinate,
empty `samples`) return `400` with details.

### `GET /v1/stats`

The ingested dataset at a glance (read-only): totals, distinct sessions,
dead-zone count, RSRP min/max/avg, bounding box, `last_received_at`, and a
chronological `per_session` breakdown (samples, time window, dead zones, RSRP
stats per drive). Empty table returns zeros/nulls, still `200`.

It also reports **latched re-reads** — consecutive samples in a session that
re-report one modem reading instead of observing something new, identified by a
shared `modem_reported_at` or by an unchanged `(rsrp, rsrq, sinr)` triple:

```json
"rereads": { "rows": 2703, "share": 0.8636,
             "independent_rows": 427, "with_modem_timestamp": 0 }
```

`independent_rows` is the effective sample size — what the model has to work with
once runs collapse, and usually far below `total`. Per session, `modem_timestamps`
gives `rows` vs `distinct`: if those are equal the modem restamped every poll, so
only the value test is meaningful on that device.

```bash
curl -s localhost:8000/v1/stats | jq .
```

### `GET /healthz`

Returns `200` when the database is reachable, `503` otherwise.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e 'ingest[dev]'          # app + test deps (incl. alembic)
pip install -r db/requirements.txt    # migration runner

# Bring up PostGIS (see infra/) and apply migrations:
docker compose -f infra/docker-compose.yml up -d db
(cd db && DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo alembic upgrade head)

# Create the test database. The suite TRUNCATEs the measurements table around
# every test, so it refuses to run against any database whose name does not end
# in "_test" — pointing it at your dev database would destroy real field data.
psql -h localhost -U mukoo -d postgres -c 'CREATE DATABASE mukoo_test'

# Run the tests. They apply the db/ migrations to the test database themselves,
# so it only has to exist — no separate `alembic upgrade` needed.
TEST_DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo_test \
  pytest ingest -v

# Run the API locally:
DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo \
  flask --app mukoo_ingest.wsgi:app run
```

If `TEST_DATABASE_URL` is unset, the suite falls back to `DATABASE_URL`, sees a
name not ending in `_test`, and **skips** rather than failing — you get
`7 passed, 23 skipped` and no error. `make test` from the repo root sets the
variable for you and runs both suites.

## Layout

```
src/mukoo_ingest/
  app.py       Flask factory + routes
  service.py   idempotent batch insert (ON CONFLICT DO NOTHING)
  schemas.py   pydantic request validation
  models.py    SQLAlchemy Core table (mirrors the migration)
  db.py        engine construction
  config.py    env-sourced config
```

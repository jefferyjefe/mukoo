# Mukoo

Rural cellular coverage verification. Field devices drive routes and log signal
measurements (including where there is *no* signal); the backend ingests them
into PostGIS and models coverage across the region.

## Repository layout

| Path        | What it is |
|-------------|------------|
| [`ingest/`](ingest/) | Flask ingestion API — batch, idempotent measurement intake. Installable package `mukoo-ingest`. |
| [`model/`](model/)   | Coverage model. Separate installable package `mukoo-model`; ordinary kriging of RSRP (with cross-validation + GeoTIFF export) is implemented — `mukoo-krige`. |
| [`db/`](db/)         | Alembic migrations and schema — the source of truth for the database. |
| [`logger/`](logger/) | Android field-logger app. Placeholder. |
| [`infra/`](infra/)   | Docker Compose stack and configuration. |

## Data model

The first migration creates `measurements`, one row per reading:

- `sample_id` (UUID, client-generated, **unique** — the idempotency key)
- `session_id` (UUID, groups one drive)
- `recorded_at` (timestamptz, the phone's clock)
- `geom` (PostGIS `POINT`, SRID 4326) with a GiST spatial index
- `rsrp`, `rsrq`, `sinr` (numeric, **nullable** — null in a dead zone)
- `network_type` (`LTE` / `5G-NR` / `none`)
- `cell_id` (nullable), `speed_mps` / `heading_deg` (nullable)
- `carrier`
- `received_at` (timestamptz, server-side `now()` default)

## Quick start

```bash
# 1. Bring up PostGIS + run migrations + start the API.
docker compose -f infra/docker-compose.yml up --build

# API on http://localhost:8000 ; POST batches to /v1/measurements.
curl -s localhost:8000/healthz
```

To develop and test the API against a local PostGIS, see
[ingest/README.md](ingest/README.md).

## Migrations

`db/` is the schema's source of truth. Apply / create migrations with Alembic:

```bash
cd db
DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo alembic upgrade head
DATABASE_URL=… alembic revision -m "add something"   # new migration
```

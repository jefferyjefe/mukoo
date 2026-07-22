# Mukoo

**Mukoo is a rural cellular coverage verification system: it audits a carrier's
FCC-filed coverage claims against real, GPS-tagged field measurements — driving
the roads and checking whether the signal the carrier told the FCC it provides
is actually there.**

![Coverage vs. claim](docs/coverage_map.png)

## The finding

Verizon's FCC Broadband Data Collection (BDC) filing claims a minimum signal
level for every hexagon it says it covers. We drove **nine routes** (2026-07-18
to 07-21) across a **~16 × 14 km** survey area in one corner of a rural Georgia
county and logged **2,039 GPS-tagged signal measurements**. Then we compared
each measurement to the carrier's own filed claim for the exact spot it was
taken.

**82% of everything we measured came in below the carrier's own claim: 1,673 of
all 2,039 GPS-tagged measurements fell below the minimum signal Verizon's FCC
filing claims for the spot they were taken.** Restricting to the like-for-like
comparison — the 1,681 points that landed inside a hex Verizon actually claims to
cover — **99.5% (1,673 of 1,681) violated the claim.** (The remaining 358
measurements fell outside any claimed hex, where there is no claim to test.)

On average the violating points came in **22.1 dB below** the claimed floor; the
worst was **54 dB below** — a measured −104 dBm where the filing claims at least
−50 dBm.

The shortfall holds across every tier of the filing, including the strongest
claims:

| Claimed min RSRP | Points in tier | Below claim | Avg measured |
|-----------------:|---------------:|------------:|-------------:|
| −50 dBm          |             17 |          17 | −95.5 dBm    |
| −60 dBm          |             89 |          89 | −96.2 dBm    |
| −70 dBm          |            240 |         240 | −98.4 dBm    |
| −80 dBm          |            489 |         481 | −102.2 dBm   |
| −90 dBm          |            846 |         846 | −108.1 dBm   |

**Verified two independent ways, with identical results.** The audit is run by a
custom tool (`mukoo-claims`) that reads the BDC GeoPackage directly with the
Python standard library — no GDAL — and does point-in-hex tests with Shapely. As
a guard against a bug in that hand-written path, the same join was reproduced
from scratch with a completely different stack: GDAL (`ogr2ogr`) loading the
hexes into PostGIS, and a SQL `ST_Contains` spatial join. Both paths return the
same 1,681 in-claim points, the same 1,673 violations, the same −22.09 dB
average gap, and the same −54.0 dB worst gap.

This is the kind of ground-truth evidence an FCC coverage-availability challenge
is built on. *No challenge has been filed* — this repository is the measurement
and analysis pipeline behind the numbers.

## How it works

The pipeline runs end to end, from a phone in a car to a claims verdict:

1. **Collect** — an Android field-logger app records GPS-tagged signal readings
   (RSRP/RSRQ/SINR, cell ID, network type) as it drives, *including where there
   is no signal at all.*
2. **Ingest** — a Flask API takes batched, idempotent uploads and writes them
   into PostGIS, one row per reading.
3. **Model** — ordinary/regression kriging interpolates RSRP across the survey
   area and produces a calibrated uncertainty surface, cross-validated honestly
   (leave-one-drive-out and spatial-block, not just the flattering random fold).
4. **Suggest** — an active-learning step reads the uncertainty surface and
   proposes where the *next* drive should go to shrink model uncertainty
   fastest, exported as a routable GPX tour.
5. **Verify** — the claims tool compares every measurement to the carrier's BDC
   filing and reports where ground truth contradicts the claim.

## How the audit works

The comparison is deliberately like-for-like:

- **Metric.** For each measurement inside a claimed hex, `gap = measured RSRP −
  the hex's filed minsignal`. A negative gap is the carrier's own filing
  contradicted by ground truth at that exact location.
- **Environment filter.** BDC mobile claims are filed per *environment* —
  in-vehicle mobile (`environmnt = 1`) and outdoor stationary (`0`). Drive data
  is in-vehicle, so the audit compares only against the in-vehicle claim. We are
  not holding a car-window reading against a walking-speed promise.
- **Source of truth.** Claimed minimums come straight from the carrier's own
  BDC H3 GeoPackage — we are grading the filing against itself, not against an
  external coverage model.

**Honest limitations.** The interpolation and active-learning components apply
*established* geostatistics (kriging, expected-variance-reduction sampling)
rigorously — with honest cross-validation that currently shows the surface does
not yet generalize to unseen roads — rather than claiming novel research. The
contribution here is the end-to-end system and the audit it enables, not a new
algorithm. And this is one carrier over one small rural area; it demonstrates
the method and a real discrepancy, not a nationwide result.

## Repository layout

| Path        | What it is |
|-------------|------------|
| [`ingest/`](ingest/) | Flask ingestion API — batch, idempotent measurement intake. Installable package `mukoo-ingest`. |
| [`model/`](model/)   | Coverage model. Separate installable package `mukoo-model`: kriging of RSRP (cross-validation + GeoTIFF export, `mukoo-krige`), active-learning drive suggestions from the uncertainty surface (`mukoo-suggest`), and FCC claimed-coverage verification against BDC filings (`mukoo-claims`). |
| [`db/`](db/)         | Alembic migrations and schema — the source of truth for the database. |
| [`logger/`](logger/) | Android field-logger app. |
| [`infra/`](infra/)   | Docker Compose stack and configuration. |

### Data model

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

Reproduce the audit against a BDC filing:

```bash
pip install -e model
export DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo
mukoo-claims --gpkg ~/Downloads/bdc_13_131425_4GLTE_mobile_broadband_h3_*.gpkg
# -> claims_report.json (summary, per-tier breakdown, worst point)
# -> claims_violations.geojson (every violating measurement, worst first)
```

To develop and test the API against a local PostGIS, see
[ingest/README.md](ingest/README.md); for the full modelling toolkit
(`mukoo-krige` / `mukoo-suggest` / `mukoo-claims`), see
[model/README.md](model/README.md).

## Migrations

`db/` is the schema's source of truth. Apply / create migrations with Alembic:

```bash
cd db
DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo alembic upgrade head
DATABASE_URL=… alembic revision -m "add something"   # new migration
```

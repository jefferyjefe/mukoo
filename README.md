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

That second path is
[`docs/verify_claims_postgis.py`](docs/verify_claims_postgis.py) — run it
yourself. It shares no code with `mukoo-claims`: different GeoPackage reader,
different geometry engine, different spatial index, and the predicate is SQL
rather than Python. It also reports how many points sit exactly on a hex
boundary, where the two paths' containment rules differ; on this data, none do.

```bash
python docs/verify_claims_postgis.py --gpkg ~/Downloads/bdc_13_*.gpkg
```

Needs the `ogr2ogr` binary (`brew install gdal` / `apt install gdal-bin`); the
Python side uses only `psycopg2`, which arrives with `mukoo-ingest`.

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
   (leave-one-drive-out and spatial-block, not just the flattering random fold),
   and defined only where the measurements can actually speak for it.
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

### Published data & privacy

The evidence is published **aggregated to the carrier's own H3 claimed-coverage
hexes** — [`verizon_claim_violations_by_hex.geojson`](verizon_claim_violations_by_hex.geojson):
one feature per hex, carrying how many measurements fell inside it and how many
came in below the filed minimum. Raw per-measurement GPS traces are deliberately
kept local (gitignored), so the published data shows *which claimed cells failed,
and by how much* without mapping individual drives. Regenerate with
[`docs/aggregate_violations_by_hex.py`](docs/aggregate_violations_by_hex.py).

A hex is published only if it holds **at least three measurements** (155 of the
184 hexes with a violation). This is a statistical floor, not a privacy one:
every per-hex figure is a rate or a mean, and on one or two readings neither is
one — a single momentary fade would publish as a 100% violation rate reading with
the same authority as a cell backed by 73 readings. The audit is unaffected; the
headline counts above are computed over every measurement, and the file's header
carries both the audit totals and the published subset.

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
- `modem_reported_at` (timestamptz, **nullable**, added in `0002`) — the modem's
  own timestamp for the reading. Consecutive rows in a session that share it are
  re-reads of one measurement, not independent observations. Null for rows
  recorded before the column existed, and for any client that does not report
  one; the field is optional in the ingest schema so older logger builds upload
  unchanged.

### Latched readings

The logger samples every ~3 s but the modem refreshes its signal report far more
slowly, so one measurement can be re-read several times. Across the first 3,130
samples, **~84% of rows repeated the previous `(rsrp, rsrq, sinr)` triple
exactly.** Those rows are not independent observations: they overstate the sample
size and distort the variogram's short-lag structure, and in random k-fold CV a
held-out row's own twin can sit in the training fold.

This is handled at both ends:

- **On the phone**, a change gate stores a sample only when the reading actually
  changed (`SignalChangeGate`). Replayed over those same 3,130 real samples it
  keeps 502 — a 6.2x reduction, and it is the moving rows that shrink, where the
  older stationary thinning never engaged.
- **In the model**, `mukoo-krige --dedupe-runs` collapses the runs at load time
  (`MUKOO_DEDUPE_RUNS=1`, set for the refresh agent), so historical rows recorded
  before the gate existed are treated the same way. The raw table is never
  modified — see [model/README.md](model/README.md).
- **`GET /v1/stats`** reports the re-read share and the resulting
  `independent_rows`, so the effective sample size is visible without running the
  model. On the current 3,130 rows: 2,703 re-reads, **427 independent** — the
  same figure the model's dedupe arrives at independently.

### Grid support

The kriging grid spans the bounding box of the measurements, which makes it
only as tight as the furthest stray drive. The audit above covers a ~16 × 14 km
corner, but one later session of 14 points ran **156 km** out of it and stretched
the box to **162 × 76 km** — 553,860 cells at 150 m, where the *median* cell sits
**18.7 km from the nearest measurement** against a fitted variogram range of
3.8 km. Past that range kriging has nothing left to say and returns the global
mean at maximum variance. That is not just wasted compute: the active-learning
step ranks cells by uncertainty, so it aimed every suggested target at empty
countryside nobody had driven to, and its road fetch for the full rectangle grew
past 1.15 GB resident before the OS killed it — the 15-minute refresh agent
failed on every tick.

So cells further than one fitted variogram range from any measurement are
dropped before prediction (`mukoo-krige --support-range-multiple`, default 1.0,
or `MUKOO_SUPPORT_RANGE_MULTIPLE` for the refresh agent; `0` restores the
full-rectangle surface). The radius comes from the variogram fitted on that run,
not a constant, because the range *is* the distance past which the data stops
informing anything — exactly so for an isotropic variogram, while under
`MUKOO_ANISOTROPY` the circular mask reaches too far along the short axis by
the scaling factor, keeping cells it could have dropped rather than dropping
ones it should keep. On the current measurements it keeps **49,627 of 553,860
cells** — the 9% the drives actually cover. The exported GeoTIFFs carry nodata
everywhere else, so a map shows unsurveyed ground as absent rather than as an
interpolation nobody should trust. See [model/README.md](model/README.md).

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
# -> claims_report.json (summary, per-tier breakdown, worst point)          [local]
# -> claims_violations.geojson (every violating measurement, worst first)   [local]

# Publishable, privacy-preserving views (hex-aggregated / not coordinate-extractable):
python docs/aggregate_violations_by_hex.py   # -> verizon_claim_violations_by_hex.geojson
python docs/make_coverage_map.py             # -> docs/coverage_map.png
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

## License

MIT — see [LICENSE](LICENSE).

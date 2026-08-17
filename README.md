# Mukoo

**Mukoo is a rural cellular coverage verification system: it audits a carrier's
FCC-filed coverage claims against real, GPS-tagged field measurements — driving
the roads and checking whether the signal the carrier told the FCC it provides
is actually there.**

![Coverage vs. claim](docs/coverage_map.png)

## The finding

Verizon's FCC Broadband Data Collection (BDC) filing claims a minimum signal
level for every hexagon it says it covers. We drove **sixteen routes**
(2026-07-18 to 08-01) in a rural Georgia county and logged **4,073 GPS-tagged
signal measurements** — 76% of them inside a **~16 × 14 km** corner every route
passes through, the rest along longer trips out from it. Then we compared each
measurement to the carrier's own filed claim for the exact spot it was taken.

**Of the 3,139 measurements that landed inside a hex Verizon actually claims to
cover, 99.2% (3,115) came in below the carrier's own filed minimum.** That is
the like-for-like comparison, and it is the number to read. Across every
measurement including those outside any claimed hex, 76.5% (3,115 of 4,073) were
below claim; the other 934 fell where there is no claim to test.

On average the violating points came in **22.5 dB below** the claimed floor; the
worst was **54 dB below** — a measured −104 dBm where the filing claims at least
−50 dBm.

The shortfall holds across every tier of the filing, including the strongest
claims:

| Claimed min RSRP | Points in tier | Below claim | Avg measured |
|-----------------:|---------------:|------------:|-------------:|
| −50 dBm          |             20 |          20 | −94.9 dBm    |
| −60 dBm          |            135 |         135 | −95.0 dBm    |
| −70 dBm          |            500 |         500 | −99.1 dBm    |
| −80 dBm          |            944 |         924 | −103.1 dBm   |
| −90 dBm          |          1,534 |       1,530 | −108.2 dBm   |
| −100 dBm         |              6 |           6 | −115.3 dBm   |

**Verified two independent ways, with identical results.** The audit is run by a
custom tool (`mukoo-claims`) that reads the BDC GeoPackage directly with the
Python standard library — no GDAL — and does point-in-hex tests with Shapely. As
a guard against a bug in that hand-written path, the same join was reproduced
from scratch with a completely different stack: GDAL (`ogr2ogr`) loading the
hexes into PostGIS, and a SQL `ST_Contains` spatial join. Both paths return the
same 3,139 in-claim points, the same 3,115 violations, the same −22.49 dB
average gap, the same −54.0 dB worst gap, and the same counts in all six claim
tiers.

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
   is no signal at all.* It lives in its own repository:
   [jefferyjefe/mukoo-logger](https://github.com/jefferyjefe/mukoo-logger).
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

## Honest limitations

**The finding does not depend on the model.** It is a point-in-polygon join:
each measurement against the carrier's own filed minimum for the hex it landed
in. No interpolation, no kriging, no assumption about what happens between
readings. That result stands on its own, and it is reproduced through two
independent stacks.

**The kriging surface: the sequence, not a snapshot.**

At nine drives (1,458 measurements), leave-one-drive-out cross-validation
reported a **negative R²** — the surface did not generalise to roads it had not
seen at all. That number was recorded and committed at the time, before the
later drives existed. At sixteen drives (4,073 measurements) the same scheme
reports **R² 0.31**, RMSE 7.5 dBm; spatial-block CV reports 0.24. Modest
numbers, but the sign flipped.

**That is not a controlled experiment, and should not be read as one.** The
later drives were not a random sample of new road: they were chosen by
`mukoo-suggest`, which ranks candidates by expected variance reduction and
therefore sends the car exactly where the model was least certain. So the
improvement reflects better *coverage of the space* as much as a larger *count*,
and this design cannot separate the two. Running the loop is what moved the
number — which is what the loop is for — but "more data fixed it" would be the
wrong summary.

**Calibration is not there yet.** On unseen drives **62.3%** of points fall
within one kriging σ, against a 68% target: still overconfident about roads it
has not driven. By spatial block it is 67.4% and by random fold 68.7%. Random
k-fold also reports the flattering R² 0.52, and is listed last on purpose —
its held-out points sit metres from training points on the same track.

The finding above never depended on any of this.

**Scope.** One carrier, one filing, one small rural area. This demonstrates a
method and a real discrepancy — not a nationwide result. No FCC challenge has
been filed.

## Repository layout

| Path        | What it is |
|-------------|------------|
| [`ingest/`](ingest/) | Flask ingestion API — batch, idempotent measurement intake. Installable package `mukoo-ingest`. |
| [`model/`](model/)   | Coverage model. Separate installable package `mukoo-model`: kriging of RSRP (cross-validation + GeoTIFF export, `mukoo-krige`), active-learning drive suggestions from the uncertainty surface (`mukoo-suggest`), and FCC claimed-coverage verification against BDC filings (`mukoo-claims`). |
| [`db/`](db/)         | Alembic migrations and schema — the source of truth for the database. |
| [`infra/`](infra/)   | Docker Compose stack and configuration. |

The Android field logger that feeds this pipeline is a separate repository —
[jefferyjefe/mukoo-logger](https://github.com/jefferyjefe/mukoo-logger) — so
this one stays a Python codebase. It is native Java against platform APIs only
(`TelephonyManager`, `LocationManager`, SQLite, `HttpURLConnection`), with
client-generated UUIDs making uploads idempotent, store-and-forward through
local SQLite so a drive through a dead zone loses nothing, and dead zones
recorded as real samples rather than gaps — absence of coverage is the signal
this project exists to map.

Its one result that this repository depends on is the change gate, below.

### Published data & privacy

The evidence is published **aggregated to the carrier's own H3 claimed-coverage
hexes** — [`verizon_claim_violations_by_hex.geojson`](verizon_claim_violations_by_hex.geojson):
one feature per hex, carrying how many measurements fell inside it and how many
came in below the filed minimum. Raw per-measurement GPS traces are deliberately
kept local (gitignored). What that buys, precisely: position is quantised to the
carrier's own ~190 m cells, there are no timestamps, no point ids, and no
per-point coordinates, so no individual reading can be recovered — and because
every published figure is a rate or a mean rather than a count, the dwell
pattern that would show where a vehicle sat still is gone too. What it does not
buy: the driven corridors remain discernible from *which* cells appear at all.
That is inherent to publishing where a claim failed, and it is the deliberate
limit of what this file protects. Regenerate with
[`docs/aggregate_violations_by_hex.py`](docs/aggregate_violations_by_hex.py).

A hex is published only if it holds **at least three measurements** (214 of the
453 hexes with a violation). This is a statistical floor, not a privacy one:
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
slowly, so one measurement can be re-read several times. Across all 4,073
samples, **76% of rows repeat the previous `(rsrp, rsrq, sinr)` triple exactly.**
Those rows are not independent observations: they overstate the sample size and
distort the variogram's short-lag structure, and in random k-fold CV a held-out
row's own twin can sit in the training fold.

This is handled at both ends:

- **On the phone**, a change gate stores a sample only when the reading actually
  changed (`SignalChangeGate`, in
  [mukoo-logger](https://github.com/jefferyjefe/mukoo-logger)). Replayed over
  the 3,130 real samples recorded before it shipped, it keeps **502 — a 6.2x
  reduction**, and it is the moving rows that shrink, where the older stationary
  thinning never engaged. That measurement is why the dedupe below exists: it
  established that most of the table was one reading counted many times.
- **In the model**, `mukoo-krige --dedupe-runs` collapses the runs at load time
  (`MUKOO_DEDUPE_RUNS=1`, set for the refresh agent), so historical rows recorded
  before the gate existed are treated the same way. The raw table is never
  modified — see [model/README.md](model/README.md).
- **`GET /v1/stats`** reports the re-read share and the resulting
  `independent_rows`, so the effective sample size is visible without running the
  model. On the current 4,073 rows: 3,112 re-reads, **961 independent**. The
  model then fits on 859 of those, having also dropped sessions too small to be
  a real drive and averaged coincident points; the two counts are derived
  independently and the gap between them is exactly those two filters.

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

Nothing to configure and no database needed to see the modelling code work:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip          # see note below — 21.x cannot do this install
pip install -e 'model[dev]'
make test-model                    # 181 tests, no database, ~70s
```

> **The pip upgrade is not optional.** These packages ship a `pyproject.toml`
> and no `setup.py`, so an editable install needs PEP 660 support. The pip
> bundled with macOS's system Python 3.9 (21.2.4) predates it and fails with
> *"Directory cannot be installed in editable mode"*. Wheels exist for every
> dependency including GDAL/rasterio, so with current pip the install takes
> about ten seconds.

The model suite runs entirely on synthetic point clouds with a known field, so
it exercises the kriging, cross-validation, variogram, and routing code without
any measurements. `make test` additionally runs the ingest suite, which does need
a PostGIS test database — see [ingest/README.md](ingest/README.md).

### Reproducing the audit

**You cannot reproduce the published numbers from this repository alone**, and
that is deliberate: the audit runs against the raw GPS-tagged measurements, which
are kept local (see *Published data & privacy*). What is here is the pipeline
that produced them, the hex-level evidence it output, and a second
implementation that checks it. To run the audit you need your own measurements
in PostGIS, plus the carrier's filing — a per-provider mobile H3 GeoPackage from
the FCC's [Broadband Data Collection download
site](https://broadbandmap.fcc.gov/data-download) (~430 MB for one state).

```bash
pip install -e 'model[viz]'        # [viz] adds matplotlib, needed by docs/ below
export DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo
mukoo-claims --gpkg ~/Downloads/bdc_13_131425_4GLTE_mobile_broadband_h3_*.gpkg
# -> claims_report.json (summary, per-tier breakdown, worst point)          [local]
# -> claims_violations.geojson (every violating measurement, worst first)   [local]

# Publishable, privacy-preserving views (hex-aggregated / not coordinate-extractable):
python docs/aggregate_violations_by_hex.py   # -> verizon_claim_violations_by_hex.geojson
python docs/make_coverage_map.py             # -> docs/coverage_map.png
```

Those outputs are written to `$MUKOO_MODEL_OUTPUT_DIR`, default `~/mukoo` —
**not** the checkout, unless you happen to have cloned there. Set it if you want
them somewhere else; the `docs/` scripts read the report back from the same
place.

To develop and test the API against a local PostGIS, see
[ingest/README.md](ingest/README.md); for the full modelling toolkit
(`mukoo-krige` / `mukoo-suggest` / `mukoo-claims`), see
[model/README.md](model/README.md).

## Migrations

`db/` is the schema's source of truth. Apply / create migrations with Alembic —
which is not a dependency of either package, so install it first:

```bash
pip install -r db/requirements.txt
cd db
DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo alembic upgrade head
DATABASE_URL=… alembic revision -m "add something"   # new migration
```

`alembic.ini` ships a deliberately non-working placeholder URL, so a missing
`DATABASE_URL` fails immediately instead of quietly migrating the wrong
database. The compose stack and the ingest test suite both apply migrations
themselves; this is for running them by hand.

## License

MIT — see [LICENSE](LICENSE).

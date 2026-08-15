# mukoo-model

Coverage modelling for Mukoo — a separate installable package that reads the
measurements collected by the ingestion API (directly from PostGIS) and produces
coverage estimates. It only ever *reads* the `measurements` table; the API owns
all writes.

## Kriging of RSRP

`mukoo-krige` interpolates RSRP across the survey area:

1. **Read** non-null RSRP points from PostGIS (with session + cell labels) and
   project them from WGS84 into the local UTM zone, so distances (and therefore
   the variogram) are in metres. Coincident points (GPS repeats) are collapsed
   to one averaged value to keep the kriging system non-singular.
2. **Cross-validate first, honestly.** Three schemes, reported in this order:
   - **leave-one-session-out** — hold out whole drives; the headline number for
     "how wrong is the surface on a road I haven't driven".
   - **spatial block (2 km tiles)** — hold out map areas; between-area skill.
   - **random k-fold** — the flattering along-track number, kept as the
     near-road bound. Random folds hold out points metres from training points
     on the same track, so this one alone would oversell the surface.
3. **Fit + predict** on all points to produce the predicted RSRP mean and the
   kriging variance (uncertainty) over the bounding box — restricted to the
   cells the measurements can actually speak for (see *grid support* below).
4. **Export** GeoTIFFs (mean, variance, stddev) + a JSON run report to `~/mukoo`.
   Cells outside the supported region are written as the raster's nodata value.

Models: `--kriging ordinary` (default) or `--kriging pathloss` — regression
kriging with a log-distance path-loss trend whose "towers" are bootstrapped
from each cell_id's power-weighted sample centroid. `--anisotropy SCALING:ANGLE`
stretches the variogram (signal along a road can decorrelate differently from
across it). `--compare` CVs the variogram families (exponential / spherical /
gaussian) vs. pathloss vs. an anisotropy scan and prints a table instead of
writing surfaces.

```bash
pip install -e model          # numpy/scipy/pykrige/pyproj/rasterio/shapely/osmnx
export DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo
mukoo-krige --metric rsrp     # CV first (3 schemes), then writes surfaces
mukoo-krige --compare         # model shoot-out, judged by session CV
```

Other flags: `--variogram-model`, `--cell-m` (150), `--folds`, `--block-m`
(2000), `--out-dir`, `--metric {rsrp,rsrq,sinr}`, `--where`, `--none-floor`,
`--dedupe-runs` / `--no-dedupe-runs`, `--lag-spacing`, `--max-lag-m`, `--nlags`,
`--support-range-multiple` (1.0).

**Variogram lag binning (`--lag-spacing`).** pykrige bins inter-point distances
into `nlags` *equal-width* bins over the full range of separations, with no way
to cap the longest lag. On a survey ~22 km across, that makes the first bin
~1.9 km wide — nothing observes sub-kilometre structure, so the nugget is
extrapolated to ~0 and the model claims near-perfect knowledge at short range.
The kriging variance is then far too small (within-1σ ≈ 40% against a 68%
target). The default `log` spacing puts several bins under 200 m while still
reaching the far field, so the nugget is *measured*; the fitted parameters are
handed to pykrige, which does the kriging but not the variogram estimation.
`--lag-spacing pykrige` restores the old delegated behaviour.

`--max-lag-m` caps the longest lag used for the fit. It only helps if the
variogram plateaus below the cap — on this survey the empirical curve is still
climbing at 6 km, so capping there sends the fitted range and sill running away.
Left unset by default for that reason.

**Grid support (`--support-range-multiple`).** The prediction grid spans the
bounding box of the measurements, and the box is only as tight as the furthest
stray drive. One session of 14 points that ran 156 km out of the survey area
(the next largest spans 62 km) stretched it to 162 × 76 km — 553,860 cells at
150 m, with the *median* cell sitting 18.7 km from the nearest measurement and
the worst 77 km out, against a fitted variogram range of 3.8 km. Past that range
the model has nothing left to say: kriging returns the global mean at the
maximum variance, for cell after cell. That is not merely wasted compute. The
suggester ranks cells by uncertainty, so it aimed every target at empty
countryside nobody had driven to, and its OSM road fetch for the full rectangle
(22 MB of cached geometry, against 1–2 MB for the surveyed corner) grew past
1.15 GB resident and was killed by the OS — the refresh agent died on every tick.

So cells further than one fitted variogram range from any measurement are
dropped before prediction. The radius is `range_m × --support-range-multiple`
(default 1.0), derived from the variogram fitted *on this run* rather than fixed
in advance, because the range is the distance past which this data stops
informing anything — exactly so while the variogram is isotropic. The mask is a
plain Euclidean circle, so under `--anisotropy SCALING:ANGLE` it over-reaches
along the minor axis, whose effective range is `range_m / SCALING`, by that
factor: it keeps cells it could have dropped, never the reverse. On the current
data it keeps 49,627 of the 553,860 cells — 9% of the rectangle, and the 9% the
drives actually cover. Raise the
multiple to extrapolate further out; `--support-range-multiple 0` disables
masking and restores the full-rectangle surface. A variogram family with no
range (linear, power) falls back to no masking rather than inventing a radius.

Masked cells are NaN in the mean, variance, and stddev surfaces, written as the
GeoTIFF nodata value, so a viewer shows unsurveyed ground as absent rather than
as an interpolation nobody should trust. `load_grid_surface` reads nodata back
to NaN and rebuilds the support mask from it, so a surface round-trips; the
suggester skips NaN cells entirely, including when it picks its candidate
quantile. The run report gains `grid.support_radius_m` and
`grid.n_supported_cells` — both `null` when no mask ran, which is a different
claim from a mask that kept every cell. Its `surface_stats` are NaN-skipping
aggregates, so those minima, maxima, and means describe the supported region
rather than the whole box, and each is `null` when a radius too small to support
any cell leaves nothing to describe (`NaN` is not valid JSON: jq, `JSON.parse`,
and Go all reject a file containing it).

**Latched modem readings (`--dedupe-runs`).** The field logger samples every
~3.5 s, but the modem refreshes its signal report far more slowly, so one
measurement is re-read several times in a row — on the current dataset ~86% of
rows repeat the previous `(rsrp, rsrq, sinr)` triple exactly. Those rows are not
independent observations: they inflate the apparent sample size, and in random
k-fold CV a held-out row's own twin can sit in the training fold. `--dedupe-runs`
keeps only the **first** sample of each such run within a session (the modem's
averaging window closes at or before the value first appears, so the reading
belongs at the run's start, never ahead of it). The raw table is never modified —
this is a load-time filter, so the stored measurements remain the record.

**Adopting a `--compare` winner.** The choice persists via environment
variables, which every entry point (including the auto-refresh agent and
`mukoo-suggest --recompute`) honours — CLI flags override per run:

| Env var | Meaning | Example |
|---------|---------|---------|
| `MUKOO_KRIGING` | `ordinary` (default) or `pathloss` | `pathloss` |
| `MUKOO_ANISOTROPY` | variogram anisotropy `SCALING:ANGLE` | `3:150` |
| `MUKOO_NONE_FLOOR` | include `network_type='none'` dead-zone rows at this RSRP (dBm); unset = excluded | `-127` |
| `MUKOO_DEDUPE_RUNS` | collapse runs of consecutive identical `(rsrp, rsrq, sinr)` samples to their first sample; unset = off | `1` |
| `MUKOO_LAG_SPACING` | variogram lag axis: `log` (default), `linear`, or `pykrige` | `log` |
| `MUKOO_MAX_LAG_M` | cap on the longest lag used to fit the variogram; unset = all pairs | `12000` |
| `MUKOO_SUPPORT_RANGE_MULTIPLE` | grid cells are predicted only within this many fitted variogram ranges of a measurement; `0` = no masking | `1.0` |

## FCC claims check

`mukoo-claims` compares every measurement against the carrier's FCC-filed
coverage claim (a BDC mobile H3 GeoPackage, read directly — no GDAL needed) and
reports where ground truth contradicts the filing: points inside a claimed hex
whose measured RSRP is below the hex's filed `minsignal`.

```bash
mukoo-claims --gpkg ~/Downloads/bdc_13_131425_4GLTE_mobile_broadband_h3_*.gpkg
# -> ~/mukoo/claims_report.json (summary, per-claim-tier breakdown, worst point)
# -> ~/mukoo/claims_violations.geojson (every violating measurement, worst first)
```

`--environment 1` (default) keeps the in-vehicle claim — the like-for-like
comparison for drive data; `0` selects the outdoor-stationary claim. `--where`
narrows the measurements (e.g. one session), `--prefix` names the outputs.

## Drive suggestions (active learning)

`mukoo-suggest` answers "where should the next drive go to shrink the model's
uncertainty the most?" — decision support; it proposes, the driver chooses.

1. **Read** the stddev + mean GeoTIFFs (or refit with `--recompute`) and the
   report's variogram range. Nodata cells come back as NaN and are never
   candidates — the unsupported majority of the box cannot be suggested.
2. **Fetch roads** from OpenStreetMap via osmnx, around the candidates only:
   scoring them needs no roads, so they are picked first and their cells
   bucketed onto a fixed ~0.05° lattice, and just the occupied tiles are
   fetched (cached in `~/mukoo/osm_cache/`). Asking for the whole bounding box
   instead is what grew past 1.15 GB resident and was killed by the OS.
3. **Score** reachable high-uncertainty cells by **expected variance
   reduction**: the covariance-weighted σ² mass a measurement there would
   inform (not just point σ — a spot surrounded by uncertainty beats an
   isolated spike), times a **weak-coverage weight** (`--weak-bias`, default
   0.5) that favours verifying probably-bad coverage.
4. **Select** greedily with an overlap discount (a pick claims its
   neighbourhood's information) plus a hard `--min-separation-m` floor.
5. **Order** the picks into an efficient open tour (nearest-neighbour + 2-opt)
   and export:
   - `rsrp_drive_suggestions.geojson` — ranked points with σ, score, road,
     `visit_order`;
   - `rsrp_drive_route.gpx` — waypoints + route in driving order, loadable by
     any navigation app.

```bash
mukoo-suggest --metric rsrp   # table: rank, drive order, σ, score, road
```

## Auto-refresh

`mukoo-refresh` re-runs krige + suggest **only when the measurements table
changed** (state file `~/mukoo/.model_refresh_state.json`; unchanged tables
cost one SELECT). The launchd agent runs it every 15 min:

```bash
make refresh              # one manual check-and-refresh
make autorefresh-install  # install + start the launchd agent (log: ~/mukoo/refresh.log)
make autorefresh-remove
```

## Package layout

| Module | Responsibility |
|--------|----------------|
| `config.py`   | Env-sourced settings (DATABASE_URL, defaults) |
| `db.py`       | SQLAlchemy engine (read-only usage) |
| `data.py`     | Load points (+ session/cell labels) from PostGIS, project, collapse |
| `kriging.py`  | `OrdinaryKrigingModel` (incl. anisotropy), grid + support mask, surface prediction |
| `towers.py`   | Bootstrap tower positions from per-cell power-weighted centroids |
| `regression.py` | `PathLossKrigingModel` — log-distance trend + residual kriging |
| `crossval.py` | Random / session / block CV → accuracy + calibration metrics |
| `compare.py`  | Model shoot-out (variogram families, pathloss, anisotropy scan), judged by CV |
| `claims.py`   | FCC BDC filing vs measurements: GPKG reader, spatial join, violation report |
| `export.py`   | GeoTIFF writing + JSON report assembly |
| `raster.py`   | Load a GeoTIFF surface back into `(array, Grid)`, nodata → NaN + support mask |
| `roads.py`    | `RoadNetwork` (Shapely) + OSM fetch, cached: `fetch_roads` per bbox, `road_tiles` / `fetch_roads_tiles` per lattice tile |
| `suggest.py`  | EVR scoring, weakness weighting, overlap-aware selection, GeoJSON |
| `route.py`    | NN + 2-opt visit order, GPX export |
| `refresh.py`  | Change-detecting auto-refresh (`mukoo-refresh`) |
| `pipeline.py` / `suggest_pipeline.py` | Orchestration |
| `cli.py` / `cli_suggest.py` / `cli_claims.py` | `mukoo-krige` / `mukoo-suggest` / `mukoo-claims` entry points |

## Reading the numbers (current data)

With nine drives over one corner of the county, session CV shows the surface
does **not** yet generalise to unseen roads (negative R², over-confident σ) —
the random k-fold RMSE alone would not have revealed that. The fix is coverage,
not modelling: drive the suggested targets (they chase exactly the areas that
would shrink uncertainty fastest) and watch the session-CV number come down on
each `mukoo-refresh`.

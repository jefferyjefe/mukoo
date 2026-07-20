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
   kriging variance (uncertainty) over the bounding box.
4. **Export** GeoTIFFs (mean, variance, stddev) + a JSON run report to `~/mukoo`.

Models: `--kriging ordinary` (default) or `--kriging pathloss` — regression
kriging with a log-distance path-loss trend whose "towers" are bootstrapped
from each cell_id's power-weighted sample centroid. `--anisotropy SCALING:ANGLE`
stretches the variogram (signal along a road can decorrelate differently from
across it). `--compare` CVs ordinary vs. pathloss vs. an anisotropy scan and
prints a table instead of writing surfaces.

```bash
pip install -e model          # numpy/scipy/pykrige/pyproj/rasterio/shapely/osmnx
export DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo
mukoo-krige --metric rsrp     # CV first (3 schemes), then writes surfaces
mukoo-krige --compare         # model shoot-out, judged by session CV
```

Other flags: `--variogram-model`, `--cell-m` (150), `--folds`, `--block-m`
(2000), `--out-dir`, `--metric {rsrp,rsrq,sinr}`, `--where`.

## Drive suggestions (active learning)

`mukoo-suggest` answers "where should the next drive go to shrink the model's
uncertainty the most?" — decision support; it proposes, the driver chooses.

1. **Read** the stddev + mean GeoTIFFs (or refit with `--recompute`) and the
   report's variogram range.
2. **Fetch roads** for the bounding box from OpenStreetMap via osmnx, cached in
   `~/mukoo/osm_cache/`.
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
| `kriging.py`  | `OrdinaryKrigingModel` (incl. anisotropy), grid, surface prediction |
| `towers.py`   | Bootstrap tower positions from per-cell power-weighted centroids |
| `regression.py` | `PathLossKrigingModel` — log-distance trend + residual kriging |
| `crossval.py` | Random / session / block CV → accuracy + calibration metrics |
| `compare.py`  | Model shoot-out + anisotropy scan, judged by CV |
| `export.py`   | GeoTIFF writing + JSON report assembly |
| `raster.py`   | Load a GeoTIFF surface back into `(array, Grid)` |
| `roads.py`    | `RoadNetwork` (Shapely) + `fetch_roads` (OSM, cached) |
| `suggest.py`  | EVR scoring, weakness weighting, overlap-aware selection, GeoJSON |
| `route.py`    | NN + 2-opt visit order, GPX export |
| `refresh.py`  | Change-detecting auto-refresh (`mukoo-refresh`) |
| `pipeline.py` / `suggest_pipeline.py` | Orchestration |
| `cli.py` / `cli_suggest.py` | `mukoo-krige` / `mukoo-suggest` entry points |

## Reading the numbers (current data)

With six drives over one corner of the county, session CV shows the surface
does **not** yet generalise to unseen roads (negative R², over-confident σ) —
the random k-fold RMSE alone would not have revealed that. The fix is coverage,
not modelling: drive the suggested targets (they chase exactly the areas that
would shrink uncertainty fastest) and watch the session-CV number come down on
each `mukoo-refresh`.

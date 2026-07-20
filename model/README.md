# mukoo-model

Coverage modelling for Mukoo — a separate installable package that reads the
measurements collected by the ingestion API (directly from PostGIS) and produces
coverage estimates. It only ever *reads* the `measurements` table; the API owns
all writes.

## Implemented: ordinary kriging of RSRP

The first model interpolates RSRP across the survey area by **ordinary kriging**:

1. **Read** non-null RSRP points from PostGIS and project them from WGS84 into
   the local UTM zone, so distances (and therefore the variogram) are in metres.
   Coincident points (GPS repeats) are collapsed to one averaged value to keep
   the kriging system non-singular.
2. **Cross-validate first.** 10-fold CV holds out points, kriges from the rest,
   and reports how well predicted matches actual on the held-out ones — RMSE,
   MAE, skill (R² vs. predicting the mean), **and** whether the kriging variance
   is honest (are ~68 % of points within ±1σ?). This decides whether the data is
   dense enough to interpolate *before* anyone trusts the surface.
3. **Fit + predict** on all points to produce two surfaces over the bounding box:
   the predicted RSRP mean and the kriging variance (uncertainty).
4. **Export** both as georeferenced GeoTIFFs (plus a stddev band and a JSON run
   report) to `~/mukoo`.

### Run it

```bash
pip install -e model          # pulls numpy/scipy/pykrige/pyproj/rasterio
export DATABASE_URL=postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo
mukoo-krige --metric rsrp     # prints CV numbers first, then writes surfaces
```

Useful flags: `--variogram-model {exponential,spherical,gaussian,linear,power}`
(exponential is the CV-chosen default), `--cell-m` (grid resolution, default
150 m), `--folds` (k for k-fold; set to the point count for leave-one-out),
`--out-dir`, `--metric {rsrp,rsrq,sinr}`, `--where` (extra SQL predicate).

### Library use

```python
from mukoo_model import Config, run
result = run(Config.from_env())
print(result.cv.summary())          # accuracy + calibration
print(result.surface_paths)         # {'mean': ..., 'variance': ..., 'stddev': ...}
```

### Outputs (written to `~/mukoo`)

| File | What it is |
|------|------------|
| `rsrp_kriging_mean.tif`     | Predicted RSRP surface (dBm), EPSG:326NN (UTM) |
| `rsrp_kriging_variance.tif` | Kriging variance (dBm²) — native uncertainty |
| `rsrp_kriging_stddev.tif`   | 1σ uncertainty (dBm) — the readable version |
| `rsrp_kriging_report.json`  | CV metrics, fitted variogram, grid + provenance |

Rasters are written in the UTM CRS the kriging ran in (regular pixels in metres);
reproject to EPSG:4326 downstream if a web map needs it.

## Implemented: active-learning route suggestions

Given the uncertainty surface, *where should the next drive go to shrink the
model's uncertainty the most?* This is **decision support** — it proposes ranked
targets, you choose; nothing autonomous.

1. **Read** the kriging standard-deviation surface (`rsrp_kriging_stddev.tif`,
   or recompute it from PostGIS).
2. **Fetch roads.** There's no roads table yet, so the drivable network for the
   surface's bounding box is pulled from OpenStreetMap via osmnx and cached to
   `~/mukoo/osm_cache/` (one network call, reused after).
3. **Rank.** Take the cells whose uncertainty is in the top quantile, snap each
   onto the nearest road, drop any with no road within `--max-road-dist-m`
   (uncertain but unreachable — mid-field), and rank the reachable ones by the
   uncertainty *at the on-road point*.
4. **Spread.** Greedily pick the top N while enforcing `--min-separation-m`, so
   suggestions don't cluster — each drive adds distinct information.

```bash
mukoo-krige --metric rsrp     # produce the surface first (if not already)
mukoo-suggest --metric rsrp   # prints the ranked targets, writes GeoJSON
```

Flags: `--top-n` (default 10), `--candidate-quantile` (0.70), `--max-road-dist-m`
(250), `--min-separation-m` (1200), `--recompute` (refit instead of reading the
tif), `--refresh-roads`, `--network-type` (osmnx, default `drive`).

Output → `~/mukoo/rsrp_drive_suggestions.geojson`: ranked Point features with
`rank`, `stddev` (1σ at the target), `road_name`, `road_distance_m`.

## Package layout

| Module | Responsibility |
|--------|----------------|
| `config.py`         | Env-sourced settings (DATABASE_URL, defaults) |
| `db.py`             | SQLAlchemy engine (read-only usage) |
| `data.py`           | Load points from PostGIS, project to UTM, collapse duplicates |
| `kriging.py`        | `OrdinaryKrigingModel`, grid construction, surface prediction |
| `crossval.py`       | k-fold CV → accuracy + variance-calibration metrics |
| `export.py`         | GeoTIFF writing + JSON report assembly |
| `raster.py`         | Load a GeoTIFF surface back into `(array, Grid)` |
| `roads.py`          | `RoadNetwork` (Shapely) + `fetch_roads` (OSM, cached) |
| `suggest.py`        | Active-learning ranking + GeoJSON export (pure, DB/OSM-free) |
| `pipeline.py`       | Kriging orchestration (`run`) |
| `suggest_pipeline.py` | Suggestion orchestration (`run_suggest`) |
| `cli.py` / `cli_suggest.py` | `mukoo-krige` / `mukoo-suggest` entry points |

## Still planned

- **Propagation prior** — a physical path-loss / terrain model as a covariate
  (regression kriging) to sharpen predictions away from driven roads.
- **Dead-zone extraction** — polygons where `network_type = 'none'` clusters.

```bash
pip install -e model
```

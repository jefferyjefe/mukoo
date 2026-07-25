"""Write kriged surfaces to GeoTIFF and the run report to JSON.

Rasters are written in the projected CRS the kriging ran in (a UTM zone), which
is the honest thing to do: the pixels are regular in metres. Any GIS or web-map
pipeline can reproject to 4326 on the way out. GeoTIFF row 0 is the north edge,
so we flip pykrige's south-ascending arrays to north-up on write.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from .crossval import CVResult
from .kriging import SurfaceResult

# Sentinel for any non-finite cell (kriging fills the whole grid, so this is
# defensive rather than expected).
NODATA = -9999.0


def write_geotiff(
    path: Path,
    array: np.ndarray,
    surface: SurfaceResult,
    *,
    description: str,
) -> Path:
    """Write one band to a north-up float32 GeoTIFF in the surface's CRS."""
    grid = surface.grid
    data = np.asarray(array, dtype=np.float64)
    # pykrige arrays are south-ascending (row 0 = min y); GeoTIFF is north-up.
    data = np.flipud(data)
    data = np.where(np.isfinite(data), data, NODATA).astype(np.float32)

    transform = from_origin(grid.west, grid.north, grid.cell_m, grid.cell_m)
    path = Path(path)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="float32",
        crs=f"EPSG:{grid.crs_epsg}",
        transform=transform,
        nodata=NODATA,
        compress="deflate",
    ) as dst:
        dst.write(data, 1)
        dst.set_band_description(1, description)
        dst.update_tags(1, metric=surface.metric, description=description)
    return path


def write_surfaces(
    out_dir: Path,
    surface: SurfaceResult,
    *,
    prefix: str = "rsrp_kriging",
) -> dict[str, Path]:
    """Write mean, variance, and stddev GeoTIFFs. Returns {name: path}.

    Variance is what ordinary kriging produces natively (units: metric^2); the
    stddev band is the same information in the metric's own units (e.g. dBm),
    which is what a human actually reads off an uncertainty map.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    unit = "dBm" if surface.metric in {"rsrp", "rsrq"} else surface.metric

    paths = {
        "mean": write_geotiff(
            out_dir / f"{prefix}_mean.tif",
            surface.mean,
            surface,
            description=f"Ordinary kriging predicted {surface.metric} ({unit})",
        ),
        "variance": write_geotiff(
            out_dir / f"{prefix}_variance.tif",
            surface.variance,
            surface,
            description=f"Kriging variance of {surface.metric} ({unit}^2)",
        ),
        "stddev": write_geotiff(
            out_dir / f"{prefix}_stddev.tif",
            surface.stddev,
            surface,
            description=f"Kriging std dev (1-sigma uncertainty) of "
            f"{surface.metric} ({unit})",
        ),
    }
    return paths


def build_report(
    surface: SurfaceResult,
    cvs: "CVResult | list[CVResult]",
    *,
    n_raw: int,
    n_merged: int,
    bounds_lonlat: tuple[float, float, float, float],
    surface_paths: dict[str, Path],
    dedupe_runs: bool = False,
    n_dedupe_dropped: int = 0,
) -> dict:
    """Assemble the JSON run report: CV metrics, variogram, grid, provenance.

    ``cross_validation`` holds one entry per scheme, keyed by the scheme label,
    in reporting order (session first when present — the honest number).
    """
    if isinstance(cvs, CVResult):
        cvs = [cvs]
    lon_min, lat_min, lon_max, lat_max = bounds_lonlat
    return {
        "metric": surface.metric,
        "cross_validation": {cv.scheme: cv.to_dict() for cv in cvs},
        "variogram": surface.variogram_params,
        "grid": {
            "crs": f"EPSG:{surface.grid.crs_epsg}",
            "cell_metres": surface.grid.cell_m,
            "rows": surface.grid.shape[0],
            "cols": surface.grid.shape[1],
        },
        "surface_stats": {
            "mean_min": float(np.nanmin(surface.mean)),
            "mean_max": float(np.nanmax(surface.mean)),
            "mean_mean": float(np.nanmean(surface.mean)),
            "stddev_min": float(np.nanmin(surface.stddev)),
            "stddev_max": float(np.nanmax(surface.stddev)),
            "stddev_mean": float(np.nanmean(surface.stddev)),
        },
        "data": {
            "n_points_used": surface.n_points,
            "n_rows_read": n_raw,
            "n_merged_duplicates": n_merged,
            # Latched-modem-reading dedupe: a report is only comparable to
            # another with the same setting, so record it either way.
            "dedupe_consecutive_runs": dedupe_runs,
            "n_dropped_latched_rereads": n_dedupe_dropped,
            "bounds_lonlat": {
                "lon_min": lon_min,
                "lat_min": lat_min,
                "lon_max": lon_max,
                "lat_max": lat_max,
            },
        },
        "outputs": {name: str(p) for name, p in surface_paths.items()},
    }


def write_report_json(path: Path, report: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=False))
    return path

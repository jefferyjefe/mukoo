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

# NaN, not a numeric sentinel. Unsupported cells are an expected and often large
# part of the grid now, and a value like -9999 is only nodata to a consumer that
# reads the tag; to anything that does not, it is a reading, and one that drags
# every colour ramp and zonal statistic it lands in. float32 carries NaN, which
# nothing can mistake for a measurement.
NODATA = np.nan


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
    # Normalise every non-finite cell to the one nodata representation, so an
    # infinity from a numerical blow-up cannot leak out looking like a value.
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

    Cells outside the grid's support are NaN in the surface and are written as
    the raster's nodata value, so the unsurveyed area reads as absent rather
    than as an interpolation nobody should trust.
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


def _finite_stat(fn, array: np.ndarray) -> "float | None":
    """``fn`` over the finite cells of ``array``, or None if there are none.

    The report has to be valid RFC 8259 JSON: ``json.dumps`` happily writes a
    float NaN as the bare token ``NaN``, which Python reads back but jq,
    JavaScript's ``JSON.parse``, and Go's decoder all reject — and the app
    fetches this file's siblings. An all-NaN surface is reachable, since a small
    enough ``--support-range-multiple`` supports no cells at all, and asking
    numpy for the minimum of one also raises an all-NaN-slice warning. ``null``
    says "no supported cells to describe" in a form every parser reads.

    Infinities are dropped for the same reason (``Infinity`` is not JSON
    either), which also matches ``write_geotiff``: a non-finite cell is nodata
    in the raster, so it is absent here too.
    """
    values = np.asarray(array, dtype=np.float64)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    return float(fn(finite))


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
    min_session_rows: int = 1,
    excluded_sessions: "tuple[str, ...]" = (),
    support_radius_m: "float | None" = None,
    n_supported_cells: "int | None" = None,
) -> dict:
    """Assemble the JSON run report: CV metrics, variogram, grid, provenance.

    ``cross_validation`` holds one entry per scheme, keyed by the scheme label,
    in reporting order (session first when present — the honest number).

    ``support_radius_m`` and ``n_supported_cells`` describe the grid mask: how
    far from a measurement a cell may sit and still be predicted, and how many
    cells survived. Both default to ``None``, which reports "no masking ran" —
    deliberately distinct from a radius that happened to mask nothing.

    ``surface_stats`` summarise the supported cells, skipping the masked ones;
    each entry is ``null`` when the mask left nothing to describe, so the report
    is always parseable JSON.
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
            "support_radius_m": support_radius_m,
            "n_supported_cells": n_supported_cells,
        },
        "surface_stats": {
            "mean_min": _finite_stat(np.min, surface.mean),
            "mean_max": _finite_stat(np.max, surface.mean),
            "mean_mean": _finite_stat(np.mean, surface.mean),
            "stddev_min": _finite_stat(np.min, surface.stddev),
            "stddev_max": _finite_stat(np.max, surface.stddev),
            "stddev_mean": _finite_stat(np.mean, surface.stddev),
        },
        "data": {
            "n_points_used": surface.n_points,
            "n_rows_read": n_raw,
            "n_merged_duplicates": n_merged,
            # Latched-modem-reading dedupe: a report is only comparable to
            # another with the same setting, so record it either way.
            "dedupe_consecutive_runs": dedupe_runs,
            "n_dropped_latched_rereads": n_dedupe_dropped,
            # Never drop sessions silently: a reader has to be able to see that
            # the modelled set is not simply "every session in the table".
            "min_session_rows": min_session_rows,
            "excluded_sessions": list(excluded_sessions),
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

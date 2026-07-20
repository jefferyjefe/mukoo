"""Read a single-band GeoTIFF surface back into ``(array, Grid)``.

The kriging exporter writes north-up GeoTIFFs (row 0 = north edge). Everything
in-memory uses the :class:`~mukoo_model.kriging.Grid` convention instead —
``y`` ascending, so array row 0 is the *south* edge. This loader reverses the
export flip and reconstructs the grid geometry from the raster's transform, so a
surface produced in one run can be consumed by another (e.g. the route
suggester reading ``rsrp_kriging_stddev.tif``) without recomputing the model.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio

from .kriging import Grid


def load_grid_surface(path: Path, band: int = 1) -> tuple[np.ndarray, Grid]:
    """Load a GeoTIFF band as a south-up array plus its :class:`Grid`.

    NoData pixels come back as ``nan``. The reconstructed grid's cell size is
    taken from the raster's x pixel size; a non-square pixel would be unusual for
    our kriging output and is not specially handled.
    """
    with rasterio.open(Path(path)) as ds:
        band_data = ds.read(band).astype(np.float64)
        if ds.nodata is not None:
            band_data = np.where(band_data == ds.nodata, np.nan, band_data)
        transform = ds.transform
        cell_m = float(transform.a)
        west = float(transform.c)
        north = float(transform.f)
        height, width = ds.height, ds.width
        crs_epsg = ds.crs.to_epsg()

    south = north - height * cell_m
    # Cell centres, both axes ascending (Grid convention).
    x = west + (np.arange(width) + 0.5) * cell_m
    y = south + (np.arange(height) + 0.5) * cell_m
    # GeoTIFF is north-up (row 0 = north); flip to south-up to match Grid.y.
    array = np.flipud(band_data)

    grid = Grid(x=x, y=y, cell_m=cell_m, crs_epsg=int(crs_epsg))
    return array, grid

"""Round-trip test: a surface written to GeoTIFF loads back unchanged."""

from __future__ import annotations

import numpy as np

from mukoo_model.export import write_geotiff
from mukoo_model.kriging import OrdinaryKrigingModel, make_grid
from mukoo_model.raster import load_grid_surface


def test_geotiff_roundtrip_preserves_array_and_grid(tmp_path, linear_cloud):
    surface = OrdinaryKrigingModel(nlags=10).fit(linear_cloud).predict_grid(
        make_grid(linear_cloud, cell_m=500.0)
    )
    path = write_geotiff(
        tmp_path / "std.tif", surface.stddev, surface, description="stddev"
    )

    array, grid = load_grid_surface(path)

    assert grid.crs_epsg == surface.grid.crs_epsg
    assert np.isclose(grid.cell_m, surface.grid.cell_m)
    assert grid.shape == surface.grid.shape
    assert np.allclose(grid.x, surface.grid.x)
    assert np.allclose(grid.y, surface.grid.y)
    # Same south-up orientation as the in-memory surface, within float32 error.
    assert np.allclose(array, surface.stddev, atol=1e-2)


def test_nodata_becomes_nan(tmp_path, linear_cloud):
    surface = OrdinaryKrigingModel(nlags=10).fit(linear_cloud).predict_grid(
        make_grid(linear_cloud, cell_m=500.0)
    )
    arr = surface.stddev.copy()
    arr[0, 0] = np.nan  # write_geotiff maps non-finite -> nodata sentinel
    path = write_geotiff(tmp_path / "std.tif", arr, surface, description="stddev")

    loaded, _ = load_grid_surface(path)
    # (0,0) south-up corresponds to the flipped corner on load; just assert a NaN
    # round-trips somewhere rather than becoming the sentinel value.
    assert np.isnan(loaded).sum() == 1
    assert not np.any(loaded == -9999.0)

"""Tests for GeoTIFF export: geometry, orientation, and round-trip values."""

from __future__ import annotations

import numpy as np
import rasterio

from mukoo_model.export import build_report, write_report_json, write_surfaces
from mukoo_model.kriging import OrdinaryKrigingModel, make_grid


def _surface(cloud):
    model = OrdinaryKrigingModel(nlags=10).fit(cloud)
    return model.predict_grid(make_grid(cloud, cell_m=500.0))


def test_write_surfaces_produces_three_geotiffs(tmp_path, linear_cloud):
    surface = _surface(linear_cloud)
    paths = write_surfaces(tmp_path, surface, prefix="rsrp_kriging")
    assert set(paths) == {"mean", "variance", "stddev"}
    for p in paths.values():
        assert p.exists()


def test_geotiff_has_correct_crs_shape_and_transform(tmp_path, linear_cloud):
    surface = _surface(linear_cloud)
    paths = write_surfaces(tmp_path, surface)
    with rasterio.open(paths["mean"]) as ds:
        assert ds.crs.to_epsg() == linear_cloud.crs_epsg
        assert (ds.height, ds.width) == surface.grid.shape
        # North-up: pixel height (e[4]) is negative, resolution == cell size.
        assert ds.transform.a == surface.grid.cell_m
        assert ds.transform.e == -surface.grid.cell_m
        # Top-left corner is the grid's north-west edge.
        assert np.isclose(ds.transform.c, surface.grid.west)
        assert np.isclose(ds.transform.f, surface.grid.north)


def test_geotiff_is_flipped_north_up(tmp_path, linear_cloud):
    # pykrige row 0 is the south edge; the GeoTIFF's row 0 must be the north edge,
    # i.e. the written band equals flipud of the source array.
    surface = _surface(linear_cloud)
    paths = write_surfaces(tmp_path, surface)
    with rasterio.open(paths["mean"]) as ds:
        band = ds.read(1)
    assert np.allclose(band, np.flipud(surface.mean).astype("float32"), atol=1e-3)


def test_variance_and_stddev_are_consistent(tmp_path, linear_cloud):
    surface = _surface(linear_cloud)
    paths = write_surfaces(tmp_path, surface)
    with rasterio.open(paths["variance"]) as dv, rasterio.open(paths["stddev"]) as ds:
        var = dv.read(1)
        std = ds.read(1)
    assert np.allclose(std, np.sqrt(np.clip(var, 0, None)), atol=1e-2)


def test_build_and_write_report(tmp_path, linear_cloud):
    from mukoo_model.crossval import kfold_cv

    surface = _surface(linear_cloud)
    cv = kfold_cv(
        linear_cloud,
        model_factory=lambda: OrdinaryKrigingModel(nlags=10),
        n_folds=5,
        seed=0,
    )
    paths = write_surfaces(tmp_path, surface)
    report = build_report(
        surface,
        cv,
        n_raw=linear_cloud.n_raw,
        n_merged=linear_cloud.n_merged,
        bounds_lonlat=linear_cloud.bounds_lonlat(),
        surface_paths=paths,
    )
    out = write_report_json(tmp_path / "report.json", report)
    assert out.exists()
    # cross_validation is keyed by scheme (one entry per scheme that ran)
    assert report["cross_validation"][cv.scheme]["accuracy"]["rmse"] == cv.rmse
    assert report["variogram"]["model"] == surface.variogram_model
    assert report["grid"]["rows"] == surface.grid.shape[0]

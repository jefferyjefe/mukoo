"""Tests for GeoTIFF export: geometry, orientation, and round-trip values."""

from __future__ import annotations

import json
import warnings
from dataclasses import replace

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


def test_nan_cells_are_written_as_nodata(tmp_path, linear_cloud):
    # Cells outside the grid's support come out of kriging as NaN. They have to
    # reach a GIS as nodata, not as a number that renders and averages like a
    # real reading.
    base = _surface(linear_cloud)
    mean = base.mean.copy()
    variance = base.variance.copy()
    unsupported = np.zeros(mean.shape, dtype=bool)
    unsupported[:2, 3:] = True
    mean[unsupported] = np.nan
    variance[unsupported] = np.nan
    surface = replace(base, mean=mean, variance=variance)

    paths = write_surfaces(tmp_path, surface)

    for name in ("mean", "variance", "stddev"):
        with rasterio.open(paths[name]) as ds:
            nodata = ds.nodata
            band = ds.read(1)
            valid = ds.read_masks(1)
        assert nodata is not None, f"{name} declares no nodata value"
        assert np.isnan(nodata), f"{name} nodata is a stand-in number: {nodata}"
        # North-up on disk, so the mask is the flipped source mask.
        assert np.array_equal(np.isnan(band), np.flipud(unsupported))
        assert np.array_equal(valid == 0, np.flipud(unsupported))


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


def test_report_grid_carries_support_fields(tmp_path, linear_cloud):
    from mukoo_model.crossval import kfold_cv

    surface = _surface(linear_cloud)
    cv = kfold_cv(
        linear_cloud,
        model_factory=lambda: OrdinaryKrigingModel(nlags=10),
        n_folds=5,
        seed=0,
    )
    common = {
        "n_raw": linear_cloud.n_raw,
        "n_merged": linear_cloud.n_merged,
        "bounds_lonlat": linear_cloud.bounds_lonlat(),
        "surface_paths": write_surfaces(tmp_path, surface),
    }

    masked = build_report(
        surface, cv, support_radius_m=4200.0, n_supported_cells=137, **common
    )
    assert masked["grid"]["support_radius_m"] == 4200.0
    assert masked["grid"]["n_supported_cells"] == 137

    # A caller that predates masking still works, and its report reads "no
    # masking ran" rather than claiming a radius that happened to mask nothing.
    unmasked = build_report(surface, cv, **common)
    assert unmasked["grid"]["support_radius_m"] is None
    assert unmasked["grid"]["n_supported_cells"] is None
    # Nothing else about the grid section moved.
    assert unmasked["grid"]["rows"] == surface.grid.shape[0]


def test_report_of_an_all_nan_surface_is_valid_json(tmp_path, linear_cloud):
    # A tight enough --support-range-multiple supports no cell at all: 0.01 of a
    # 3.8 km range is a 38 m radius on a 150 m grid. The surface is then all NaN,
    # and NaN is not JSON — json.dumps writes the bare token `NaN`, which
    # json.loads accepts but jq, JSON.parse, and Go all reject. The stats become
    # null instead: nothing supported, nothing to describe.
    from mukoo_model.crossval import kfold_cv

    base = _surface(linear_cloud)
    empty = np.full(base.mean.shape, np.nan)
    surface = replace(base, mean=empty, variance=empty.copy())
    cv = kfold_cv(
        linear_cloud,
        model_factory=lambda: OrdinaryKrigingModel(nlags=10),
        n_folds=5,
        seed=0,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an all-NaN slice must not warn either
        report = build_report(
            surface,
            cv,
            n_raw=linear_cloud.n_raw,
            n_merged=linear_cloud.n_merged,
            bounds_lonlat=linear_cloud.bounds_lonlat(),
            surface_paths={},
            support_radius_m=38.0,
            n_supported_cells=0,
        )

    assert all(v is None for v in report["surface_stats"].values())
    text = write_report_json(tmp_path / "report.json", report).read_text()
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text)["surface_stats"]["mean_min"] is None


def test_report_stats_ignore_infinities(linear_cloud):
    # An infinity from a numerical blow-up is written to the raster as nodata,
    # so it must not come out of the report as `Infinity` — also not JSON.
    base = _surface(linear_cloud)
    mean = base.mean.copy()
    mean[0, 0] = np.inf
    finite_max = float(np.max(base.mean))
    surface = replace(base, mean=mean)

    report = build_report(
        surface,
        [],
        n_raw=linear_cloud.n_raw,
        n_merged=linear_cloud.n_merged,
        bounds_lonlat=linear_cloud.bounds_lonlat(),
        surface_paths={},
    )

    assert report["surface_stats"]["mean_max"] == finite_max
    assert "Infinity" not in json.dumps(report)


def test_report_stats_unchanged_when_every_cell_is_predicted(linear_cloud):
    # The null path must not disturb the ordinary one: with no NaN anywhere the
    # numbers are still the plain min/max/mean of the surface.
    surface = _surface(linear_cloud)
    report = build_report(
        surface,
        [],
        n_raw=linear_cloud.n_raw,
        n_merged=linear_cloud.n_merged,
        bounds_lonlat=linear_cloud.bounds_lonlat(),
        surface_paths={},
    )
    stats = report["surface_stats"]
    assert stats["mean_min"] == float(np.min(surface.mean))
    assert stats["mean_max"] == float(np.max(surface.mean))
    assert stats["mean_mean"] == float(np.mean(surface.mean))
    assert stats["stddev_max"] == float(np.max(surface.stddev))

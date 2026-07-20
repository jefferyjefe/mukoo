"""Shared fixtures: synthetic point clouds with a known field.

The unit tests deliberately avoid the database. A synthetic field with a known
gradient and controlled noise lets us assert that kriging recovers the field and
that cross-validation reports sane, bounded metrics — properties that hold
regardless of what is in PostGIS.
"""

from __future__ import annotations

import numpy as np
import pytest

from mukoo_model.data import PointCloud


def make_linear_cloud(n: int = 300, noise: float = 0.5, seed: int = 0) -> PointCloud:
    """A smooth planar field over a ~10 km x 10 km UTM patch, plus light noise.

    RSRP-like values around -100 dBm with a spatial gradient, so kriging has real
    structure to exploit and cross-validation skill should be high.
    """
    rng = np.random.RandomState(seed)
    x = rng.rand(n) * 10000.0
    y = rng.rand(n) * 10000.0
    z = -100.0 + 6e-4 * x + 4e-4 * y + rng.randn(n) * noise
    lon = -81.8 + x / 1e5
    lat = 32.35 + y / 1e5
    return PointCloud(
        lon=lon, lat=lat, x=x, y=y, values=z, crs_epsg=32617, metric="rsrp", n_raw=n
    )


@pytest.fixture
def linear_cloud() -> PointCloud:
    return make_linear_cloud()

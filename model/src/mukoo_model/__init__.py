"""Mukoo coverage model.

Turns the point measurements collected by the ingestion API into continuous
coverage surfaces. The first model implemented here is **ordinary kriging** of
RSRP: it reads points from PostGIS, projects them into a metric UTM grid, fits
a variogram, and predicts both a mean surface and a kriging-variance
(uncertainty) surface — with k-fold cross-validation to decide whether the data
is dense enough to interpolate in the first place.

This is a separate installable package (``mukoo-model``) so the modelling
pipeline runs and scales independently of the API; it only ever *reads* the
``measurements`` table.

Typical use::

    from mukoo_model import Config, run
    result = run(Config.from_env())
    print(result.cv.summary())

or the CLI::

    mukoo-krige --metric rsrp
"""

from .config import Config
from .crossval import CVResult, kfold_cv
from .data import PointCloud, load_rsrp_points, utm_epsg_for
from .kriging import Grid, OrdinaryKrigingModel, SurfaceResult, make_grid
from .pipeline import PipelineResult, run

__version__ = "0.1.0"

__all__ = [
    "Config",
    "CVResult",
    "kfold_cv",
    "PointCloud",
    "load_rsrp_points",
    "utm_epsg_for",
    "Grid",
    "OrdinaryKrigingModel",
    "SurfaceResult",
    "make_grid",
    "PipelineResult",
    "run",
    "__version__",
]

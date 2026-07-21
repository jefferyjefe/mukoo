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

from .claims import (
    ClaimedHexes,
    ClaimsResult,
    evaluate_claims,
    read_claimed_hexes,
    write_claims_outputs,
)
from .config import Config
from .crossval import CVResult, block_cv, kfold_cv, session_cv
from .data import PointCloud, load_rsrp_points, utm_epsg_for
from .kriging import Grid, OrdinaryKrigingModel, SurfaceResult, make_grid
from .pipeline import PipelineResult, run
from .raster import load_grid_surface
from .refresh import run_refresh
from .regression import PathLossKrigingModel
from .roads import NearestRoad, RoadNetwork, fetch_roads
from .route import apply_visit_order, order_route, write_gpx
from .suggest import (
    Suggestion,
    expected_reduction_scores,
    suggest_targets,
    suggestions_to_geojson,
    weakness_weight,
    write_suggestions_geojson,
)
from .suggest_pipeline import SuggestResult, run_suggest

__version__ = "0.1.0"

__all__ = [
    "ClaimedHexes",
    "ClaimsResult",
    "evaluate_claims",
    "read_claimed_hexes",
    "write_claims_outputs",
    "Config",
    "CVResult",
    "kfold_cv",
    "session_cv",
    "block_cv",
    "PathLossKrigingModel",
    "run_refresh",
    "order_route",
    "apply_visit_order",
    "write_gpx",
    "expected_reduction_scores",
    "weakness_weight",
    "PointCloud",
    "load_rsrp_points",
    "utm_epsg_for",
    "Grid",
    "OrdinaryKrigingModel",
    "SurfaceResult",
    "make_grid",
    "PipelineResult",
    "run",
    "load_grid_surface",
    "RoadNetwork",
    "NearestRoad",
    "fetch_roads",
    "Suggestion",
    "suggest_targets",
    "suggestions_to_geojson",
    "write_suggestions_geojson",
    "SuggestResult",
    "run_suggest",
    "__version__",
]

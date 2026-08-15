"""Pipeline grid-support helpers: the radius the mask is drawn with, and the
two fields that describe it in the run report.

Both are pure functions of a fitted model / a grid, so they are exercised here
against a three-line fake model and a synthetic cloud — no database, and no
kriging run, which is the point: these decide how much of the box gets
predicted at all.
"""

from __future__ import annotations

from mukoo_model.config import Config
from mukoo_model.kriging import make_grid
from mukoo_model.pipeline import support_radius_m, support_report_fields

# A fitted exponential variogram on the real survey: 3.8 km of correlation.
FITTED_RANGE_M = 3791.0


class FakeFittedModel:
    """Stands in for a fitted model: ``support_radius_m`` reads nothing else."""

    def __init__(self, **params):
        self.variogram_params = dict(params)


def _config(multiple: float) -> Config:
    return Config(support_range_multiple=multiple)


def test_radius_is_range_times_multiple():
    model = FakeFittedModel(model="exponential", range_m=FITTED_RANGE_M)
    assert support_radius_m(model, _config(1.0)) == FITTED_RANGE_M
    assert support_radius_m(model, _config(2.5)) == FITTED_RANGE_M * 2.5
    # Small multiples are legal and are how you tighten the mask; the radius
    # follows the multiple all the way down rather than snapping to a floor.
    assert support_radius_m(model, _config(0.01)) == FITTED_RANGE_M * 0.01


def test_zero_multiple_disables_masking_rather_than_masking_everything():
    # 0 means "predict the whole box", not "a radius of zero metres" — the
    # latter would support no cell at all and export an all-NaN surface.
    model = FakeFittedModel(model="exponential", range_m=FITTED_RANGE_M)
    assert support_radius_m(model, _config(0.0)) is None


def test_negative_multiple_disables_masking():
    # Config rejects negatives at both entry points, so this is only reachable
    # by constructing a Config by hand. It still has to mean "off": the flag's
    # help and the env parser's error both promise that nothing below zero can
    # quietly invert the mask.
    model = FakeFittedModel(model="exponential", range_m=FITTED_RANGE_M)
    assert support_radius_m(model, _config(-5.0)) is None


def test_rangeless_variogram_family_falls_back_to_no_masking():
    # Linear and power variograms never plateau, so they have no range to scale
    # — masking off is honest, inventing a radius from the slope is not.
    linear = FakeFittedModel(model="linear", slope=0.004, nugget=1.2)
    power = FakeFittedModel(model="power", scale=0.01, exponent=1.4, nugget=1.2)
    assert support_radius_m(linear, _config(1.0)) is None
    assert support_radius_m(power, _config(1.0)) is None


def test_report_fields_are_both_none_when_no_mask_ran(linear_cloud):
    # "No masking ran" and "a mask kept every cell" are different claims, and
    # the report distinguishes them by nulling both fields — so an unmasked grid
    # must not report its full cell count beside a null radius.
    grid = make_grid(linear_cloud, cell_m=500.0)
    assert support_report_fields(grid) == (None, None)


def test_report_fields_describe_the_mask_that_ran(linear_cloud):
    grid = make_grid(linear_cloud, cell_m=500.0, support_radius_m=300.0)
    radius, n_supported = support_report_fields(grid)
    assert radius == 300.0
    assert n_supported == grid.n_supported
    rows, cols = grid.shape
    assert 0 < n_supported < rows * cols

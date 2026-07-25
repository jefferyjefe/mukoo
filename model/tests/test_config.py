"""Config env parsing: kriging mode, anisotropy, dead-zone floor, run dedupe."""

from __future__ import annotations

import pytest

from mukoo_model.config import Config, parse_anisotropy, parse_bool
from mukoo_model.data import load_rsrp_points

ENV_VARS = (
    "MUKOO_KRIGING",
    "MUKOO_ANISOTROPY",
    "MUKOO_NONE_FLOOR",
    "MUKOO_DEDUPE_RUNS",
)


def test_defaults(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    config = Config.from_env()
    assert config.kriging_mode == "ordinary"
    assert config.anisotropy == (1.0, 0.0)
    assert config.none_floor is None
    # Off by default: published reports and row counts stay reproducible.
    assert config.dedupe_runs is False


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MUKOO_KRIGING", "pathloss")
    monkeypatch.setenv("MUKOO_ANISOTROPY", "3:150")
    monkeypatch.setenv("MUKOO_NONE_FLOOR", "-127")
    monkeypatch.setenv("MUKOO_DEDUPE_RUNS", "1")
    config = Config.from_env()
    assert config.kriging_mode == "pathloss"
    assert config.anisotropy == (3.0, 150.0)
    assert config.none_floor == -127.0
    assert config.dedupe_runs is True


@pytest.mark.parametrize(
    "raw,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("0", False), ("false", False), ("no", False), ("off", False)],
)
def test_parse_bool_accepts_common_spellings(raw, expected):
    assert parse_bool(raw) is expected


def test_parse_bool_rejects_nonsense():
    with pytest.raises(ValueError, match="boolean"):
        parse_bool("maybe")


def test_dedupe_runs_env_rejects_nonsense(monkeypatch):
    monkeypatch.setenv("MUKOO_DEDUPE_RUNS", "sometimes")
    with pytest.raises(ValueError, match="boolean"):
        Config.from_env()


def test_invalid_kriging_mode_rejected(monkeypatch):
    monkeypatch.setenv("MUKOO_KRIGING", "universal")
    with pytest.raises(ValueError, match="MUKOO_KRIGING"):
        Config.from_env()


def test_parse_anisotropy_rejects_malformed():
    with pytest.raises(ValueError, match="SCALING:ANGLE"):
        parse_anisotropy("3x150")


def test_none_floor_requires_rsrp_metric():
    with pytest.raises(ValueError, match="none_floor"):
        load_rsrp_points(None, metric="rsrq", none_floor=-127.0)

"""Config env parsing: kriging mode, anisotropy, dead-zone floor, run dedupe,
grid-support radius — plus the CLI flags that must agree with them."""

from __future__ import annotations

import argparse

import pytest

from mukoo_model.cli import _parse_support_range_multiple
from mukoo_model.cli import main as cli_main
from mukoo_model.config import (
    Config,
    parse_anisotropy,
    parse_bool,
    parse_support_range_multiple,
)
from mukoo_model.data import load_rsrp_points

ENV_VARS = (
    "MUKOO_KRIGING",
    "MUKOO_ANISOTROPY",
    "MUKOO_NONE_FLOOR",
    "MUKOO_DEDUPE_RUNS",
    "MUKOO_SUPPORT_RANGE_MULTIPLE",
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
    # On by default: one variogram range of support around the drives.
    assert config.support_range_multiple == 1.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MUKOO_KRIGING", "pathloss")
    monkeypatch.setenv("MUKOO_ANISOTROPY", "3:150")
    monkeypatch.setenv("MUKOO_NONE_FLOOR", "-127")
    monkeypatch.setenv("MUKOO_DEDUPE_RUNS", "1")
    monkeypatch.setenv("MUKOO_SUPPORT_RANGE_MULTIPLE", "1.5")
    config = Config.from_env()
    assert config.kriging_mode == "pathloss"
    assert config.anisotropy == (3.0, 150.0)
    assert config.none_floor == -127.0
    assert config.dedupe_runs is True
    assert config.support_range_multiple == 1.5


def test_support_range_multiple_zero_disables_masking(monkeypatch):
    # "0" is a chosen value, not an absent one, so it must survive parsing —
    # it is how an operator asks for the whole bounding box back.
    monkeypatch.setenv("MUKOO_SUPPORT_RANGE_MULTIPLE", "0")
    assert Config.from_env().support_range_multiple == 0.0


def test_negative_support_range_multiple_rejected(monkeypatch):
    # A negative multiple would fall through to "masking off", silently handing
    # back the whole bounding box under a flag that reads like a tighter radius.
    monkeypatch.setenv("MUKOO_SUPPORT_RANGE_MULTIPLE", "-1")
    with pytest.raises(ValueError, match="must be >= 0"):
        Config.from_env()


def test_parse_support_range_multiple_accepts_zero_and_positives():
    assert parse_support_range_multiple("0") == 0.0
    assert parse_support_range_multiple("1.5") == 1.5


def test_cli_support_range_multiple_shares_the_env_parser():
    # One setting, two entry points. The flag routes through the same parser as
    # MUKOO_SUPPORT_RANGE_MULTIPLE (as --anisotropy does), so they cannot drift
    # on what they accept or on what a value means; the CLI wrapper only
    # restates the failure in argparse's own exception type.
    assert _parse_support_range_multiple("0") == 0.0
    assert _parse_support_range_multiple("2") == 2.0
    with pytest.raises(argparse.ArgumentTypeError, match="must be >= 0"):
        _parse_support_range_multiple("-5")


def test_cli_rejects_negative_support_range_multiple(capsys):
    # End to end through argparse: -5 must be refused at parse time, not
    # accepted as a float and quietly turned into "no masking" downstream.
    with pytest.raises(SystemExit) as exc:
        cli_main(["--support-range-multiple", "-5"])
    assert exc.value.code == 2
    assert "must be >= 0" in capsys.readouterr().err


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

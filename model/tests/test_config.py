"""Config env parsing: kriging mode and anisotropy."""

from __future__ import annotations

import pytest

from mukoo_model.config import Config, parse_anisotropy


def test_defaults(monkeypatch):
    for var in ("MUKOO_KRIGING", "MUKOO_ANISOTROPY"):
        monkeypatch.delenv(var, raising=False)
    config = Config.from_env()
    assert config.kriging_mode == "ordinary"
    assert config.anisotropy == (1.0, 0.0)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MUKOO_KRIGING", "pathloss")
    monkeypatch.setenv("MUKOO_ANISOTROPY", "3:150")
    config = Config.from_env()
    assert config.kriging_mode == "pathloss"
    assert config.anisotropy == (3.0, 150.0)


def test_invalid_kriging_mode_rejected(monkeypatch):
    monkeypatch.setenv("MUKOO_KRIGING", "universal")
    with pytest.raises(ValueError, match="MUKOO_KRIGING"):
        Config.from_env()


def test_parse_anisotropy_rejects_malformed():
    with pytest.raises(ValueError, match="SCALING:ANGLE"):
        parse_anisotropy("3x150")

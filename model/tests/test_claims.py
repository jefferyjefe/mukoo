"""Tests for the FCC BDC claims comparison (GeoPackage parsing + spatial join).

A GeoPackage is a SQLite file, so the tests build a miniature but spec-shaped
one from scratch — real gpkg_contents / gpkg_geometry_columns rows and real
GPKG geometry blobs — and drive the whole read → match → evaluate path.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import numpy as np
import pytest
import shapely
from shapely.geometry import box

from mukoo_model.claims import (
    evaluate_claims,
    parse_gpkg_geometry,
    plain_language_summary,
    read_claimed_hexes,
    write_claims_outputs,
)


def _gpkg_blob(geom, *, envelope: bool = False, srs_id: int = 4326) -> bytes:
    """A GeoPackage geometry blob: GP magic, flags, srs_id, [envelope], WKB."""
    flags = 0x01  # little-endian header
    env_bytes = b""
    if envelope:
        flags |= 0x02  # envelope indicator 1: [minx, maxx, miny, maxy]
        minx, miny, maxx, maxy = geom.bounds
        env_bytes = struct.pack("<4d", minx, maxx, miny, maxy)
    return b"GP" + bytes([0, flags]) + struct.pack("<i", srs_id) + env_bytes + shapely.to_wkb(geom)


def _write_gpkg(path: Path, rows, *, envelope: bool = False) -> Path:
    """A minimal BDC-shaped GeoPackage: (geom, minsignal, environmnt, h3)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, "
        "identifier TEXT, srs_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO gpkg_contents VALUES ('coverage', 'features', 'coverage', 4326)"
    )
    conn.execute(
        "CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
        "geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER)"
    )
    conn.execute(
        "INSERT INTO gpkg_geometry_columns VALUES "
        "('coverage', 'geom', 'POLYGON', 4326, 0, 0)"
    )
    conn.execute(
        "CREATE TABLE coverage (fid INTEGER PRIMARY KEY, geom BLOB, "
        "minsignal INTEGER, environmnt INTEGER, h3_res9_id TEXT)"
    )
    for i, (geom, minsig, env, h3) in enumerate(rows):
        conn.execute(
            "INSERT INTO coverage VALUES (?, ?, ?, ?, ?)",
            (i + 1, _gpkg_blob(geom, envelope=envelope), minsig, env, h3),
        )
    conn.commit()
    conn.close()
    return path


# Two adjacent 0.01°-square "hexes" and one far-away one.
HEX_A = box(-81.75, 32.40, -81.74, 32.41)  # env 1, claims -80
HEX_B = box(-81.74, 32.40, -81.73, 32.41)  # env 1, claims -100
HEX_OUTDOOR = box(-81.75, 32.40, -81.74, 32.41)  # env 0 twin of A, claims -60
HEX_FAR = box(-80.00, 33.00, -79.99, 33.01)  # env 1, outside any bbox

ROWS = [
    (HEX_A, -80, 1, "hexA"),
    (HEX_B, -100, 1, "hexB"),
    (HEX_OUTDOOR, -60, 0, "hexA0"),
    (HEX_FAR, -50, 1, "hexFar"),
]


@pytest.fixture(params=[False, True], ids=["no-envelope", "with-envelope"])
def gpkg(tmp_path, request) -> Path:
    return _write_gpkg(tmp_path / "claims.gpkg", ROWS, envelope=request.param)


def test_parse_gpkg_geometry_roundtrip():
    for envelope in (False, True):
        wkb, env = parse_gpkg_geometry(_gpkg_blob(HEX_A, envelope=envelope))
        assert shapely.from_wkb(wkb).equals(HEX_A)
        if envelope:
            assert env == pytest.approx(HEX_A.bounds)
        else:
            assert env is None


def test_parse_gpkg_geometry_rejects_garbage():
    with pytest.raises(ValueError):
        parse_gpkg_geometry(b"NOTGPKG")


def test_read_filters_environment_and_bbox(gpkg):
    hexes = read_claimed_hexes(gpkg, environment=1)
    assert sorted(hexes.h3_ids) == ["hexA", "hexB", "hexFar"]  # env-0 twin dropped

    bboxed = read_claimed_hexes(
        gpkg, environment=1, bbox=(-81.76, 32.39, -81.72, 32.42)
    )
    assert sorted(bboxed.h3_ids) == ["hexA", "hexB"]  # far hex pre-filtered

    outdoor = read_claimed_hexes(gpkg, environment=0)
    assert outdoor.h3_ids == ["hexA0"]


def test_read_rejects_non_bdc_layer(tmp_path):
    path = tmp_path / "notbdc.gpkg"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, "
        "identifier TEXT, srs_id INTEGER)"
    )
    conn.execute("INSERT INTO gpkg_contents VALUES ('t', 'features', 't', 4326)")
    conn.execute(
        "CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
        "geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER)"
    )
    conn.execute("INSERT INTO gpkg_geometry_columns VALUES ('t','geom','POLYGON',4326,0,0)")
    conn.execute("CREATE TABLE t (fid INTEGER PRIMARY KEY, geom BLOB)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="minsignal"):
        read_claimed_hexes(path)


def test_evaluate_inside_outside_and_violations(gpkg):
    hexes = read_claimed_hexes(gpkg, environment=1)
    # p0: in hexA (-80 claim), measured -95  -> violation, gap -15
    # p1: in hexA (-80 claim), measured -70  -> meets claim
    # p2: in hexB (-100 claim), measured -123 -> violation, gap -23 (worst)
    # p3: outside every claimed hex
    lon = np.array([-81.745, -81.744, -81.735, -81.60])
    lat = np.array([32.405, 32.406, 32.405, 32.405])
    rsrp = np.array([-95.0, -70.0, -123.0, -110.0])
    ids = np.array([10, 11, 12, 13])

    result = evaluate_claims(lon, lat, rsrp, ids, hexes)
    assert result.inside.sum() == 3
    assert result.violation.sum() == 2

    s = result.summary()
    assert s["points"] == {
        "total": 4,
        "inside_claimed_hex": 3,
        "outside_any_claim": 1,
    }
    assert s["violations"]["count"] == 2
    assert s["violations"]["worst_gap_db"] == -23.0
    assert s["violations"]["worst_point"]["measurement_id"] == 12
    assert s["violations"]["worst_point"]["h3_res9_id"] == "hexB"
    tiers = {t["claimed_minsignal_dbm"]: t for t in s["by_claimed_minsignal"]}
    assert tiers[-80.0]["points"] == 2 and tiers[-80.0]["violations"] == 1
    assert tiers[-100.0]["points"] == 1 and tiers[-100.0]["violations"] == 1


def test_boundary_point_matches_exactly_one_hex(gpkg):
    hexes = read_claimed_hexes(gpkg, environment=1)
    # Exactly on the shared edge of hexA and hexB.
    result = evaluate_claims(
        np.array([-81.74]),
        np.array([32.405]),
        np.array([-120.0]),
        np.array([1]),
        hexes,
    )
    assert result.inside.sum() == 1  # matched once, not double-counted


def test_outputs_written_and_ordered(tmp_path, gpkg):
    hexes = read_claimed_hexes(gpkg, environment=1)
    lon = np.array([-81.745, -81.735])
    lat = np.array([32.405, 32.405])
    result = evaluate_claims(
        lon, lat, np.array([-95.0, -123.0]), np.array([1, 2]), hexes
    )
    paths = write_claims_outputs(tmp_path, result, prefix="test")
    report = json.loads(paths["report"].read_text())
    geo = json.loads(paths["violations"].read_text())
    assert report["violations"]["count"] == 2
    assert geo["properties"]["count"] == 2
    gaps = [f["properties"]["gap_db"] for f in geo["features"]]
    assert gaps == sorted(gaps)  # worst (most negative) first
    assert "BELOW the filed minimum" in plain_language_summary(result)


def test_empty_claim_set_yields_all_outside(tmp_path):
    path = _write_gpkg(tmp_path / "empty.gpkg", [(HEX_FAR, -50, 0, "x")])
    hexes = read_claimed_hexes(path, environment=1)  # nothing has env=1
    result = evaluate_claims(
        np.array([-81.7]), np.array([32.4]), np.array([-100.0]), np.array([1]), hexes
    )
    assert result.inside.sum() == 0
    assert "nothing to compare" in plain_language_summary(result)

"""Tests for coordinate projection and duplicate collapsing."""

from __future__ import annotations

import types
from dataclasses import dataclass

import numpy as np

from mukoo_model.data import PointCloud, _collapse_coincident, load_rsrp_points, utm_epsg_for


@dataclass
class _Row:
    lon: float
    lat: float
    value: float
    session: str
    cell: str


class _FakeConn:
    """Records executed SQL and replays canned results.

    The model's unit tests stay off the database, but the run-dedupe lives in
    SQL, so this captures the statement the loader builds and lets the tests
    assert on its shape.
    """

    def __init__(self, rows, count):
        self.rows = rows
        self.count = count
        self.statements: list[str] = []

    def execute(self, sql):
        self.statements.append(str(sql))
        if "count(*)" in str(sql):
            return types.SimpleNamespace(scalar_one=lambda: self.count)
        return types.SimpleNamespace(all=lambda: self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _three_rows():
    return [
        _Row(-81.74, 32.40, -100.0, "s1", "c1"),
        _Row(-81.75, 32.41, -105.0, "s1", "c1"),
        _Row(-81.76, 32.42, -110.0, "s2", "c2"),
    ]


def test_utm_epsg_georgia_is_17n():
    # Statesboro, GA sits in UTM zone 17, northern hemisphere -> 32617.
    assert utm_epsg_for(-81.74, 32.40) == 32617


def test_utm_epsg_southern_hemisphere():
    # Same zone longitude but south of the equator -> 327xx.
    assert utm_epsg_for(-60.0, -33.0) == 32721


def test_collapse_coincident_averages_duplicate_locations():
    # Two points at (0,0) with values 10 and 20 must become one point at 15.
    x = np.array([0.0, 0.0, 100.0])
    y = np.array([0.0, 0.0, 100.0])
    v = np.array([10.0, 20.0, 5.0])
    cx, cy, cv, _ = _collapse_coincident(x, y, v, tol_m=1.0)
    assert cx.shape == (2,)
    # The collapsed group's value is the mean of the coincident pair.
    assert np.allclose(np.sort(cv), sorted([15.0, 5.0]))
    assert 15.0 in np.round(cv, 6)


def test_collapse_coincident_noop_when_all_distinct():
    x = np.array([0.0, 100.0, 200.0])
    y = np.array([0.0, 100.0, 200.0])
    v = np.array([1.0, 2.0, 3.0])
    cx, cy, cv, _ = _collapse_coincident(x, y, v)
    assert cx.shape == (3,)
    assert np.array_equal(cv, v)


def test_collapse_snaps_within_tolerance():
    # Sub-metre GPS jitter should be treated as the same location.
    x = np.array([0.0, 0.3])
    y = np.array([0.0, 0.2])
    v = np.array([10.0, 30.0])
    cx, cy, cv, _ = _collapse_coincident(x, y, v, tol_m=1.0)
    assert cx.shape == (1,)
    assert np.isclose(cv[0], 20.0)


def test_collapse_carries_labels_first_of_group():
    # A merged group keeps its first member's labels (session, cell, ...).
    x = np.array([0.0, 0.3, 100.0])
    y = np.array([0.0, 0.2, 100.0])
    v = np.array([10.0, 30.0, 5.0])
    session = np.array(["s1", "s2", "s3"], dtype=object)
    cx, cy, cv, (kept,) = _collapse_coincident(x, y, v, tol_m=1.0, labels=(session,))
    assert cx.shape == (2,)
    assert set(kept) == {"s1", "s3"}  # s2 merged into the s1 group


def test_dedupe_off_queries_the_table_directly():
    conn = _FakeConn(_three_rows(), count=3)
    cloud = load_rsrp_points(_FakeEngine(conn), metric="rsrp", dedupe_runs=False)
    (select,) = conn.statements  # no second count(*) query when dedupe is off
    assert "FROM measurements WHERE" in select
    assert "lag(" not in select
    assert cloud.n_before_dedupe is None
    assert cloud.n_dedupe_dropped == 0


def test_dedupe_on_keeps_only_run_starts():
    # 3 rows survive the run filter out of 11 matching the predicate.
    conn = _FakeConn(_three_rows(), count=11)
    cloud = load_rsrp_points(_FakeEngine(conn), metric="rsrp", dedupe_runs=True)
    select = conn.statements[0]
    # The run boundary is defined per session, on the phone's clock, over the
    # whole signal triple, with NULL == NULL counting as unchanged.
    assert "PARTITION BY m.session_id ORDER BY m.recorded_at" in select
    assert select.count("IS NOT DISTINCT FROM") == 3
    for col in ("rsrp", "rsrq", "sinr"):
        assert f"lag(m.{col})" in select
    assert cloud.n_raw == 3
    assert cloud.n_before_dedupe == 11
    assert cloud.n_dedupe_dropped == 8


def test_dedupe_also_treats_a_shared_modem_stamp_as_a_reread():
    """The modem saying "same measurement" must count, not just equal values.

    Verified semantically against PostGIS separately; this pins the clause so the
    modem_reported_at branch cannot be dropped from the query by accident.
    """
    conn = _FakeConn(_three_rows(), count=9)
    load_rsrp_points(_FakeEngine(conn), metric="rsrp", dedupe_runs=True)
    select = conn.statements[0]
    assert "lag(m.modem_reported_at)" in select
    assert "s.modem_reported_at = s._prev_modem" in select
    # Both tests must be present, and joined by OR: either identifies a re-read,
    # so a NULL stamp falls back to the value comparison instead of keeping rows.
    assert "s.modem_reported_at IS NOT NULL" in select
    assert "s._prev_modem IS NOT NULL" in select


def test_dedupe_applies_caller_where_to_both_queries():
    conn = _FakeConn(_three_rows(), count=5)
    load_rsrp_points(
        _FakeEngine(conn), metric="rsrp", where="carrier = 'Verizon'", dedupe_runs=True
    )
    select, count = conn.statements
    # The count must use the same predicate, or n_dedupe_dropped is meaningless.
    assert "carrier = 'Verizon'" in select
    assert "carrier = 'Verizon'" in count
    assert "lag(" not in count  # the baseline count is un-deduped by definition


def test_n_dedupe_dropped_defaults_to_zero():
    cloud = PointCloud(
        lon=np.array([0.0]),
        lat=np.array([0.0]),
        x=np.array([0.0]),
        y=np.array([0.0]),
        values=np.array([-100.0]),
        crs_epsg=32617,
        metric="rsrp",
        n_raw=1,
    )
    assert cloud.n_dedupe_dropped == 0

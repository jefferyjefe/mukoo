"""Tests for route ordering (NN + 2-opt) and GPX export."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import numpy as np

from mukoo_model.route import apply_visit_order, order_route, write_gpx
from mukoo_model.suggest import Suggestion


def _tour_len(x, y, order):
    return sum(
        np.hypot(x[order[i + 1]] - x[order[i]], y[order[i + 1]] - y[order[i]])
        for i in range(len(order) - 1)
    )


def test_order_route_line_is_traversed_in_order():
    # Points on a line, given shuffled: optimal open tour from index 0 (=0.0)
    # walks straight down the line.
    x = np.array([0.0, 5000.0, 1000.0, 4000.0, 2000.0, 3000.0])
    y = np.zeros(6)
    order = order_route(x, y, start=0)
    assert order[0] == 0
    assert [round(x[i]) for i in order] == [0, 1000, 2000, 3000, 4000, 5000]


def test_two_opt_beats_naive_rank_order():
    # A zig-zag rank order across two clusters; 2-opt must do no worse than
    # visiting by rank, and here strictly better.
    x = np.array([0.0, 9000.0, 100.0, 9100.0, 200.0])
    y = np.array([0.0, 0.0, 100.0, 100.0, 200.0])
    order = order_route(x, y, start=0)
    assert _tour_len(x, y, order) < _tour_len(x, y, [0, 1, 2, 3, 4])


def test_order_route_handles_trivial_sizes():
    assert order_route(np.array([]), np.array([])) == []
    assert order_route(np.array([1.0]), np.array([2.0])) == [0]


def _sugg(rank, x, y):
    return Suggestion(
        rank=rank,
        lon=-81.7 + x / 1e5,
        lat=32.4 + y / 1e5,
        x=x,
        y=y,
        stddev=8.0,
        road_name=f"Road {rank}",
        road_dist_m=50.0,
        score=10.0 - rank,
    )


def test_apply_visit_order_preserves_rank_and_fills_order():
    suggestions = [_sugg(1, 0.0, 0.0), _sugg(2, 9000.0, 0.0), _sugg(3, 500.0, 0.0)]
    out = apply_visit_order(suggestions)
    assert [s.rank for s in out] == [1, 2, 3]  # rank order untouched
    orders = {s.rank: s.visit_order for s in out}
    # anchored at rank 1; rank 3 (500m away) comes before rank 2 (9km away)
    assert orders[1] == 1
    assert orders[3] == 2
    assert orders[2] == 3


def test_write_gpx_valid_xml_with_waypoints_and_route(tmp_path):
    suggestions = apply_visit_order(
        [_sugg(1, 0.0, 0.0), _sugg(2, 9000.0, 0.0), _sugg(3, 500.0, 0.0)]
    )
    path = write_gpx(tmp_path / "route.gpx", suggestions, metric="rsrp")
    root = ET.parse(path).getroot()
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    wpts = root.findall("g:wpt", ns)
    rtepts = root.findall("g:rte/g:rtept", ns)
    assert len(wpts) == 3
    assert len(rtepts) == 3
    # waypoints are emitted in visit order and named accordingly
    names = [w.find("g:name", ns).text for w in wpts]
    assert names[0].startswith("T1 ")
    assert names[1].startswith("T2 ")
    # coordinates present and in range
    for w in wpts:
        assert -90 <= float(w.get("lat")) <= 90
        assert -180 <= float(w.get("lon")) <= 180

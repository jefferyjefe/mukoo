"""Order the suggested targets into one efficient drive, and export GPX.

The suggester ranks targets by information value; this module answers the
practical follow-up — "in what order do I actually drive them?" — with a
straight-line TSP heuristic: nearest-neighbour construction from the top-ranked
target, then 2-opt until no swap shortens the tour. Ten-ish points, so this is
exact-enough and instant. Distances are straight-line in the projected CRS, not
road-network distances; for spread-out rural targets that ordering is almost
always the same one the road network gives, and the navigation app does the
real routing per leg anyway.

The GPX file carries one waypoint per target (named by visit order, described
with rank/road/σ) plus an ordered ``<rte>``, so any navigation app or Garmin
can load the whole drive.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

from .suggest import Suggestion


def order_route(x: np.ndarray, y: np.ndarray, *, start: int = 0) -> "list[int]":
    """Visiting order over points (indices), nearest-neighbour + 2-opt.

    ``start`` anchors the tour (default: index 0, the top-ranked target). Open
    tour, not a loop — the drive ends at the last target, not back at the first.
    """
    n = int(np.asarray(x).shape[0])
    if n <= 1:
        return list(range(n))
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    d = np.sqrt((x[:, None] - x[None, :]) ** 2 + (y[:, None] - y[None, :]) ** 2)

    # nearest-neighbour construction
    unvisited = set(range(n))
    order = [start]
    unvisited.discard(start)
    while unvisited:
        last = order[-1]
        nxt = min(unvisited, key=lambda j: d[last, j])
        order.append(nxt)
        unvisited.discard(nxt)

    # 2-opt: reverse any segment that shortens the open tour, to convergence.
    def tour_len(o: "list[int]") -> float:
        return float(sum(d[o[i], o[i + 1]] for i in range(len(o) - 1)))

    improved = True
    while improved:
        improved = False
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                candidate = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                if tour_len(candidate) < tour_len(order) - 1e-9:
                    order = candidate
                    improved = True
    return order


def apply_visit_order(suggestions: "list[Suggestion]") -> "list[Suggestion]":
    """Return suggestions with ``visit_order`` filled from the 2-opt tour.

    Rank (information priority) is untouched; visit_order is the driving order,
    anchored at rank 1. The returned list keeps the original rank ordering.
    """
    from dataclasses import replace

    if not suggestions:
        return []
    xs = np.array([s.x for s in suggestions])
    ys = np.array([s.y for s in suggestions])
    order = order_route(xs, ys, start=0)
    position = {idx: pos + 1 for pos, idx in enumerate(order)}
    return [replace(s, visit_order=position[i]) for i, s in enumerate(suggestions)]


def write_gpx(
    path: Path, suggestions: "list[Suggestion]", *, metric: str = "rsrp"
) -> Path:
    """Write waypoints + an ordered route for the suggested drive.

    Waypoints are emitted in visit order (falling back to rank order when
    visit_order is unset), named ``T<visit_order>`` so the numbers on a nav
    screen match the order to drive them in.
    """
    ordered = sorted(
        suggestions, key=lambda s: s.visit_order if s.visit_order else s.rank
    )
    unit = "dBm" if metric in {"rsrp", "rsrq"} else metric

    def wpt(tag: str, s: Suggestion) -> str:
        road = escape(s.road_name) if s.road_name else "unnamed road"
        name = f"T{s.visit_order or s.rank} {road}"
        desc = f"rank {s.rank}, sigma {s.stddev:.2f} {unit}"
        return (
            f'  <{tag} lat="{s.lat:.6f}" lon="{s.lon:.6f}">'
            f"<name>{escape(name)}</name><desc>{escape(desc)}</desc></{tag}>"
        )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="mukoo-model" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
    ]
    lines += [wpt("wpt", s) for s in ordered]
    lines.append("  <rte>")
    lines.append(f"    <name>Mukoo {escape(metric)} drive suggestions</name>")
    lines += ["  " + wpt("rtept", s) for s in ordered]
    lines.append("  </rte>")
    lines.append("</gpx>")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path

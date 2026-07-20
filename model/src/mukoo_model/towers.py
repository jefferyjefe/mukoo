"""Estimate serving-tower positions from the measurements themselves.

There is no tower database, but every sample carries its serving ``cell_id``
and RSRP. Received power falls off with distance, so a cell's tower sits near
where that cell was heard loudest: we estimate each tower as the power-weighted
centroid of its cell's sample positions, with weights on a *linear* power scale
(10^(RSRP/10)) so the strongest readings dominate and the weak fringe barely
pulls. It's a bootstrap, not surveying — good enough to give the path-loss
trend a distance covariate.
"""

from __future__ import annotations

import numpy as np

# A cell heard only a handful of times gives a junk centroid; skip it. The
# fallback for points served by skipped/unknown cells is simply "distance to
# the nearest estimated tower", so coverage of every point is not required.
MIN_SAMPLES_PER_CELL = 20


def estimate_towers(
    x: np.ndarray,
    y: np.ndarray,
    rsrp: np.ndarray,
    cell: np.ndarray,
    *,
    min_samples: int = MIN_SAMPLES_PER_CELL,
) -> np.ndarray:
    """Return an (m, 2) array of estimated tower positions (projected metres).

    One row per cell_id with at least ``min_samples`` samples; empty labels
    (samples with no cell id) are ignored. Returns an empty (0, 2) array when
    nothing qualifies — callers must handle that.
    """
    cell = np.asarray(cell, dtype=object)
    towers = []
    for c in np.unique(cell):
        if not c:  # "" = no serving cell recorded
            continue
        mask = cell == c
        if int(mask.sum()) < min_samples:
            continue
        w = np.power(10.0, np.asarray(rsrp, dtype=np.float64)[mask] / 10.0)
        w_sum = float(w.sum())
        if w_sum <= 0.0:
            continue
        towers.append(
            (float(np.sum(x[mask] * w) / w_sum), float(np.sum(y[mask] * w) / w_sum))
        )
    if not towers:
        return np.empty((0, 2), dtype=np.float64)
    return np.asarray(towers, dtype=np.float64)


def nearest_tower_distance(
    x: np.ndarray, y: np.ndarray, towers: np.ndarray, *, min_dist_m: float = 30.0
) -> np.ndarray:
    """Distance (m) from each point to its nearest estimated tower.

    Clipped below at ``min_dist_m``: the log-distance trend must never see a
    zero (log blows up), and within a few tens of metres of a tower the
    far-field path-loss model is meaningless anyway.
    """
    if towers.shape[0] == 0:
        raise ValueError("no towers to measure distance to")
    dx = np.asarray(x, dtype=np.float64)[:, None] - towers[None, :, 0]
    dy = np.asarray(y, dtype=np.float64)[:, None] - towers[None, :, 1]
    d = np.sqrt(dx * dx + dy * dy).min(axis=1)
    return np.clip(d, min_dist_m, None)

"""Render the README coverage-vs-claim map (docs/coverage_map.png).

Draws the audit at the carrier's own H3 hex level: the claimed-coverage cells as
recessive context, and the cells our measurements contradicted shaded by how far
below the filed minimum they came in. Layers, bottom to top: OSM roads (from the
osm_cache, if present), all claimed hexes in the area (from the BDC GeoPackage,
if present), then the violation hexes. Everything is projected to the local UTM
zone so distances and aspect are true, with a 2 km scale bar.

**No individual measurements are plotted.** An earlier version drew all 2,039
points, which reconstructed every drive: route geometry, where each trip started
and ended, and — because a stationary vehicle re-reads one latched modem value —
dense clusters exactly where the car sat still. The hex aggregate says the same
thing about the filing (which claimed cells failed, and by how much) while
carrying none of that. Shading by a per-hex *rate* rather than a count is part of
this: a count would put the dwell signal straight back on the map.

The map refuses to disagree with the shipped audit: the aggregate's totals are
cross-checked against claims_report.json and the run aborts on any mismatch — if
data changed, re-run ``mukoo-claims`` and the aggregator first, then this script.

Reads only committed artifacts (the hex GeoJSON + the report), so it needs no
database. The GeoPackage and road cache are optional context; without them the
map still renders, showing the violation hexes alone.

Usage (after ``mukoo-claims`` and ``aggregate_violations_by_hex.py``)::

    pip install -e 'model[viz]'
    python docs/make_coverage_map.py
    python docs/make_coverage_map.py --shade-by share   # rate, not magnitude
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.patches import Patch
from shapely.geometry import shape

from mukoo_model.config import DEFAULT_OUTPUT_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent

# Chrome and ink. Claimed coverage is *context*, so it wears neutral chart
# chrome and the one hue on the map belongs to the finding.
SURFACE = "#fcfcfb"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
ROAD = "#e1e0d9"
CLAIM_FILL, CLAIM_EDGE = "#f0efec", "#e1e0d9"
HAIRLINE = "#d9d8d2"

# Sequential ramp, one hue light->dark (blue 100..700). Magnitude gets a
# sequential scale, never a rainbow and never a diverging one: "how far below
# the claim" has no meaningful midpoint to diverge around.
BLUE_RAMP = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
    "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
]
CMAP = LinearSegmentedColormap.from_list("mukoo_shortfall", BLUE_RAMP)

TECHNOLOGY = {400: "LTE", 500: "5G-NR"}
ENVIRONMENT = {0: "outdoor stationary", 1: "in-vehicle"}

SHADE_BY = {
    # key -> (property, label, unit_suffix)
    "gap": ("avg_gap_db", "Average shortfall below the filed minimum", "dB"),
    "share": ("violation_share", "Share of measurements below the claim", ""),
}


def utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def covering_cache_file(cache_dir: Path, bounds: tuple) -> "Path | None":
    """An osm_cache file whose bbox contains ``bounds``, if one exists.

    Cache filenames embed their bbox (see mukoo_model.roads), so reuse any
    cached fetch that covers the map extent instead of re-fetching.
    """
    lon_min, lat_min, lon_max, lat_max = bounds
    for path in sorted(Path(cache_dir).glob("osm_roads_*.geojson")):
        nums = re.findall(r"-?\d+\.\d+", path.name)
        if len(nums) != 4:
            continue
        c_lon_min, c_lat_min, c_lon_max, c_lat_max = map(float, nums)
        if (c_lon_min <= lon_min and c_lat_min <= lat_min
                and c_lon_max >= lon_max and c_lat_max >= lat_max):
            return path
    return None


def check_against_report(agg_props: dict, report: dict) -> None:
    """Abort unless the aggregate's totals are the ones the audit published.

    Checked against the aggregate's own header rather than by re-deriving the
    join here: this script draws what the aggregator wrote, so the useful
    question is whether *that file* still describes the shipped audit.
    """
    checks = {
        "violations": (
            agg_props.get("n_violations"),
            report["violations"]["count"],
        ),
        "measurements inside a claimed hex": (
            agg_props.get("n_measurements_in_claimed_hexes"),
            report["points"]["inside_claimed_hex"],
        ),
    }
    bad = {k: v for k, v in checks.items() if v[0] != v[1]}
    if bad:
        for k, (got, want) in bad.items():
            print(f"MISMATCH {k}: aggregate={got} report={want}", file=sys.stderr)
        sys.exit(
            "hex aggregate disagrees with claims_report.json — re-run "
            "mukoo-claims, then aggregate_violations_by_hex.py, then this script"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", type=Path,
                        default=DEFAULT_OUTPUT_DIR / "claims_report.json",
                        help="claims_report.json from mukoo-claims")
    parser.add_argument("--hexes", type=Path,
                        default=REPO_ROOT / "verizon_claim_violations_by_hex.geojson",
                        help="the published per-hex aggregate")
    parser.add_argument("--gpkg", type=Path, default=None,
                        help="BDC GeoPackage for the claimed-coverage backdrop "
                             "(default: the one the report used; optional)")
    parser.add_argument("--cache-dir", type=Path,
                        default=DEFAULT_OUTPUT_DIR / "osm_cache")
    parser.add_argument("--shade-by", choices=sorted(SHADE_BY), default="gap",
                        help="hex fill: 'gap' = average dB below the claim "
                             "(default), 'share' = fraction of measurements below it")
    parser.add_argument("--period", default=None,
                        help="date range for the subtitle, e.g. '2026-07-18 → 2026-07-21'")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent / "coverage_map.png")
    args = parser.parse_args()

    # See the note in aggregate_violations_by_hex.py: the report lives under
    # $MUKOO_MODEL_OUTPUT_DIR (default ~/mukoo), which is not the checkout.
    if not args.report.is_file():
        sys.exit(
            f"no claims report at {args.report}\n"
            "Run `mukoo-claims --gpkg <bdc.gpkg>` first, or pass --report. "
            "Outputs go to $MUKOO_MODEL_OUTPUT_DIR (default ~/mukoo), not the "
            "repository."
        )
    if not args.hexes.is_file():
        sys.exit(
            f"no hex aggregate at {args.hexes}\n"
            "Run `python docs/aggregate_violations_by_hex.py` first, or pass --hexes."
        )
    report = json.loads(args.report.read_text())
    agg = json.loads(args.hexes.read_text())
    agg_props = agg.get("properties", {})
    environment = report["environment"]

    check_against_report(agg_props, report)

    # ---- the data layer: one polygon per contradicted hex ----
    rows = [f["properties"] for f in agg["features"]]
    geoms = [shape(f["geometry"]) for f in agg["features"]]
    viol = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
    if viol.empty:
        sys.exit("no violation hexes to draw")

    prop, ramp_label, unit = SHADE_BY[args.shade_by]
    # Both metrics are "worse when larger" once gap is read as a magnitude; the
    # ramp then runs light -> dark in the direction of the finding either way.
    viol["_v"] = viol[prop].abs() if args.shade_by == "gap" else viol[prop]

    bounds = tuple(viol.total_bounds)
    pad = 0.02

    # ---- optional context: every claimed hex in the area ----
    gpkg = args.gpkg or Path(report.get("gpkg", ""))
    claimed = None
    if gpkg and gpkg.is_file():
        claimed = gpd.read_file(
            gpkg, where=f"environmnt = {environment}",
            bbox=(bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad))
    else:
        print(f"note: no GeoPackage at {gpkg} — drawing violation hexes only",
              file=sys.stderr)

    # ---- project ----
    lon_c, lat_c = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
    utm = f"EPSG:{utm_epsg(lon_c, lat_c)}"
    viol = viol.to_crs(utm)
    if claimed is not None:
        claimed = claimed.to_crs(utm)

    road_lines = None
    cache_file = covering_cache_file(args.cache_dir, bounds)
    if cache_file is not None:
        road_lines = gpd.read_file(cache_file).set_crs("EPSG:4326").to_crs(utm).geometry

    # ---- labels ----
    if claimed is not None and "brandname" in claimed and len(claimed):
        carrier = str(claimed["brandname"].iloc[0])
        tech = TECHNOLOGY.get(int(claimed["technology"].iloc[0]), "")
    else:
        carrier, tech = "Carrier", ""
    env_label = ENVIRONMENT.get(environment, f"environment {environment}")
    claim_label = f"{carrier} claimed coverage ({env_label}{' ' + tech if tech else ''})"
    # Drawn totals come from the features, audit totals from the report. The
    # aggregate withholds hexes below its measurement floor, so the two differ
    # and the subtitle must not imply the map carries the whole audit.
    n_hex = len(viol)
    n_drawn = int(viol["n_measurements"].sum())
    floor = agg_props.get("min_measurements_per_hex", 1)
    n_viol = report["violations"]["count"]
    n_inside = report["points"]["inside_claimed_hex"]
    pct = 100.0 * n_viol / n_inside

    # ---- extent from the hexes (+900 m margin) ----
    minx, miny, maxx, maxy = viol.total_bounds
    M = 900
    minx, miny, maxx, maxy = minx - M, miny - M, maxx + M, maxy + M
    w_km, h_km = (maxx - minx) / 1000, (maxy - miny) / 1000

    AX_H = 8.0
    fig_w, fig_h = AX_H * (w_km / h_km) + 0.4, AX_H + 1.65
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    if road_lines is not None:
        road_lines.plot(ax=ax, color=ROAD, linewidth=0.7, zorder=1)
    if claimed is not None:
        claimed.plot(ax=ax, facecolor=CLAIM_FILL, edgecolor=CLAIM_EDGE,
                     linewidth=0.25, zorder=2)

    vmax = float(viol["_v"].max())
    norm = Normalize(vmin=0.0, vmax=vmax)
    # A hairline in the surface colour between fills, so adjacent hexes stay
    # countable instead of merging into one blob.
    viol.plot(ax=ax, column="_v", cmap=CMAP, norm=norm,
              edgecolor=SURFACE, linewidth=0.3, zorder=3)

    # ---- worst hex callout (the aggregate is written worst-first) ----
    worst = viol.iloc[0]
    wc = worst.geometry.centroid
    label = (f"worst hex:  {worst['worst_measured_dbm']:.0f} dBm measured\n"
             f"where ≥ {worst['claimed_minsignal_dbm']:.0f} dBm claimed")
    ax.annotate(
        label.replace("-", "−"),
        xy=(wc.x, wc.y), xytext=(wc.x + 2400, wc.y + 1000),
        fontsize=8.5, color=INK2, ha="left", va="center",
        arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8, shrinkA=2, shrinkB=4),
        bbox=dict(boxstyle="round,pad=0.35", fc=SURFACE, ec=ROAD, lw=0.8),
        zorder=6,
    )

    # ---- scale bar ----
    sb_x1, sb_y = maxx - 2900, miny + 750
    ax.plot([sb_x1, sb_x1 + 2000], [sb_y, sb_y], color=INK2, lw=1.6,
            solid_capstyle="butt", zorder=6)
    for x in (sb_x1, sb_x1 + 2000):
        ax.plot([x, x], [sb_y - 60, sb_y + 60], color=INK2, lw=1.2, zorder=6)
    ax.text(sb_x1 + 1000, sb_y + 170, "2 km", ha="center", va="bottom",
            fontsize=8.5, color=INK2, zorder=6)

    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_edgecolor(HAIRLINE)
        s.set_linewidth(0.8)

    # ---- colourbar for the sequential scale ----
    cax = fig.add_axes([0.055, 0.104, 0.24, 0.016])
    sm = plt.cm.ScalarMappable(cmap=CMAP, norm=norm)
    cb = fig.colorbar(sm, cax=cax, orientation="horizontal")
    cb.outline.set_edgecolor(HAIRLINE)
    cb.outline.set_linewidth(0.6)
    if args.shade_by == "gap":
        ticks = [0, vmax / 2, vmax]
        cb.set_ticks(ticks)
        cb.set_ticklabels([f"{t:.0f}" for t in ticks])
    else:
        cb.set_ticks([0, 0.5, 1.0])
        cb.set_ticklabels(["0", "50%", "100%"])
    cb.ax.tick_params(labelsize=8, colors=INK2, length=2, width=0.6, pad=2)
    cax.set_title(f"{ramp_label}{f' ({unit})' if unit else ''}",
                  fontsize=8.5, color=INK2, loc="left", pad=5)

    if claimed is not None:
        ax.legend(
            handles=[Patch(facecolor=CLAIM_FILL, edgecolor=CLAIM_EDGE,
                           linewidth=0.6, label=claim_label)],
            loc="lower left", fontsize=9, frameon=True, framealpha=0.94,
            edgecolor=ROAD, facecolor=SURFACE, borderpad=0.7,
            handletextpad=0.7, labelcolor=INK,
        )

    fig.text(0.045, 0.980, f"Measured signal vs. {carrier}’s FCC-claimed coverage",
             fontsize=15, fontweight="bold", color=INK, ha="left", va="top")
    period = f"{args.period} · " if args.period else ""
    floor_note = f" holding ≥{floor} readings" if floor > 1 else ""
    fig.text(0.045, 0.951,
             f"{n_drawn:,} measurements in {n_hex} of the carrier’s own "
             f"H3 cells{floor_note}",
             fontsize=9.5, color=INK2, ha="left", va="top")
    fig.text(0.045, 0.9315,
             f"{period}{pct:.1f}% of all {n_inside:,} in-claim measurements "
             "fell below the carrier’s filed minimum",
             fontsize=9.5, color=INK2, ha="left", va="top")
    fig.text(0.045, 0.033,
             f"Claims: {carrier} FCC BDC mobile filing — {Path(gpkg).name}",
             fontsize=8, color=MUTED, ha="left", va="bottom")
    fig.text(0.045, 0.013,
             "Roads © OpenStreetMap contributors · no individual measurements plotted",
             fontsize=8, color=MUTED, ha="left", va="bottom")

    fig.subplots_adjust(left=0.02, right=0.98, top=0.905, bottom=0.145)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=200, facecolor=SURFACE)
    size_kb = args.out.stat().st_size / 1024
    print(f"wrote {args.out} ({size_kb:.0f} KB)")
    print(f"{n_hex} violation hexes · shaded by {prop} "
          f"(0 … {vmax:.1f}) · {n_viol:,} violations / {n_inside:,} in-claim")


if __name__ == "__main__":
    main()

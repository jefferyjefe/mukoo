"""Render the README coverage-vs-claim map (docs/coverage_map.png).

Draws every measurement over the carrier's FCC-claimed coverage hexes, colored
by the audit verdict: below the hex's filed minimum (red), meets the claim
(green triangles), or outside any claimed hex (gray). Layers, bottom to top:
OSM roads (from the osm_cache), claimed hexes read straight from the BDC
GeoPackage, then the three point classes. Everything is projected to the local
UTM zone so distances and aspect are true, with a 2 km scale bar.

The map refuses to disagree with the shipped audit: point counts are
cross-checked against claims_report.json and the run aborts on any mismatch —
if data changed, re-run ``mukoo-claims`` first, then this script.

Usage (after ``mukoo-claims`` has produced its report + violations GeoJSON)::

    pip install -e 'model[viz]'
    python docs/make_coverage_map.py            # defaults from ~/mukoo
    python docs/make_coverage_map.py --gpkg path/to/bdc_*.gpkg

Reads DATABASE_URL like every other entry point (mukoo default otherwise).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from shapely.geometry import Point
from sqlalchemy import create_engine

from mukoo_model.config import DEFAULT_DATABASE_URL, DEFAULT_OUTPUT_DIR
from mukoo_model.roads import fetch_roads

# Palette: light chart surface + recessive chrome; red/green chosen for
# colorblind separation (protan ΔE 7.8) with shape as the second channel —
# meets-claim points are white-ringed triangles, so hue never works alone.
SURFACE = "#fcfcfb"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
ROAD = "#e1e0d9"
HEX_FILL, HEX_EDGE = "#cde2fb", "#6da7ea"
RED, GREEN, GRAY = "#e34948", "#006300", "#898781"

# FCC BDC mobile technology codes seen in the per-provider H3 files.
TECHNOLOGY = {400: "LTE", 500: "5G-NR"}
ENVIRONMENT = {0: "outdoor stationary", 1: "in-vehicle"}


def utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def covering_cache_file(cache_dir: Path, bounds: tuple) -> Path | None:
    """An osm_cache file whose bbox contains ``bounds``, if one exists.

    Cache filenames embed their bbox (see mukoo_model.roads), so reuse any
    cached fetch that covers the measurement extent instead of re-fetching.
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", type=Path,
                        default=DEFAULT_OUTPUT_DIR / "claims_report.json",
                        help="claims_report.json from mukoo-claims")
    parser.add_argument("--violations", type=Path,
                        default=DEFAULT_OUTPUT_DIR / "claims_violations.geojson",
                        help="claims_violations.geojson from mukoo-claims")
    parser.add_argument("--gpkg", type=Path, default=None,
                        help="BDC GeoPackage (default: the one the report used)")
    parser.add_argument("--cache-dir", type=Path,
                        default=DEFAULT_OUTPUT_DIR / "osm_cache")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent / "coverage_map.png")
    args = parser.parse_args()

    report = json.loads(args.report.read_text())
    gpkg = args.gpkg or Path(report["gpkg"])
    environment = report["environment"]

    # ---- data ----
    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    points = gpd.read_postgis(
        "SELECT id AS measurement_id, rsrp::float8 AS rsrp, recorded_at, geom"
        " FROM measurements", engine, geom_col="geom")

    bounds = tuple(points.total_bounds)  # lon/lat, EPSG:4326
    pad = 0.02  # deg — read hexes a little past the extent so edges stay filled
    hexes = gpd.read_file(
        gpkg, where=f"environmnt = {environment}",
        bbox=(bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad))

    viol = json.loads(args.violations.read_text())
    red_ids = {f["properties"]["measurement_id"] for f in viol["features"]}
    worst_feat = viol["features"][0]  # mukoo-claims writes worst-first

    # in-claim = the same point-in-hex join the audit does
    joined = gpd.sjoin(points, hexes, predicate="within", how="inner")
    in_claim_ids = set(joined["measurement_id"].unique())

    red = points[points["measurement_id"].isin(red_ids)]
    green = points[points["measurement_id"].isin(in_claim_ids - red_ids)]
    gray = points[~points["measurement_id"].isin(in_claim_ids)]

    # ---- refuse to draw numbers the shipped report doesn't back ----
    checks = {
        "total measurements": (len(points), report["points"]["total"]),
        "inside claimed hex": (len(in_claim_ids), report["points"]["inside_claimed_hex"]),
        "outside any claim": (len(gray), report["points"]["outside_any_claim"]),
        "violations": (len(red), report["violations"]["count"]),
    }
    bad = {k: v for k, v in checks.items() if v[0] != v[1]}
    if not red_ids <= in_claim_ids or bad:
        for k, (got, want) in bad.items():
            print(f"MISMATCH {k}: map={got} report={want}", file=sys.stderr)
        sys.exit("map disagrees with claims_report.json — re-run mukoo-claims, "
                 "then regenerate the map")

    # ---- project to the local UTM zone ----
    lon_c = (bounds[0] + bounds[2]) / 2
    lat_c = (bounds[1] + bounds[3]) / 2
    utm = f"EPSG:{utm_epsg(lon_c, lat_c)}"
    points, hexes = points.to_crs(utm), hexes.to_crs(utm)
    red, green, gray = red.to_crs(utm), green.to_crs(utm), gray.to_crs(utm)
    worst = gpd.GeoSeries(
        [Point(worst_feat["geometry"]["coordinates"])],
        crs="EPSG:4326").to_crs(utm).iloc[0]

    cache_file = covering_cache_file(args.cache_dir, bounds)
    if cache_file is not None:
        roads = gpd.read_file(cache_file).set_crs("EPSG:4326").to_crs(utm)
        road_lines = roads.geometry
    else:  # live OSM fetch (cached for next time)
        network = fetch_roads(bounds, int(utm.split(":")[1]),
                              cache_dir=args.cache_dir)
        road_lines = gpd.GeoSeries(network.lines, crs=utm)

    # ---- labels, computed from the data ----
    carrier = str(hexes["brandname"].iloc[0]) if "brandname" in hexes else "Carrier"
    tech = TECHNOLOGY.get(int(hexes["technology"].iloc[0]), "") if "technology" in hexes else ""
    env_label = ENVIRONMENT.get(environment, f"environment {environment}")
    claim_label = f"{carrier} claimed coverage ({env_label}{' ' + tech if tech else ''})"
    d0, d1 = points["recorded_at"].min().date(), points["recorded_at"].max().date()
    pct = 100 * len(red) / len(in_claim_ids)
    wp = worst_feat["properties"]

    # ---- extent (measurements + 700 m margin) ----
    minx, miny, maxx, maxy = points.total_bounds
    M = 700
    minx, miny, maxx, maxy = minx - M, miny - M, maxx + M, maxy + M
    w_km, h_km = (maxx - minx) / 1000, (maxy - miny) / 1000

    # ---- figure ----
    AX_H = 8.0
    fig_w, fig_h = AX_H * (w_km / h_km) + 0.4, AX_H + 1.55
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    road_lines.plot(ax=ax, color=ROAD, linewidth=0.7, zorder=1)
    hexes.plot(ax=ax, facecolor=HEX_FILL, edgecolor=HEX_EDGE, linewidth=0.25,
               alpha=0.55, zorder=2)
    gray.plot(ax=ax, color=GRAY, markersize=5, alpha=0.8, linewidth=0, zorder=3)
    red.plot(ax=ax, color=RED, markersize=6, alpha=0.85, linewidth=0, zorder=4)
    green.plot(ax=ax, color=GREEN, marker="^", markersize=55,
               edgecolor="white", linewidth=1.2, zorder=5)

    # worst-point callout; offsets tuned for a ~15 x 17 km extent
    ax.annotate(
        (f"worst point:  {wp['measured_rsrp_dbm']:.0f} dBm measured\n"
         f"where ≥ {wp['claimed_minsignal_dbm']:.0f} dBm claimed"
         ).replace("-", "−"),  # true minus sign for dBm values
        xy=(worst.x, worst.y), xytext=(worst.x + 2300, worst.y + 950),
        fontsize=8.5, color=INK2, ha="left", va="center",
        arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8, shrinkA=2, shrinkB=4),
        bbox=dict(boxstyle="round,pad=0.35", fc=SURFACE, ec="#e1e0d9", lw=0.8),
        zorder=6,
    )

    # scale bar, bottom-right: 2 km
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
        s.set_edgecolor("#d9d8d2")
        s.set_linewidth(0.8)

    handles = [
        Patch(facecolor=HEX_FILL, edgecolor=HEX_EDGE, linewidth=0.6,
              label=claim_label),
        Line2D([], [], marker="o", color="none", markerfacecolor=RED,
               markersize=7, label=f"Below claimed minimum — {len(red):,}"),
        Line2D([], [], marker="^", color="none", markerfacecolor=GREEN,
               markeredgecolor="white", markersize=9,
               label=f"Meets claim — {len(green):,}"),
        Line2D([], [], marker="o", color="none", markerfacecolor=GRAY,
               markersize=6, label=f"Outside any claimed hex — {len(gray):,}"),
    ]
    leg = ax.legend(handles=handles, loc="lower left", fontsize=9,
                    frameon=True, framealpha=0.94, edgecolor="#e1e0d9",
                    facecolor=SURFACE, borderpad=0.8, handletextpad=0.7)
    for t in leg.get_texts():
        t.set_color(INK)

    fig.text(0.045, 0.980, f"Measured signal vs. {carrier}’s FCC-claimed coverage",
             fontsize=15, fontweight="bold", color=INK, ha="left", va="top")
    fig.text(0.045, 0.951,
             f"{len(points):,} GPS-tagged drive measurements, {d0} → {d1}",
             fontsize=9.5, color=INK2, ha="left", va="top")
    fig.text(0.045, 0.9315,
             f"{pct:.1f}% of points inside claimed hexes measured below the "
             "carrier’s filed minimum",
             fontsize=9.5, color=INK2, ha="left", va="top")
    fig.text(0.045, 0.033,
             f"Claims: {carrier} FCC BDC mobile filing — {gpkg.name}",
             fontsize=8, color=MUTED, ha="left", va="bottom")
    fig.text(0.045, 0.013, "Roads © OpenStreetMap contributors",
             fontsize=8, color=MUTED, ha="left", va="bottom")

    fig.subplots_adjust(left=0.02, right=0.98, top=0.905, bottom=0.058)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=200, facecolor=SURFACE)
    print(f"wrote {args.out}")
    print(f"points: {len(points):,} total, {len(red):,} below claim, "
          f"{len(green):,} meet, {len(gray):,} outside")


if __name__ == "__main__":
    main()

"""Spine-coverage metric for the curve-native-spine work
(docs/curve_native_spine_v2_plan.md, Phase 0).

Builds the layout in-process and measures, for the AIRCRAFT taxi
centerlines (``layout.apt_taxi_centerlines`` minus service routes):

  * **spine-coverage %** — the fraction of total centerline length that
    has a real spine NODE on it.  A spine node is a built-shape ring
    vertex within ``SPINE_PERP_TOL_M`` (1.0 m) of the centerline — the
    SAME rule the grade solver uses (``grade_graph._spine_membership``).
    Coverage is the union of ``±R`` arc-intervals around each near node
    (``R = 0.75·SPINE_STEP_M`` ≈ 9 m, so nodes at the 12 m spine step
    join into a continuous chain, but a stretch of centerline running
    through pavement with NO node — the curved-interior-piece failure —
    reads as a gap).

  * **dangling dead-ends** — centerline endpoints that terminate INSIDE
    the pavement footprint (``> _BOUNDARY_TOL_M`` from the
    ``source_pavement_union`` boundary) and are a FREE end of the taxi
    graph (no other centerline within ``_JOIN_TOL_M``).  These are the
    termini the Phase-3 keyhole tip-cap must close; a boundary-reaching
    or junction-joined end is not counted.

Run with ``O4_RECOGNIZED_CENTERLINES=1`` to measure coverage of the
RECOGNIZED curved centerlines (the input to the new model); without it
you measure the straight apt.dat routes.

Usage:
    O4_RECOGNIZED_CENTERLINES=1 python3 tools/spine_coverage.py SPJC
    python3 tools/spine_coverage.py SPJC --detail        # per-line rows
    python3 tools/spine_coverage.py SPJC --csv           # one summary row
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shapely.geometry import LineString, Point  # noqa: E402

from auto_patch.config import SPINE_STEP_M  # noqa: E402
from auto_patch.grade_graph import SPINE_PERP_TOL_M  # noqa: E402
from auto_patch.pipeline import build_airport_pavement  # noqa: E402

# Half-width of the covered arc-interval a single node paints, as a
# fraction of the spine step, so 12 m-spaced nodes overlap into a chain.
_COVER_R_FRAC = 0.75
# An endpoint farther than this from the pavement boundary is INTERIOR.
_BOUNDARY_TOL_M = 3.0
# Another centerline within this of an endpoint means it is JOINED, not
# a free dead-end.
_JOIN_TOL_M = 3.0


def _aircraft_centerlines(layout):
    """Unique aircraft taxi centerlines (continuous ``chained_line``),
    excluding service routes.  De-duped by route identity so bend-split
    pieces of one route are measured once."""
    out = []
    seen = set()
    for item in getattr(layout, "apt_taxi_centerlines", []) or []:
        if getattr(item, "is_service", False):
            continue
        ln = getattr(item, "chained_line", None) or getattr(item, "line", None)
        if ln is None or ln.is_empty or ln.length < 1.0:
            continue
        key = id(ln)
        if key in seen:
            continue
        seen.add(key)
        out.append(ln)
    return out


def _shape_vertices(layout):
    """Every ring vertex of every built shape (exterior + holes)."""
    verts = []
    for s in layout.shapes:
        poly = getattr(s, "polygon", None)
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            continue
        verts.extend(poly.exterior.coords)
        for hole in poly.interiors:
            verts.extend(hole.coords)
    return verts


def _merge_intervals(intervals):
    """Total length of the union of ``[a, b]`` intervals."""
    if not intervals:
        return 0.0
    intervals.sort()
    total = 0.0
    ca, cb = intervals[0]
    for a, b in intervals[1:]:
        if a <= cb:
            cb = max(cb, b)
        else:
            total += cb - ca
            ca, cb = a, b
    total += cb - ca
    return total


def _line_coverage(ln: LineString, verts, tol, cover_r):
    """Covered length of ``ln`` given nearby shape vertices."""
    L = ln.length
    intervals = []
    for (x, y) in verts:
        p = Point(x, y)
        if ln.distance(p) > tol:
            continue
        a = ln.project(p)
        intervals.append((max(0.0, a - cover_r), min(L, a + cover_r)))
    return _merge_intervals(intervals), L


def _dangling_dead_ends(lines, pav_union):
    """Count centerline endpoints that terminate inside the pavement and
    are a free end of the taxi graph."""
    n = 0
    ends = []
    for i, ln in enumerate(lines):
        cs = list(ln.coords)
        ends.append((i, Point(cs[0])))
        ends.append((i, Point(cs[-1])))
    for i, pt in ends:
        if pav_union is not None and not pav_union.is_empty:
            if pav_union.boundary.distance(pt) <= _BOUNDARY_TOL_M:
                continue  # reaches a pavement edge — not a dead-end
            if not pav_union.contains(pt):
                continue  # off pavement entirely — not our concern
        else:
            continue
        joined = False
        for j, other in enumerate(lines):
            if j == i:
                continue
            if other.distance(pt) <= _JOIN_TOL_M:
                joined = True
                break
        if not joined:
            n += 1
    return n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--xplane", default="/Users/noah/X-Plane 12")
    ap.add_argument("--detail", action="store_true",
                    help="print a per-centerline coverage row")
    ap.add_argument("--csv", action="store_true",
                    help="print one machine-readable summary row")
    args = ap.parse_args(argv)

    layout = build_airport_pavement(
        args.icao, args.xplane, compute_elevations=True)

    lines = _aircraft_centerlines(layout)
    verts = _shape_vertices(layout)
    pav_union = getattr(layout, "source_pavement_union", None)
    cover_r = _COVER_R_FRAC * SPINE_STEP_M

    total_len = 0.0
    total_cov = 0.0
    rows = []
    for i, ln in enumerate(lines):
        cov, L = _line_coverage(ln, verts, SPINE_PERP_TOL_M, cover_r)
        total_len += L
        total_cov += cov
        rows.append((i, L, cov))

    dead_ends = _dangling_dead_ends(lines, pav_union)
    pct = (100.0 * total_cov / total_len) if total_len > 0 else 0.0

    if args.csv:
        print(f"{args.icao},{len(lines)},{total_len:.1f},{pct:.1f},{dead_ends}")
        return 0

    if args.detail:
        print(f"# {args.icao} per-centerline coverage "
              f"(tol={SPINE_PERP_TOL_M} m, R={cover_r:.1f} m)")
        print(f"# {'idx':>4} {'len_m':>8} {'cov_m':>8} {'cov%':>6}")
        for i, L, cov in rows:
            p = (100.0 * cov / L) if L > 0 else 0.0
            print(f"  {i:4d} {L:8.1f} {cov:8.1f} {p:6.1f}")

    print(f"{args.icao}: {len(lines)} aircraft centerline(s), "
          f"{total_len:.0f} m total; spine-coverage {pct:.1f}%; "
          f"dangling dead-ends {dead_ends}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

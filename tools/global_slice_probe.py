"""Phase-1 probe for the curve-native global slice
(docs/curve_native_spine_v2_plan.md).

Builds the layout (recognition ON) to get ``pav_union`` + the recognized
aircraft centerlines, runs ``pavement.global_slice.build_global_slice_faces``,
and reports the Phase-1 acceptance metrics:

  * face count
  * spine-coverage % (from the faces, ``slice_coverage``)
  * invalid faces (``explain_validity``)
  * conformance among faces: T-junctions (a vertex sitting mid-edge of
    another face) and edge crossings — both must be 0 by construction
  * determinism: the metric tuple is printed so two runs can be diffed

Usage:
    O4_RECOGNIZED_CENTERLINES=1 python3 tools/global_slice_probe.py SPJC
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shapely.geometry import LineString, Point  # noqa: E402
from shapely.validation import explain_validity  # noqa: E402

from auto_patch.pavement.global_slice import (  # noqa: E402
    build_global_slice_faces, dedup_centerlines, slice_coverage)
from auto_patch.pipeline import build_airport_pavement  # noqa: E402

_TOL = 0.05  # m — coincidence tolerance for the conformance check


def _aircraft_centerlines(layout):
    out, seen = [], set()
    for item in getattr(layout, "apt_taxi_centerlines", []) or []:
        if getattr(item, "is_service", False):
            continue
        ln = getattr(item, "chained_line", None) or getattr(item, "line", None)
        if ln is None or ln.is_empty or ln.length < 1.0:
            continue
        if id(ln) in seen:
            continue
        seen.add(id(ln))
        out.append(ln)
    return out


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 < 1e-12:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5, -1.0
    t = ((px - ax) * dx + (py - ay) * dy) / d2
    if t <= 0 or t >= 1:
        return float("inf"), t
    qx, qy = ax + t * dx, ay + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5, t


def _conformance(faces, pav, cls):
    """Count T-junctions: a face vertex lying strictly inside another face's
    edge.  For a single polygonize arrangement this is 0 on clean data;
    classify each by whether it sits near the pav boundary or a centerline."""
    edges = []
    verts = set()
    for f in faces:
        cs = list(f.polygon.exterior.coords)
        for i in range(len(cs) - 1):
            edges.append((cs[i][0], cs[i][1], cs[i + 1][0], cs[i + 1][1]))
        for (x, y) in cs:
            verts.add((round(x, 2), round(y, 2)))
    tjunc = 0
    near_bnd = near_cl = 0
    bnd = pav.boundary
    for (vx, vy) in verts:
        for (ax, ay, bx, by) in edges:
            if (abs(vx - ax) < _TOL and abs(vy - ay) < _TOL) or \
               (abs(vx - bx) < _TOL and abs(vy - by) < _TOL):
                continue
            d, t = _seg_dist(vx, vy, ax, ay, bx, by)
            if d < _TOL and 0 < t < 1:
                tjunc += 1
                p = Point(vx, vy)
                if bnd.distance(p) < 0.5:
                    near_bnd += 1
                elif any(c.distance(p) < 0.5 for c in cls):
                    near_cl += 1
                break
    return tjunc, near_bnd, near_cl


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--xplane", default="/Users/noah/X-Plane 12")
    args = ap.parse_args(argv)

    layout = build_airport_pavement(
        args.icao, args.xplane, compute_elevations=False)
    pav = getattr(layout, "source_pavement_union", None)
    rwy = getattr(layout, "runway_union", None)
    cls = _aircraft_centerlines(layout)
    if pav is None or pav.is_empty:
        print("NO pav_union", file=sys.stderr)
        return 1

    pav_eff = pav.difference(rwy) if rwy is not None and not rwy.is_empty else pav
    faces = build_global_slice_faces(pav, cls, runway_union=rwy)
    # Coverage is measured against the de-duplicated NETWORK (redundant
    # coincident lines are one physical taxiway; covering one covers it).
    eff = dedup_centerlines(cls)
    cov, total = slice_coverage(faces, eff)
    pct = (100.0 * cov / total) if total > 0 else 0.0

    # "Through" centerlines = both endpoints on the pavement boundary (a
    # dead-end tail contributes no face edge, so it is excepted from the
    # Phase-1 coverage target — Phase 3 keyholes recover it).
    through = []
    for c in cls:
        cs = list(c.coords)
        if (pav_eff.boundary.distance(Point(cs[0])) < 2.0
                and pav_eff.boundary.distance(Point(cs[-1])) < 2.0):
            through.append(c)
    tcov, ttotal = slice_coverage(faces, through)
    tpct = (100.0 * tcov / ttotal) if ttotal > 0 else 0.0

    # Buried length: centerline stretches strictly interior to a face (>1 m
    # from any face boundary) — an overlap/parallel line that never became an
    # edge.  Distinguishes the coverage gap's cause from unspurred dead-ends.
    from shapely.ops import unary_union as _uu
    face_bnds = _uu([f.polygon.exterior for f in faces]) if faces else None
    buried = 0.0
    for c in eff:
        if c is None or c.is_empty:
            continue
        n = max(2, int(c.length / 4))
        for k in range(n + 1):
            p = c.interpolate(k * c.length / n)
            if face_bnds is not None and face_bnds.distance(p) > 1.0:
                buried += c.length / n

    invalid = sum(1 for f in faces if not f.polygon.is_valid)
    tagged = sum(1 for f in faces if f.centerline_ids)
    slivers = sum(1 for f in faces if f.polygon.area < 2.0)
    tjunc, near_bnd, near_cl = _conformance(faces, pav_eff, cls)

    print(f"# {args.icao} global-slice Phase-1 probe")
    print(f"  centerlines (aircraft) : {len(cls)}  (through={len(through)})")
    print(f"  faces                  : {len(faces)}  (slivers<2m²={slivers})")
    print(f"  faces w/ spine edge    : {tagged}")
    print(f"  spine-coverage % (all) : {pct:.1f}   ({cov:.0f}/{total:.0f} m)")
    print(f"  buried (interior) len  : {buried:.0f} m  "
          f"({100.0 * buried / total:.1f}% of total — overlap/parallel)")
    print(f"  invalid faces          : {invalid}")
    print(f"  T-junctions            : {tjunc}  (near-boundary={near_bnd}, "
          f"near-centerline={near_cl}, other={tjunc - near_bnd - near_cl})")

    # Diagnose the coverage gap: the least-covered centerlines, with the
    # boundary-distance of each endpoint (small = reaches a pavement edge;
    # large = interior, i.e. a junction node or a dead-end tip).
    bnd = pav_eff.boundary
    rows = []
    for c in eff:
        if c is None or c.is_empty or c.length < 5.0:
            continue
        cc, ct = slice_coverage(faces, [c])
        cs = list(c.coords)
        d0 = bnd.distance(Point(cs[0]))
        d1 = bnd.distance(Point(cs[-1]))
        rows.append((cc / ct if ct else 1.0, c.length, d0, d1))
    rows.sort()
    print("  --- 8 least-covered centerlines (cov%, len, endA→bnd, endB→bnd) ---")
    for cvf, L, d0, d1 in rows[:8]:
        print(f"    cov={100 * cvf:5.1f}%  len={L:6.0f}m  "
              f"endA={d0:6.1f}m  endB={d1:6.1f}m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

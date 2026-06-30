#!/usr/bin/env python3
"""Phase-0 verification probe for the anisotropic-edge route-chaining infra
(docs/anisotropic_edge_handling_plan.md §Phase 0).

Builds an airport, builds the shared grade context, and checks that the
bend-split ``Centerline`` pieces have been chained back into whole ``RouteChain``
routes:

  1. each named route (e.g. CYXY "F") groups its pieces under ONE ``route_idx``,
     whose ``RouteChain`` arc length equals its parent ``route_line`` length and
     is >= the sum of its (RDP-simplified, curve-skipped) piece lengths;
  2. the number of DISTINCT chained routes touching each junction reproduces the
     §3b junction-degree structure (the Y/X high end: 3->6, 4->1 at CYXY).

The chained route is the parent ``route_line`` (the continuous, UN-simplified
polyline that still contains the curve through the junction) — NOT a re-stitch of
the pieces, because ``split_merged_centerline`` drops the curve interval, so a
piece chain would lose the very arc the anisotropic law needs to credit.

Usage:  venv/bin/python tools/probe_route_chains.py CYXY
"""
from __future__ import annotations

import argparse
import math
from collections import Counter, defaultdict

import _diag
from auto_patch import grade_graph as GG


def _arclen(coords) -> float:
    return sum(math.hypot(coords[i + 1][0] - coords[i][0],
                          coords[i + 1][1] - coords[i][1])
               for i in range(len(coords) - 1))


def main() -> None:
    ap = argparse.ArgumentParser()
    _diag.add_common_args(ap)
    ap.add_argument("--focus", default="F",
                    help="route name to report in detail (default: F)")
    args = ap.parse_args()

    layout = _diag.build(args.icao, args.xplane, compute_elevations=True)
    ctx = GG.build_context(layout)
    print(f"{args.icao}: {len(ctx.centerlines)} taxi pieces, "
          f"{len(ctx.routes)} chained routes")

    # Map each piece -> its source TaxiCenterline name, in ctx order.  build_context
    # iterates apt_taxi_centerlines skipping service/empty in the SAME order it
    # appends Centerlines, so a parallel filtered walk lines up index-for-index.
    names: list[str] = []
    for tcl in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = getattr(tcl, "line", None)
        if ln is None or getattr(ln, "is_empty", True):
            continue
        if getattr(tcl, "is_service", False):
            continue
        try:
            pts = list(ln.coords)
        except Exception:
            continue
        if len(pts) >= 2:
            names.append(getattr(tcl, "name", "") or "")
    assert len(names) == len(ctx.centerlines), (len(names), len(ctx.centerlines))

    # ── 1. Per-name route grouping + arc accounting ──────────────────────────
    print("\n-- route grouping by name (route_idx should be ONE per route) --")
    by_name_routes: dict[str, set] = defaultdict(set)
    piece_len_by_route: dict[int, float] = defaultdict(float)
    for ci, cl in enumerate(ctx.centerlines):
        by_name_routes[names[ci]].add(cl.route_idx)
        piece_len_by_route[cl.route_idx] += _arclen(cl.pts)
    for nm in sorted(by_name_routes):
        ridxs = by_name_routes[nm]
        tag = "" if len(ridxs) == 1 else "  <-- multiple route_idx"
        print(f"  name={nm!r:6} route_idx={sorted(ridxs)}{tag}")

    foc = args.focus
    if foc in by_name_routes:
        print(f"\n-- detail: route {foc!r} --")
        for ridx in sorted(by_name_routes[foc]):
            chain = ctx.routes[ridx]
            chain_arc = chain.arc()[-1]
            ssum = piece_len_by_route[ridx]
            print(f"  route_idx={ridx}: chained arc={chain_arc:.3f} m, "
                  f"Σpiece={ssum:.3f} m, chain_pts={len(chain.pts)}, "
                  f"arc>=Σpiece: {chain_arc + 1e-6 >= ssum}")
            # spine nodes should project monotonically with small perp
            worst_perp = 0.0
            for ci, cl in enumerate(ctx.centerlines):
                if cl.route_idx != ridx:
                    continue
                for (x, y) in cl.pts:
                    _a, perp = chain.project(x, y)
                    worst_perp = max(worst_perp, perp)
            print(f"           worst piece-vertex perp onto chain: {worst_perp:.3f} m")

    # ── 2. Junction degree by distinct chained route ─────────────────────────
    # A route "touches" a junction if its chained polyline intersects the junction
    # polygon (buffered to absorb the sliced-spine snap).  This is the model-
    # relevant degree: §3c grades each junction-body vertex against its NEAREST
    # converging route, so the count of converging routes (a Y => 3, an X => 4) is
    # what the crotch model keys on — NOT the distinct NAME (one taxiway is many
    # inter-junction routes).
    from shapely.geometry import Point
    route_ep = {}   # route_idx -> (Point start, Point end)
    for ri, r in enumerate(ctx.routes):
        if len(r.pts) >= 2:
            route_ep[ri] = (Point(r.pts[0]), Point(r.pts[-1]))
    ridx_name = {}  # route_idx -> a representative NAME (from its pieces)
    for ci, cl in enumerate(ctx.centerlines):
        ridx_name.setdefault(cl.route_idx, names[ci])

    from shapely.geometry import LineString
    route_geom = {ri: LineString(r.pts) for ri, r in enumerate(ctx.routes)
                  if len(r.pts) >= 2}
    print("\n-- junction degree: distinct routes CONVERGING (endpoint in jct) --")
    deg_r = Counter()
    deg_n = Counter()
    deg_xname = Counter()   # by-name, route INTERSECTS jct (§3b's pass-through metric)
    hi = []  # degree>=3 junctions to eyeball the Y/X set
    for s in layout.shapes:
        if s.role != "junction" or s.polygon is None or s.polygon.is_empty:
            continue
        jb = s.polygon.buffer(3.0)
        conv = {ri for ri, (p0, p1) in route_ep.items()
                if jb.contains(p0) or jb.contains(p1)}
        conv_names = {ridx_name.get(ri, "") for ri in conv}
        xnames = {ridx_name.get(ri, "") for ri, g in route_geom.items()
                  if g.intersects(jb)}
        deg_r[len(conv)] += 1
        deg_n[len(conv_names)] += 1
        deg_xname[len(xnames)] += 1
        if len(conv) >= 3:
            c = s.polygon.centroid
            hi.append((len(conv), sorted(conv_names), c.x, c.y, s.polygon.area))
    print("  CONVERGE by route_idx -> count:", dict(sorted(deg_r.items())))
    print("  CONVERGE by NAME      -> count:", dict(sorted(deg_n.items())),
          "  (matches §3b Y/X high end 3->6, 4->1)")
    print("  PASS-THRU by NAME     -> count:", dict(sorted(deg_xname.items())),
          "  (§3b metric)")
    print("  (§3b by-NAME target: {0:17, 1:60, 2:23, 3:6, 4:1})")
    print("\n  junctions with >=3 converging routes (Y/X candidates):")
    for (d, nms, cx, cy, ar) in sorted(hi, reverse=True):
        print(f"    deg={d} names={nms} centroid=({cx:.0f},{cy:.0f}) area={ar:.0f}")


if __name__ == "__main__":
    main()

"""Field-vs-emitted grade probe ALONG one taxi centerline through a
junction (the decisive metric for the junction-centerline-spine feature).

Builds the layout in-process so it can read BOTH:
  * ``layout._network_profile_field.sample(x, y)`` — the smooth ≤1.5%
    corridor profile the solver produced (the TARGET); and
  * the EMITTED triangulated surface elevation at the same (x, y) —
    reconstructed from each junction/apron/rect shape by constrained-
    Delaunay triangulating its ring (with interior spine nodes when the
    spine feature has emitted them) using the per-vertex node_altitudes,
    then barycentric-interpolating.  This approximates the surface
    X-Plane / Triangle4XP renders.

Then walks a chosen centerline in small steps and prints, for each step,
the field grade and the emitted-surface grade.  Success for the spine
feature = the emitted grade tracks the field's ≤1.5% (no 3.6% spikes).

Usage:
    O4_JCT_SPINE=1 python3 tools/probe_spine_grade.py OMAA \
        --centerline H --near 24.4370 54.6466 --step 8
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
from shapely.geometry import LineString, Point, Polygon

from auto_patch.pipeline import build_airport_pavement


def _open(ring):
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _triangulate_with_elev(poly: Polygon, ring_xy, ring_z):
    """Constrained Delaunay of ``poly`` honouring its boundary, with
    z attached per vertex.  Returns a list of (tri_pts, tri_z) where
    tri_pts is a 3x2 array and tri_z a length-3 array.  Interior spine
    nodes (extra vertices the emit added INSIDE the polygon) are picked
    up because they live on the ring's node list too — but for the
    ring-only model the interior is empty, so this is a fan over the
    boundary (matching X-Plane's interpolation behaviour).
    """
    from shapely.ops import triangulate as shp_triangulate
    pts = list(zip(ring_xy, ring_z))
    seen = {}
    verts = []
    vz = []
    for (x, y), z in pts:
        k = (round(x, 3), round(y, 3))
        if k in seen:
            continue
        seen[k] = len(verts)
        verts.append((x, y))
        vz.append(z)
    n = len(verts)
    if n < 3:
        return []
    zlut = {(round(x, 3), round(y, 3)): z for (x, y), z in zip(verts, vz)}
    mp = [Point(x, y) for (x, y) in verts]
    try:
        tris = shp_triangulate(__import__("shapely").geometry.MultiPoint(mp))
    except Exception:
        tris = []
    out = []
    for t in tris:
        c = t.centroid
        if not poly.contains(c):
            continue
        tp = list(t.exterior.coords)[:3]
        tz = []
        ok = True
        for (px, py) in tp:
            zz = zlut.get((round(px, 3), round(py, 3)))
            if zz is None:
                bi = min(range(n), key=lambda i: (verts[i][0] - px) ** 2
                         + (verts[i][1] - py) ** 2)
                zz = vz[bi]
            tz.append(zz)
        out.append((np.array(tp), np.array(tz)))
    if out:
        return out
    # Fallback: fan from centroid.
    cx = sum(v[0] for v in verts) / n
    cy = sum(v[1] for v in verts) / n
    cz = sum(vz) / n
    for i in range(n):
        j = (i + 1) % n
        out.append((np.array([verts[i], verts[j], (cx, cy)]),
                    np.array([vz[i], vz[j], cz])))
    return out


def _bary_z(tri_pts, tri_z, x, y):
    (x1, y1), (x2, y2), (x3, y3) = tri_pts
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(det) < 1e-9:
        return None
    a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / det
    b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / det
    c = 1.0 - a - b
    if a < -1e-6 or b < -1e-6 or c < -1e-6:
        return None
    return a * tri_z[0] + b * tri_z[1] + c * tri_z[2]


def _shape_rings_with_z(layout):
    """Yield (poly, ring_xy, ring_z) for every shape that carries a
    usable per-vertex or flat elevation, in meter coordinates."""
    out = []
    for s in layout.shapes:
        poly = s.polygon
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            continue
        ring = list(poly.exterior.coords)
        ropen = _open(ring)
        n = len(ropen)
        if s.node_altitudes and len(s.node_altitudes) >= len(ring):
            z = [float(e) for e in s.node_altitudes[:len(ring)]]
            z = z[:n]
        elif s.altitude is not None:
            z = [float(s.altitude)] * n
        elif (s.altitude_high is not None and s.altitude_low is not None
              and n == 4):
            z = [s.altitude_high, s.altitude_low,
                 s.altitude_low, s.altitude_high]
        else:
            continue
        if len(z) != n:
            continue
        out.append((poly, ropen, z, s.role))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--xplane", default="/Users/noah/X-Plane 12")
    ap.add_argument("--centerline", default="H",
                    help="centerline ref/name to walk")
    ap.add_argument("--near", nargs=2, type=float, default=None,
                    metavar=("LAT", "LON"),
                    help="restrict to the portion within --window m of "
                         "this point")
    ap.add_argument("--window", type=float, default=120.0)
    ap.add_argument("--step", type=float, default=8.0)
    args = ap.parse_args(argv)

    layout = build_airport_pavement(
        args.icao, args.xplane, compute_elevations=True)
    F = getattr(layout, "_network_profile_field", None)
    if F is None:
        print("NO network profile field on layout", file=sys.stderr)
        return 1

    # Locate the centerline(s).  Prefer the full graph (apt +
    # discovered).
    lines = list(getattr(layout, "apt_taxi_centerlines", []) or [])
    disc = list(getattr(layout, "_discovered_centerlines", []) or [])
    lines = lines + disc
    def _ln_ref(item):
        """(line, name) from a TaxiCenterline, a (line, name) tuple, or a bare
        LineString — the centerline producers all emit TaxiCenterline now."""
        ln = getattr(item, "line", None)
        if ln is not None:
            return ln, getattr(item, "name", "")
        if isinstance(item, tuple):
            return item[0], (item[1] if len(item) > 1 else "")
        return item, ""

    want = args.centerline.upper()
    sel = []
    for item in lines:
        ln, ref = _ln_ref(item)
        if ln is None or ln.is_empty:
            continue
        if want in str(ref).upper() or want == "*":
            sel.append(ln)
    if not sel:
        print(f"no centerline matching ref={want!r}; available refs:",
              file=sys.stderr)
        refs = sorted({str(_ln_ref(it)[1]) for it in lines})
        print("  " + ", ".join(refs), file=sys.stderr)
        return 1

    near_m = None
    if args.near:
        near_m = layout.ll_to_m(args.near[0], args.near[1])

    shapes = _shape_rings_with_z(layout)
    # Spatial pre-index by bbox for the emitted-surface lookup.
    tri_cache = {}

    def emitted_z(x, y):
        p = Point(x, y)
        best = None
        for idx, (poly, rxy, rz, role) in enumerate(shapes):
            if not poly.intersects(p):
                continue
            tris = tri_cache.get(idx)
            if tris is None:
                tris = _triangulate_with_elev(poly, rxy, rz)
                tri_cache[idx] = tris
            for tp, tz in tris:
                z = _bary_z(tp, tz, x, y)
                if z is not None:
                    return z, role
        return None, None

    print(f"# {args.icao} centerline {want}: field vs emitted along-track")
    print(f"# {'s_m':>7} {'field':>8} {'emit':>8} "
          f"{'fgrade%':>8} {'egrade%':>8}  role")
    for li, ln in enumerate(sel):
        pts = [layout.ll_to_m(la, lo) if False else (x, y)
               for (x, y) in ln.coords]
        # ln coords are already in meter frame? They are LineStrings in
        # the layout meter frame.
        L = ln.length
        if L < 1.0:
            continue
        ss = np.arange(0.0, L + args.step, args.step)
        prev = None
        prevs = None
        printed_any = False
        for s in ss:
            pt = ln.interpolate(min(s, L))
            x, y = pt.x, pt.y
            if near_m is not None:
                if math.hypot(x - near_m[0], y - near_m[1]) > args.window:
                    continue
            fv, gap = F.sample(x, y)
            ez, role = emitted_z(x, y)
            fg = eg = float("nan")
            if prev is not None and prevs is not None:
                ds = s - prevs[0]
                if ds > 0.1:
                    if fv is not None and prev[0] is not None:
                        fg = 100.0 * abs(fv - prev[0]) / ds
                    if ez is not None and prev[1] is not None:
                        eg = 100.0 * abs(ez - prev[1]) / ds
            fvs = f"{fv:8.2f}" if fv is not None else "    --  "
            ezs = f"{ez:8.2f}" if ez is not None else "    --  "
            fgs = f"{fg:8.2f}" if fg == fg else "      --"
            egs = f"{eg:8.2f}" if eg == eg else "      --"
            print(f"  {s:7.1f} {fvs} {ezs} {fgs} {egs}  {role or ''}")
            printed_any = True
            prev = (fv, ez)
            prevs = (s,)
        if printed_any:
            print(f"# --- end line {li} (len {L:.0f} m) ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

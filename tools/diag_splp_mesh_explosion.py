"""Diagnose the SPLP -13/-77 mesh-explosion: find geometry that forces
Triangle4XP to over-subdivide (target ~250k tris, observed >2M).

Pathologies it looks for in the FINAL emitted layout:
  1. Vertical walls   — multiple vertices in one canonical XY bucket whose
     altitudes disagree by > VERTEX_ALT_MERGE_TOL_M (a cliff edge). Each
     wall edge forces near-vertical triangle refinement.
  2. Tiny edges       — polygon edges shorter than TINY_EDGE_M.
  3. Crossing slivers — pairs of shapes whose exteriors cross (not just
     touch), i.e. overlapping constraint edges.
All three are reported with locations and contributing shape roles, so we
can confirm/deny "boundary slice shapes" as the culprit.

Run: venv/bin/python tools/diag_splp_mesh_explosion.py
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import xplane_root  # noqa: E402
from O4_DEM_Utils import DEM as O4DEM  # noqa: E402
from auto_patch.pipeline import build_airport_pavement  # noqa: E402
from auto_patch.layout import (  # noqa: E402
    VERTEX_ALT_MERGE_TOL_M, corner_alts_from_high_low)
from auto_patch.canonical_points import CanonicalPointRegistry  # noqa: E402

TILE_LAT, TILE_LON = -13, -77
TINY_EDGE_M = 0.75


def shape_vertices_with_alts(s):
    """Yield (x, y, alt_or_None) for each exterior vertex (open ring),
    matching to_osm's per-corner altitude derivation."""
    poly = s.polygon
    if poly is None or poly.is_empty:
        return
    coords = list(poly.exterior.coords)
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    n = len(coords)
    if n < 3:
        return
    if s.node_altitudes:
        elevs = list(s.node_altitudes)
        if len(elevs) > n:
            elevs = elevs[:n]
        for k, (x, y) in enumerate(coords):
            yield x, y, (elevs[k] if k < len(elevs) else None)
    elif s.altitude is not None:
        for (x, y) in coords:
            yield x, y, float(s.altitude)
    elif (s.altitude_high is not None and s.altitude_low is not None
          and n == 4):
        ca = corner_alts_from_high_low(
            float(s.altitude_high), float(s.altitude_low))
        for k, (x, y) in enumerate(coords):
            yield x, y, ca[k]
    else:
        for (x, y) in coords:
            yield x, y, None


def main():
    print(f"Building SPLP tile {TILE_LAT:+d}{TILE_LON:+d} "
          f"(compute_elevations=True)…")
    dem = O4DEM(TILE_LAT, TILE_LON, fill_nodata="to zero")
    layout = build_airport_pavement(
        "SPLP", xplane_root(), compute_elevations=True,
        tile_dem=dem, current_tile_lat=TILE_LAT,
        current_tile_lon=TILE_LON)

    shapes = [s for s in layout.shapes
              if s.polygon is not None and not s.polygon.is_empty]
    print(f"\nShapes: {len(shapes)}")
    by_role = Counter(s.role for s in shapes)
    total_verts = 0
    verts_by_role = Counter()
    for s in shapes:
        nv = sum(1 for _ in shape_vertices_with_alts(s))
        total_verts += nv
        verts_by_role[s.role] += nv
    for r in sorted(by_role):
        print(f"  {r:<24} shapes={by_role[r]:<4} verts={verts_by_role[r]}")
    print(f"  TOTAL vertices (open rings): {total_verts}")

    # ── 1. Vertical walls ────────────────────────────────────────
    reg = CanonicalPointRegistry(tol_m=0.5)
    # canonical key -> list of (alt, shape_idx)
    bucket = defaultdict(list)
    for idx, s in enumerate(shapes):
        for (x, y, alt) in shape_vertices_with_alts(s):
            key = reg.get_or_add(float(x), float(y))
            bucket[key].append((alt, idx))

    walls = []  # (key, max_dalt, roles)
    for key, entries in bucket.items():
        alts = [a for (a, _i) in entries if a is not None]
        if len(alts) < 2:
            continue
        dalt = max(alts) - min(alts)
        if dalt > VERTEX_ALT_MERGE_TOL_M:
            roles = Counter(shapes[i].role for (_a, i) in entries)
            walls.append((key, dalt, roles, min(alts), max(alts)))

    walls.sort(key=lambda w: -w[1])
    print(f"\n=== VERTICAL WALLS (Δalt > {VERTEX_ALT_MERGE_TOL_M} m at a "
          f"shared XY): {len(walls)} ===")
    role_pair_counter = Counter()
    for (_key, _dalt, roles, _lo, _hi) in walls:
        role_pair_counter[tuple(sorted(roles))] += 1
    print("  by contributing-role-set:")
    for rp, c in role_pair_counter.most_common():
        print(f"    {c:>4}  {rp}")
    print("  worst 15 (by Δalt):")
    for (key, dalt, roles, lo, hi) in walls[:15]:
        print(f"    Δ{dalt:6.2f} m  at ({key[0]:8.1f},{key[1]:8.1f})  "
              f"{lo:.1f}->{hi:.1f}  roles={dict(roles)}")

    # ── 2. Tiny edges ────────────────────────────────────────────
    tiny_by_role = Counter()
    tiny_examples = []
    import math
    for s in shapes:
        coords = list(s.polygon.exterior.coords)
        for i in range(len(coords) - 1):
            (x0, y0), (x1, y1) = coords[i], coords[i + 1]
            d = math.hypot(x1 - x0, y1 - y0)
            if 0 < d < TINY_EDGE_M:
                tiny_by_role[s.role] += 1
                if len(tiny_examples) < 12:
                    tiny_examples.append((d, s.role, x0, y0))
    print(f"\n=== TINY EDGES (< {TINY_EDGE_M} m): "
          f"{sum(tiny_by_role.values())} ===")
    for r, c in tiny_by_role.most_common():
        print(f"    {c:>4}  {r}")
    for (d, r, x, y) in tiny_examples:
        print(f"    {d:5.3f} m  {r:<22} near ({x:8.1f},{y:8.1f})")

    # ── 3. Crossing slivers ──────────────────────────────────────
    print("\n=== CROSSING SHAPE PAIRS (exteriors cross, not just "
          "touch) ===")
    from shapely.strtree import STRtree
    polys = [s.polygon for s in shapes]
    tree = STRtree(polys)
    cross_role_pairs = Counter()
    crossings = 0
    seen_pairs = set()
    for i, s in enumerate(shapes):
        for j in tree.query(s.polygon):
            j = int(j)
            if j <= i:
                continue
            if (i, j) in seen_pairs:
                continue
            seen_pairs.add((i, j))
            a, b = polys[i], polys[j]
            try:
                if a.exterior.crosses(b.exterior):
                    crossings += 1
                    cross_role_pairs[tuple(sorted(
                        (s.role, shapes[j].role)))] += 1
            except Exception:
                pass
    print(f"  total crossing pairs: {crossings}")
    for rp, c in cross_role_pairs.most_common():
        print(f"    {c:>4}  {rp}")

    # ── 4. T-junctions (hanging nodes) ───────────────────────────
    # A vertex of shape A lies ON an edge of shape B (within
    # PERP_TOL perpendicular, strictly between B's edge endpoints)
    # but is NOT itself near a vertex of B. Forces a mesh crack;
    # if A's alt there disagrees with B's interpolated alt, a wall.
    print("\n=== T-JUNCTIONS (hanging node on another shape's edge) ===")
    PERP_TOL = 0.30
    VTX_TOL = 0.6
    # Pre-extract per-shape (x,y,alt) vertex lists.
    sv = [list(shape_vertices_with_alts(s)) for s in shapes]
    tjunc_role_pairs = Counter()
    tjunc_wall = 0
    tjunc = 0
    tj_examples = []
    for i, s in enumerate(shapes):
        bi = polys[i].bounds
        for j in tree.query(polys[i].buffer(PERP_TOL)):
            j = int(j)
            if j == i:
                continue
            cb = list(polys[j].exterior.coords)
            for (vx, vy, valt) in sv[i]:
                # skip if near any vertex of j (genuine shared corner)
                near_vtx = False
                for (bx, by, _ba) in sv[j]:
                    if math.hypot(vx - bx, vy - by) < VTX_TOL:
                        near_vtx = True
                        break
                if near_vtx:
                    continue
                for e in range(len(cb) - 1):
                    (ax, ay), (bx, by) = cb[e], cb[e + 1]
                    dx, dy = bx - ax, by - ay
                    L2 = dx * dx + dy * dy
                    if L2 < 1e-9:
                        continue
                    t = ((vx - ax) * dx + (vy - ay) * dy) / L2
                    if t <= 0.02 or t >= 0.98:
                        continue
                    fx, fy = ax + t * dx, ay + t * dy
                    perp = math.hypot(vx - fx, vy - fy)
                    if perp > PERP_TOL:
                        continue
                    tjunc += 1
                    tjunc_role_pairs[
                        (shapes[i].role, shapes[j].role)] += 1
                    # interpolated alt on edge
                    ealt = None
                    bj = sv[j]
                    if (valt is not None and bj and e + 1 < len(cb)
                            and e < len(bj) and (e + 1) < len(bj)):
                        a0 = bj[e][2]
                        a1 = bj[(e + 1) % len(bj)][2]
                        if a0 is not None and a1 is not None:
                            ealt = a0 + t * (a1 - a0)
                    if (valt is not None and ealt is not None
                            and abs(valt - ealt) > 1.0):
                        tjunc_wall += 1
                        if len(tj_examples) < 12:
                            tj_examples.append(
                                (abs(valt - ealt), shapes[i].role,
                                 shapes[j].role, vx, vy))
                    break
    print(f"  total hanging nodes: {tjunc}  "
          f"(with Δalt>1m vs host edge: {tjunc_wall})")
    for rp, c in tjunc_role_pairs.most_common(12):
        print(f"    {c:>4}  {rp[0]} -> on edge of {rp[1]}")
    print("  worst Δalt examples:")
    for (d, ra, rb, x, y) in sorted(tj_examples, reverse=True):
        print(f"    Δ{d:6.2f} m  {ra} on {rb} edge near "
              f"({x:8.1f},{y:8.1f})")

    # ── 5. taxiway_clearance dump ────────────────────────────────
    print("\n=== taxiway_clearance shape detail ===")
    for idx, s in enumerate(shapes):
        if s.role != "taxiway_clearance":
            continue
        coords = list(s.polygon.exterior.coords)
        n = len(coords) - 1
        edge_lens = [math.hypot(coords[k + 1][0] - coords[k][0],
                                coords[k + 1][1] - coords[k][1])
                     for k in range(n)]
        na = s.node_altitudes
        arange = (f"{min(na):.1f}..{max(na):.1f}"
                  if na else "n/a")
        valid = s.polygon.is_valid
        print(f"  #{idx} verts={n} valid={valid} "
              f"area={s.polygon.area:.1f} alt[{arange}] "
              f"min_edge={min(edge_lens):.3f} "
              f"max_edge={max(edge_lens):.1f} "
              f"tiny(<0.75)={sum(1 for e in edge_lens if e < 0.75)}")

    # ── 6. boundary slice-shape profile ─────────────────────────
    print("\n=== boundary shape profile (211 expected) ===")
    bshapes = [(idx, s) for idx, s in enumerate(shapes)
               if s.role == "boundary"]
    n_sliver = n_tiny = n_thin = n_smallarea = 0
    areas = []
    min_ang_all = []
    for idx, s in enumerate(shapes):
        if s.role != "boundary":
            continue
        ring = list(s.polygon.exterior.coords)[:-1]
        m = len(ring)
        areas.append(s.polygon.area)
        if s.polygon.area < 2.0:
            n_smallarea += 1
        # min edge / min angle
        edge_lens = [math.hypot(ring[(k + 1) % m][0] - ring[k][0],
                                ring[(k + 1) % m][1] - ring[k][1])
                     for k in range(m)]
        if edge_lens and min(edge_lens) < 0.75:
            n_tiny += 1
        worst = 180.0
        for vi in range(m):
            ax, ay = ring[(vi - 1) % m]
            bx, by = ring[vi]
            cx, cy = ring[(vi + 1) % m]
            v1 = (ax - bx, ay - by)
            v2 = (cx - bx, cy - by)
            n1 = math.hypot(*v1)
            n2 = math.hypot(*v2)
            if n1 < 1e-9 or n2 < 1e-9:
                continue
            cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
            ang = math.degrees(math.acos(max(-1, min(1, cosang))))
            worst = min(worst, ang)
        min_ang_all.append(worst)
        if worst < 5.0:
            n_sliver += 1
        # thin aspect: bbox aspect
        bx0, by0, bx1, by1 = s.polygon.bounds
        w = max(bx1 - bx0, 1e-6)
        h = max(by1 - by0, 1e-6)
        if max(w, h) / min(w, h) > 12.0:
            n_thin += 1
    if areas:
        print(f"  count={len(areas)}  area min={min(areas):.2f} "
              f"max={max(areas):.0f} mean={sum(areas)/len(areas):.1f}")
        print(f"  sliver-angle(<5deg)={n_sliver}  tiny-edge={n_tiny}  "
              f"thin-aspect(>12:1)={n_thin}  area<2m2={n_smallarea}")
        print(f"  worst min-angle overall: {min(min_ang_all):.2f} deg")

    # boundary-boundary overlap (stacked / duplicate shapes)
    bidx = [idx for (idx, _s) in bshapes]
    overlap_pairs = 0
    for ii in range(len(bidx)):
        a = polys[bidx[ii]]
        for jj_id in tree.query(a):
            jj = int(jj_id)
            if jj <= bidx[ii] or shapes[jj].role != "boundary":
                continue
            b = polys[jj]
            try:
                inter = a.intersection(b).area
            except Exception:
                continue
            small = min(a.area, b.area)
            if small > 0 and inter > 0.5 * small:
                overlap_pairs += 1
    print(f"  boundary-boundary >50%-overlap pairs: {overlap_pairs}")

    # ── 7. dump degenerate #249 fan ──────────────────────────────
    print("\n=== shape #249 fan dump (first 20 verts) ===")
    s249 = shapes[249]
    rc = list(s249.polygon.exterior.coords)
    na = s249.node_altitudes or []
    for k in range(min(20, len(rc))):
        a = na[k] if k < len(na) else None
        print(f"    [{k:2}] ({rc[k][0]:9.3f},{rc[k][1]:9.3f})  "
              f"alt={a}")

    print("\nDone.")


if __name__ == "__main__":
    main()

"""Build-time verification of an emitted airport layout.

Single source of truth for the auto-patch invariant checks, shared by:

  * the PRODUCTION build — ``driver.generate_auto_patches`` calls
    :func:`verify_and_log` on every airport it builds for a tile, so a
    user running Ortho4XP is told when an airport patch has errors; and
  * the DEV pytest gate — the baseline-airport tests call the same check
    functions and ``assert`` on them.

There is exactly ONE implementation of each check.  Thresholds are
UNIVERSAL — no per-airport exceptions.

Diagnostics: the user's only fix lever is the source data (apt.dat /
DSF), so every reported violation says WHAT, WHERE — the ``shapeID`` to
open in the patch, a lat/lon, and (for junctions/aprons) the taxiways
that meet there, e.g. "junction [#375] where taxiways A, M meet" — a
likely CAUSE, and a suggested FIX.  Once the elevation solver is
complete a grade violation almost always means a geometry / source
problem, so the hints lean that way.

``shapeID`` == the shape's index in ``layout.shapes`` (the same value
``layout.to_osm`` writes as the ``shapeID`` tag), so a reported id maps
directly to the way in the emitted patch.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import O4_UI_Utils as UI

_TAXI_ROLES = ("primary_parallel", "secondary_parallel",
               "stub", "cross_connector")

# Roles that are NOT airside pavement built from apt.dat row-110 / DSF —
# excluded from the source-adjacency check.
_NON_SOURCE_PAVEMENT_ROLES = frozenset({
    "boundary", "taxiway_clearance", "runway_clearance",
    "retaining_wall", "tunnel_ramp", "groundside_pavement",
    "service_road", "service_junction", "terminal",
})

_HINTS = {
    "overlap": ("two pavement shapes share footprint — likely a duplicate "
                "DSF .pol overlay over apt.dat row-110, or two row-110 "
                "polygons covering the same area. Fix: remove the redundant "
                "overlay / merge the duplicate source pavement."),
    "source":  ("emitted pavement with NO apt.dat/DSF source beneath it — a "
                "spurious synthesis or a non-pavement polygon (grass/decor) "
                "tagged as pavement. Fix: correct or remove that source "
                "polygon."),
    "within":  ("the surface cannot stay within its grade cap — terrain "
                "(DEM) too steep across it, or incompatible anchor "
                "elevations. Fix: check the apt.dat geometry isn't spanning "
                "a real slope and the CIFP runway-threshold elevations are "
                "right (with a complete elevation solver, a residual here is "
                "a geometry/source problem)."),
    "cross":   ("adjacent surfaces meet at incompatible elevations (e.g. "
                "flat areas at different levels sharing a corner). Fix: check "
                "the apt.dat pavement layout where these shapes meet."),
    "steps":   ("a surface meets a neighbour at a vertical step — usually "
                "resolves once the grade issues above are fixed."),
}


def _ll(layout, x, y) -> str:
    """Format a layout-meter point as a ``lat,lon`` string."""
    try:
        lat, lon = layout.m_to_ll(x, y)
        return f"{lat:.5f},{lon:.5f}"
    except Exception:
        return "?,?"


def build_taxi_index(layout):
    """STRtree of taxi-rect polygons + parallel ref list, for naming the
    taxiways adjacent to a junction/apron.  Returns ``(tree, geoms,
    refs)`` or ``(None, [], [])``."""
    from shapely.strtree import STRtree
    geoms, refs = [], []
    for s in layout.shapes:
        if (s.role in _TAXI_ROLES and s.polygon is not None
                and not s.polygon.is_empty and (s.ref or "").strip()):
            geoms.append(s.polygon)
            refs.append((s.ref or "").strip())
    if not geoms:
        return (None, [], [])
    return (STRtree(geoms), geoms, refs)


def _neighbour_taxi_refs(poly, taxi_index, tol_m: float = 1.0):
    """Distinct taxiway refs whose rect touches ``poly`` (within
    ``tol_m``).  Sub-refs are collapsed to their base letter so
    "A, A3, M" reads "A, M"."""
    tree, geoms, refs = taxi_index
    if tree is None or poly is None or poly.is_empty:
        return []
    out = set()
    try:
        cand = tree.query(poly)
    except Exception:
        return []
    for j in cand:
        try:
            if poly.distance(geoms[j]) <= tol_m:
                r = refs[j]
                base = r[0] if r and r[0].isalpha() else r
                out.add(base)
        except Exception:
            continue
    return sorted(out)


def describe_shape(layout, idx, taxi_index=None) -> str:
    """Human description of ``layout.shapes[idx]``: role, ref, the
    ``shapeID`` to open in the patch, and — for a junction/apron — the
    taxiways that meet there."""
    try:
        s = layout.shapes[idx]
    except (IndexError, TypeError):
        return f"shape [#{idx}]"
    role = s.role or "?"
    ref = (s.ref or "").strip()
    head = f"{role} {ref}".strip() if ref else role
    out = f"{head} [#{idx}]"
    if role in ("junction", "apron") and taxi_index is not None:
        nb = _neighbour_taxi_refs(s.polygon, taxi_index)
        if len(nb) >= 2:
            out += f" where taxiways {', '.join(nb[:4])} meet"
        elif len(nb) == 1:
            out += f" off taxiway {nb[0]}"
    return out


def _import_check_grade():
    """``tools/check_grade.py`` is the canonical grade validator but lives
    in the repo's ``tools`` dir (not an installed package)."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    tools_dir = os.path.join(repo_root, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import check_grade  # noqa: E402
    return check_grade


# ── Geometry invariants ─────────────────────────────────────────────
def check_self_overlap(layout):
    """Invariant A1: no two emitted pavement polygons may overlap.
    Returns ``[(area_m2, idx_a, idx_b, "lat,lon"), …]`` largest first."""
    from shapely.strtree import STRtree
    polys = [(i, s.polygon) for i, s in enumerate(layout.shapes)
             if s.polygon is not None and not s.polygon.is_empty]
    if len(polys) < 2:
        return []
    tree = STRtree([p for _, p in polys])
    pairs = []
    for k, (idx_a, pa) in enumerate(polys):
        for q in tree.query(pa):
            if q <= k:
                continue
            idx_b, pb = polys[q]
            try:
                inter = pa.intersection(pb)
            except Exception:
                continue
            if inter.is_empty or inter.area <= 0.0:
                continue
            c = inter.representative_point()
            pairs.append((inter.area, idx_a, idx_b, _ll(layout, c.x, c.y)))
    pairs.sort(key=lambda r: r[0], reverse=True)
    return pairs


def check_source_adjacency(layout, min_on_source_frac: float = 0.5):
    """Invariant: every emitted PAVEMENT shape must rest on real source
    pavement (apt.dat row-110 ∪ DSF ∪ runway) by ≥ ``min_on_source_frac``
    of its own area.  Source-relative, per-shape, no per-airport ratio.
    Returns ``[(idx, area_m2, on_frac, "lat,lon"), …]`` largest first;
    ``[]`` when no source union was recorded."""
    src = getattr(layout, "source_pavement_union", None)
    if src is None or src.is_empty:
        return []
    rwy = getattr(layout, "runway_union", None)
    if rwy is not None and not rwy.is_empty:
        try:
            src = src.union(rwy)
        except Exception:
            pass
    out = []
    for i, s in enumerate(layout.shapes):
        if s.polygon is None or s.polygon.is_empty:
            continue
        if (s.role or "") in _NON_SOURCE_PAVEMENT_ROLES:
            continue
        area = s.polygon.area
        if area <= 1.0:
            continue
        try:
            on = s.polygon.intersection(src).area
        except Exception:
            continue
        frac = on / area if area > 0 else 1.0
        if frac < min_on_source_frac:
            c = s.polygon.representative_point()
            out.append((i, area, frac, _ll(layout, c.x, c.y)))
    out.sort(key=lambda r: r[1], reverse=True)
    return out


_COVERAGE_FEATURE_ROLES = frozenset({
    "boundary", "taxiway_clearance", "runway_clearance",
    "retaining_wall", "tunnel_ramp",
})


def uncovered_interior_source_pieces(layout, min_gap_area_m2: float = 5.0,
                                     min_enclosed_frac: float = 0.70):
    """The INTERIOR gaps where emitted pavement fails to cover the source: the
    ``source_pavement_union`` (∪ runway) minus the union of every pavement-
    occupying shape (all roles except pure FEATURES — boundary, clearance
    shadows, walls, tunnel ramps), keeping only pieces that are (a) ≥
    ``min_gap_area_m2`` and (b) ENCLOSED — at least ``min_enclosed_frac`` of the
    perimeter shared with emitted pavement (so the airport's outer perimeter and
    real voids touching open ground are excluded).  Returns ``[(Polygon,
    enclosed_frac), …]`` largest first — the shared geometry source for the
    ``check_source_coverage`` invariant and the reclaim pass."""
    src = getattr(layout, "source_pavement_union", None)
    if src is None or src.is_empty:
        return []
    rwy = getattr(layout, "runway_union", None)
    if rwy is not None and not rwy.is_empty:
        try:
            src = src.union(rwy)
        except Exception:
            return []
    from shapely.ops import unary_union
    emitted = [s.polygon for s in layout.shapes
               if (s.role or "") not in _COVERAGE_FEATURE_ROLES
               and s.polygon is not None and not s.polygon.is_empty]
    if not emitted:
        return []
    try:
        emit_u = unary_union(emitted)
        leftover = src.difference(emit_u)
        emit_boundary = emit_u.boundary
    except Exception:
        return []
    pieces = (leftover.geoms if hasattr(leftover, "geoms") else [leftover])
    out = []
    for p in pieces:
        if p.geom_type != "Polygon" or p.is_empty or p.area < min_gap_area_m2:
            continue
        try:
            shared = p.boundary.intersection(emit_boundary).length
            frac = shared / p.boundary.length if p.boundary.length else 0.0
        except Exception:
            continue
        if frac >= min_enclosed_frac:
            out.append((p, frac))
    out.sort(key=lambda r: r[0].area, reverse=True)
    return out


def check_source_coverage(layout, min_gap_area_m2: float = 5.0,
                          min_enclosed_frac: float = 0.70):
    """Invariant: the emitted pavement must COVER the source pavement — no
    INTERIOR gap (a hole surrounded by pavement that uncovers source, so X-Plane
    interpolates terrain across it as a visible bump).  The dual of
    ``check_source_adjacency`` (emitted ⊆ source); here source ⊆ emitted for
    interior regions.  Returns ``[(area_m2, enclosed_frac, "lat,lon"), …]``
    largest first."""
    return [(p.area, frac, _ll(layout, *p.representative_point().coords[0]))
            for p, frac in uncovered_interior_source_pieces(
                layout, min_gap_area_m2, min_enclosed_frac)]


def _longest_pair_axis(pts):
    """``(ox, oy, ux, uy, length)`` of the longest vertex pair in ``pts`` —
    the runway centerline axis (origin at one end, unit direction, length).
    ``None`` if fewer than 2 points or degenerate."""
    import math
    best = -1.0
    A = B = None
    n = len(pts)
    for i in range(n):
        xa, ya = pts[i]
        for j in range(i + 1, n):
            d2 = (pts[j][0] - xa) ** 2 + (pts[j][1] - ya) ** 2
            if d2 > best:
                best, A, B = d2, pts[i], pts[j]
    if A is None or best <= 0:
        return None
    ln = math.sqrt(best)
    return (A[0], A[1], (B[0] - A[0]) / ln, (B[1] - A[1]) / ln, ln)


def _runway_rect_cross_ends(s, coords):
    """The two flat cross-end edges of a 4-corner runway rect as
    ``(mid_x, mid_y, elev)`` tuples.  ``coords`` = the 4 open-ring corners.
    Corner elevations come from the shape's altitude tags (the solver orders a
    sloped rect ring ``[high, low, low, high]``); the two SHORT ring edges are
    the flat cross-ends."""
    import math
    if s.node_altitudes and len(s.node_altitudes) >= 4:
        ce = [float(s.node_altitudes[i]) for i in range(4)]
    elif s.altitude_high is not None and s.altitude_low is not None:
        ah, al = float(s.altitude_high), float(s.altitude_low)
        ce = [ah, al, al, ah]
    elif s.altitude is not None:
        a = float(s.altitude)
        ce = [a, a, a, a]
    else:
        return []
    edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
    edges.sort(key=lambda ab: math.hypot(
        coords[ab[1]][0] - coords[ab[0]][0],
        coords[ab[1]][1] - coords[ab[0]][1]))
    out = []
    for (a, b) in edges[:2]:           # the two shortest = cross-ends
        out.append((0.5 * (coords[a][0] + coords[b][0]),
                    0.5 * (coords[a][1] + coords[b][1]),
                    0.5 * (ce[a] + ce[b])))
    return out


def check_runway_profile(layout, end_grade_cap="default",
                         check_curvature: bool = True, noise_m: float = 0.05):
    """Invariant: the EMITTED runway longitudinal profile must obey the
    FAA/EASA grade caps AND the vertical-curve rate-of-grade-change limit — the
    elevation solver (or a runway-flex MOVE) must never pull a runway out of
    compliance.

    Each runway emits as a chain of sloped ``ROLE_RUNWAY`` rects sharing their
    flat cross-end edges.  Reconstruct each runway's centerline profile (one
    elevation sample per rect cross-end, ordered along the runway axis) and
    check, per consecutive segment:

      * longitudinal grade ≤ ``end_grade_cap`` inside the first/last
        ``RUNWAY_END_FRACTION`` of the length, ``RUNWAY_MAX_GRADE`` (1.5%)
        elsewhere; and (when ``check_curvature``)
      * grade change between consecutive segments ``|g_right − g_left| ≤
        RUNWAY_MAX_GRADE_CHANGE_PER_M · (L_left + L_right)/2`` — the FAA
        vertical-curve K-factor the runway solver's ``faa_rate_of_change_pass``
        enforces on the sample chain (a runway-flex move uses only the grade-cap
        constraints, so it can reintroduce a curvature violation — this catches
        that).

    ``end_grade_cap`` defaults to ``RUNWAY_END_GRADE`` (0.8%); pass ``None`` for
    a uniform ``RUNWAY_MAX_GRADE`` cap (the only longitudinal limit the default
    profile currently enforces — the 0.8% end cap is opt-in and the
    vertical-curve smoothing is STATUS item D, so the strict defaults are RED
    until those land).  ``noise_m`` absorbs altitude float noise.  Returns
    ``[(kind, ref, value, cap, "lat,lon"), …]`` worst-excess first; ``kind`` ∈
    {"grade", "curvature"}; ``value``/``cap`` are decimal grades (grade) or
    grade-change-per-metre (curvature)."""
    import math
    from .config import (
        RUNWAY_MAX_GRADE, RUNWAY_END_GRADE, RUNWAY_END_FRACTION,
        RUNWAY_MAX_GRADE_CHANGE_PER_M)
    if end_grade_cap == "default":
        end_grade_cap = RUNWAY_END_GRADE

    by_ref: dict = {}
    for s in layout.shapes:
        if (s.role or "") != "runway":
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        cs = list(s.polygon.exterior.coords)
        if len(cs) > 1 and cs[0] == cs[-1]:
            cs = cs[:-1]
        if len(cs) != 4:
            continue              # clipped/irregular rect — no clean cross-ends
        by_ref.setdefault(s.ref or "", []).append((s, cs))

    out = []
    for ref, items in by_ref.items():
        ax = _longest_pair_axis([p for _s, cs in items for p in cs])
        if ax is None:
            continue
        ox, oy, ux, uy, L = ax
        if L <= 0:
            continue
        samples = []              # (dist_along_axis, elev, x, y)
        for s, cs in items:
            for (mx, my, e) in _runway_rect_cross_ends(s, cs):
                samples.append(((mx - ox) * ux + (my - oy) * uy, e, mx, my))
        if len(samples) < 2:
            continue
        samples.sort(key=lambda t: t[0])
        # Merge the shared cross-edges of adjacent rects (~coincident).
        merged = []               # (dist, elev, x, y, n)
        for d, e, mx, my in samples:
            if merged and abs(d - merged[-1][0]) <= 5.0:
                pd, pe, px, py, pn = merged[-1]
                k = pn + 1
                merged[-1] = ((pd * pn + d) / k, (pe * pn + e) / k,
                              (px * pn + mx) / k, (py * pn + my) / k, k)
            else:
                merged.append((d, e, mx, my, 1))
        if len(merged) < 2:
            continue
        grades = []               # (g, seg_len, mid_x, mid_y)
        for i in range(len(merged) - 1):
            d0, e0, x0, y0, _ = merged[i]
            d1, e1, x1, y1, _ = merged[i + 1]
            seg = d1 - d0
            if seg < 0.5:
                continue
            fi, fj = d0 / L, d1 / L
            in_end = (min(fi, fj) < RUNWAY_END_FRACTION
                      or max(fi, fj) > 1.0 - RUNWAY_END_FRACTION)
            cap = (end_grade_cap if (in_end and end_grade_cap is not None)
                   else RUNWAY_MAX_GRADE)
            if abs(e1 - e0) - cap * seg > noise_m:
                out.append(("grade", ref, abs(e1 - e0) / seg, cap,
                            _ll(layout, 0.5 * (x0 + x1), 0.5 * (y0 + y1))))
            grades.append(((e1 - e0) / seg, seg,
                           0.5 * (x0 + x1), 0.5 * (y0 + y1)))
        if not check_curvature:
            continue
        for i in range(len(grades) - 1):
            gl, Ll, _xl, _yl = grades[i]
            gr, Lr, mx, my = grades[i + 1]
            max_dg = RUNWAY_MAX_GRADE_CHANGE_PER_M * 0.5 * (Ll + Lr)
            # Altitude-noise floor on the grade difference (each grade carries
            # ~noise_m/seg sampling noise).
            noise_dg = noise_m * (1.0 / Ll + 1.0 / Lr)
            if abs(gr - gl) - max_dg > noise_dg:
                out.append(("curvature", ref, abs(gr - gl), max_dg,
                            _ll(layout, mx, my)))
    out.sort(key=lambda r: -(r[2] - r[3]))
    return out


def check_terminal_flat(layout):
    """Invariant H26: a terminal moves as one rigid flat unit — a single
    ``altitude`` tag, never per-vertex ``node_altitudes`` or two-end
    ``altitude_high``/``altitude_low``.  Returns ``[(idx, detail,
    "lat,lon"), …]``."""
    out = []
    for i, s in enumerate(layout.shapes):
        if (s.role or "") != "terminal":
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        c = s.polygon.representative_point()
        loc = _ll(layout, c.x, c.y)
        if s.altitude is None:
            out.append((i, "no altitude tag (terminal must be flat)", loc))
            continue
        if s.node_altitudes is not None:
            out.append((i, f"has node_altitudes ({len(s.node_altitudes)} "
                           f"entries) — terminals must be flat", loc))
        if s.altitude_high is not None or s.altitude_low is not None:
            out.append((i, "has altitude_high/low — terminals must be flat",
                        loc))
    return out


def check_vertex_on_sloping_edge(layout):
    """Invariant: a non-rect vertex may touch a sloping rect only at a
    CORNER, never on an edge interior (an off-corner vertex injects an
    extra elevation constraint and kinks the rect's plane).  Also flags a
    "sloping" rect that isn't 4-corner.  Returns ``[(rect_idx, detail,
    "lat,lon"), …]``."""
    import math
    sloping_roles = {"runway", "primary_parallel", "secondary_parallel",
                     "stub", "cross_connector", "service_road"}
    # Sloped rects only: skip flat single-altitude shapes (variable node
    # count is fine when elevation is constant) and node_altitudes shapes
    # (slice-conforming, may carry arbitrary ring vertices by design).
    sloping = [(i, s) for i, s in enumerate(layout.shapes)
               if s.role in sloping_roles and s.polygon is not None
               and not s.polygon.is_empty
               and not (s.altitude is not None and s.altitude_high is None
                        and s.altitude_low is None)
               and s.node_altitudes is None]
    others = [s for s in layout.shapes
              if s.role not in sloping_roles and s.polygon is not None
              and not s.polygon.is_empty]
    if not sloping or not others:
        return []
    EDGE_PROX_M = 0.5
    CORNER_GUARD_M = 0.5
    out = []
    for ridx, s in sloping:
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            c = s.polygon.representative_point()
            out.append((ridx, f"sloping rect is non-rect "
                              f"({len(coords)} corners)",
                        _ll(layout, c.x, c.y)))
            continue
        edges = [(coords[i], coords[(i + 1) % 4]) for i in range(4)]
        for o in others:
            ocoords = list(o.polygon.exterior.coords)
            if ocoords and ocoords[0] == ocoords[-1]:
                ocoords = ocoords[:-1]
            for px, py in ocoords:
                if any(math.hypot(px - cx, py - cy) <= CORNER_GUARD_M
                       for cx, cy in coords):
                    continue
                for (ax, ay), (bx, by) in edges:
                    dx, dy = bx - ax, by - ay
                    L2 = dx * dx + dy * dy
                    if L2 <= 0:
                        continue
                    t = ((px - ax) * dx + (py - ay) * dy) / L2
                    if t <= 0.001 or t >= 0.999:
                        continue
                    pjx, pjy = ax + t * dx, ay + t * dy
                    d = math.hypot(px - pjx, py - pjy)
                    d_a = math.hypot(px - ax, py - ay)
                    d_b = math.hypot(px - bx, py - by)
                    if (d < EDGE_PROX_M and d_a > CORNER_GUARD_M
                            and d_b > CORNER_GUARD_M):
                        out.append((
                            ridx,
                            f"{o.role}({o.ref or '?'}) vertex lands on a "
                            f"sloping edge (t={t:.3f}, d={d:.2f} m)",
                            _ll(layout, px, py)))
                        break
    return out


def _rect_flat_edges(shape):
    """The two FLAT (cross) edges of a 4-corner rect — perpendicular to
    ``source_axis`` (constant altitude along them).  ``[]`` if not a
    4-corner rect."""
    import math
    poly = shape.polygon
    coords = list(poly.exterior.coords)
    if not coords:
        return []
    if coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) != 4:
        return []
    edges = [(coords[i], coords[(i + 1) % 4]) for i in range(4)]
    sa = getattr(shape, "source_axis", None)
    if sa is not None and not sa.is_empty:
        axp = list(sa.coords)
        if len(axp) >= 2:
            axdx, axdy = axp[-1][0] - axp[0][0], axp[-1][1] - axp[0][1]
            axlen = math.hypot(axdx, axdy)
            if axlen >= 1e-6:
                aux, auy = axdx / axlen, axdy / axlen
                dots = []
                for a, b in edges:
                    ex, ey = b[0] - a[0], b[1] - a[1]
                    elen = math.hypot(ex, ey)
                    dots.append(0.0 if elen < 1e-6
                                else abs(ex * aux + ey * auy) / elen)
                flat_idx = sorted(range(4), key=lambda i: dots[i])[:2]
                return [edges[i] for i in flat_idx]
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in edges]
    short_idx = sorted(range(4), key=lambda i: lengths[i])[:2]
    return [edges[i] for i in short_idx]


def check_vertex_on_flat_edge(layout):
    """Invariant: a sloping rect's FLAT (cross) edge meets a junction /
    apron 1:1 — only its 2 corners are legal shared vertices, never a
    node on the edge interior (a third node there steps the rect's slope
    away from its linear-corner plane).  Returns ``[(rect_idx, detail,
    "lat,lon"), …]``."""
    import math
    sloping_roles = {"primary_parallel", "secondary_parallel",
                     "stub", "cross_connector", "service_road"}
    sloping = [(i, s) for i, s in enumerate(layout.shapes)
               if s.role in sloping_roles and s.polygon is not None
               and not s.polygon.is_empty
               and s.altitude_high is not None
               and s.altitude_low is not None]
    if not sloping:
        return []
    # Must EXCEED the 1.0 m perpendicular nudge that
    # ``_push_junction_vertices_off_taxi_rect_edges`` applies (edge_gap_m=1.0):
    # a vertex pushed to *exactly* 1.0 m off a flat edge straddles a 1.0 m
    # threshold (float-flaky — caught at 0.999, missed at 1.0001, so the HECA
    # W2/#303 gap slipped through while stub C was caught).  1.5 m reliably
    # catches the pushed-off vertex plus minor subsequent weld/conformance drift.
    EDGE_PROX_M = 1.5
    CORNER_GUARD_M = 1.0
    out = []
    for ridx, s in sloping:
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        flat_edges = _rect_flat_edges(s)
        if not flat_edges:
            continue
        for o in layout.shapes:
            if o is s or o.polygon is None or o.polygon.is_empty:
                continue
            if o.role in sloping_roles:
                continue
            ocoords = list(o.polygon.exterior.coords)
            if ocoords and ocoords[0] == ocoords[-1]:
                ocoords = ocoords[:-1]
            for px, py in ocoords:
                if any(math.hypot(px - cx, py - cy) <= CORNER_GUARD_M
                       for cx, cy in coords):
                    continue
                for (ax, ay), (bx, by) in flat_edges:
                    dx, dy = bx - ax, by - ay
                    L2 = dx * dx + dy * dy
                    if L2 <= 0:
                        continue
                    t = ((px - ax) * dx + (py - ay) * dy) / L2
                    if t <= 0.001 or t >= 0.999:
                        continue
                    pjx, pjy = ax + t * dx, ay + t * dy
                    d = math.hypot(px - pjx, py - pjy)
                    d_a = math.hypot(px - ax, py - ay)
                    d_b = math.hypot(px - bx, py - by)
                    if (d <= EDGE_PROX_M and d_a > CORNER_GUARD_M
                            and d_b > CORNER_GUARD_M):
                        out.append((
                            ridx,
                            f"{o.role}({o.ref or '?'}) vertex on the flat "
                            f"(cross) edge (t={t:.3f}, d={d:.2f} m)",
                            _ll(layout, px, py)))
                        break
    return out


def check_sloping_rect_axis(layout):
    """Invariant: a canonical sloping rect may slope ONLY along its
    centerline — each of its two AXIS-END (flat) edges must be level
    (both endpoints at the same elevation).  A non-flat axis-end means
    the rect tilts ACROSS the taxiway.  node_altitudes shapes are exempt
    (slice-conforming).  Returns ``[(idx, detail, "lat,lon"), …]``."""
    from .layout import corner_alts_from_high_low
    sloping_roles = {"primary_parallel", "secondary_parallel", "stub",
                     "cross_connector", "service_road"}
    TOL = 0.3
    out = []
    for i, s in enumerate(layout.shapes):
        if s.role not in sloping_roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.node_altitudes:
            continue
        if s.altitude_high is None or s.altitude_low is None:
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        alts = corner_alts_from_high_low(s.altitude_high, s.altitude_low)
        cmap = {(round(c[0], 3), round(c[1], 3)): alts[k]
                for k, c in enumerate(coords)}
        for a, b in _rect_flat_edges(s):
            za = cmap.get((round(a[0], 3), round(a[1], 3)))
            zb = cmap.get((round(b[0], 3), round(b[1], 3)))
            if za is None or zb is None:
                continue
            if abs(za - zb) > TOL:
                c = s.polygon.representative_point()
                out.append((i, f"axis-end edge not flat (Δ{abs(za - zb):.2f} "
                               f"m — rect tilts across the taxiway)",
                            _ll(layout, c.x, c.y)))
                break
    return out


def check_rect_short_edges(layout):
    """Invariant: each of a taxi rect's two SHORT edges must connect to
    something (junction / runway / terminal / other rect) — at least one
    corner shared with another shape.  A fully-disconnected short edge =
    a rect ending in mid-air.  Faithful exemptions: tile-cut boundary
    edges (bridged by the neighbour tile), and discovered ("TX") lanes
    that genuinely dead-end (isolated, or at the pavement tip).  Returns
    ``[(rect_idx, detail, "lat,lon"), …]``."""
    import math
    from shapely.geometry import Point as _P
    CORNER_SHARE_TOL_M = 0.5
    TILE_EDGE_TOL_M = 8.0
    DEAD_END_ISOLATION_M = 25.0
    TIP_BOUNDARY_TOL_M = 1.0
    rect_roles = {"primary_parallel", "secondary_parallel",
                  "stub", "cross_connector"}

    def _on_tile_edge(x, y):
        lat, lon = layout.m_to_ll(x, y)
        dlat_m = abs(lat - round(lat)) * 111195.0
        dlon_m = (abs(lon - round(lon)) * 111195.0
                  * math.cos(math.radians(lat)))
        return dlat_m < TILE_EDGE_TOL_M or dlon_m < TILE_EDGE_TOL_M

    all_vertices = []
    for si, s in enumerate(layout.shapes):
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except Exception:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        for x, y in coords:
            all_vertices.append((x, y, si))
    tol2 = CORNER_SHARE_TOL_M * CORNER_SHARE_TOL_M
    pav_b = getattr(layout, "apt_pavement_boundary", None)
    out = []
    for ri, r in enumerate(layout.shapes):
        if r.role not in rect_roles or r.polygon is None or r.polygon.is_empty:
            continue
        try:
            rc = list(r.polygon.exterior.coords)
        except Exception:
            continue
        if rc and rc[0] == rc[-1]:
            rc = rc[:-1]
        if len(rc) != 4:
            continue
        for end_label, (i_a, i_b) in (("end_A", (0, 3)), ("end_B", (1, 2))):
            ax, ay = rc[i_a]
            bx, by = rc[i_b]
            shared_a = shared_b = False
            for vx, vy, vsi in all_vertices:
                if vsi == ri:
                    continue
                if not shared_a and (vx - ax) ** 2 + (vy - ay) ** 2 <= tol2:
                    shared_a = True
                if not shared_b and (vx - bx) ** 2 + (vy - by) ** 2 <= tol2:
                    shared_b = True
                if shared_a and shared_b:
                    break
            if shared_a or shared_b:
                continue
            if _on_tile_edge(ax, ay) and _on_tile_edge(bx, by):
                continue
            if (r.ref or "").startswith("TX"):
                o_a, o_b = ((1, 2) if end_label == "end_A" else (0, 3))
                oax, oay = rc[o_a]
                obx, oby = rc[o_b]
                other_shared = False
                near_iso2 = DEAD_END_ISOLATION_M ** 2
                min_a2 = min_b2 = float("inf")
                for vx, vy, vsi in all_vertices:
                    if vsi == ri:
                        continue
                    if not other_shared and (
                            (vx - oax) ** 2 + (vy - oay) ** 2 <= tol2
                            or (vx - obx) ** 2 + (vy - oby) ** 2 <= tol2):
                        other_shared = True
                    da2 = (vx - ax) ** 2 + (vy - ay) ** 2
                    db2 = (vx - bx) ** 2 + (vy - by) ** 2
                    if da2 < min_a2:
                        min_a2 = da2
                    if db2 < min_b2:
                        min_b2 = db2
                isolated = (min_a2 > near_iso2 and min_b2 > near_iso2)
                on_tip = False
                if pav_b is not None:
                    try:
                        on_tip = (pav_b.distance(_P(ax, ay)) <= TIP_BOUNDARY_TOL_M
                                  and pav_b.distance(_P(bx, by))
                                  <= TIP_BOUNDARY_TOL_M)
                    except Exception:
                        on_tip = False
                if other_shared and (isolated or on_tip):
                    continue
            mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
            out.append((ri, f"{end_label} short edge connects to nothing "
                            f"(rect ends in mid-air)", _ll(layout, mx, my)))
    return out


# ── Grade invariants (reuse the check_grade engine) ─────────────────
def taxi_axes_ll(layout):
    """Per-axis taxi grading mirror — the SAME construction the grade
    test uses."""
    try:
        from .elevation_per_surface import unified_jacobi as _uj
    except Exception:
        return None
    if not getattr(_uj, "_PER_AXIS_JUNCTIONS", False):
        return None
    letters = getattr(layout, "apt_taxi_letters", {}) or {}
    axes = []
    for ln, name in (getattr(layout, "apt_taxi_centerlines", []) or []):
        if ln is None or ln.is_empty:
            continue
        letter = letters.get(name)
        cL = 0.03 if letter in ("A", "B") else 0.015
        cT = 0.02 if letter in ("A", "B") else 0.015
        pts = [layout.m_to_ll(x, y) for (x, y) in ln.coords]
        axes.append((pts, cL, cT))
    return axes


def run_grade_checks(layout):
    """Run the grade engine on ``layout``.  Returns ``(within, cross,
    steps)`` with ``.lat`` / ``.lon`` + way labels populated."""
    check_grade = _import_check_grade()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "verify.osm"
        layout.to_osm(str(out))
        return check_grade.run_checks(
            out, max_grade_pct=1.5, proximity_m=1.0, edge_search_m=5.0,
            edge_step_m=0.5, top_n=5, taxi_axes_ll=taxi_axes_ll(layout),
            quiet=True)


# ── Build-time entry point ──────────────────────────────────────────
def verify_and_log(layout, icao: str) -> dict:
    """Run every verification check on a freshly-built layout and LOG a
    diagnostic summary (never raises).  Returns a counts dict.  Problems
    log at verbosity 0; a clean airport at verbosity 1."""
    overlaps = source = within = cross = steps = []
    try:
        overlaps = check_self_overlap(layout)
    except Exception:                              # pragma: no cover
        pass
    try:
        source = check_source_adjacency(layout)
    except Exception:                              # pragma: no cover
        pass
    flat = edge_v = flat_v = axis_v = []
    try:
        flat = check_terminal_flat(layout)
    except Exception:                              # pragma: no cover
        pass
    try:
        edge_v = check_vertex_on_sloping_edge(layout)
    except Exception:                              # pragma: no cover
        pass
    try:
        flat_v = check_vertex_on_flat_edge(layout)
    except Exception:                              # pragma: no cover
        pass
    try:
        axis_v = check_sloping_rect_axis(layout)
    except Exception:                              # pragma: no cover
        pass
    short_e = []
    try:
        short_e = check_rect_short_edges(layout)
    except Exception:                              # pragma: no cover
        pass
    try:
        within, cross, steps = run_grade_checks(layout)
    except Exception as exc:                       # pragma: no cover
        UI.lvprint(0, f"  [verify] {icao}: grade verification "
                       f"unavailable ({exc})")
        within = cross = steps = []
    # Runway longitudinal grade at the uniform 1.5% cap — the binding limit the
    # runway solver enforces today.  (The 0.8% end cap + FAA vertical-curve
    # rate are deliberately NOT logged here: they are expected RED until the
    # vertical-curve smoothing lands and would spam every airport — they are
    # tracked by the test_runway_vertical_curve xfail instead.)
    rwy_grade = []
    try:
        rwy_grade = check_runway_profile(
            layout, end_grade_cap=None, check_curvature=False)
    except Exception:                              # pragma: no cover
        pass

    counts = {"overlap": len(overlaps), "source": len(source),
              "terminal_flat": len(flat), "vertex_on_edge": len(edge_v),
              "vertex_on_flat_edge": len(flat_v),
              "axis_tilt": len(axis_v), "short_edge": len(short_e),
              "cross": len(cross), "within": len(within),
              "steps": len(steps), "runway_grade": len(rwy_grade)}
    if not sum(counts.values()):
        UI.vprint(1, f"  [verify] {icao}: OK — no overlap / source / "
                     f"grade issues.")
        return counts

    taxi_index = build_taxi_index(layout)
    try:
        glabel_id = _import_check_grade()
    except Exception:                              # pragma: no cover
        glabel_id = None

    def _gdesc(way):
        """Describe a grade-violation way: prefer the layout shapeID (gives
        adjacency); fall back to check_grade's label."""
        sid = way.tags.get("shapeID") if getattr(way, "tags", None) else None
        if sid is not None:
            try:
                return describe_shape(layout, int(sid), taxi_index)
            except Exception:
                pass
        return glabel_id._label(way) if glabel_id else "?"

    tally = " ".join(f"{k}={v}" for k, v in counts.items() if v)
    UI.lvprint(0,
        f"  [verify] {icao}: PATCH ISSUES — {tally}. "
        f"Likely apt.dat / DSF source problems — details below.")

    if overlaps:
        for area, ia, ib, loc in overlaps[:5]:
            UI.lvprint(0, f"  [verify]   OVERLAP {area:.1f} m² @ {loc}: "
                          f"{describe_shape(layout, ia, taxi_index)} ∩ "
                          f"{describe_shape(layout, ib, taxi_index)}")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['overlap']}")
    if source:
        for idx, area, frac, loc in source[:5]:
            UI.lvprint(0, f"  [verify]   OFF-SOURCE {area:.0f} m² "
                          f"({frac*100:.0f}% on source) @ {loc}: "
                          f"{describe_shape(layout, idx, taxi_index)}")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['source']}")
    if flat:
        for idx, detail, loc in flat[:5]:
            UI.lvprint(0, f"  [verify]   TERMINAL-FLAT @ {loc}: "
                          f"{describe_shape(layout, idx, taxi_index)} {detail}")
        UI.lvprint(0, f"  [verify]     ↳ a terminal building pad must be a "
                      f"single flat altitude. Fix: usually a builder issue, "
                      f"not source data.")
    if edge_v:
        for idx, detail, loc in edge_v[:5]:
            UI.lvprint(0, f"  [verify]   VERTEX-ON-EDGE @ {loc}: "
                          f"{describe_shape(layout, idx, taxi_index)} — {detail}")
        UI.lvprint(0, f"  [verify]     ↳ a junction/apron vertex sits on a "
                      f"taxi-rect edge interior (should meet only at corners). "
                      f"Fix: usually a builder issue, not source data.")
    if flat_v:
        for idx, detail, loc in flat_v[:5]:
            UI.lvprint(0, f"  [verify]   FLAT-EDGE @ {loc}: "
                          f"{describe_shape(layout, idx, taxi_index)} — {detail}")
        UI.lvprint(0, f"  [verify]     ↳ a junction/apron vertex sits on a "
                      f"taxi-rect FLAT (cross) edge interior (only the 2 corners "
                      f"may be shared). Fix: usually a builder issue.")
    if axis_v:
        for idx, detail, loc in axis_v[:5]:
            UI.lvprint(0, f"  [verify]   AXIS-TILT @ {loc}: "
                          f"{describe_shape(layout, idx, taxi_index)} — {detail}")
        UI.lvprint(0, f"  [verify]     ↳ a taxi rect tilts across its centerline "
                      f"instead of sloping only along it. Fix: usually a builder "
                      f"issue, not source data.")
    if short_e:
        for idx, detail, loc in short_e[:5]:
            UI.lvprint(0, f"  [verify]   SHORT-EDGE @ {loc}: "
                          f"{describe_shape(layout, idx, taxi_index)} — {detail}")
        UI.lvprint(0, f"  [verify]     ↳ a taxiway rect ends without meeting a "
                      f"junction/runway/terminal. Fix: often a gap in the apt.dat "
                      f"taxi network or a missing connecting pavement.")
    if cross:
        for v in sorted(cross, key=lambda v: -v.de_m)[:5]:
            loc = f"{v.lat:.5f},{v.lon:.5f}" if v.lat is not None else "?,?"
            UI.lvprint(0, f"  [verify]   CROSS-SHAPE {v.de_m:.2f} m @ {loc}: "
                          f"{_gdesc(v.way_a)} ↔ {_gdesc(v.way_b)}")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['cross']}")
    if within:
        for v in sorted(within, key=lambda v: -v.grade_pct)[:5]:
            loc = f"{v.lat:.5f},{v.lon:.5f}" if v.lat is not None else "?,?"
            UI.lvprint(0, f"  [verify]   WITHIN-SHAPE {v.grade_pct:.1f}% over "
                          f"{v.distance_m:.1f} m @ {loc}: {_gdesc(v.way_a)}")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['within']}")
    if steps:
        UI.lvprint(0, f"  [verify]   EDGE-STEPS: {len(steps)} vertical "
                      f"step(s) > 0.5 m between adjacent surfaces.")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['steps']}")
    if rwy_grade:
        for kind, ref, val, cap, loc in rwy_grade[:5]:
            UI.lvprint(0, f"  [verify]   RUNWAY-GRADE {val*100:.2f}% > "
                          f"{cap*100:.1f}% @ {loc}: runway {ref}")
        UI.lvprint(0, f"  [verify]     ↳ the runway longitudinal profile "
                      f"exceeds the 1.5% grade cap — the solver / runway-flex "
                      f"pulled it out of compliance. Fix: the runway solver.")
    return counts

"""Build-time verification of an emitted airport layout.

Single source of truth for the auto-patch invariant checks, shared by:

  * the PRODUCTION build — ``driver.generate_auto_patches`` calls
    :func:`verify_and_log` on every airport it builds for a tile; and
  * the DEV pytest gate — the baseline-airport tests call the same check
    functions and ``assert`` on them.

There is exactly ONE implementation of each check.  Thresholds are
UNIVERSAL — no per-airport exceptions.

Diagnostics: EVERY finding is an auto-patch BUG to be tracked down and
fixed by an engineer, NOT something the user can correct in the source
data (user ruling 2026-06-16).  So no finding is printed as ``[verify]``
chatter — ``verify_and_log`` appends them ALL to the per-tile verify
DEBUG log (``<patch_dir>/auto_patch_verify_debug.log``), each saying WHAT,
WHERE — the ``shapeID`` to open in the patch, a lat/lon, and (for
junctions/aprons) the taxiways that meet there, e.g. "junction [#375]
where taxiways A, M meet".

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
    "service_road", "service_junction", "building",
})

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
    Returns ``[(area_m2, idx_a, idx_b, "lat,lon"), …]`` largest first.

    Noise floor 0.1 m²: the post-solve feature conformance welds seam
    nodes by inserting a neighbour's exact vertex, which detours a
    ring by float-epsilon and leaves sliver "overlaps" 0.00–0.05 m²
    (sub-millimetre wide over tens of metres).  Those quantize to
    nothing at the .11f OSM emit precision — they cannot reach the
    mesh — so flagging them is pure noise."""
    from shapely.strtree import STRtree
    NOISE_M2 = 0.1
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
            if inter.is_empty or inter.area <= NOISE_M2:
                continue
            # Hairline weave: boundary conformance + later sliver-
            # vertex drops leave mm-wide ribbons along long shared
            # edges (KPHL terminal22 ∩ apron: 0.185 m² over a 127 m
            # run = ~1.5 mm wide) whose raw AREA beats the flat floor
            # but which have no mesh-scale width.  An overlap that
            # erodes away at 1 cm cannot survive triangulation; a
            # real double-cover (≥ a few cm wide) does survive.
            try:
                if inter.area <= 5.0:
                    # The intersection of two weaving boundaries is
                    # typically a GeometryCollection (polygons + line
                    # fragments) — erode only its polygonal part.
                    if inter.geom_type == "GeometryCollection":
                        from shapely.ops import unary_union as _uu
                        inter_poly = _uu([g for g in inter.geoms
                                          if g.geom_type in
                                          ("Polygon", "MultiPolygon")])
                    else:
                        inter_poly = inter
                    if (inter_poly.is_empty
                            or inter_poly.buffer(-0.01).is_empty):
                        continue
            except Exception:
                pass
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
                         check_curvature: bool = True, noise_m: float = 0.10):
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
    until those land).  ``noise_m`` (0.10 m) absorbs altitude QUANTIZATION noise:
    runway altitudes EMIT rounded to 0.1 m, so a grade-change reconstructed from
    three quantized cross-end samples carries worst-case noise ~0.1·(1/Ll+1/Lr);
    a tighter floor (e.g. 0.05) flags sub-quantization grade-changes as phantom
    curvature kinks (HECA's 3 "1.1–1.3× kinks" were entirely emit-rounding noise
    — the unrounded solver profile is compliant).  Returns
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


def check_runway_end_skirt(layout, dem, tile_lat, tile_lon,
                           source_runways=None,
                           tolerance_m: float = 1.5,
                           step_m: float = 5.0):
    """Invariant: within the governed length beyond each runway end, the
    RENDERED surface (clearance patches where emitted, pavement or
    natural DEM elsewhere) must not drop below the runway-end-skirt law
    floor (``grade_law.runway_end_skirt_floor_profile`` — FAA 0…−3 % in
    the first 61 m, −5 % beyond, grade-change rate-limited).  Beyond the
    governed length a drop is LAWFUL, so nothing out there is checked.

    Marches the extended centerline of each runway end (the same anchor
    geometry, entry-grade window and governed length the Pass D emitter
    uses — clearance/grade_law are the single source) and reports the
    WORST station per end.  ``tolerance_m`` absorbs the emitter's fill
    trigger (1 m — terrain within 1 m of the floor is deliberately left
    unfilled), emit rounding (0.1 m) and DEM interpolation.

    Pure reporter (verification-architecture ruling): returns
    ``[("end_drop", "<ref>:<desig>", metres_below_floor, tolerance_m,
    "lat,lon"), …]`` worst-first; empty when every end is lawful or when
    ``dem`` is None.  With the ``O4_RUNWAY_END_SKIRT`` gate off this
    reports the cliffs the skirt WOULD govern — the motivating defect —
    so fixture baselines can be captured before flipping the gate.
    """
    import math
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from . import clearance as CL
    from .config import runway_end_approach_class
    from .grade_law import (
        runway_end_constrained_length_m, runway_end_governed_length_m,
        runway_end_skirt_floor_profile)
    from .layout import R_EARTH

    if dem is None:
        return []
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))

    def _ll_to_m(lat, lon):
        return (math.radians(lon - lon0) * R_EARTH * cos0,
                math.radians(lat - lat0) * R_EARTH)

    def _sample(x, y):
        from .elevation import _sample_dem
        try:
            lat = lat0 + math.degrees(y / R_EARTH)
            lon = lon0 + math.degrees(x / (R_EARTH * cos0))
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except (ValueError, ArithmeticError):
            return None

    airside = [s for s in layout.shapes
               if s.role in CL._AIRSIDE_PAVEMENT_ROLES
               and s.polygon is not None and not s.polygon.is_empty]
    if not airside:
        return []
    try:
        prep_pav = prep(unary_union([s.polygon for s in airside]))
    except CL._GEOM_EXC:
        return []
    # Shapes whose surface can lawfully COVER a drop: clearance patches
    # (the skirt itself / RESA cuts) and any other elevation-carrying
    # emitted shape (a crossing taxiway, apron, groundside lot…).
    covering = [s for s in layout.shapes
                if s.polygon is not None and not s.polygon.is_empty
                and (s.node_altitudes or s.altitude is not None
                     or (s.altitude_high is not None
                         and s.altitude_low is not None))]

    _CLEARANCE_ROLES = frozenset(
        {"runway_clearance", "taxiway_clearance"})

    def _surface_alt(x, y, direction):
        """Rendered surface at ``(x, y)`` as ``(altitude, source)``:
        the covering shape's ruled interior along ``direction`` (linear
        between the two boundary crossings bracketing the point — how a
        two-row ``node_altitudes`` band triangulates), else the natural
        DEM.  ``source`` is ``"clearance"`` (skirt / clearance patch —
        this law's own subject), ``"pavement"`` (any OTHER emitted
        shape: an apron, taxiway, groundside lot, service road … graded
        by the SOLVER under its own laws — the skirt lawfully clips
        around it and this check has no jurisdiction there — KCLT 18L's
        flank apron sits 4 m below the pad, correctly), or ``"dem"``
        (un-governed natural terrain — the law's target)."""
        from shapely.geometry import LineString, Point
        pt = Point(x, y)
        nx, ny = direction
        for s in covering:
            try:
                if not s.polygon.covers(pt):
                    continue
            except CL._GEOM_EXC:
                continue
            source = ("clearance" if s.role in _CLEARANCE_ROLES
                      else "pavement")
            if s.node_altitudes:
                try:
                    probe = LineString([
                        (x - nx * 500.0, y - ny * 500.0),
                        (x + nx * 500.0, y + ny * 500.0)])
                    xing = probe.intersection(s.polygon.boundary)
                    pts = ([xing] if xing.geom_type == "Point"
                           else [g for g in getattr(xing, "geoms", [])
                                 if g.geom_type == "Point"])
                except CL._GEOM_EXC:
                    pts = []
                behind, ahead = None, None
                for g in pts:
                    t = (g.x - x) * nx + (g.y - y) * ny
                    if t <= 0.0 and (behind is None or t > behind[0]):
                        behind = (t, g)
                    if t >= 0.0 and (ahead is None or t < ahead[0]):
                        ahead = (t, g)
                if behind is not None and ahead is not None:
                    eb = CL._edge_interp_alt(s, behind[1].x, behind[1].y)
                    ea = CL._edge_interp_alt(s, ahead[1].x, ahead[1].y)
                    if eb is not None and ea is not None:
                        span = ahead[0] - behind[0]
                        if span < 1e-9:
                            return 0.5 * (eb + ea), source
                        return (eb + (ea - eb) * (-behind[0]) / span,
                                source)
            e = CL._edge_interp_alt(s, x, y)
            if e is not None:
                return e, source
        # Narrow-seam bridging: the finalize keeps a small clearance
        # notch (pavement gap + clip buffer, ≤ a station step) between
        # abutting patches — e.g. a blast-pad end and the skirt's inner
        # edge.  The mesh spans it with constraint edges on BOTH sides,
        # so a DEM dip inside the notch never renders.  A station
        # bracketed by two surfaces along the march direction reads the
        # lower of the two instead of the raw DEM; a genuine unfilled
        # drop has pavement on ONE side only and still flags.  A notch
        # abutting NON-clearance pavement inherits that jurisdiction.
        from shapely.ops import nearest_points
        bracketing = []
        for s in covering:
            try:
                if s.polygon.distance(pt) > step_m:
                    continue
                np_pt = nearest_points(s.polygon, pt)[0]
            except CL._GEOM_EXC:
                continue
            t = (np_pt.x - x) * nx + (np_pt.y - y) * ny
            e = CL._edge_interp_alt(s, np_pt.x, np_pt.y)
            if e is not None:
                bracketing.append(
                    (t, e, "clearance" if s.role in _CLEARANCE_ROLES
                     else "pavement"))
        if (bracketing
                and min(t for t, _e, _src in bracketing) <= 0.0
                and max(t for t, _e, _src in bracketing) >= 0.0):
            alt = min(e for _t, e, _src in bracketing)
            source = ("pavement" if any(
                src == "pavement" for _t, _e, src in bracketing)
                else "clearance")
            return alt, source
        # One-sided edge snap: a station within HALF a station step of a
        # covering shape sits inside that fill's own discretization cell
        # — the rendered mesh is dominated by the constraint edge there,
        # so a sub-half-step DEM notch at a jagged fill edge never
        # renders (KCLT pad-corner stations 0.3–2.2 m off the emitted
        # skirt edges).  Genuinely open ground still flags: an
        # un-governed drop spans many stations ≥ half a step from any
        # fill.
        best = None
        for s in covering:
            try:
                d = s.polygon.distance(pt)
            except CL._GEOM_EXC:
                continue
            if d <= 0.5 * step_m and (best is None or d < best[0]):
                best = (d, s)
        if best is not None:
            s = best[1]
            try:
                np_pt = nearest_points(s.polygon, pt)[0]
                e = CL._edge_interp_alt(s, np_pt.x, np_pt.y)
            except CL._GEOM_EXC:
                e = None
            if e is not None:
                return e, ("clearance" if s.role in _CLEARANCE_ROLES
                           else "pavement")
        dem_alt = _sample(x, y)
        return (None, "dem") if dem_alt is None else (dem_alt, "dem")

    # Enumerate runway ends exactly as the Pass D emitter does.
    ends = []
    if source_runways:
        for r in source_runways:
            try:
                ax, ay = _ll_to_m(r.lat_a, r.lon_a)
                bx, by = _ll_to_m(r.lat_b, r.lon_b)
            except CL._GEOM_EXC:
                continue
            dx, dy = bx - ax, by - ay
            full_len = math.hypot(dx, dy)
            if full_len < 1.0:
                continue
            ux, uy = dx / full_len, dy / full_len
            width = float(getattr(r, "width_m", 0.0) or 0.0)
            ends.append((
                (ax, ay), (-ux, -uy), full_len, width, r.desig_a,
                runway_end_approach_class(
                    getattr(r, "markings_a", 0),
                    getattr(r, "approach_lights_a", 0))))
            ends.append((
                (bx, by), (ux, uy), full_len, width, r.desig_b,
                runway_end_approach_class(
                    getattr(r, "markings_b", 0),
                    getattr(r, "approach_lights_b", 0))))
    else:
        runway_shapes = [s for s in layout.shapes if s.role == "runway"
                         and s.polygon is not None
                         and not s.polygon.is_empty]
        for s, a, b, full_len in CL._runway_end_edges(runway_shapes):
            outward = CL._outward_normal(s.polygon, a, b)
            if outward is None:
                continue
            mid = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
            info = CL._rect_long_short_edges(CL._open_coords(s.polygon))
            runway_width = (info[1] if info
                            else math.hypot(b[0] - a[0], b[1] - a[1]))
            ends.append((mid, outward, full_len, runway_width, s.ref,
                         runway_end_approach_class(0, 0)))

    # Stations the EMITTER lawfully cannot fill are exempt (lockstep
    # with its clip list — flagging them would demand the impossible):
    #   * OSM SURFACE road / railway corridors (shared source:
    #     ``clearance._surface_road_corridors``);
    #   * EMITTED infrastructure at its own grade — service roads,
    #     groundside lots, tunnel ramps, retaining walls, building pads
    #     (the static clip keeps the skirt off them, so the rendered
    #     surface next to a runway end can be a perimeter road metres
    #     below the law floor and that is CORRECT — KCLT 18L);
    #   * ground beyond the airport boundary (the skirt is clipped to
    #     the boundary interior).
    road_block = CL._surface_road_corridors(layout, _ll_to_m)
    # EMAS-inference constraint geometry, IDENTICAL to the emitter's.
    constraint_block = CL._end_constraint_block(layout, _ll_to_m)
    _INFRASTRUCTURE_ROLES = frozenset({
        "service_road", "service_junction", "groundside_pavement",
        "tunnel_ramp", "retaining_wall", "building",
    })
    infrastructure = [s for s in layout.shapes
                      if s.role in _INFRASTRUCTURE_ROLES
                      and s.polygon is not None
                      and not s.polygon.is_empty]
    boundary_polygon = layout.airport_boundary

    def _station_exempt(x, y):
        from shapely.geometry import Point
        pt = Point(x, y)
        try:
            if (road_block is not None and not road_block.is_empty
                    and road_block.covers(pt)):
                return True
            if (boundary_polygon is not None
                    and not boundary_polygon.is_empty
                    and not boundary_polygon.covers(pt)):
                return True
            for s in infrastructure:
                # Covering, or inside the emitter's clip gap around it.
                if s.polygon.distance(pt) <= 2.0 * CL._PAVEMENT_GAP_M:
                    return True
        except CL._GEOM_EXC:
            return False
        return False

    out = []
    for end_pt, outward, full_len, runway_width, desig, approach_class \
            in ends:
        nx, ny = outward
        seed = (end_pt[0] - nx * CL._RESA_SEED_INSET_M,
                end_pt[1] - ny * CL._RESA_SEED_INSET_M)
        start = CL._pavement_exit_along(
            prep_pav, seed[0], seed[1], nx, ny,
            CL._RESA_PAVEMENT_PROBE_MAX_M, step_m)
        p0 = (seed[0] + nx * start, seed[1] + ny * start)
        # Containment-free reads, IDENTICAL to the emitter's (see
        # ``clearance._nearest_pav_alt``) — a containment miss on one
        # side silently flattens its entry grade and the two floors
        # diverge (KCLT 18L phantom-flag).
        ref = CL._nearest_pav_alt(
            airside, p0[0] - nx * 1.0, p0[1] - ny * 1.0)
        if ref is None:
            continue
        inside = CL._nearest_pav_alt(
            airside,
            p0[0] - nx * (1.0 + CL._SKIRT_END_GRADE_WINDOW_M),
            p0[1] - ny * (1.0 + CL._SKIRT_END_GRADE_WINDOW_M))
        entry_grade = 0.0
        if inside is not None:
            entry_grade = max(-0.05, min(0.05, (
                float(ref) - float(inside))
                / CL._SKIRT_END_GRADE_WINDOW_M))
        governed = runway_end_governed_length_m(full_len, approach_class)
        # EMAS inference, IDENTICAL to the emitter: a road / service
        # road / water crossing the end zone marks a NON-standard end
        # and shortens the governed length (shared constraint geometry
        # + law clamp).
        governed = runway_end_constrained_length_m(
            governed,
            CL._end_constraint_distance(
                p0, (nx, ny), governed, constraint_block))
        # Check stations strictly INSIDE the governed length: the
        # governed endpoint itself is the crest of the lawful
        # beyond-zone face — a cap-truncated skirt lawfully ends there
        # in a steep engineered face (Madeira-style), and sampling that
        # exact boundary would flag every such skirt at its own edge.
        # A fully constrained end (governed < one station) skips the
        # end march; the FLANKS below are still checked (the overrun
        # pavement exists regardless of what sits beyond it).
        if governed >= step_m:
            n_stations = max(1, int(math.floor(
                (governed - 0.5 * step_m) / step_m)))
            distances = [float(k) * step_m
                         for k in range(1, n_stations + 1)]
            depths = runway_end_skirt_floor_profile(
                distances, entry_grade)
            worst = None
            for d, depth in zip(distances, depths):
                qx, qy = p0[0] + nx * d, p0[1] + ny * d
                if _station_exempt(qx, qy):
                    continue
                surface, source = _surface_alt(qx, qy, (nx, ny))
                if surface is None or source == "pavement":
                    continue
                below = (float(ref) - depth) - float(surface)
                if below > tolerance_m and (
                        worst is None or below > worst[0]):
                    worst = (below, qx, qy)
            if worst is not None:
                out.append(("end_drop", f"{desig}", worst[0],
                            tolerance_m,
                            _ll(layout, worst[1], worst[2])))

        # ── Blast-pad / stopway FLANKS (same governed end zone) ──
        # Between the runway end point and the pavement exit, the
        # overrun pavement's SIDE edges carry the same law: march each
        # flank laterally out to the end-zone corridor (± half), floor
        # measured from the local pavement-edge altitude with a flat
        # entry (mirrors the emitter's flank wrap).
        if start < 2.0 * step_m:
            continue
        half = max(runway_width, CL.runway_strip_half_width_m(full_len))
        perp = (-ny, nx)
        flank_worst = None
        for side in (perp, (-perp[0], -perp[1])):
            sxn, syn = side
            axis_t = step_m
            while axis_t < start - 0.5 * step_m:
                cx = seed[0] + nx * axis_t
                cy = seed[1] + ny * axis_t
                lateral_exit = CL._pavement_exit_along(
                    prep_pav, cx, cy, sxn, syn, half, step_m)
                axis_t += step_m
                room = half - lateral_exit
                if room <= 2.0 * step_m:
                    continue
                edge_x = cx + sxn * lateral_exit
                edge_y = cy + syn * lateral_exit
                edge_alt = CL._nearest_pav_alt(
                    airside, edge_x - sxn * 1.0, edge_y - syn * 1.0)
                if edge_alt is None:
                    continue
                lateral_stations = max(1, int(math.floor(
                    (room - 0.5 * step_m) / step_m)))
                lateral_distances = [float(k) * step_m
                                     for k in range(1, lateral_stations + 1)]
                lateral_depths = runway_end_skirt_floor_profile(
                    lateral_distances, 0.0)
                for dl, depth in zip(lateral_distances, lateral_depths):
                    qx = edge_x + sxn * dl
                    qy = edge_y + syn * dl
                    if _station_exempt(qx, qy):
                        continue
                    surface, source = _surface_alt(qx, qy, side)
                    if surface is None or source == "pavement":
                        continue
                    below = (float(edge_alt) - depth) - float(surface)
                    if below > tolerance_m and (
                            flank_worst is None or below > flank_worst[0]):
                        flank_worst = (below, qx, qy)
        if flank_worst is not None:
            out.append(("end_drop_flank", f"{desig}", flank_worst[0],
                        tolerance_m,
                        _ll(layout, flank_worst[1], flank_worst[2])))
    out.sort(key=lambda r: -r[2])
    return out


def check_terminal_flat(layout):
    """Invariant H26: a terminal moves as one rigid flat unit — a single
    ``altitude`` tag, never per-vertex ``node_altitudes`` or two-end
    ``altitude_high``/``altitude_low``.  Returns ``[(idx, detail,
    "lat,lon"), …]``.

    Only applies when terminals are configured FLAT (``TERMINAL_MAX_GRADE``
    == 0).  When terminals are allowed to grade like aprons (cap > 0) they
    legitimately carry per-vertex altitudes, so the invariant is skipped."""
    from auto_patch.config import TERMINAL_MAX_GRADE
    if TERMINAL_MAX_GRADE > 0.0:
        return []
    out = []
    for i, s in enumerate(layout.shapes):
        if (s.role or "") != "building":
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
    # Designed-clearance road features are exempt: the depressed-road
    # plates and their retaining walls are clipped to exactly
    # wall_gap_m = 0.5 m from all airside pavement (the road passes
    # UNDER; the gap IS the separation, no shared node intended) —
    # their vertices therefore always sit at d≈EDGE_PROX_M and would
    # permanently false-positive here (same rule as the groundside
    # exemption in check_vertex_on_flat_edge).
    _CLEARANCE_FEATURE_ROLES = {"tunnel_ramp", "retaining_wall"}
    others = [s for s in layout.shapes
              if s.role not in sloping_roles
              and s.role not in _CLEARANCE_FEATURE_ROLES
              and s.polygon is not None
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
    away from its linear-corner plane).  Groundside pavement is exempt:
    ``_separate_groundside_from_airside`` clips it to exactly
    GROUNDSIDE_CLEARANCE_M (1.0 m) from all airside pavement, so its
    vertices legitimately sit inside EDGE_PROX_M with no shared node
    (the gap IS the separation — same skip as check_grade's
    airside<->groundside rule).  Returns ``[(rect_idx, detail,
    "lat,lon"), …]``."""
    import math
    from .layout import ROLE_GROUNDSIDE_PAVEMENT
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
            if o.role == ROLE_GROUNDSIDE_PAVEMENT:
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
    # Groundside-clearance terminus exemption: a taxiway that runs to a
    # GROUNDSIDE ramp / vehicle area legitimately STOPS at the airside↔
    # groundside boundary, where ``_separate_groundside_from_airside`` clips
    # the groundside pavement back by GROUNDSIDE_CLEARANCE_M (1.0 m) from all
    # airside pavement.  The short edge therefore shares no corner — the gap
    # IS the connection (the same exemption check_vertex_on_flat_edge /
    # check_vertex_on_sloping_edge already make for groundside).  An end whose
    # BOTH corners sit within GROUNDSIDE_PROX_M of a groundside shape is such a
    # terminus, not a rect ending in mid-air.
    from .layout import ROLE_GROUNDSIDE_PAVEMENT
    GROUNDSIDE_PROX_M = 1.5            # clearance 1.0 m + weld/conformance drift
    _gs_polys = [s.polygon for s in layout.shapes
                 if s.role == ROLE_GROUNDSIDE_PAVEMENT
                 and s.polygon is not None and not s.polygon.is_empty]
    _gs_tree = None
    if _gs_polys:
        try:
            from shapely.strtree import STRtree
            _gs_tree = STRtree(_gs_polys)
        except Exception:
            _gs_tree = None

    def _abuts_groundside(px, py):
        if _gs_tree is None:
            return False
        pt = _P(px, py)
        try:
            for j in _gs_tree.query(pt.buffer(GROUNDSIDE_PROX_M)):
                if _gs_polys[j].distance(pt) <= GROUNDSIDE_PROX_M:
                    return True
        except Exception:
            return False
        return False

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
            if _abuts_groundside(ax, ay) and _abuts_groundside(bx, by):
                continue                       # airside→groundside terminus
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
    """The builder's APT.DAT taxi centerlines as
    ``[(latlon_pts, cL, cT, route_ordinal), …]`` — the within-shape grade
    test's CENTERLINE source (spine membership + per-letter cap), the SAME
    centerlines the build used.  ``route_ordinal`` indexes
    ``taxi_routes_ll(layout)`` (−1 = no route): the validator binds each axis
    to its route BY IDENTITY, exactly like ``grade_graph.build_context`` —
    the old nearest-route-by-midpoint re-derivation mis-bound axes near
    junctions and the two readers baked different anisotropic budgets for
    the same pair (SPJC: 91 apron chords at 1.7 % solver credit vs the
    validator's flat 1.5 %)."""
    def _cLcT(letter):
        return ((0.03, 0.02) if letter in ("A", "B") else (0.015, 0.015))

    from .config import SERVICE_ROAD_MAX_GRADE as _SVC_CAP
    # Route ordinals in taxi_routes_ll's exact iteration/dedup order.
    route_ord: dict = {}
    for tcl in (getattr(layout, "apt_taxi_centerlines", []) or []):
        if getattr(tcl, "is_service", False):
            continue
        rl = getattr(tcl, "route_line", None)
        if rl is None:
            rl = getattr(tcl, "line", None)
        if rl is None or getattr(rl, "is_empty", True):
            continue
        route_ord.setdefault(id(rl), len(route_ord))

    def _ridx(_cl):
        rl = getattr(_cl, "route_line", None)
        if rl is None:
            rl = getattr(_cl, "line", None)
        if rl is None:
            return -1
        return route_ord.get(id(rl), -1)

    axes = []
    for _cl in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln, name = _cl.line, _cl.name
        if ln is None or ln.is_empty:
            continue
        cs = list(ln.coords)
        # Service ROADS carry the road cap, not the taxi per-letter cap —
        # they were never 1.5 % taxiways (matches grade_graph.build_context's
        # road-spine caps under the global slice).  Routes exclude service
        # chains, so a road axis carries no route binding (isotropic).
        if getattr(_cl, "is_service", False):
            if len(cs) >= 2:
                axes.append(([layout.m_to_ll(x, y) for (x, y) in cs],
                             _SVC_CAP, _SVC_CAP, -1))
            continue
        sizes = list(getattr(_cl, "seg_sizes", []) or [])
        if not sizes or len(cs) < 2:
            cL, cT = _cLcT(_cl.dominant_size()
                           if hasattr(_cl, "dominant_size") else None)
            axes.append(([layout.m_to_ll(x, y) for (x, y) in cs], cL, cT,
                         _ridx(_cl)))
            continue
        # Split the route into PER-SIZE sub-axes (group consecutive same-size
        # segments) so each gets its own cL/cT — a route may change width.
        i = 0
        nseg = len(cs) - 1
        while i < nseg:
            sz = sizes[i] if i < len(sizes) else sizes[-1]
            j = i
            while (j + 1 < nseg
                   and (sizes[j + 1] if j + 1 < len(sizes) else sizes[-1]) == sz):
                j += 1
            cL, cT = _cLcT(sz)
            pts = [layout.m_to_ll(cs[k][0], cs[k][1]) for k in range(i, j + 2)]
            axes.append((pts, cL, cT, _ridx(_cl)))
            i = j + 1
    return axes


def taxi_axes_exact_ll(layout):
    """EXACT mirror of ``grade_graph.build_context``'s centerline construction,
    exported for the sidecar: the validator reconstructs the solver's
    ``Centerline`` objects verbatim, so the two law readers cannot diverge on
    spine geometry, per-segment caps, splitting, or route binding.

    Returns ``(axes, routes)``: ``axes`` = ``[(latlon_pts, seg_caps,
    route_ordinal), …]`` (UNSPLIT polylines — the per-size splitting the old
    ``taxi_axes_ll`` export did broke shared-centerline pair membership: a
    long chord whose endpoints projected onto different split pieces lost the
    anisotropic budget on the validator side only — SPJC's 91-pair class);
    ``routes`` = ``[latlon_pts, …]`` deduped by ``route_line`` identity in
    encounter order, INCLUDING service chains, exactly like build_context."""
    from .config import (CURVE_NATIVE_SPINE as _CNS, ROUTE_ARC_SPINE as _RAS,
                         SERVICE_ROAD_MAX_GRADE as _SVC_CAP,
                         taxi_grade_cap_for_letter)
    _svc_spines = _CNS or _RAS
    axes = []
    routes = []
    route_key_to_idx: dict = {}

    def _route_ordinal(tcl, ln, pts):
        rline = getattr(tcl, "route_line", None)
        rkey = id(rline) if rline is not None else ("self", id(ln))
        ridx = route_key_to_idx.get(rkey)
        if ridx is None:
            try:
                rpts = list(rline.coords) if rline is not None else pts
            except Exception:
                rpts = pts
            ridx = len(routes)
            routes.append([layout.m_to_ll(x, y) for (x, y) in rpts])
            route_key_to_idx[rkey] = ridx
        return ridx

    for tcl in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = getattr(tcl, "line", tcl)
        if ln is None or getattr(ln, "is_empty", True):
            continue
        _is_svc = getattr(tcl, "is_service", False)
        if _is_svc and not _svc_spines:
            continue
        try:
            pts = list(ln.coords)
        except Exception:
            continue
        if len(pts) < 2:
            continue
        if _is_svc:
            seg_caps = [_SVC_CAP] * (len(pts) - 1)
        else:
            sizes = list(getattr(tcl, "seg_sizes", []) or [])
            seg_caps = [
                taxi_grade_cap_for_letter(sizes[i]) if i < len(sizes)
                else taxi_grade_cap_for_letter(sizes[-1] if sizes else None)
                for i in range(len(pts) - 1)]
        ridx = _route_ordinal(tcl, ln, pts)
        axes.append(([layout.m_to_ll(x, y) for (x, y) in pts],
                     seg_caps, ridx))
    return axes, routes


def junction_mesh_edges_ll(layout):
    """The SOLVER's junction triangle-mesh EDGE set (the grade law's JUNCTION
    MESH RULE), as lat/lon endpoint pairs — the sidecar's ``mesh_edges`` key.

    Computed from the layout's IN-MEMORY rings, i.e. the same rings the last
    law-graph build (the solve / ``final_grade_projection``) triangulated —
    ``to_osm`` is a pure emitter and never mutates them.  The EMITTED ring can
    differ (emit repairs: buffer(0), needle-vertex removal, canonical-point
    interning), so a validator that triangulates the emitted ring gets a
    DIFFERENT Delaunay than the solver graded to — cm-scale false junction
    violations (SPJC 2026-07-05, 44 pairs a median 1.8 cm over allowance).
    The validator consumes this set 1:1 instead
    (``grade_graph.MeshEdgesExact``).  Edges are sorted for a byte-stable
    sidecar; empty when the junction-mesh gate is off."""
    from .config import JUNCTION_MESH_CONSTRAINTS
    from .grade_graph import JUNCTION_ROLES, _open_ring, mesh_edge_keys
    if not JUNCTION_MESH_CONSTRAINTS:
        return []
    edges_ll = []
    for shape in layout.shapes:
        if (shape.role not in JUNCTION_ROLES or shape.polygon is None
                or shape.polygon.is_empty):
            continue
        ring = _open_ring(list(shape.polygon.exterior.coords))
        if len(ring) < 3:
            continue
        index_pairs = sorted(
            (min(pair), max(pair))
            for pair in mesh_edge_keys(ring, list(range(len(ring))))
            if len(pair) == 2)
        for (index_a, index_b) in index_pairs:
            lat_a, lon_a = layout.m_to_ll(*ring[index_a])
            lat_b, lon_b = layout.m_to_ll(*ring[index_b])
            edges_ll.append([[round(lat_a, 7), round(lon_a, 7)],
                             [round(lat_b, 7), round(lon_b, 7)]])
    return edges_ll


def taxi_routes_ll(layout):
    """The WHOLE chained taxi routes (one per distinct ``route_line``) as lat/lon
    polylines, for the anisotropic-edge grade test: the standalone ``check_grade``
    decomposes a soft-shape pair against its route's spine ARC (Δs∥), so it must
    see the SAME continuous routes the solver's ``grade_graph.build_context`` does
    (``Centerline.route_idx`` → these).  Deduped by ``route_line`` identity; a
    piece with no parent route (synthetic / service-excluded handled by caller)
    falls back to its own ``line``."""
    seen = set()
    out = []
    for tcl in (getattr(layout, "apt_taxi_centerlines", []) or []):
        if getattr(tcl, "is_service", False):
            continue
        rl = getattr(tcl, "route_line", None)
        if rl is None:
            rl = getattr(tcl, "line", None)
        if rl is None or getattr(rl, "is_empty", True):
            continue
        key = id(rl)
        if key in seen:
            continue
        seen.add(key)
        try:
            out.append([layout.m_to_ll(x, y) for (x, y) in rl.coords])
        except Exception:
            continue
    return out


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
            routes_ll=taxi_routes_ll(layout), quiet=True,
            crown_drops_ll=[[la, lo, c] for (la, lo, c) in
                            (getattr(layout, "_crown_drop_ll", None)
                             or [])])




# ── Per-tile verify DEBUG log ────────────────────────────────────────
# EVERY verification finding is an auto-patch BUG to be tracked down and
# fixed, NOT something the user can correct in the source data (user ruling
# 2026-06-16).  So no finding is printed as [verify] chatter — they are all
# appended to the per-tile verify debug log instead.  The few that could in
# principle be a source-data issue (overlap = duplicate DSF overlay, source =
# non-pavement polygon tagged as pavement) are in practice still our geometry
# bugs at the airports we build, and the user does not want to chase them.

def _verify_debug_lines(layout, icao, taxi_index, gdesc, *,
                        overlaps, source, flat, edge_v, flat_v, axis_v,
                        short_e, cross, within, steps, rwy_grade) -> list:
    """Build the full per-category diagnostic lines for the verify debug
    log (no 5-item cap — this is for an engineer, not the console)."""
    def ds(idx):
        return describe_shape(layout, idx, taxi_index)

    out = []
    for area, ia, ib, loc in overlaps:
        out.append(f"  OVERLAP {area:.1f} m² @ {loc}: {ds(ia)} ∩ {ds(ib)}")
    for idx, area, frac, loc in source:
        out.append(f"  OFF-SOURCE {area:.0f} m² ({frac*100:.0f}% on source) "
                   f"@ {loc}: {ds(idx)}")
    for idx, detail, loc in flat:
        out.append(f"  TERMINAL-FLAT @ {loc}: {ds(idx)} {detail}")
    for idx, detail, loc in edge_v:
        out.append(f"  VERTEX-ON-EDGE @ {loc}: {ds(idx)} — {detail}")
    for idx, detail, loc in flat_v:
        out.append(f"  FLAT-EDGE @ {loc}: {ds(idx)} — {detail}")
    for idx, detail, loc in axis_v:
        out.append(f"  AXIS-TILT @ {loc}: {ds(idx)} — {detail}")
    for idx, detail, loc in short_e:
        out.append(f"  SHORT-EDGE @ {loc}: {ds(idx)} — {detail}")
    for v in sorted(cross, key=lambda v: -v.de_m):
        loc = f"{v.lat:.5f},{v.lon:.5f}" if v.lat is not None else "?,?"
        out.append(f"  CROSS-SHAPE {v.de_m:.2f} m @ {loc}: "
                   f"{gdesc(v.way_a)} ↔ {gdesc(v.way_b)}")
    for v in sorted(within, key=lambda v: -v.grade_pct):
        loc = f"{v.lat:.5f},{v.lon:.5f}" if v.lat is not None else "?,?"
        out.append(f"  WITHIN-SHAPE {v.grade_pct:.1f}% over {v.distance_m:.1f} "
                   f"m @ {loc}: {gdesc(v.way_a)}")
    for v in sorted(steps, key=lambda v: -v.step_m):
        loc = f"{v.lat:.5f},{v.lon:.5f}" if v.lat is not None else "?,?"
        out.append(f"  EDGE-STEP {v.step_m:.2f} m @ {loc}: "
                   f"{gdesc(v.way_v)} ↔ {gdesc(v.way_e)}")
    for kind, ref, val, cap, loc in rwy_grade:
        out.append(f"  RUNWAY-GRADE {val*100:.2f}% > {cap*100:.1f}% @ {loc}: "
                   f"runway {ref}")
    return out


def _write_verify_debug(path, icao, counts, lines) -> None:
    """Append a per-airport section (tally header + every finding) to the
    per-tile verify debug log at ``path``.  No-op when ``path`` is falsy or
    there is nothing to write; never raises."""
    if not path or not lines:
        return
    tally = " ".join(f"{k}={v}" for k, v in counts.items() if v)
    section = [f"=== {icao}: {tally} ==="] + lines
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(section) + "\n")
    except Exception:                                  # pragma: no cover
        pass


# ── Build-time entry point ──────────────────────────────────────────
def verify_and_log(layout, icao: str, debug_log_path: str | None = None) -> dict:
    """Run every verification check on a freshly-built layout and route the
    diagnostics to the per-tile verify DEBUG log (never raises).  Returns a
    counts dict.

    EVERY finding is an auto-patch bug to be tracked down — none is a
    user-fixable source-data problem (user ruling 2026-06-16) — so nothing is
    printed as [verify] chatter.  The full per-category detail is appended to
    ``debug_log_path``; the console gets only a one-line vprint(1) summary
    (suppressed at the build's LOG_VERBOSITY)."""
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
    # The OSM-patch grade validation (write the patch to a temp OSM and re-check
    # it with tools/check_grade) is DEBUG-ONLY: once the solver is proven there is
    # no reason to re-validate the shipped patch on every build — the grade test
    # (test_pavement_grade) still runs check_grade on the emitted patches in CI.
    # Enable with O4_VERIFY_OSM_GRADE=1 to log within/cross/step findings to the
    # verify debug log.  The cheap in-memory geometry checks above always run.
    if os.environ.get("O4_VERIFY_OSM_GRADE", "0") == "1":
        try:
            within, cross, steps = run_grade_checks(layout)
        except Exception as exc:                   # pragma: no cover
            UI.vprint(1, f"  [verify] {icao}: grade verification "
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
        UI.vprint(1, f"  [verify] {icao}: OK — no patch issues.")
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

    lines = _verify_debug_lines(
        layout, icao, taxi_index, _gdesc,
        overlaps=overlaps, source=source, flat=flat, edge_v=edge_v,
        flat_v=flat_v, axis_v=axis_v, short_e=short_e, cross=cross,
        within=within, steps=steps, rwy_grade=rwy_grade)
    _write_verify_debug(debug_log_path, icao, counts, lines)

    # User console: one summary line only (suppressed at build verbosity 0);
    # every finding is an auto-patch bug logged to the verify debug file.
    tally = " ".join(f"{k}={v}" for k, v in counts.items() if v)
    UI.vprint(1, f"  [verify] {icao}: {sum(counts.values())} patch issue(s) "
                 f"({tally}) — logged to the verify debug file.")
    return counts

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

from .geom_safe import min_rotated_rect

_TAXI_ROLES = ("primary_parallel", "secondary_parallel",
               "stub", "cross_connector")

# Roles that are NOT airside pavement built from apt.dat row-110 / DSF —
# excluded from the source-adjacency check.
_NON_SOURCE_PAVEMENT_ROLES = frozenset({
    "boundary", "taxiway_clearance", "runway_clearance",
    "retaining_wall", "tunnel_ramp", "groundside_pavement",
    "service_road", "service_junction", "building",
    # Adjacent-ground graded strips (adjacent_ground.py) are terrain
    # earthwork beside the pavement edge — off the apt.dat/DSF source
    # by construction, exactly like the boundary/clearance features.
    "graded_strip",
    # Object-bridge terrain plates (feature B, ruling R12): the trench
    # sits where the pack CUT its pavement (under the deck) and the
    # causeway spans the deliberate source gap behind the abutments —
    # off-source by construction.
    "bridge_trench",
    "bridge_causeway",
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


def _runway_principal_axis(pts):
    """``(ox, oy, ux, uy, length)`` of the runway CENTERLINE axis — the
    principal (largest-variance) direction of the vertex cloud ``pts``, with
    the origin at the low-projection end and ``length`` = the projected extent.
    ``None`` if fewer than 2 points or degenerate.

    This replaces the former longest-vertex-PAIR axis, which picked the
    corner-to-corner DIAGONAL of a runway rectangle — skewed ~1–2° off the true
    centerline (worse the wider/shorter the rect, and worse still on a
    tile-clipped ring whose oblique end-cap pulls the extreme pair off-axis).
    The principal axis is parallel to the runway centerline by construction (a
    long thin rectangle's dominant variance is along its length), so station
    fractions and end-zone tests are assigned from a true centerline-parallel
    frame."""
    import math
    n = len(pts)
    if n < 2:
        return None
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    sxx = syy = sxy = 0.0
    for x, y in pts:
        dx, dy = x - cx, y - cy
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    tr = sxx + syy
    det = sxx * syy - sxy * sxy
    disc = max(0.0, (0.5 * tr) ** 2 - det)
    lam = 0.5 * tr + math.sqrt(disc)              # largest eigenvalue
    if abs(sxy) > 1e-9:
        ux, uy = lam - syy, sxy
    else:
        ux, uy = (1.0, 0.0) if sxx >= syy else (0.0, 1.0)
    norm = math.hypot(ux, uy)
    if norm < 1e-12:
        return None
    ux, uy = ux / norm, uy / norm
    ts = [(x - cx) * ux + (y - cy) * uy for x, y in pts]
    t_lo = min(ts)
    length = max(ts) - t_lo
    if length <= 0:
        return None
    return (cx + t_lo * ux, cy + t_lo * uy, ux, uy, length)


def _runway_vertex_elevations(shape, n):
    """Per-vertex elevations for a runway ring's ``n`` open-ring corners, using
    the same accessor as ``_runway_rect_cross_ends`` /
    ``_runway_single_poly_cross_stations``: ``node_altitudes`` when present,
    else a flat ``altitude`` tag, else ``None`` (unknown → caller skips)."""
    if shape.node_altitudes and len(shape.node_altitudes) >= n:
        return [float(shape.node_altitudes[i]) for i in range(n)]
    if shape.altitude is not None:
        return [float(shape.altitude)] * n
    return None


def _longest_same_sign_run(signs, target, n):
    """Indices of the longest CYCLIC run of ``target`` in ``signs`` (length
    ``n``, treated as a closed ring).  ``[]`` if ``target`` is absent."""
    if all(s == target for s in signs):
        return list(range(n))
    start = None
    for k in range(n):
        if signs[k] == target and signs[(k - 1) % n] != target:
            start = k
            break
    if start is None:
        return []
    best: list = []
    cur: list = []
    for step in range(n):
        idx = (start + step) % n
        if signs[idx] == target:
            cur.append(idx)
        else:
            if len(cur) > len(best):
                best = cur
            cur = []
    if len(cur) > len(best):
        best = cur
    return best


def _runway_long_edge_chains(coords, elevations, axis, coalesce_m: float = 2.0):
    """Split a single-poly runway ring into its two LONG-EDGE chains and return
    ``[chain_plus, chain_minus]``, each a list of ``(station, elev, x, y)``
    sorted by ascending axis station; ``None`` if the ring does not split into
    two clean, runway-length rails (caller falls back to the legacy
    station-sample reconstruction).

    The longitudinal profile of a runway lives on its two long edges — the two
    monotone runs of ring vertices that hug either side of the centerline.  The
    END-CAP edges (the flat cross-ends, an OBLIQUE tile-clipped cap that can be
    LONGER than the runway is wide, and any densification vertices along those
    caps) are CROSS-runway boundary vertices, not longitudinal stations, and
    must be excluded — projecting them onto the axis and clustering by station
    (the former single-poly reconstruction) fabricates a phantom jog near an
    oblique clipped end.

    Rails are separated by LATERAL offset from the centerline axis: a
    long-edge vertex sits near ``±half_width`` (one side), while a cap vertex
    sweeps ACROSS the centerline, so its |offset| is small.  Vertices with
    |lateral − mid| ≥ ½·half_width on the positive / negative side form the two
    rails; near-duplicate consecutive vertices (corner doublings, T-weld /
    crossing-vertex clusters) within ``coalesce_m`` are collapsed so a
    quantization-level step across two coincident corners cannot fabricate a
    huge grade.  Validation requires each rail to span ≥ 50 % of the ring's own
    longitudinal extent (both rails run nearly the full piece length); a
    carved-out crossing PIECE validates on its own partial extent."""
    import math
    ox, oy, ux, uy, _length = axis
    vx, vy = -uy, ux
    n = len(coords)
    if n < 4 or len(elevations) < n:
        return None
    station = [(coords[i][0] - ox) * ux + (coords[i][1] - oy) * uy
               for i in range(n)]
    lateral = [(coords[i][0] - ox) * vx + (coords[i][1] - oy) * vy
               for i in range(n)]
    half_w = max(abs(l) for l in lateral)
    if half_w <= 0:
        return None
    mid = sum(lateral) / n
    thresh = 0.5 * half_w
    signs = [(+1 if (lateral[i] - mid) >= thresh
              else (-1 if (lateral[i] - mid) <= -thresh else 0))
             for i in range(n)]
    piece_extent = max(station) - min(station)
    if piece_extent <= 0:
        return None
    chains = []
    for target in (+1, -1):
        arc = _longest_same_sign_run(signs, target, n)
        if len(arc) < 2:
            return None
        kept = []
        for idx in arc:
            if kept:
                px, py = coords[kept[-1]]
                if math.hypot(coords[idx][0] - px,
                              coords[idx][1] - py) < coalesce_m:
                    continue
            kept.append(idx)
        if len(kept) < 2:
            return None
        chain = sorted(
            ((station[i], elevations[i], coords[i][0], coords[i][1])
             for i in kept), key=lambda t: t[0])
        if chain[-1][0] - chain[0][0] < 0.5 * piece_extent:
            return None
        chains.append(chain)
    return chains


def _runway_rect_cross_ends(s, coords, axis=None):
    """The two flat cross-end edges of a runway rect as ``(mid_x, mid_y,
    elev)`` tuples.  ``coords`` = the open-ring corners.  Corner elevations
    come from the shape's altitude tags (the solver orders a sloped rect ring
    ``[high, low, low, high]``); the two SHORT ring edges are the flat
    cross-ends.

    N-CORNER handling (Phase 0 hotfix, user 2026-07-07): the interior runway
    cross-edge crown inserts a CENTERLINE vertex at the midpoint of an interior
    cross-edge, so a crowned sub-rect has 5+ corners.  When ``axis`` (the
    runway ``(ox, oy, ux, uy, L)`` tuple) is given, reconstruct the cross-ends
    from the axis directly — cluster the corners at the two EXTREME axis
    stations and average each cluster's elevation — so a crowned rect's profile
    is reconstructed correctly (the centerline vertex sits at the same station
    as its cross-edge corners and averages in cleanly).  Without an axis the
    legacy 4-corner shortest-edge path runs (unchanged)."""
    import math
    n = len(coords)
    if s.node_altitudes and len(s.node_altitudes) >= n:
        ce = [float(s.node_altitudes[i]) for i in range(n)]
    elif s.altitude_high is not None and s.altitude_low is not None and n == 4:
        ah, al = float(s.altitude_high), float(s.altitude_low)
        ce = [ah, al, al, ah]
    elif s.altitude is not None:
        a = float(s.altitude)
        ce = [a] * n
    else:
        return []
    if axis is not None and n >= 4:
        ox, oy, ux, uy, _L = axis
        # station of each corner along the axis
        st = [((coords[i][0] - ox) * ux + (coords[i][1] - oy) * uy)
              for i in range(n)]
        s_lo, s_hi = min(st), max(st)
        tol = max(1.0, 0.05 * (s_hi - s_lo))   # station cluster tolerance
        out = []
        for target in (s_lo, s_hi):
            grp = [i for i in range(n) if abs(st[i] - target) <= tol]
            if not grp:
                continue
            # Cross-end profile sample = the EDGE elevation (the two edge
            # corners at this station, both at profile − crown_drop).  A
            # crowned rect also carries the inserted CENTERLINE vertex at this
            # station at profile level (higher) — exclude it (take the MIN) so
            # the reconstructed longitudinal profile is the EDGE profile,
            # exactly as for an uncrowned 4-corner rect (the crown drop is
            # uniform per ref, so the edge profile carries the true grades).
            e_end = min(ce[i] for i in grp)
            out.append((sum(coords[i][0] for i in grp) / len(grp),
                        sum(coords[i][1] for i in grp) / len(grp),
                        e_end))
        if len(out) == 2:
            return out
        # fall through to the legacy path on a degenerate cluster
    if n != 4:
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


def _runway_single_poly_cross_stations(shape, coords, axis):
    """Longitudinal-profile samples for a DE-SEGMENTED single-poly runway
    ring (``O4_RUNWAY_SINGLE_POLY``).

    A de-segmented runway emits as ONE polygon ring per ref whose profile
    stations live as interior LONG-EDGE vertices, not as separate sub-rect
    ends.  ``_runway_rect_cross_ends`` clusters a shape's corners at its two
    EXTREME axis stations (correct for a segmented sub-rect, where every
    segment boundary was a distinct rect end) — but on a single ring that
    sees ONLY the runway's two physical ends, so the whole interior profile
    goes dark.  Here we cluster the RING'S OWN vertices by axis station:
    project every vertex onto the ref axis, sort by station, group vertices
    within the same 5.0 m tolerance the downstream sample merge uses (so a
    station's two long-edge vertices — one per side — collapse to one
    sample), and emit ``(mean_x, mean_y, MIN elevation)`` per cluster.

    The MIN convention matches ``_runway_rect_cross_ends``: it takes the
    EDGE profile and excludes a crown-ridge / crossing-dome-lifted vertex
    that sits higher at the same station.  Per-vertex elevations are read
    from ``node_altitudes`` (same accessor as ``_runway_rect_cross_ends``);
    a fully-flat ring carrying a single ``altitude`` tag yields equal
    elevations → zero grades.
    """
    n = len(coords)
    if shape.node_altitudes and len(shape.node_altitudes) >= n:
        cluster_elevations = [float(shape.node_altitudes[i]) for i in range(n)]
    elif shape.altitude is not None:
        cluster_elevations = [float(shape.altitude)] * n
    else:
        return []
    ox, oy, ux, uy, _length = axis
    stations = [((coords[i][0] - ox) * ux + (coords[i][1] - oy) * uy)
                for i in range(n)]
    order = sorted(range(n), key=lambda i: stations[i])
    out = []
    group: list = []          # vertex indices in the current station cluster

    def _emit(group_indices):
        return (sum(coords[j][0] for j in group_indices) / len(group_indices),
                sum(coords[j][1] for j in group_indices) / len(group_indices),
                min(cluster_elevations[j] for j in group_indices))

    for i in order:
        if group and stations[i] - stations[group[-1]] > 5.0:
            out.append(_emit(group))
            group = []
        group.append(i)
    if group:
        out.append(_emit(group))
    return out


def check_runway_profile(layout, end_grade_cap="default",
                         check_curvature: bool = True, noise_m: float = 0.10):
    """Invariant: the EMITTED runway longitudinal profile must obey the
    FAA/EASA grade caps AND the vertical-curve rate-of-grade-change limit — the
    elevation solver (or a runway-flex MOVE) must never pull a runway out of
    compliance.

    Reconstruct each runway's longitudinal profile and check, per consecutive
    segment, the caps below.  A de-segmented single-poly ring is measured
    EDGE-AWARE: it is split into its two LONG-EDGE chains (the monotone runs of
    ring vertices hugging either side of the centerline) and each rail is
    checked independently with true 2D horizontal segment distances; the
    end-cap edges — the flat cross-ends, an OBLIQUE tile-clipped cap that can be
    longer than the runway is wide, and any densification vertices on those caps
    — are excluded, so they cannot fabricate a phantom jog (the earlier
    per-station cluster mixed both rails and jogged near an oblique clipped
    end).  A legacy segmented runway (one flat cross-end sample per sub-rect,
    ordered along the axis) and any ring whose chain split fails validation keep
    the station-sample reconstruction.  Checks:

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
        # ≥4 corners: a plain sub-rect (4) or a crowned sub-rect (5+ — the
        # interior cross-edge crown inserted a centerline vertex).  The
        # axis-clustered cross-end reconstruction below handles both; a
        # clipped/irregular <4 rect has no clean cross-ends → skip.
        if len(cs) < 4:
            continue
        by_ref.setdefault(s.ref or "", []).append((s, cs))

    out = []
    for ref, items in by_ref.items():
        ax = _runway_principal_axis([p for _s, cs in items for p in cs])
        if ax is None:
            continue
        ox, oy, ux, uy, L = ax
        if L <= 0:
            continue
        # Crossing-influence exclusion (de-segmented single-poly rings only —
        # a gate-off runway carries no ``from_single_poly`` shape, so
        # ``crossing_zones`` stays empty and the legacy path below is
        # byte-identical).  A single-poly ring is CARVED at each runway-runway
        # crossing: the crossing slab is removed from the ring and emitted as a
        # separate ``runway_crossing`` shape (not role=="runway", so it is
        # invisible to this check).  That leaves a GAP in the sample chain; the
        # segment spanning the gap — and the carve-edge segment on either side,
        # where the crossing crown DOME perturbs the MIN-per-station edge value
        # — reconstruct a phantom grade/curvature kink the ground-truth (fully
        # segmented) profile does not carry (measured at CYXY: gate-off 0
        # findings, gate-on 2 phantom curvature findings on ``14R/32L`` at its
        # ``02/20+14R/32L`` crossing).  The crossing's OWN longitudinal profile
        # is governed by the crossing junction, not this runway check, so
        # exclude any profile segment whose station interval touches a crossing
        # slab (expanded by the 5 m merge tolerance for float robustness).  Do
        # NOT widen ``noise_m`` for this — the wobble is localized to the
        # crossing, not a global quantization effect.
        crossing_zones: list = []
        if any(getattr(s, "from_single_poly", False) for s, _ in items):
            ref_tokens = set((ref or "").split("+"))
            for xs in layout.shapes:
                if (xs.role or "") != "runway_crossing":
                    continue
                if not (set((xs.ref or "").split("+")) & ref_tokens):
                    continue
                if xs.polygon is None or xs.polygon.is_empty:
                    continue
                xstations = [((x - ox) * ux + (y - oy) * uy)
                             for x, y in xs.polygon.exterior.coords]
                if xstations:
                    crossing_zones.append(
                        (min(xstations) - 5.0, max(xstations) + 5.0))

        def _touches_crossing(station_lo, station_hi):
            return any(station_lo <= hi and station_hi >= lo
                       for lo, hi in crossing_zones)

        # Partition the ref's shapes.  A de-segmented single-poly ring that
        # splits cleanly into its two long-edge chains is measured EDGE-AWARE:
        # the longitudinal grade is checked ALONG each rail independently with
        # true 2D horizontal distances (exactly the measurement that proves a
        # tile-clipped runway compliant), with the end-cap edges — including an
        # OBLIQUE clipped cap and its densification vertices — excluded so they
        # cannot fabricate a phantom jog.  Everything else — a legacy segmented
        # sub-rect (gate-off), or a single-poly ring whose chain split fails
        # validation — falls through to the unchanged station-sample
        # reconstruction.
        edge_aware = []           # (shape, [chain_plus, chain_minus])
        legacy_items = []         # (shape, coords)
        for s, cs in items:
            chains = None
            if getattr(s, "from_single_poly", False):
                elevs = _runway_vertex_elevations(s, len(cs))
                if elevs is not None:
                    chains = _runway_long_edge_chains(cs, elevs, ax)
            if chains is not None:
                edge_aware.append((s, chains))
            else:
                legacy_items.append((s, cs))

        # --- edge-aware long-edge rails ---------------------------------
        for _s, chains in edge_aware:
            for chain in chains:      # (station, elev, x, y), station-sorted
                chain_grades = []     # (g, seg_len, mid_x, mid_y, at_crossing)
                for i in range(len(chain) - 1):
                    d0, e0, x0, y0 = chain[i]
                    d1, e1, x1, y1 = chain[i + 1]
                    seg = math.hypot(x1 - x0, y1 - y0)   # true 2D horiz. run
                    if seg < 0.5:
                        continue
                    at_crossing = _touches_crossing(min(d0, d1), max(d0, d1))
                    fi, fj = d0 / L, d1 / L
                    in_end = (min(fi, fj) < RUNWAY_END_FRACTION
                              or max(fi, fj) > 1.0 - RUNWAY_END_FRACTION)
                    cap = (end_grade_cap
                           if (in_end and end_grade_cap is not None)
                           else RUNWAY_MAX_GRADE)
                    if (not at_crossing) and abs(e1 - e0) - cap * seg > noise_m:
                        out.append(("grade", ref, abs(e1 - e0) / seg, cap,
                                    _ll(layout, 0.5 * (x0 + x1),
                                        0.5 * (y0 + y1))))
                    chain_grades.append(((e1 - e0) / seg, seg,
                                         0.5 * (x0 + x1), 0.5 * (y0 + y1),
                                         at_crossing))
                if not check_curvature:
                    continue
                for i in range(len(chain_grades) - 1):
                    gl, Ll, _xl, _yl, xl_cross = chain_grades[i]
                    gr, Lr, mx, my, xr_cross = chain_grades[i + 1]
                    if xl_cross or xr_cross:
                        continue
                    max_dg = RUNWAY_MAX_GRADE_CHANGE_PER_M * 0.5 * (Ll + Lr)
                    noise_dg = noise_m * (1.0 / Ll + 1.0 / Lr)
                    if abs(gr - gl) - max_dg > noise_dg:
                        out.append(("curvature", ref, abs(gr - gl), max_dg,
                                    _ll(layout, mx, my)))

        # --- legacy station-sample reconstruction (unchanged) -----------
        if not legacy_items:
            continue
        samples = []              # (dist_along_axis, elev, x, y)
        for s, cs in legacy_items:
            # A single-poly ring that failed the chain split still uses the
            # per-station cluster; segmented sub-rects use the cross-end path.
            if getattr(s, "from_single_poly", False):
                station_samples = _runway_single_poly_cross_stations(s, cs, ax)
            else:
                station_samples = _runway_rect_cross_ends(s, cs, axis=ax)
            for (mx, my, e) in station_samples:
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
        grades = []               # (g, seg_len, mid_x, mid_y, at_crossing)
        for i in range(len(merged) - 1):
            d0, e0, x0, y0, _ = merged[i]
            d1, e1, x1, y1, _ = merged[i + 1]
            seg = d1 - d0
            if seg < 0.5:
                continue
            at_crossing = _touches_crossing(d0, d1)
            fi, fj = d0 / L, d1 / L
            in_end = (min(fi, fj) < RUNWAY_END_FRACTION
                      or max(fi, fj) > 1.0 - RUNWAY_END_FRACTION)
            cap = (end_grade_cap if (in_end and end_grade_cap is not None)
                   else RUNWAY_MAX_GRADE)
            if (not at_crossing) and abs(e1 - e0) - cap * seg > noise_m:
                out.append(("grade", ref, abs(e1 - e0) / seg, cap,
                            _ll(layout, 0.5 * (x0 + x1), 0.5 * (y0 + y1))))
            grades.append(((e1 - e0) / seg, seg,
                           0.5 * (x0 + x1), 0.5 * (y0 + y1), at_crossing))
        if not check_curvature:
            continue
        for i in range(len(grades) - 1):
            gl, Ll, _xl, _yl, xl_cross = grades[i]
            gr, Lr, mx, my, xr_cross = grades[i + 1]
            # A grade change straddling a carved crossing (either segment
            # touches the crossing slab) is a crown-dome artifact, not a real
            # runway kink — the crossing junction owns that transition.  Skip.
            if xl_cross or xr_cross:
                continue
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
        runway_end_constrained_length_m,
        runway_end_governed_length_beyond_pavement_m,
        runway_end_governed_length_m,
        runway_end_skirt_floor_profile,
        runway_end_skirt_floor_profile_beyond_pavement)
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
        # Governed footprint anchored at the RUNWAY END, overrun
        # pavement inside it — IDENTICAL to the emitter (the pavement
        # past the end consumes the first ``pavement_beyond_end`` metres
        # and the floor arrives at the exit already that far into its
        # descent).
        pavement_beyond_end = max(0.0, start - CL._RESA_SEED_INSET_M)
        governed = runway_end_governed_length_beyond_pavement_m(
            runway_end_governed_length_m(full_len, approach_class),
            pavement_beyond_end)
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
            depths = runway_end_skirt_floor_profile_beyond_pavement(
                distances, entry_grade, pavement_beyond_end)
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


def check_adjacent_ground(layout, dem, tile_lat, tile_lon,
                          source_runways=None,
                          tolerance_m: float = 1.5,
                          step_m: float = 5.0):
    """Invariant (adjacent-ground LATERAL grade law): the RENDERED surface
    beside every terrain-facing airside pavement edge must stay inside the
    lawful corridor ``grade_law.adjacent_ground_envelope`` — the LATERAL
    generalization of the runway-end skirt reader, in lockstep with the
    ``adjacent_ground`` emitter (both consume the ONE law function).

    Marches perpendicular transects outward from every terrain-facing
    airside edge (runway / taxiway family / apron — the SAME scope, edge
    selection, END-edge skip and terrain-facing probe the emitter uses),
    and at each outward distance ``d`` reads the corridor
    ``(floor_offset, ceiling_offset)`` relative to the local pavement-edge
    altitude.  A station's rendered surface is:

      * EXEMPT when it is covered by the emitter's own static clip — any
        shape (INCLUDING the emitted ``graded_strip`` bands, so a lawfully
        graded column is exempt) buffered by the pavement gap — or lies
        outside the airport boundary.  This is byte-for-byte the emitter's
        clip (``adjacent_ground`` builds bands by differencing the same
        ``static_union.buffer(_PAVEMENT_GAP_M)`` and intersecting the
        boundary), so a covered / clamped column is never flagged.
      * otherwise OPEN GROUND rendered by the smoothed DEM.  It violates
        the law when the DEM sits BELOW a finite floor (zones 1-2 — a fill
        band was owed but not emitted; zone-3's ``None`` floor makes a
        cliff LAWFUL, never flagged — the boundary-bridge killer) or ABOVE
        a finite ceiling (a cut band was owed but not emitted) by more
        than ``tolerance_m`` — AND lies WITHIN the daylight-supported depth.

    DAYLIGHT slope-limit lockstep (``grade_law.adjacent_ground_supported_depths``,
    user 2026-07-09): the emitter marches each station outward independently,
    then CLAMPS the per-station daylight depths so the daylight line benches
    along the frontage (an isolated deep ray no neighbour corroborates is
    clamped to a shallow bench, not a knife-slot blade — CYXY 417).  A column
    BEYOND that supported depth is therefore not the emitter's to grade, so
    the validator EXEMPTS it (flagging it would mint findings against the
    emitter's own lawful clamp).  This reader reproduces the emitter's raw
    outward scan per station (breach beyond the emitter's
    ``CLEARANCE_OBSTRUCTION_THRESHOLD_M`` trigger, one step out), applies the
    ONE law over the SAME station sequence, and flags only columns within the
    supported depth.

    ``tolerance_m`` (1.5 m) sits ABOVE the emitter's 1 m fill/cut trigger
    (``CLEARANCE_OBSTRUCTION_THRESHOLD_M``) so terrain the emitter
    deliberately left ungraded (deviation ≤ 1 m) is never flagged, plus
    emit rounding (0.1 m) and DEM interpolation.  ``step_m`` follows the
    emitter's / skirt's 5 m station convention.

    Pure reporter (verification-architecture ruling): returns
    ``[("should_fill"|"should_cut", "<ref>", metres_outside_corridor,
    tolerance_m, "lat,lon"), …]`` worst-first, ONE worst station per shape
    per kind; empty when ``dem`` is None or there is no airside pavement.
    With the ``O4_ADJACENT_GROUND_LAW`` gate OFF (no bands emitted) this
    reports the columns the law WOULD govern — the pre-flip baseline, like
    the skirt reader's off-gate behaviour.
    """
    import math
    from shapely.geometry import box, LineString, Point
    from shapely.prepared import prep
    from shapely.strtree import STRtree
    from . import clearance as CL
    from .config import (
        CLEARANCE_MAX_REACH_M, CLEARANCE_OBSTRUCTION_THRESHOLD_M,
        runway_code_number)
    from .grade_law import (
        adjacent_ground_envelope, adjacent_ground_supported_depths,
        _ADJACENT_RUNWAY_ROLES, _ADJACENT_TAXIWAY_ROLES)
    from .adjacent_ground import airside_seam_vertex_keys
    from .emit_decimate import _key as _vertex_key
    from .layout import R_EARTH, taxi_shape_code_letter
    from .pavement.runways import _sample_runway_segment_elev
    from .elevation import _sample_dem

    if dem is None:
        return []
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    _deg = math.degrees
    _rad = math.radians

    def _ll_to_m(lat, lon):
        return (_rad(lon - lon0) * R_EARTH * cos0,
                _rad(lat - lat0) * R_EARTH)

    def _sample(x, y):
        try:
            lat = lat0 + _deg(y / R_EARTH)
            lon = lon0 + _deg(x / (R_EARTH * cos0))
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except (ValueError, ArithmeticError):
            return None

    # Scope = the SAME airside pavement roles the emitter marches
    # (``clearance._AIRSIDE_PAVEMENT_ROLES`` IS the emitter's in_scope set:
    # runway/runway_crossing + taxiway family + apron).
    scoped = [s for s in layout.shapes
              if s.role in CL._AIRSIDE_PAVEMENT_ROLES
              and s.polygon is not None and not s.polygon.is_empty
              and s.polygon.geom_type == "Polygon"]
    if not scoped:
        return []

    # CONTINUATION-SEAM keys (user 2026-07-10, cross-shape run-end taper):
    # vertices shared between two airside pavement shapes.  Computed exactly as
    # the emitter (over the SAME airside roles, graded_strip bands excluded) so
    # the daylight bench-in is suppressed at the identical terminal stations in
    # both readers — lockstep (the emitter reads the SAME O4_SEAM_TAPER_PIN).
    import os as _os
    seam_keys = (airside_seam_vertex_keys(layout)
                 if _os.environ.get("O4_SEAM_TAPER_PIN", "1") != "0"
                 else set())

    # Coverage exemption, indexed for point queries: a station is exempt
    # when it sits within ``_PAVEMENT_GAP_M`` of ANY shape (incl. an
    # emitted band) or outside the boundary.  The emitter clips EXACTLY
    # since the 2026-07-09 weld ruling (no standoff), so the 1 m here is
    # pure READ-side slack around shape edges — the march samples at 5 m
    # stations, so it never decides more than a band-edge-adjacent
    # column.  An STRtree over the INDIVIDUAL polygons + a per-candidate
    # distance test is ~orders faster than ``.covers`` on a single
    # buffered union at a plateau airport where nearly every marched
    # point is out-of-corridor.
    static_geoms = [s.polygon for s in layout.shapes
                    if s.polygon is not None and not s.polygon.is_empty]
    if not static_geoms:
        return []
    try:
        tree = STRtree(static_geoms)
    except CL._GEOM_EXC:
        return []
    gap = CL._PAVEMENT_GAP_M

    def _covered(px, py):
        """True when ``(px, py)`` is within the pavement gap of any shape —
        the emitter's static clip (``static_union.buffer(gap)``).  Queried
        against a small box around the point (gap-inflated) so a shape whose
        bbox does not literally contain the point but lies within the gap is
        still a candidate."""
        p = Point(px, py)
        try:
            cand = tree.query(box(px - gap, py - gap, px + gap, py + gap))
        except CL._GEOM_EXC:
            return False
        for gi in cand:
            if static_geoms[gi].distance(p) <= gap:
                return True
        return False

    def _inside_shape(px, py):
        """True when ``(px, py)`` lies inside any shape — the emitter's
        terrain-facing probe (``prep_static.contains``)."""
        p = Point(px, py)
        try:
            cand = tree.query(p)
        except CL._GEOM_EXC:
            return False
        for gi in cand:
            if static_geoms[gi].covers(p):
                return True
        return False

    boundary = layout.airport_boundary
    prep_boundary = None
    if boundary is not None and not boundary.is_empty:
        try:
            prep_boundary = prep(boundary)
        except CL._GEOM_EXC:
            prep_boundary = None

    # Runway axes for code-number keying + END-edge skipping, IDENTICAL to
    # the emitter: from ``source_runways`` when available, else the runway
    # shapes' own long extent as the standalone fallback.
    rw_axes = []
    if source_runways:
        for r in source_runways:
            try:
                rax, ray = _ll_to_m(r.lat_a, r.lon_a)
                rbx, rby = _ll_to_m(r.lat_b, r.lon_b)
            except CL._GEOM_EXC:
                continue
            rlen = math.hypot(rbx - rax, rby - ray)
            if rlen < 1.0:
                continue
            rw_axes.append((LineString([(rax, ray), (rbx, rby)]),
                            ((rbx - rax) / rlen, (rby - ray) / rlen), rlen))

    def _long_edge_length_and_unit(poly):
        """``(long_side_length_m, (ux, uy))`` of the polygon's minimum
        rotated rectangle — the runway shape's own length + axis, the
        ``source_runways=None`` fallback for code-number keying and
        END-edge skipping.  ``(0.0, None)`` when degenerate (never raises;
        ``runway_code_number(0.0)`` keys the smallest code)."""
        try:
            xs = list(min_rotated_rect(poly).exterior.coords)
        except CL._GEOM_EXC:
            return (0.0, None)
        best = None
        for i in range(len(xs) - 1):
            dx = xs[i + 1][0] - xs[i][0]
            dy = xs[i + 1][1] - xs[i][1]
            length = math.hypot(dx, dy)
            if length > 0.0 and (best is None or length > best[0]):
                best = (length, dx / length, dy / length)
        return (0.0, None) if best is None else (best[0], (best[1], best[2]))

    def _params(s):
        """(env_role, code_number, code_letter, reach, axis) for one shape;
        mirrors ``adjacent_ground._family_params``.  Where the emitter
        SKIPS runway shapes without ``rw_axes`` (the pipeline always
        threads apt.runways), the validator must still read them —
        production ``verify_and_log`` has no runway rows — so it falls
        back to the runway shape's OWN geometry (minimum-rotated-rectangle
        long side = length for the code number, its direction for the
        END-edge skip)."""
        role = s.role
        if role in _ADJACENT_RUNWAY_ROLES:
            code_number = None
            axis = None
            if rw_axes:
                try:
                    cen = s.polygon.centroid
                    ax = min(rw_axes, key=lambda a: a[0].distance(cen))
                    code_number = runway_code_number(ax[2])
                    axis = ax[1]
                except (CL._GEOM_EXC + (ValueError,)):
                    code_number = None
            if code_number is None:
                length_m, axis = _long_edge_length_and_unit(s.polygon)
                code_number = runway_code_number(length_m)
            return (role, code_number, None,
                    CLEARANCE_MAX_REACH_M["runway"], axis)
        if role in _ADJACENT_TAXIWAY_ROLES:
            return (role, None, taxi_shape_code_letter(layout, s),
                    CLEARANCE_MAX_REACH_M["taxiway"], None)
        # apron family
        return (role, None, None, CLEARANCE_MAX_REACH_M["taxiway"], None)

    out = []
    for s in scoped:
        env_role, code_number, code_letter, reach, axis = _params(s)
        try:
            coords = list(s.polygon.exterior.coords)
            ccw = bool(s.polygon.exterior.is_ccw)
        except CL._GEOM_EXC:
            continue
        if len(coords) < 4:
            continue
        na = s.node_altitudes
        if na:
            nm = min(len(na), len(coords))
            ring_alts = [None if na[i] is None else float(na[i])
                         for i in range(nm)]
            ring_alts += [None] * (len(coords) - nm)
        elif s.altitude is not None:
            ring_alts = [float(s.altitude)] * len(coords)
        else:
            ring_alts = [_sample_runway_segment_elev(s, x, y)
                         for x, y in coords]

        n_out = max(1, int(math.ceil(reach / step_m)))
        # Emitter obstruction TRIGGER for this family (the raw-scan gate, NOT
        # the flagging tolerance): runway strips use the runway threshold, the
        # taxiway + apron families the taxiway one — exactly
        # ``adjacent_ground``'s ``trigger_by_family``.
        trigger = (CLEARANCE_OBSTRUCTION_THRESHOLD_M["runway"]
                   if env_role in _ADJACENT_RUNWAY_ROLES
                   else CLEARANCE_OBSTRUCTION_THRESHOLD_M["taxiway"])

        # STATION SEQUENCE — built exactly as the emitter's non-fan station
        # generation so the daylight slope-limit law couples the SAME stations
        # in both readers.  Every (edge, k) sample is a station; ``flag`` marks
        # the ones the emitter would actually scan/grade (terrain-facing,
        # non-END, both edge alts known).  END-edge and inside-facing stations
        # stay in the sequence as depth-0 COUPLING nodes (the emitter carries
        # them too, with a None edge reference) but are never flagged —
        # dropping them would loosen the slope clamp at run boundaries near a
        # runway end and flag columns the emitter's own clamp already benched
        # away (lockstep).  Corner FANS are omitted: they share the corner
        # coordinate (dist 0), so they never change a non-fan station's
        # supported depth (see grade_law).
        st_x, st_y, st_outn, st_ref, st_flag = [], [], [], [], []
        st_seam = []
        for i in range(len(coords) - 1):
            eax, eay = coords[i]
            ebx, eby = coords[i + 1]
            u = CL._unit(ebx - eax, eby - eay)
            if u is None:
                continue
            outn = (u[1], -u[0]) if ccw else (-u[1], u[0])
            a0 = ring_alts[i]
            a1 = ring_alts[i + 1]
            # Runway END edges: the runway-end skirt law owns terrain beyond
            # an end (its own governed length + lawful beyond-drop).
            end_edge = (axis is not None
                        and abs(outn[0] * axis[0] + outn[1] * axis[1])
                        > CL._RING_END_NORMAL_DOT)
            seglen = math.hypot(ebx - eax, eby - eay)
            nseg = max(1, int(math.ceil(seglen / step_m)))
            # Continuation-seam flags for this edge's stations — the SAME rule
            # the emitter uses (k == 0 on ``coords[i]`` / k == nseg-1 before
            # ``coords[i+1]``); mirrored so the daylight pins land identically.
            edge_a_seam = _vertex_key(eax, eay) in seam_keys
            edge_b_seam = _vertex_key(ebx, eby) in seam_keys
            for k in range(nseg):
                t = k / nseg
                sx = eax + (ebx - eax) * t
                sy = eay + (eby - eay) * t
                ref = None
                flag = False
                # Terrain-facing, non-END, alts known → a scanned/flagged
                # station; otherwise a depth-0 coupling node (the emitter's
                # own probe: an outward point covered by a shape owns its band).
                if (not end_edge and a0 is not None and a1 is not None
                        and not _inside_shape(
                            sx + outn[0] * CL._RING_PROBE_M,
                            sy + outn[1] * CL._RING_PROBE_M)):
                    ref = a0 + t * (a1 - a0)
                    flag = True
                st_x.append(sx)
                st_y.append(sy)
                st_outn.append(outn)
                st_ref.append(ref)
                st_flag.append(flag)
                st_seam.append((k == 0 and edge_a_seam)
                               or (k == nseg - 1 and edge_b_seam))

        # RAW OUTWARD SCAN — the emitter's ``outer[i]``: the furthest distance
        # the DEM breaches the corridor by more than the emitter TRIGGER, one
        # step out, capped at the reach (fill breaches exist only inside the
        # graded width, where the floor is finite).  One DEM march per station,
        # cached so the tolerance flagging below re-samples nothing.
        n_st = len(st_x)
        fill_raw = [0.0] * n_st
        cut_raw = [0.0] * n_st
        marched = [None] * n_st        # station → [(d, offset, fo, co, qx, qy)]
        for idx in range(n_st):
            if not st_flag[idx]:
                continue
            sx, sy, outn, ref = (st_x[idx], st_y[idx],
                                 st_outn[idx], st_ref[idx])
            samples = []
            last_fill = last_cut = 0.0
            for j in range(1, n_out + 1):
                d = min(reach - 1e-3, j * step_m)
                floor_off, ceil_off = adjacent_ground_envelope(
                    env_role, code_number, code_letter, d)
                if floor_off is None and ceil_off is None:
                    break              # at/beyond the reach — ungoverned
                qx = sx + outn[0] * d
                qy = sy + outn[1] * d
                dd = _sample(qx, qy)
                if dd is None:
                    continue
                offset = float(dd) - ref
                samples.append((d, offset, floor_off, ceil_off, qx, qy))
                if floor_off is not None and offset < floor_off - trigger:
                    last_fill = d
                if ceil_off is not None and offset > ceil_off + trigger:
                    last_cut = d
            marched[idx] = samples
            if last_fill > 0.0:
                fill_raw[idx] = min(reach - 1e-3, last_fill + step_m)
            if last_cut > 0.0:
                cut_raw[idx] = min(reach - 1e-3, last_cut + step_m)

        # DAYLIGHT slope-limit (the ONE law, in lockstep with the emitter's
        # ``_build_*_bands`` clamp): a column BEYOND the supported depth is not
        # the emitter's to grade, so it is EXEMPT; columns within it keep the
        # tolerance flagging.
        positions = list(zip(st_x, st_y))
        supported_fill = adjacent_ground_supported_depths(
            fill_raw, positions, st_seam)
        supported_cut = adjacent_ground_supported_depths(
            cut_raw, positions, st_seam)

        worst = {}                     # kind -> (magnitude, x, y)
        for idx in range(n_st):
            if not st_flag[idx] or marched[idx] is None:
                continue
            sf = supported_fill[idx]
            sc = supported_cut[idx]
            worst_below = None
            worst_above = None
            for d, offset, floor_off, ceil_off, qx, qy in marched[idx]:
                if (floor_off is not None and d <= sf
                        and offset < floor_off - tolerance_m):
                    mag = floor_off - offset          # metres below floor
                    if worst_below is None or mag > worst_below[0]:
                        worst_below = (mag, qx, qy)
                if (ceil_off is not None and d <= sc
                        and offset > ceil_off + tolerance_m):
                    mag = offset - ceil_off            # metres above ceiling
                    if worst_above is None or mag > worst_above[0]:
                        worst_above = (mag, qx, qy)
            for kind, cand in (("should_fill", worst_below),
                               ("should_cut", worst_above)):
                if cand is None:
                    continue
                mag, qx, qy = cand
                # Out of corridor — is this column the emitter's to grade?
                # Covered (incl. by an emitted band) or outside the boundary
                # ⇒ nothing to flag (the emitter's clip).
                if _covered(qx, qy):
                    continue
                if (prep_boundary is not None
                        and not prep_boundary.covers(Point(qx, qy))):
                    continue
                cur = worst.get(kind)
                if cur is None or mag > cur[0]:
                    worst[kind] = (mag, qx, qy)
        ident = (s.ref or "").strip() or s.role
        for kind, (mag, qx, qy) in worst.items():
            out.append((kind, ident, mag, tolerance_m,
                        _ll(layout, qx, qy)))
    out.sort(key=lambda r: -r[2])
    return out


def _shape_vertex_altitudes(shape, vertex_count):
    """Per-vertex solved altitudes for a shape's open ring, or ``None``
    when the shape carries no elevation representation yet.  Mirrors the
    solver's own seeding priority: ``node_altitudes`` (per-vertex) →
    flat ``altitude`` → 4-corner ``altitude_high/low`` ([H, L, L, H])."""
    if shape.node_altitudes:
        values = [float(a) for a in shape.node_altitudes[:vertex_count]]
        if len(values) < vertex_count:
            values += [values[-1]] * (vertex_count - len(values))
        return values
    if shape.altitude is not None:
        return [float(shape.altitude)] * vertex_count
    if (shape.altitude_high is not None
            and shape.altitude_low is not None and vertex_count == 4):
        return [float(shape.altitude_high), float(shape.altitude_low),
                float(shape.altitude_low), float(shape.altitude_high)]
    return None


def check_bridge_deck_end_pins(layout, dem, tile_lat, tile_lon,
                               tolerance_m: float = 0.25):
    """Object-bridge deck-end pin law reader (feature B stage 2,
    lockstep with ``bridges.insert_bridge_deck_end_pins``): every
    pavement ring vertex on a DECK_CARRIED (or cosmetic) bridge's
    abutment line must sit at ``grade_law.bridge_deck_end_pin_elevation_
    m`` — the same function the writer used.  The acceptance number is
    the spec's ±0.25 m (KBNA: abutment terrain = 167.0 ± 0.25).

    Pure reporter.  Returns ``[("deck_end_pin", ref, deviation_m,
    tolerance_m, "lat,lon"), …]`` worst-first; empty when the gate is
    off or nothing is classified."""
    from .config import OBJECT_BRIDGE_TERRAIN
    if not OBJECT_BRIDGE_TERRAIN:
        return []
    from shapely.geometry import Point as _Point
    from .bridges import (
        _BRIDGE_PIN_ON_LINE_TOLERANCE_M,
        _BRIDGE_PIN_ROLES,
        _abutment_lines_layout_meters,
        _bridge_datum_elevation_m,
        _object_bridge_classification,
        _partition_bridges_for_corridors,
    )
    from .grade_law import bridge_deck_end_pin_elevation_m
    classification = _object_bridge_classification(layout)
    if classification is None:
        return []
    from .config import BRIDGE_ABUTMENT_PIN_CAPTURE_BAND_M
    corridor_bridges, _suppress, _refused, _road_carried, _portals = (
        _partition_bridges_for_corridors(classification, layout)
    )
    out = []
    for bridge in corridor_bridges:
        datum = _bridge_datum_elevation_m(bridge, dem, tile_lat, tile_lon)
        if datum is None:
            continue
        abutment_lines = _abutment_lines_layout_meters(bridge, layout)
        reference = ",".join(bridge.object_resources)
        for end_index, line in enumerate(abutment_lines):
            end_y = (
                bridge.deck_end_elevations_y_m[end_index]
                if end_index < len(bridge.deck_end_elevations_y_m)
                else bridge.deck_top_y_m
            )
            law_value = bridge_deck_end_pin_elevation_m(datum, end_y)
            for shape in layout.shapes:
                is_causeway = (
                    getattr(shape, "ref", "") == "object_bridge_causeway"
                )
                if shape.role not in _BRIDGE_PIN_ROLES and not is_causeway:
                    continue
                if shape.polygon is None or shape.polygon.is_empty:
                    continue
                if is_causeway:
                    # Causeway plate sub-check (stage 2b): a plate at
                    # this end must carry the SAME law value it was
                    # emitted from (flat at the deck-end elevation,
                    # amendment A10) — same law import, lockstep.
                    try:
                        near_end = shape.polygon.distance(
                            line.interpolate(0.5, normalized=True)
                        ) <= BRIDGE_ABUTMENT_PIN_CAPTURE_BAND_M
                    except Exception:
                        continue
                    if not near_end:
                        continue
                    # R12 causeway shapes are born with per-vertex
                    # node_altitudes (flat by law); pre-R12 plates
                    # carried a flat ``altitude`` tag — read either.
                    if shape.altitude is not None:
                        plate_value = float(shape.altitude)
                    elif shape.node_altitudes:
                        plate_value = float(shape.node_altitudes[0])
                    else:
                        continue
                    deviation = abs(plate_value - law_value)
                    if deviation > tolerance_m:
                        centroid = shape.polygon.centroid
                        out.append((
                            "causeway_plate",
                            f"{reference}:end{end_index}",
                            deviation,
                            tolerance_m,
                            _ll(layout, centroid.x, centroid.y),
                        ))
                    continue
                ring = list(shape.polygon.exterior.coords)
                if ring and ring[0] == ring[-1]:
                    ring = ring[:-1]
                solved = _shape_vertex_altitudes(shape, len(ring))
                if solved is None:
                    continue
                for (x, y), value in zip(ring, solved):
                    try:
                        # The capture band is the pin writer's own reach
                        # (stage 2b): every vertex the writer pinned is
                        # law-bound, wherever in the band it sits.
                        on_line = (
                            line.distance(_Point(x, y))
                            <= BRIDGE_ABUTMENT_PIN_CAPTURE_BAND_M
                        )
                    except Exception:
                        continue
                    if not on_line:
                        continue
                    deviation = abs(float(value) - law_value)
                    if deviation > tolerance_m:
                        out.append((
                            "deck_end_pin",
                            f"{reference}:end{end_index}",
                            deviation,
                            tolerance_m,
                            _ll(layout, x, y),
                        ))
    out.sort(key=lambda r: -r[2])
    return out


def check_bridge_crossing_floor(layout, dem, tile_lat, tile_lon,
                                tolerance_m: float = 0.25):
    """Object-bridge crossing-floor law reader (feature B stage 2,
    lockstep with the solve-side producer): every pavement ring vertex
    inside a TERRAIN/PROFILE_CARRIED span footprint whose road beneath
    is un-lowered must sit AT or ABOVE ``grade_law.bridge_crossing_
    floor_m``.  The floor value and every guard come from the SHARED
    ``bridges._bridge_crossing_floor_for_bridge`` — the identical
    decision the solver consumed.

    Pure reporter.  Returns ``[("crossing_floor", ref,
    metres_below_floor, tolerance_m, "lat,lon"), …]`` worst-first."""
    from .config import OBJECT_BRIDGE_TERRAIN
    if not OBJECT_BRIDGE_TERRAIN:
        return []
    from shapely.geometry import Point as _Point
    from .bridges import (
        _BRIDGE_PIN_ROLES,
        _bridge_crossing_floor_for_bridge,
        _local_meter_projections,
        _object_bridge_classification,
        _object_bridge_road_networks,
    )
    classification = _object_bridge_classification(layout)
    if classification is None:
        return []
    road_networks = _object_bridge_road_networks(layout)
    if not road_networks:
        return []
    to_meters, meters_to_lat_lon = _local_meter_projections(layout.anchor)
    out = []
    for bridge in classification.bridges:
        crossing = _bridge_crossing_floor_for_bridge(
            bridge, road_networks, dem, tile_lat, tile_lon,
            to_meters, meters_to_lat_lon,
        )
        if crossing is None:
            continue
        floor_value, footprint = crossing
        reference = ",".join(bridge.object_resources)
        for shape in layout.shapes:
            if shape.role not in _BRIDGE_PIN_ROLES:
                continue
            if shape.polygon is None or shape.polygon.is_empty:
                continue
            ring = list(shape.polygon.exterior.coords)
            if ring and ring[0] == ring[-1]:
                ring = ring[:-1]
            solved = _shape_vertex_altitudes(shape, len(ring))
            if solved is None:
                continue
            for (x, y), value in zip(ring, solved):
                try:
                    inside = footprint.contains(_Point(x, y))
                except Exception:
                    continue
                if not inside:
                    continue
                below = floor_value - float(value)
                if below > tolerance_m:
                    out.append((
                        "crossing_floor",
                        reference,
                        below,
                        tolerance_m,
                        _ll(layout, x, y),
                    ))
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


def lockstep_pair_caps_ll(layout):
    """The solver's WITHIN-SHAPE baked pair allowances as lat/lon endpoint
    pairs + metre caps — the sidecar's ``pair_caps`` key (the last lockstep
    reader: axes, routes, seam pins, mesh edges and crown drops are already
    exported; the PAIR SELECTION + per-pair anisotropic allowance was not,
    so the standalone check re-baked them from the emitted ring and drifted
    whenever post-projection vertex inserts shortened the spans — measured
    CYXY 2026-07-17: 11 of 12 within-shape flags were pairs the projection
    had enforced at a LOOSER lawful cap, the 12th a pair the law-side bake
    never selected).

    Source: ``layout._lockstep_shape_bake`` (grade_graph's per-shape export,
    ring-POSITION space at the ring state of the last law-graph build — the
    solve or ``final_grade_projection``).  Positions are resolved to
    coordinates through the entry's own stored ring signature, then to the
    CANONICAL point (the exact coordinates ``to_osm`` emits), so post-solve
    ring mutations cannot desynchronize the mapping; a vertex that no
    longer exists simply drops its pairs.  Caps are metres (the pair's
    grade budget); duplicate pairs keep the SMALLEST cap (the MIN-budget
    aggregation ruling, test_single_graph_acceptance 2026-07-17)."""
    import math as _math
    from .grade_law import pair_grade_budget_m
    store = getattr(layout, "_lockstep_shape_bake", None)
    registry = getattr(layout, "canonical_points", None)
    if not store or registry is None:
        return []
    best: dict = {}
    for (_role, ring_signature, baked_edges, _spine) in store.values():
        for (position_a, position_b, cap_allowance) in baked_edges:
            if (position_a >= len(ring_signature)
                    or position_b >= len(ring_signature)):
                continue
            (ax, ay) = ring_signature[position_a]
            (bx, by) = ring_signature[position_b]
            point_a = registry.find_nearest(ax, ay, registry.tol_m)
            point_b = registry.find_nearest(bx, by, registry.tol_m)
            if point_a is None or point_b is None:
                continue
            # The metre budget through THE shared pair-law formula, at
            # the solve-ring pair distance (the distance the projection
            # enforced the budget over).
            distance = _math.hypot(bx - ax, by - ay)
            if distance < 1e-9:
                continue
            try:
                budget = float(pair_grade_budget_m(cap_allowance, distance))
            except Exception:
                continue
            lat_a, lon_a = layout.m_to_ll(*point_a)
            lat_b, lon_b = layout.m_to_ll(*point_b)
            key_a = (round(lat_a, 7), round(lon_a, 7))
            key_b = (round(lat_b, 7), round(lon_b, 7))
            if key_a == key_b:
                continue
            pair_key = (min(key_a, key_b), max(key_a, key_b))
            if pair_key not in best or budget < best[pair_key]:
                best[pair_key] = budget
    return [[list(a), list(b), budget]
            for ((a, b), budget) in sorted(best.items())]


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


def check_epsilon_wedges(layout,
                         angle_deg_max: float = 0.5,
                         div_max_m: float = 0.20,
                         div_min_m: float = 1e-9,
                         bucket_m: float = 0.5):
    """Detect EPSILON WEDGES: two constrained edges that share a node, run
    near-parallel (angle < ``angle_deg_max``), and whose shorter edge's far
    endpoint sits ``div_min_m < d < div_max_m`` off the longer edge.

    Such a sliver is the KJQF/​part-30j regression class: where one shape
    RE-DERIVES a neighbour's outline with a slightly different vertex set,
    a foreign vertex lands ON the neighbour's edge WITHOUT a shared node.
    Triangle4XP's Ruppert encroachment rule then ping-pongs edge splits on
    the near-zero-area sliver down to machine epsilon, exploding the tile
    (KJQF: the boundary↔groundside_pavement seam alone drove ~2.0M tris).
    The FINAL epsilon-wedge weld in ``pipeline`` welds these; this check
    is the always-on regression tripwire so a future emitter that mints a
    fresh unwelded outline is flagged per-airport in the verify log.

    Operates on the in-memory layout (shapes share vertices by canonical
    coordinate, not an explicit node id, so vertices are grouped by a
    ``bucket_m`` XY bucket — the same tolerance the OSM emitter welds at).
    Returns a list of ``(angle_deg, div_m, role_a, role_b, loc)`` tuples,
    mirroring ``tools/wedge_audit.py``.
    """
    import math
    from collections import defaultdict
    from shapely.geometry import LineString, Point

    def _bucket(x, y):
        return (int(round(x / bucket_m)), int(round(y / bucket_m)))

    # incident[bucket] -> list of (far_xy, shape_idx, role)
    incident = defaultdict(list)
    for s_idx, s in enumerate(layout.shapes):
        poly = getattr(s, "polygon", None)
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            continue
        try:
            ring = list(poly.exterior.coords)
        except Exception:
            continue
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) < 3:
            continue
        role = s.role or "?"
        n = len(ring)
        for i in range(n):
            a = ring[i]
            for b in (ring[(i - 1) % n], ring[(i + 1) % n]):
                if b == a:
                    continue
                incident[_bucket(*a)].append((b, s_idx, role, a))

    cos_max = math.cos(math.radians(angle_deg_max))
    out = []
    seen = set()
    for _, inc in incident.items():
        if len(inc) < 2:
            continue
        for i in range(len(inc)):
            for j in range(i + 1, len(inc)):
                (b1, si1, r1, a1), (b2, si2, r2, a2) = inc[i], inc[j]
                if si1 == si2:
                    continue                    # same shape: kink, fine
                x0, y0 = a1                     # shared node (bucketed equal)
                v1 = (b1[0] - x0, b1[1] - y0)
                v2 = (b2[0] - x0, b2[1] - y0)
                n1 = math.hypot(*v1)
                n2 = math.hypot(*v2)
                if not n1 or not n2:
                    continue
                # same far endpoint (shared edge) → not a wedge
                if abs(b1[0] - b2[0]) < bucket_m \
                        and abs(b1[1] - b2[1]) < bucket_m:
                    continue
                cosv = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
                if cosv < cos_max:
                    continue
                if n1 < n2:
                    e = LineString([(x0, y0), b2])
                    d = e.distance(Point(b1))
                else:
                    e = LineString([(x0, y0), b1])
                    d = e.distance(Point(b2))
                if not (div_min_m < d < div_max_m):
                    continue
                ang = math.degrees(math.acos(min(1.0, cosv)))
                key = (round(x0, 2), round(y0, 2),
                       tuple(sorted((r1, r2))))
                if key in seen:
                    continue
                seen.add(key)
                out.append((ang, d, r1, r2, _ll(layout, x0, y0)))
    out.sort(key=lambda t: t[1])
    return out


# Airside pavement roles the mid-edge STEP gate governs.  A step between two
# of these within 2 m that exceeds the tolerance is a bug (the runway-crossing
# wedge class).  Everything else — groundside, clearances, and the designed
# vertical storeys (retaining_wall, bridge_causeway/deck welds, bridge_trench
# corridors + tunnel portals, tunnel_ramp) — is EXCLUDED: those roles either
# carry a None grade cap (already skipped by check_grade) or are groundside
# (wall-separated by design), so a height step across them is lawful.
_MIDEDGE_AIRSIDE_ROLES = frozenset({
    "runway", "runway_crossing", "primary_parallel", "secondary_parallel",
    "stub", "cross_connector", "apron", "junction", "terminal",
})
# Refs that mark a designed vertical storey even when a numeric-cap role tag
# leaks through (belt-and-suspenders on top of the role gate above).
_MIDEDGE_EXCLUDE_REFS = frozenset({
    "object_bridge_corridor", "object_bridge_causeway",
    "object_bridge_deck_weld", "object_bridge_approach", "tunnel_wall",
    "tunnel_ramp", "object_tunnel_portal_collar",
    "object_tunnel_portal_crown", "object_tunnel_portal_mouth",
})

# Mid-edge STEP gate thresholds.  "Airside shape pair within 2 m; a
# vertex-to-opposing-edge altitude step above 2.5 m is an ERROR."  The 2 m
# contact tolerance (vs check_grade's default 1 m) is deliberate: the KBNA
# 02L/20R+13/31 wedge put a 174.1 m crossing vertex 1.18 m from the 165.5 m
# runway edge — a 1 m gate misses it.
_MIDEDGE_CONTACT_TOL_M = 2.0
_MIDEDGE_STEP_TOL_M = 2.5


def check_midedge_step(layout):
    """MID-EDGE STEP gate — the blind-spot closure for the runway-crossing
    wedge class.

    For every AIRSIDE shape pair within ``_MIDEDGE_CONTACT_TOL_M``, project
    each shape's vertices (and interior edge samples) onto the neighbour's
    edge and flag any elevation step above ``_MIDEDGE_STEP_TOL_M``.  Reuses
    ``tools/check_grade`` for parsing AND for the projection logic
    (``_check_vertex_to_edge_step`` / ``_check_edge_midpoint_step``, called
    with the airside pair predicate + the wider 2 m touch tolerance) — no
    duplicated geometry.  Designed vertical storeys (retaining walls, bridge
    plates/causeways/deck welds, bridge-trench corridors + tunnel portals,
    tunnel ramps) are excluded via the airside-role gate and the ref list.

    Returns ``[(step_m, ref_v, ref_e, "lat,lon"), …]`` worst first — one row
    per over-tolerance mid-edge step."""
    check_grade = _import_check_grade()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "verify.osm"
        layout.to_osm(str(out))
        nodes, ways = check_grade._parse_osm(out)
    ll_to_m = check_grade._ll_to_m_factory(nodes, anchor=layout.anchor)
    vertices, edges = check_grade._build_vertex_edge_tables(nodes, ways, ll_to_m)

    def _airside(w) -> bool:
        role = w.tags.get("role")
        if role not in _MIDEDGE_AIRSIDE_ROLES:
            return False
        return w.tags.get("ref") not in _MIDEDGE_EXCLUDE_REFS

    def _pair_ok(way_v, way_e) -> bool:
        return _airside(way_v) and _airside(way_e)

    steps = check_grade._check_vertex_to_edge_step(
        vertices, edges, ways,
        edge_search_m=_MIDEDGE_CONTACT_TOL_M,
        edge_step_m=_MIDEDGE_STEP_TOL_M,
        contact_tol_m=_MIDEDGE_CONTACT_TOL_M, pair_ok=_pair_ok)
    steps += check_grade._check_edge_midpoint_step(
        edges, ways,
        edge_search_m=_MIDEDGE_CONTACT_TOL_M,
        edge_step_m=_MIDEDGE_STEP_TOL_M,
        contact_tol_m=_MIDEDGE_CONTACT_TOL_M, pair_ok=_pair_ok)

    # De-duplicate near-coincident findings (a vertex hit + its edge-sample
    # twin at the same spot) and convert the sample point back to lat/lon.
    out_rows = []
    seen = set()
    for s in steps:
        vx, vy = s.vert_pt
        key = (round(vx, 1), round(vy, 1), round(s.step_m, 1))
        if key in seen:
            continue
        seen.add(key)
        lat, lon = layout.m_to_ll(vx, vy)
        out_rows.append((s.step_m, s.way_v.tags.get("ref") or s.way_v.role,
                         s.way_e.tags.get("ref") or s.way_e.role,
                         f"{lat:.5f},{lon:.5f}"))
    out_rows.sort(key=lambda r: -r[0])
    return out_rows


def check_runway_join_step(layout):
    """RUNWAY-JOIN gate (user ruling 2026-07-16): every taxi/junction
    join vertex anchors to the RUNWAY EDGE value — the crowned edge —
    never the centerline/crown profile.  Runs the shared in-memory join
    validator (``grade_graph_validate._spine_runway_join_violations`` —
    the SAME check the solver's ``_runway_anchors`` mirrors, lockstep),
    which asserts a COINCIDENT join vertex within
    ``grade_law.RUNWAY_JOIN_COINCIDENT_TOL_M`` of the crowned-edge value
    and holds non-coincident pairs to the per-letter grade law.

    Returns ``[(step_or_pct, cap_pct, dist_m, "lat,lon"), …]`` worst
    first — coincident rows carry the raw step ×100 in the first slot."""
    from .grade_graph_validate import _spine_runway_join_violations
    from .config import ELEV_ROUNDING_NOISE_M
    rows = []
    for (pct, cap_pct, d, _kind, _sp, x, y) in \
            _spine_runway_join_violations(layout, ELEV_ROUNDING_NOISE_M):
        lat, lon = layout.m_to_ll(x, y)
        rows.append((pct, cap_pct, d, f"{lat:.7f},{lon:.7f}"))
    rows.sort(key=lambda r: -r[0])
    return rows


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
                             or [])],
            crown_centerline_ll=[[la, lo] for (la, lo) in
                                 (getattr(layout, "_crown_centerline_ll",
                                          None) or [])])




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
                        short_e, wedges, cross, within, steps,
                        rwy_grade, adjacent=(), bridge_pins=(),
                        bridge_floor=(), midedge=(),
                        join_steps=()) -> list:
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
    for ang, div, ra, rb, loc in sorted(wedges, key=lambda w: w[1]):
        out.append(f"  EPSILON-WEDGE {div * 1000:.3f} mm @ {ang:.4f}° "
                   f"@ {loc}: {ra} ~ {rb}")
    for step_m, ref_v, ref_e, loc in sorted(midedge, key=lambda r: -r[0]):
        out.append(f"  MID-EDGE-STEP {step_m:.2f} m airside "
                   f"(tol {_MIDEDGE_STEP_TOL_M:.1f} m) @ {loc}: "
                   f"{ref_v} ↔ {ref_e}")
    for pct, cap_pct, dist, loc in join_steps:
        if dist < 1e-6:
            out.append(f"  RUNWAY-JOIN step {pct / 100.0:.2f} m off the "
                       f"crowned edge at a COINCIDENT join @ {loc}")
        else:
            out.append(f"  RUNWAY-JOIN {pct:.1f}% > {cap_pct:.1f}% over "
                       f"{dist:.1f} m @ {loc}")
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
    for kind, ref, mag, tol, loc in sorted(adjacent, key=lambda r: -r[2]):
        verb = "un-filled below floor" if kind == "should_fill" \
            else "un-cut above ceiling"
        out.append(f"  ADJACENT-GROUND {mag:.1f} m {verb} "
                   f"(tol {tol:.1f} m) @ {loc}: {ref}")
    for _kind, ref, mag, tol, loc in sorted(
            bridge_pins, key=lambda r: -r[2]):
        out.append(f"  BRIDGE-DECK-PIN {mag:.2f} m off the deck-end law "
                   f"value (tol {tol:.2f} m) @ {loc}: {ref}")
    for _kind, ref, mag, tol, loc in sorted(
            bridge_floor, key=lambda r: -r[2]):
        out.append(f"  BRIDGE-CROSSING-FLOOR {mag:.2f} m below floor "
                   f"(tol {tol:.2f} m) @ {loc}: {ref}")
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
def verify_and_log(layout, icao: str, debug_log_path: str | None = None,
                   *, dem=None, tile_lat: int | None = None,
                   tile_lon: int | None = None,
                   source_runways=None) -> dict:
    """Run every verification check on a freshly-built layout and route the
    diagnostics to the per-tile verify DEBUG log (never raises).  Returns a
    counts dict.

    The adjacent-ground LATERAL grade law (``check_adjacent_ground``) is a
    DEM-based reader, so it runs ONLY when its gate (config.
    ADJACENT_GROUND_LAW_ENABLED) is on — with the gate off it is neither
    called nor counted, so the counts dict, the console summary and the
    debug file are byte-identical to a pre-law build.  When on, the DEM is
    reloaded from ``dem`` if provided (tests/lockstep) or via
    ``elevation._load_airport_dem`` at the layout anchor (production
    verify) — the same smoothed raster the standalone build path samples.

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
    wedges = []
    try:
        wedges = check_epsilon_wedges(layout)
    except Exception:                              # pragma: no cover
        pass
    # MID-EDGE STEP gate — the runway-crossing wedge blind-spot closure.
    # Always on (unlike the O4_VERIFY_OSM_GRADE block below): it is the
    # only reader that catches an airside vertex-to-opposing-edge step the
    # within-shape / cross-shape checks miss when the neighbour is ~1-2 m
    # away.  Never raises.
    midedge = []
    try:
        midedge = check_midedge_step(layout)
    except Exception:                              # pragma: no cover
        pass
    # RUNWAY-JOIN gate (user ruling 2026-07-16) — always on: the join
    # vertex must sit at the crowned runway edge value; the coincident
    # class was invisible to every other reader.  Never raises.
    join_steps = []
    try:
        join_steps = check_runway_join_step(layout)
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

    # Adjacent-ground LATERAL grade law — DEM-based, gate-guarded so a
    # law-off build has ZERO overhead and byte-identical verify output.
    adjacent = []
    from .config import ADJACENT_GROUND_LAW_ENABLED
    if ADJACENT_GROUND_LAW_ENABLED:
        import math as _math
        from .clearance import _GEOM_EXC as _shapely_domain_exceptions
        _dem = dem
        _tlat, _tlon = tile_lat, tile_lon
        if _dem is None:
            from .elevation import _load_airport_dem
            _dem = _load_airport_dem(layout.anchor[0], layout.anchor[1])
        if _tlat is None or _tlon is None:
            _tlat = int(_math.floor(layout.anchor[0]))
            _tlon = int(_math.floor(layout.anchor[1]))
        # Shapely-domain failures only (the repo's _GEOM_EXC rule): a
        # geometry-degeneracy may skip the check, but a programming error
        # (TypeError & co.) must SURFACE, never read as a false 0 count.
        try:
            adjacent = check_adjacent_ground(
                layout, _dem, _tlat, _tlon, source_runways=source_runways)
        except _shapely_domain_exceptions:         # pragma: no cover
            adjacent = []

    # Object-bridge law readers (feature B stage 2) — gate-guarded like
    # the adjacent-ground law: a gate-off build has ZERO overhead and
    # byte-identical verify output.  Shapely-domain failures only may
    # skip a check (the _GEOM_EXC rule); programming errors surface.
    bridge_pins = []
    bridge_floor = []
    from .config import OBJECT_BRIDGE_TERRAIN
    if OBJECT_BRIDGE_TERRAIN:
        import math as _math
        from .clearance import _GEOM_EXC as _shapely_domain_exceptions
        _dem = dem
        _tlat, _tlon = tile_lat, tile_lon
        if _dem is None:
            from .elevation import _load_airport_dem
            _dem = _load_airport_dem(layout.anchor[0], layout.anchor[1])
        if _tlat is None or _tlon is None:
            _tlat = int(_math.floor(layout.anchor[0]))
            _tlon = int(_math.floor(layout.anchor[1]))
        try:
            bridge_pins = check_bridge_deck_end_pins(
                layout, _dem, _tlat, _tlon)
        except _shapely_domain_exceptions:         # pragma: no cover
            bridge_pins = []
        try:
            bridge_floor = check_bridge_crossing_floor(
                layout, _dem, _tlat, _tlon)
        except _shapely_domain_exceptions:         # pragma: no cover
            bridge_floor = []

    # Feature B approach self-overlap gets its OWN named class (user
    # 2026-07-10): two object_bridge_approach rects double-covering
    # ground previously hid inside the generic overlap tally, and
    # overlapping sloped rects can never be repaired downstream — the
    # emitter must prevent them, so the class must be visible to gate
    # on.  The findings stay in the generic list too (the debug lines
    # show the pair detail there).
    approach_overlaps = []
    if OBJECT_BRIDGE_TERRAIN and overlaps:
        for finding in overlaps:
            _area, _index_a, _index_b, _loc = finding
            try:
                reference_a = getattr(layout.shapes[_index_a], "ref", "")
                reference_b = getattr(layout.shapes[_index_b], "ref", "")
            except Exception:                      # pragma: no cover
                continue
            if (reference_a == "object_bridge_approach"
                    and reference_b == "object_bridge_approach"):
                approach_overlaps.append(finding)

    counts = {"overlap": len(overlaps), "source": len(source),
              "terminal_flat": len(flat), "vertex_on_edge": len(edge_v),
              "vertex_on_flat_edge": len(flat_v),
              "axis_tilt": len(axis_v), "short_edge": len(short_e),
              "epsilon_wedge": len(wedges),
              "midedge_step": len(midedge),
              "runway_join": len(join_steps),
              "cross": len(cross), "within": len(within),
              "steps": len(steps), "runway_grade": len(rwy_grade)}
    # Build-time airside-piece drops (shared-vertex weld could not preserve a
    # >100 m² airside piece; see pavement.vertices._record_airside_drop).  A
    # healthy build reads ZERO — a non-zero count is a silent-pavement-loss
    # regression of the KBNA Donelson class (2026-07-16).
    counts["airside_weld_drop"] = len(
        getattr(layout, "airside_weld_drops", []) or [])
    if ADJACENT_GROUND_LAW_ENABLED:
        counts["adjacent_ground"] = len(adjacent)
    if OBJECT_BRIDGE_TERRAIN:
        counts["bridge_deck_pins"] = len(bridge_pins)
        counts["bridge_crossing_floor"] = len(bridge_floor)
        counts["object_bridge_approach_overlap"] = len(approach_overlaps)
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
        flat_v=flat_v, axis_v=axis_v, short_e=short_e, wedges=wedges,
        cross=cross, within=within, steps=steps, rwy_grade=rwy_grade,
        adjacent=adjacent, bridge_pins=bridge_pins,
        bridge_floor=bridge_floor, midedge=midedge,
        join_steps=join_steps)
    _write_verify_debug(debug_log_path, icao, counts, lines)

    # User console: one summary line only (suppressed at build verbosity 0);
    # every finding is an auto-patch bug logged to the verify debug file.
    tally = " ".join(f"{k}={v}" for k, v in counts.items() if v)
    UI.vprint(1, f"  [verify] {icao}: {sum(counts.values())} patch issue(s) "
                 f"({tally}) — logged to the verify debug file.")
    return counts

"""OSM aeroway centerline extraction + splitting.

Builds the polyline graph that taxi-rect emission walks.  Reads the
``aeroway=taxiway`` ways out of the cached OSM tile, links short
fragments into per-ref polylines, simplifies via RDP, then splits
each polyline at:

* significant chart-level direction changes
* width-profile transitions where the underlying pavement narrows
  or widens
* same-ref endpoints that other centerlines bend toward

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _bridge_same_ref_polylines
    _extract_osm_taxi_centerlines
    _insert_points_on_ring
    _insert_points_on_boundary
    _split_by_width_profile
    _sub_ref_narrow_corridor
    _split_centerlines_at_points
"""
from __future__ import annotations

import math

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import linemerge

from ..config import MIN_SEGMENT_LEN_M, taxi_ref_is_sub_index

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


# RDP simplification tolerance applied after per-ref linemerge.
RDP_SIMPLIFY_TOL_M = 1.0
# Only split parallels at bends sharper than this (degrees).
SIGNIFICANT_BEND_DEG = 5.0
# Cluster consecutive bends within this distance into one split point.
# 100 m because a taxi's direction change at an intersection area
# (e.g. V bends slightly around V2's junction) may span 70-90 m with
# 2 small bends marking the start and end of the transition.
BEND_CLUSTER_M = 100.0
# Bridge same-ref polyline gaps up to this distance via concatenation.
GAP_BRIDGE_MAX_M = 120.0


__all__ = [
    "BEND_CLUSTER_M",
    "GAP_BRIDGE_MAX_M",
    "RDP_SIMPLIFY_TOL_M",
    "SIGNIFICANT_BEND_DEG",
    "_bridge_same_ref_polylines",
    "_extract_osm_taxi_centerlines",
    "_find_width_transition_breakpoints",
    "_insert_points_on_boundary",
    "_insert_points_on_ring",
    "_split_by_width_profile",
    "_split_centerlines_at_points",
    "_sub_ref_narrow_corridor",
    "split_merged_centerline",
]


def _bridge_same_ref_polylines(lines: list[LineString]
                               ) -> list[LineString]:
    """Greedily connect endpoints of same-ref polylines within
    ``GAP_BRIDGE_MAX_M`` by concatenation.  Produces fewer, longer
    polylines covering the ref's full extent.
    """
    if len(lines) < 2:
        return lines

    remaining = list(lines)
    merged_lines: list[LineString] = []
    while remaining:
        cur = remaining.pop(0)
        while True:
            cur_coords = list(cur.coords)
            cur_start = cur_coords[0]
            cur_end = cur_coords[-1]
            best_idx = -1
            best_d = GAP_BRIDGE_MAX_M
            best_order = None  # "append_end", "append_start", "append_end_rev", "append_start_rev"
            for i, other in enumerate(remaining):
                oc = list(other.coords)
                o_start, o_end = oc[0], oc[-1]
                for order, pair in (
                    ("append_end", (cur_end, o_start)),
                    ("append_end_rev", (cur_end, o_end)),
                    ("append_start", (cur_start, o_end)),
                    ("append_start_rev", (cur_start, o_start)),
                ):
                    d = math.hypot(pair[0][0]-pair[1][0],
                                   pair[0][1]-pair[1][1])
                    if d < best_d:
                        best_d = d
                        best_idx = i
                        best_order = order
            if best_idx < 0:
                merged_lines.append(cur)
                break
            other = remaining.pop(best_idx)
            oc = list(other.coords)
            if best_order == "append_end":
                cur = LineString(cur_coords + oc)
            elif best_order == "append_end_rev":
                cur = LineString(cur_coords + oc[::-1])
            elif best_order == "append_start":
                cur = LineString(oc + cur_coords)
            elif best_order == "append_start_rev":
                cur = LineString(oc[::-1] + cur_coords)
    return merged_lines


def split_merged_centerline(
        ls: LineString,
        ref: str,
        rwy_centerlines: list[LineString] | None = None,
) -> list[tuple[LineString, str]]:
    """Split a single merged taxi-name polyline into rect-axis
    segments via RDP simplification + bend-split.

    Public helper so both the OSM extractor and apt.dat taxi-
    network builder feed merged polylines through the same
    splitting machinery.  Without it apt.dat-derived polylines
    (whose nodes follow real bends in the taxi route) emit as
    single straight rects spanning curves, producing rects that
    visibly drift off the actual pavement at every bend.

    Returns a list of ``(LineString, ref)`` segments — typically
    1-N pieces depending on how many significant bends survive
    RDP simplification.
    """
    out: list[tuple[LineString, str]] = []
    try:
        simp = ls.simplify(RDP_SIMPLIFY_TOL_M,
                           preserve_topology=False)
    except _GEOM_EXC:
        return out
    scoords = list(simp.coords)
    if len(scoords) < 2:
        return out
    # Geometrically-straight-enough centerlines emit as ONE
    # rect rather than being bend-split.  This catches
    # continuous diagonal taxis at any airport (e.g. SPJC's
    # B/C/E/G, CYXY's E parallel) where the simplified
    # polyline has small bends that would otherwise get
    # bend-split into too-short fragments.  Sub-refs are
    # excluded because they're typically already short
    # connector spurs that benefit from bend-splitting at
    # their natural curve points.
    #
    # The chord/path-only test is INSUFFICIENT for taxis like
    # SPJC's L that have a long mostly-straight middle plus
    # tight curves at the ends (chord/path = 0.955 even though
    # L bends 23° at one end and 14-22° at the other).  A
    # post-pass STRAIGHT-ENOUGH check requires chord/path
    # close to 1 AND every interior bend below
    # ``MAX_INTERIOR_BEND_DEG``.  L's max bend is 23° → fails;
    # B/C/E/G/CYXY-E's max wobble is ~5° → still passes.
    MAX_INTERIOR_BEND_DEG = 15.0
    has_digit = bool(ref) and taxi_ref_is_sub_index(ref)
    if not has_digit:
        path_len = ls.length
        sc = list(simp.coords)
        if len(sc) >= 2 and path_len > 1e-6:
            chord = math.hypot(sc[-1][0] - sc[0][0],
                               sc[-1][1] - sc[0][1])
            chord_ratio = chord / path_len
            max_interior_bend = 0.0
            for k in range(1, len(sc) - 1):
                ax, ay = sc[k - 1]
                bx, by = sc[k]
                cx, cy = sc[k + 1]
                v1x, v1y = bx - ax, by - ay
                v2x, v2y = cx - bx, cy - by
                m1 = math.hypot(v1x, v1y)
                m2 = math.hypot(v2x, v2y)
                if m1 < 1e-6 or m2 < 1e-6:
                    continue
                d = (v1x * v2x + v1y * v2y) / (m1 * m2)
                if d > 1.0:
                    d = 1.0
                elif d < -1.0:
                    d = -1.0
                ang = math.degrees(math.acos(d))
                if ang > max_interior_bend:
                    max_interior_bend = ang
            if (chord_ratio > 0.95
                    and max_interior_bend
                    < MAX_INTERIOR_BEND_DEG):
                out.append((simp, ref))
                return out
    # SHORT UNREFED runway-connecting stubs: SPLP has short
    # curvy unrefed taxis (e.g. way -696731, 165 m chord
    # 144 m) that link runway to apron/primary.  Target
    # emits a single rect in the middle of each.  Bend-
    # splitting fragments them into pieces too small to
    # survive the 40 m floor in `_split_centerlines_at_points`.
    # Emit atomically when: ref="" (unrefed) AND path < 300 m
    # AND one endpoint is inside the runway polygon.
    if (not ref
            and ls.length < 300.0
            and rwy_centerlines):
        try:
            sc = list(simp.coords)
            if len(sc) >= 2:
                ep0 = Point(sc[0])
                ep1 = Point(sc[-1])
                ep0_near = any(
                    ep0.distance(r) < 30.0 for r in rwy_centerlines)
                ep1_near = any(
                    ep1.distance(r) < 30.0 for r in rwy_centerlines)
                if ep0_near or ep1_near:
                    out.append((simp, ref))
                    return out
        except _GEOM_EXC:
            pass
    # All refs (including sub-refs) split at significant
    # bends.  Per user (2026-04-20 refined): intersections
    # + sharp curves define rect break points; there's no
    # reason sub-refs should be exempt from curve detection.
    # Split at INTERNAL bends with angle change ≥
    # SIGNIFICANT_BEND_DEG, but cluster consecutive
    # bends within BEND_CLUSTER_M together.  A curve
    # (many tiny bends adding up to a big turn) counts
    # as ONE break point at its midpoint — matching
    # how the target treats a curve as a single logical
    # transition between rects.
    candidate_bends: list[int] = []
    for i in range(1, len(scoords) - 1):
        a = scoords[i - 1]
        b = scoords[i]
        c = scoords[i + 1]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        m1 = math.hypot(*v1)
        m2 = math.hypot(*v2)
        if m1 < 1e-6 or m2 < 1e-6:
            continue
        dot = (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)
        dot = max(-1.0, min(1.0, dot))
        angle_change = math.degrees(math.acos(dot))
        if angle_change >= SIGNIFICANT_BEND_DEG:
            candidate_bends.append(i)
    # Cluster consecutive bends within BEND_CLUSTER_M of
    # each other.  Per user rule (2026-04-20): when a
    # primary taxi curves at the runway, emit the straight
    # portions as rects and leave the curve itself as
    # junction territory (no rect emitted for the curve).
    # → Each cluster yields TWO break indices
    #   (cluster start, cluster end) with the curve
    #   interval between them skipped.
    # A single isolated bend (cluster of 1) gives only
    # ONE break at itself.
    # Sub-refs (letter+digit: V3, V5, L1, …) are usually
    # short stub taxis whose straight portion is much
    # less than the primary's junction-bend-transition
    # span.  Using the primary's 100 m cluster distance
    # swallows a true 90°-corner stub's straight middle
    # run (e.g. V5 indices [13..15] are a 97 m straight
    # between two tight curves).  For sub-refs we cluster
    # bends far more conservatively so the straight run
    # between two curves can survive as its own segment.
    cluster_m = BEND_CLUSTER_M
    if ref and taxi_ref_is_sub_index(ref):
        cluster_m = 30.0
    clusters: list[list[int]] = []
    for bi in candidate_bends:
        if clusters and (scoords[bi][0] - scoords[clusters[-1][-1]][0])**2 + \
                (scoords[bi][1] - scoords[clusters[-1][-1]][1])**2 \
                <= cluster_m * cluster_m:
            clusters[-1].append(bi)
        else:
            clusters.append([bi])
    # Break the simplified polyline into rect-axis segments at bend
    # POINTS.  Each single bend is one break point; a multi-bend cluster
    # (a curve) is split either at EVERY bend or at the cluster midpoint.
    events: list[int] = [0]
    for cl in clusters:
        if len(cl) == 1:
            events.append(cl[0])
            continue
        # A near-runway curve at a polyline ENDPOINT is a taxiway turning
        # and CLIMBING into an apron / toward a runway threshold.  Earlier
        # this curve was SKIPPED ("junction territory"), which dropped the
        # climbing taxi spine and left the curve pavement to grade as flat
        # apron (CYXY taxiway E: a whole-route curve whose spine should
        # turn and climb into the north apron).  Per user 2026-06-29 we KEEP
        # such curves — split at EVERY bend so the rects (and the spine they
        # carry) follow the turn.  A gentle mid-route curve (e.g. A's bend
        # mid-airport) instead stays a single midpoint dogleg break, as
        # before — splitting it finely is unnecessary and a mid-polyline
        # curve must remain rect territory anyway (skipping it once let the
        # neighbour rect overshoot past the split — CYXY taxiway D, 2026-05-15).
        near_rwy = False
        if rwy_centerlines:
            cp = Point(scoords[cl[len(cl) // 2]])
            near_rwy = any(cp.distance(r) < 200.0 for r in rwy_centerlines)
        at_endpoint = False
        if near_rwy:
            cs = Point(scoords[cl[0]])
            ce = Point(scoords[cl[-1]])
            ls_start = Point(scoords[0])
            ls_end = Point(scoords[-1])
            CLUSTER_TO_POLYLINE_END_TOL_M = 30.0
            at_endpoint = (
                cs.distance(ls_start) < CLUSTER_TO_POLYLINE_END_TOL_M
                or ce.distance(ls_end) < CLUSTER_TO_POLYLINE_END_TOL_M)
        if near_rwy and at_endpoint:
            events.extend(cl)                   # follow every bend
        else:
            events.append(cl[len(cl) // 2])     # single dogleg break
    events.append(len(scoords) - 1)
    ev = sorted(set(events))
    for i0, i1 in zip(ev, ev[1:]):
        try:
            seg = LineString(scoords[i0:i1 + 1])
        except _GEOM_EXC:
            continue
        if seg.is_empty or seg.length < MIN_SEGMENT_LEN_M:
            continue
        out.append((seg, ref))
    return out


def _extract_osm_taxi_centerlines(
    nodes: dict[str, tuple[float, float]],
    ways: list[tuple[str, list[str], dict[str, str]]],
    to_m,
    rwy_centerlines: list[LineString] | None = None,
) -> list[tuple[LineString, str]]:
    """Extract one polyline segment per (ref, straight-run).

    Algorithm:
      1. Gather all OSM taxi ways per ref.
      2. linemerge() the per-ref ways into the fewest possible
         contiguous polylines.  OSM splits one physical taxi
         across many <way> rows at each node — merging restores
         the single polyline per strip.
      3. RDP-simplify each merged polyline at
         ``RDP_SIMPLIFY_TOL_M``.  Minor GPS wobble collapses;
         genuine bends remain as internal vertices.
      4. Split the simplified polyline at each remaining vertex
         into individual straight-run segments.
      5. Drop segments shorter than ``MIN_SEGMENT_LEN_M`` and
         (at airports with any refs) drop unrefed segments.

    Per user convention, stubs and cross-connectors typically merge
    down to a single polyline with a single straight run (emitted
    as 1 rect).  Long parallel taxis that physically bend (like L
    at SPJC) retain bend vertices and emit multiple rects that
    share corner vertices at the bend.
    """
    from .rects import _natural_half_width
    by_ref: dict[str, list[LineString]] = {}
    for wid, nds, tags in ways:
        # Per user 2026-05-04: treat aeroway=parking_position as
        # taxiway.  At SPJC and similar airports, parking_position
        # ways outnumber taxiway ways (244 vs 234) and represent the
        # painted yellow lines aircraft follow from taxi to gate —
        # functionally part of the apron taxi network.  Without
        # them, the apron lacks junction-attachments at parking
        # stands and ends up with the aprons spanning past the rect
        # corners that ought to bound it.
        if tags.get("aeroway") not in ("taxiway", "parking_position"):
            continue
        ref = tags.get("ref", "")
        pts = []
        for n in nds:
            if n in nodes:
                lat, lon = nodes[n]
                pts.append(to_m(lon, lat))
        if len(pts) < 2:
            continue
        try:
            ls = LineString(pts)
        except _GEOM_EXC:
            continue
        if ls.is_empty or ls.length < 5.0:
            continue
        by_ref.setdefault(ref, []).append(ls)

    out: list[tuple[LineString, str]] = []
    for ref, lines in by_ref.items():
        # Stage 1: contiguous-endpoint linemerge.
        if len(lines) > 1:
            try:
                merged = linemerge(MultiLineString(lines))
            except _GEOM_EXC:
                merged = None
            if merged is None or merged.is_empty:
                merged_lines = lines
            elif merged.geom_type == "LineString":
                merged_lines = [merged]
            else:
                merged_lines = list(merged.geoms)
        else:
            merged_lines = lines

        # Stage 2: gap bridging.  Bridge gaps for PRIMARY refs
        # (long continuous taxis) where OSM fragments across
        # intersections.  For SUB-REFS (letter+digit like V2, L3)
        # each OSM way is typically a separate short stub;
        # bridging their gaps creates fake segments through
        # non-pavement and confuses downstream width-profile
        # narrow-corridor detection.
        is_sub_ref = ref and taxi_ref_is_sub_index(ref)
        if ref and len(merged_lines) > 1 and not is_sub_ref:
            merged_lines = _bridge_same_ref_polylines(merged_lines)

        for ls in merged_lines:
            out.extend(split_merged_centerline(ls, ref, rwy_centerlines))

    # Drop unrefed centerlines AT AIRPORTS THAT HAVE ANY REFED
    # CENTERLINES (per user 2026-04-27).  At SPJC etc. the OSM data
    # has comprehensive refs on real taxiways; unrefed lines that
    # remain are typically apron decorations / vehicle paths /
    # painted markings that, if extracted into rects, get inserted
    # INSIDE apron polygons — junction polygons then wrap around
    # them and produce visible elevation ridges where the rect's
    # short edges meet the junction at slightly different heights.
    # The user's directive: don't insert rects inside junction /
    # apron polygons.
    #
    # Previously this was an "if any_ref drop unrefed" filter in
    # this same place; an in-flight Session 8 change removed it on
    # the theory that downstream geometric-overlap dedup would
    # catch spurious unrefed sub-segments.  But spurious unrefed
    # apron lines DON'T overlap any refed rect (they sit inside an
    # apron region, not along a real taxi corridor) so the dedup
    # never fires for them, and HEAD-clean's clean baseline (47
    # rects, all refed at SPJC) regressed to 120 rects (65 of them
    # unrefed) inside apron areas.  Restoring the filter here.
    #
    # At airports with NO refed centerlines (CYXY where every OSM
    # taxi is unrefed) the filter is a no-op — every centerline is
    # kept.
    any_ref = any(r for (_, r) in out)
    if any_ref:
        out = [(ls, r) for (ls, r) in out if r]
    return out




def _insert_points_on_ring(
    ring_coords: list[tuple[float, float]],
    pts: list[tuple[float, float]],
    tol: float,
) -> list[tuple[float, float]]:
    """Insert each point in ``pts`` as a vertex at its projected
    position on the closed ring (list of coords, first == last),
    if within ``tol``.  Returns the new ring coords (closed).
    Pure helper so both exterior and interior rings are handled
    uniformly."""
    if not pts or len(ring_coords) < 4:
        return ring_coords
    ring = LineString(ring_coords)
    inserts: list[tuple[float, tuple[float, float]]] = []
    for (x, y) in pts:
        p = Point(x, y)
        if p.distance(ring) > tol:
            continue
        try:
            param = ring.project(p)
            proj = ring.interpolate(param)
        except _GEOM_EXC:
            continue
        inserts.append((param, (proj.x, proj.y)))
    if not inserts:
        return ring_coords
    inserts.sort()
    coords = list(ring_coords)
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    new_coords: list[tuple[float, float]] = []
    cur_param = 0.0
    insert_i = 0
    for i in range(len(coords)):
        new_coords.append(coords[i])
        next_i = (i + 1) % len(coords)
        seg_len = math.hypot(coords[next_i][0] - coords[i][0],
                             coords[next_i][1] - coords[i][1])
        seg_end = cur_param + seg_len
        while (insert_i < len(inserts)
               and inserts[insert_i][0] < seg_end):
            new_coords.append(inserts[insert_i][1])
            insert_i += 1
        cur_param = seg_end
    new_coords.append(new_coords[0])
    return new_coords




def _insert_points_on_boundary(
    poly: Polygon,
    pts: list[tuple[float, float]],
    tol: float = 2.0,
) -> Polygon:
    """Insert each point in ``pts`` as a vertex on the polygon's
    boundary (exterior + all interior rings) at its projected
    position, if within ``tol`` of the boundary.  Interior rings
    are preserved — critical when the polygon represents a
    pavement residue with rect-shaped holes.  Used to seam
    junction polygons with their neighbouring rect / terminal
    corners."""
    if not pts:
        return poly
    try:
        ext = list(poly.exterior.coords)
        new_ext = _insert_points_on_ring(ext, pts, tol)
        new_ints = []
        for ring in poly.interiors:
            ri = list(ring.coords)
            new_ints.append(_insert_points_on_ring(ri, pts, tol))
        new_poly = Polygon(new_ext, new_ints)
        if not new_poly.is_valid:
            new_poly = new_poly.buffer(0)
        if (new_poly.geom_type == "Polygon"
                and new_poly.is_valid and not new_poly.is_empty):
            return new_poly
    except _GEOM_EXC:
        pass
    return poly




def _split_by_width_profile(
    centerlines: list[tuple[LineString, str]],
    pav_union: Polygon,
    probe_step_m: float = 5.0,
    wide_factor: float = 1.20,
    min_rect_len_m: float = 30.0,
) -> list[tuple[LineString, str]]:
    """Split each centerline into NARROW-CORRIDOR intervals per
    user rule 4 (2026-04-20): rects cover only the narrowest
    straight sections; any widening (around intersections or
    terminal aprons) is junction territory and is skipped.

    For each line:
      1. Probe pav half-width at ``probe_step_m`` intervals.
      2. narrow_hw = 10th-percentile probe (robust narrow baseline).
      3. Interval flag per probe: NARROW if hw ≤ wide_factor × narrow_hw.
      4. Emit contiguous NARROW intervals ≥ min_rect_len_m as rects.
    """
    if not centerlines or pav_union is None or pav_union.is_empty:
        return centerlines
    from shapely.ops import substring
    pav_boundary = pav_union.boundary

    result: list[tuple[LineString, str]] = []
    for ls, ref in centerlines:
        if ls.length < min_rect_len_m:
            result.append((ls, ref))
            continue
        n_probes = max(10, int(ls.length / probe_step_m))
        # sample (param, hw) pairs along the line
        samples: list[tuple[float, float]] = []
        for i in range(n_probes + 1):
            t = i / n_probes * ls.length
            pt = ls.interpolate(t)
            hw = pt.distance(pav_boundary) if pav_union.contains(pt) else 0.0
            samples.append((t, hw))
        # narrow_hw = 10th-percentile of positive hw values
        hws = sorted(h for _, h in samples if h > 0)
        if not hws:
            result.append((ls, ref))
            continue
        narrow_hw = hws[max(1, len(hws) // 10)]
        wide_thresh = wide_factor * narrow_hw
        # Flag each sample narrow or wide
        is_narrow = [h > 0 and h <= wide_thresh for (_, h) in samples]
        # Find contiguous narrow intervals
        intervals: list[tuple[float, float]] = []
        i = 0
        while i < len(samples):
            if not is_narrow[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(samples) and is_narrow[j + 1]:
                j += 1
            start_t = samples[i][0]
            end_t = samples[j][0]
            if end_t - start_t >= min_rect_len_m:
                intervals.append((start_t, end_t))
            i = j + 1
        if not intervals:
            # Whole line is "wide" — probably an apron traverse.
            # Drop it; user's target wouldn't emit a rect here.
            continue
        for (s, e) in intervals:
            try:
                seg = substring(ls, s, e)
            except _GEOM_EXC:
                continue
            if (seg.geom_type == "LineString"
                    and not seg.is_empty
                    and seg.length >= min_rect_len_m):
                result.append((seg, ref))
    return result




def _sub_ref_narrow_corridor(
    centerlines: list[tuple[LineString, str]],
    pav_union: Polygon,
    probe_step_m: float = 4.0,
    wide_factor: float = 1.30,
    narrow_margin_frac: float = 0.15,
) -> list[tuple[LineString, str]]:
    """For each sub-ref (ref like V1/V3/A1/L3 — letter+digit),
    replace its centerline(s) with the 70% middle slice of the
    LONGEST narrow-corridor interval.

    Algorithm:
      1. Perpendicular ray-cast half-width probes every
         ``probe_step_m`` along the centerline.
      2. narrow_hw = min probe (≥ 3.5 m floor).
      3. Flag each probe narrow/wide by ``wide_factor × narrow_hw``.
      4. Longest contiguous narrow interval → emit 70% middle.

    Also de-dupes multiple OSM ways with the same sub-ref by
    keeping the one whose selected-slice is longest and narrowest.
    """
    if not centerlines or pav_union is None or pav_union.is_empty:
        return centerlines
    from shapely.ops import substring

    RAY_CAP_M = 40.0
    RAY_STEP_M = 0.5

    def _perp_hw(line: LineString, t: float) -> float:
        dt = min(2.0, line.length * 0.05)
        t0 = max(0.0, t - dt)
        t1 = min(line.length, t + dt)
        a = line.interpolate(t0)
        b = line.interpolate(t1)
        tx, ty = b.x - a.x, b.y - a.y
        mag = math.hypot(tx, ty)
        if mag < 1e-6:
            return 0.0
        ux, uy = tx / mag, ty / mag
        nx, ny = -uy, ux
        pt = line.interpolate(t)
        ox, oy = pt.x, pt.y
        best = RAY_CAP_M
        for sign in (-1, 1):
            d = 0.0
            while d <= RAY_CAP_M:
                qx = ox + sign * nx * d
                qy = oy + sign * ny * d
                if not pav_union.contains(Point(qx, qy)):
                    if d < best:
                        best = d
                    break
                d += RAY_STEP_M
        return best

    # ICAO Code E taxi: 23m wide = 11.5m half-width.
    # Allow up to 16m half-width as "still on the taxi strip";
    # beyond that we're in a widening (intersection or apron).
    NARROW_TAXI_HW_M = 16.0

    def _narrow_slice(ls: LineString) -> tuple[LineString, float] | None:
        """Return (slice, avg_hw_in_narrow) for the 70% middle of
        the longest narrow-corridor interval along ls.  Uses a
        FIXED narrow-width threshold (NARROW_TAXI_HW_M) based on
        ICAO standards rather than per-line percentiles, which
        are unreliable on short or highly-curved sub-refs."""
        if ls.length < MIN_SEGMENT_LEN_M:
            return None
        n = max(10, int(ls.length / probe_step_m))
        samples: list[tuple[float, float]] = []
        for i in range(n + 1):
            t = i / n * ls.length
            hw = _perp_hw(ls, t)
            samples.append((t, hw))
        if not any(h > 0 for _, h in samples):
            return None
        is_narrow = [3.5 <= h <= NARROW_TAXI_HW_M for (_, h) in samples]
        # Longest contiguous narrow interval.
        best_i, best_j = -1, -1
        i = 0
        while i < len(samples):
            if not is_narrow[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(samples) and is_narrow[j + 1]:
                j += 1
            if j - i > best_j - best_i:
                best_i, best_j = i, j
            i = j + 1
        if best_i < 0:
            return None
        a_t = samples[best_i][0]
        b_t = samples[best_j][0]
        interval_len = b_t - a_t
        if interval_len < MIN_SEGMENT_LEN_M:
            return None
        margin = narrow_margin_frac * interval_len
        s_t = a_t + margin
        e_t = b_t - margin
        if e_t - s_t < MIN_SEGMENT_LEN_M:
            return None
        try:
            seg = substring(ls, s_t, e_t)
        except _GEOM_EXC:
            return None
        if (seg.geom_type != "LineString"
                or seg.is_empty
                or seg.length < MIN_SEGMENT_LEN_M):
            return None
        # Average hw within the narrow interval (for dedup scoring).
        narrow_hws = [h for (t, h) in samples
                      if best_i <= samples.index((t, h)) <= best_j
                      if 3.5 <= h <= NARROW_TAXI_HW_M]
        avg_hw = sum(narrow_hws) / len(narrow_hws) if narrow_hws else 0.0
        return seg, avg_hw

    # Group sub-ref lines; keep all other lines as-is.
    from collections import defaultdict
    sub_ref_lines: dict[str, list[LineString]] = defaultdict(list)
    result: list[tuple[LineString, str]] = []
    for ls, ref in centerlines:
        if ref and taxi_ref_is_sub_index(ref):
            sub_ref_lines[ref].append(ls)
        else:
            result.append((ls, ref))

    # For each sub-ref, pick the best slice.
    for ref, lines in sub_ref_lines.items():
        slices: list[tuple[LineString, float]] = []
        for l in lines:
            r = _narrow_slice(l)
            if r is not None:
                slices.append(r)
        if not slices:
            # Fallback: keep the longest raw polyline.
            lines_sorted = sorted(lines, key=lambda l: -l.length)
            if lines_sorted and lines_sorted[0].length >= MIN_SEGMENT_LEN_M:
                result.append((lines_sorted[0], ref))
            continue
        # Prefer the LONGEST slice — the physical taxi corridor
        # typically has the longest continuous narrow interval.
        slices.sort(key=lambda sh: -sh[0].length)
        best = slices[0][0]
        result.append((best, ref))

    return result




def _find_width_transition_breakpoints(
    centerlines: list[tuple[LineString, str]],
    pav_union: Polygon,
    widen_factor: float = 1.5,
    n_probes_per_100m: float = 0.5,
    min_probes: int = 12,
    max_probes: int = 60,
) -> list[tuple[float, float]]:
    """Find axial positions along each centerline where the
    perpendicular half-width transitions between "narrow" and
    "wide" zones.  Each transition is added to the global
    junction-points list so downstream ``_split_centerlines_at_points``
    can split the centerline at width-change locations.

    Per user 2026-05-12: apt.dat row-1202 has fewer, longer taxi
    edges than OSM aeroway — a single 500 m apt.dat V edge can
    span multiple pavement-width regimes (constant 41 m corridor +
    61 m widened section near an apron-merge), where the OSM
    target had each regime represented by a separate way.
    Width-transition detection finds these regime boundaries
    geometrically and adds them as split points so V (and similar
    long-edge apt.dat taxis) get the same multi-rect breakdown
    target uses.

    Algorithm per centerline:
      1. Probe perpendicular half-width at evenly-spaced axial
         positions (``_perpendicular_half_at`` from rects.py).
      2. ``narrow_hw`` = MIN probe value.
      3. Walk probes in order; track NARROW vs WIDE state
         (wide = hw > narrow_hw * widen_factor).
      4. On any state change, emit a breakpoint at the midpoint
         between the two adjacent probes.

    Endpoint probes do NOT emit breakpoints (the centerline
    endpoint is already a natural breakpoint or junction).
    """
    from .rects import _natural_half_width
    if pav_union is None or pav_union.is_empty:
        return []

    RAY_CAP_M = 40.0
    RAY_STEP_M = 0.5

    def _perp_hw_at(ls: LineString, t: float) -> float:
        dt = min(2.0, ls.length * 0.05)
        t0 = max(0.0, t - dt)
        t1 = min(ls.length, t + dt)
        a = ls.interpolate(t0)
        b = ls.interpolate(t1)
        tx, ty = b.x - a.x, b.y - a.y
        mag = math.hypot(tx, ty)
        if mag < 1e-6:
            return 0.0
        ux, uy = tx / mag, ty / mag
        nx, ny = -uy, ux
        pt = ls.interpolate(t)
        ox, oy = pt.x, pt.y
        sides: list[float] = []
        for sign in (-1, 1):
            side = RAY_CAP_M
            d = 0.0
            while d <= RAY_CAP_M:
                qx = ox + sign * nx * d
                qy = oy + sign * ny * d
                if not pav_union.contains(Point(qx, qy)):
                    side = d
                    break
                d += RAY_STEP_M
            sides.append(side)
        return sum(sides) / 2.0 if sides else 0.0

    breakpoints: list[tuple[float, float]] = []
    for ls, _ref in centerlines:
        if ls.length < 60.0:
            continue
        n_probes = max(min_probes, min(max_probes,
                                        int(ls.length * n_probes_per_100m
                                            / 100.0)))
        probes: list[tuple[float, float]] = []  # (t, hw)
        for k in range(n_probes):
            t = (k + 0.5) / n_probes * ls.length
            hw = _perp_hw_at(ls, t)
            if hw > 0.1:
                probes.append((t, hw))
        if len(probes) < 4:
            continue
        narrow_hw = min(p[1] for p in probes)
        if narrow_hw < 3.0:
            continue
        threshold = narrow_hw * widen_factor
        prev_wide = probes[0][1] > threshold
        for i in range(1, len(probes)):
            t_i, hw_i = probes[i]
            curr_wide = hw_i > threshold
            if curr_wide != prev_wide:
                # Transition between probe i-1 and probe i.
                # Emit breakpoint at the midpoint.
                t_mid = 0.5 * (probes[i - 1][0] + t_i)
                # Skip if very close to either endpoint.
                if (t_mid < 30.0
                        or t_mid > ls.length - 30.0):
                    prev_wide = curr_wide
                    continue
                pt = ls.interpolate(t_mid)
                breakpoints.append((pt.x, pt.y))
                prev_wide = curr_wide
    return breakpoints


# Tunable junction/bend margins (default = historical). Exposed as
# module globals so the centerline output can be regenerated with
# different pullbacks for review without editing the call site.
_CHART_JUNCTION_MARGIN_M = 20.0
_BEND_ENDPOINT_MARGIN_M = 8.0


# Off-corridor centerline drop thresholds (session 56).
_RWY_CROSS_DROP_M = 5.0       # drop if >this much of the line is inside a runway
_BURIED_HALFWIDTH_M = 50.0    # drop if the line's MEDIAN perp half-width >=this
                              # (no real taxiway is this wide; it's apron/junction)


_BEND_HOOK_DEG = 12.0       # bend angle that marks a non-straight piece
_BEND_HOOK_MAX_FRAC = 0.45  # only drop the shorter side if it's < this
                            # fraction of the piece (a short hook, not a
                            # genuine L-bend whose two arms are both real)


def _trim_short_bend_hooks(
    centerlines: list[tuple[LineString, str]],
) -> list[tuple[LineString, str]]:
    """The target represents taxiways as STRAIGHT pieces — a bend is never a
    single segment, it is a break at the corner into two straights.  So at
    each bend (>= ``_BEND_HOOK_DEG``) we SPLIT the centerline at the corner:
      * if one arm is a short hook (< ``_BEND_HOOK_MAX_FRAC`` of the piece) —
        a stub into a junction/apron the target omits — drop it and keep the
        long straight run;
      * otherwise both arms are real taxiway, so keep BOTH straight pieces.
    Recurse so every emitted piece is straight; no bent piece survives."""
    def _sharpest(coords):
        worst = 0.0
        wi = -1
        for i in range(1, len(coords) - 1):
            a, b, c = coords[i - 1], coords[i], coords[i + 1]
            b1 = math.atan2(b[0] - a[0], b[1] - a[1])
            b2 = math.atan2(c[0] - b[0], c[1] - b[1])
            d = abs(math.degrees(b1 - b2)) % 360.0
            d = min(d, 360.0 - d)
            if d > worst:
                worst, wi = d, i
        return worst, wi

    def _straighten(ls):
        """Return a list of straight pieces for ``ls`` (hooks dropped)."""
        coords = list(ls.coords)
        if len(coords) < 3:
            return [ls]
        ba, bi = _sharpest(coords)
        if ba < _BEND_HOOK_DEG or bi <= 0:
            return [ls]  # already straight
        try:
            p1 = LineString(coords[:bi + 1])
            p2 = LineString(coords[bi:])
        except _GEOM_EXC:
            return [ls]
        short, lng = (p1, p2) if p1.length < p2.length else (p2, p1)
        if ls.length > 0 and short.length < _BEND_HOOK_MAX_FRAC * ls.length:
            return _straighten(lng)              # short hook — drop it
        return _straighten(p1) + _straighten(p2)  # real L — keep both straights

    out: list[tuple[LineString, str]] = []
    for ls, ref in centerlines:
        for p in _straighten(ls):
            out.append((p, ref))
    return out


def _median_perp_halfwidth(ls: LineString, pav: Polygon,
                           n: int = 10, cap_m: float = 70.0) -> float:
    """Median, along ``ls``, of the NEARER pavement edge (min of the left
    and right perpendicular ray distances, capped at ``cap_m``).

    Using the nearer edge (not the average) is what distinguishes a
    centerline BURIED in a wide junction/apron — wide on BOTH sides, so a
    large min — from a taxiway running ALONG the edge of a wide apron —
    one narrow (taxi-edge) side, so a small min.  An average would flag the
    edge taxiway as buried (SPJC R1/R2/L)."""
    if pav is None or pav.is_empty or ls.length < 1.0:
        return 0.0
    STEP = 1.0
    vals: list[float] = []
    for i in range(n + 1):
        t = i / n * ls.length
        dt = min(2.0, ls.length * 0.05)
        a = ls.interpolate(max(0.0, t - dt))
        b = ls.interpolate(min(ls.length, t + dt))
        dx, dy = b.x - a.x, b.y - a.y
        mag = math.hypot(dx, dy)
        if mag < 1e-6:
            continue
        nx, ny = -dy / mag, dx / mag
        p = ls.interpolate(t)
        sides = []
        for sign in (-1, 1):
            d = 0.0
            hit = cap_m
            while d <= cap_m:
                if not pav.contains(Point(p.x + sign * nx * d,
                                          p.y + sign * ny * d)):
                    hit = d
                    break
                d += STEP
            sides.append(hit)
        vals.append(min(sides))
    if not vals:
        return 0.0
    vals.sort()
    return vals[len(vals) // 2]


def _drop_offcorridor_centerlines(
    centerlines: list[tuple[LineString, str]],
    pav_union: Polygon | None,
    rwy_union: Polygon | None,
) -> tuple[list[tuple[LineString, str]], int, int]:
    """Drop centerlines that do not correspond to a taxiway rect:
      * runway-crossing — > ``_RWY_CROSS_DROP_M`` of the line lies inside
        the runway (the runway emit covers that surface); and
      * junction/apron-buried — the line's median perpendicular pavement
        half-width is >= ``_BURIED_HALFWIDTH_M`` (it runs through the
        middle of a wide junction/apron, not a narrow taxi corridor).
    Returns (kept, n_runway_dropped, n_buried_dropped)."""
    import os as _osm
    _dbg = _osm.environ.get("O4_RECT_DROP_DEBUG") == "1"
    kept: list = []
    n_rwy = n_buried = 0
    for _cl in centerlines:
        ls = _cl.line if hasattr(_cl, "line") else _cl[0]
        ref = _cl.name if hasattr(_cl, "name") else (_cl[1] if len(_cl) > 1 else "")
        if rwy_union is not None and not rwy_union.is_empty:
            try:
                inter = ls.intersection(rwy_union)
                if getattr(inter, "length", 0.0) > _RWY_CROSS_DROP_M:
                    n_rwy += 1
                    if _dbg:
                        print(f"[cl-drop] RWY ref={ref} len={ls.length:.0f} "
                              f"bounds={tuple(round(v,1) for v in ls.bounds)}")
                    continue
            except _GEOM_EXC:
                pass
        if (pav_union is not None and not pav_union.is_empty
                and _median_perp_halfwidth(ls, pav_union)
                >= _BURIED_HALFWIDTH_M):
            n_buried += 1
            if _dbg:
                print(f"[cl-drop] BURIED ref={ref} len={ls.length:.0f} "
                      f"bounds={tuple(round(v,1) for v in ls.bounds)}")
            continue
        kept.append(_cl)
    return kept, n_rwy, n_buried


def _split_centerlines_at_points(
    centerlines: list[tuple[LineString, str]],
    split_points: list[tuple[float, float]],
    approach_tol_m: float = 25.0,
    endpoint_guard_m: float = 5.0,
    pav_union: Polygon | None = None,
    rwy_union: Polygon | None = None,
    rwy_centerlines: list[LineString] | None = None,
) -> list[tuple[LineString, str]]:
    """Split each centerline at intersection points; emit between-
    break rects (15 % margin normally, 30 % for non-perpendicular
    taxis).

    Per user (2026-04-20 refined + 2026-04-21): rects are defined
    by intersections + sharp curves.  Two consecutive cut params
    merge into ONE junction region if the pavement at their
    midpoint is WIDER than the centerline's own narrow half-width
    (factor 1.2) — i.e. the pavement is widening in between
    (intersection widening).  Otherwise they remain separate
    junctions with a rect emitted between them.  This replaces
    the previous fixed ``CLOSE_INTERSECTION_M`` distance
    threshold, which was too coarse: 200 m was needed for
    OSM-fragmented V3 on V but merged real Q/R 3-rect splits too.

    Non-perpendicular taxis (45° stubs like V3) use a GAP_MARGIN_FRAC
    of 0.30 instead of 0.15 because the intersection point on the
    primary and on the runway sit farther down the taxi's own axis
    — without the larger margin the rect overlaps both junctions.
    """
    from .rects import _natural_half_width
    if not centerlines:
        return centerlines
    from shapely.ops import substring

    pav_for_probe = pav_union
    if pav_for_probe is not None and rwy_union is not None:
        try:
            pav_for_probe = pav_for_probe.union(rwy_union)
        except _GEOM_EXC:
            pass

    # Per user 2026-04-28: at sub-segment endpoints that are SHARED
    # with another sub-segment endpoint (i.e. the centerline was
    # bend-split there in ``_extract_osm_taxi_centerlines``), the
    # natural break is the bend itself.  A small junction polygon
    # at the bend is unavoidable (rect axes are straight; bent
    # centerlines need a junction at every angle change).  But the
    # default 15-30% margin on the segment overshoots the bend by
    # tens of metres, leaving a long uncovered corridor that the
    # downstream junction balloons into.  At bend-shared endpoints
    # use a small fixed margin (``BEND_ENDPOINT_MARGIN_M``) instead
    # of the percentage so the rect extends right up to the bend.
    #
    # Per user 2026-05-15: that 5 m bend-shared margin is correct
    # for SAME-REF bends (where two sub-segments of one continuous
    # taxiway meet at an angle change — only the tiny natural
    # triangular junction at the angle is needed).  It is WRONG
    # for MULTI-REF chart-level junctions (where ≥ 2 distinct
    # taxiway names converge, or a taxiway meets a runway) — the
    # 5 m override forces adjacent rects to KISS at the junction,
    # leaving no room for a proper junction polygon and producing
    # rect bodies that extend into the junction area (CYXY -10007
    # SE corner 26 m from chart junction node 141, when the user
    # expects it 50 m+ away to leave room for the D-E junction
    # polygon).  At chart-level junctions, force a larger margin
    # (``CHART_JUNCTION_MARGIN_M``) so the rect ends well before
    # the junction position.  This margin REPLACES the bend-shared
    # 5 m override AND the percentage margin when at chart-junction
    # (the chart geometry, not the angle change, is what bounds
    # the rect here).
    BEND_ENDPOINT_MARGIN_M = _BEND_ENDPOINT_MARGIN_M
    # Per user (session 44): reduced 25 → 15 m so rects run a little
    # longer toward chart junctions (now safe to do because rects are
    # placed against the actual pavement edges via the asymmetric
    # half-width logic, not centred on a possibly-off-centre axis).
    CHART_JUNCTION_MARGIN_M = _CHART_JUNCTION_MARGIN_M
    BEND_SHARED_TOL_M = 25.0
    bend_share_tol2 = BEND_SHARED_TOL_M * BEND_SHARED_TOL_M
    chart_junction_tol2 = BEND_SHARED_TOL_M * BEND_SHARED_TOL_M
    centerline_endpoints: list[tuple[tuple[float, float],
                                     tuple[float, float]]] = []
    for ls, _ref in centerlines:
        try:
            cs = list(ls.coords)
            centerline_endpoints.append((cs[0], cs[-1]))
        except _GEOM_EXC:
            centerline_endpoints.append(((0.0, 0.0), (0.0, 0.0)))

    def _is_bend_shared(idx: int, endpoint: tuple[float, float]) -> bool:
        """True iff ``endpoint`` of centerline ``idx`` lies within
        ``BEND_SHARED_TOL_M`` of any other centerline's endpoint —
        signalling that the two centerlines were bend-split apart
        from one continuous OSM way at that point."""
        ex, ey = endpoint
        for j, (s2, e2) in enumerate(centerline_endpoints):
            if j == idx:
                continue
            for px, py in (s2, e2):
                if (ex - px) ** 2 + (ey - py) ** 2 <= bend_share_tol2:
                    return True
        return False

    def _is_chart_junction(endpoint: tuple[float, float]) -> bool:
        """True iff ``endpoint`` is within ``BEND_SHARED_TOL_M`` of
        any chart-level junction position (passed in via
        ``split_points`` — apt.dat nodes referenced by ≥ 2 distinct
        taxi names OR endpoints of any runway-typed edge).  This
        distinguishes a multi-ref intersection (needs a sizable
        junction polygon between adjacent rect bodies) from a
        same-ref bend (a small triangular junction is sufficient).
        """
        if not split_points:
            return False
        ex, ey = endpoint
        for (sx, sy) in split_points:
            if (ex - sx) ** 2 + (ey - sy) ** 2 <= chart_junction_tol2:
                return True
        return False

    def _avg_perp_halfwidth(ls: LineString, t: float) -> float:
        """(left+right)/2 perpendicular half-width at axis param t,
        so widening detection is comparable to narrow_hw (which is
        also derived from (left+right)/2 per-probe averages)."""
        if pav_for_probe is None or pav_for_probe.is_empty:
            return 0.0
        RAY_CAP_M = 40.0
        RAY_STEP_M = 0.5
        dt = min(2.0, ls.length * 0.05)
        a = ls.interpolate(max(0.0, t - dt))
        b = ls.interpolate(min(ls.length, t + dt))
        tx, ty = b.x - a.x, b.y - a.y
        mag = math.hypot(tx, ty)
        if mag < 1e-6:
            return 0.0
        ux, uy = tx / mag, ty / mag
        nx, ny = -uy, ux
        pt = ls.interpolate(t)
        sides: list[float] = []
        for sign in (-1, 1):
            side = RAY_CAP_M
            d = 0.0
            while d <= RAY_CAP_M:
                qx = pt.x + sign * nx * d
                qy = pt.y + sign * ny * d
                if not pav_for_probe.contains(Point(qx, qy)):
                    side = d
                    break
                d += RAY_STEP_M
            sides.append(side)
        return sum(sides) / 2.0 if sides else 0.0

    def _rect_margin_frac_for(ls: LineString, ref: str) -> float:
        # Stubs / cross-connectors oriented > 30° off perpendicular
        # to the nearest runway get a larger margin because the
        # intersection points (primary and runway) sit farther along
        # the taxi's axis due to the oblique crossing.
        if not rwy_centerlines:
            return 0.15
        c = list(ls.coords)
        if len(c) < 2:
            return 0.15
        dx = c[-1][0] - c[0][0]
        dy = c[-1][1] - c[0][1]
        mag = math.hypot(dx, dy)
        if mag < 1e-6:
            return 0.15
        axis_bearing = math.degrees(math.atan2(dx, dy)) % 180.0
        # Nearest runway to centerline mid
        mid = ls.interpolate(ls.length / 2)
        best_r = None
        best_d = float("inf")
        for r in rwy_centerlines:
            d = mid.distance(r)
            if d < best_d:
                best_d = d
                best_r = r
        if best_r is None:
            return 0.15
        rc = list(best_r.coords)
        if len(rc) < 2:
            return 0.15
        rx = rc[-1][0] - rc[0][0]
        ry = rc[-1][1] - rc[0][1]
        rmag = math.hypot(rx, ry)
        if rmag < 1e-6:
            return 0.15
        rwy_bearing = math.degrees(math.atan2(rx, ry)) % 180.0
        delta = abs(axis_bearing - rwy_bearing)
        delta = min(delta, 180.0 - delta)
        # Perpendicular = 90°.  Taxis that connect to the runway
        # AT AN ANGLE (not parallel, not perpendicular) get the
        # diagonal-stub treatment: fixed 30 m margin on each end
        # of the centerline gap, no bias (user 2026-05-12 — was
        # 35 %-retained biased-away; the bias overshot short
        # stubs into adjacent primary_parallel territory).  Stub
        # length = gap − 60 m; dropped if below the 40 m emit
        # floor.  Primary
        # parallels (delta ≈ 0°, perp_diff ≈ 90°) stay at 15 %;
        # perpendicular cross-connectors (delta ≈ 90°,
        # perp_diff ≈ 0°) stay at 15 %.  B/C/E/G at SPJC measure
        # perp_diff ≈ 69° — just outside the old 25-65 window —
        # so widen to 20-75 to cover the full "at an angle"
        # band while still excluding pure parallels and
        # perpendiculars.
        #
        # Per user 2026-04-27: this used to be gated on length
        # < 250 m (long diagonals fell back to 15 %).  But long
        # diagonals like SPJC's B/C/E (575 m / 572 m / 593 m at
        # ~21° to runway) ARE diagonal stubs in the same sense as
        # short ones — they should get the same 35 % retention so
        # the rect doesn't extend deep into the adjacent apron.
        # Removed the length gate; the angle alone classifies.
        perp_diff = abs(delta - 90.0)
        if 20.0 < perp_diff < 75.0:
            # Per user 2026-04-28: the diagonal-stub margin is
            # appropriate ONLY when at least one endpoint sits near
            # the runway — i.e. the segment IS the stub between
            # a parallel taxi and the runway.  When NEITHER endpoint
            # is near a runway, the segment is a non-stub diagonal
            # connector (e.g. CYXY E nodes 2-4 transitioning between
            # the south-of-apron parallel section and the apron-
            # internal parallel section, perp_diff ≈ 60° but both
            # ends far from any runway).  Such segments shouldn't
            # get the stub treatment; they're not bordered by
            # junctions on both sides.
            #
            # Per user 2026-05-12: threshold bumped 50 → 80 m to
            # cover centerlines that have just been trimmed back
            # by the 30 m runway-buffer pull-back in pipeline.py
            # (runway_centerline-to-trimmed-endpoint distance =
            # half_width + 30 = 22.5 + 30 ≈ 52.5 m).  At 50 m the
            # buffer-trimmed endpoint fell just past the threshold
            # and the segment was reclassified as a non-stub
            # diagonal connector, dropping the 30 m fixed-margin
            # treatment and causing V3 at SPJC to disappear.
            STUB_ENDPOINT_RUNWAY_M = 80.0
            ep0 = Point(c[0])
            ep1 = Point(c[-1])
            ep0_near = any(
                ep0.distance(r) <= STUB_ENDPOINT_RUNWAY_M
                for r in rwy_centerlines)
            ep1_near = any(
                ep1.distance(r) <= STUB_ENDPOINT_RUNWAY_M
                for r in rwy_centerlines)
            if ep0_near or ep1_near:
                return 0.30
            # Neither endpoint near a runway — treat as a long
            # diagonal connector with the parallel-style 15 % margin.
        # Short unrefed parallel-to-runway rect (perp_diff >= 75°,
        # length < 150 m) sitting between two diagonal stubs on
        # SPLP's south chain — apply 30 % margin each side so the
        # resulting primary rect is half its default length and
        # doesn't overlap the adjacent diagonals.
        if not ref and ls.length < 150.0 and perp_diff >= 75.0:
            return 0.30
        return 0.15

    # Per user 2026-05-16: a split point should mark a place where
    # MORE THAN ONE taxiway centerline crosses (a true intersection)
    # or where a taxi crosses a runway.  The upstream
    # ``taxi_junction_points`` also returns same-name graph forks
    # (case ``degree_per_name[(nid, n)] >= 3`` — e.g. a 3-way branch
    # internal to one taxiway's apt.dat graph) which are NOT
    # geometric crossings of different centerlines.  At SPLP, A's
    # graph has same-name forks at (-195,-108) and (3,501) that
    # split A's centerline into short segments and produce phantom
    # mid-chain stub rects.  Pre-filter ``split_points`` to keep
    # only points where ≥ 2 distinct centerline refs pass within
    # ``approach_tol_m`` (treating unnamed connectors as a distinct
    # sentinel ref so a named taxi + connector counts), or where a
    # runway centerline passes within the same tolerance.
    validated_split_points: list[tuple[float, float]] = []
    if split_points:
        _CONN_SENTINEL = "_conn"
        for (sx, sy) in split_points:
            sp = Point(sx, sy)
            refs_near: set = set()
            for ls_v, ref_v in centerlines:
                try:
                    if ls_v.distance(sp) <= approach_tol_m:
                        refs_near.add(ref_v if ref_v else _CONN_SENTINEL)
                        if len(refs_near) >= 2:
                            break
                except _GEOM_EXC:
                    continue
            if len(refs_near) >= 2:
                validated_split_points.append((sx, sy))
                continue
            if rwy_centerlines:
                near_runway = False
                for rw in rwy_centerlines:
                    try:
                        if rw.distance(sp) <= approach_tol_m:
                            near_runway = True
                            break
                    except _GEOM_EXC:
                        continue
                if near_runway:
                    validated_split_points.append((sx, sy))

    result: list[tuple[LineString, str]] = []
    for ls_idx, (ls, ref) in enumerate(centerlines):
        gap_margin_frac = _rect_margin_frac_for(ls, ref)
        # Detect whether the centerline's start / end is a bend-shared
        # endpoint (continuation with a neighbouring sub-segment via
        # bend).  Used below to clamp the margin at those endpoints.
        try:
            _ls_cs = list(ls.coords)
            _start_endpoint = _ls_cs[0]
            _end_endpoint = _ls_cs[-1]
        except _GEOM_EXC:
            _start_endpoint = (0.0, 0.0)
            _end_endpoint = (0.0, 0.0)
        start_is_bend = _is_bend_shared(ls_idx, _start_endpoint)
        end_is_bend = _is_bend_shared(ls_idx, _end_endpoint)
        # Estimate centerline's narrow half-width for midpoint check.
        if pav_for_probe is not None and not pav_for_probe.is_empty:
            _nat, _p90, narrow_hw = _natural_half_width(ls, pav_for_probe)
        else:
            narrow_hw = 0.0

        # Collect cut params for intersections that lie on this line.
        cut_params: list[float] = []
        for (sx, sy) in validated_split_points or ():
            sp = Point(sx, sy)
            if ls.distance(sp) > approach_tol_m:
                continue
            try:
                param = ls.project(sp)
            except _GEOM_EXC:
                continue
            if param < endpoint_guard_m:
                continue
            if param > ls.length - endpoint_guard_m:
                continue
            cut_params.append(param)

        cut_params.sort()
        # Pav-width midpoint cluster: two consecutive cut_params
        # merge when the midpoint half-width > narrow_hw × 1.2.
        # Always merge when they're within 25 m (same-crossing
        # multi-node noise).  Never merge past 400 m apart.
        clusters: list[list[float]] = []
        WIDEN_FACTOR = 1.2
        MIN_ALWAYS_MERGE = 25.0
        MAX_CLUSTER_SPAN_M = 400.0
        for p in cut_params:
            if not clusters:
                clusters.append([p])
                continue
            prev = clusters[-1][-1]
            gap = p - prev
            merge = False
            if gap <= MIN_ALWAYS_MERGE:
                merge = True
            elif gap > MAX_CLUSTER_SPAN_M:
                merge = False
            elif narrow_hw > 0:
                try:
                    mid_hw = _avg_perp_halfwidth(ls, (prev + p) / 2.0)
                    if mid_hw > narrow_hw * WIDEN_FACTOR:
                        merge = True
                except _GEOM_EXC:
                    pass
            if merge:
                clusters[-1].append(p)
            else:
                clusters.append([p])

        breaks: list[float] = [0.0]
        for cl in clusters:
            breaks.append(cl[0])
            breaks.append(cl[-1])
        breaks.append(ls.length)

        # Enumerate candidate segments and identify which are the
        # first/last ones that would actually emit.  Cross-connector
        # taxis (perpendicular to the runway, terminating into wider
        # parallel taxis) use 30 % margin each side on first/last
        # emitted segments because the parallel-taxi widening zone
        # extends into the cross-connector's axis.  Detected by
        # geometry: bearing within 20° of perpendicular to nearest
        # runway, axis midpoint > 250 m from runway centerline.
        n_breaks = len(breaks)
        # Collect only segments that will actually emit (post-margin
        # length >= 40 m under ANY margin we might apply, so first/
        # last indexing is stable).  The 40 m floor matches the
        # post-emit filter below.  Use smallest possible retained
        # fraction (35 % for diagonal, 40 % for cross-connector
        # ends) to test.
        candidates: list[tuple[float, float]] = []
        for i in range(0, n_breaks - 1, 2):
            p0, p1 = breaks[i], breaks[i + 1]
            gap = p1 - p0
            if gap < MIN_SEGMENT_LEN_M:
                continue
            # Will this segment emit under any plausible margin?
            # Diagonal stubs (gap_margin_frac >= 0.25) use fixed
            # 30 m margins, falling back to proportional 0.15 each
            # side when the fixed margins would consume too much
            # of a short gap (CYXY D-west, user 2026-05-15).  Test
            # eligibility against the fallback retention so short
            # diagonal stubs are kept as candidates.
            if gap_margin_frac >= 0.25:
                # Fixed 30 m × 2 OR fallback 0.15 × 2 = 0.70 retained.
                fallback_retained = gap * 0.70
                fixed_retained = gap - 60.0
                best_retained = max(fallback_retained, fixed_retained)
                if best_retained < 40.0:
                    continue
            else:
                min_retained_frac = 0.35
                if (gap * min_retained_frac < 40.0
                        and gap * (1 - 2 * gap_margin_frac) < 40.0):
                    continue
            candidates.append((p0, p1))
        # Cross-connector detection: full centerline is perpendicular
        # (within 20°) to nearest runway AND its midpoint is > 250 m
        # from any runway centerline (i.e. it's a connector BETWEEN
        # parallels, not a runway-touching stub).
        is_cross = False
        if rwy_centerlines:
            try:
                lc = list(ls.coords)
                if len(lc) >= 2:
                    ldx = lc[-1][0] - lc[0][0]
                    ldy = lc[-1][1] - lc[0][1]
                    lmag = math.hypot(ldx, ldy)
                    if lmag > 1e-6:
                        l_bearing = math.degrees(
                            math.atan2(ldx, ldy)) % 180.0
                        mid = ls.interpolate(ls.length / 2.0)
                        rmid = min(rwy_centerlines,
                                   key=lambda r: mid.distance(r))
                        rcc = list(rmid.coords)
                        if len(rcc) >= 2:
                            rdx2 = rcc[-1][0] - rcc[0][0]
                            rdy2 = rcc[-1][1] - rcc[0][1]
                            rmag2 = math.hypot(rdx2, rdy2)
                            if rmag2 > 1e-6:
                                r_bearing = math.degrees(
                                    math.atan2(rdx2, rdy2)) % 180.0
                                d = abs(l_bearing - r_bearing)
                                d = min(d, 180.0 - d)
                                d_perp = abs(d - 90.0)
                                d_to_rwy = mid.distance(rmid)
                                if d_perp <= 20.0 and d_to_rwy > 250.0:
                                    is_cross = True
            except _GEOM_EXC:
                pass
        for idx, (p0, p1) in enumerate(candidates):
            gap = p1 - p0
            is_end_seg = (idx == 0 or idx == len(candidates) - 1)
            # Per-segment parallel check: a SHORT SLICE of a long
            # unrefed curving taxi can be parallel-to-runway even
            # when the full centerline's orientation is diagonal.
            # Classify such a slice for the "short primary between
            # diagonals" shrinkage (30 % margin each side, no bias)
            # — fixes SPLP's (-500,-1054) which sits between two
            # diagonal stubs and would otherwise overlap them.
            is_short_parallel_slice = False
            if (not ref and gap < 150.0 and rwy_centerlines):
                try:
                    seg_a = ls.interpolate(p0)
                    seg_b = ls.interpolate(p1)
                    sdx = seg_b.x - seg_a.x
                    sdy = seg_b.y - seg_a.y
                    smag = math.hypot(sdx, sdy)
                    if smag > 1e-6:
                        seg_bearing = math.degrees(
                            math.atan2(sdx, sdy)) % 180.0
                        rseg_best = min(
                            rwy_centerlines,
                            key=lambda r: ls.interpolate(
                                (p0 + p1) / 2.0).distance(r))
                        rseg = list(rseg_best.coords)
                        rdx = rseg[-1][0] - rseg[0][0]
                        rdy = rseg[-1][1] - rseg[0][1]
                        rmag2 = math.hypot(rdx, rdy)
                        if rmag2 > 1e-6:
                            seg_rwy_bearing = math.degrees(
                                math.atan2(rdx, rdy)) % 180.0
                            seg_delta = abs(
                                seg_bearing - seg_rwy_bearing)
                            seg_delta = min(
                                seg_delta, 180.0 - seg_delta)
                            seg_perp = abs(seg_delta - 90.0)
                            if seg_perp >= 75.0:
                                is_short_parallel_slice = True
                except _GEOM_EXC:
                    pass
            if is_short_parallel_slice:
                # Shrink to half length, no bias (it's parallel,
                # not diagonal).  Using 22 % margin each side
                # (56 % retained) keeps the piece above the 40 m
                # post-margin floor even for short 60-70 m slices
                # of SPLP's main taxi.
                m_start = 0.22 * gap
                m_end = 0.22 * gap
            elif is_cross and is_end_seg:
                # The 30 % end margin clears the parallel-taxi widening
                # zone where a cross-connector meets the parallel taxi.
                # That zone is a FIXED physical distance (~one taxiway
                # width + fillet), not a fraction of the connector's
                # length, so cap it at CROSS_END_MARGIN_MAX_M.  Without
                # the cap a long segment mis-classified as a cross-
                # connector (HECA taxiway L: 1165 m, perpendicular-ish
                # to a runway and > 250 m from it) loses 0.30·1165 =
                # 349 m off its start — dropping target coverage of L
                # from 100 % to 71 %.  Short genuine cross-connectors
                # (gap < ~167 m, where 0.30·gap < 50 m) are unaffected.
                CROSS_END_MARGIN_MAX_M = 50.0
                m_start = min(0.30 * gap, CROSS_END_MARGIN_MAX_M)
                m_end = min(0.30 * gap, CROSS_END_MARGIN_MAX_M)
            elif gap_margin_frac >= 0.25:
                # Non-perpendicular diagonal stub (V3-like).
                #
                # Per user 2026-05-12: centered, no bias.  Fixed
                # 30 m margin on each end so the junction polygons
                # at the runway-facing and apron-facing ends have
                # room to follow the pavement curves that lead into
                # the diagonal.  Stub length = gap − 60 m; if that
                # would fall below the 40 m emit floor, the
                # downstream filter drops the stub.
                #
                # Earlier iterations of this rule used a 35 %-retained
                # margin biased 20 % of the gap TOWARD or AWAY from
                # the runway.  Both directions had problems: TOWARD
                # placed the rect's runway-corner at the runway edge
                # (corner-snap collapsed the adjacent junction to a
                # sliver); AWAY pushed the apron-end of short stubs
                # into adjacent primary_parallel territory, where
                # the absorption pass dropped the rect entirely
                # (V3 at SPJC).  Fixed 30 m each side, no bias, is
                # length-independent and gives junction polygons a
                # predictable approach corridor on both sides.
                # Long diagonal segments running into an apron need MORE than
                # the length-independent 30 m to clear the junction curve (SPJC
                # stub B, 256 m: 30 m back still straddles the A/A1 apron curve
                # → two parallel rects).  Use max(30 m, 0.20·gap) so long
                # diagonals trim back enough; short ones keep the fixed 30 m
                # (proportional only bites once 0.20·gap > 30, i.e. gap > 150 m).
                STUB_END_MARGIN_M = 30.0
                m_start = max(STUB_END_MARGIN_M, 0.20 * gap)
                m_end = max(STUB_END_MARGIN_M, 0.20 * gap)
                # Per user 2026-05-15: when the fixed 30 m margins
                # would consume too much of a short gap (e.g. a
                # pre-split sub-polyline between adjacent junctions
                # where the chart pavement is only 70-120 m long
                # after corridor trim), fall back to a proportional
                # margin so the diagonal stub can still emit as a
                # small rect.  Without this fallback, CYXY taxiway
                # D-west (post-corridor-trim 71 m, two pre-split
                # bend-split halves) drops both halves to the 15 m
                # MIN_SEGMENT_LEN_M floor — leaving the pavement
                # between E_split and the runway-D junction as
                # residue absorbed into the SE apron U-junction.
                # The 40 m emit-floor below still rejects truly
                # tiny stubs.
                if gap - m_start - m_end < 40.0:
                    m_start = 0.15 * gap
                    m_end = 0.15 * gap
                if gap - m_start - m_end < MIN_SEGMENT_LEN_M:
                    continue
            else:
                m_start = gap_margin_frac * gap
                m_end = gap_margin_frac * gap
            # Per user 2026-04-28: at bend-shared centerline endpoints,
            # the rect should extend right up to the bend (only the
            # tiny natural triangular junction at the angle change is
            # unavoidable).  Override the percentage margin with a
            # small fixed value when the corresponding endpoint of
            # this segment touches a bend-shared end of the
            # centerline.
            #
            # BUT: cap the extension at the point where the corridor
            # widens past 1.3 × narrow_hw — past that the rect would
            # extend deep into an apron, fail the apron-interior
            # check in ``_build_taxi_rects`` (≥ 2 corners off-
            # boundary), and never be emitted (e.g. CYXY's North F
            # bend-extends 57 m into an apron).  Walk inward from
            # the centerline's end probing the half-width; stop
            # where the corridor is back to within 1.3 × narrow_hw.
            CORRIDOR_WIDTH_FACTOR = 1.3
            def _bend_margin_at(end_param: float, sign: int,
                                base: float = BEND_ENDPOINT_MARGIN_M
                                ) -> float:
                """``end_param`` = 0 (start) or ls.length (end);
                ``sign`` = +1 (walk forward into the line) or -1
                (walk backward).  Returns the margin (≥ ``base``) at
                which the corridor first narrows back to
                ``CORRIDOR_WIDTH_FACTOR × narrow_hw`` walking inward
                from the endpoint, or ``inf`` if it never narrows
                within half the gap.  ``base`` is the minimum margin
                kept regardless (5 m at a same-ref bend; the larger
                ``CHART_JUNCTION_MARGIN_M`` at a chart junction, which
                still needs room for its polygon)."""
                if narrow_hw <= 0:
                    return base
                # Corridor-BODY half-width reference (user 2026-06-13,
                # HECA G): ``narrow_hw`` is the p10 over the WHOLE ref,
                # so where this ref runs shoulder-less elsewhere it is
                # far below the local corridor width.  A uniformly WIDE
                # run here (taxiway + apt.dat/DSF shoulder pavement)
                # then never narrows to ``narrow_hw × 1.3``, so the
                # walk treats the entire wide run as a junction approach
                # and trims hundreds of metres of real taxiway into
                # apron (G: 375 m end-trim dropped the gap_param-880
                # corridor into apron #253, the 19.9 % wall).  Reference
                # the LOCAL body width (median of interior probes) so
                # only a widening PAST the corridor's own body counts as
                # a junction; a genuine wide junction at the end (J1,
                # ~240 m) is still wider than the body and trims to where
                # it narrows back to the corridor.
                body_probes = []
                for _fr in (0.35, 0.5, 0.65):
                    try:
                        _bh = _avg_perp_halfwidth(ls, p0 + gap * _fr)
                    except _GEOM_EXC:
                        _bh = 0.0
                    if _bh > 0:
                        body_probes.append(_bh)
                body_hw = (sorted(body_probes)[len(body_probes) // 2]
                           if body_probes else narrow_hw)
                target_hw = max(narrow_hw, body_hw) * CORRIDOR_WIDTH_FACTOR
                STEP = 5.0
                MAX = max(base, gap / 2.0)
                u = base
                while u <= MAX:
                    t = end_param + sign * u
                    if t < 0 or t > ls.length:
                        break
                    try:
                        hw_here = _avg_perp_halfwidth(ls, t)
                    except _GEOM_EXC:
                        hw_here = 0.0
                    if 0 < hw_here <= target_hw:
                        return u
                    u += STEP
                # Corridor never narrowed to ≤ target_hw within
                # half the gap — fall back to the percentage margin
                # so the rect doesn't extend into apron territory.
                return float('inf')
            # Margin selection at each endpoint:
            #
            #   * Chart-level junction (multi-ref or runway-touch):
            #     REPLACE m_* with CHART_JUNCTION_MARGIN_M.  The
            #     chart-junction polygon needs room — the bend-
            #     shared 5 m fallback would force adjacent rects
            #     to overlap at the junction position.
            #
            #   * Same-ref bend-shared (no chart junction): use
            #     the small bend-margin so the rect extends right
            #     up to the angle change (only a tiny triangular
            #     junction is unavoidable).
            #
            #   * Neither: use the percentage margin (already set
            #     in m_start / m_end above).
            if abs(p0) < 0.5:
                if _is_chart_junction(_start_endpoint):
                    # Corridor-aware (session 44): extend the rect up
                    # to where the pavement actually widens into the
                    # junction, floored at CHART_JUNCTION_MARGIN_M, so
                    # a connector running between two distant junctions
                    # isn't trimmed by a blind percentage of the long
                    # inter-junction gap (SPLP taxiway B: 0.15·gap was
                    # ~72 m of trim where the corridor stays narrow to
                    # within ~40 m of the junction).  Fall back to the
                    # percentage margin only if the corridor never
                    # narrows (rect would otherwise reach into apron).
                    cm = _bend_margin_at(0.0, +1,
                                         base=CHART_JUNCTION_MARGIN_M)
                    m_start = (cm if cm != float('inf')
                               else max(CHART_JUNCTION_MARGIN_M, m_start))
                elif start_is_bend:
                    bm = _bend_margin_at(0.0, +1)
                    if bm != float('inf'):
                        m_start = min(m_start, bm)
            if abs(p1 - ls.length) < 0.5:
                if _is_chart_junction(_end_endpoint):
                    cm = _bend_margin_at(ls.length, -1,
                                         base=CHART_JUNCTION_MARGIN_M)
                    m_end = (cm if cm != float('inf')
                             else max(CHART_JUNCTION_MARGIN_M, m_end))
                elif end_is_bend:
                    bm = _bend_margin_at(ls.length, -1)
                    if bm != float('inf'):
                        m_end = min(m_end, bm)
            rect_p0 = p0 + m_start
            rect_p1 = p1 - m_end
            if rect_p1 - rect_p0 < MIN_SEGMENT_LEN_M:
                continue
            try:
                piece = substring(ls, rect_p0, rect_p1)
            except _GEOM_EXC:
                continue
            # Drop short between-junction fragments (< 40 m).  Target
            # cross_connector smallest = 52 m, Q smallest = 59 m,
            # so 40 m post-margin is a safe floor that still drops
            # spurious junction-approach tails (e.g. R switchback
            # tails at the V/Q/R triple junction).
            if (piece.geom_type == "LineString"
                    and not piece.is_empty
                    and piece.length >= 40.0):
                result.append((piece, ref))
    return result


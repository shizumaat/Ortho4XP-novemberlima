"""Taxi rect construction from OSM centerlines.

Builds 4-vertex taxi rectangles from the centerline graph produced
by ``O4_Pavement_Centerlines``: probes the underlying apt.dat
pavement to determine the natural half-width along each axis,
extends the rect corners perpendicular to the axis, snaps corners
to the apt.dat boundary, classifies each emitted rect's role
(primary parallel / secondary parallel / stub / cross-connector),
and post-refines based on bearing-to-runway.

This is the OSM-centerline-driven rect builder.  It is distinct
from ``O4_Taxiway_Rects`` (the apt.dat-polygon-to-rect-chain
extractor that uses Voronoi medial-axis centerlines).

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _build_taxi_rects
    _merge_collinear_rects, _merge_collinear_rects_principled
    _natural_half_width, _trim_to_narrow, _probe_axis_width
    _extend_rect_corners_perpendicular
    _rect_from_axis_extended
    _snap_corners_to_pavement
    _cap_rect_length_to_width
    _classify_role, _axis_to_nearest_rwy_db, _refine_roles
"""
from __future__ import annotations

import math
import os

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import nearest_points, unary_union

from ..canonical_points import CanonicalPointRegistry
from ..config import (
    MIN_SEGMENT_LEN_M, MIN_RECT_LENGTH_M, RECT_SQUARE_ENDS,
    RECT_END_SQUARE_TOL_M, taxi_ref_is_sub_index)
from ..geom_safe import min_rotated_rect
from ..layout import (
    ROLE_CROSS_CONNECTOR,
    ROLE_PRIMARY_PARALLEL,
    ROLE_SECONDARY_PARALLEL,
    ROLE_SERVICE_ROAD,
    ROLE_STUB,
    SHARED_VERTEX_TOL_M,
)

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = [
    "EDGE_SNAP_RADIUS_M",
    "VERTEX_SNAP_RADIUS_M",
    "_axis_to_nearest_rwy_db",
    "_build_taxi_rects",
    "_cap_rect_length_to_width",
    "_classify_role",
    "_extend_rect_corners_perpendicular",
    "_merge_collinear_rects",
    "_merge_collinear_rects_principled",
    "_natural_half_width",
    "_probe_axis_width",
    "_rect_from_axis_extended",
    "_refine_roles",
    "_snap_corners_to_pavement",
    "_snap_rect_sloping_edges_to_holes",
    "_trim_to_narrow",
]


def _build_taxi_rects(
    centerlines: list[tuple[LineString, str]],
    pav_union: Polygon | None,
    rwy_union: Polygon | None,
    rwy_centerlines: list[LineString],
    apt_vertices: list[tuple[float, float]] | None = None,
    ref_overall_bearings: dict[str, float] | None = None,
    registry: CanonicalPointRegistry | None = None,
    svc_widths: dict[str, float] | None = None,
) -> list[tuple[Polygon, LineString, str, str]]:
    """Convert each usable centerline into a 4-vertex rect.

    For each centerline we:
      1. Probe the half-width (distance to apt.dat pavement boundary)
         at many points along the axis.
      2. Determine the ``natural half-width`` of the strip as the
         median of those probes.
      3. TRIM axis endpoints inward until the probe there is
         ≤ 1.3 × natural_half_width — this is the user's rule of
         "pull rects back to the narrowest part of the taxiway."
         Everything past the trim is widening territory, reserved
         for junction polygons.
      4. Emit a 4-vertex rect over the trimmed axis with width =
         2 × natural_half_width.  The rect's 4 corners sit at the
         trimmed axis endpoints ± perpendicular half-width.

    Returns list of (rect, clipped_axis, role, ref).
    """
    if pav_union is None:
        return []

    # Per user 2026-05-05: single source of truth for the pavement
    # boundary.  Previously this function did its own additional
    # runway subtraction (``pav_non_rwy = pav_union - rwy_union``)
    # to keep rect corners off runway edges, but that produced a
    # different boundary than ``pav_union`` carries into junction
    # emit — corners snapped here landed on a boundary that doesn't
    # exist when residue is computed.  Use ``pav_union`` directly
    # so corners snap to the same boundary residue subtraction
    # uses.  The ``rwy_union`` parameter is retained for the role
    # / dedup classifier but no longer gates rect geometry.
    pav_non_rwy = pav_union

    # Canonical-point registry: the pipeline builds + seeds the
    # registry on ``layout.canonical_points`` BEFORE rect
    # construction so every shape downstream resolves vertices
    # through the same shared store.  When invoked from a context
    # without a shared registry (tests / tools that call this
    # function directly), build a local one seeded from pav_union
    # + runway corners as a defensive fallback.
    if registry is None:
        registry = CanonicalPointRegistry(tol_m=SHARED_VERTEX_TOL_M)
        registry.seed(_pav_boundary_nodes(pav_union))
        if rwy_union is not None and not rwy_union.is_empty:
            try:
                for poly in (rwy_union.geoms
                             if rwy_union.geom_type == "MultiPolygon"
                             else [rwy_union]):
                    if poly.geom_type != "Polygon":
                        continue
                    ext = list(poly.exterior.coords)
                    if ext and ext[0] == ext[-1]:
                        ext = ext[:-1]
                    registry.seed(ext)
            except _GEOM_EXC:
                pass

    centerlines = sorted(centerlines, key=lambda x: -x[0].length)

    emitted: list[tuple[Polygon, LineString, str, str]] = []
    emitted_union: Polygon | None = None

    for axis, ref in centerlines:
        # Clip to the full pavement (including runway).  The rect may
        # overlap runway slightly at stubs that reach the runway edge;
        # we prefer a stub that correctly reaches the runway over
        # clipping it at the runway boundary and losing most of it.
        try:
            clipped = axis.intersection(pav_union)
        except _GEOM_EXC:
            continue
        if clipped.is_empty:
            continue
        if clipped.geom_type == "MultiLineString":
            clipped = max(clipped.geoms, key=lambda g: g.length)
        if clipped.geom_type != "LineString":
            continue
        if clipped.length < 20.0:
            if ref.startswith("SVC") \
                    and os.environ.get("O4_SVC_DEBUG") == "1":
                print(f"[svc-drop] {ref} axis={axis.length:.0f}: "
                      f"clipped to {clipped.length:.0f} m (<20)")
            continue

        # Probe half-widths along axis.  ``narrow_hw`` (p10) is the
        # strip's narrowest portion; we use it both as the trim
        # baseline AND as the rect's half-width per the user's
        # authoritative rule.  The factor 1.15 means trim ends
        # where pavement grows more than 15% wider than the rect —
        # that extra width is junction territory.
        _natural_hw, _max_hw, narrow_hw = _natural_half_width(
            clipped, pav_non_rwy)
        # (s79) SVC road axes hug the OUTER pavement edge (the truck
        # drives the strip's grass side), so the boundary-distance
        # probe under-reads the half-width.  Use the detection's
        # measured cross-section (``svc_widths``, keyed by run ref)
        # as the floor instead of dropping the lane.
        _is_svc = ref.startswith("SVC")
        _svc_w = (svc_widths or {}).get(ref) if _is_svc else None
        if _svc_w:
            narrow_hw = max(narrow_hw, _svc_w / 2.0)
        # (s79) roads may be narrower than any taxiway (CYXY 'D': 5.3 m
        # ramp) — the SVC floor is 2.5 m half-width, taxi stays 3.5.
        _hw_floor = 2.5 if _is_svc else 3.5
        if narrow_hw < _hw_floor or narrow_hw > 40.0:
            if _is_svc and os.environ.get("O4_SVC_DEBUG") == "1":
                print(f"[svc-drop] {ref} len={clipped.length:.0f}: "
                      f"narrow_hw={narrow_hw:.1f} out of "
                      f"[{_hw_floor},40]")
            continue

        # Width-based endpoint trim (general rule, user 2026-05-30):
        # the intersection split (_split_centerlines_at_points) cuts at
        # network nodes, but a centerline still runs full-length THROUGH
        # apron mouths / widenings that aren't network nodes, so the rect
        # over-runs into junction territory.  Trim each end inward to
        # where the pavement narrows back to the strip width — the
        # widened ends become junction polygons.  This is the dominant
        # lever closing the ~25% over-length gap vs the hand-verified
        # centerline target, and it is purely geometric (no per-airport
        # tuning).
        trimmed = _trim_axis_to_narrow_corridor(
            clipped, pav_non_rwy, narrow_hw)
        trim_narrow_hw = narrow_hw

        # Dedup against trimmed axis.  (s79) SVC road pieces are EXEMPT:
        # a discovered TX rect emitted earlier over the same lane must
        # not silently swallow the road — both emit, and the overlap
        # pass drops the DISCOVERED one (1206 provenance wins, user
        # 2026-06-11).
        if not _is_svc and emitted_union is not None \
                and not emitted_union.is_empty:
            try:
                inside_len = trimmed.intersection(emitted_union).length
                if inside_len / trimmed.length > 0.7:
                    continue
            except _GEOM_EXC:
                pass

        width = 2.0 * trim_narrow_hw
        # Off-centre axis: place the two long edges on the ACTUAL
        # left/right pavement edges (location from the centerline,
        # lateral extent from the pavement) so a centerline that runs
        # near one edge doesn't push a symmetric rect's long edge past
        # the boundary (→ absorption clip).  Below OFFCENTER_ASYM_TOL_M
        # the axis is effectively centred → symmetric path unchanged.
        half_left = half_right = None
        _hl, _hr = _natural_half_widths_lr(trimmed, pav_non_rwy)
        if (_hl is not None and _hr is not None
                and abs(_hl - _hr) > OFFCENTER_ASYM_TOL_M):
            half_left, half_right = _hl, _hr
        rect = _rect_from_axis_extended(trimmed, width, pav_non_rwy,
                                        apt_vertices=apt_vertices,
                                        half_left=half_left,
                                        half_right=half_right,
                                        registry=registry)
        # Diagonal-stub fallback (user 2026-05-12): when the strict
        # symmetric-rect builder rejects a digit-ref centerline whose
        # bearing is diagonal to the runway, retry with
        # accept_asymmetric=True.  Diagonal connectors (V3 at SPJC,
        # similar refs elsewhere) physically flare wide where they
        # meet aprons/parallels; the resulting snapped quad is a
        # trapezoid, not a rectangle, but it carries the correct
        # 4 corner positions to anchor the surrounding junction
        # polygon.  Without this fallback the rect is silently
        # discarded and the junction perimeter draws a 100+ m
        # straight edge across the diagonal pavement.
        if ((rect is None or rect.is_empty)
                and ref and taxi_ref_is_sub_index(ref)
                and rwy_centerlines):
            db_axis = _axis_to_nearest_rwy_db(
                trimmed, rwy_centerlines)
            # (s79) SVC roads always get the asymmetric retry — a road
            # flares where it meets aprons regardless of its bearing to
            # the runway (the bearing test is a taxi-stub heuristic).
            if _is_svc or (db_axis is not None and db_axis >= 20.0):
                rect = _rect_from_axis_extended(
                    trimmed, width, pav_non_rwy,
                    apt_vertices=apt_vertices,
                    accept_asymmetric=True,
                    half_left=half_left,
                    half_right=half_right,
                    registry=registry)
        if rect is None or rect.is_empty \
                or (_is_svc and rect.area < 100.0):
            if _is_svc and os.environ.get("O4_SVC_DEBUG") == "1":
                print(f"[svc-drop] {ref} len={trimmed.length:.0f}: "
                      f"rect builder returned none/degenerate")
            continue
        # Skip invalid rects (self-intersecting after snap).
        if not rect.is_valid:
            try:
                rect = rect.buffer(0)
            except _GEOM_EXC:
                continue
            if (rect.is_empty or rect.geom_type != "Polygon"
                    or not rect.is_valid):
                continue

        # ── Apron-interior rect rejection (user 2026-04-27) ────────
        # A rect whose 4 corners aren't on (or very near) the
        # pavement boundary is sitting INSIDE an apron — the
        # surrounding pavement wraps around it, downstream junction
        # construction has to wrap a junction around it too, and the
        # junction's elevation has to bridge the rect's slope on
        # both long edges → visible elevation ridges in JOSM and at
        # render time.
        #
        # Real taxi rects have their 4 corners at pavement-boundary
        # points (the intersections where the taxi corridor meets
        # the adjacent apron / parallel / runway).  If the rect's
        # corners are well INSIDE the pavement, the centerline runs
        # through an apron and shouldn't emit a separate rect — the
        # apron stays as one continuous junction.
        CORNER_OFF_BOUNDARY_TOL_M = 2.0
        rect_coords = list(rect.exterior.coords)
        if rect_coords and rect_coords[0] == rect_coords[-1]:
            rect_coords = rect_coords[:-1]
        boundary = pav_union.boundary
        n_off_boundary = sum(
            1 for (cx, cy) in rect_coords
            if Point(cx, cy).distance(boundary) > CORNER_OFF_BOUNDARY_TOL_M)
        # (s79) SVC road rects are EXEMPT from the two interior gates
        # below: `detect_road_runs` already guarantees strip-ness
        # (cross-section ≤ cap or a narrow source polygon), and an
        # edge-blended road — its own strip running ALONG apron
        # pavement (CYXY pav[1] "New Taxiway 40", user-confirmed ROAD
        # 2026-06-11) — legitimately has its inner long edge inside
        # the fused pavement.
        if n_off_boundary >= 2 and not _is_svc:
            # ≥ 2 corners away from any pavement edge — the rect
            # sits inside an apron.  Skip it; the apron pavement
            # stays as residue → junction.
            continue

        # ── Long-edge-at-pavement-boundary check (user 2026-05-16) ──
        # Invariant: a rect's two LONG edges (parallel to source_axis)
        # must coincide with the natural pavement boundary on the
        # outside — i.e. just outward of the long edge is NON-pavement
        # (grass / out-of-airport).  If just-outward is still pavement,
        # the rect is sitting in the INTERIOR of a wider pavement area
        # (a junction at a multi-ref intersection) and is mis-sized:
        # its long edges cut THROUGH pavement that should be junction
        # territory, putting "junctions on its sloping edges" (the
        # user-invariant we wanted to enforce) and pulling junction
        # polygons into the rect's slope-grade chain.
        #
        # Concrete case (CYXY D-west, user 2026-05-16): the 155 m
        # D polyline from node 141 (E_split) to node 135 (runway
        # crossing) runs through E-D junction → narrow D taxi →
        # D-runway junction.  Probing for natural half-width gives
        # a wide value (~32 m) because most samples sit in the
        # junction-wide areas — the rect emits as a 64 m × 75 m
        # quasi-rectangle whose long edges sit deep inside junction
        # pavement (with adjacent junctions on both sides).  Drop
        # the rect so the pavement stays as junction residue.
        if not _is_svc and not _rect_long_edges_at_pavement_boundary(
                rect, trimmed, pav_non_rwy):
            continue

        # Apron-blob rejection (user 2026-05-30): corner-snapping pulls
        # the rect's corners onto the pavement boundary, which can
        # INFLATE it far beyond its strip width when the centerline runs
        # through a wide apron — the snapped quad then fills the apron
        # rather than tracing a taxi corridor (HECA U1: narrow_hw 34 →
        # snapped 570x162 m).  A real taxi rect's mean width stays
        # ~2*narrow_hw; a mean width well beyond that means the snap
        # blew it into apron, so leave the pavement as residue/junction.
        # Guarded by an absolute floor so genuinely-narrow rects (whose
        # snap legitimately widens a little) are never touched.
        try:
            mean_w = rect.area / max(trimmed.length, 1e-6)
        except _GEOM_EXC:
            mean_w = width
        if mean_w > 50.0 and mean_w > 1.7 * width:
            continue

        role = _classify_role(trimmed, width, rwy_centerlines,
                               rwy_union, ref=ref,
                               ref_overall_bearings=ref_overall_bearings)
        # (s79) SVC refs are ground-vehicle ROAD centerlines (qualifying
        # apt.dat 1206 runs, docs/service_road_carve.md) — classified by
        # PROVENANCE, not geometry: the 4 % ``service_road`` law applies
        # regardless of bearing/length (a road parallel to the runway is
        # still a road, never a primary_parallel/stub).
        if _is_svc:
            role = ROLE_SERVICE_ROAD
            # PAVEMENT-COVERAGE acceptance (user 2026-06-12, HECA
            # SVC9/18/22/23): SVC rects are exempt from the taxiway
            # interior gates (edge-blended roads need that), so a
            # mis-placed rect — off-centre placement / corner snapping
            # drifting it onto GRASS (HECA SVC23: 0 % on pavement,
            # SVC9: 6 %) — has nothing left to reject it.  A road rect
            # that is not substantially ON pavement must not emit.
            # 0.55 calibration: the user-flagged set measures ≤43 %;
            # reviewed-unflagged borderline pieces sit at 58-66 %.
            try:
                _cov = (rect.intersection(pav_union).area
                        / rect.area if rect.area > 0 else 0.0)
            except _GEOM_EXC:
                _cov = 0.0
            if _cov < 0.55:
                if os.environ.get("O4_SVC_DEBUG") == "1":
                    print(f"[svc-drop] {ref} len={trimmed.length:.0f}: "
                          f"only {_cov*100:.0f}% on pavement")
                continue
        # Per user 2026-05-16: drop unrefed STUB rects whose
        # centerline is short.  Unrefed centerlines come from
        # apt.dat taxi edges with no name — at most airports those
        # are noise (small connector fragments) inside an apron
        # rather than a real stub taxi.  Long unrefed centerlines
        # (e.g. SPLP main taxi, 1.4 - 2.6 km) ARE real primary
        # parallels and pass this filter (they'd already classify
        # as primary_parallel, not stub).  Threshold 150 m: longer
        # than typical apron-edge fragments, shorter than any real
        # named-taxi stub at SPJC / SPLP / CYXY / KBNA / HECA.
        from ..layout import ROLE_STUB
        # SHORT-RECT → JUNCTION (user 2026-06-30, gate O4_MIN_RECT_LENGTH_M):
        # an AIRCRAFT taxi rect below this length is a rigid sloping PLANE where
        # the spine wants to curve through smoothly (HECA's curved taxiways).
        # Don't emit it — the pavement stays junction residue
        # (pav_union.difference(rects)) so the centerline grades through it
        # continuously instead of as planar facets.  Service roads are EXCLUDED:
        # they are car roads (4% cap, own semantics), not taxiways, and must not
        # become aircraft-pavement junctions.
        if (MIN_RECT_LENGTH_M > 0.0 and trimmed.length < MIN_RECT_LENGTH_M
                and role != ROLE_SERVICE_ROAD):
            continue
        from ..config import is_unnamed_taxi_ref
        if (is_unnamed_taxi_ref(ref) and role == ROLE_STUB
                and trimmed.length < 150.0):
            continue
        emitted.append((rect, trimmed, role, ref))
        try:
            emitted_union = (unary_union([emitted_union, rect])
                             if emitted_union is not None else rect)
        except _GEOM_EXC:
            # Self-intersection of accumulated union — skip update.
            pass

    # Per user 2026-05-05: ``_refine_roles`` disabled.  It demoted
    # short perpendicular PRIMARY_PARALLEL segments to STUB; with
    # the 20 m cross_connector threshold those segments classify
    # correctly the first time and the demote rule no longer earns
    # its keep.
    # _refine_roles(emitted, rwy_centerlines)

    # Stub-ref dedup: OSM often has multiple disjoint ways with the
    # same sub-ref label (e.g. V2 has 3 separate OSM pieces, each
    # contributing a rect).  Target emits ONE rect per stub ref.
    # Keep only the LONGEST rect per stub ref.  Applies to both
    # sub-refs (letter+digit like V1, L3) AND letter-only stubs
    # (B, C, E, G) which are single-stub taxis that must not
    # fragment across internal bends.
    def _should_dedup(ref_str: str, role_str: str) -> bool:
        if not ref_str:
            return False
        if ref_str.startswith("SVC"):
            # (s79) SVC digits are run indices, not stub sub-refs — a
            # road run legitimately emits several rects along its bends.
            return False
        if taxi_ref_is_sub_index(ref_str):
            return True
        # Letter-only: dedup when classified as stub (B, C, E, G, D).
        if role_str == ROLE_STUB:
            return True
        return False
    # Per-ref, group same-ref rects into geometrically-overlapping
    # clusters; within each cluster, keep only the longest.  Non-
    # overlapping rects of the same ref (e.g. multiple sub-segments
    # of one OSM way that bend-split into rects covering distinct
    # parts of the corridor) coexist — only OSM fragmentation that
    # produces actual duplicates gets deduped.
    by_ref: dict[str, list[int]] = {}
    for i, (_r, _a, role, ref) in enumerate(emitted):
        if not _should_dedup(ref, role):
            continue
        by_ref.setdefault(ref, []).append(i)
    drop: set = set()
    for ref, idxs in by_ref.items():
        # Build overlap clusters.
        n = len(idxs)
        parent = list(range(n))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for a in range(n):
            ra = emitted[idxs[a]][0]
            for b in range(a + 1, n):
                rb = emitted[idxs[b]][0]
                try:
                    overlap = ra.intersection(rb).area
                except _GEOM_EXC:
                    overlap = 0.0
                # Cluster (→ keep-longest) only on genuine AREA OVERLAP —
                # i.e. two rects share footprint, so one is a duplicate of
                # the other (OSM fragmentation of a single short stub:
                # SPJC F's two pieces overlap 325 m²).  Do NOT cluster on
                # mere proximity: distinct end-to-end SECTIONS of one long
                # taxiway (HECA R/B/C, crossed by connectors into 3-5
                # collinear pieces with junction gaps between them) abut or
                # sit metres apart with ZERO area overlap — they are real,
                # separate rects and must all survive (user 2026-05-31).
                if overlap > 1.0:
                    pa, pb = find(a), find(b)
                    if pa != pb:
                        parent[pa] = pb
        clusters: dict[int, list[int]] = {}
        for k in range(n):
            clusters.setdefault(find(k), []).append(idxs[k])
        # Within each cluster, keep only the longest axis.
        for members in clusters.values():
            if len(members) <= 1:
                continue
            members.sort(key=lambda m: -emitted[m][1].length)
            for m in members[1:]:
                drop.add(m)

    # NOTE (user 2026-05-31): a former "diagonal-parent stub = EXACTLY ONE
    # rect" rule lived here — it dropped all-but-longest for single-letter
    # stubs whose bearing was 20-45° off the runway, on the theory that
    # extra fragments were artefacts.  That was WRONG for long multi-
    # section taxiways: HECA's R/B/C run diagonally to the runway and are
    # crossed by connectors into 3-5 real collinear sections (R wraps both
    # sides of 05C/23C); the rule deleted every section but one, leaving
    # the taxiway on one side only.  Genuine OSM-fragment duplicates share
    # footprint and are already removed by the overlap cluster-dedup above,
    # so this blanket bearing rule is removed entirely.

    keep: list[tuple[Polygon, LineString, str, str]] = [
        item for i, item in enumerate(emitted) if i not in drop]
    return keep


# (session 51) Apron-interior taxilane rects (session-47 EXPERIMENTAL) were
# REMOVED per user 2026-05-27: in the clean no-absorption model, taxi
# centerlines through open apron interiors produce NO rect — the apron
# (= pav_union − rects) wraps the whole footprint as one polygon.  This
# trades directional grading for those middle-of-apron lanes for cleaner
# overall geometry (no fixed-width ribbon chains).  See
# docs/pipeline_invariants.md.




def _trim_end(coords: list[tuple[float, float]], back: float,
              at_start: bool) -> list[tuple[float, float]]:
    """Trim ``back`` metres off the start (or end) of a polyline, leaving
    a bounded gap at a shared routing node so a junction can form there."""
    pts = coords if at_start else coords[::-1]
    if len(pts) < 2:
        return coords
    remaining = back
    out = list(pts)
    while len(out) >= 2 and remaining > 0:
        ax, ay = out[0]
        bx, by = out[1]
        seg = math.hypot(bx - ax, by - ay)
        if seg <= remaining:
            out = out[1:]
            remaining -= seg
        else:
            t = remaining / seg
            out[0] = (ax + t * (bx - ax), ay + t * (by - ay))
            remaining = 0
    if len(out) < 2:
        return coords
    return out if at_start else out[::-1]


def _rect_long_edges_at_pavement_boundary(
    rect: Polygon,
    axis: LineString,
    pav: Polygon,
    offset_m: float = 5.0,
    n_samples: int = 5,
    interior_frac_tol: float = 0.0,
) -> bool:
    """Return True iff at least one of the rect's LONG edges
    (parallel to ``axis``) has its just-outward samples OUTSIDE
    ``pav``.

    Per user 2026-05-16 refined: a rect is rejected as
    "interior-to-pavement" ONLY when BOTH long edges have most of
    their outward probes hit pavement (the rect sits inside a
    multi-ref wide pavement area, like CYXY D-west passing through
    E-D and D-runway junctions).  When only ONE long edge is
    embedded — the partial-apron-adjacency case where the rect IS
    a primary parallel passing alongside an apron — the rect must
    survive so the downstream
    ``_split_primary_parallels_at_pavement_boundary`` can clip
    the embedded prefix/suffix and keep the unbounded middle as a
    shorter rect.  Canonical case: CYXY taxi E with apron on its
    west side along the NW half, free along the SE half.

    Long edges are identified geometrically: the two of the four
    rect-ring edges whose direction is closest to ``axis``
    direction (cosine > 0.7, i.e. within ~45°).

    ``offset_m`` (default 5 m) is the outward perpendicular probe
    distance — larger than typical boundary precision (~1 m) but
    smaller than typical junction width (~30 m).
    ``interior_frac_tol`` is the fraction of samples allowed
    inside ``pav`` before a long edge counts as "embedded"; the
    rect is rejected only when BOTH long edges exceed this
    threshold.
    """
    if pav is None or pav.is_empty:
        return True
    try:
        axis_coords = list(axis.coords)
    except _GEOM_EXC:
        return True
    if len(axis_coords) < 2:
        return True
    adx = axis_coords[-1][0] - axis_coords[0][0]
    ady = axis_coords[-1][1] - axis_coords[0][1]
    a_mag = math.hypot(adx, ady)
    if a_mag < 1e-6:
        return True
    aux, auy = adx / a_mag, ady / a_mag
    try:
        rect_coords = list(rect.exterior.coords)
    except _GEOM_EXC:
        return True
    if rect_coords and rect_coords[0] == rect_coords[-1]:
        rect_coords = rect_coords[:-1]
    if len(rect_coords) != 4:
        # Not a clean 4-corner rect — defer to other checks.
        return True
    centroid = rect.centroid
    cx, cy = centroid.x, centroid.y
    long_edges_embedded = 0
    long_edges_checked = 0
    for i in range(4):
        a = rect_coords[i]
        b = rect_coords[(i + 1) % 4]
        edx = b[0] - a[0]
        edy = b[1] - a[1]
        e_mag = math.hypot(edx, edy)
        if e_mag < 1e-6:
            continue
        # Long edges: direction within ~45° of axis (cos > 0.7).
        cos_with_axis = abs((edx * aux + edy * auy) / e_mag)
        if cos_with_axis <= 0.7:
            continue
        # Outward perpendicular: from edge midpoint away from
        # rect centroid.
        mx = 0.5 * (a[0] + b[0])
        my = 0.5 * (a[1] + b[1])
        ox = mx - cx
        oy = my - cy
        o_mag = math.hypot(ox, oy)
        if o_mag < 1e-6:
            # Edge midpoint == centroid (degenerate); skip.
            continue
        ox /= o_mag
        oy /= o_mag
        n_inside = 0
        for k in range(n_samples):
            t = (k + 0.5) / n_samples
            sx = a[0] + t * edx + ox * offset_m
            sy = a[1] + t * edy + oy * offset_m
            try:
                if pav.contains(Point(sx, sy)):
                    n_inside += 1
            except _GEOM_EXC:
                continue
        long_edges_checked += 1
        if n_inside / n_samples > interior_frac_tol:
            long_edges_embedded += 1
    # Reject only when BOTH long edges are embedded (a rect that
    # sits in the interior of a wide pavement region).  If only one
    # is embedded, the partial-absorption pass will clip the
    # embedded end and keep the unbounded middle.
    if long_edges_checked >= 2 and long_edges_embedded >= 2:
        return False
    return True


def _merge_collinear_rects_principled(
    emitted: list[tuple[Polygon, LineString, str, str]],
    pav: Polygon,
    apt_vertices: list[tuple[float, float]] | None = None,
    angle_tol_deg: float = 4.0,
    gap_tol_m: float = 12.0,
    width_uniformity_tol: float = 1.10,
) -> list[tuple[Polygon, LineString, str, str]]:
    """Merge adjacent same-ref rects whose joining point shows
    NO widening — the pavement runs straight at uniform narrow
    width through the joint.  This is the only case where "single
    rect between junctions on straight sections" applies.
    """
    if not emitted or pav is None or pav.is_empty:
        return emitted
    boundary = pav.boundary
    changed = True
    work = list(emitted)
    while changed:
        changed = False
        for i in range(len(work)):
            for j in range(i + 1, len(work)):
                ri, ai, roli, refi = work[i]
                rj, aj, rolj, refj = work[j]
                if refi != refj or refi == "":
                    continue  # refless airports handled separately
                if roli != rolj:
                    continue
                # Bearings (mod 180°).
                def _bearing(a):
                    c = list(a.coords)
                    return math.degrees(
                        math.atan2(c[-1][0] - c[0][0],
                                   c[-1][1] - c[0][1])) % 180.0
                bi = _bearing(ai)
                bj = _bearing(aj)
                db = abs(bi - bj)
                db = min(db, 180.0 - db)
                if db > angle_tol_deg:
                    continue
                # Closest endpoints & far endpoints.
                coords_i = list(ai.coords)
                coords_j = list(aj.coords)
                pairs = [
                    (coords_i[0], coords_j[0], 0, 0),
                    (coords_i[0], coords_j[-1], 0, 1),
                    (coords_i[-1], coords_j[0], 1, 0),
                    (coords_i[-1], coords_j[-1], 1, 1),
                ]
                best = min(pairs, key=lambda p: math.hypot(
                    p[0][0] - p[1][0], p[0][1] - p[1][1]))
                endp_i, endp_j, ei, ej = best
                gap = math.hypot(endp_i[0] - endp_j[0],
                                 endp_i[1] - endp_j[1])
                if gap > gap_tol_m:
                    continue
                # Joining-region pavement half-width: probe at the
                # midpoint of the two touching endpoints.
                mid = Point((endp_i[0] + endp_j[0]) / 2,
                            (endp_i[1] + endp_j[1]) / 2)
                hw_joint = mid.distance(boundary) if pav.contains(mid) else 0
                if hw_joint <= 0:
                    continue
                # Each rect's own half-width (MRR short side / 2).
                def _rect_hw(p):
                    mrr = min_rotated_rect(p)
                    c = list(mrr.exterior.coords)
                    if len(c) < 5:
                        return 0.0
                    s1 = math.hypot(c[1][0] - c[0][0], c[1][1] - c[0][1])
                    s2 = math.hypot(c[2][0] - c[1][0], c[2][1] - c[1][1])
                    return min(s1, s2) / 2.0
                hwi = _rect_hw(ri)
                hwj = _rect_hw(rj)
                if hwi <= 0 or hwj <= 0:
                    continue
                # No widening: joint hw is within uniformity_tol of
                # each rect's own hw (equivalently, joint hw ≤
                # max(hwi, hwj) × uniformity_tol AND rects have
                # similar widths).
                max_hw = max(hwi, hwj)
                if hw_joint > max_hw * width_uniformity_tol:
                    continue
                ratio_ij = max(hwi, hwj) / min(hwi, hwj)
                if ratio_ij > width_uniformity_tol:
                    continue
                # All checks pass — merge.
                far_i = coords_i[0] if ei == 1 else coords_i[-1]
                far_j = coords_j[0] if ej == 1 else coords_j[-1]
                try:
                    merged_axis = LineString([far_i, far_j])
                except _GEOM_EXC:
                    continue
                merged_rect = _rect_from_axis_extended(
                    merged_axis, 2.0 * (hwi + hwj) / 2.0, pav,
                    apt_vertices=apt_vertices)
                if merged_rect is None or merged_rect.is_empty:
                    continue
                work[i] = (merged_rect, merged_axis, roli, refi)
                del work[j]
                changed = True
                break
            if changed:
                break
    return work


def _merge_collinear_rects(
    emitted: list[tuple[Polygon, LineString, str, str]],
    pav: Polygon,
    apt_vertices: list[tuple[float, float]] | None = None,
    angle_tol_deg: float = 4.0,
    gap_tol_m: float = 8.0,
    width_ratio_tol: float = 1.15,
) -> list[tuple[Polygon, LineString, str, str]]:
    """Merge adjacent same-ref rects whose axes are nearly
    collinear and whose meeting point sits in the narrow corridor
    (no widening).  Produces one rect per straight pavement
    section between real junctions.
    """
    if not emitted:
        return emitted
    # Group by ref and role so we only merge within a ref's rects.
    merged = True
    work = list(emitted)
    while merged:
        merged = False
        n = len(work)
        for i in range(n):
            for j in range(i + 1, n):
                ri, ai, roli, refi = work[i]
                rj, aj, rolj, refj = work[j]
                # Only merge same ref + compatible role.
                if refi != refj or roli != rolj:
                    continue
                if refi == "" or refj == "":
                    continue  # refless: don't auto-merge (SPLP)
                # Axis bearings (mod 180°).
                def _bearing(a):
                    c = list(a.coords)
                    return math.degrees(
                        math.atan2(c[-1][0] - c[0][0],
                                   c[-1][1] - c[0][1])) % 180.0
                bi = _bearing(ai)
                bj = _bearing(aj)
                db = abs(bi - bj)
                db = min(db, 180.0 - db)
                if db > angle_tol_deg:
                    continue
                # Closest endpoints between axes.
                coords_i = list(ai.coords)
                coords_j = list(aj.coords)
                pairs = [
                    (coords_i[0], coords_j[0], 0, 0),
                    (coords_i[0], coords_j[-1], 0, 1),
                    (coords_i[-1], coords_j[0], 1, 0),
                    (coords_i[-1], coords_j[-1], 1, 1),
                ]
                best = min(pairs, key=lambda p: math.hypot(
                    p[0][0] - p[1][0], p[0][1] - p[1][1]))
                endp_i, endp_j, ei, ej = best
                gap = math.hypot(endp_i[0] - endp_j[0],
                                 endp_i[1] - endp_j[1])
                if gap > gap_tol_m:
                    continue
                # Width similarity: compare rect widths (from their
                # polygons' minimum-rotated-rect short-side).
                def _rect_width(p):
                    mrr = min_rotated_rect(p)
                    coords = list(mrr.exterior.coords)
                    if len(coords) < 5:
                        return 0.0
                    s1 = math.hypot(coords[1][0] - coords[0][0],
                                    coords[1][1] - coords[0][1])
                    s2 = math.hypot(coords[2][0] - coords[1][0],
                                    coords[2][1] - coords[1][1])
                    return min(s1, s2)
                wi = _rect_width(ri)
                wj = _rect_width(rj)
                if wi < 1.0 or wj < 1.0:
                    continue
                if max(wi, wj) / min(wi, wj) > width_ratio_tol:
                    continue
                # Build merged axis: take the 2 FAR endpoints.
                far_i = coords_i[0] if ei == 1 else coords_i[-1]
                far_j = coords_j[0] if ej == 1 else coords_j[-1]
                try:
                    merged_axis = LineString([far_i, far_j])
                except _GEOM_EXC:
                    continue
                # Build merged rect from merged axis at average width.
                avg_width = (wi + wj) / 2.0
                merged_rect = _rect_from_axis_extended(
                    merged_axis, avg_width, pav,
                    apt_vertices=apt_vertices)
                if merged_rect is None or merged_rect.is_empty:
                    continue
                # Replace i with merged, drop j.
                work[i] = (merged_rect, merged_axis, roli, refi)
                del work[j]
                merged = True
                break
            if merged:
                break
    return work


def _natural_half_width(axis: LineString, pav: Polygon,
                        n_probes: int = 15) -> tuple[float, float, float]:
    """Return (natural_hw, max_hw, narrow_hw) LOCAL half-width probes
    along the axis.

    Uses PERPENDICULAR RAY CAST (not distance-to-boundary) so the
    probe measures the taxi's own local half-width on EACH side
    rather than the distance to some faraway edge.  Per user
    rule 4 (2026-04-20): the rect half-width should be the
    NARROWEST pavement width (the taxi's own strip width), not
    an inflated value from adjacent aprons or runway clearance.

    For each probe point:
      * cast a ray perpendicular LEFT from axis; find where ray
        first exits the pavement polygon.
      * cast a ray perpendicular RIGHT from axis similarly.
      * half-width at this probe = min(left, right), capped at
        RAY_CAP_M to avoid saturating across an apron.
    """
    RAY_CAP_M = 40.0
    RAY_STEP_M = 0.5
    if axis.length < 1e-3:
        return 0.0, 0.0, 0.0

    def _perpendicular_half_at(t: float) -> float:
        """Cast perpendicular rays left/right at axis param t.

        Returns the AVERAGE of the two sides — (left + right) / 2 —
        so the width reflects the full pavement strip centered on
        the pavement (not a narrow corridor seen from an off-center
        axis).  Corner snapping downstream pulls the 4 rect corners
        onto the pav boundary, centering the rect on the actual
        pavement regardless of the axis's offset.
        """
        # Local tangent: use points slightly before/after t.
        dt = min(2.0, axis.length * 0.05)
        t0 = max(0.0, t - dt)
        t1 = min(axis.length, t + dt)
        a = axis.interpolate(t0)
        b = axis.interpolate(t1)
        tx, ty = b.x - a.x, b.y - a.y
        mag = math.hypot(tx, ty)
        if mag < 1e-6:
            return 0.0
        ux, uy = tx / mag, ty / mag
        nx, ny = -uy, ux  # left-perp
        pt = axis.interpolate(t)
        ox, oy = pt.x, pt.y
        sides: list[float] = []
        for sign in (-1, 1):
            side = RAY_CAP_M
            d = 0.0
            while d <= RAY_CAP_M:
                qx = ox + sign * nx * d
                qy = oy + sign * ny * d
                if not pav.contains(Point(qx, qy)):
                    side = d
                    break
                d += RAY_STEP_M
            sides.append(side)
        return sum(sides) / 2.0 if sides else RAY_CAP_M

    dists: list[float] = []
    for k in range(n_probes):
        t = (k + 1) / (n_probes + 1) * axis.length
        hw = _perpendicular_half_at(t)
        if hw > 0.1:
            dists.append(hw)
    if not dists:
        return 0.0, 0.0, 0.0
    dists.sort()
    median = dists[len(dists) // 2]
    p90_idx = max(0, int(len(dists) * 0.9) - 1)
    p90 = dists[p90_idx] if p90_idx < len(dists) else dists[-1]
    # ``narrow`` = the MIN half-width probe with a floor to guard
    # against grazing a building corner (skip probes < 3.5 m as
    # noise).  Per user rule 4: rect width = the ACTUAL narrowest
    # section, so the rect fits snugly along the taxi's own narrow
    # corridor, leaving widened areas to junctions.
    filtered = [d for d in dists if d >= 3.5]
    narrow = filtered[0] if filtered else dists[0]
    return median, p90, narrow


def _axis_half_width_at(axis: LineString, t: float, pav: Polygon,
                        cap: float = 40.0, step: float = 0.5) -> float:
    """Average perpendicular half-width of ``pav`` at axis param ``t``
    (metres along ``axis``).  Mirrors ``_natural_half_width``'s ray
    cast for a single point; used by the endpoint trim below."""
    dt = min(2.0, axis.length * 0.05)
    a = axis.interpolate(max(0.0, t - dt))
    b = axis.interpolate(min(axis.length, t + dt))
    tx, ty = b.x - a.x, b.y - a.y
    mag = math.hypot(tx, ty)
    if mag < 1e-6:
        return cap
    nx, ny = -ty / mag, tx / mag
    pt = axis.interpolate(t)
    ox, oy = pt.x, pt.y
    sides: list[float] = []
    for sign in (-1, 1):
        d = 0.0
        side = cap
        while d <= cap:
            if not pav.contains(Point(ox + sign * nx * d,
                                      oy + sign * ny * d)):
                side = d
                break
            d += step
        sides.append(side)
    return sum(sides) / 2.0


def _trim_axis_to_narrow_corridor(
        axis: LineString, pav: Polygon, narrow_hw: float,
        factor: float = 1.3, step_m: float = 2.0,
        min_keep_m: float = 20.0) -> LineString:
    """Trim the axis ENDS inward while the local pavement half-width
    exceeds ``factor * narrow_hw``.

    A taxi centerline runs the full taxiway, but the rect should cover
    only the NARROW corridor — where the pavement fans out at an
    intersection / apron mouth the widened end is junction territory,
    not taxi rect.  This walks each end inward to the first point where
    the pavement narrows back to ~the strip width, so the emitted rect
    (= this trimmed axis, widened) stops at the junction mouth.

    Purely geometric and airport-agnostic: the trigger is the pavement
    widening past the strip's own narrow half-width, nothing tuned per
    airport.  Bounded by ``min_keep_m`` so a short taxiway is never
    trimmed away (removal of whole non-taxi segments is the gates' job,
    not the trim's).
    """
    from shapely.ops import substring
    L = axis.length
    if L < min_keep_m:
        return axis
    # Strip half-width reference, measured with a HIGH cap.  The passed
    # ``narrow_hw`` is capped at 40 m (RAY_CAP_M) and SATURATES at wide
    # airports — then ``factor*narrow_hw`` can exceed the cap and the
    # trim never fires.  Re-measure the strip with a high cap so the
    # junction widenings (60-100 m half) are actually visible above the
    # threshold.
    HIGH_CAP = 120.0
    n = 12
    halves = [_axis_half_width_at(axis, (k + 1) / (n + 1) * L, pav,
                                  cap=HIGH_CAP, step=1.0)
              for k in range(n)]
    halves_f = sorted(h for h in halves if h >= 3.5)
    if not halves_f:
        return axis
    strip = halves_f[min(len(halves_f) - 1, max(0, n // 5))]   # ~p20
    thresh = factor * strip

    def _wider(t: float) -> bool:
        """True if pavement extends past ``thresh`` on EITHER side at
        ``t`` — a one-sided junction widening counts.  Cheap: one
        containment test per side at the threshold distance."""
        a = axis.interpolate(max(0.0, t - 1.0))
        b = axis.interpolate(min(L, t + 1.0))
        tx, ty = b.x - a.x, b.y - a.y
        mag = math.hypot(tx, ty)
        if mag < 1e-6:
            return False
        nx, ny = -ty / mag, tx / mag
        pt = axis.interpolate(t)
        return (pav.contains(Point(pt.x + nx * thresh, pt.y + ny * thresh))
                or pav.contains(Point(pt.x - nx * thresh,
                                      pt.y - ny * thresh)))

    lo = 0.0
    while lo < L * 0.5 and _wider(lo):
        lo += step_m
    hi = L
    while hi > L * 0.5 and _wider(hi):
        hi -= step_m
    if hi - lo < min_keep_m:
        return axis                       # would over-trim — leave it
    if lo <= step_m and hi >= L - step_m:
        return axis                       # nothing to trim
    try:
        sub = substring(axis, lo, hi)
        if sub.is_empty or sub.geom_type != "LineString":
            return axis
        return sub
    except _GEOM_EXC:
        return axis


# When the centerline is off-centre within its pavement strip, the two
# perpendicular half-widths differ.  A rect built symmetric about such
# an axis pokes its long edge past the pavement boundary on the narrow
# side and leaves uncovered pavement on the wide side — the long edge
# then runs along the pavement boundary and gets clipped by the
# long-edge-adjacent absorption pass (SPLP taxiway B: ~2.7 m off-centre
# toward its right edge → connector clipped short → node-20 junction
# dropped).  Above this asymmetry we place the two long edges on the
# ACTUAL left/right pavement edges instead (see
# ``_natural_half_widths_lr`` + ``_rect_from_axis_extended`` half_left/
# half_right).  Below it the axis is effectively centred and the
# symmetric path is used unchanged.
OFFCENTER_ASYM_TOL_M = 1.5


def _natural_half_widths_lr(
        axis: LineString, pav: Polygon, n_probes: int = 15,
) -> tuple[float | None, float | None]:
    """Return (half_left, half_right) — the taxiway strip's own pavement
    half-widths on each side of the axis, by perpendicular ray cast.

    "Left" is the ``+(-uy, ux)`` side of the axis's first→last
    direction (matching ``_rect_from_axis_extended``'s ``px``); "right"
    is the opposite side.  Use these to place a rect's two long edges on
    the actual pavement edges when the centerline is off-centre, so the
    rect's lateral position is determined by the PAVEMENT rather than by
    a centerline that happens to run near one edge.

    Per-side value reflects the NARROWEST cross-section (the taxiway's
    own strip) — only probes whose BOTH sides are bounded (``< RAY_CAP``)
    count, so junction / apron widenings (one side opens out and
    saturates) are excluded.  Among the bounded probes we average the
    L and R of those within 3 m of the narrowest total width.

    Returns ``(None, None)`` when the corridor can't be measured on both
    sides at any probe — the caller falls back to the symmetric width.
    """
    RAY_CAP_M = 40.0
    RAY_STEP_M = 0.5
    if axis.length < 1e-3:
        return None, None
    probes: list[tuple[float, float, float]] = []  # (total, left, right)
    for k in range(n_probes):
        t = (k + 1) / (n_probes + 1) * axis.length
        dt = min(2.0, axis.length * 0.05)
        a = axis.interpolate(max(0.0, t - dt))
        b = axis.interpolate(min(axis.length, t + dt))
        tx, ty = b.x - a.x, b.y - a.y
        mag = math.hypot(tx, ty)
        if mag < 1e-6:
            continue
        ux, uy = tx / mag, ty / mag
        nx, ny = -uy, ux  # left-perp (matches _rect_from_axis_extended px)
        pt = axis.interpolate(t)
        sides: dict[str, float] = {}
        bounded = True
        for sign, key in ((1.0, "L"), (-1.0, "R")):
            d = 0.0
            hit = RAY_CAP_M
            while d <= RAY_CAP_M:
                if not pav.contains(Point(pt.x + sign * nx * d,
                                          pt.y + sign * ny * d)):
                    hit = d
                    break
                d += RAY_STEP_M
            if hit >= RAY_CAP_M:
                bounded = False
                break
            sides[key] = hit
        if not bounded:
            continue
        probes.append((sides["L"] + sides["R"], sides["L"], sides["R"]))
    if not probes:
        return None, None
    min_total = min(p[0] for p in probes)
    narrow = [p for p in probes if p[0] <= min_total + 3.0]
    hl = sum(p[1] for p in narrow) / len(narrow)
    hr = sum(p[2] for p in narrow) / len(narrow)
    return max(1.5, hl), max(1.5, hr)


def _trim_to_narrow(axis: LineString, pav: Polygon, natural_hw: float,
                    widen_factor: float = 1.3) -> LineString | None:
    """Trim the axis inward from each end until the PERPENDICULAR
    half-width at the endpoint drops below ``widen_factor × natural_hw``.

    Uses the same perpendicular ray-cast probing as
    ``_natural_half_width`` so the trim threshold is applied to
    the taxi's LOCAL half-width (not distance-to-boundary).

    Trim step is 2 m.  We never trim more than 50 % of the axis
    length.
    """
    RAY_CAP_M = 40.0
    RAY_STEP_M = 0.5
    total_len = axis.length
    thresh = natural_hw * widen_factor
    step = 2.0
    max_trim = total_len * 0.45

    def _perp_hw(t: float) -> float:
        dt = min(2.0, total_len * 0.05)
        t0 = max(0.0, t - dt)
        t1 = min(total_len, t + dt)
        a = axis.interpolate(t0)
        b = axis.interpolate(t1)
        tx, ty = b.x - a.x, b.y - a.y
        mag = math.hypot(tx, ty)
        if mag < 1e-6:
            return 0.0
        ux, uy = tx / mag, ty / mag
        nx, ny = -uy, ux
        pt = axis.interpolate(t)
        ox, oy = pt.x, pt.y
        best = RAY_CAP_M
        for sign in (-1, 1):
            d = 0.0
            while d <= RAY_CAP_M:
                qx = ox + sign * nx * d
                qy = oy + sign * ny * d
                if not pav.contains(Point(qx, qy)):
                    if d < best:
                        best = d
                    break
                d += RAY_STEP_M
        return best

    trim_a = 0.0
    while trim_a < max_trim:
        if _perp_hw(trim_a) <= thresh:
            break
        trim_a += step

    trim_b = total_len
    min_b = total_len - max_trim
    while trim_b > min_b:
        if _perp_hw(trim_b) <= thresh:
            break
        trim_b -= step

    if trim_b - trim_a < 10.0:
        return None
    from shapely.ops import substring
    return substring(axis, trim_a, trim_b)


def _probe_axis_width(axis: LineString, pav: Polygon,
                     n_probes: int = 9) -> float:
    """Return 2× the MEDIAN distance-to-boundary along the axis.

    At a large connected pavement (SPJC, where apron + taxi + runway
    are all one blob), ray-casting perpendicular overshoots into the
    apron.  The distance-to-boundary gives the local narrow-corridor
    half-width — which for a taxi centered in its strip is the
    half-strip-width.  Using the median over several probe points
    is robust to both (a) endpoints that sit at wider apron junctions
    and (b) narrow bottlenecks from adjacent building edges.
    """
    if axis.length < 1e-3:
        return 0.0
    boundary = pav.boundary
    dists = []
    for k in range(n_probes):
        t = (k + 1) / (n_probes + 1)
        pt = axis.interpolate(t, normalized=True)
        if not pav.contains(pt):
            continue
        d = pt.distance(boundary)
        if d > 0.1:
            dists.append(d)
    if not dists:
        return 0.0
    dists.sort()
    # Median, then 2× for full width
    return dists[len(dists) // 2] * 2.0


def _extend_rect_corners_perpendicular(
        rect: Polygon, axis: LineString,
        pav: Polygon, max_dist: float = 80.0,
        ) -> Polygon:
    """Extend each rect corner OUTWARD perpendicular to the rect's
    AXIS until the apt.dat pavement boundary (or ``max_dist``).

    Used for runway-end stubs (e.g. SPJC's F) where the half-width
    probe is capped at ``RAY_CAP_M = 40 m`` in
    ``_natural_half_width``, so a stub sitting in a wide runway-end
    ramp ends up under-sized.  The pavement extends past the
    rect's long edges, and the surrounding junction wraps around
    them (wrap-around = polygon along long edge of sloping rect,
    forbidden by the user's invariant).  Per user 2026-04-27: the
    rect should cover the FULL pavement width at each end —
    turning into a trapezoid where each corner sits exactly on the
    pavement boundary independently.

    The returned polygon has the same 4 corners in the same order
    (so X-Plane's altitude_high/low convention is preserved); each
    corner is just shifted outward to its respective pavement edge.
    """
    coords = list(rect.exterior.coords)
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) != 4:
        return rect
    a = list(axis.coords)
    if len(a) < 2:
        return rect
    p1, p2 = a[0], a[-1]
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    mag = math.hypot(dx, dy)
    if mag < 1e-6:
        return rect
    ux = dx / mag
    uy = dy / mag
    px = -uy
    py = ux
    STEP = 0.5

    def _ray_extent(ax: float, ay: float,
                    dir_x: float, dir_y: float,
                    base_dist: float) -> float:
        """From axis point (ax, ay) heading (dir_x, dir_y), find
        the distance d at which ``(ax+d*dir, ay+d*dir)`` is on
        ``pav``'s boundary.

        If ``base_dist`` is INSIDE pav: walk OUTWARD to find pav
        exit (rect corner expands to pav width at that location).

        Per user 2026-05-05: if ``base_dist`` is OUTSIDE pav (the
        natural rect corner sits in a void), walk INWARD from
        base_dist toward the axis until we cross into pav.  The
        first inside point is the boundary on the inward side.
        Without this branch, the corner is left in the void and
        the rect's polygon spills outside pav.
        """
        if pav.contains(
                Point(ax + dir_x * base_dist,
                      ay + dir_y * base_dist)):
            # Inside: walk outward to find exit.
            d = base_dist
            while d < max_dist:
                d_test = d + STEP
                if not pav.contains(
                        Point(ax + dir_x * d_test,
                              ay + dir_y * d_test)):
                    return d
                d = d_test
            return d
        # Outside: walk inward toward axis until we enter pav.
        d = base_dist
        while d > STEP:
            d_test = d - STEP
            if pav.contains(
                    Point(ax + dir_x * d_test,
                          ay + dir_y * d_test)):
                return d_test
            d = d_test
        return 0.0

    new_corners: list[tuple[float, float]] = []
    pav_nodes = _pav_boundary_nodes(pav)
    for cx, cy in coords:
        # For each corner: project onto axis to determine which
        # endpoint (p1 or p2) it belongs to and which perp side.
        vx = cx - p1[0]
        vy = cy - p1[1]
        proj = vx * ux + vy * uy
        # axis endpoint nearer this corner
        ax_pt = p1 if proj < mag * 0.5 else p2
        # perpendicular signed distance from axis
        rel_x = cx - ax_pt[0]
        rel_y = cy - ax_pt[1]
        perp_signed = rel_x * px + rel_y * py
        if perp_signed >= 0:
            dir_x, dir_y = px, py
        else:
            dir_x, dir_y = -px, -py
        base = abs(perp_signed)
        if base < 1.0:
            new_corners.append((cx, cy))
            continue
        d = _ray_extent(ax_pt[0], ax_pt[1], dir_x, dir_y, base)
        boundary_pt = (ax_pt[0] + dir_x * d, ax_pt[1] + dir_y * d)
        # Prefer a pav.boundary node within 5 m (user 2026-05-05).
        new_corners.append(_prefer_pav_node(boundary_pt, pav_nodes))

    try:
        new_rect = Polygon(new_corners)
        if new_rect.is_valid and not new_rect.is_empty:
            return new_rect
    except _GEOM_EXC:
        pass
    return rect


def _rect_from_axis_extended(axis: LineString, width: float,
                            pav: Polygon,
                            apt_vertices: list[tuple[float, float]] | None = None,
                            accept_asymmetric: bool = False,
                            registry: CanonicalPointRegistry | None = None,
                            half_left: float | None = None,
                            half_right: float | None = None,
                            ) -> Polygon | None:
    """Build a rect around the axis at its first-to-last direction.

    The 4 corners are placed at axis endpoints ± perpendicular half-
    width, then each corner is snapped FIRST to the nearest apt.dat
    pavement vertex within ``VERTEX_SNAP_RADIUS_M``, ELSE to the
    nearest pavement edge point within ``EDGE_SNAP_RADIUS_M``.
    This matches the snapped target convention where every non-
    runway vertex sits on an apt.dat pavement vertex.

    ``half_left`` / ``half_right``: when both are given, the rect's
    long edges are offset asymmetrically — ``half_left`` on the
    ``+(-uy, ux)`` side, ``half_right`` on the opposite side — instead
    of ``width / 2`` each.  Use this (with ``_natural_half_widths_lr``)
    to place the two long edges on the ACTUAL left/right pavement edges
    when the centerline is off-centre, so the rect fits the pavement
    rather than poking past one edge.  When either is ``None`` the rect
    is symmetric about the axis (``width / 2`` each) — unchanged
    behaviour.  The longitudinal asymmetric-trim retry below is
    orthogonal (it trims the rect's LENGTH, not its lateral offsets).

    Asymmetric-snap trim: when the two snapped end-widths differ
    by more than ``ASYM_WIDTH_TOL_M``, the rect has extended into
    a widening pavement area (apron or junction) on the wider end.
    Per user (2026-04-21): "if we're getting an asymmetric rect,
    most likely it's too long and needs to be shortened a bit so
    it's not pulled into a junction."  Retry once with the axis
    trimmed by ``ASYM_TRIM_FRAC`` of its length on the wider end.

    When ``accept_asymmetric`` is True, the function returns the
    MOST-SYMMETRIC snapped quadrilateral encountered across all
    retries even if no iteration converges to within the symmetry
    tolerances.  Used as a fallback for diagonal-stub refs where
    the diagonal connector pavement physically flares wide at the
    apron end (asymmetry can't be eliminated by trimming).  The
    returned quad is still a 4-corner sloped shape that downstream
    code can handle as a "rect" — corners ordered [H, L, L, H]
    along the axis — but its long edges have unequal lengths.
    """
    from shapely.ops import substring

    # Symmetry tolerances: a proper rectangle has equal long sides
    # and equal short sides.  Use a RATIO check so small rects
    # aren't over-trimmed — a 5 m width delta on a 30 m-wide stub
    # (17 %) reads as near-symmetric, while a 5 m delta on a
    # 22 m-wide cross-connector (23 %) reads as a trapezoid.  User
    # (2026-04-21): "I don't see why it should trim the second
    # diagonal at the south end, which is already quite short and
    # appears symmetrical" — that stub had width Δ = 5 m / max
    # 29 m = 17 %, below threshold.
    ASYM_WIDTH_RATIO_TOL = 0.20   # width Δ / max_width > 20 % → trim
    ASYM_LENGTH_RATIO_TOL = 0.10  # length Δ / max_length > 10 % → trim
    # Per-iteration axis shrink per user (2026-04-21): trim BOTH
    # ends by 2.5 % of length each (5 % total) so the rect STAYS
    # CENTERED on its axis as it shrinks — trimming only the
    # wider end shifts the rect toward the narrower side and
    # leaves the problematic (wider) end snapped to the same
    # widening zone on the next iteration.
    ASYM_TRIM_EACH_END = 0.025     # 2.5 % off each end per iteration
    MAX_ASYM_RETRIES = 15          # 15 * 5 % = up to 75 % shrink

    cur_axis = axis
    best_snapped: list[tuple[float, float]] | None = None
    best_asym_score = float("inf")
    for attempt in range(MAX_ASYM_RETRIES + 1):
        coords = list(cur_axis.coords)
        if len(coords) < 2:
            break
        p1 = coords[0]
        p2 = coords[-1]
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        mag = math.hypot(dx, dy)
        if mag < 1e-6:
            break
        ux, uy = dx / mag, dy / mag
        px, py = -uy, ux
        if half_left is not None and half_right is not None:
            hl, hr = half_left, half_right
        else:
            hl = hr = width / 2.0
        corners = [
            (p1[0] + px * hl, p1[1] + py * hl),   # 0: end1 side1 (left)
            (p2[0] + px * hl, p2[1] + py * hl),   # 1: end2 side1 (left)
            (p2[0] - px * hr, p2[1] - py * hr),   # 2: end2 side2 (right)
            (p1[0] - px * hr, p1[1] - py * hr),   # 3: end1 side2 (right)
        ]
        snapped = _snap_corners_to_pavement(
            corners, pav, apt_vertices, registry=registry)
        if snapped is None:
            # Degenerate rect (≥2 corners collapsed within 1 m of
            # each other after snap).  Stop iterating — further
            # shrinks will only collapse more aggressively.
            break
        # NOTE: end-squaring is NOT done here — the later
        # ``_snap_rect_sloping_edges_to_holes`` pass re-snaps a rect's long
        # edge onto an apt.dat hole boundary and would re-slant the end.
        # Squaring is applied once, last, in ``_square_taxi_rect_ends``
        # (called after hole-snap + split in the pipeline).

        # Symmetry check: equal widths (end1 vs end2) AND equal
        # lengths (side1 vs side2).
        w_end1 = math.hypot(snapped[0][0] - snapped[3][0],
                            snapped[0][1] - snapped[3][1])
        w_end2 = math.hypot(snapped[1][0] - snapped[2][0],
                            snapped[1][1] - snapped[2][1])
        l_side1 = math.hypot(snapped[1][0] - snapped[0][0],
                             snapped[1][1] - snapped[0][1])
        l_side2 = math.hypot(snapped[2][0] - snapped[3][0],
                             snapped[2][1] - snapped[3][1])
        width_asym = abs(w_end1 - w_end2)
        length_asym = abs(l_side1 - l_side2)
        max_w = max(w_end1, w_end2)
        max_l = max(l_side1, l_side2)
        width_ratio = (width_asym / max_w) if max_w > 1e-6 else 0.0
        length_ratio = (length_asym / max_l) if max_l > 1e-6 else 0.0
        symmetric = (width_ratio <= ASYM_WIDTH_RATIO_TOL
                     and length_ratio <= ASYM_LENGTH_RATIO_TOL)
        # Track the most-symmetric snapped quad in case we exit
        # without converging — accept_asymmetric will return it.
        asym_score = width_ratio + length_ratio
        if asym_score < best_asym_score:
            best_asym_score = asym_score
            best_snapped = snapped
        if symmetric or attempt == MAX_ASYM_RETRIES:
            return Polygon(snapped)

        # Asymmetric — trim BOTH ends and retry, keeping the rect
        # centered on its axis (per user 2026-04-21 feedback).
        trim_each = ASYM_TRIM_EACH_END * cur_axis.length
        new_start = trim_each
        new_end = cur_axis.length - trim_each
        if new_end - new_start < MIN_SEGMENT_LEN_M:
            # Axis would become too short; accept current asymmetric
            # rect rather than discarding.
            return Polygon(snapped)
        try:
            cur_axis = substring(cur_axis, new_start, new_end)
        except _GEOM_EXC:
            return Polygon(snapped)
    # Loop fell through (snap returned None on some retry, or the
    # axis collapsed).  Diagonal-stub callers use ``accept_asymmetric``
    # to fall back to the best snapped quad encountered along the way.
    # Per user 2026-05-12: SPJC's V3 diagonal connector flares wide
    # at the apron end (width asymmetry 42-47%, never converges by
    # axis shrink); without this fallback the rect is silently
    # discarded and the surrounding junction draws a 100+ m straight
    # edge across the diagonal pavement.
    if accept_asymmetric and best_snapped is not None:
        return Polygon(best_snapped)
    return None


APRON_INTERIOR_DEPTH_M = 15.0   # if 2+ natural corners are deeper
                                 # than this from pav.boundary, the
                                 # rect is sitting in apron interior
                                 # — reject so apron stays as residue


PAV_NODE_PREFER_RADIUS_M = 5.0


def _pav_boundary_nodes(pav: Polygon) -> list[tuple[float, float]]:
    """Return all pav_union ring vertices (exterior + interiors)."""
    out: list[tuple[float, float]] = []
    parts = (list(pav.geoms)
             if pav.geom_type == "MultiPolygon" else [pav])
    for poly in parts:
        if poly.geom_type != "Polygon":
            continue
        ext = list(poly.exterior.coords)
        if ext and ext[0] == ext[-1]:
            ext = ext[:-1]
        out.extend(ext)
        for ring in poly.interiors:
            ri = list(ring.coords)
            if ri and ri[0] == ri[-1]:
                ri = ri[:-1]
            out.extend(ri)
    return out


def _prefer_pav_node(snapped: tuple[float, float],
                     pav_nodes: list[tuple[float, float]],
                     radius: float = PAV_NODE_PREFER_RADIUS_M
                     ) -> tuple[float, float]:
    """If a pav-boundary vertex sits within ``radius`` of the
    boundary-snapped point, return that vertex.  Otherwise return
    ``snapped`` unchanged.

    Per user 2026-05-05: rect corners should prefer a pav.boundary
    NODE over an arbitrary boundary edge-projection when one is
    close, so the corner shares an exact vertex with pav and
    downstream OSM-emit bucketing assigns the same node ID.  Tight
    radius (5 m) keeps this a refinement, not a major reposition —
    the snap-to-nearest-edge has already placed the corner on
    pav.boundary; this merely prefers an adjacent vertex when one
    is close enough.
    """
    best = snapped
    best_d2 = radius * radius
    for v in pav_nodes:
        d2 = (v[0] - snapped[0]) ** 2 + (v[1] - snapped[1]) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best = (float(v[0]), float(v[1]))
    return best


def _square_rect_ends(
    snapped: list[tuple[float, float]],
    p1: tuple[float, float],
    p2: tuple[float, float],
    ux: float, uy: float,
) -> list[tuple[float, float]]:
    """Keep each rect END perpendicular to the axis after the per-corner
    pavement snap (gate ``RECT_SQUARE_ENDS``).

    ``_snap_corners_to_pavement`` snaps every corner INDEPENDENTLY to the
    nearest pavement vertex, so where an end meets an angled junction mouth
    its two corners land at different AXIAL positions and the end goes
    slanted (the rect emits as a trapezoid).  For a genuinely-slanted end
    this re-seats BOTH its corners onto the axis ENDPOINT's perpendicular
    line (p1 for one end, p2 for the other), KEEPING each corner's lateral
    (perpendicular) offset so the long edges still sit on the actual
    pavement edges.  The rect then spans exactly its (already
    pavement-clipped) centerline with square ends; the slanted-pavement
    wedge it no longer covers becomes junction (``pav_union - rect``) —
    the user's "junctions align to the rect" rule.

    A perpendicular end (its two corners already within
    ``RECT_END_SQUARE_TOL_M`` of each other axially) is left untouched, so
    only genuinely slanted ends move and already-square rects stay
    byte-identical.  Corner order is the builder's
    ``[p1+perp, p2+perp, p2-perp, p1-perp]`` — corners 0,3 are the p1 end,
    1,2 the p2 end.
    """
    if len(snapped) != 4:
        return snapped
    px, py = -uy, ux

    def _tp(c):
        vx, vy = c[0] - p1[0], c[1] - p1[1]
        return (vx * ux + vy * uy, vx * px + vy * py)

    tp = [_tp(c) for c in snapped]
    mag = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    out = list(snapped)
    moved: set = set()
    # (corner pair, axial position of that end's axis endpoint)
    for (a, b), t_end in (((0, 3), 0.0), ((1, 2), mag)):
        if abs(tp[a][0] - tp[b][0]) <= RECT_END_SQUARE_TOL_M:
            continue                       # end already perpendicular
        for idx in (a, b):
            perp = tp[idx][1]
            out[idx] = (p1[0] + ux * t_end + px * perp,
                        p1[1] + uy * t_end + py * perp)
            moved.add(idx)
    return out, moved


def _square_taxi_rect_ends(
    taxi_rects: list[tuple[Polygon, LineString, str, str]],
    pav_union: Polygon | None = None,
) -> list[tuple[Polygon, LineString, str, str]]:
    """Final end-squaring pass over the built taxi rects (gate
    ``RECT_SQUARE_ENDS``).

    Runs AFTER ``_snap_rect_sloping_edges_to_holes`` and the long-rect
    split, so the per-corner pavement snap AND the hole-edge snap have both
    had their say — whichever left a rect end slanted (its two corners at
    different axial positions) is straightened here, the LAST word, by
    :func:`_square_rect_ends`.  Each end's corners collapse to that end's
    axis-endpoint perpendicular line, keeping their lateral pavement fit; the
    angled-pavement wedge becomes junction via ``pav_union - rect``.

    Skipped for DIGIT refs (diagonal stubs / SVC roads), whose flare is
    intentional — the same family the builder admits via
    ``accept_asymmetric``.  Already-perpendicular ends are left untouched,
    so non-slanted rects stay byte-identical.

    ``pav_union`` (when given): each squared corner is then snapped back onto
    the pavement boundary (within ``SQUARE_BOUNDARY_SNAP_M``) so the squared
    end edge doesn't run a hair INSIDE the angled boundary and leave a thin
    sliver that ``pav_union - rect`` turns into orphan junction vertices.
    The snap keeps the corner at its (perpendicular) axial position — it only
    fixes the lateral offset to land on the boundary."""
    if not RECT_SQUARE_ENDS:
        return list(taxi_rects)
    _SLOPING = (ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
                ROLE_STUB, ROLE_CROSS_CONNECTOR)
    SQUARE_BOUNDARY_SNAP_M = 2.5
    boundary = (pav_union.boundary
                if (pav_union is not None and not pav_union.is_empty)
                else None)
    out: list[tuple[Polygon, LineString, str, str]] = []
    for rect, axis, role, ref in taxi_rects:
        if (role in _SLOPING
                and ref and not taxi_ref_is_sub_index(ref)
                and rect is not None and not rect.is_empty
                and rect.geom_type == "Polygon"
                and axis is not None and not axis.is_empty):
            cs = list(rect.exterior.coords)
            if cs and cs[0] == cs[-1]:
                cs = cs[:-1]
            ac = list(axis.coords)
            if len(cs) == 4 and len(ac) >= 2:
                p1, p2 = ac[0], ac[-1]
                ddx, ddy = p2[0] - p1[0], p2[1] - p1[1]
                mag = math.hypot(ddx, ddy)
                if mag > 1e-6:
                    sq, moved = _square_rect_ends(
                        cs, p1, p2, ddx / mag, ddy / mag)
                    # Only keep the squared rect if EVERY moved corner can
                    # land on the pavement boundary — otherwise the squared
                    # (perpendicular) end runs off the angled boundary and
                    # ``pav_union - rect`` leaves an orphan-vertex sliver.
                    # When a corner can't reach the boundary, that mouth is
                    # too angled to square cleanly: leave the rect
                    # trapezoidal (corners stay on the boundary, no orphan).
                    ok = moved and boundary is not None
                    if ok:
                        for idx in moved:
                            near, _ = nearest_points(boundary, Point(sq[idx]))
                            d = math.hypot(near.x - sq[idx][0],
                                           near.y - sq[idx][1])
                            if d > SQUARE_BOUNDARY_SNAP_M:
                                ok = False
                                break
                            sq[idx] = (float(near.x), float(near.y))
                    if ok:
                        try:
                            newp = Polygon(sq)
                            # Accept the squared rect only if it stays WITHIN
                            # the pavement (no corner pushed outside → no
                            # outside-pavement junction vertex, rect still
                            # rests on source).  A clean square (like G) is a
                            # subset of the original trapezoid and passes;
                            # anything the boundary snap nudged out reverts.
                            if (newp.is_valid and not newp.is_empty
                                    and (pav_union is None
                                         or newp.difference(pav_union).area
                                         <= 0.5)):
                                rect = newp
                        except _GEOM_EXC:
                            pass
        out.append((rect, axis, role, ref))
    return out


def _snap_corners_to_pavement(
    corners: list[tuple[float, float]],
    pav: Polygon,
    apt_vertices: list[tuple[float, float]] | None = None,
    registry: CanonicalPointRegistry | None = None,
) -> list[tuple[float, float]] | None:
    """Snap each rect corner to ``pav.boundary`` and resolve
    through the canonical-point registry.

    Per user 2026-05-18:

    1. Project the input corner to the nearest ``pav.boundary``
       point.
    2. Resolve the boundary point through the canonical-point
       registry: ``get_or_add`` returns the existing canonical
       point within ``SHARED_VERTEX_TOL_M`` if one exists, else
       inserts the boundary point as a new canonical entry.

    Result: every rect built in the same pipeline pass converges
    on EXACT identical coordinates at the same intersection.  Two
    rects approaching the same physical corner from different
    centerlines get the EXACT same (x, y), so adjacent rects
    share vertices and ``pav_union.difference(rects)`` inherits
    those shared positions on the junction perimeter.  No
    ``buffer(0)`` repairs needed downstream.

    Per user 2026-05-11: this function does NOT decide whether a
    rect "should be emitted at all" — that's the absorption pass's
    job (``_drop_primary_parallels_embedded_in_pavement``).
    Always snap.  Apron-interior fragments are discarded
    downstream by the absorption kept-fragment guard.

    Returns ``None`` only for genuine geometric degeneracy: when
    the snap collapses two corners onto near-identical points
    (within 1 m).
    """
    boundary = pav.boundary
    pav_nodes = _pav_boundary_nodes(pav)
    snapped: list[tuple[float, float]] = []
    for (cx, cy) in corners:
        p = Point(cx, cy)
        near, _ = nearest_points(boundary, p)
        boundary_pt = (float(near.x), float(near.y))
        # Layer 1: prefer a pav.boundary vertex within
        # ``PAV_NODE_PREFER_RADIUS_M`` (5 m) of the projection.
        # Keeps the long-standing snap-to-row-110-vertex behavior.
        prefered = _prefer_pav_node(boundary_pt, pav_nodes)
        # Layer 2: route through the canonical-point registry at
        # ``SHARED_VERTEX_TOL_M`` (0.5 m) so two rects whose corners
        # snap to within sub-metre distance of each other converge
        # on the EXACT same coordinates.  Combined with Layer 1's
        # vertex preference, rect corners at multi-rect intersections
        # share canonical coordinates whether the intersection has
        # a row-110 vertex or not.
        if registry is not None:
            prefered = registry.get_or_add(prefered[0], prefered[1])
        snapped.append(prefered)
    # Reject degenerate rects where two corners collapsed onto the
    # same point (within 1 m).
    for i in range(4):
        for j in range(i + 1, 4):
            if math.hypot(snapped[i][0] - snapped[j][0],
                          snapped[i][1] - snapped[j][1]) < 1.0:
                return None
    return snapped


def _cap_rect_length_to_width(
    taxi_rects: list[tuple[Polygon, LineString, str, str]],
    rwy_centerlines: list[LineString],
    pav: Polygon,
    apt_vertices: list[tuple[float, float]] | None,
) -> list[tuple[Polygon, LineString, str, str]]:
    """Cap each rect's length-to-width ratio per the user's
    2026-04-27 spec: rects should be roughly square (length ≈
    width) so the long edges sit on the pavement-narrowing
    boundary, corners snap there, and surrounding junctions
    connect only at the short edges (never wrap around long
    edges).

    Cap depends on the bearing-to-nearest-runway:

      * Parallel  (db < 20°)  — NO CAP (long parallel taxis are
        legitimate, often running 500 m+ along the runway).
      * Diagonal  (20° ≤ db < 45°) — length ≤ 1.0 × width
        (truly square; matches the tighter 30 % margin used for
        diagonal stubs in ``_rect_margin_frac_for``).
      * Perpendicular (db ≥ 45°) — length ≤ 1.3 × width (small
        excess so the rect can extend slightly past the apron's
        narrow corridor without forcing surrounding junctions to
        wrap).

    Shrinks the axis symmetrically (same amount from both ends) so
    the rect's centre stays put, then re-runs
    ``_rect_from_axis_extended`` so corners re-snap to apt.dat
    pavement boundary on the new axis.
    """
    PERP_CAP_RATIO = 1.3
    DIAG_CAP_RATIO = 1.0
    from shapely.ops import substring
    out: list[tuple[Polygon, LineString, str, str]] = []
    for rect, axis, role, ref in taxi_rects:
        try:
            coords = list(rect.exterior.coords)
        except _GEOM_EXC:
            out.append((rect, axis, role, ref))
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            out.append((rect, axis, role, ref))
            continue
        edge_lens = [
            math.hypot(coords[(i + 1) % 4][0] - coords[i][0],
                       coords[(i + 1) % 4][1] - coords[i][1])
            for i in range(4)]
        length = max(edge_lens)
        width = min(edge_lens)
        if width < 1.0 or length < 1.0:
            out.append((rect, axis, role, ref))
            continue
        db = _axis_to_nearest_rwy_db(axis, rwy_centerlines)
        if db is None:
            out.append((rect, axis, role, ref))
            continue
        if db < 20.0:
            # Parallel — no cap.
            out.append((rect, axis, role, ref))
            continue
        cap_ratio = (DIAG_CAP_RATIO if db < 45.0
                     else PERP_CAP_RATIO)
        max_len = cap_ratio * width
        if length <= max_len + 0.5:
            out.append((rect, axis, role, ref))
            continue
        axis_len = axis.length
        new_axis_len = max_len
        margin = (axis_len - new_axis_len) / 2.0
        if margin <= 0:
            out.append((rect, axis, role, ref))
            continue
        try:
            new_axis = substring(
                axis, margin, axis_len - margin)
        except _GEOM_EXC:
            out.append((rect, axis, role, ref))
            continue
        if (new_axis.is_empty
                or new_axis.geom_type != "LineString"
                or new_axis.length < 5.0):
            out.append((rect, axis, role, ref))
            continue
        new_rect = _rect_from_axis_extended(
            new_axis, width, pav, apt_vertices=apt_vertices)
        if (new_rect is None or new_rect.is_empty
                or new_rect.geom_type != "Polygon"
                or not new_rect.is_valid):
            out.append((rect, axis, role, ref))
            continue
        out.append((new_rect, new_axis, role, ref))
    return out


def _classify_role(axis: LineString, width: float,
                   rwy_centerlines: list[LineString],
                   rwy_union: Polygon | None,
                   ref: str = "",
                   ref_overall_bearings: dict[str, float] | None
                   = None) -> str:
    """Classify a taxi rect by axis geometry alone.

    The role is determined entirely by:
      * bearing to nearest runway (parallel within 20°,
        perpendicular beyond 45°),
      * straight-line distance from axis midpoint to nearest runway
        centerline (close → primary, far → secondary or cross),
      * axis length (must clear minimum length per role).

    Ref letters are NOT used as a classifier — they vary wildly
    between airports (SPJC's parallel taxis are A/F/L/V; CYXY's
    is E; KBNA uses different letters again; many CYXY taxis have
    no ref at all).  The only ref-pattern rules retained are:

      * Sub-ref (any digit in the label, e.g. V1, L3, A1) →
        always STUB.  Sub-ref tagging is universal: a digit
        suffix means a short connector spur regardless of airport.
      * Diagonal parent ref → always STUB.  When the parent OSM
        way's chord bearing is itself diagonal (db ≥ 20°), every
        segment of that ref is part of a diagonal stub even if
        a curving end-segment happens to align near-parallel
        locally.  At SPJC, B/C/E/G enter the runway at shallow
        angles; without this check, the post-curve sub-segment
        (B's 85 m piece, db_local = 18°) gets misclassified as
        PRIMARY_PARALLEL even though there's no actual B parallel
        taxiway.

    Per user 2026-05-05 (revised stub definition): a stub can be at
    a wide range of angles BUT cannot be directly parallel to the
    runway.  When db < 20° (parallel), the rect is always a parallel
    classification regardless of length — never a stub.

    Roles:
      * PRIMARY_PARALLEL  — db < 20°, < 400 m from runway
      * SECONDARY_PARALLEL — db < 20°, ≥ 400 m from runway
      * CROSS_CONNECTOR   — db > 45°, length ≥ 20 m, > 250 m from runway
      * STUB              — db ≥ 20° (not parallel), runway-adjacent
                             perp/diagonal segment
    """
    db = _axis_to_nearest_rwy_db(axis, rwy_centerlines)
    if db is None:
        # No runway to compare against — fall back to STUB (only
        # case where a parallel determination is impossible).
        return ROLE_STUB

    try:
        mid = axis.interpolate(0.5, normalized=True)
        dist_rwy = min(mid.distance(r) for r in rwy_centerlines)
    except _GEOM_EXC:
        dist_rwy = 1e6
    length = axis.length

    # Diagonal-parent check FIRST: if the rect's REF has an overall-
    # DIAGONAL parent OSM way (parent db_overall ∈ [20°, 45°)),
    # force STUB regardless of the local segment bearing.  At SPJC
    # B/C/E/G enter the runway at shallow angles; a curving sub-
    # segment can have db_local = 19° (just inside the parallel
    # band), which historically misclassified it as
    # PRIMARY_PARALLEL — fragmenting the diagonal-stub junction
    # area into separate parallel + multiple junction polygons.
    # This check must run BEFORE the db<20° → PRIMARY_PARALLEL
    # branch below so the near-parallel sub-segment is caught
    # (user 2026-05-12; fixes SPJC stub C -10030 misclassification
    # and the resulting 3-junction split between runway and stub C).
    if (ref and ref_overall_bearings
            and ref in ref_overall_bearings
            and rwy_centerlines):
        try:
            _rwy = min(rwy_centerlines,
                       key=lambda r: axis.distance(r))
            _rc = list(_rwy.coords)
            _rdx = _rc[-1][0] - _rc[0][0]
            _rdy = _rc[-1][1] - _rc[0][1]
            if math.hypot(_rdx, _rdy) > 1e-6:
                _rwy_bearing = (math.degrees(
                    math.atan2(_rdx, _rdy)) % 180.0)
                _ref_db = abs(ref_overall_bearings[ref]
                              - _rwy_bearing)
                _ref_db = min(_ref_db, 180.0 - _ref_db)
                if 20.0 <= _ref_db < 45.0:
                    return ROLE_STUB
        except _GEOM_EXC:
            pass

    # Sub-ref check (digit suffix → STUB), now also runs before the
    # db<20° branch so e.g. a curving V3 sub-segment with
    # db_local = 18° still classifies as STUB rather than short
    # PRIMARY_PARALLEL fragments.  Per user 2026-05-12: require
    # db_local >= 15° so digit-ref segments that are essentially
    # parallel to the runway (e.g. SPJC V3 near-parallel pieces
    # at db = 6° / 12°, geometrically part of V) classify as
    # PRIMARY_PARALLEL.  The diagonal-parent check above already
    # caught curving sub-segments whose PARENT way is diagonal;
    # this gate only fires on standalone near-parallel sub-refs.
    if ref and taxi_ref_is_sub_index(ref) and db >= 15.0:
        return ROLE_STUB

    # Per user 2026-05-05: parallel rects (db < 20°) that pass
    # the diagonal-parent + sub-ref filters above are NEVER stubs.
    # Classify by distance to runway alone.
    if db < 20.0:
        if dist_rwy < 400.0:
            return ROLE_PRIMARY_PARALLEL
        return ROLE_SECONDARY_PARALLEL

    if db > 45.0 and length >= 20.0:
        if dist_rwy > 250.0:
            return ROLE_CROSS_CONNECTOR
        return ROLE_STUB
    return ROLE_STUB


def _axis_to_nearest_rwy_db(axis: LineString,
                            rwy_centerlines: list[LineString]
                            ) -> float | None:
    """Return the bearing difference from ``axis`` to the nearest
    runway centerline, modulo 180°."""
    if not rwy_centerlines:
        return None
    rwy = min(rwy_centerlines, key=lambda r: axis.distance(r))

    def _bearing(ls):
        c = list(ls.coords)
        return math.degrees(math.atan2(c[-1][0] - c[0][0],
                                       c[-1][1] - c[0][1])) % 180.0
    db = abs(_bearing(axis) - _bearing(rwy))
    return min(db, 180.0 - db)


def _refine_roles(emitted, rwy_centerlines):
    """Post-classify: demote the stub-A / stub-F segment (the short
    runway-connector within a parallel ref's polyline) from
    primary_parallel to stub.

    Rule: for each parallel ref, find SEGMENTS that are
    significantly perpendicular (>= 40° off runway) AND short (< 150 m).
    Demote to stub.  Multiple per ref allowed.
    """
    if not rwy_centerlines or not emitted:
        return
    for i, (rect, axis, role, ref) in enumerate(emitted):
        if role != ROLE_PRIMARY_PARALLEL:
            continue
        db = _axis_to_nearest_rwy_db(axis, rwy_centerlines)
        if db is None:
            continue
        if db >= 40.0 and axis.length < 150.0:
            emitted[i] = (rect, axis, ROLE_STUB, ref)


# ──────────────────────────────────────────────────────────────────────
# Hole-aware sloping-edge snap
# ──────────────────────────────────────────────────────────────────────

def _try_align_sloping_to_hole(
    rect: Polygon,
    axis: LineString | None,
    hole_segs: list[tuple[float, float, float, float]],
    perp_tol_m: float,
    length_overlap_frac: float,
    max_corner_shift_m: float,
) -> tuple[Polygon | None, LineString | None]:
    """Try to align one of ``rect``'s sloping (long) edges with the
    nearest matching apt.dat hole-boundary segment.

    Returns ``(new_rect, new_axis)`` if a match is found and the
    realigned rect is valid; ``(None, None)`` otherwise.

    See ``_snap_rect_sloping_edges_to_holes`` for the matching rule.
    """
    rc = list(rect.exterior.coords)
    if rc and rc[0] == rc[-1]:
        rc = rc[:-1]
    if len(rc) != 4:
        return None, None

    sloping_edges = [
        (0, 1, rc[0], rc[1]),
        (2, 3, rc[2], rc[3]),
    ]

    best: tuple[int, int, tuple[float, float],
                          tuple[float, float]] | None = None
    best_score = float("inf")
    for ca_idx, cb_idx, ca, cb in sloping_edges:
        edge_len = math.hypot(cb[0] - ca[0], cb[1] - ca[1])
        if edge_len < 1.0:
            continue
        for ax, ay, bx, by in hole_segs:
            hx = bx - ax
            hy = by - ay
            h_len = math.hypot(hx, hy)
            if h_len < 1.0:
                continue
            mx = 0.5 * (ca[0] + cb[0])
            my = 0.5 * (ca[1] + cb[1])
            t = ((mx - ax) * hx + (my - ay) * hy) / (h_len * h_len)
            t_clamped = max(0.0, min(1.0, t))
            px = ax + t_clamped * hx
            py = ay + t_clamped * hy
            perp_d = math.hypot(mx - px, my - py)
            if perp_d > perp_tol_m:
                continue
            ta = ((ca[0] - ax) * hx + (ca[1] - ay) * hy) / (h_len * h_len)
            tb = ((cb[0] - ax) * hx + (cb[1] - ay) * hy) / (h_len * h_len)
            t_lo = max(0.0, min(ta, tb))
            t_hi = min(1.0, max(ta, tb))
            if t_hi <= t_lo:
                continue
            overlap = (t_hi - t_lo) * h_len
            rect_overlap = overlap / edge_len
            hole_overlap = overlap / h_len
            if max(rect_overlap, hole_overlap) < length_overlap_frac:
                continue
            ta_c = max(0.0, min(1.0, ta))
            tb_c = max(0.0, min(1.0, tb))
            new_a = (ax + ta_c * hx, ay + ta_c * hy)
            new_b = (ax + tb_c * hx, ay + tb_c * hy)
            shift_a = math.hypot(new_a[0] - ca[0], new_a[1] - ca[1])
            shift_b = math.hypot(new_b[0] - cb[0], new_b[1] - cb[1])
            if max(shift_a, shift_b) > max_corner_shift_m:
                continue
            score = shift_a + shift_b
            if score < best_score:
                best_score = score
                best = (ca_idx, cb_idx, new_a, new_b)

    if best is None:
        return None, None

    ca_idx, cb_idx, new_a, new_b = best
    width = math.hypot(rc[0][0] - rc[3][0], rc[0][1] - rc[3][1])
    if width < 1.0:
        return None, None
    dxn = new_b[0] - new_a[0]
    dyn = new_b[1] - new_a[1]
    new_len = math.hypot(dxn, dyn)
    if new_len < 1.0:
        return None, None
    ux, uy = dxn / new_len, dyn / new_len
    perp_x, perp_y = -uy, ux
    old_axis_coords = list(axis.coords) if axis is not None else []
    if len(old_axis_coords) >= 2:
        old_mid_x = 0.5 * (old_axis_coords[0][0]
                            + old_axis_coords[-1][0])
        old_mid_y = 0.5 * (old_axis_coords[0][1]
                            + old_axis_coords[-1][1])
    else:
        old_mid_x = 0.5 * (rc[0][0] + rc[2][0])
        old_mid_y = 0.5 * (rc[0][1] + rc[2][1])
    new_edge_mid_x = 0.5 * (new_a[0] + new_b[0])
    new_edge_mid_y = 0.5 * (new_a[1] + new_b[1])
    offset_x = old_mid_x - new_edge_mid_x
    offset_y = old_mid_y - new_edge_mid_y
    sign = 1.0 if offset_x * perp_x + offset_y * perp_y > 0 else -1.0
    half = width / 2.0
    new_p1 = (new_a[0] + sign * perp_x * half,
              new_a[1] + sign * perp_y * half)
    new_p2 = (new_b[0] + sign * perp_x * half,
              new_b[1] + sign * perp_y * half)
    if (ca_idx, cb_idx) == (2, 3):
        new_p1, new_p2 = new_p2, new_p1
        ux, uy = -ux, -uy
        perp_x, perp_y = -perp_x, -perp_y
        sign = -sign
        new_p1 = (new_b[0] + sign * perp_x * half,
                  new_b[1] + sign * perp_y * half)
        new_p2 = (new_a[0] + sign * perp_x * half,
                  new_a[1] + sign * perp_y * half)
    new_rc = [
        (new_p1[0] + perp_x * half, new_p1[1] + perp_y * half),  # 0
        (new_p2[0] + perp_x * half, new_p2[1] + perp_y * half),  # 1
        (new_p2[0] - perp_x * half, new_p2[1] - perp_y * half),  # 2
        (new_p1[0] - perp_x * half, new_p1[1] - perp_y * half),  # 3
    ]
    try:
        new_rect = Polygon(new_rc)
        new_axis = LineString([new_p1, new_p2])
    except _GEOM_EXC:
        return None, None
    if (not new_rect.is_valid) or new_rect.is_empty:
        return None, None
    return new_rect, new_axis


def _snap_rect_sloping_edges_to_holes(
    taxi_rects: list[tuple[Polygon, LineString, str, str]],
    pav_union: Polygon | None,
    perp_tol_m: float = 3.0,
    length_overlap_frac: float = 0.30,
    min_hole_area_m2: float = 100.0,
    max_corner_shift_m: float = 8.0,
) -> list[tuple[Polygon, LineString, str, str]]:
    """Per user 2026-05-04 (apron-boundary rule): when a sloping rect's
    long edge runs near and parallel to an apt.dat row-110 hole's
    boundary, snap the rect so its sloping edge LIES ON the hole
    boundary.  This makes the rect "form one side of the hole" — the
    surrounding junction then traces around the hole via the rect's
    cross (short) edges, never sharing boundary with the rect's
    sloping side.

    Per user 2026-05-05: hole alignment alone isn't sufficient — the
    snap can leave one of the OTHER (non-aligned) corners off any
    pav.boundary, which produces a long thin sliver between the
    rect's straight edge and pav.boundary along the rect's full
    length.  After alignment, this function ensures **all 4 corners
    sit on pav_union.boundary** (within ``CORNER_ON_BOUNDARY_TOL_M``):

      * Corner already on boundary: keep.
      * Corner not on boundary, within ``NODE_SNAP_RADIUS_M`` of an
        apt.dat pavement vertex: snap to the closest one.
      * No nearby boundary or vertex: shorten rect axis by
        ``AXIS_SHORTEN_M`` (centered) and retry the whole
        snap+validate cycle.  Cap retries at ``MAX_SHORTEN_RETRIES``.

    For each rect's two sloping edges (corners [0,1] and [2,3] per the
    ``_rect_from_axis_extended`` corner convention):
      1. Find the closest apt.dat hole boundary segment that is
         within ``perp_tol_m`` perpendicular distance AND has at
         least ``length_overlap_frac`` of length-overlap with the
         rect's sloping edge.
      2. Project the rect's two sloping-edge corners onto that hole
         segment (= where they'd land if the rect's edge moved onto
         the segment).
      3. If the corner shifts are within ``max_corner_shift_m``,
         rebuild the rect with the new sloping-edge corners and the
         opposite-side corners shifted to preserve width.

    Width is preserved; axis direction is updated to be parallel to
    the new sloping edge; axis MIDPOINT shifts perpendicular to the
    new sloping edge by half-width on the side where the old axis
    was (preserves orientation).

    Returns updated list.  Original taxi_rects list is not mutated.
    """
    if pav_union is None or pav_union.is_empty or not taxi_rects:
        return list(taxi_rects)

    SLOPING_RECT_ROLES_LOCAL = (
        ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL,
        ROLE_STUB,
        ROLE_CROSS_CONNECTOR,
    )

    # Corner-validation tolerances.
    CORNER_ON_BOUNDARY_TOL_M = 0.5  # corner is "on boundary" if within this
    NODE_SNAP_RADIUS_M = 5.0  # snap off-boundary corner to nearest node
    AXIS_SHORTEN_M = 5.0      # shorten by this when no node within range
    MAX_SHORTEN_RETRIES = 5
    MIN_AXIS_LENGTH_M = 30.0  # don't shorten below this

    # Collect big holes from pav_union as polygons + boundary segments.
    holes: list[Polygon] = []
    parts = (list(pav_union.geoms)
             if pav_union.geom_type == "MultiPolygon" else [pav_union])
    for p in parts:
        if p.geom_type != "Polygon":
            continue
        for h in p.interiors:
            hp = Polygon(h)
            if hp.area > min_hole_area_m2:
                holes.append(hp)
    hole_segs: list[tuple[float, float, float, float]] = []
    for H in holes:
        coords = list(H.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        n = len(coords)
        for i in range(n):
            a = coords[i]
            b = coords[(i + 1) % n]
            hole_segs.append((a[0], a[1], b[0], b[1]))

    # Collect ALL pav_union boundary nodes (exterior + holes) for
    # the corner-validation node snap.  Used regardless of whether
    # holes exist — corners may need to snap to exterior verts.
    pav_nodes: list[tuple[float, float]] = []
    for p in parts:
        if p.geom_type != "Polygon":
            continue
        ext = list(p.exterior.coords)
        if ext and ext[0] == ext[-1]:
            ext = ext[:-1]
        pav_nodes.extend(ext)
        for h in p.interiors:
            ri = list(h.coords)
            if ri and ri[0] == ri[-1]:
                ri = ri[:-1]
            pav_nodes.extend(ri)

    from shapely.ops import substring

    out: list[tuple[Polygon, LineString, str, str]] = []
    for rect, axis, role, ref in taxi_rects:
        if role not in SLOPING_RECT_ROLES_LOCAL:
            out.append((rect, axis, role, ref))
            continue
        if rect is None or rect.is_empty or rect.geom_type != "Polygon":
            out.append((rect, axis, role, ref))
            continue
        rc0 = list(rect.exterior.coords)
        if rc0 and rc0[0] == rc0[-1]:
            rc0 = rc0[:-1]
        if len(rc0) != 4:
            out.append((rect, axis, role, ref))
            continue
        original_width = math.hypot(rc0[0][0] - rc0[3][0],
                                     rc0[0][1] - rc0[3][1])
        if original_width < 1.0:
            out.append((rect, axis, role, ref))
            continue

        cur_rect = rect
        cur_axis = axis
        # Default fallback: if no retry produces a fully-on-boundary
        # rect, keep the ORIGINAL.  Don't ship a partially-shortened/
        # realigned rect with off-boundary corners we couldn't fix —
        # that often makes the residue worse than leaving the rect
        # alone (esp. in apron-merged-runway areas where pav_union's
        # boundary doesn't match pav_for_rects, so the corners can't
        # land on pav_union.boundary at all).
        final_rect = rect
        final_axis = axis
        for retry_i in range(MAX_SHORTEN_RETRIES + 1):
            # Step 1: try to align a sloping edge with a hole.
            if hole_segs:
                aligned, aligned_axis = _try_align_sloping_to_hole(
                    cur_rect, cur_axis, hole_segs,
                    perp_tol_m, length_overlap_frac,
                    max_corner_shift_m)
                if aligned is not None:
                    cur_rect = aligned
                    cur_axis = aligned_axis

            # Step 2: validate all 4 corners on pav.boundary.  Snap
            # off-boundary corners to nearest pav node within
            # NODE_SNAP_RADIUS_M.
            cc = list(cur_rect.exterior.coords)
            if cc and cc[0] == cc[-1]:
                cc = cc[:-1]
            if len(cc) != 4:
                # Degenerate after alignment — bail out with original.
                break
            new_corners = list(cc)
            unfixable = False
            for i, c in enumerate(cc):
                d = Point(c).distance(pav_union.boundary)
                if d <= CORNER_ON_BOUNDARY_TOL_M:
                    continue
                # Snap to nearest pav node within radius.
                best_node = None
                best_d = NODE_SNAP_RADIUS_M
                for v in pav_nodes:
                    vd = math.hypot(v[0] - c[0], v[1] - c[1])
                    if vd < best_d:
                        best_d = vd
                        best_node = v
                if best_node is not None:
                    new_corners[i] = (float(best_node[0]),
                                       float(best_node[1]))
                else:
                    unfixable = True
                    break

            if not unfixable:
                # Validate corner-pair separations after snap.
                ok = True
                for i in range(4):
                    for j in range(i + 1, 4):
                        if math.hypot(
                                new_corners[i][0] - new_corners[j][0],
                                new_corners[i][1] - new_corners[j][1]
                                ) < 1.0:
                            ok = False
                            break
                    if not ok:
                        break
                if ok:
                    try:
                        candidate = Polygon(new_corners)
                        if (candidate.is_valid
                                and not candidate.is_empty):
                            final_rect = candidate
                            final_axis = cur_axis
                            break
                    except _GEOM_EXC:
                        pass
                # Polygon invalid — fall through to shorten retry.

            # Step 3: shorten axis by AXIS_SHORTEN_M (centered) and
            # retry from step 1.
            if (cur_axis is None
                    or cur_axis.length
                    < MIN_AXIS_LENGTH_M + AXIS_SHORTEN_M):
                # Axis too short to shrink further; the alignment +
                # node-snap couldn't produce a fully-on-boundary
                # rect.  Fall back to the ORIGINAL (which still has
                # corners on pav.boundary from _rect_from_axis_
                # extended's snap) — better to skip the hole
                # alignment than to ship a rect with corners 30 m+
                # into pav's interior.
                break
            half_shorten = AXIS_SHORTEN_M / 2.0
            try:
                cur_axis = substring(
                    cur_axis,
                    half_shorten,
                    cur_axis.length - half_shorten)
            except _GEOM_EXC:
                break
            new_rect = _rect_from_axis_extended(
                cur_axis, original_width, pav_union,
                apt_vertices=None)
            if new_rect is None or new_rect.is_empty:
                break
            cur_rect = new_rect
            # Loop back to step 1 (alignment + validation).
        # Loop exited without break (all retries failed): final_rect
        # remains the original (set before the loop).
        out.append((final_rect, final_axis, role, ref))

    return out


def _interp_at(ts: list[float], vals: list[float], t: float) -> float:
    """Linear-interpolate ``vals`` (sampled at the monotone fractions
    ``ts`` in [0, 1]) at fraction ``t``."""
    if t <= ts[0]:
        return vals[0]
    if t >= ts[-1]:
        return vals[-1]
    for i in range(1, len(ts)):
        if ts[i] >= t:
            f = (t - ts[i - 1]) / (ts[i] - ts[i - 1])
            return vals[i - 1] + f * (vals[i] - vals[i - 1])
    return vals[-1]


def _find_terrain_splits(ts, e, t_lo, t_hi, axis_len_m,
                          min_seg_m, dev_thresh_m):
    """Recursively locate split fractions along a rect's axis where the
    terrain bulges from the straight chord between the sub-range
    endpoints by more than ``dev_thresh_m``.  Splits at the worst
    deviation, then recurses on each side; never splits closer than
    ``min_seg_m`` to a sub-range end."""
    if (t_hi - t_lo) * axis_len_m < 2.0 * min_seg_m:
        return []
    e_lo = _interp_at(ts, e, t_lo)
    e_hi = _interp_at(ts, e, t_hi)
    margin = min_seg_m / axis_len_m
    best_t = None
    best_dev = 0.0
    for i, t in enumerate(ts):
        if t <= t_lo + margin or t >= t_hi - margin:
            continue
        chord = e_lo + (e_hi - e_lo) * ((t - t_lo) / (t_hi - t_lo))
        dev = abs(e[i] - chord)
        if dev > best_dev:
            best_dev = dev
            best_t = t
    if best_t is None or best_dev < dev_thresh_m:
        return []
    return ([best_t]
            + _find_terrain_splits(ts, e, t_lo, best_t, axis_len_m,
                                   min_seg_m, dev_thresh_m)
            + _find_terrain_splits(ts, e, best_t, t_hi, axis_len_m,
                                   min_seg_m, dev_thresh_m))


def _split_one_rect_along_terrain(rect, sample_dem, min_len_m,
                                   min_seg_m, dev_thresh_m, step_m):
    """Return a list of (sub_poly, sub_axis) splitting ``rect`` at
    interior terrain extrema, or None to keep the rect unsplit."""
    try:
        coords = list(rect.exterior.coords)
    except (GEOSException, TopologicalError):
        return None
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) != 4:
        return None
    c0, c1, c2, c3 = coords           # [H, L, L, H] along the axis
    hi_mid = (0.5 * (c0[0] + c3[0]), 0.5 * (c0[1] + c3[1]))
    lo_mid = (0.5 * (c1[0] + c2[0]), 0.5 * (c1[1] + c2[1]))
    axis_len = math.hypot(lo_mid[0] - hi_mid[0], lo_mid[1] - hi_mid[1])
    if axis_len < min_len_m:
        return None
    n = max(2, int(axis_len / step_m))
    ts = [i / n for i in range(n + 1)]
    e: list[float] = []
    for t in ts:
        x = hi_mid[0] + t * (lo_mid[0] - hi_mid[0])
        y = hi_mid[1] + t * (lo_mid[1] - hi_mid[1])
        v = sample_dem(x, y)
        if v is None:
            return None               # can't sample → leave unsplit
        e.append(float(v))
    fracs = sorted(set(_find_terrain_splits(
        ts, e, 0.0, 1.0, axis_len, min_seg_m, dev_thresh_m)))
    if not fracs:
        return None

    def _a(t):                        # along edge c0->c1 (one side)
        return (c0[0] + t * (c1[0] - c0[0]), c0[1] + t * (c1[1] - c0[1]))

    def _b(t):                        # along edge c3->c2 (other side)
        return (c3[0] + t * (c2[0] - c3[0]), c3[1] + t * (c2[1] - c3[1]))

    bounds = [0.0] + fracs + [1.0]
    pieces = []
    for ta, tb in zip(bounds, bounds[1:]):
        a_hi, a_lo = _a(ta), _a(tb)
        b_hi, b_lo = _b(ta), _b(tb)
        try:
            poly = Polygon([a_hi, a_lo, b_lo, b_hi])   # [H, L, L, H]
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon":
                return None
        except (GEOSException, TopologicalError):
            return None
        m_hi = (0.5 * (a_hi[0] + b_hi[0]), 0.5 * (a_hi[1] + b_hi[1]))
        m_lo = (0.5 * (a_lo[0] + b_lo[0]), 0.5 * (a_lo[1] + b_lo[1]))
        pieces.append((poly, LineString([m_hi, m_lo])))
    return pieces



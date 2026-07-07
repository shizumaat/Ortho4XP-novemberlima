"""Spine crown v2 — lateral drainage built INTO the solve (part 30).

USER RULING 2026-07-07: everything with a spine — runways, taxiways,
service roads — crowns for drainage: the spine stays at the solved /
FAA-profile level and the EDGES sit LOWER by ``rate × lateral distance``
(capped at the corridor half-width).  Rates: ``config.py``
(RUNWAY/TAXI/SERVICE_ROAD_CROWN_TRANSVERSE, cited in docs/STANDARDS.md
"Transverse grades").

v1 (part 29b) applied the crown POST-solve as an edge-drop pass and
needed freeze sets, zero-drop vetoes, all-pairs smoothing and a revoke
valve to fight the already-projected surface — and still had to exclude
runways (crowned corners broke the runway_join spine check).  v2 puts
the crown INSIDE the construction:

* RUNWAYS: ``runway_redistribute._apply_profile_to_shapes`` stamps every
  ring vertex at ``profile(station) − RUNWAY_CROWN_TRANSVERSE ×
  half_width`` (the ring is the pavement EDGE; the persisted profile
  stays the centerline authority).  Every downstream reader — solver
  hard seeds, runway_join anchors, the flex hook, skirts — samples the
  same crowned shape values, so nothing disagrees by construction.
  Seam-bucket vertices are exempt (tile-seam pins are cross-tile terrain
  contracts).

* TAXI / SERVICE corridors: a per-node CROWN DROP FIELD ``c`` (this
  module) — ``c = rate × min(lateral, half_width)`` against the nearest
  same-family centerline, 0 for any node owned by a non-crowned shape,
  a seam pin, or a solver anchor.  The route-profile solve runs in
  UNCROWNED space ``z' = z + c`` (byte-identical to today's solve), and
  the writeback emits ``z = z' − c``: because the field is single-valued
  per canonical node, every weld stays consistent, and because the LAW
  reads the pair offset ``o_ab = c_b − c_a`` (``grade_law.
  crown_pair_offset``), the emitted surface satisfies
  ``|Δz − o_ab| ≤ budget`` exactly wherever the uncrowned solve
  satisfied ``|Δz'| ≤ budget`` — the solver and the validator share the
  one field (exported per node via the axes sidecar ``crown_drops``).

* EMISSION: the spine ridge is an OPEN way with per-node ``alt_abs``
  (``layout.crown_spines`` → ``to_osm`` → ``include_patches`` inserts it
  as constrained DUMMY breakline edges).  Taxi/service spines sample the
  SOLVED route profiles; runway spines sample the crowned pieces + their
  stamped drop (= the profile), so the breakline always agrees with the
  emitted pavement.

Gate: ``config.ENABLE_SPINE_CROWN`` (env ``O4_SPINE_CROWN``, default on).
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point

from .config import (
    CROWN_RUNWAYS,
    CROWN_SERVICE,
    CROWN_TAXI,
    ENABLE_SPINE_CROWN,
    RUNWAY_CROWN_TRANSVERSE,
    SERVICE_ROAD_CROWN_TRANSVERSE,
    TAXI_CROWN_TRANSVERSE,
)
from .layout import (
    ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_SERVICE_JUNCTION,
    ROLE_SERVICE_ROAD,
    ROLE_STUB,
    vertex_bucket,
)

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

# Crown-family roles and their governing centerline family.  Junction faces
# ARE the corridor cross-sections under the curve-native global slice, so
# they crown against the taxi (non-service) centerlines; the service network
# crowns against the row-1206 service lines.
_TAXI_FAMILY = frozenset({
    ROLE_JUNCTION, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
})
_SERVICE_FAMILY = frozenset({ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION})

# Half-width caps: the crown is a cross-SECTION feature.  Taxi corridors cap
# at a code-E half-width; service roads at ~an 8 m road; runways at the
# profile half-width capped below (shoulder-widened runways like HECA's 86 m
# 05C would otherwise crown 40+ cm).
_TAXI_HALFW_CAP_M = 12.0
_SERVICE_HALFW_CAP_M = 4.0
_RUNWAY_HALFW_CAP_M = 30.0

# On-spine tolerance: a node within this of its governing centerline IS a
# spine node (grade_graph.SPINE_PERP_TOL_M) — it carries the solved profile
# and must never crown (crowning it would shift the profile itself).
_ON_SPINE_TOL_M = 1.0        # == grade_graph.SPINE_PERP_TOL_M

# Runway SHADOW adoption: a taxi/service node this close to a crowned
# runway's pavement is VALUE-TIED to the runway edge (the vertex-push pass
# keeps a designed 1.0 m standoff, drift measured to ~1.8 m; the solver
# stamps shadow vertices at the edge-plane altitude and anchors join nodes
# at the edge sample) — it must carry the RUNWAY's drop or the emitted
# surface steps where the runway crowns and the shadow does not.
_RWY_SHADOW_M = 2.5

# Runway-crossing BLEND (part 30c): near a runway-runway crossing the uniform
# per-ref drop is replaced by a per-node DRAINAGE DOME —
#   drop(p) = min over member runways r of
#             RUNWAY_CROWN_TRANSVERSE × min(perp_dist_to_axis_r(p), hw_cap_r)
# so along EITHER centerline the drop is 0 (both ridges continue through the
# intersection at profile level) and in the four quadrants the edges fall
# away smoothly.  The formula is applied ONLY inside the crossing's influence
# zone (the crossing polygon + any runway node within ``_XING_INFLUENCE_M`` of
# a *foreign* member's centerline); on straight sections far from a crossing
# the node keeps the plain uniform drop (keeps longitudinal profile
# reconstruction simple).  The two regimes agree at the zone boundary: a node
# at the edge of its own runway and ≥ hw_cap from every foreign axis evaluates
# to its own uniform drop, so there is no step at the transition.
_XING_INFLUENCE_M = 40.0     # foreign-centerline reach of the blend zone

_SPINE_SAMPLE_STEP_M = 12.0  # breakline node spacing along the spine
_SPINE_EDGE_CLEAR_M = 1.0    # keep spine samples ≥ this inside the pavement
_SPINE_RING_CLEAR_M = 0.9    # and ≥ this from any pavement ring line
_MIN_AXIS_LEN_M = 8.0        # shorter spines: no meaningful crown ridge


def runway_crown_drop_m(half_width_m: float) -> float:
    """THE runway edge drop: ``RUNWAY_CROWN_TRANSVERSE × half_width``,
    half-width capped (a shoulder-widened runway crowns its runway
    cross-section, not the shoulder span).  Rounded to the emit grid so
    the stamped values and the exported drop agree exactly.

    Gated by ``CROWN_RUNWAYS`` (part 30c family scoping): when runways are
    de-scoped this returns 0 and the persisted ``crown_drop_m`` is 0, so
    the runway family carries no drop and emits no ridge."""
    if (not ENABLE_SPINE_CROWN or not CROWN_RUNWAYS
            or not half_width_m or half_width_m <= 0.0):
        return 0.0
    return round(RUNWAY_CROWN_TRANSVERSE
                 * min(float(half_width_m), _RUNWAY_HALFW_CAP_M), 2)


# ── the per-node crown drop field (taxi / service corridors) ────────────────

def _family_lines(layout, service: bool):
    """All centerline geometries of one family, as shapely LineStrings +
    an STRtree (or (None, []) when the family has none)."""
    from shapely.strtree import STRtree
    geoms = []
    for cl in (getattr(layout, "apt_taxi_centerlines", None) or []):
        if bool(getattr(cl, "is_service", False)) != service:
            continue
        ln = getattr(cl, "line", None)
        if ln is None or ln.is_empty or ln.length < 1e-6:
            continue
        geoms.append(ln)
    if not geoms:
        return None, []
    try:
        return STRtree(geoms), geoms
    except _GEOM_EXC:                                   # pragma: no cover
        return None, []


def _nearest_line_dist(tree, geoms, x: float, y: float,
                       search_m: float) -> Optional[float]:
    """Distance to the nearest family line, or None when none is within
    ``search_m`` (cheap bbox query first; exact distance on candidates)."""
    if tree is None:
        return None
    from shapely.geometry import Point as _Pt
    p = _Pt(x, y)
    try:
        k = tree.nearest(p)
    except _GEOM_EXC:                                   # pragma: no cover
        return None
    if k is None:
        return None
    try:
        d = geoms[int(k)].distance(p)
    except _GEOM_EXC:                                   # pragma: no cover
        return None
    return d if d <= search_m else None


def _crossing_blend_axes(layout):
    """Build the per-runway-crossing member-axis geometry used by the
    drainage-dome blend.  Returns ``(members_by_axis, all_member_refs)``:

    * ``members_by_axis`` — ``[(ref, axis_LineString, hw_cap_m), …]`` for
      every runway ref that participates in at least one crossing (its
      persisted profile axis, half-width capped at ``_RUNWAY_HALFW_CAP_M``);
    * ``all_member_refs`` — the set of those refs.

    A node's dome drop is ``min`` over these axes of
    ``RUNWAY_CROWN_TRANSVERSE × min(perp_dist_to_axis, hw_cap)``; the influence
    test uses the SAME axes (a node is in-zone when a *foreign* member axis is
    within ``_XING_INFLUENCE_M``).  Empty when the airport has no crossing."""
    profiles = getattr(layout, "_runway_redistributed_profiles", None) or {}
    member_refs: set = set()
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY_CROSSING:
            continue
        for part in (getattr(s, "ref", "") or "").split("+"):
            if part in profiles:
                member_refs.add(part)
    axes = []
    for ref in member_refs:
        p = profiles[ref]
        ax_a = p["axis_a"]
        dx, dy = p["axis_d"]
        try:
            ln = LineString([ax_a, (ax_a[0] + dx, ax_a[1] + dy)])
        except _GEOM_EXC:                                   # pragma: no cover
            continue
        if ln.is_empty or ln.length < 1e-6:
            continue
        hw_cap = min(float(p.get("half_width_m") or 0.0), _RUNWAY_HALFW_CAP_M)
        axes.append((ref, ln, hw_cap))
    return axes, member_refs


def _crossing_dome_drop(x, y, axes):
    """The drainage-dome drop at ``(x, y)`` — ``min`` over member axes of
    ``RUNWAY_CROWN_TRANSVERSE × min(perp_dist, hw_cap)`` — and whether the
    node is INSIDE the blend influence zone (a foreign axis within
    ``_XING_INFLUENCE_M``).  Returns ``(drop, in_zone, near_dist)`` where
    ``near_dist`` is the smallest perpendicular distance to any member axis
    (0 on a centerline → drop 0, both ridges pass through)."""
    p = Point(x, y)
    best = None
    near = None
    for (_ref, ln, hw_cap) in axes:
        try:
            d = ln.distance(p)
        except _GEOM_EXC:                                   # pragma: no cover
            continue
        if near is None or d < near:
            near = d
        contrib = RUNWAY_CROWN_TRANSVERSE * min(d, hw_cap)
        if best is None or contrib < best:
            best = contrib
    if best is None:
        return 0.0, False, None
    # in-zone when a *second* (foreign) axis is close: the nearest axis is the
    # node's own runway edge, so a foreign axis within the influence reach means
    # the node sits in the crossing's drainage region.
    n_close = sum(1 for (_r, ln, _h) in axes
                  if ln.distance(p) <= _XING_INFLUENCE_M)
    in_zone = n_close >= 2
    return best, in_zone, near


def build_crown_drop_field(layout, nodes, bucket_to_idx,
                           freeze_idx) -> Dict[int, float]:
    """Compute the per-node crown drop ``c`` (metres, > 0).  Returns
    ``{node_idx: drop}`` (the writeback transform set) and persists:

    * ``layout._crown_drop_key``  — canonical (x, y) key → drop (consumed by
      ``final_grade_projection``'s transform and the in-memory validators);
    * ``layout._crown_drop_ll``   — ``[(lat, lon, drop), …]`` (the axes
      sidecar export the OSM validator maps to nids).

    Field law (single source, both readers), first match wins per node:

    * FROZEN (c = 0): any owner is a non-crown shape (apron / terminal /
      building / boundary / groundside / adopted / degenerate), the node
      is a tile-seam bucket, or it is a solver value contract passed in
      ``freeze_idx`` (seam pins, building seats, groundside mouth welds,
      seam spine anchors).
    * RUNWAY-owned (incl. runway_crossing): the UNIFORM per-ref drop
      ``profiles[ref]['crown_drop_m']`` (crossings: min over member
      refs; shared keys: min over owning refs — uniformity keeps the
      reconstructed longitudinal profile untouched), axially TAPERED at
      ``TAXI_CROWN_TRANSVERSE`` toward any seam-bucket vertex so the
      crown eases into the uncrowned seam pieces instead of stepping.
    * TAXI / SERVICE corridor: ``rate_family × min(lateral to the
      nearest same-family centerline, half_width_family)``, 0 on the
      spine itself (≤ the spine tolerance); MIN over owning families.

    FAMILY SCOPING (part 30c): ``CROWN_RUNWAYS`` / ``CROWN_TAXI`` /
    ``CROWN_SERVICE`` gate which families contribute.  A de-scoped family's
    nodes are simply not crowned (c = 0, held via the frozen-key set) — the
    code path stays intact, it just registers no drop.  Default this
    iteration = runways only.

    RUNWAY-CROSSING BLEND (part 30c): a runway node inside a crossing's
    influence zone takes the drainage-dome drop (``_crossing_dome_drop``)
    instead of the uniform per-ref value — 0 on both centerlines, tapering to
    the min member half-width in the quadrants — so the two ridges cross at
    profile level and the edges blend smoothly."""
    cps = getattr(layout, "canonical_points", None)
    if cps is None or not ENABLE_SPINE_CROWN:
        layout._crown_drop_key = {}
        layout._crown_drop_ll = []
        return {}

    profiles = getattr(layout, "_runway_redistributed_profiles", None) or {}

    def _ref_drop(ref: str) -> float:
        drops = []
        for part in (ref or "").split("+"):
            p = profiles.get(part)
            if p and p.get("crown_drop_m"):
                drops.append(float(p["crown_drop_m"]))
        return min(drops) if drops else 0.0

    # ownership: runway drops (min across refs), taxi/service families,
    # frozen keys (any non-crown owner / degenerate crown shape).
    frozen_keys: set = set()
    # Keys frozen ONLY because their crown family (taxi/junction/service) is
    # de-scoped this iteration — held at c = 0 for the corridor readers, but a
    # co-owning RUNWAY's drop overrides them (part 30c family scoping).
    descoped_frozen: set = set()
    fam_by_key: Dict[object, set] = {}
    rwy_by_key: Dict[object, float] = {}
    # Runway-SHADOW candidates: taxi/service ring nodes eligible to be
    # value-tied to a crowned runway edge (the vertex-push standoff keeps them
    # within ~2.5 m of the runway).  Collected INDEPENDENT of the taxi/service
    # crown-family gating (part 30c): even when those families are de-scoped,
    # a node hugging a crowned runway must carry the RUNWAY's drop or the
    # emitted surface STEPS where the runway crowns and the neighbour does not.
    shadow_cand_keys: set = set()
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        is_runway = s.role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)
        _fam_on = ((s.role in _TAXI_FAMILY and CROWN_TAXI)
                   or (s.role in _SERVICE_FAMILY and CROWN_SERVICE))
        eligible = (
            _fam_on
            and not getattr(s, "adopts_apron_grade", False)
            and s.polygon.geom_type == "Polygon"
            and not s.polygon.interiors)
        try:
            if s.polygon.geom_type == "Polygon":
                rings = [list(s.polygon.exterior.coords)]
                rings.extend(list(h.coords) for h in s.polygon.interiors)
            else:
                rings = []
                for g in getattr(s.polygon, "geoms", ()):
                    if g.geom_type == "Polygon":
                        rings.append(list(g.exterior.coords))
                        rings.extend(list(h.coords) for h in g.interiors)
        except _GEOM_EXC:
            continue
        if is_runway:
            d = _ref_drop(getattr(s, "ref", "") or "")
            for ring in rings:
                for (x, y) in ring:
                    key = cps.get_or_add(float(x), float(y))
                    if d <= 0.0:
                        frozen_keys.add(key)   # uncrowned runway: hold it
                    else:
                        prev = rwy_by_key.get(key)
                        rwy_by_key[key] = d if prev is None else min(prev, d)
            continue
        _is_crown_family = (s.role in _TAXI_FAMILY or s.role in _SERVICE_FAMILY)
        fam = ("service" if s.role in _SERVICE_FAMILY else "taxi")
        # A well-formed taxi/service polygon node is a runway-shadow candidate
        # regardless of whether its own family is crowned this iteration.
        _shadow_ok = (not getattr(s, "adopts_apron_grade", False)
                      and s.polygon.geom_type == "Polygon"
                      and not s.polygon.interiors)
        for ring in rings:
            for (x, y) in ring:
                key = cps.get_or_add(float(x), float(y))
                if _shadow_ok:
                    shadow_cand_keys.add(key)
                if eligible:
                    fam_by_key.setdefault(key, set()).add(fam)
                elif _is_crown_family:
                    # De-SCOPED crown family (taxi/junction/service off this
                    # iteration): freeze at c = 0, but let a co-owning RUNWAY's
                    # drop still WIN (else a runway edge vertex shared with a
                    # de-scoped junction stays uncrowned and the runway's own
                    # edge steps at the weld — part 30c).
                    descoped_frozen.add(key)
                else:
                    # Genuinely non-crown owner (apron / terminal / building /
                    # boundary / groundside): a hard c = 0 contract.
                    frozen_keys.add(key)

    seam_keys = getattr(layout, "_seam_anchor_keys", None) or set()
    taxi_tree, taxi_geoms = _family_lines(layout, service=False)
    svc_tree, svc_geoms = _family_lines(layout, service=True)

    # Seam-bucket vertex positions (for the runway axial taper).
    seam_pts: List[Tuple[float, float]] = []
    if seam_keys:
        for key in set(rwy_by_key) | set(fam_by_key):
            idx = bucket_to_idx.get(key)
            if idx is None:
                continue
            x, y = nodes[idx]
            if vertex_bucket(float(x), float(y)) in seam_keys:
                seam_pts.append((x, y))

    drop_by_idx: Dict[int, float] = {}
    drop_by_key: Dict[object, float] = {}

    def _register(key, idx, c):
        c = round(c, 3)
        if c > 0.005:
            drop_by_idx[idx] = c
            drop_by_key[key] = c

    # Runway-crossing drainage-dome axes (empty when no crossing): a runway
    # node inside a crossing influence zone takes the per-node dome drop in
    # place of the uniform per-ref value, so the two centerlines meet at
    # profile level and the quadrants blend (part 30c).
    _xing_axes, _ = _crossing_blend_axes(layout)

    # RUNWAY-owned keys (runway wins over co-owning taxi families).
    for key, d in rwy_by_key.items():
        if key in frozen_keys:
            continue
        idx = bucket_to_idx.get(key)
        if idx is None or idx in freeze_idx:
            continue
        x, y = nodes[idx]
        if vertex_bucket(float(x), float(y)) in seam_keys:
            continue
        if _xing_axes:
            dome, in_zone, _near = _crossing_dome_drop(x, y, _xing_axes)
            if in_zone:
                # dome ≤ own uniform by construction (own-axis contribution
                # caps at the node's uniform); take it as the blended drop.
                d = min(d, dome)
        if seam_pts:
            d_seam = min(math.hypot(x - sx, y - sy)
                         for (sx, sy) in seam_pts)
            d = min(d, TAXI_CROWN_TRANSVERSE * d_seam)
        _register(key, idx, d)

    # Crowned-runway pavement (for the shadow-adoption rule below).
    rwy_shadow = None
    _rwy_shadow_items = []
    for s in layout.shapes:
        if (s.role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)
                and s.polygon is not None and not s.polygon.is_empty):
            d = _ref_drop(getattr(s, "ref", "") or "")
            if d > 0.0:
                _rwy_shadow_items.append((s.polygon, d))
    if _rwy_shadow_items:
        try:
            from shapely.strtree import STRtree as _ShTree
            rwy_shadow = (_ShTree([p for (p, _d) in _rwy_shadow_items]),
                          _rwy_shadow_items)
        except _GEOM_EXC:                               # pragma: no cover
            rwy_shadow = None

    def _shadow_drop(x, y):
        """The crowned-runway edge drop this node must adopt (value-tied
        within ``_RWY_SHADOW_M`` of a crowned runway), seam-tapered, or None.
        Uses the crossing dome inside a crossing influence zone so a shadow
        node at the crossing meets the blended runway edge, not the uniform
        drop."""
        if rwy_shadow is None:
            return None
        tree, items = rwy_shadow
        p = Point(x, y)
        try:
            k = tree.nearest(p)
        except _GEOM_EXC:                               # pragma: no cover
            return None
        if k is None:
            return None
        poly, d_ref = items[int(k)]
        try:
            if poly.distance(p) > _RWY_SHADOW_M:
                return None
        except _GEOM_EXC:
            return None
        best = d_ref
        if _xing_axes:
            dome, in_zone, _n = _crossing_dome_drop(x, y, _xing_axes)
            if in_zone:
                best = min(best, dome)
        if seam_pts:
            d_seam = min(math.hypot(x - sx, y - sy) for (sx, sy) in seam_pts)
            best = min(best, TAXI_CROWN_TRANSVERSE * d_seam)
        return best

    # RUNWAY SHADOW pass (part 30c): value-tie every taxi/service ring node
    # hugging a crowned runway to that runway's edge drop — RUN INDEPENDENT of
    # the taxi/service crown-family gating so a de-scoped corridor still welds
    # cleanly to the crowned runway (else a step appears at the join).
    for key in shadow_cand_keys:
        if key in frozen_keys or key in rwy_by_key or key in drop_by_key:
            continue
        idx = bucket_to_idx.get(key)
        if idx is None or idx in freeze_idx:
            continue
        x, y = nodes[idx]
        if vertex_bucket(float(x), float(y)) in seam_keys:
            continue
        best = _shadow_drop(x, y)
        if best is not None:
            _register(key, idx, best)

    # TAXI / SERVICE corridor keys.
    for key, fams in fam_by_key.items():
        if key in frozen_keys or key in rwy_by_key or key in drop_by_key:
            continue
        idx = bucket_to_idx.get(key)
        if idx is None or idx in freeze_idx:
            continue
        x, y = nodes[idx]
        if vertex_bucket(float(x), float(y)) in seam_keys:
            continue
        # RUNWAY SHADOW: value-tied to the runway edge → the runway's drop.
        best = _shadow_drop(x, y)
        if best is not None:
            _register(key, idx, best)
            continue
        drops = []
        for fam in fams:
            if fam == "taxi":
                lat = _nearest_line_dist(taxi_tree, taxi_geoms, x, y,
                                         search_m=1e9)
                if lat is None or lat <= _ON_SPINE_TOL_M:
                    drops.append(0.0)
                    continue
                drops.append(TAXI_CROWN_TRANSVERSE
                             * min(lat, _TAXI_HALFW_CAP_M))
            else:
                lat = _nearest_line_dist(svc_tree, svc_geoms, x, y,
                                         search_m=1e9)
                if lat is None or lat <= _ON_SPINE_TOL_M:
                    drops.append(0.0)
                    continue
                drops.append(SERVICE_ROAD_CROWN_TRANSVERSE
                             * min(lat, _SERVICE_HALFW_CAP_M))
        if not drops:
            continue
        _register(key, idx, min(drops))

    # Equalize over each crown-family RECT ring (and thereby its
    # level-coupled flat ends): a rect emits as a tilted PLANE whose axial
    # grade may sit exactly at cap (flex law: taxi at max cap first) — a
    # corner-to-corner drop DIFFERENCE would tip it over.  MIN wins;
    # runway-owned keys keep their (uniform) runway drop.
    from .elevation_per_surface.solver_primitives import (
        SLOPING_RECT_ROLES as _RECT_ROLES)
    for s in layout.shapes:
        if (s.role not in _RECT_ROLES or s.polygon is None
                or s.polygon.is_empty or s.polygon.geom_type != "Polygon"):
            continue
        try:
            ring = list(s.polygon.exterior.coords)[:-1]
        except _GEOM_EXC:
            continue
        keys = [cps.get_or_add(float(x), float(y)) for (x, y) in ring]
        own_keys = [k for k in keys if k not in rwy_by_key]
        vals = [drop_by_key.get(k) for k in own_keys]
        if not vals:
            continue
        if any(v is None for v in vals):
            # a frozen / uncrowned corner ⇒ the whole rect stays flat-space
            for k in own_keys:
                if k in drop_by_key:
                    idx = bucket_to_idx.get(k)
                    drop_by_key.pop(k, None)
                    if idx is not None:
                        drop_by_idx.pop(idx, None)
            continue
        mn = min(vals)
        for k in own_keys:
            drop_by_key[k] = mn
            idx = bucket_to_idx.get(k)
            if idx is not None:
                drop_by_idx[idx] = mn

    layout._crown_drop_key = dict(drop_by_key)
    layout._crown_drop_ll = []
    for idx, c in drop_by_idx.items():
        x, y = nodes[idx]
        la, lo = layout.m_to_ll(x, y)
        layout._crown_drop_ll.append((round(la, 7), round(lo, 7), c))
    return drop_by_idx


def extend_field_to_new_ring_nodes(layout, bucket_to_idx) -> int:
    """Extend ``layout._crown_drop_key`` to ring vertices minted AFTER the
    solve (planarize inserts, final T-vertex weld adoptions).

    Such a vertex's VALUE was linearly interpolated along one owning
    ring's edge, so its drop is VALUE-DERIVED: on each owning ring, lift
    the flanking solve-time vertices into uncrowned space (value + their
    field drop), interpolate z′ at the new vertex's arc position, and take
    ``c = z′_interp − value`` — exact for the ring the insert was born on;
    across rings the MAX wins (the born ring shows the full drop, the
    other rings' interpolation can only under-read it).  A geometric
    nearest-node adoption measured wrong at CYXY: a T-weld insert between
    a crowned and an uncrowned runway vertex read a phantom 4.2 % pair.
    Returns the number of nodes added; updates ``_crown_drop_ll``."""
    if not ENABLE_SPINE_CROWN:
        return 0
    field = getattr(layout, "_crown_drop_key", None)
    solved_keys = getattr(layout, "_crown_solved_keys", None)
    cps = getattr(layout, "canonical_points", None)
    if not field or not solved_keys or cps is None:
        return 0
    from .elevation_per_surface.solver_primitives import PAVEMENT_ROLES
    new_c: Dict[object, float] = {}
    new_pos: Dict[object, Tuple[float, float]] = {}
    for s in layout.shapes:
        if (s.role not in PAVEMENT_ROLES or s.polygon is None
                or s.polygon.is_empty or s.polygon.geom_type != "Polygon"):
            continue
        try:
            ring = list(s.polygon.exterior.coords)[:-1]
        except _GEOM_EXC:
            continue
        n = len(ring)
        if n < 3:
            continue
        keys = [cps.get_or_add(float(x), float(y)) for (x, y) in ring]
        if all(k in solved_keys for k in keys):
            continue
        # per-vertex emitted values (open ring).
        vals: Optional[List[float]] = None
        if s.node_altitudes is not None:
            na = list(s.node_altitudes)
            if len(na) == n + 1:
                na = na[:-1]
            if len(na) == n and all(v is not None for v in na):
                vals = [float(v) for v in na]
        elif s.altitude is not None:
            vals = [float(s.altitude)] * n
        elif (s.altitude_high is not None and s.altitude_low is not None
              and n == 4):
            hi, lo = float(s.altitude_high), float(s.altitude_low)
            vals = [hi, lo, lo, hi]
        if vals is None:
            continue
        seg = [math.hypot(ring[(i + 1) % n][0] - ring[i][0],
                          ring[(i + 1) % n][1] - ring[i][1])
               for i in range(n)]
        for i in range(n):
            if keys[i] in solved_keys:
                continue
            # walk to the nearest SOLVE-TIME vertex on each side.
            db = 0.0
            j = i
            found_b = found_f = False
            for _ in range(n):
                j = (j - 1) % n
                db += seg[j]
                if keys[j] in solved_keys:
                    found_b = True
                    break
            df = 0.0
            k2 = i
            for _ in range(n):
                df += seg[k2]
                k2 = (k2 + 1) % n
                if keys[k2] in solved_keys:
                    found_f = True
                    break
            if not (found_b and found_f):
                continue
            c_max = max(field.get(keys[j], 0.0), field.get(keys[k2], 0.0))
            # Both flanks uncrowned ⇒ the insert inherits NO crown; a nonzero
            # z_interp−value here is just the ring's own non-planarity, not a
            # drop (spurious ≤5 cm drops appeared on uncrowned junction rings
            # once the taxi family was de-scoped).  Only a crowned flank can
            # give a new vertex a drop.
            if c_max <= 0.0:
                continue
            zb = vals[j] + field.get(keys[j], 0.0)
            zf = vals[k2] + field.get(keys[k2], 0.0)
            tot = db + df
            z_interp = zb if tot <= 1e-9 else zb + (zf - zb) * (db / tot)
            c = min(max(0.0, z_interp - vals[i]), c_max + 0.05)
            key = keys[i]
            if c > new_c.get(key, 0.0):
                new_c[key] = c
                new_pos[key] = ring[i]
    n_added = 0
    if new_c:
        ll_new = list(getattr(layout, "_crown_drop_ll", None) or [])
        for key, c in new_c.items():
            solved_keys.add(key)
            c = round(c, 3)
            if c > 0.005:
                field[key] = c
                x, y = new_pos[key]
                la, lo = layout.m_to_ll(x, y)
                ll_new.append((round(la, 7), round(lo, 7), c))
                n_added += 1
        layout._crown_drop_ll = ll_new
    return n_added


def crown_drop_at(layout, x: float, y: float) -> float:
    """The crown drop at a coordinate, via the canonical-point registry —
    the in-memory validators' lookup (same field both readers share)."""
    field = getattr(layout, "_crown_drop_key", None)
    if not field:
        return 0.0
    reg = getattr(layout, "canonical_points", None)
    if reg is None:
        return 0.0
    try:
        cp = reg.find_nearest(x, y, reg.tol_m)
    except Exception:                                   # pragma: no cover
        return 0.0
    if cp is None:
        return 0.0
    return field.get(cp, 0.0)


# ── spine breakline emission ─────────────────────────────────────────────────

def _emit_ways_for_profile(seg, ax, alt_at, inner, ring_tree, ring_geoms,
                           layout) -> List[Tuple[list, list]]:
    """Sample one clipped spine segment every ~12 m; drop samples outside
    the eligible inner buffer or within the ring clearance; split into ways
    at gaps.  Returns ``[(latlon_pts, alts), …]``."""
    out: List[Tuple[list, list]] = []
    n_pts = max(2, int(seg.length / _SPINE_SAMPLE_STEP_M) + 1)
    way_ll: list = []
    way_alt: list = []

    def _flush():
        nonlocal way_ll, way_alt
        if len(way_ll) >= 2:
            out.append((way_ll, way_alt))
        way_ll, way_alt = [], []

    for j in range(n_pts):
        p = seg.interpolate(j * seg.length / (n_pts - 1))
        ok = True
        try:
            if inner is not None and not inner.covers(p):
                ok = False
        except _GEOM_EXC:
            ok = False
        if ok and ring_tree is not None:
            try:
                k = ring_tree.nearest(p)
                if (k is not None
                        and ring_geoms[int(k)].distance(p)
                        < _SPINE_RING_CLEAR_M):
                    ok = False
            except _GEOM_EXC:
                pass
        if not ok:
            _flush()
            continue
        try:
            st = ax.project(p)
        except _GEOM_EXC:
            _flush()
            continue
        a = alt_at(st)
        if a is None:
            _flush()
            continue
        way_ll.append(layout.m_to_ll(p.x, p.y))
        way_alt.append(round(float(a), 2))
    _flush()
    return out


def emit_crown_spines(layout, nodes, bucket_to_idx, elev,
                      drop_by_idx) -> int:
    """Populate ``layout.crown_spines`` from the SOLVED route profiles
    (taxi + service centerlines: the solved elevations of the graph nodes
    ON each line, interpolated by arc) and from the crowned runway pieces
    (edge sample + stamped drop = the centerline profile).  Returns the
    number of spine ways staged."""
    if not ENABLE_SPINE_CROWN:
        return 0
    from shapely.strtree import STRtree

    # eligible pavement + its rings (clip + clearance geometry).
    polys = []
    ring_geoms = []
    for s in layout.shapes:
        if (s.polygon is None or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        if ((s.role in _TAXI_FAMILY or s.role in _SERVICE_FAMILY)
                and not getattr(s, "adopts_apron_grade", False)):
            polys.append(s.polygon)
        if s.role in _TAXI_FAMILY or s.role in _SERVICE_FAMILY \
                or s.role == ROLE_RUNWAY:
            try:
                ring_geoms.append(LineString(s.polygon.exterior.coords))
            except _GEOM_EXC:
                continue
    inner = None
    if polys:
        try:
            from shapely.ops import unary_union
            inner = unary_union(polys).buffer(-_SPINE_EDGE_CLEAR_M)
            if inner.is_empty:
                inner = None
            else:
                from shapely.prepared import prep
                inner = prep(inner)
        except _GEOM_EXC:
            inner = None
    ring_tree = None
    if ring_geoms:
        try:
            ring_tree = STRtree(ring_geoms)
        except _GEOM_EXC:                               # pragma: no cover
            ring_tree = None

    # node STRtree for on-line profile extraction.
    from shapely.geometry import Point as _Pt, box as _box
    try:
        node_pts = [_Pt(x, y) for (x, y) in nodes]
        node_tree = STRtree(node_pts)
    except _GEOM_EXC:                                   # pragma: no cover
        return 0

    spine_ways: List[Tuple[list, list]] = []

    # ── taxi + service routes: solved on-line node profile ──
    # FAMILY SCOPING (part 30c): only emit a family's ridge when that family
    # is crowned; de-scoped taxi/service lines carry no drop, so a ridge there
    # would sit at the flat surface and be a spurious breakline.
    seen_lines = set()
    for cl in (getattr(layout, "apt_taxi_centerlines", None) or []):
        ln = getattr(cl, "line", None)
        if ln is None or ln.is_empty or ln.length < _MIN_AXIS_LEN_M:
            continue
        _is_svc = bool(getattr(cl, "is_service", False))
        if (_is_svc and not CROWN_SERVICE) or (not _is_svc and not CROWN_TAXI):
            continue
        if id(ln) in seen_lines:
            continue
        seen_lines.add(id(ln))
        try:
            xs, ys = zip(*ln.coords)
            q = _box(min(xs) - _ON_SPINE_TOL_M, min(ys) - _ON_SPINE_TOL_M,
                     max(xs) + _ON_SPINE_TOL_M, max(ys) + _ON_SPINE_TOL_M)
            cand = [int(k) for k in node_tree.query(q)]
        except _GEOM_EXC:
            continue
        prof: List[Tuple[float, float]] = []
        for k in cand:
            p = node_pts[k]
            try:
                d = ln.distance(p)
            except _GEOM_EXC:
                continue
            if d > _ON_SPINE_TOL_M:
                continue
            try:
                prof.append((ln.project(p), float(elev[k])))
            except _GEOM_EXC:
                continue
        if len(prof) < 2:
            continue
        prof.sort()
        merged: List[Tuple[float, float, int]] = []
        for st, a in prof:
            if merged and st - merged[-1][0] <= 3.0:
                pst, pa, pn = merged[-1]
                merged[-1] = (pst, (pa * pn + a) / (pn + 1), pn + 1)
            else:
                merged.append((st, a, 1))
        prof2 = [(st, a) for (st, a, _n) in merged]
        if len(prof2) < 2:
            continue

        def _alt_at(st, _prof=prof2):
            if st <= _prof[0][0]:
                return _prof[0][1]
            if st >= _prof[-1][0]:
                return _prof[-1][1]
            for j in range(1, len(_prof)):
                if st <= _prof[j][0]:
                    s0, a0 = _prof[j - 1]
                    s1, a1 = _prof[j]
                    f = (st - s0) / max(1e-6, s1 - s0)
                    return a0 + f * (a1 - a0)
            return _prof[-1][1]

        spine_ways.extend(_emit_ways_for_profile(
            ln, ln, _alt_at, inner, ring_tree, ring_geoms, layout))

    # ── runways: the persisted (post-flex) centerline profile IS the
    # spine — the pavement emits at profile − crown_drop (field), so the
    # breakline at profile renders the ridge.  Clip inside each piece.
    #
    # CROSSING CONTINUITY (part 30c, closes the v2 ridge gap): a runway ref's
    # ridge must also run THROUGH any runway_crossing polygon it belongs to.
    # We clip each ref's axis against the union of its ROLE_RUNWAY pieces AND
    # every ROLE_RUNWAY_CROSSING whose members include this ref, so the ridge
    # is one continuous breakline at the ref's own profile.  At the crossing
    # both member profiles agree (runway_segments centerline-crossing
    # reconciliation forces the same ``agreed`` altitude — probe: ≤ 2 cm), so
    # the two ridges genuinely meet where the centerlines cross.
    from .runway_redistribute import _interp_profile
    profiles = getattr(layout, "_runway_redistributed_profiles", None) or {}
    pieces_by_ref: Dict[str, list] = {}
    xing_by_ref: Dict[str, list] = {}
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty \
                or s.polygon.geom_type != "Polygon":
            continue
        if s.role == ROLE_RUNWAY and s.ref:
            pieces_by_ref.setdefault(s.ref, []).append(s.polygon)
        elif s.role == ROLE_RUNWAY_CROSSING and s.ref:
            for part in s.ref.split("+"):
                if part in profiles:
                    xing_by_ref.setdefault(part, []).append(s.polygon)
    for ref in set(pieces_by_ref) | set(xing_by_ref):
        p = profiles.get(ref)
        # Only crowned runways emit a ridge (crown_drop_m > 0 ⇒ CROWN_RUNWAYS
        # on and the runway actually crowns); a flat runway has no ridge.
        if not p or not float(p.get("crown_drop_m") or 0.0):
            continue
        ax_a = p["axis_a"]
        dx, dy = p["axis_d"]
        try:
            ax = LineString([ax_a, (ax_a[0] + dx, ax_a[1] + dy)])
        except _GEOM_EXC:
            continue
        if ax.length < _MIN_AXIS_LEN_M:
            continue
        fr, el = p["fractions"], p["elevs"]
        ax_len = ax.length

        def _alt_at_rwy(st, _fr=fr, _el=el, _L=ax_len):
            return _interp_profile(_fr, _el, st / max(_L, 1e-6))

        # Union the ref's runway pieces + its crossing polygons, then clip the
        # axis against the inner buffer of that union so the ridge is a single
        # continuous line across the crossing rather than gapping at it.
        parts = list(pieces_by_ref.get(ref, ())) + list(
            xing_by_ref.get(ref, ()))
        if not parts:
            continue
        try:
            from shapely.ops import unary_union
            body = unary_union(parts)
            body_inner = body.buffer(-_SPINE_EDGE_CLEAR_M)
            if body_inner.is_empty:
                continue
            clipped = ax.intersection(body_inner)
        except _GEOM_EXC:
            continue
        segs = ([clipped] if clipped.geom_type == "LineString"
                else [g for g in getattr(clipped, "geoms", ())
                      if g.geom_type == "LineString"])
        for seg in segs:
            if seg.length < 3.0:
                continue
            spine_ways.extend(_emit_ways_for_profile(
                seg, ax, _alt_at_rwy, None, ring_tree, ring_geoms,
                layout))

    if spine_ways:
        existing = getattr(layout, "crown_spines", None) or []
        layout.crown_spines = existing + spine_ways
    return len(spine_ways)

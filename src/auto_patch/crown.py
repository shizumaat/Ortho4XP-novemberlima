"""Spine crown — lateral drainage curvature for spine-carrying pavement.

USER RULING 2026-07-07: everything with a spine (runways, taxiways,
service roads) crowns for drainage — the spine stays at the solved
surface level and the EDGES drop relative to it, each role at its own
transverse rate (docs/STANDARDS.md "Transverse grades", researched from
FAA AC 150/5300-13B / EASA CS-ADR-DSN / ICAO Annex 14 / AASHTO).

Mechanism (two halves, both here):

1. EDGE DROP.  For every crown-eligible shape (a shape that carries a
   usable axis — corridor junctions / taxi rects / service roads with
   ``source_axis``, runways via their persisted redistributed profile
   axis), each ring vertex drops by ``rate × min(lateral, half_width)``,
   tapered near the axis ends.  Pre-crown the solver leaves the whole
   cross-section at the spine level (ring nodes follow the route
   profile), so the drop turns the flat section into a crowned one.

2. SPINE BREAKLINE.  The spine polyline is emitted as an OPEN way with
   per-node ``alt_abs`` at the PRE-crown surface level —
   ``O4_Vector_Map.include_patches`` inserts open patch ways as
   constrained DUMMY edges, so the triangulation renders the ridge with
   no polygon splitting (same mechanism as the legacy
   ``altitude_high/low`` cross-cuts).

Safety rules (why the crown is a LATE, weld-aware pass):

* a vertex whose canonical point is ALSO owned by a non-crowned shape
  (apron, terminal, building, boundary, groundside, clearance…) never
  moves — the crown tapers to zero at flush welds, so no tear is minted
  (buildings hold the 1 % frontage law; aprons meet taxiways flush);
* a TILE-SEAM vertex (within ``tile_cut._SEAM_LINE_TOL_M`` of an
  integer lat/lon line) never moves — seam pins are cross-tile
  contracts (see the part-29 SPLP scarp);
* a canonical point shared by SEVERAL crowned shapes takes the MINIMUM
  proposed drop, written into every owner, so the emit-time consensus
  cannot split the node into a vertical tear.

Gate: ``config.ENABLE_SPINE_CROWN`` (env ``O4_SPINE_CROWN``, default on).
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point

from .config import (
    RUNWAY_CROWN_TRANSVERSE,
    SERVICE_ROAD_CROWN_TRANSVERSE,
    SERVICE_ROAD_MAX_TRANSVERSE,
    TAXI_CROWN_TRANSVERSE,
)
from .layout import (
    ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_SECONDARY_PARALLEL,
    ROLE_SERVICE_ROAD,
    ROLE_STUB,
)

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

# role → crown rate.  Junction shapes are eligible ONLY when they carry a
# ``source_axis`` (curve-native corridor faces); axis-less junction faces
# (convergence areas) have no meaningful cross-section and stay flat.
#
# RUNWAYS EXCLUDED for now (part 29): dropping runway edge corners
# breaks the FAA-profile readers that treat edge values as profile
# authority — the runway_join spine check read a 23 % step at CYXY
# where a crowned corner met its join anchor.  The runway crown needs
# those readers (runway_join anchors, flex audit, skirt base reads,
# seam pins) taught about the crown offset first — queued; the
# profile-axis machinery below already supports it.
_CROWN_RATES = {
    ROLE_PRIMARY_PARALLEL: TAXI_CROWN_TRANSVERSE,
    ROLE_SECONDARY_PARALLEL: TAXI_CROWN_TRANSVERSE,
    ROLE_STUB: TAXI_CROWN_TRANSVERSE,
    ROLE_CROSS_CONNECTOR: TAXI_CROWN_TRANSVERSE,
    ROLE_JUNCTION: TAXI_CROWN_TRANSVERSE,
    ROLE_SERVICE_ROAD: SERVICE_ROAD_CROWN_TRANSVERSE,
}
_ = RUNWAY_CROWN_TRANSVERSE      # part-2 wiring keeps the constant live

_SPINE_SAMPLE_STEP_M = 12.0     # breakline node spacing along the spine
_SPINE_EDGE_CLEAR_M = 1.0       # keep spine nodes off the ring (no merge)
_END_TAPER_M = 10.0             # crown ramps in over this from axis ends
_MIN_AXIS_LEN_M = 8.0           # shorter axes: no meaningful crown
# Half-width caps per role: the 75th-percentile lateral estimate blows
# up on big junction faces crossed by a fallback centerline (CYXY: 60 m
# laterals → 60 cm drops → 6 % mesh-edge pairs).  A crown is a
# cross-SECTION feature; cap it at a plausible half-width.
_HALFW_CAP_M = {
    ROLE_SERVICE_ROAD: 4.0,     # ~8 m road
    ROLE_RUNWAY: 30.0,          # profile half_width_m usually governs
}
_HALFW_CAP_DEFAULT_M = 12.0     # taxiway family (code E ~23 m wide)


def _seam_tol_deg(layout) -> Tuple[float, float]:
    """(dlat, dlon) degree tolerances matching tile_cut._SEAM_LINE_TOL_M."""
    from .tile_cut import _SEAM_LINE_TOL_M
    lat0, _ = layout.anchor
    dlat = _SEAM_LINE_TOL_M / 111320.0
    cos0 = max(1e-6, math.cos(math.radians(lat0)))
    dlon = _SEAM_LINE_TOL_M / (111320.0 * cos0)
    return dlat, dlon


def _is_seam_vertex(layout, x: float, y: float,
                    tol_deg: Tuple[float, float]) -> bool:
    lat, lon = layout.m_to_ll(x, y)
    return (abs(lat - round(lat)) <= tol_deg[0]
            or abs(lon - round(lon)) <= tol_deg[1])


def _shape_axis(layout, s) -> Optional[LineString]:
    """The spine axis for shape ``s``, or None when it has no usable one.

    ``source_axis`` when the shape still carries one; runways fall back
    to their persisted redistributed-profile axis; everything else falls
    back to the longest matching apt.dat centerline crossing the shape —
    the clip / decompose / tile-cut passes recreate ``BuiltShape``s
    without ``source_axis``, so most corridor faces reach emission
    bare (SPLP: only 9 of ~35 spine shapes kept an axis).
    """
    ax = getattr(s, "source_axis", None)
    if ax is not None and not ax.is_empty and ax.length >= _MIN_AXIS_LEN_M:
        return ax
    if s.role == ROLE_RUNWAY:
        profiles = getattr(layout, "_runway_redistributed_profiles", None)
        p = (profiles or {}).get(s.ref)
        if p:
            ax_a = p["axis_a"]
            dx, dy = p["axis_d"]
            full = LineString([ax_a, (ax_a[0] + dx, ax_a[1] + dy)])
            if full.length >= _MIN_AXIS_LEN_M:
                return full
        return None
    want_service = (s.role == ROLE_SERVICE_ROAD)
    best = None
    best_len = _MIN_AXIS_LEN_M
    for cl in (getattr(layout, "apt_taxi_centerlines", None) or []):
        if bool(getattr(cl, "is_service", False)) != want_service:
            continue
        ln = (getattr(cl, "chained_line", None)
              or getattr(cl, "line", None))
        if ln is None or ln.is_empty:
            continue
        try:
            inter = ln.intersection(s.polygon)
        except _GEOM_EXC:
            continue
        if inter.is_empty:
            continue
        parts = ([inter] if inter.geom_type == "LineString"
                 else [g for g in getattr(inter, "geoms", ())
                       if g.geom_type == "LineString"])
        for g in parts:
            if g.length > best_len:
                best = g
                best_len = g.length
    return best


def _ring_alts(s, n_open: int) -> Optional[List[float]]:
    """Open-ring altitude list for ``s`` (expanding a flat ``altitude``)."""
    if s.node_altitudes is not None:
        alts = list(s.node_altitudes)
        if len(alts) >= n_open:
            return alts[:n_open]
        return None
    if s.altitude is not None:
        return [float(s.altitude)] * n_open
    return None


def apply_spine_crown(layout, icao: str = "") -> Tuple[int, int]:
    """Drop crown-eligible shapes' edges below their spine and stash the
    spine breaklines on ``layout.crown_spines`` for ``to_osm``.

    Returns ``(n_shapes_crowned, n_spine_ways)``.
    """
    cps = getattr(layout, "canonical_points", None)
    if cps is None:
        return (0, 0)
    tol_deg = _seam_tol_deg(layout)

    # Pass 0: canonical key → owned-by-non-crowned-role?  A key owned by
    # ANY shape outside the crown family freezes that vertex.
    frozen_keys: set = set()
    ring_cache: Dict[int, Tuple[List[Tuple[float, float]], List]] = {}
    def _all_rings(poly):
        """Every ring (exterior + holes) of a Polygon/MultiPolygon."""
        geoms = ([poly] if poly.geom_type == "Polygon"
                 else list(getattr(poly, "geoms", ())))
        for g in geoms:
            if g.geom_type != "Polygon":
                continue
            try:
                yield list(g.exterior.coords)
                for hole in g.interiors:
                    yield list(hole.coords)
            except _GEOM_EXC:
                continue

    for si, s in enumerate(layout.shapes):
        if s.polygon is None or s.polygon.is_empty:
            continue
        cached = False
        if s.polygon.geom_type == "Polygon" and not s.polygon.interiors:
            try:
                closed = list(s.polygon.exterior.coords)
            except _GEOM_EXC:
                closed = []
            if len(closed) >= 4:
                coords = closed[:-1]
                keys = [cps.get_or_add(float(x), float(y))
                        for (x, y) in coords]
                ring_cache[si] = (coords, keys)
                cached = True
                if s.role not in _CROWN_RATES:
                    frozen_keys.update(keys)
        if not cached:
            # MultiPolygon / holed / degenerate shape: never crowned,
            # and its vertices must not MOVE either — a drop leaking in
            # through a shared canonical point mints a step inside a
            # ring nobody smooths or valve-checks (CYXY -10116 +2.9 %).
            for ring in _all_rings(s.polygon):
                for (x, y) in ring:
                    frozen_keys.add(cps.get_or_add(float(x), float(y)))

    # Pass 1a: eligibility (axis + altitudes + half-width) only.
    shape_axis: Dict[int, LineString] = {}
    shape_halfw: Dict[int, float] = {}
    shape_laterals: Dict[int, List[float]] = {}
    eligible: List[int] = []
    for si, s in enumerate(layout.shapes):
        if s.role not in _CROWN_RATES or si not in ring_cache:
            continue
        # Corridor junctions need an axis of their own; other roles too.
        ax = _shape_axis(layout, s)
        if ax is None:
            continue
        coords, keys = ring_cache[si]
        alts = _ring_alts(s, len(coords))
        if alts is None or any(a is None for a in alts):
            continue
        laterals = []
        for (x, y) in coords:
            try:
                laterals.append(ax.distance(Point(x, y)))
            except _GEOM_EXC:
                laterals.append(0.0)
        # Half-width: runway from its persisted profile, else the 75th
        # percentile of ring lateral distances (mitred corners exceed
        # the true half-width; the cap keeps them from over-dropping).
        halfw = None
        if s.role == ROLE_RUNWAY:
            p = (getattr(layout, "_runway_redistributed_profiles", None)
                 or {}).get(s.ref)
            if p:
                halfw = float(p.get("half_width_m", 0.0)) or None
        if halfw is None:
            srt = sorted(laterals)
            halfw = srt[int(0.75 * (len(srt) - 1))] if srt else 0.0
        halfw = min(halfw,
                    _HALFW_CAP_M.get(s.role, _HALFW_CAP_DEFAULT_M))
        if halfw <= 0.1:
            continue
        eligible.append(si)
        shape_axis[si] = ax
        shape_halfw[si] = halfw
        shape_laterals[si] = laterals

    if not eligible:
        return (0, 0)

    # Pass 1b: freeze the rings of every NON-ELIGIBLE shape — including
    # family-role shapes that just lack an axis.  A shared vertex
    # dropped through a crowned neighbour but never smoothed in the
    # axis-less owner's own ring minted a step INSIDE that owner (CYXY
    # junction -10029: 13 cm over 1 m = a 13 % pair that no amount of
    # neighbour-side smoothing could see).
    eligible_set = set(eligible)
    for si, (coords, keys) in ring_cache.items():
        if si not in eligible_set:
            frozen_keys.update(keys)

    # Pass 1c: per-canonical proposed drops (MIN wins across owners).
    proposals: Dict[object, float] = {}
    for si in eligible:
        s = layout.shapes[si]
        coords, keys = ring_cache[si]
        ax = shape_axis[si]
        halfw = shape_halfw[si]
        laterals = shape_laterals[si]
        rate = _CROWN_RATES[s.role]
        L = ax.length
        for k, (x, y) in enumerate(coords):
            key = keys[k]
            if key in frozen_keys:
                continue
            if _is_seam_vertex(layout, x, y, tol_deg):
                frozen_keys.add(key)
                continue
            try:
                station = ax.project(Point(x, y))
            except _GEOM_EXC:
                continue
            taper = 1.0
            if s.role != ROLE_RUNWAY:      # runways crown to the ends
                taper = max(0.0, min(1.0, min(station, L - station)
                                     / _END_TAPER_M))
            drop = max(0.0, rate * min(laterals[k], halfw) * taper)
            # Register even a ZERO drop: the MIN reconcile must let a
            # near-axis owner veto a far-lateral neighbour's big drop
            # at a shared vertex (CYXY -10116: a runway-edge 23 cm drop
            # stood at a junction-mouth node whose own crown was ~0 —
            # a pit inside a ring nobody proposed for).
            prev = proposals.get(key)
            if prev is None or drop < prev:
                proposals[key] = drop

    # Pass 1.5: BUDGET-AWARE Lipschitz projection.  The crown drop
    # stacks on whatever longitudinal grade the ring edge already
    # carries, so smoothing the DROP field alone still minted pairs
    # over cap where the surface was already near it (SPLP: 75 pairs
    # 1.5–2.0 %).  Work in FINAL-value space instead: per ring, start
    # from ``e = a − raw_drop`` (``a`` = pre-crown altitude; frozen
    # vertices keep ``e = a``) and lift ``e`` until every ring-adjacent
    # pair satisfies ``|e_i − e_j| ≤ cap·seg`` — lifting only (a lift
    # can only shrink the drop, never overshoot ``a``).  Shared
    # canonical points reconcile to the SMALLEST drop (largest ``e``)
    # across owners each iteration.
    from .config import ROLE_GRADE_LIMITS
    for _ in range(4):
        changed_any = False
        for si in eligible:
            s = layout.shapes[si]
            coords, keys = ring_cache[si]
            n = len(coords)
            alts = _ring_alts(s, n)
            if alts is None:
                continue
            cap = float(ROLE_GRADE_LIMITS.get(s.role, 0.015) or 0.015)
            e = []
            frozen_mask = []
            for k in range(n):
                key = keys[k]
                fz = key in frozen_keys
                d = 0.0 if fz else proposals.get(key, 0.0)
                e.append(float(alts[k]) - d)
                frozen_mask.append(fz)
            # Constraint edges: ALL vertex pairs within a local window,
            # budgeted at ``min(longitudinal cap, transverse cap) ×
            # straight-line distance`` — a PROVABLE lower bound on every
            # allowance the law can assign a pair (flat cap·d, and the
            # anisotropic √((cL·Δs∥)²+(cT·Δs⊥)²) ≥ min(cL,cT)·d), so a
            # drop field feasible here is feasible under the real law
            # regardless of which rule classifies each pair.  Ring-only
            # and even ring+mesh graphs under-covered (CYXY: a 13 cm
            # crown step across a 1 m neck read at 13 % on a chord pair
            # neither graph contained).  Conservative by design — it can
            # only shrink the crown near already-steep geometry.
            if s.role == ROLE_SERVICE_ROAD:
                cap_pair = min(cap, SERVICE_ROAD_MAX_TRANSVERSE)
            else:
                cap_pair = cap
            _WINDOW_M = 40.0
            edge_list = []
            for ia in range(n):
                xa, ya = coords[ia]
                for ib in range(ia + 1, n):
                    xb, yb = coords[ib]
                    dx = xb - xa
                    if dx > _WINDOW_M or dx < -_WINDOW_M:
                        continue
                    d = math.hypot(dx, yb - ya)
                    if d > _WINDOW_M:
                        continue
                    edge_list.append((ia, ib, cap_pair * max(0.05, d)))
            for _sweep in range(6):
                moved = False
                for (ia, ib, budget) in edge_list:
                    if not frozen_mask[ib] and e[ib] < e[ia] - budget:
                        e[ib] = min(float(alts[ib]), e[ia] - budget)
                        moved = True
                    if not frozen_mask[ia] and e[ia] < e[ib] - budget:
                        e[ia] = min(float(alts[ia]), e[ib] - budget)
                        moved = True
                if not moved:
                    break
            for k in range(n):
                key = keys[k]
                if key in frozen_keys:
                    continue
                new_drop = max(0.0, float(alts[k]) - e[k])
                cur = proposals.get(key)
                if cur is not None and new_drop < cur - 1e-4:
                    proposals[key] = new_drop
                    changed_any = True
        if not changed_any:
            break
    # prune vanished drops
    proposals = {k: v for k, v in proposals.items() if v > 0.005}

    # Pass 1.6: SAFETY VALVE.  The projection above converges through
    # shared-key reconciliation, but cross-owner interactions can leave
    # residual over-cap pairs (CYXY: 14 after smoothing).  Verify each
    # eligible shape's FINAL values against the conservative all-pairs
    # bound; a shape that still violates gets its crown REVOKED (all
    # its ring keys frozen — like an axis-less shape) and the field
    # re-smoothed.  No new violation can ship; the cost is a flat
    # cross-section on the few problem shapes.
    for _valve_round in range(3):
        revoked: List[int] = []
        for si in list(eligible):
            s = layout.shapes[si]
            coords, keys = ring_cache[si]
            n = len(coords)
            alts = _ring_alts(s, n)
            if alts is None:
                continue
            cap = float(ROLE_GRADE_LIMITS.get(s.role, 0.015) or 0.015)
            cap_pair = (min(cap, SERVICE_ROAD_MAX_TRANSVERSE)
                        if s.role == ROLE_SERVICE_ROAD else cap)
            vals = []
            moved_any = False
            for k in range(n):
                d = (0.0 if keys[k] in frozen_keys
                     else proposals.get(keys[k], 0.0))
                if d:
                    moved_any = True
                vals.append(float(alts[k]) - d)
            if not moved_any:
                continue
            bad = False
            for ia in range(n):
                xa, ya = coords[ia]
                base_a = float(alts[ia])
                for ib in range(ia + 1, n):
                    xb, yb = coords[ib]
                    dxy = math.hypot(xb - xa, yb - ya)
                    if dxy > 40.0 or dxy < 0.05:
                        continue
                    # only pairs the CROWN made worse can be charged
                    # to the crown (pre-existing over-cap stays theirs)
                    if (abs(vals[ia] - vals[ib])
                            > cap_pair * dxy + 1e-6
                            and abs(vals[ia] - vals[ib])
                            > abs(base_a - float(alts[ib])) + 1e-6):
                        bad = True
                        break
                if bad:
                    break
            if bad:
                revoked.append(si)
        if not revoked:
            break
        for si in revoked:
            eligible.remove(si)
            frozen_keys.update(ring_cache[si][1])
        # re-smooth the survivors against the newly frozen keys
        for si in eligible:
            s = layout.shapes[si]
            coords, keys = ring_cache[si]
            n = len(coords)
            alts = _ring_alts(s, n)
            if alts is None:
                continue
            cap = float(ROLE_GRADE_LIMITS.get(s.role, 0.015) or 0.015)
            cap_pair = (min(cap, SERVICE_ROAD_MAX_TRANSVERSE)
                        if s.role == ROLE_SERVICE_ROAD else cap)
            e = []
            frozen_mask = []
            for k in range(n):
                fz = keys[k] in frozen_keys
                d = 0.0 if fz else proposals.get(keys[k], 0.0)
                e.append(float(alts[k]) - d)
                frozen_mask.append(fz)
            for _sweep in range(6):
                moved = False
                for ia in range(n):
                    xa, ya = coords[ia]
                    for ib in range(ia + 1, n):
                        xb, yb = coords[ib]
                        dxy = math.hypot(xb - xa, yb - ya)
                        if dxy > 40.0:
                            continue
                        budget = cap_pair * max(0.05, dxy)
                        if (not frozen_mask[ib]
                                and e[ib] < e[ia] - budget):
                            e[ib] = min(float(alts[ib]), e[ia] - budget)
                            moved = True
                        if (not frozen_mask[ia]
                                and e[ia] < e[ib] - budget):
                            e[ia] = min(float(alts[ia]), e[ib] - budget)
                            moved = True
                if not moved:
                    break
            for k in range(n):
                if keys[k] in frozen_keys:
                    continue
                new_drop = max(0.0, float(alts[k]) - e[k])
                cur = proposals.get(keys[k])
                if cur is not None and new_drop < cur - 1e-4:
                    proposals[keys[k]] = new_drop
    proposals = {k: v for k, v in proposals.items() if v > 0.005}

    # Pass 2: apply the agreed drop to every crowned owner + collect the
    # PRE-crown ring samples the spine interpolation needs.
    n_crowned = 0
    spine_ways: List[Tuple[List[Tuple[float, float]], List[float]]] = []
    for si in eligible:
        s = layout.shapes[si]
        coords, keys = ring_cache[si]
        n_open = len(coords)
        alts = _ring_alts(s, n_open)
        if alts is None:
            continue
        pre_crown = list(alts)

        # Spine breakline: sample the axis clipped ~1 m inside the ring,
        # altitudes = inverse-distance interpolation of the PRE-crown
        # ring values (pre-crown the section is flat at spine level, so
        # this IS the spine profile).
        ax = shape_axis[si]
        try:
            inner = s.polygon.buffer(-_SPINE_EDGE_CLEAR_M)
            if inner.is_empty:
                inner = s.polygon
            clipped = ax.intersection(inner)
            if clipped.is_empty:
                clipped = ax.intersection(s.polygon)
        except _GEOM_EXC:
            clipped = None
        segs = []
        if clipped is not None and not clipped.is_empty:
            if clipped.geom_type == "LineString":
                segs = [clipped]
            else:
                segs = [g for g in getattr(clipped, "geoms", ())
                        if g.geom_type == "LineString" and g.length >= 3.0]
        # STATION → ALTITUDE profile from the PRE-crown ring: every ring
        # node projects to its axis station; nodes at (nearly) the same
        # station — the two edges of one cross-section — average.  The
        # spine value at any station is the linear interpolation of that
        # profile.  Handles both sparse 4-corner runway rects (corners
        # at 0 and L → the plane) and dense corridors, and cannot mix
        # parallel branches of a curving corridor (projection follows
        # the curving axis).
        prof: List[Tuple[float, float]] = []
        for k, (x, y) in enumerate(coords):
            try:
                prof.append((ax.project(Point(x, y)),
                             float(pre_crown[k])))
            except _GEOM_EXC:
                continue
        prof.sort()
        merged: List[Tuple[float, float]] = []
        for st, a in prof:
            if merged and st - merged[-1][0] <= 3.0:
                pst, pa, pn = merged[-1][0], merged[-1][1], merged[-1][2]
                merged[-1] = (pst, (pa * pn + a) / (pn + 1), pn + 1)
            else:
                merged.append((st, a, 1))
        prof2 = [(st, a) for st, a, _n in merged]

        def _alt_at(st: float) -> Optional[float]:
            if not prof2:
                return None
            if st <= prof2[0][0]:
                return prof2[0][1]
            if st >= prof2[-1][0]:
                return prof2[-1][1]
            for j in range(1, len(prof2)):
                if st <= prof2[j][0]:
                    s0, a0 = prof2[j - 1]
                    s1, a1 = prof2[j]
                    f = (st - s0) / max(1e-6, s1 - s0)
                    return a0 + f * (a1 - a0)
            return prof2[-1][1]

        try:
            ring_line = s.polygon.exterior
        except _GEOM_EXC:
            ring_line = None
        for seg in segs:
            n_pts = max(2, int(seg.length / _SPINE_SAMPLE_STEP_M) + 1)
            way_ll: List[Tuple[float, float]] = []
            way_alt: List[float] = []
            for j in range(n_pts):
                p = seg.interpolate(j * seg.length / (n_pts - 1))
                # Never place a spine node on/near the ring: a
                # coincident vertex at the interpolated (un-crowned)
                # level fights the ring vertex's crowned altitude in
                # the mesh (the narrow-shape polygon fallback above
                # can put the clipped axis on the boundary).
                if ring_line is not None:
                    try:
                        if ring_line.distance(p) < 0.9:
                            continue
                    except _GEOM_EXC:
                        pass
                try:
                    st = ax.project(p)
                except _GEOM_EXC:
                    continue
                a = _alt_at(st)
                if a is None:
                    continue
                way_ll.append(layout.m_to_ll(p.x, p.y))
                way_alt.append(a)
            if len(way_ll) >= 2:
                spine_ways.append((way_ll, way_alt))

        # Edge drop.
        changed = False
        new_alts = list(pre_crown)
        for k in range(n_open):
            drop = proposals.get(keys[k])
            if drop:
                new_alts[k] = pre_crown[k] - drop
                changed = True
        if changed:
            s.node_altitudes = new_alts + [new_alts[0]]
            s.altitude = None
            s.altitude_high = None
            s.altitude_low = None
            n_crowned += 1

    if spine_ways:
        existing = getattr(layout, "crown_spines", None) or []
        layout.crown_spines = existing + spine_ways
    return (n_crowned, len(spine_ways))

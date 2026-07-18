"""Shared depressed-public-road exclusion corridor (road-lane exclusion).

Neutral module.  ``adjacent_ground`` imports ``clearance`` (a one-way
dependency), so a helper both need cannot live in either without a cycle;
it lives here and both import it.

WHY IT EXISTS
-------------
A public road that dips into a cut / bore to pass under (or between)
airport pavement must not be BURIED by the terrain-grading features the
patch lays down around that pavement — the runway-end down-slope skirt
and the adjacent-ground bands both drape terrain, and where they run over
a depressed road they laid their floor ACROSS the trench (measured KBNA
Donelson Pike: skirt pieces ~3-7 m above the road, 201 m² of skirt over
the buried carriageway).  The airside-precedence ruling (2026-07-10) makes
the skirt override airside pavement / groundside / RESA cuts, but it does
NOT extend to a depressed PUBLIC-ROAD corridor: the skirt (and the bands)
CLEAR that corridor, exactly as they already clear surface roads.

PHASE-INDEPENDENT DESIGN (owner ruling 2026-07-16, round 8)
-----------------------------------------------------------
The protection derives from the tile's MAPPED BIG ROADS, not from the
emitted tunnel-ramp / bridge-approach pieces.  The earlier design built
the corridor only from those EMITTED pieces, which fail two ways:

  * ORDER: the runway-end skirt is emitted PRE-SOLVE (one-solve terrain
    absorption, stage B1) but the ramp / approach pieces are emitted
    POST-solve — so the skirt clip ran against an EMPTY corridor and the
    skirt buried the road anyway.
  * REACH: a fixed ~60 m extension past the emitted pieces left the long
    depressed stretch BETWEEN two crossings (~235 m at KBNA) unprotected.

The corridor is therefore rebuilt each call from the mapped roads and is
naturally phase-aware (pre-solve it is anchored on the mapped ``tunnel=yes``
bores; post-solve the emitted approach OBB lanes add to it).

CONSTRUCTION
------------
1. Load the tile's mapped big-road centrelines (read-only, via
   ``osm_load._load_osm_big_roads`` — the same cache the bridge / tunnel
   emitters read).  ``bridge=yes`` elevated ways are DROPPED (they fly over
   on their own structure); ``tunnel=yes`` bores are KEPT (the buried body
   of the road).  Each way is buffered to its carriageway half-width
   (``width=`` → ``lanes=`` → per-highway-type table, via the read-only
   ``bridges._carriageway_width_from_tags``) plus a shoulder margin.

2. SCOPE to relevance: only ways within ``_RELEVANCE_BUFFER_M`` (~120 m) of
   the airport's built geometry (airside pavement + every layout shape +
   the emitted approach OBB lanes) are eligible — a road far from the
   airport is never buffered.

3. SEED + GROW the DEPRESSED corridor: the seeds are the mapped
   ``tunnel=yes`` bores and the emitted approach OBB lanes (both
   unambiguously depressed).  From the seeds the corridor GROWS along the
   connected road chain (a way joins when it comes within
   ``_CONNECT_TOL_M`` of the corridor so far), bounded to
   ``_EXTENSION_REACH_M`` beyond the seeds — so the buried / descending
   continuation is followed through the between-crossings gap without ever
   annexing an unrelated tile road.  An airport with NO tunnel bore and NO
   emitted approach has no seed, so the union is ``None`` and both
   consumers are byte-inert (a level crossing that stays at grade never
   seeds a corridor).

4. VERTICAL SANITY (only clip where the road actually conflicts): a grown
   SURFACE segment is kept only where the road's local grade is DEPRESSED
   below the local terrain by more than ``_VERTICAL_SANITY_M`` (1.5 m) — a
   band lawfully crossing at road grade (flat terrain far from the trench)
   is not needlessly punched.  The road's local grade comes from the
   emitted approach chain profile where available (the descending ramp /
   approach piece altitudes), else the DEM sample; bands and skirts sit at
   roughly the local terrain grade, so comparing the road grade to the DEM
   is the piece-vs-road test the ruling calls for.  When neither source is
   available (pre-solve: no approach pieces yet) a grown segment is KEPT —
   it is anchored to a real ``tunnel=yes`` bore and is physically depressed.
   The tunnel-bore and OBB-lane SEEDS are never trimmed.

Consumers:
  * ``adjacent_ground`` DIFFERENCES its band pieces against this union and
    drops any march station whose seed/probe falls inside it.
  * ``clearance`` clips runway-end skirts against it (now effective on the
    PRE-SOLVE path — the reason for the rework).
"""
from __future__ import annotations

import math

from shapely.geometry import LineString
from shapely.errors import GEOSException, TopologicalError
from shapely.ops import unary_union

from .geom_safe import min_rotated_rect
from .layout import ROLE_TUNNEL_RAMP

__all__ = ["road_lane_exclusion_union"]

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

# Half-lane clearance added beyond the carriageway (bands/skirts stand this
# far off the lane even where the buffer's round caps extend past a step end).
_LANE_MARGIN_M = 2.0
# Only roads within this distance of the airport's built geometry are
# eligible — never buffer a road out in open country (owner ruling: scope
# to airside pavement / existing graded features).
_RELEVANCE_BUFFER_M = 120.0
# Grow the depressed corridor at most this far beyond the seeds (the mapped
# tunnel bores / emitted approach lanes); covers the between-crossings gap
# (KBNA Donelson ~235 m) while staying bounded.
_EXTENSION_REACH_M = 300.0
# A road way joins the corridor when it comes at least this close to the
# corridor built so far (the buried bore abuts the ramps; connected chain).
_CONNECT_TOL_M = 6.0
# Vertical-sanity threshold: a grown surface segment is a road CONFLICT
# only where the road grade sits more than this far below the local terrain
# (a band/skirt draped at terrain grade would otherwise bury the road).
_VERTICAL_SANITY_M = 1.5
# Per-lane width for a mapped road's ``lanes`` tag, and the fallback
# carriageway width when neither the tag nor the highway type resolves one.
_LANE_WIDTH_M = 3.5
_DEFAULT_CARRIAGEWAY_WIDTH_M = 7.0
# Big-road highway classes eligible to carry a buried corridor (mirrors the
# legacy underpass road filter).
_BIG_ROAD_HIGHWAY_TYPES = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link", "residential",
    "service",
})


def _obb_lane(poly):
    """``(centreline LineString, half_width)`` of a ramp/approach piece's
    oriented bounding box, or ``None``.  The half-width is the OBB short
    side / 2 plus ``_LANE_MARGIN_M``."""
    try:
        mrr = min_rotated_rect(poly)
        corners = list(mrr.exterior.coords)[:4]
        if len(corners) < 4:
            return None
        edges = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
        edges.sort(key=lambda e: math.hypot(e[1][0] - e[0][0],
                                            e[1][1] - e[0][1]))
        width = math.hypot(edges[0][1][0] - edges[0][0][0],
                           edges[0][1][1] - edges[0][0][1])
        # The two LONG edges; pair endpoints crosswise to recover the two
        # SHORT-edge midpoints — the endpoints of the travel centreline.
        long_a, long_b = edges[2], edges[3]
        mid_0 = ((long_a[0][0] + long_b[1][0]) / 2.0,
                 (long_a[0][1] + long_b[1][1]) / 2.0)
        mid_1 = ((long_a[1][0] + long_b[0][0]) / 2.0,
                 (long_a[1][1] + long_b[0][1]) / 2.0)
        return LineString([mid_0, mid_1]), width / 2.0 + _LANE_MARGIN_M
    except _GEOM_EXC:
        return None


def _ramp_pieces(layout):
    """``[(polygon, min_alt_or_None)]`` for every ``tunnel_ramp`` sloped
    rect and ``object_bridge_approach`` step polygon on the layout — the
    emitted descending-road pieces.  ``min_alt`` is the piece's lowest
    vertex altitude (the road surface at that step), used as the road's
    local grade for the vertical-sanity trim.  Empty pre-solve (the pieces
    are emitted after the solve)."""
    out = []
    for s in layout.shapes:
        if not (s.role == ROLE_TUNNEL_RAMP
                or getattr(s, "ref", None) == "object_bridge_approach"):
            continue
        poly = getattr(s, "polygon", None)
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            continue
        alt = None
        for attr in ("altitude_low", "altitude"):
            v = getattr(s, attr, None)
            if v is not None:
                alt = float(v)
                break
        out.append((poly, alt))
    return out


def _carriageway_half_width(highway_type, tags):
    """Carriageway half-width + shoulder margin for a mapped road way, via
    the read-only ``bridges._carriageway_width_from_tags`` (``width=`` →
    ``lanes=`` → per-highway-type table).  Falls back to a local lane-count
    / default computation if that helper cannot be imported."""
    try:
        from .bridges import _carriageway_width_from_tags
        width = _carriageway_width_from_tags(
            highway_type, tags, _DEFAULT_CARRIAGEWAY_WIDTH_M)
    except Exception:
        width = _DEFAULT_CARRIAGEWAY_WIDTH_M
        lanes = (tags or {}).get("lanes")
        if lanes:
            try:
                width = max(4.0, float(lanes) * _LANE_WIDTH_M)
            except (TypeError, ValueError):
                pass
    return width / 2.0 + _LANE_MARGIN_M


def _load_mapped_road_ways(layout):
    """Mapped big-road ways as ``[{line, half, is_tunnel, nodes}]`` in local
    metres, INCLUDING ``tunnel=yes`` bores (the buried continuation of the
    road) but EXCLUDING elevated ``bridge`` ways (they fly over on their own
    structure).  ``nodes`` is the way's node-id tuple (endpoints drive the
    connectivity growth).  Empty when the tile has no big-road cache
    (headless tests, roadless airports) or no layout anchor."""
    if getattr(layout, "anchor", None) is None:
        return []
    try:
        from .osm_load import _load_osm_big_roads
        nodes_raw, ways_raw = _load_osm_big_roads(
            layout.anchor[0], layout.anchor[1])
    except Exception:
        return []
    if not ways_raw:
        return []
    to_m = layout.ll_to_m
    nodes_m: dict = {}
    for nid, latlon in nodes_raw.items():
        try:
            lat, lon = latlon
            nodes_m[nid] = to_m(lat, lon)
        except (TypeError, ValueError):
            continue
    ways: list = []
    for _way_id, node_refs, tags in ways_raw:
        if tags.get("highway") not in _BIG_ROAD_HIGHWAY_TYPES:
            continue
        if tags.get("bridge") and tags.get("bridge") != "no":
            continue
        pts = [nodes_m[n] for n in node_refs if n in nodes_m]
        if len(pts) < 2:
            continue
        try:
            line = LineString(pts)
        except _GEOM_EXC:
            continue
        if line.is_empty or line.length < 5.0:
            continue
        is_tunnel = bool(tags.get("tunnel")) and tags.get("tunnel") != "no"
        ways.append({
            "line": line,
            "half": _carriageway_half_width(tags.get("highway"), tags),
            "is_tunnel": is_tunnel,
            "nodes": tuple(n for n in node_refs if n in nodes_m),
        })
    return ways


def _relevance_geometry(layout, obb_polys):
    """The airport's built geometry (every layout shape polygon + the
    emitted approach OBB lanes) buffered by ``_RELEVANCE_BUFFER_M``, or
    ``None``.  A mapped road is eligible for the corridor only where it lies
    inside this region."""
    parts = list(obb_polys)
    for s in getattr(layout, "shapes", []):
        poly = getattr(s, "polygon", None)
        if poly is not None and not poly.is_empty:
            parts.append(poly)
    if not parts:
        return None
    try:
        built = unary_union(parts)
        if built.is_empty:
            return None
        return built.buffer(_RELEVANCE_BUFFER_M)
    except _GEOM_EXC:
        return None


def _road_local_grade(line, ramp_alts):
    """The road's local grade under ``line`` from the emitted approach chain
    profile — the lowest ``min_alt`` of a ramp/approach piece within
    ``_EXTENSION_REACH_M`` of the way (the descending road surface), or
    ``None`` when no approach piece is near (pre-solve, or a bore-only
    stretch)."""
    best = None
    for poly, alt in ramp_alts:
        if alt is None:
            continue
        try:
            if line.distance(poly) > _EXTENSION_REACH_M:
                continue
        except _GEOM_EXC:
            continue
        if best is None or alt < best:
            best = alt
    return best


def road_lane_exclusion_union(layout, sample_dem=None,
                              extra_seed_geometries=None):
    """Union (or ``None``) of the DEPRESSED-public-road exclusion corridor.

    Built from the tile's mapped big roads (phase-independent), scoped to
    the airport's built geometry, seeded on the mapped ``tunnel=yes`` bores
    and emitted approach OBB lanes, grown along the connected road chain,
    and vertically sanity-trimmed.  See the module docstring.

    ``sample_dem`` (``(x, y) -> alt|None``, optional): enables the
    vertical-sanity trim of grown surface segments.  Omitted (pre-solve /
    callers without a DEM) keeps every bore-anchored grown segment.

    ``extra_seed_geometries`` (optional list of polygons): additional
    depression anchors for the SEED + GROW step — the crossing influence
    zone (``crossing_terrain``) passes the recognized crossings' deck
    boxes / portal footprints here, so the road passing under a recognized
    deck is followed even where the mapper omitted ``tunnel=yes``.  They
    anchor growth and reach only; they are NEVER part of the returned
    corridor (a caller that wants them in its keep-out unions them
    itself)."""
    # ── OBB lanes of the emitted ramp / approach pieces (post-solve). ──
    ramp_alts = _ramp_pieces(layout)
    obb_polys: list = []
    for poly, _alt in ramp_alts:
        lane = _obb_lane(poly)
        if lane is None:
            continue
        line, half = lane
        try:
            buffered = line.buffer(half)
        except _GEOM_EXC:
            continue
        if buffered is not None and not buffered.is_empty:
            obb_polys.append(buffered)

    extra_seeds = [g for g in (extra_seed_geometries or [])
                   if g is not None and not g.is_empty]

    ways = _load_mapped_road_ways(layout)
    if not obb_polys and not ways:
        return None

    # ── Scope the mapped ways to the airport's built geometry. ──
    relevance = _relevance_geometry(layout, obb_polys)
    if ways and relevance is not None and not relevance.is_empty:
        scoped = []
        for w in ways:
            try:
                if w["line"].intersects(relevance):
                    scoped.append(w)
            except _GEOM_EXC:
                continue
        ways = scoped

    # ── Seeds: the mapped tunnel bores + the emitted approach OBB lanes
    # (+ the caller's extra anchors — growth/reach only, never output). ──
    tunnel_ways = [w for w in ways if w["is_tunnel"]]
    seed_geoms = list(obb_polys) + extra_seeds
    for w in tunnel_ways:
        try:
            b = w["line"].buffer(w["half"])
            if not b.is_empty:
                seed_geoms.append(b)
        except _GEOM_EXC:
            continue
    if not seed_geoms:
        # No depression anchor anywhere near the airport — a level crossing
        # that stays at grade never seeds a corridor.
        return None
    try:
        seed_union = unary_union(seed_geoms)
    except _GEOM_EXC:
        return None
    if seed_union is None or seed_union.is_empty:
        return None
    try:
        reach = seed_union.buffer(_EXTENSION_REACH_M)
    except _GEOM_EXC:
        reach = None

    # ── Grow the corridor along the connected road chain, bounded to the
    # reach, from the seeds. ──
    surface_ways = [w for w in ways if not w["is_tunnel"]]
    grown = seed_union
    included: list = []
    if reach is not None and not reach.is_empty:
        remaining = list(surface_ways)
        changed = True
        while changed:
            changed = False
            still: list = []
            for w in remaining:
                line = w["line"]
                try:
                    if not line.intersects(reach):
                        continue  # never re-considered (out of reach)
                    if line.distance(grown) <= _CONNECT_TOL_M:
                        piece = line.buffer(w["half"]).intersection(reach)
                        if piece is not None and not piece.is_empty:
                            included.append((w, piece))
                            grown = unary_union([grown, piece])
                        changed = True
                        continue
                except _GEOM_EXC:
                    continue
                still.append(w)
            remaining = still

    # ── Vertical sanity: keep a grown SURFACE segment only where the road
    # is depressed below the local terrain (bands/skirts drape at terrain
    # grade).  The tunnel-bore and OBB seeds are never trimmed. ──
    corridor_parts = list(obb_polys)
    for w in tunnel_ways:
        try:
            b = w["line"].buffer(w["half"])
            if not b.is_empty:
                corridor_parts.append(b)
        except _GEOM_EXC:
            continue
    for w, piece in included:
        keep = True
        if sample_dem is not None:
            grade = _road_local_grade(w["line"], ramp_alts)
            if grade is not None:
                try:
                    mid = w["line"].interpolate(0.5, normalized=True)
                    local = sample_dem(mid.x, mid.y)
                except _GEOM_EXC:
                    local = None
                # Road grade known AND local terrain known AND the road is
                # NOT depressed below terrain by > 1.5 m -> at grade -> drop.
                if local is not None and (local - grade) <= _VERTICAL_SANITY_M:
                    keep = False
        if keep:
            corridor_parts.append(piece)

    if not corridor_parts:
        return None
    try:
        corridor = unary_union(corridor_parts)
    except _GEOM_EXC:
        return None
    if corridor is None or corridor.is_empty:
        return None
    # Bound the final corridor to the airport's built geometry.
    if relevance is not None and not relevance.is_empty:
        try:
            corridor = corridor.intersection(relevance)
        except _GEOM_EXC:
            pass
    return None if corridor.is_empty else corridor

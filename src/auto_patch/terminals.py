"""OSM terminal pad extraction + groundside-zone derivation.

Builds terminal-building footprints from OSM ``aeroway=terminal``
ways and ``building=terminal`` polygons, plus the matching
groundside-pavement zone (curbside / drop-off / parking) inferred
from the airport's road network.

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _terminal_pad_from_building
    _build_osm_aeroway_footprint
    _terminal_groundside_zone
    _extract_osm_terminals
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import JOIN_STYLE as _JOIN_STYLE
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from .config import (
    BUILDING_CLOSE_MIN_PIECE_M2,
    BUILDING_OUTLINE_FILL_R,
    BUILDING_OUTLINE_FILL_GATE_M,
    DSF_CLUSTER_SIMPLIFY_TOL_M,
    DSF_FACADE_MERGE_GAP_M,
    DSF_MIN_BUILDING_AREA_M2,
    HANGAR_PADS,
)

_MITRE_JOIN = _JOIN_STYLE.mitre

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = [
    "_build_osm_aeroway_footprint",
    "_extract_osm_terminals",
    "_terminal_groundside_zone",
    "_terminal_pad_from_building",
]


def _terminal_pad_from_building(
    building: Polygon,
    pav_polys: List[Polygon],
) -> Optional[Polygon]:
    """Return the OSM building outline as the terminal pad.

    Per user 2026-04-29: the terminal shape must sit right at the
    building edge — no padding, no expansion to the containing
    apt.dat polygon, no fallback buffer.  The earlier behaviour
    (expand to smallest containing apt.dat polygon, or buffer 20 m
    if none) pushed the terminal border out beyond the building
    footprint.

    ``pav_polys`` is unused but kept in the signature so callers
    don't need to change.
    """
    del pav_polys  # intentionally unused
    if building.is_empty:
        return None
    return building


def _build_osm_aeroway_footprint(
    nodes: Dict[str, Tuple[float, float]],
    ways: List[Tuple[str, List[str], Dict[str, str]]],
    to_m,
    taxi_half_width_m: float = 15.0,
) -> Optional[Polygon]:
    """Return the OSM-known pavement footprint as a Polygon /
    MultiPolygon in meter coordinates.

    Sources:
      * ``aeroway=apron`` / ``stand`` / ``parking_position`` /
        ``hangar_apron`` closed ways → polygons
      * ``aeroway=taxiway`` / ``taxi_lane`` / ``runway`` open ways
        → linestrings buffered by ``taxi_half_width_m`` (covers
        the typical taxi corridor when no width tag is present)
      * If the way IS closed (apron-style polygon tagged taxiway),
        treat as a polygon — covers airports where mappers drew
        taxiway extents instead of centerlines.

    Used by the DSF-loading gate to decide whether apt.dat
    already covers the airport's user-mapped pavement.  When
    apt.dat covers ≥ 85 % of this footprint, DSF additions are
    refused.  When apt.dat has gaps, DSF is admitted only inside
    the gap.
    """
    AREA_AEROWAYS = {
        "apron", "stand", "parking_position",
        "hangar_apron",
    }
    LINEAR_AEROWAYS = {
        "taxiway", "taxi_lane", "runway",
    }
    pieces: List[Polygon] = []
    for _wid, nrefs, tags in ways:
        ay = tags.get("aeroway", "")
        if ay not in AREA_AEROWAYS and ay not in LINEAR_AEROWAYS:
            continue
        pts: List[Tuple[float, float]] = []
        for n in nrefs:
            if n in nodes:
                lat, lon = nodes[n]
                pts.append(to_m(lon, lat))
        if len(pts) < 2:
            continue
        is_closed = (
            len(pts) >= 3
            and abs(pts[0][0] - pts[-1][0]) < 0.5
            and abs(pts[0][1] - pts[-1][1]) < 0.5)
        try:
            if ay in AREA_AEROWAYS or is_closed:
                if len(pts) < 3:
                    continue
                p = Polygon(pts).buffer(0)
                if (p.geom_type == "Polygon"
                        and not p.is_empty
                        and p.area >= 1.0):
                    pieces.append(p)
                elif p.geom_type == "MultiPolygon":
                    for g in p.geoms:
                        if (g.geom_type == "Polygon"
                                and not g.is_empty
                                and g.area >= 1.0):
                            pieces.append(g)
            else:
                ls = LineString(pts)
                if ls.is_empty:
                    continue
                buf = ls.buffer(taxi_half_width_m)
                if (buf.geom_type == "Polygon"
                        and not buf.is_empty):
                    pieces.append(buf)
                elif buf.geom_type == "MultiPolygon":
                    for g in buf.geoms:
                        if (g.geom_type == "Polygon"
                                and not g.is_empty):
                            pieces.append(g)
        except _GEOM_EXC:
            continue
    if not pieces:
        return None
    try:
        merged = unary_union(pieces)
        if merged.is_empty:
            return None
        return merged
    except _GEOM_EXC:
        return None


def _terminal_groundside_zone(
    buildings: List[Polygon],
    nodes: Dict[str, Tuple[float, float]],
    ways: List[Tuple[str, List[str], Dict[str, str]]],
    to_m,
    edge_classify_radius_m: float = 30.0,
    groundside_extent_m: float = 100.0,
    apt_pavement_seeds: Optional[List[Polygon]] = None,
    apt_pavement_polys: Optional[List[Polygon]] = None,
    relations: Optional[List[Tuple[str, List[str], Dict[str, str]]]] = None,
) -> Optional[Polygon]:
    """Identify pavement strips on the GROUNDSIDE of terminal
    buildings — the road/curbside frontage where access roads,
    drop-off lanes, and parking sit on pavement at a different
    elevation than the airside apron.  Returns a Polygon /
    MultiPolygon to subtract from ``pav_union`` so groundside
    pavement does NOT become an apron junction grade-clamped to
    the terminal altitude.

    Per user 2026-04-29: combines two airside / groundside
    indicators on each building edge:

      1. APRON ADJACENCY (approach 1).  If the apt.dat polygon
         abutting the edge is connected (touching, transitive)
         to a runway-bearing polygon — i.e. the pavement chain
         from this edge reaches the runway — it's airside.
         Implemented by passing ``apt_pavement_seeds`` and
         testing whether the probe area intersects the
         airside-reachable subset.
      2. OSM TAGS (approach 3).  ``aeroway`` features
         (apron / taxiway / taxi_lane / stand / runway / gate)
         within ``edge_classify_radius_m`` of an outward-edge
         probe → AIRSIDE.  ``highway`` road-class features
         (anything but footway / path / pedestrian / steps)
         within the same probe → GROUNDSIDE.

    An edge is GROUNDSIDE only when at least one groundside
    indicator fires AND no airside indicator does.  An edge with
    NEITHER indicator (most common at airports with sparse OSM
    coverage) defaults to airside (no subtraction) — the safer
    choice when the data can't tell us.

    For each groundside edge a perpendicular outward rectangle
    (depth ``groundside_extent_m``, width = edge length) is added
    to the subtraction zone.  The result is the union of all such
    rectangles; subtracting it from ``pav_union`` keeps the
    airside apron intact while removing the curbside / drop-off
    pavement that should not grade-clamp to the building.
    """
    if not buildings:
        return None
    # NB: the real OSM tag for gate lead-in lanes is ``taxilane`` (one
    # word) — ``taxi_lane`` never occurs in OSM but is kept for safety.
    # KCLT has 159 taxilane + 124 jet_bridge ways at the concourses;
    # missing both left every gate-facing edge UNKNOWN (2026-07-07).
    AIRSIDE_AEROWAY = {
        "apron", "taxiway", "taxilane", "taxi_lane", "stand",
        "runway", "gate", "parking_position", "jet_bridge",
    }
    # Highway road classes — exclude pedestrian-class tags
    # (footway / path / steps / pedestrian / corridor) which
    # commonly trace airside pedestrian routes ON the apron.
    GROUNDSIDE_HIGHWAY = {
        "primary", "secondary", "tertiary",
        "residential", "unclassified", "service",
        "motorway", "trunk", "primary_link",
        "secondary_link", "tertiary_link",
        "motorway_link", "trunk_link", "living_street",
        "raceway", "road",
    }
    # Build airside / groundside OSM geometry catalogs (meter
    # coords).  Polygons (closed ways) become Polygon; open ways
    # become LineString.
    airside_geoms: List = []
    groundside_geoms: List = []
    for _wid, nrefs, tags in ways:
        ay = tags.get("aeroway", "")
        hw = tags.get("highway", "")
        is_airside = ay in AIRSIDE_AEROWAY
        is_groundside = hw in GROUNDSIDE_HIGHWAY
        if not is_airside and not is_groundside:
            continue
        pts: List[Tuple[float, float]] = []
        for n in nrefs:
            if n in nodes:
                lat, lon = nodes[n]
                pts.append(to_m(lon, lat))
        if len(pts) < 2:
            continue
        try:
            if (len(pts) >= 3
                    and abs(pts[0][0] - pts[-1][0]) < 0.5
                    and abs(pts[0][1] - pts[-1][1]) < 0.5):
                g = Polygon(pts).buffer(0)
            else:
                g = LineString(pts)
            if g.is_empty:
                continue
        except _GEOM_EXC:
            continue
        if is_airside:
            airside_geoms.append(g)
        else:
            groundside_geoms.append(g)
    # Aeroway multipolygon RELATIONS (KCLT, 2026-07-07): big terminal
    # ramps are commonly mapped as multipolygon relations whose member
    # ways carry NO tags of their own, so the ways-only catalog above
    # is blind to them — every ramp-facing edge classifies UNKNOWN and
    # the any-airside promotion below subtracts 100 m groundside
    # rectangles straight across the ramp (KCLT: 482 k m² including
    # the whole Concourse E apron, demoted to DEM groundside).
    # Reconstruct each matching relation's rings the same way
    # ``_extract_osm_terminals`` does: closed members are rings
    # already; open members are boundary segments that polygonize.
    if relations:
        _way_by_id = {wid: nrefs for wid, nrefs, _wt in ways}
        for _rid, _member_wids, _rtags in relations:
            if _rtags.get("aeroway") not in AIRSIDE_AEROWAY:
                continue
            _seglines: List[LineString] = []
            _ring_polys: List[Polygon] = []
            for _wid in _member_wids:
                _nrefs = _way_by_id.get(_wid)
                if not _nrefs:
                    continue
                _pts = [to_m(nodes[n][1], nodes[n][0])
                        for n in _nrefs if n in nodes]
                if len(_pts) < 2:
                    continue
                if len(_pts) >= 4 and _nrefs[0] == _nrefs[-1]:
                    try:
                        _p = Polygon(_pts).buffer(0)
                    except _GEOM_EXC:
                        continue
                    if not _p.is_empty:
                        _ring_polys.append(_p)
                else:
                    _seglines.append(LineString(_pts))
            if _seglines:
                try:
                    _ring_polys.extend(
                        g for g in polygonize(unary_union(_seglines))
                        if not g.is_empty)
                except _GEOM_EXC:
                    pass
            airside_geoms.extend(_ring_polys)
    # Approach 1: build the airside-reachable subset of apt.dat
    # pavement.  Two polygons are "connected" if their boundaries
    # are within ``TOUCH_TOL_M`` of each other.  Seeds are
    # polygons that contain or touch a runway centerline (passed
    # in as ``apt_pavement_seeds``).  BFS from seeds through
    # transitive touches; the reached set is the AIRSIDE-REACHABLE
    # portion of apt.dat pavement.
    #
    # Per user 2026-04-30 (CYXY -10123 NW phantom groundside):
    # When ``apt_pavement_polys`` is provided (full row-110 list),
    # we BFS through it.  Without that list, fall back to using
    # the seeds alone (legacy behaviour).  At CYXY, the apron
    # extends NW past the terminal as part of one giant
    # "Apron 1 and E" polygon that touches all three runways —
    # the BFS reaches that whole polygon, so the NW-edge probe
    # sees airside-reachable pavement and classifies AIRSIDE.
    airside_apt_polys: List[Polygon] = []
    if apt_pavement_seeds:
        TOUCH_TOL_M = 1.0
        if apt_pavement_polys:
            # BFS over apt.dat pavement, seeded by polys that
            # touch any runway / aeroway seed within TOUCH_TOL_M.
            try:
                from shapely.strtree import STRtree as _STRtree
                pav_tree = _STRtree(apt_pavement_polys)
            except _GEOM_EXC:
                pav_tree = None
            seed_buf_polys = [
                s.buffer(TOUCH_TOL_M)
                for s in apt_pavement_seeds
                if s is not None and not s.is_empty]
            airside_idxs: set = set()
            queue: List[int] = []
            # Initial seeds: any apt.dat poly intersecting any
            # buffered seed geometry (i.e. within TOUCH_TOL_M).
            for sb in seed_buf_polys:
                if pav_tree is not None:
                    cands = pav_tree.query(sb)
                else:
                    cands = range(len(apt_pavement_polys))
                for hit in cands:
                    pi = (int(hit) if hasattr(hit, "__int__")
                          else hit)
                    if not isinstance(pi, int):
                        continue
                    if pi in airside_idxs:
                        continue
                    cand = apt_pavement_polys[pi]
                    if cand is None or cand.is_empty:
                        continue
                    try:
                        if cand.intersects(sb):
                            airside_idxs.add(pi)
                            queue.append(pi)
                    except _GEOM_EXC:
                        continue
            # BFS: a poly is airside-reachable if its boundary is
            # within TOUCH_TOL_M of an already-airside poly's
            # boundary.
            while queue:
                pi = queue.pop()
                src = apt_pavement_polys[pi]
                src_buf = src.buffer(TOUCH_TOL_M)
                if pav_tree is not None:
                    cands = pav_tree.query(src_buf)
                else:
                    cands = range(len(apt_pavement_polys))
                for hit in cands:
                    qi = (int(hit) if hasattr(hit, "__int__")
                          else hit)
                    if not isinstance(qi, int):
                        continue
                    if qi in airside_idxs:
                        continue
                    cand = apt_pavement_polys[qi]
                    if cand is None or cand.is_empty:
                        continue
                    try:
                        if cand.intersects(src_buf):
                            airside_idxs.add(qi)
                            queue.append(qi)
                    except _GEOM_EXC:
                        continue
            # Use EXTERIOR rings only (drop interior holes).  Per
            # user 2026-04-30 (CYXY -10123): apt.dat row-110
            # apron polygons commonly have interior rings around
            # terminals / non-pavement zones.  An airside-side
            # building edge whose probe lands INSIDE such a hole
            # would otherwise miss the airside polygon entirely
            # and fall back to UNKNOWN/groundside.  Treating the
            # hole as part of the airside-reachable region (since
            # by definition it is SURROUNDED by airside pavement)
            # restores correct classification.
            for pi in airside_idxs:
                src = apt_pavement_polys[pi]
                if src is None or src.is_empty:
                    continue
                try:
                    ext_only = Polygon(list(src.exterior.coords))
                    if not ext_only.is_valid:
                        ext_only = ext_only.buffer(0)
                    if (ext_only.geom_type == "Polygon"
                            and not ext_only.is_empty):
                        airside_apt_polys.append(ext_only)
                except _GEOM_EXC:
                    airside_apt_polys.append(src)
            # Always include the seeds themselves so the probe
            # also fires when the building edge faces directly
            # onto a runway.
            airside_apt_polys.extend(apt_pavement_seeds)
        else:
            # Legacy fallback: use seeds directly (no BFS).
            airside_apt_polys = list(apt_pavement_seeds)
    if (not airside_geoms
            and not groundside_geoms
            and not airside_apt_polys):
        # No data to classify with — bail out, conservative.
        return None
    # STRtree indexes for fast spatial query.
    try:
        from shapely.strtree import STRtree
    except _GEOM_EXC:
        STRtree = None
    air_tree = (STRtree(airside_geoms)
                if STRtree and airside_geoms else None)
    grd_tree = (STRtree(groundside_geoms)
                if STRtree and groundside_geoms else None)
    apt_air_tree = (STRtree(airside_apt_polys)
                    if STRtree and airside_apt_polys else None)
    zones: List[Polygon] = []
    for bldg in buildings:
        if bldg is None or bldg.is_empty:
            continue
        try:
            coords = list(bldg.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        n = len(coords)
        if n < 3:
            continue
        # (No building-level airside skip; per-edge probe
        # below handles classification.)
        # Per-building two-pass classification.  An edge becomes
        # GROUNDSIDE only when the OSM data gives us an EXPLICIT
        # indicator on that edge — either ``highway=*`` (road
        # class) immediately outward (approach 3) or the apt.dat
        # polygon at that edge is NOT reachable from any runway
        # via touching connectivity (approach 1, future work).
        # An edge with NO clear indicator stays UNKNOWN and is
        # NOT subtracted — promoting unknown to groundside on a
        # building that has at least one airside edge proved too
        # aggressive at HECA, where apron-tagging is patchy and
        # several airside edges look UNKNOWN to OSM.
        EDGE_AIRSIDE = 1
        EDGE_GROUNDSIDE = 2
        EDGE_UNKNOWN = 0
        edge_class: List[int] = [EDGE_UNKNOWN] * n
        edge_geom: List[Optional[Tuple[float, float, float, float,
                                         float, float]]] = [None] * n
        for i in range(n):
            ax, ay = coords[i]
            bx, by = coords[(i + 1) % n]
            edge_len = math.hypot(bx - ax, by - ay)
            if edge_len < 1.0:
                continue
            tx = (bx - ax) / edge_len
            ty = (by - ay) / edge_len
            n_x = ty
            n_y = -tx
            mid_x = 0.5 * (ax + bx)
            mid_y = 0.5 * (ay + by)
            if bldg.contains(
                    Point(mid_x + n_x * 1.0,
                          mid_y + n_y * 1.0)):
                n_x = -n_x
                n_y = -n_y
            edge_geom[i] = (ax, ay, bx, by, n_x, n_y)
            try:
                probe = Polygon([
                    (ax, ay), (bx, by),
                    (bx + n_x * edge_classify_radius_m,
                     by + n_y * edge_classify_radius_m),
                    (ax + n_x * edge_classify_radius_m,
                     ay + n_y * edge_classify_radius_m),
                ])
                if not probe.is_valid:
                    probe = probe.buffer(0)
                if probe.is_empty:
                    continue
            except _GEOM_EXC:
                continue
            airside = False
            groundside = False
            # STRtree.query in Shapely 2.x returns numpy int64
            # indices into the original geometry list — always
            # index back to fetch the geometry.
            if air_tree is not None:
                for hit in air_tree.query(probe):
                    g = airside_geoms[int(hit)]
                    if g.intersects(probe):
                        airside = True
                        break
            if not airside and apt_air_tree is not None:
                # Per user 2026-04-30 (CYXY -10123): use a
                # LONGER probe for the apt.dat-pavement-
                # connectivity check (vs the 30 m default for
                # OSM aeroway tags).  At CYXY the terminal sits
                # in a building-shaped concavity in the apron's
                # exterior ring; the 30 m probe falls inside
                # the concavity and never hits airside pavement.
                # A 100 m probe reaches past the concavity into
                # the apron's main body.  Groundside detection
                # via OSM highway tags still uses the 30 m probe
                # so this doesn't make the classifier more eager
                # to fire airside on roads close to the building.
                _far_radius_m = 100.0
                far_probe = Polygon([
                    (ax, ay), (bx, by),
                    (bx + n_x * _far_radius_m,
                     by + n_y * _far_radius_m),
                    (ax + n_x * _far_radius_m,
                     ay + n_y * _far_radius_m),
                ])
                if not far_probe.is_valid:
                    far_probe = far_probe.buffer(0)
                if not far_probe.is_empty:
                    for hit in apt_air_tree.query(far_probe):
                        g = airside_apt_polys[int(hit)]
                        if g.intersects(far_probe):
                            airside = True
                            break
            if airside:
                edge_class[i] = EDGE_AIRSIDE
                continue
            if grd_tree is not None:
                for hit in grd_tree.query(probe):
                    g = groundside_geoms[int(hit)]
                    if g.intersects(probe):
                        groundside = True
                        break
            if groundside:
                edge_class[i] = EDGE_GROUNDSIDE
                continue
            edge_class[i] = EDGE_UNKNOWN
        # Per user 2026-04-29 (latest): on the airside, the apron
        # SHARES VERTICES with the terminal building (the apron
        # junction wraps around the building corners).  On the
        # groundside, NO airport pavement should connect to the
        # terminal — driveways / parking are typically several
        # metres higher than the airside apron (CYXY 4 m
        # difference) for terrain or elevated road decks.
        #
        # Identification: airside if OSM aeroway features are
        # nearby; UNKNOWN if neither aeroway nor highway is
        # present (typical at airports with sparse OSM road
        # coverage — most of our test set).  When at least ONE
        # edge has an airside indicator, every edge that ISN'T
        # airside is treated as groundside (UNKNOWN promoted).
        # When no edge is airside, we have no signal to pick
        # which direction is which — leave the building alone.
        any_airside = any(c == EDGE_AIRSIDE for c in edge_class)
        for i in range(n):
            cls = edge_class[i]
            if cls == EDGE_AIRSIDE:
                continue
            if cls == EDGE_UNKNOWN and not any_airside:
                continue
            geom = edge_geom[i]
            if geom is None:
                continue
            ax, ay, bx, by, n_x, n_y = geom
            try:
                zone = Polygon([
                    (ax, ay), (bx, by),
                    (bx + n_x * groundside_extent_m,
                     by + n_y * groundside_extent_m),
                    (ax + n_x * groundside_extent_m,
                     ay + n_y * groundside_extent_m),
                ])
                if not zone.is_valid:
                    zone = zone.buffer(0)
                if (zone.geom_type == "Polygon"
                        and not zone.is_empty):
                    zones.append(zone)
            except _GEOM_EXC:
                continue
    if not zones:
        return None
    try:
        merged = unary_union(zones)
        if merged.is_empty:
            return None
        return merged
    except _GEOM_EXC:
        return None


def _extract_osm_terminals(
    nodes: Dict[str, Tuple[float, float]],
    ways: List[Tuple[str, List[str], Dict[str, str]]],
    relations: List[Tuple[str, List[str], Dict[str, str]]],
    to_m,
) -> List[Polygon]:
    """Extract terminal-class building polygons (ways OR
    multipolygon relations with outer rings) in meter space.

    Per user 2026-04-28: also recognize ``aeroway=hangar`` and
    ``aeroway=tower`` because OSM mappers at some airports
    (notably HECA Cairo) tag passenger-terminal-class buildings
    as hangars rather than terminals.  Functionally these are
    identical for the pavement-grading pipeline: flat, fixed-
    altitude structures on apron pavement that the surrounding
    apron should grade to.

    HANGAR_PADS (s81, user 2026-06-12): hangars are ALWAYS
    building pads, terminal-airports included — aprons weld to
    their edges and taxi centerlines stop at their footprints
    (the pipeline trims lanes at pad boundaries, which retires
    the original malformed-sloping-rect concern below).

    Guard against false positives (pre-s81, still governs
    ``aeroway=tower`` and gate-off hangars): hangar / tower are
    only used when the airport has NO ``aeroway=terminal``
    items.  At airports where mappers DID use
    ``aeroway=terminal`` (e.g. SPJC), the explicit terminals
    are authoritative and the hangar/tower buildings are likely
    actual hangars / towers that overlap pavement and would
    cause overlap-clip to malform sloping rects.

    All accepted categories are emitted as ROLE_BUILDING.
    """
    # Detect whether this airport uses explicit aeroway=terminal.
    has_explicit_terminal = any(
        tags.get("aeroway") == "terminal"
        for _wid, _nds, tags in ways)
    has_explicit_terminal = has_explicit_terminal or any(
        tags.get("aeroway") == "terminal"
        for _rid, _wids, tags in relations)
    if has_explicit_terminal:
        terminal_aeroway_tags = {"terminal"}
    else:
        terminal_aeroway_tags = {"terminal", "hangar", "tower"}
    if HANGAR_PADS:
        terminal_aeroway_tags = terminal_aeroway_tags | {"hangar"}
    TERMINAL_AEROWAY_TAGS = terminal_aeroway_tags
    out: List[Polygon] = []
    way_by_id = {wid: (nds, tags) for wid, nds, tags in ways}

    def _ring_polygon(nds: List[str]) -> Optional[Polygon]:
        pts = []
        for n in nds:
            if n in nodes:
                lat, lon = nodes[n]
                pts.append(to_m(lon, lat))
        if len(pts) < 3:
            return None
        try:
            p = Polygon(pts).buffer(0)
        except _GEOM_EXC:
            return None
        if p.is_empty:
            return None
        if p.geom_type == "MultiPolygon":
            p = max(p.geoms, key=lambda g: g.area)
        return p if p.geom_type == "Polygon" else None

    # Way terminals
    for wid, nds, tags in ways:
        if tags.get("aeroway") not in TERMINAL_AEROWAY_TAGS:
            continue
        p = _ring_polygon(nds)
        if p is not None and p.area >= 100.0:
            out.append(p)

    # Relation terminals — STITCH the multipolygon's outer members into
    # the building's boundary ring(s), then emit each significant
    # component as its OWN building pad.
    #
    # ⚠ OSM multipolygon relations encode the outer boundary as a run of
    # OPEN member ways (segments) sharing endpoints — they are NOT each a
    # closed ring.  Treating every member as an independent ring (the old
    # ``_ring_polygon`` per-way path) turned each segment into a sliver
    # and fragmented the building into garbage; the old code then masked
    # that by emitting the CONVEX HULL of the slivers, which spanned the
    # apron between detached concourses into a giant pyramid (SPJC rel -2
    # → 247,422 m², 16× the real building — user 2026-06-15 "turning a T
    # into a big pyramid").  Polygonizing the open linework reconstructs
    # the true footprint: SPJC rel -1 → 56,007 m², rel -2 → 95,594 m²
    # (single concave buildings).  Some relations instead use already-
    # closed member ways — handle both.
    MIN_TERMINAL_COMPONENT_M2 = 500.0
    for rid, outer_wids, tags in relations:
        if tags.get("aeroway") not in TERMINAL_AEROWAY_TAGS:
            continue
        seglines: List[LineString] = []
        ring_polys: List[Polygon] = []
        for wid in outer_wids:
            if wid not in way_by_id:
                continue
            nds, _ = way_by_id[wid]
            pts = [to_m(nodes[n][1], nodes[n][0])
                   for n in nds if n in nodes]
            if len(pts) < 2:
                continue
            if len(pts) >= 4 and nds[0] == nds[-1]:
                p = _ring_polygon(nds)
                if p is not None:
                    ring_polys.append(p)
            else:
                seglines.append(LineString(pts))
        if seglines:
            try:
                ring_polys.extend(
                    g for g in polygonize(unary_union(seglines))
                    if not g.is_empty)
            except _GEOM_EXC:
                pass
        if not ring_polys:
            continue
        try:
            merged = unary_union(
                [g.buffer(0) for g in ring_polys]).buffer(0)
        except _GEOM_EXC:
            continue
        # Collect significant components.
        if merged.geom_type == "Polygon":
            components = [merged] if merged.area >= 100.0 else []
        elif merged.geom_type == "MultiPolygon":
            components = [g for g in merged.geoms
                          if g.geom_type == "Polygon"
                          and g.area >= MIN_TERMINAL_COMPONENT_M2]
        else:
            continue
        out.extend(components)
    return out


def _close_building_outline(pad: Polygon) -> List[Polygon]:
    """Absorb the gate-stand teeth of a finger-pier terminal into a clean
    straight-sided pad (user 2026-06-15) by filling only the NARROW gaps.

    Gate stands are small fingers extending perpendicular off a pier; the
    gaps between them give a terminal a noisy sawtooth boundary.  We fill
    those narrow gaps out to the tooth tips (flat edges) while leaving
    genuine open spaces untouched::

        closed = pad.close(R)      # dilate→erode: bridges EVERY gap up to 2R
        fill   = closed − pad      # all the area the close added
        wide   = fill.open(GATE)   # the WIDE fills — open courtyards/centres
        result = closed − wide     # keep only the narrow teeth-gaps filled

    ``R`` (``BUILDING_OUTLINE_FILL_R``) is how far the fill reaches to
    bridge a teeth gap; a gap WIDER than ``2×GATE``
    (``BUILDING_OUTLINE_FILL_GATE_M``) is reopened as a genuine open space
    (a U courtyard, the space between two piers, the open centre of a
    finger comb).  Because the wide fill is SUBTRACTED from the connected
    closed shape (rather than narrow fills being added back as fragments),
    the pad stays in ONE piece — no floating rinds, no severed spines.
    This supersedes the old plain morphological close, which could only
    bridge gaps ≤ ``2×r`` and so left HECA's sparse, wide-gapped stands as
    a sawtooth (or severed the pier spine when the radius was raised).

    Robust across topologies (U-terminals, blob+pier, bars, long
    buildings) with no limb decomposition; a MITRE join keeps STRAIGHT
    square edges.  No-op for a simple convex/solid footprint.  Applies to
    ALL building sources — OSM and DSF terminals alike.  Returns a LIST for
    API compatibility: a single connected piece in the normal case, but if
    the wide subtraction ever pinches the pad apart, each piece ≥
    ``BUILDING_CLOSE_MIN_PIECE_M2`` is returned.
    """
    R = BUILDING_OUTLINE_FILL_R
    G = BUILDING_OUTLINE_FILL_GATE_M
    if R <= 0 or pad is None or pad.is_empty:
        return [pad]
    try:
        closed = pad.buffer(
            R, join_style=_MITRE_JOIN, mitre_limit=8.0).buffer(
            -R, join_style=_MITRE_JOIN, mitre_limit=8.0)
        fill = closed.difference(pad)
        if not fill.is_empty and G > 0:
            wide = fill.buffer(
                -G, join_style=_MITRE_JOIN).buffer(
                G, join_style=_MITRE_JOIN)
            if not wide.is_empty:
                closed = closed.difference(wide)
    except _GEOM_EXC:
        return [pad]
    # The result always ⊇ pad (we only ever ADD narrow fill); guard anyway.
    if closed.is_empty or closed.area < pad.area - 1.0:
        return [pad]
    if closed.geom_type == "Polygon":
        return [closed]
    if closed.geom_type == "MultiPolygon":
        pieces = [g for g in closed.geoms
                  if g.geom_type == "Polygon" and not g.is_empty
                  and g.area >= BUILDING_CLOSE_MIN_PIECE_M2]
        if pieces and sum(g.area for g in pieces) >= 0.9 * pad.area:
            return pieces
    return [pad]


def _cluster_dsf_building_facades(
    facades: List[Polygon],
    min_area_m2: float = DSF_MIN_BUILDING_AREA_M2,
) -> List[Polygon]:
    """Collapse a flat list of DSF facade footprints into one polygon
    per physical building.

    X-Plane assembles a single (often complex) building from SEVERAL
    facade pieces — stacked (``term_building_Ground`` + ``…_Levels`` on
    the SAME corners), abutting (a long terminal split into a run of
    touching segments), and link spans (``term_bridge_*`` slabs that join
    two wings or, at some airports, ARE the concourse floor).  Unioning
    the lot merges each stack / run / span into one solid footprint; the
    connected components of that union are the individual buildings.  A
    tiny snap-buffer bridges sub-decimetre gaps between facades that share
    an edge but don't quite touch, then is removed so the outline isn't
    inflated.

    The caller decides which facade classes enter ``facades`` (terminal +
    hangar always; ``term_bridge`` gated by ``TERM_BRIDGE_GROUPING``) —
    everything passed in is unioned together.  Returns one outline Polygon
    per building (≥ ``min_area_m2``).
    """
    if not facades:
        return []
    clean = [f for f in facades
             if f is not None and not f.is_empty]
    if not clean:
        return []
    try:
        # Merge facade PIECES of one building (stacked / abutting / scattered
        # panels — e.g. a pier_wooden concourse rendered as dozens of ~0.6 m²
        # panels with 1–3 m gaps) by bridging gaps up to
        # ``DSF_FACADE_MERGE_GAP_M``, so each building becomes ONE cluster
        # instead of a swarm of dropped sub-min-area pieces (user 2026-06-23).
        # The close also fills the panel-grid interior; the DP-simplify below +
        # the downstream outline-close trim the rounding.  buffer(0) first so an
        # unclosed / self-intersecting facade is repaired, never dropped.
        gap = DSF_FACADE_MERGE_GAP_M
        clean = [(f if f.is_valid else f.buffer(0)) for f in clean]
        clean = [f for f in clean if f is not None and not f.is_empty]
        merged = unary_union([f.buffer(gap) for f in clean]).buffer(-gap)
    except _GEOM_EXC:
        try:
            merged = unary_union(clean)
        except _GEOM_EXC:
            return [f for f in clean if f.area >= min_area_m2]
    if merged.is_empty:
        return []
    geoms = (list(merged.geoms)
             if merged.geom_type == "MultiPolygon" else [merged])
    out: List[Polygon] = []
    for g in geoms:
        if g.geom_type != "Polygon" or g.is_empty:
            continue
        if g.area < min_area_m2:
            continue
        # Reduce each cluster to a SOLID footprint (fill the buffer-artifact
        # interior holes — a grading pad is solid) and DP-simplify away the
        # snap-buffer arc noise, keeping the real corners.  Without this a
        # complex terminal carries 1000s of arc vertices that split the
        # outline close and over-resolve the overlap-clip (user 2026-06-15).
        try:
            solid = Polygon(g.exterior)
            simp = solid.simplify(
                DSF_CLUSTER_SIMPLIFY_TOL_M, preserve_topology=True)
            if (simp.geom_type == "Polygon" and not simp.is_empty
                    and simp.area >= min_area_m2):
                g = simp
            else:
                g = solid
        except _GEOM_EXC:
            pass
        out.append(g)
    return out


def _combine_building_sources(
    dsf_buildings: List[Polygon],
    osm_buildings: List[Polygon],
    overlap_frac: float,
) -> List[Polygon]:
    """Union DSF and OSM building outlines, PREFERRING the DSF.

    The DSF footprints are kept verbatim (the sim renders the building
    there, so the grade should match it).  An OSM building is added
    only when it is NOT already represented in the DSF — i.e. less than
    ``overlap_frac`` of its area overlaps the union of DSF footprints.
    OSM buildings clearing that bar are distinct structures the DSF
    didn't place (OSM fills the gap) and are kept as-is.

    Returns the combined seed list (DSF first, then surviving OSM),
    ready to flow through the existing terminal-pad pipeline.
    """
    dsf = [b for b in dsf_buildings if b is not None and not b.is_empty]
    osm = [b for b in osm_buildings if b is not None and not b.is_empty]
    if not dsf:
        return osm
    try:
        dsf_union = unary_union(dsf)
    except _GEOM_EXC:
        dsf_union = None
    combined: List[Polygon] = list(dsf)
    for ob in osm:
        if dsf_union is None or ob.area <= 0:
            combined.append(ob)
            continue
        try:
            inter = ob.intersection(dsf_union).area
        except _GEOM_EXC:
            combined.append(ob)
            continue
        if inter / ob.area < overlap_frac:
            combined.append(ob)  # distinct from any DSF building
        # else: DSF already covers this building → DSF wins, drop OSM.
    return combined




def trim_centerlines_at_buildings(
    centerlines: List[Tuple[LineString, str]],
    building_union,
    min_piece_m: float = 1.0,
) -> Tuple[List[Tuple[LineString, str]], int]:
    """(s81) Taxilanes stop at building edges (user 2026-06-12).

    Subtract the building-pad union from every taxi / service
    centerline feeding the rect builder, so each rect's axis — and
    therefore its end edge — lands ON the pad boundary.  The unify
    weld + conformance passes then share the boundary nodes between
    rect and pad (one altitude per shared vertex = the smooth
    transition the ruling asks for), exactly as rects end against
    aprons.  A lane crossing a building splits into independent
    pieces, each kept as its own axis under the same ref; the rect
    builder's own minimum-length filter decides which pieces are
    long enough to emit.

    Returns ``(trimmed_list, n_trimmed)`` where ``n_trimmed`` counts
    input centerlines that lost any length to a building.
    """
    if building_union is None or getattr(building_union, "is_empty", True):
        return list(centerlines), 0
    out: List[Tuple[LineString, str]] = []
    n_trimmed = 0
    for axis, ref in centerlines:
        try:
            if not axis.intersects(building_union):
                out.append((axis, ref))
                continue
            diff = axis.difference(building_union)
        except _GEOM_EXC:
            out.append((axis, ref))
            continue
        n_trimmed += 1
        if diff.is_empty:
            continue  # lane entirely inside the building
        if diff.geom_type == "LineString":
            pieces = [diff]
        elif hasattr(diff, "geoms"):
            pieces = [g for g in diff.geoms
                      if g.geom_type == "LineString"]
        else:
            pieces = []
        for piece in pieces:
            if piece.length >= min_piece_m:
                out.append((piece, ref))
    return out, n_trimmed

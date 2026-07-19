"""Validate elevation continuity + grade across an X-Plane patch OSM.

Usage:
    python3 tools/check_grade.py <output.osm> [--max-grade 1.5]
                                                [--proximity-m 1.0]
                                                [--edge-step-m 0.5]
                                                [--top-n 10]
                                                [--strict]

Three checks are run on the per-vertex elevations encoded in the
patch (``altitude`` / ``altitude_high`` + ``altitude_low`` /
``node_altitudes`` tags):

1. **Within-shape grade.**  Every pair of vertices on the same way
   must obey ``|de| / dist <= max_grade%`` (default 1.5%).

2. **Cross-shape proximity.**  Two vertices on different ways that
   sit within ``proximity-m`` of each other should agree on
   elevation: max permitted step is the same grade rule applied to
   the (sub-metre) distance — effectively zero step for shared
   corners.  Catches "shape A's corner says X, shape B's matching
   corner says Y" desyncs.

3. **Vertex-to-edge step.**  For every vertex, find the closest
   edge of any OTHER way within 5 m, project the vertex onto that
   edge, and compute the elevation X-Plane would render for the
   *edge* at that projected position (linear interpolation between
   the edge's two endpoint elevations).  The vertex's elevation
   should match within ``edge-step-m`` (default 0.5 m).  Catches
   the "junction triangle 1 m below the sloped taxi rect next to
   it" case.

Exit code is 1 in ``--strict`` mode if any check has any violation
beyond its threshold; 0 otherwise.  Without ``--strict`` the tool
always exits 0 and only reports counts — useful as an informational
diagnostic.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

R_EARTH = 6_378_137.0


# Import per-role grade limits from the auto_patch package (the
# single source of truth).  ``ROLE_GRADE_LIMITS`` maps role-tag to
# decimal grade (e.g. 0.015 for 1.5 %); a value of ``None`` means
# "skip the within-shape grade check for this role".  Roles
# missing from the dict fall back to ``max_grade`` (the function
# argument, default 1.5 %).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_THIS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
try:
    from auto_patch.config import (
        ROLE_GRADE_LIMITS,
        GRADE_VISIBILITY_BUFFER_M as _GRADE_VISIBILITY_BUFFER_M,
        ELEV_ROUNDING_NOISE_M,
        SLOPED_QUAD_ROUNDING_NOISE_M,
        ROUTE_FIELD_MODEL,
        ROUTE_FIELD_LOCAL_WINDOW_M,
        ROAD_FRONTAGE_TOL_M,
        SERVICE_ROAD_MAX_GRADE,
        TAXI_GRADE_BY_WIDTH,
        TAXI_GRADE_WIDTH_ROLES,
        taxi_grade_cap_for_letter,
    )
    from auto_patch.layout import SHARED_VERTEX_TOL_M
except Exception:
    ROLE_GRADE_LIMITS: Dict[str, Optional[float]] = {}
    # Fallbacks (kept in sync with auto_patch.config/layout) so the standalone
    # validator still runs if the package import fails.
    SHARED_VERTEX_TOL_M = 0.5
    _GRADE_VISIBILITY_BUFFER_M = 1.0
    ELEV_ROUNDING_NOISE_M = 0.03
    SLOPED_QUAD_ROUNDING_NOISE_M = 0.1
    ROUTE_FIELD_MODEL = False
    ROUTE_FIELD_LOCAL_WINDOW_M = 80.0
    ROAD_FRONTAGE_TOL_M = 3.0
    SERVICE_ROAD_MAX_GRADE = 0.05
    TAXI_GRADE_BY_WIDTH = True
    TAXI_GRADE_WIDTH_ROLES = frozenset({
        "primary_parallel", "secondary_parallel", "stub", "cross_connector",
    })

    def taxi_grade_cap_for_letter(letter, *, enabled=None):
        on = TAXI_GRADE_BY_WIDTH if enabled is None else enabled
        if on and letter and str(letter).upper() in ("A", "B"):
            return 0.030
        return 0.015


# ── OSM parsing ─────────────────────────────────────────────────

_NODE_RE = re.compile(
    r"<node id='(-?\d+)'[^>]*lat='([^']+)'[^>]*lon='([^']+)'"
)
# A node carrying a per-node ``alt_abs`` tag (the backward-compatible
# replacement for the ``node_altitudes`` way tag): the opening <node ...>
# is NOT self-closing and is immediately followed by its alt_abs child.
_NODE_ALT_RE = re.compile(
    r"<node id='(-?\d+)'[^>]*?>\s*<tag k='alt_abs' v='([^']+)'", re.S
)
_WAY_RE = re.compile(r"<way id='(-?\d+)'[^>]*>(.*?)</way>", re.S)
_ND_RE = re.compile(r"<nd ref='(-?\d+)'")
_TAG_RE = re.compile(r"<tag k='([^']+)' v='([^']+)'")


@dataclass
class Way:
    wid: str
    role: str
    ref: str
    aeroway: str
    nids: List[str]               # closed ring (first repeats at end)
    elevs: List[Optional[float]]  # one per nid (closed-ring length)
    tags: Dict[str, str]


def _parse_osm(path: Path) -> Tuple[Dict[str, Tuple[float, float]],
                                    List[Way]]:
    txt = path.read_text()
    nodes: Dict[str, Tuple[float, float]] = {}
    for m in _NODE_RE.finditer(txt):
        nodes[m.group(1)] = (float(m.group(2)), float(m.group(3)))
    node_alt: Dict[str, float] = {}
    for m in _NODE_ALT_RE.finditer(txt):
        try:
            node_alt[m.group(1)] = float(m.group(2))
        except ValueError:
            pass
    ways: List[Way] = []
    for m in _WAY_RE.finditer(txt):
        wid = m.group(1)
        body = m.group(2)
        nids = _ND_RE.findall(body)
        if len(nids) < 3:
            continue
        tags = dict(_TAG_RE.findall(body))
        # Crown-spine breaklines (crown.py, user 2026-07-07) are OPEN
        # ways carrying the PRE-crown spine profile as per-node
        # ``alt_abs`` — mesh input only, not a pavement shape.  Grading
        # them as a '?'-role ring produced phantom 10 % pairs (the
        # closing pseudo-edge spans the whole spine).  The gap-fill
        # drainage spines (2026-07-09) and gap interior rings (ratified
        # 2026-07-11) are the same class: open constrained breakline
        # ways inside a gap face — their phantom closing pseudo-edge
        # cuts across the gap and minted artifact vertex-to-edge steps
        # against real interior nodes.
        if tags.get("o4_feature") in ("crown_spine",
                                      "gap_drainage_spine",
                                      "gap_interior_ring"):
            continue
        elevs = _derive_per_vertex_elevations(nids, tags, node_alt)
        ways.append(Way(
            wid=wid,
            role=tags.get("role", ""),
            ref=tags.get("ref", ""),
            aeroway=tags.get("aeroway", ""),
            nids=nids,
            elevs=elevs,
            tags=tags,
        ))
    return nodes, ways


def _derive_per_vertex_elevations(nids: List[str], tags: Dict[str, str],
                                  node_alt: Optional[Dict[str, float]] = None
                                  ) -> List[Optional[float]]:
    """Decode the X-Plane patch elevation tags into a per-nid
    elevation list of the same length as ``nids`` (i.e. closed-ring
    length, last entry == first entry's elevation)."""
    n = len(nids)
    if "altitude_high" in tags and "altitude_low" in tags:
        try:
            ah = float(tags["altitude_high"])
            al = float(tags["altitude_low"])
        except ValueError:
            return [None] * n
        # X-Plane patch convention for a 4-corner rect way:
        # nids[0]=hi-left, [1]=lo-left, [2]=lo-right, [3]=hi-right,
        # [4]=closing repeat of [0].  Way[-2:] = [n3, n0] is the
        # HIGH short edge; way[1:3] = [n1, n2] is the LOW short
        # edge.  See O4_Vector_Map.include_patches() for the parser.
        # altitude_high/low ways are COMPLEX upstream (cplx_way=True):
        # the per-node alt_abs override does NOT apply to them.
        if n == 5:
            return [ah, al, al, ah, ah]
        # Rectangles with insertions along the long edges are
        # unsupported by X-Plane's altitude_high/low parser
        # (it expects exactly 5 nodes); flag as unknown.
        return [None] * n
    # Non-complex ways: start from the way-level altitude form, then apply
    # the per-node ``alt_abs`` override exactly as O4_Vector_Map.include_
    # patches() does (it overrides alti_way[i] for every node carrying the
    # tag).  The legacy ``node_altitudes`` way tag is handled the same way
    # so old and new patches validate identically.
    base: List[Optional[float]]
    if "node_altitudes" in tags:
        try:
            vals = [float(x) for x in tags["node_altitudes"].split(",")]
        except ValueError:
            vals = []
        base = [float(v) for v in vals] if len(vals) == n else [None] * n
    elif "altitude" in tags:
        try:
            base = [float(tags["altitude"])] * n
        except ValueError:
            base = [None] * n
    else:
        base = [None] * n
    if node_alt:
        for i, nid in enumerate(nids):
            if nid in node_alt:
                base[i] = node_alt[nid]
    return base


# ── Coordinate space ────────────────────────────────────────────

def _ll_to_m_factory(nodes: Dict[str, Tuple[float, float]],
                     anchor: "Optional[Tuple[float, float]]" = None):
    """Equirectangular lat/lon→meter factory.

    ``anchor`` (lat, lon): use the BUILDER's projection anchor (from the axes
    sidecar) instead of the mean of nodes — the two frames differ in x-scale
    via ``cos(lat0)``, millimetres over a chord, enough to flip epsilon
    contact predicates (the crossing-skip rule) and make the validator read
    the law differently from the solver.  Same formula and R_EARTH as
    ``auto_patch.layout._projection``, so with the anchor the frames are
    identical to float precision."""
    if anchor is not None:
        lat0, lon0 = float(anchor[0]), float(anchor[1])
    elif nodes:
        lats = [v[0] for v in nodes.values()]
        lons = [v[1] for v in nodes.values()]
        lat0 = sum(lats) / len(lats)
        lon0 = sum(lons) / len(lons)
    else:
        return lambda lat, lon: (0.0, 0.0)
    cos0 = math.cos(math.radians(lat0))

    def _f(lat: float, lon: float) -> Tuple[float, float]:
        x = math.radians(lon - lon0) * R_EARTH * cos0
        y = math.radians(lat - lat0) * R_EARTH
        return x, y
    return _f


# ── Vertex / edge tables ────────────────────────────────────────

@dataclass
class Vertex:
    way_idx: int
    nid: str
    x: float
    y: float
    elev: Optional[float]


@dataclass
class Edge:
    way_idx: int
    a: Tuple[float, float]
    b: Tuple[float, float]
    ea: float
    eb: float


def _build_vertex_edge_tables(
    nodes: Dict[str, Tuple[float, float]],
    ways: List[Way],
    ll_to_m,
) -> Tuple[List[Vertex], List[Edge]]:
    vertices: List[Vertex] = []
    edges: List[Edge] = []
    for way_idx, w in enumerate(ways):
        # Vertices (skip the closing repeat to avoid double-counting).
        for k, nid in enumerate(w.nids[:-1] if len(w.nids) > 1
                                and w.nids[0] == w.nids[-1]
                                else w.nids):
            if nid not in nodes:
                continue
            lat, lon = nodes[nid]
            x, y = ll_to_m(lat, lon)
            vertices.append(Vertex(
                way_idx=way_idx, nid=nid, x=x, y=y, elev=w.elevs[k]))
        # Edges (use the closed ring so the last edge wraps).
        ring = w.nids
        if len(ring) >= 2 and ring[0] != ring[-1]:
            ring = ring + [ring[0]]
        for k in range(len(ring) - 1):
            a_nid = ring[k]
            b_nid = ring[k + 1]
            if a_nid not in nodes or b_nid not in nodes:
                continue
            a_xy = ll_to_m(*nodes[a_nid])
            b_xy = ll_to_m(*nodes[b_nid])
            ea = w.elevs[k] if k < len(w.elevs) else None
            eb = (w.elevs[k + 1] if (k + 1) < len(w.elevs)
                  else w.elevs[0])
            if ea is None or eb is None:
                continue
            edges.append(Edge(
                way_idx=way_idx, a=a_xy, b=b_xy, ea=ea, eb=eb))
    return vertices, edges


# ── Spatial bucketing ───────────────────────────────────────────

def _bucket_vertices(vertices: List[Vertex], cell_m: float
                     ) -> Dict[Tuple[int, int], List[int]]:
    out: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, v in enumerate(vertices):
        out[(int(math.floor(v.x / cell_m)),
             int(math.floor(v.y / cell_m)))].append(i)
    return out


def _bucket_edges(edges: List[Edge], cell_m: float
                  ) -> Dict[Tuple[int, int], List[int]]:
    """Bucket each edge into every cell its bounding box touches
    (inflated by 1 cell so a query within any neighbour cell finds
    it)."""
    out: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, e in enumerate(edges):
        x_lo = min(e.a[0], e.b[0])
        x_hi = max(e.a[0], e.b[0])
        y_lo = min(e.a[1], e.b[1])
        y_hi = max(e.a[1], e.b[1])
        cx_lo = int(math.floor(x_lo / cell_m))
        cx_hi = int(math.floor(x_hi / cell_m))
        cy_lo = int(math.floor(y_lo / cell_m))
        cy_hi = int(math.floor(y_hi / cell_m))
        for cx in range(cx_lo, cx_hi + 1):
            for cy in range(cy_lo, cy_hi + 1):
                out[(cx, cy)].append(i)
    return out


# ── Checks ──────────────────────────────────────────────────────

@dataclass
class Violation:
    grade_pct: float       # %
    excess_pct: float      # %
    distance_m: float
    de_m: float
    way_a: Way
    way_b: Way
    pt_a: Tuple[float, float]
    pt_b: Tuple[float, float]
    elev_a: float
    elev_b: float
    # Geographic location of the violation (lat, lon), filled in by
    # run_checks so callers can point a user at the spot.  None until set.
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass
class EdgeStep:
    step_m: float
    distance_m: float
    way_v: Way
    way_e: Way
    vert_pt: Tuple[float, float]
    proj_pt: Tuple[float, float]
    elev_v: float
    elev_proj: float
    lat: Optional[float] = None
    lon: Optional[float] = None


# ELEV_ROUNDING_NOISE_M now imported from auto_patch.config (single source of
# truth shared with the runtime audit) — see the import block above.


# X-Plane tile seams run along integer latitude / longitude lines.
# auto_patch handles them in two passes:
#
#   1. ``seam_anchors`` inserts vertices on each integer line that
#      crosses the airport and HARD-anchors them to ``dem.alt_strict``
#      — the terrain mesh in the neighbour tile pins the same points
#      to DEM, so they must agree or the patch tears at the boundary.
#   2. ``tile_cut`` later subtracts a ``half_width_m`` strip
#      (default 5 m each side, 10 m total) at every integer line.
#      The pre-cut seam vertex at the integer line is removed; the
#      resulting polygon gets new boundary vertices on the airport
#      side of the strip, exactly ``half_width_m`` away from the
#      integer line.  Those new vertices inherit altitudes by
#      resampling (nearest-neighbour or slope-projected) from the
#      pre-cut DEM-anchored ring — so they're effectively DEM-pinned
#      too, even though they no longer carry the seam tag.
#
# Both classes of vertex are immovable from the solver's POV: their
# altitudes are dictated by the DEM at the tile boundary, and the
# adjacent-tile patch + terrain mesh must agree exactly.  Within-
# shape grade between any pair touching one of these vertices is a
# function of DEM noise at the tile edge, not of solver feasibility,
# so we skip those pairs (and any triangle that touches one).
#
# Cross-shape proximity and edge/mid-edge step checks naturally
# still pass because both adjacent shapes sample the same DEM at
# the same XY, so the tile-edge vertex altitudes agree across
# shapes.
#
# Detection is geometric: a vertex is on the tile seam iff its lat
# OR its lon is within ``_SEAM_LL_TOL_DEG`` of an integer value.
# The tolerance (1e-4 °, ~11 m) covers the 5-m offset of post-cut
# boundary vertices plus slack for projection round-trip drift.
_SEAM_LL_TOL_DEG = 1e-4

# SEAM TERRAIN-MATCHING ZONE (user 2026-06-20): at a tile boundary the
# pavement must MATCH the neighbour tile's terrain mesh (so X-Plane bridges the
# gap without a cliff), so it follows the DEM from the seam inward — not the
# designed flat/compliant surface.  The within-shape grade cap and the
# runway-anchored route-band law both assume a designed surface, so they YIELD
# inside this zone.  Only shapes that actually reach a seam (a real integer-line
# crossing) get the zone; single-tile airports are unaffected.  The width
# covers the cross-seam sliver where terrain controls (SPLP descends ~4 m to
# the seam over a few hundred metres).  NOT special-cased per airport.
_SEAM_ZONE_M = 400.0
_M_PER_DEG_LAT = 110540.0


def _seam_lines(nodes: Dict[str, Tuple[float, float]]) -> Tuple[set, set]:
    """Integer lat / lon values that an exact seam vertex sits on — i.e. the
    tile boundaries the airport actually CROSSES (a real seam, not just being
    near a tile edge)."""
    seam_lats: set = set()
    seam_lons: set = set()
    for (lat, lon) in nodes.values():
        if abs(lat - round(lat)) <= _SEAM_LL_TOL_DEG:
            seam_lats.add(round(lat))
        if abs(lon - round(lon)) <= _SEAM_LL_TOL_DEG:
            seam_lons.add(round(lon))
    return seam_lats, seam_lons


def _seam_nids_from_pins(nodes: Dict[str, Tuple[float, float]],
                         seam_pins_ll: list) -> set:
    """Nids coincident (≤ ``SHARED_VERTEX_TOL_M``) with a sidecar seam-PIN
    vertex — the exact DEM-pinned anchors the solver graded to.  Replaces
    the 400 m zone of :func:`_seam_nids` when the sidecar carries pins."""
    if not seam_pins_ll:
        return set()
    out: set = set()
    for nid, (lat, lon) in nodes.items():
        mlon = _M_PER_DEG_LAT * max(0.05, math.cos(math.radians(lat)))
        for (pla, plo) in seam_pins_ll:
            d_lat = abs(lat - pla) * _M_PER_DEG_LAT
            if d_lat > SHARED_VERTEX_TOL_M:
                continue
            if math.hypot(d_lat, abs(lon - plo) * mlon) \
                    <= SHARED_VERTEX_TOL_M:
                out.add(nid)
                break
    return out


def _crown_drops_by_nid(nodes: Dict[str, Tuple[float, float]],
                        crown_drops_ll: list) -> Dict[str, float]:
    """Map each nid to its solver crown drop (axes sidecar ``crown_drops``,
    ``[[lat, lon, drop], …]``) — nids coincident (≤ ``SHARED_VERTEX_TOL_M``)
    with an exported field node.  The within-shape law re-centres each
    pair's budget on ``grade_law.crown_pair_offset`` from this field, so
    the validator reads the SAME designed crown the solver built (part
    30).  Empty/None ⇒ offset 0 everywhere (uncrowned/old patches)."""
    if not crown_drops_ll:
        return {}
    # coarse lat/lon grid (~SHARED_VERTEX_TOL_M cells) for O(1) lookups.
    # ONE cell size for build AND lookup (reference latitude): a per-point
    # cos(lat) cell size shifts the integer cell index by whole cells at
    # large |lon| (~14.6 M cells at lon −135), silently missing matches.
    ref_lat = crown_drops_ll[0][0]
    cell_lat = SHARED_VERTEX_TOL_M / _M_PER_DEG_LAT
    mlon_ref = _M_PER_DEG_LAT * max(0.05, math.cos(math.radians(ref_lat)))
    cell_lon = SHARED_VERTEX_TOL_M / mlon_ref
    grid: Dict[Tuple[int, int], list] = defaultdict(list)
    for (pla, plo, drop) in crown_drops_ll:
        grid[(int(pla // cell_lat), int(plo // cell_lon))].append(
            (pla, plo, float(drop)))
    out: Dict[str, float] = {}
    for nid, (lat, lon) in nodes.items():
        mlon = _M_PER_DEG_LAT * max(0.05, math.cos(math.radians(lat)))
        gx, gy = int(lat // cell_lat), int(lon // cell_lon)
        best = None
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for (pla, plo, drop) in grid.get((gx + ox, gy + oy), ()):
                    d = math.hypot((lat - pla) * _M_PER_DEG_LAT,
                                   (lon - plo) * mlon)
                    if d <= SHARED_VERTEX_TOL_M and (best is None
                                                     or d < best[0]):
                        best = (d, drop)
        if best is not None:
            out[nid] = best[1]
    return out


def _crown_centerline_nids(nodes: Dict[str, Tuple[float, float]],
                           crown_centerline_ll: list) -> set:
    """The nids coincident (≤ ``SHARED_VERTEX_TOL_M``) with a CROWN-CENTERLINE
    vertex the interior runway cross-edge crown inserted (axes sidecar
    ``crown_centerline``, ``[[lat, lon], …]``).  Such a node lies ON the
    runway ridge, so its longitudinal grade is governed by the SPINE PROFILE
    check and its lateral crown is sub-cap by design — the within-shape
    flat-cap all-pairs plane law skips any runway pair touching one (a
    cross-station diagonal to it would conflate the two).  Empty/None ⇒ no
    skips (uncrowned / old patches)."""
    if not crown_centerline_ll:
        return set()
    ref_lat = crown_centerline_ll[0][0]
    cell_lat = SHARED_VERTEX_TOL_M / _M_PER_DEG_LAT
    mlon_ref = _M_PER_DEG_LAT * max(0.05, math.cos(math.radians(ref_lat)))
    cell_lon = SHARED_VERTEX_TOL_M / mlon_ref
    grid: Dict[Tuple[int, int], list] = defaultdict(list)
    for (pla, plo) in crown_centerline_ll:
        grid[(int(pla // cell_lat), int(plo // cell_lon))].append((pla, plo))
    out: set = set()
    for nid, (lat, lon) in nodes.items():
        mlon = _M_PER_DEG_LAT * max(0.05, math.cos(math.radians(lat)))
        gx, gy = int(lat // cell_lat), int(lon // cell_lon)
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for (pla, plo) in grid.get((gx + ox, gy + oy), ()):
                    d = math.hypot((lat - pla) * _M_PER_DEG_LAT,
                                   (lon - plo) * mlon)
                    if d <= SHARED_VERTEX_TOL_M:
                        out.add(nid)
                        break
    return out


def _seam_nids(nodes: Dict[str, Tuple[float, float]]) -> set:
    """Set of nids in the seam terrain-matching zone: within ``_SEAM_ZONE_M``
    of a tile boundary the airport CROSSES (lat/lon line carrying an exact seam
    vertex).  Empty for single-tile airports → no exemption (byte-identical)."""
    seam_lats, seam_lons = _seam_lines(nodes)
    if not seam_lats and not seam_lons:
        return set()
    out: set = set()
    for nid, (lat, lon) in nodes.items():
        d = float("inf")
        for sl in seam_lats:
            d = min(d, abs(lat - sl) * _M_PER_DEG_LAT)
        if seam_lons:
            mlon = _M_PER_DEG_LAT * max(0.05, math.cos(math.radians(lat)))
            for sl in seam_lons:
                d = min(d, abs(lon - sl) * mlon)
        if d <= _SEAM_ZONE_M:
            out.add(nid)
    return out


def _check_plane_gradient(ways: List[Way],
                          nodes: Dict[str, Tuple[float, float]],
                          ll_to_m,
                          max_grade: float,
                          seam_nids: Optional[set] = None,
                          crown_by_nid: Optional[Dict[str, float]] = None,
                          ) -> List[Violation]:
    """For each 3-vertex polygon (a triangle, which X-Plane renders
    as a planar surface), compute the plane's elevation gradient
    and flag if its magnitude exceeds ``max_grade``.

    A triangle can pass every vertex-PAIR grade check yet still
    have a steep perpendicular gradient: for (A=20, B=19, C=19.5)
    placed with A 300 m from B (edge grades ~0.3 %), the plane's
    gradient perpendicular to BC may be several %.  This shows up
    as a visible slope inside the triangle even though no vertex
    pair is "too steep".

    Triangles that touch any seam vertex are skipped — their plane
    is dictated by DEM-pinned corners the solver cannot move.

    SPINE CROWN (part 30): ``crown_by_nid`` is the solver's designed
    crown-drop field (sidecar ``crown_drops`` → ``_crown_drops_by_nid``).
    The plane is evaluated in UNCROWNED space ``z' = z + crown_drop`` —
    the SAME space the solver grades in (``grade_law`` crown block) and
    the within-shape pair check re-centres to via
    ``grade_law.crown_pair_offset``.  Without the lift this check was
    the one crown-blind reader of the three (solver / within-pair /
    plane): a lawful crowned triangle spanning a ridge node and dropped
    edge nodes false-flagged whenever the raw resultant tilt
    (longitudinal grade ⊕ transverse crown) exceeded the cap — SPJC
    junction #141 read 2.30 % raw vs 1.10 % designed.  Since every
    crown rate ≤ every transverse cap, the lift can only remove the
    designed-crown component — a genuinely over-cap surface still
    flags — and an empty field (uncrowned / old patches) leaves
    ``z' = z``, byte-identical to the unlifted check.
    """
    seam_nids = seam_nids or set()
    crown_by_nid = crown_by_nid or {}
    out: List[Violation] = []
    for w in ways:
        grade_cap = _role_grade_limit(w, max_grade)
        if grade_cap is None:
            continue
        # Pre-screen ring nids — skip the whole triangle if any
        # vertex lies on the tile seam.
        ring_nids = (w.nids[:-1] if (len(w.nids) > 1
                     and w.nids[0] == w.nids[-1])
                     else w.nids)
        if any(nid in seam_nids for nid in ring_nids):
            continue
        pts: List[Tuple[float, float, float]] = []
        for k, nid in enumerate(w.nids[:-1] if (len(w.nids) > 1
                                and w.nids[0] == w.nids[-1])
                                else w.nids):
            if nid not in nodes:
                continue
            lat, lon = nodes[nid]
            x, y = ll_to_m(lat, lon)
            e = w.elevs[k]
            if e is None:
                continue
            # Crown lift: evaluate the plane in the solver's UNCROWNED
            # space (see the docstring) — 0 for nodes off the field.
            pts.append((x, y, e + crown_by_nid.get(nid, 0.0)))
        if len(pts) != 3:
            continue  # only check triangles
        (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = pts
        # Plane normal via cross product of two in-plane vectors.
        ux, uy, uz = x2 - x1, y2 - y1, z2 - z1
        vx, vy, vz = x3 - x1, y3 - y1, z3 - z1
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        if abs(nz) < 1e-6:
            continue  # degenerate triangle in xy plane
        # Plane: nx*X + ny*Y + nz*Z = d; dz/dx = -nx/nz, dz/dy = -ny/nz.
        gx = -nx / nz
        gy = -ny / nz
        grad = math.hypot(gx, gy)
        # Project vertices along the gradient direction to get the
        # plane's altitude swing across the triangle.  The
        # gradient check fires only when the swing exceeds the
        # grade cap allowance for that swing distance — matching
        # the within-shape pair check's rounding-noise envelope.
        gnorm = grad
        if gnorm < 1e-9:
            continue
        ghx, ghy = gx / gnorm, gy / gnorm
        proj = [(p[0] * ghx + p[1] * ghy, p[2], p)
                for p in pts]
        proj.sort()
        lo_p, lo_z, lo_pt = proj[0]
        hi_p, hi_z, hi_pt = proj[-1]
        dist_along_grad = hi_p - lo_p
        de_along_grad = abs(hi_z - lo_z)
        allowance = grade_cap * dist_along_grad + ELEV_ROUNDING_NOISE_M
        if de_along_grad <= allowance:
            continue
        out.append(Violation(
            grade_pct=grad * 100,
            excess_pct=(grad - grade_cap) * 100,
            distance_m=dist_along_grad if dist_along_grad > 0.5
                       else 1.0,
            de_m=de_along_grad,
            way_a=w, way_b=w,
            pt_a=(lo_pt[0], lo_pt[1]),
            pt_b=(hi_pt[0], hi_pt[1]),
            elev_a=lo_z, elev_b=hi_z))
    return out


# (The old WITHIN_SHAPE_MAX_PAIR_DIST_M distance cap was removed: the
# within-shape check is now uncapped + visibility-gated — see
# _check_within_shape.)



def _role_grade_limit(way: "Way",
                      default_grade: float) -> Optional[float]:
    """Resolve the within-shape grade limit for a way.

    Looks up the way's ``role`` tag in ``ROLE_GRADE_LIMITS`` (the
    single source of truth in ``auto_patch.config``):

    * Returns the role-specific limit (decimal, e.g. 0.015) if
      the role is present.
    * Returns ``None`` if the role is explicitly mapped to ``None``
      (skip the check — vertical structures, terrain-following
      outlines).
    * Falls back to ``default_grade`` when the role is absent
      from the dict (unknown role; use the function-argument
      cap so behaviour stays compatible with un-tagged input).
    """
    role = way.tags.get("role")
    # APRON-EDGE GRADE ADOPTION (USER RULING 2026-07-06): a service
    # road/junction portion inside or alongside an apron follows the
    # apron grading rules — the build stamps ``o4_grade_law='apron'``
    # on exactly those pieces; validate them at that role's cap so both
    # readers apply the same law.
    _law_override = way.tags.get("o4_grade_law")
    # TAXIWAY-EDGE ADOPTION (USER RULING 2026-07-07): a service-road
    # portion inside/alongside a taxiway follows the taxiway grade law
    # (1.5 %, letter-aware).  The build stamps ``o4_grade_law='taxi'`` +
    # ``code_letter``; validate at the same letter-aware cap the solver
    # used so both readers apply the same law.
    if _law_override == "taxi":
        return taxi_grade_cap_for_letter(way.tags.get("code_letter"))
    if _law_override and _law_override in ROLE_GRADE_LIMITS:
        return ROLE_GRADE_LIMITS[_law_override]
    # Size-dependent taxiway cap (gate TAXI_GRADE_BY_WIDTH): a sized
    # taxiway carries the ICAO code letter the build stamped on it; code
    # A/B (narrow, <15 m) validate at 3 %, C–F at 1.5 % — ICAO Annex 14
    # §3.9.3.  Mirrors the solver's per-shape cap so the validator and
    # build stay in lockstep.  Patches without the tag (gate off / older
    # builds) fall through to the uniform role cap below.
    if role in TAXI_GRADE_WIDTH_ROLES:
        letter = way.tags.get("code_letter")
        if letter:
            return taxi_grade_cap_for_letter(letter)
    if role in ROLE_GRADE_LIMITS:
        return ROLE_GRADE_LIMITS[role]
    return default_grade


def _pair_grade_limit(way_a: "Way", way_b: "Way",
                      default_grade: float) -> Optional[float]:
    """Resolve the cross-shape grade limit between two ways.

    Returns ``None`` (= skip the pair) when either way's role is
    on the skip-list (boundary, retaining_wall,
    groundside_pavement) — these are intentionally at terrain
    elevations or stacked at different vertical layers (a
    retaining_wall sitting at apt_elev above a tunnel_ramp at
    apt_elev−8m at the same XY is not an elevation
    "disagreement"; the wall and the ramp are different terrain
    layers by design).

    Otherwise returns the more restrictive of the two role's
    grade caps so close-but-not-shared vertices satisfy both
    surfaces' grade rules.
    """
    a = _role_grade_limit(way_a, default_grade)
    if a is None:
        return None
    b = _role_grade_limit(way_b, default_grade)
    if b is None:
        return None
    return min(a, b)


# Groundside pavement (vehicle roads, curbside drop-off, parking) is
# deliberately SEPARATED from airside pavement by a clearance gap and a
# retaining / vertical wall (user 2026-05-28): the two surfaces are NOT meant to
# be flush and can legitimately differ by several metres.  So the cross-shape
# STEP checks below — which assume neighbouring pavement should be vertically
# continuous — must NOT fire across the airside <-> groundside boundary.  (Each
# side's own within-shape grade still applies.)
# ``tunnel_ramp`` (the depressed-road plates + portal ramps) is the same class
# (user 2026-06-10): the road runs at apt_elev−8 m, clipped 0.5 m short of all
# airside pavement — the 8 m face across that designed gap is the retaining
# wall, not an elevation defect.  KPHX's ZDP aprons abutting Sky Harbor Blvd
# fired 307 step / 32 cross warnings on this designed separation.
_GROUNDSIDE_ROLES = {"groundside_pavement", "service_road", "service_junction",
                     "tunnel_ramp"}


def _is_groundside(way: "Way") -> bool:
    return way.tags.get("role") in _GROUNDSIDE_ROLES


_ROAD_FAMILY_ROLES = {"service_road", "service_junction"}


def _airside_groundside_pair(way_a: "Way", way_b: "Way") -> bool:
    """True iff a designed wall separates the two ways: exactly one is
    groundside, OR exactly one is ROAD-family (s79 Step D) — a
    ground-vehicle road grades at 5 % from its apron mouth down to
    terrain, so where it runs beside curbside groundside (the CYXY
    pav[1] ramp: a 6.5 m retaining wall vs the parking lot) or beside
    airside pavement, the vertical seam is by design.  Road↔road pairs
    stay checked — the road network itself is one continuous surface.
    (Both-groundside-family pairs previously slipped the exactly-one
    test and fired 151 false steps at the CYXY ramp.)"""
    a_road = way_a.tags.get("role") in _ROAD_FAMILY_ROLES
    b_road = way_b.tags.get("role") in _ROAD_FAMILY_ROLES
    if a_road != b_road:
        return True
    return _is_groundside(way_a) != _is_groundside(way_b)


# The step checks enforce vertical continuity only where two shapes actually
# TOUCH (share a boundary).  Beyond this perpendicular contact distance the
# shapes are separated by a GAP (no pavement between them) and a height
# difference is allowed (user 2026-05-28) — do not flag it.  Genuinely adjacent
# pavement shares welded / conformance-inserted vertices (contact ~0); the
# documented real case (a junction edge ~0.3 m alongside a sloped rect) stays
# within this tolerance, while gapped neighbours 2-5 m apart are excluded.
_STEP_CONTACT_TOL_M = 1.0


# _GRADE_VISIBILITY_BUFFER_M now imported from auto_patch.config (single source
# of truth) — see the import block above.


def _polygon_visibility(pts):
    """Return a ``vis(xa, ya, xb, yb) -> bool`` predicate: True iff the chord
    stays inside the polygon defined by ``pts`` (ring order, [(x, y, ...), ...])
    grown by ``_GRADE_VISIBILITY_BUFFER_M``.  Returns ``None`` if shapely is
    unavailable or the polygon is degenerate, so callers fall back to plain
    all-pair (the prior behaviour)."""
    try:
        from shapely.geometry import LineString, Polygon
        from shapely.prepared import prep
    except ImportError:
        return None
    try:
        poly = Polygon([(p[0], p[1]) for p in pts])
        if not poly.is_valid:
            poly = poly.buffer(0)
        poly = poly.buffer(_GRADE_VISIBILITY_BUFFER_M)
        if poly.is_empty:
            return None
        pg = prep(poly)
    except Exception:
        return None

    def _vis(xa, ya, xb, yb):
        try:
            return pg.contains(LineString(((xa, ya), (xb, yb))))
        except Exception:
            return True

    return _vis


@dataclass
class ShapePairConstraint:
    """One within-shape grade constraint on a vertex pair (the SINGLE source
    of truth for the constrained pair set — consumed by the validator AND the
    feasibility oracle ``tools/grade_feasibility_audit.py``).  The grade law
    is ``|elev_a - elev_b| <= cap * dist`` plus the emit/weld quantization
    envelope, folded into ``allowance`` by :func:`_pair_grade_allowance`
    (``max(route-baked budget, cap*dist)`` — never TIGHTER than the flat cap —
    plus the per-shape quantization noise, :func:`_pair_quant_noise_m`)."""
    way: "Way"
    nid_a: str
    nid_b: str
    xa: float
    ya: float
    ea: float
    xb: float
    yb: float
    eb: float
    dist: float
    cap: float          # decimal grade limit for this pair (role / road / ramp)
    allowance: float    # max(baked, cap*dist) + quant-noise (validator tolerance)
    # SPINE CROWN (part 30): the designed crown target of ``ea − eb``
    # (``grade_law.crown_pair_offset`` over the sidecar drop field); the
    # law is ``|(ea − eb) − offset| ≤ allowance``.  0 for uncrowned pairs.
    offset: float = 0.0


_WELD_HUB_ROLES = frozenset({"junction", "service_junction"})


def _pair_quant_noise_m(way: "Way") -> float:
    """Quantization allowance for a within-shape PAIR on ``way`` — the emit /
    weld micro-step envelope the pair's Δz can carry without meaning a real
    grade defect.

    Per-node ``alt_abs`` emits at 0.01 m, so an all-per-node body pair carries
    only ``ELEV_ROUNDING_NOISE_M`` of rounding.  The coarse envelope
    ``SLOPED_QUAD_ROUNDING_NOISE_M`` (0.1 m) applies to two shape classes whose
    ring edges carry a full decimetre of emit/weld displacement:

    * **Sloped-quad ways** (``altitude_high``/``altitude_low``) emit at 0.1 m
      (``bridges.py`` ``_emit_tunnel_portals`` grade_safety_margin) — a pair
      spanning the high and low corners carries a full 0.1-m round.
    * **Junction-family ways** (the emit WELD HUBS): junction rings are
      rebuilt by the conformance / planarization pass — T-vertex inserts,
      unshared-neighbour-corner inserts, epsilon-wedge welds (see the
      ``[conformance]`` / ``[pav-builder]`` junction logs) — which displace a
      short ring edge by up to the same decimetre.  A junction's short ring
      edge can therefore read a few % over its 1.5 % cap purely from that
      weld displacement (SPLP junction #68: 6 cm over 0.85 m).  On a LONG
      junction pair the absolute 0.1 m adds only a fraction of a % of grade
      headroom, so this loosens only the short weld-artifact edges, not the
      real long-range junction grade the field solver owns."""
    if "altitude_high" in way.tags and "altitude_low" in way.tags:
        return SLOPED_QUAD_ROUNDING_NOISE_M
    if way.tags.get("role") in _WELD_HUB_ROLES:
        return SLOPED_QUAD_ROUNDING_NOISE_M
    return ELEV_ROUNDING_NOISE_M


def _pair_grade_allowance(cap_allow, dist: float, way: "Way") -> float:
    """The within-shape PAIR grade tolerance ``cap × run + allowance`` (the
    symmetric quantization envelope the plane-gradient law already grants —
    ``_check_plane_gradient`` uses ``grade_cap·dist + ELEV_ROUNDING_NOISE_M``).

    ``cap × run`` is the FLAT budget ``cap_allow.flat_cap() · dist``; a
    route-decomposed BAKED allowance (``cap_allow.at`` returning an anisotropic
    ``√((cL·Δs∥)² + (cT·Δs⊥)²)`` budget) is honoured only when it EXCEEDS the
    flat budget (the curve arc-credit case) — never when it is TIGHTER, so an
    at-cap emitted pair never false-flags merely because its route projection
    trimmed the budget below the flat cap (SPJC service_road #461: 5.006 % =
    0.5 mm over the 5 % cap, but the baked budget sat 5 cm below it).  The
    quantization allowance on top is the emit/weld envelope for the shape's
    encoding (``_pair_quant_noise_m``).  The budget core is
    ``grade_law.pair_grade_budget_m`` — THE single formula shared with
    ``grade_graph_validate`` so the two pair-law readers cannot drift."""
    from auto_patch.grade_law import pair_grade_budget_m
    return pair_grade_budget_m(cap_allow, dist) + _pair_quant_noise_m(way)


_SLOPING_RECT_OSM_ROLES = frozenset({
    "primary_parallel", "secondary_parallel", "stub", "cross_connector",
})


def _grade_context_from_osm(ways, nodes, ll_to_m, taxi_axes, seam_nids,
                            max_grade, road_zone=None, routes_m=None,
                            mesh_edges_m=None):
    """Build the SAME ``grade_graph.GradeContext`` the solver uses, but from the
    emitted OSM — so the grade TEST reads the one shared within-shape LAW
    (``grade_law.classify_pair`` via ``grade_graph.shape_constraints``).  Keys are
    OSM node ids (the ``GradeShape.keys`` the soft-shape reader puts on its ring),
    so the seam / building-step exemptions match by identity.  Mirrors
    ``grade_graph.build_context`` (centerlines from the apt.dat taxi axes, spine-
    less junction cap inherited from the nearest taxi rect, building-pad keys)."""
    from auto_patch import grade_graph as GG
    from auto_patch.config import TAXI_MAX_GRADE

    centerlines = []
    axis_ridx = []
    for entry in (taxi_axes or []):
        poly, cL = entry[0], entry[1]
        if len(poly) < 2:
            continue
        # ``cL`` is a scalar (legacy split axes: one cap for the whole piece)
        # or a per-SEGMENT cap list (exact build_context mirror).
        if isinstance(cL, (list, tuple)):
            seg_caps = list(cL)[:len(poly) - 1]
            if len(seg_caps) < len(poly) - 1:
                pad = seg_caps[-1] if seg_caps else TAXI_MAX_GRADE
                seg_caps += [pad] * (len(poly) - 1 - len(seg_caps))
        else:
            seg_caps = [cL] * (len(poly) - 1)
        centerlines.append(GG.Centerline(pts=poly, seg_caps=seg_caps))
        # 4th element = the BUILDER's route ordinal (identity binding);
        # legacy 3-tuple sidecars fall back to nearest-route below.
        axis_ridx.append(entry[3] if len(entry) > 3 else None)

    # ANISOTROPIC EDGES (gate O4_ANISO_EDGES): chained ROUTES (meter polylines)
    # for the spine-arc decomposition, so the standalone grade TEST uses the SAME
    # anisotropic budget the solver built to (else a curve the solver arc-credited
    # would false-flag here).  Each centerline carries the builder's OWN route
    # binding when the sidecar provides it (identity, like build_context);
    # nearest-route-by-midpoint is only the legacy-sidecar fallback — it
    # mis-binds axes near junctions and the readers then bake different
    # anisotropic budgets for the same pair.  Gate OFF / no routes ⇒ ``routes``
    # empty, ``route_idx`` -1, isotropic (byte-ident).
    # index-preserving (axis ordinals are positional — see routes_m note)
    routes = [GG.RouteChain(pts=list(r)) for r in (routes_m or [])]
    if routes:
        for cl, ridx in zip(centerlines, axis_ridx):
            if ridx is not None:
                cl.route_idx = (int(ridx)
                                if 0 <= int(ridx) < len(routes) else -1)
                continue
            mx = 0.5 * (cl.pts[0][0] + cl.pts[-1][0])
            my = 0.5 * (cl.pts[0][1] + cl.pts[-1][1])
            best_i, best_d = -1, float("inf")
            for ri, r in enumerate(routes):
                _a, d, _f = GG._project(r, mx, my)
                if d < best_d:
                    best_d, best_i = d, ri
            cl.route_idx = best_i

    bld_keys = {nid for w in ways if w.tags.get("role") == "building"
                for nid in w.nids}

    rect_cap_at: dict = {}
    for w in ways:
        if w.tags.get("role") not in _SLOPING_RECT_OSM_ROLES:
            continue
        cap = _role_grade_limit(w, max_grade)
        if cap is None:
            continue
        for nid in w.nids:
            if rect_cap_at.get(nid, -1.0) < cap:
                rect_cap_at[nid] = cap

    def _inherited(shape):
        best = None
        for k in shape.keys:
            c = rect_cap_at.get(k)
            if c is not None and (best is None or c > best):
                best = c
        return best if best is not None else TAXI_MAX_GRADE

    # Taxi-ROUTE pavement zone (O4_APRON_ROUTE_CONTACT), mirroring
    # ``grade_graph.build_context``: an apron ring edge welded to a taxi-route
    # pavement is a contact ramp and earns the taxi cap in its climbing
    # direction.  This reader previously never built the zone, so it refused
    # contact budgets the solver lawfully granted (SPJC apron #188:
    # ring-adjacent 1.46 % flagged against the 1.17 % blend while the solver
    # graded the contact at 1.5 %).
    route_zone = None
    if GG.APRON_ROUTE_CONTACT:
        try:
            from shapely.geometry import Polygon as _RzPoly
            from shapely.ops import unary_union as _rz_union
            from shapely.prepared import prep as _rz_prep
            _route_polys = []
            for w in ways:
                if w.tags.get("role") not in (
                        "junction", "primary_parallel",
                        "secondary_parallel", "stub", "cross_connector"):
                    continue
                ring = [ll_to_m(*nodes[nid]) for nid in w.nids
                        if nid in nodes]
                if len(ring) >= 3:
                    _route_polys.append(_RzPoly(ring).buffer(0))
            if _route_polys:
                route_zone = _rz_prep(_rz_union(_route_polys)
                                      .buffer(GG._ROUTE_CONTACT_TOL_M))
        except Exception:
            route_zone = None

    return GG.GradeContext(
        centerlines=centerlines,
        routes=routes,
        seam_keys=frozenset(seam_nids or ()),
        inherited_junction_cap=_inherited,
        building_keys=frozenset(bld_keys),
        road_zone=road_zone,
        route_zone=route_zone,
        # EXACT-MESH sidecar: the solver's junction mesh, consumed 1:1
        # (emit-time ring repairs otherwise make this reader's Delaunay
        # differ from the one the solver graded to).
        mesh_edges_exact=(GG.MeshEdgesExact(mesh_edges_m)
                          if mesh_edges_m else None))


def iter_shape_grade_constraints(
        ways: List[Way],
        nodes: Dict[str, Tuple[float, float]],
        ll_to_m,
        max_grade: float,
        seam_nids: Optional[set] = None,
        taxi_axes: Optional[list] = None,
        routes_ll: Optional[list] = None,
        mesh_edges_m: Optional[list] = None,
        crown_by_nid: Optional[Dict[str, float]] = None,
        crown_centerline_nids: Optional[set] = None,
        pair_caps_ll: Optional[list] = None,
        ) -> "list[ShapePairConstraint]":
    """Yield every within-shape vertex-pair the grade check constrains.

    SINGLE SOURCE OF TRUTH for "which pairs are graded" — ``_check_within_shape``
    (the validator) and the feasibility oracle both consume this so the
    solver-target and the audit can never drift (W1 graph lockstep).  Encodes:
    triangle = all 3 edges; 4+ = mutually-visible pairs (apron/junction gated by
    ``_polygon_visibility``; convex rects all-pair) within the ROUTE-FIELD local
    window (ring edges always kept); per-axis junction/apron allowance with the
    cross-axis diagonal skip; seam-anchored pairs dropped (DEM controls); road-
    frontage and back-edge-ramp relaxed caps.
    """
    seam_nids = seam_nids or set()
    out: List[ShapePairConstraint] = []
    # ROAD-FRONTAGE zone (config.ROAD_FRONTAGE_TOL_M): an apron/junction
    # pair with BOTH endpoints welded to a service-road carve carries the
    # ROAD's 5 % law, not the shape's 1.5 % — the carve corners sit ON
    # the host ring, so the host's law would otherwise regulate the
    # road's own descent (CYXY road #30: the apron-ring frontage edge
    # read the road's drop as a 3.13 % apron violation; the squeeze is
    # hard-anchored, so no legal apron value exists).  VALIDATOR-ONLY:
    # the solver still solves at the strict cap (see config note).
    road_zone = None
    try:
        from shapely.geometry import Point as _FzPt, Polygon as _FzPoly
        from shapely.ops import unary_union as _fz_union
        from shapely.prepared import prep as _fz_prep
        _fz_polys = []
        for w in ways:
            if w.tags.get("role") not in _ROAD_FAMILY_ROLES:
                continue
            ring = [ll_to_m(*nodes[nid]) for nid in w.nids
                    if nid in nodes]
            if len(ring) >= 3:
                _fz_polys.append(_FzPoly(ring).buffer(0))
        if _fz_polys:
            road_zone = _fz_prep(
                _fz_union(_fz_polys).buffer(ROAD_FRONTAGE_TOL_M))
    except Exception:
        road_zone = None
    # THE LAW reader for soft airside shapes (apron / junction / service_junction):
    # build the shared grade context once and route every soft shape through
    # ``grade_graph.shape_constraints`` (→ ``grade_law.classify_pair``), so the
    # test selects the SAME within-shape pairs at the SAME base caps the solver
    # enforces.  The road-frontage / back-edge relaxations below stay a test-only
    # layer ON TOP (they only RELAX a cap).  Non-soft shapes (rects / runway /
    # terminal) keep their per-role all-pair handling further down.
    from auto_patch import grade_graph as _GG
    # INDEX-PRESERVING: axis route ordinals index this list positionally, so a
    # degenerate route must keep its slot (never filter — that shifts every
    # later ordinal).  A <2-point route becomes a 2-point degenerate chain.
    routes_m = ([([(ll_to_m(la, lo)) for (la, lo) in pts] if pts
                  and len(pts) >= 2 else
                  [ll_to_m(*pts[0]), ll_to_m(*pts[0])] if pts else
                  [(0.0, 0.0), (0.0, 0.0)])
                 for pts in routes_ll]
                if routes_ll else None)
    _law_ctx = _grade_context_from_osm(ways, nodes, ll_to_m, taxi_axes,
                                       seam_nids, max_grade, road_zone=road_zone,
                                       routes_m=routes_m,
                                       mesh_edges_m=mesh_edges_m)
    # SPINE CROWN (part 30): per-nid designed drops (sidecar field);
    # every pair's law re-centres on grade_law.crown_pair_offset.
    from auto_patch.grade_law import crown_pair_offset as _crown_off
    crown_by_nid = crown_by_nid or {}
    crown_centerline_nids = crown_centerline_nids or set()
    # BAKED PAIR CAPS (sidecar ``pair_caps``, 2026-07-17): the exact pair
    # selection + metre allowances the solver's final projection enforced
    # (``verification.lockstep_pair_caps_ll``).  When a SOFT shape has
    # coverage, constrain exactly these pairs — re-baking from the
    # emitted ring diverges: post-projection vertex inserts shorten the
    # spans (tighter anisotropic credit than was lawfully enforced) and
    # the OSM-side context can select pairs the law-side bake never did.
    _pair_cap_map: Dict[tuple, float] = {}
    for _entry in (pair_caps_ll or []):
        (_pla, _plo), (_plb, _plo2), _pcap = _entry
        _ka = (round(float(_pla), 7), round(float(_plo), 7))
        _kb = (round(float(_plb), 7), round(float(_plo2), 7))
        _pk = (min(_ka, _kb), max(_ka, _kb))
        _pcap = abs(float(_pcap))
        if _pk not in _pair_cap_map or _pcap < _pair_cap_map[_pk]:
            _pair_cap_map[_pk] = _pcap
    _SOFT_ROLES = _GG.SOFT_VISIBILITY_ROLES
    for w in ways:
        grade_cap = _role_grade_limit(w, max_grade)
        if grade_cap is None:
            continue  # skip ROLE_GRADE_LIMITS[role] is None
        pts: List[Tuple[float, float, float, bool]] = []
        pnids: List[str] = []
        for k, nid in enumerate(w.nids[:-1] if (len(w.nids) > 1
                                and w.nids[0] == w.nids[-1])
                                else w.nids):
            if nid not in nodes:
                continue
            lat, lon = nodes[nid]
            x, y = ll_to_m(lat, lon)
            e = w.elevs[k]
            if e is None:
                continue
            pts.append((x, y, e, nid in seam_nids))
            pnids.append(nid)
        n = len(pts)
        if n < 3:
            continue
        # ── SOFT airside shapes → THE LAW (one shared within-shape rule set) ──
        role0 = w.tags.get("role")
        if role0 in _SOFT_ROLES and _pair_cap_map:
            # LOCKSTEP CONSUMPTION: constrain exactly the solver-baked
            # pairs of this ring (matched by rounded lat/lon endpoint
            # keys).  Vertices absent from the bake (post-projection
            # inserts — their values interpolate along a baked edge)
            # contribute no pairs; a ring with ZERO matches falls
            # through to the re-bake path below (no bake ⇒ old law).
            _llk = [(round(nodes[pnids[k]][0], 7),
                     round(nodes[pnids[k]][1], 7)) for k in range(n)]
            _matched = []
            for _ia in range(n):
                for _ib in range(_ia + 1, n):
                    _pk = (min(_llk[_ia], _llk[_ib]),
                           max(_llk[_ia], _llk[_ib]))
                    _cap_m = _pair_cap_map.get(_pk)
                    if _cap_m is not None:
                        _matched.append((_ia, _ib, _cap_m))
            if _matched:
                for (_ia, _ib, _cap_m) in _matched:
                    xi, yi, ei, _si = pts[_ia]
                    xj, yj, ej, _sj = pts[_ib]
                    d = math.hypot(xi - xj, yi - yj)
                    if d < 0.5:
                        continue
                    # Same envelope structure as _pair_grade_allowance:
                    # the baked budget, floored at the flat cap (the
                    # pair-law MAX), plus the shape's quantization
                    # noise.
                    out.append(ShapePairConstraint(
                        way=w, nid_a=pnids[_ia], nid_b=pnids[_ib],
                        xa=xi, ya=yi, ea=ei, xb=xj, yb=yj, eb=ej,
                        dist=d, cap=grade_cap,
                        allowance=(max(_cap_m, grade_cap * d)
                                   + _pair_quant_noise_m(w)),
                        offset=_crown_off(
                            crown_by_nid.get(pnids[_ia], 0.0),
                            crown_by_nid.get(pnids[_ib], 0.0))))
                continue
        if role0 in _SOFT_ROLES:
            ring = [(p[0], p[1]) for p in pts]
            gs = _GG.GradeShape(
                role=role0, ring=ring, keys=list(pnids),
                adopts_apron_grade=(
                    w.tags.get("o4_grade_law") == "apron"),
                adopts_taxi_grade=(
                    w.tags.get("o4_grade_law") == "taxi"),
                adopted_taxi_letter=(
                    w.tags.get("code_letter")
                    if w.tags.get("o4_grade_law") == "taxi" else None))
            sc = _GG.shape_constraints(gs, _law_ctx)
            idx = {pnids[k]: k for k in range(n)}
            for (ka, kb, cap) in sc.edges:
                ia = idx.get(ka)
                ib = idx.get(kb)
                if ia is None or ib is None:
                    continue
                xi, yi, ei, _si = pts[ia]
                xj, yj, ej, _sj = pts[ib]
                d = math.hypot(xi - xj, yi - yj)
                if d < 0.5:
                    continue
                # ``cap`` comes ENTIRELY from the shared law (incl. the road-
                # frontage relaxation).  No test-only back-edge relaxation: the
                # live model (TAXI_SLACK_TERMINALS) regulates the apron strictly
                # and the back-edge-ramp model it superseded is gone — a steep
                # building-facing apron pair is a real solver failure to flag.
                out.append(ShapePairConstraint(
                    way=w, nid_a=pnids[ia], nid_b=pnids[ib],
                    xa=xi, ya=yi, ea=ei, xb=xj, yb=yj, eb=ej,
                    dist=d, cap=cap.flat_cap(),
                    allowance=_pair_grade_allowance(cap, d, w),
                    offset=_crown_off(crown_by_nid.get(pnids[ia], 0.0),
                                      crown_by_nid.get(pnids[ib], 0.0))))
            continue
        # PLANE shapes (rects / runway / terminal) → the SAME law: all vertex
        # pairs at the role cap, via grade_graph.plane_constraints (the single
        # rule source for every shape).  classify_pair owns the seam skip, the
        # min-pair-distance and the road-carve relaxation, so the old per-axis /
        # triangle / visibility branches here are gone.
        # ``o4_single_poly`` marks a DE-SEGMENTED runway ring so
        # ``plane_constraints`` scopes its within-shape pairs to LATERAL +
        # same/adjacent-station (user ruling 2026-07-08); a segmented rect
        # carries no marker and keeps its full all-pair check.
        gs = _GG.GradeShape(role=role0, ring=[(p[0], p[1]) for p in pts],
                            keys=list(pnids),
                            single_poly=(w.tags.get("o4_single_poly") == "1"))
        sc = _GG.plane_constraints(gs, _law_ctx, grade_cap)
        idx = {pnids[k]: k for k in range(n)}
        _is_runway = role0 in ("runway", "runway_crossing")
        for (ka, kb, capp) in sc.edges:
            ia = idx.get(ka)
            ib = idx.get(kb)
            if ia is None or ib is None:
                continue
            # CROWN CENTERLINE skip (Phase 0 hotfix): a runway pair touching a
            # crown-centerline node lies on the ridge — its longitudinal grade
            # is the SPINE PROFILE check's domain and its lateral crown is
            # sub-cap by design; a cross-station diagonal to it conflates the
            # two.  Same exemption class as the crown_spine breakline.
            if _is_runway and (ka in crown_centerline_nids
                               or kb in crown_centerline_nids):
                continue
            xi, yi, ei, _sa = pts[ia]
            xj, yj, ej, _sb = pts[ib]
            d = math.hypot(xi - xj, yi - yj)
            out.append(ShapePairConstraint(
                way=w, nid_a=pnids[ia], nid_b=pnids[ib],
                xa=xi, ya=yi, ea=ei, xb=xj, yb=yj, eb=ej,
                dist=d, cap=capp.flat_cap(),
                allowance=_pair_grade_allowance(capp, d, w),
                offset=_crown_off(crown_by_nid.get(pnids[ia], 0.0),
                                  crown_by_nid.get(pnids[ib], 0.0))))
    return out


def _check_runway_end_skirt_edges(ways: List[Way],
                                  nodes: Dict[str, Tuple[float, float]],
                                  ll_to_m) -> List[Violation]:
    """DEM-free skirt-law reader on the EMITTED patch: every ring edge
    of a ``runway_end_skirt`` way must stay within the law's maximum
    down-grade (``grade_law.RUNWAY_END_SKIRT_MAX_DOWN_GRADE``).  By
    construction the skirt surface never exceeds it in ANY direction —
    band rows are level and the descent is rate-limited — so a steeper
    emitted edge means the patch was corrupted after emission.  The
    full DEM-aware floor/curvature law lives in
    ``verification.check_runway_end_skirt``; this is the always-on
    lockstep reader for emitted patches."""
    from auto_patch.grade_law import RUNWAY_END_SKIRT_MAX_DOWN_GRADE
    # Skirt altitudes emit at 0.1 m quantization — a pair carries up to
    # ~0.1 m of rounding; padded slightly for float noise.
    skirt_edge_noise_m = 0.15
    # LIFT-ONLY tolerance (2026-07-07): the skirt surface is
    # ``max(analytic_floor, DEM)`` per vertex (clearance._skirt_lift_alt
    # — the fill-only / flat-shadow convention: a fill never cuts).  A
    # band that TRIGGERS on falling terrain can still SPAN a DEM bump
    # that sits above the floor; that vertex lifts to the DEM instead of
    # being graded DOWN to the floor.  So a band row is no longer level,
    # and the surface can descend from a lifted bump back to the floor
    # FASTER than the down-grade cap — this is LAWFUL (the law bounds how
    # far BELOW the floor the surface may drop, not how it rides an
    # existing bump back up; a vertex at/above the floor descending no
    # lower than the floor is compliant).  DEM-free we cannot read the
    # floor, but we CAN tell the two apart by SHAPE: a lift raises a
    # vertex ABOVE its ring neighbours (a peak); post-emission corruption
    # DROPS a vertex below its neighbours (a valley).  So an over-steep
    # edge is flagged ONLY when its HIGHER endpoint is NOT a local lift
    # peak — i.e. the steepness comes from the lower vertex dropping, the
    # one signature ``max(floor, DEM)`` can never produce.  The DEM-aware
    # floor/curvature law (verification.check_runway_end_skirt) remains
    # the full below-floor check.

    def _ring_neighbour_elevs(w, i):
        """Elevations of the two ring-adjacent vertices of index ``i``
        on the closed way ``w`` (its ring's last nid repeats the first)."""
        m = len(w.nids) - 1  # distinct vertices (ring closes)
        if m < 2:
            return []
        elevs = []
        for j in (i - 1, i + 1):
            k = j % m
            e = w.elevs[k] if k < len(w.elevs) else None
            if e is not None:
                elevs.append(float(e))
        return elevs

    out: List[Violation] = []
    for w in ways:
        if w.ref != "runway_end_skirt":
            continue
        ring = w.nids
        for i in range(len(ring) - 1):
            nid_a, nid_b = ring[i], ring[i + 1]
            if nid_a not in nodes or nid_b not in nodes:
                continue
            ea = w.elevs[i] if i < len(w.elevs) else None
            eb = w.elevs[i + 1] if i + 1 < len(w.elevs) else None
            if ea is None or eb is None:
                continue
            xa, ya = ll_to_m(*nodes[nid_a])
            xb, yb = ll_to_m(*nodes[nid_b])
            dist = math.hypot(xb - xa, yb - ya)
            if dist < 0.5:
                continue
            de = abs(float(ea) - float(eb))
            allowance = (RUNWAY_END_SKIRT_MAX_DOWN_GRADE * dist
                         + skirt_edge_noise_m)
            if de <= allowance:
                continue
            # Lift check: if the HIGHER endpoint is strictly above BOTH
            # its ring neighbours, it is a lifted DEM bump and the steep
            # descent off it is lawful (max(floor, DEM) riding terrain).
            hi_idx = i if float(ea) >= float(eb) else i + 1
            hi_elev = max(float(ea), float(eb))
            neigh = _ring_neighbour_elevs(w, hi_idx)
            if neigh and all(hi_elev > ne + skirt_edge_noise_m
                             for ne in neigh):
                continue
            grade = de / dist
            out.append(Violation(
                grade_pct=grade * 100,
                excess_pct=(grade - RUNWAY_END_SKIRT_MAX_DOWN_GRADE) * 100,
                distance_m=dist,
                de_m=de,
                way_a=w, way_b=w,
                pt_a=(xa, ya), pt_b=(xb, yb),
                elev_a=float(ea), elev_b=float(eb)))
    out.sort(key=lambda v: -v.excess_pct)
    return out


def _check_adjacent_ground_edges(ways: List[Way],
                                 nodes: Dict[str, Tuple[float, float]],
                                 ll_to_m) -> List[Violation]:
    """DEM-free TEAR sentinel on the EMITTED patch: an ``adjacent_ground``
    graded-strip must not carry a near-vertical SUB-METRE edge (a clip /
    weld discontinuity — the epsilon-wedge corruption class).

    Why not a corridor-grade check (the skirt's approach).  The runway-end
    skirt is a fill-ONLY ramp with a single bounded down-grade, so its
    OSM-side reader can flag any over-steep edge that is not a lawful DEM
    lift-peak (``_check_runway_end_skirt_edges``).  A LATERAL band is a
    two-directional corridor whose FILL rides ``max(floor, DEM)`` and whose
    CUT rides the ceiling: it lawfully follows terrain UP a bump (a peak)
    AND down a pit (a valley) AND along a monotonic hillside — DEM-free NONE
    of those are distinguishable from a real over-steep edge, so a
    corridor-grade check false-flags every terrain-riding band edge
    (measured: ~200-1100 at CYXY/HECA, nearly all lawful).  The DEM-aware
    corridor law — coverage + floor/ceiling — is
    ``verification.check_adjacent_ground`` (the authoritative reader); the
    ONE thing a DEM-free reader CAN prove wrong here is a TEAR: a band edge
    far shorter than the emitter's station step carrying an altitude jump no
    lawful graded slope produces, i.e. a vertical face the clip/weld
    introduced.  Real band edges span ~one station step (5 m) or the
    pavement gap (~1 m); a sub-gap edge with a metre-plus jump is a tear.

    Returns ``Violation`` rows (``grade_pct`` = the tear's near-vertical
    grade), worst-jump first — the LATERAL twin of
    ``_check_runway_end_skirt_edges``, narrowed to what DEM-free is sound."""
    from auto_patch.config import CLEARANCE_STATION_STEP_M
    # A lawful band edge spans ~a station step; anything under a fifth of it
    # is a clip sliver, not a graded row.  A jump exceeding this over so
    # short a span is a vertical face (> ~100 % grade) — no lawful ≤5 %
    # corridor slope reaches it.
    tear_max_edge_m = 0.2 * CLEARANCE_STATION_STEP_M      # 1.0 m
    tear_min_jump_m = 1.0

    # WALL-SPANNED EXEMPTION (no-stacked-nodes unit, 2026-07-19): a
    # strip edge whose BOTH endpoints a ``retaining_wall`` way
    # references is the side of a deliberate wall wedge (the strip
    # retreat's end taper) — the face fills the jump; it is not a bare
    # clip tear.  Same reading as the strip-SEAM check's exemption.
    wall_nid_ways: Dict[str, set] = defaultdict(set)
    for wi, w in enumerate(ways):
        if w.tags.get("role") == "retaining_wall":
            for nid in w.nids:
                wall_nid_ways[nid].add(wi)

    out: List[Violation] = []
    for w in ways:
        if w.ref != "adjacent_ground":
            continue
        ring = w.nids
        for i in range(len(ring) - 1):
            nid_a, nid_b = ring[i], ring[i + 1]
            if nid_a not in nodes or nid_b not in nodes:
                continue
            ea = w.elevs[i] if i < len(w.elevs) else None
            eb = w.elevs[i + 1] if i + 1 < len(w.elevs) else None
            if ea is None or eb is None:
                continue
            xa, ya = ll_to_m(*nodes[nid_a])
            xb, yb = ll_to_m(*nodes[nid_b])
            dist = math.hypot(xb - xa, yb - ya)
            de = abs(float(ea) - float(eb))
            if not (dist < tear_max_edge_m and de > tear_min_jump_m):
                continue
            if wall_nid_ways.get(nid_a, set()) & wall_nid_ways.get(
                    nid_b, set()):
                continue  # side of a deliberate retaining_wall wedge
            grade = de / dist if dist > 1e-9 else float("inf")
            out.append(Violation(
                grade_pct=grade * 100,
                excess_pct=grade * 100,
                distance_m=dist,
                de_m=de,
                way_a=w, way_b=w,
                pt_a=(xa, ya), pt_b=(xb, yb),
                elev_a=float(ea), elev_b=float(eb)))
    out.sort(key=lambda v: -v.de_m)
    return out


# ── Cross-shape graded-strip SEAM tear thresholds ───────────────
# A ``graded_strip`` drapes raw terrain and legitimately has NO
# within-shape grade cap (``_check_adjacent_ground_edges`` above only
# proves the SUB-METRE within-shape tear).  The one DEM-free-provable
# defect that class misses is a large vertical STEP between the nodes of
# two DIFFERENT strips: a clip / weld seam the in-sim renderer draws as a
# sharp cliff.  Thresholds chosen from the SPJC inventory, where real
# seam tears are Δalt 1.8-4.4 m at 1-6 m node spacing — safely above the
# ~0.3 m steps lawful terracing between adjacent strips produces.
STRIP_SEAM_TEAR_RADIUS_M = 6.0         # only NEAR-adjacent strip nodes pair
STRIP_SEAM_TEAR_MIN_STEP_M = 1.0       # Δalt at/under this = lawful terrace / noise
# Grade floor: steep-relief airports (CYXY) hold LAWFUL >1 m deltas between
# strips 4-6 m apart (hillside drape, ~30-40 % max); genuine seam cliffs and
# stacked same-coordinate walls run 100-350 %.  Only steps implying >50 %
# are tears.  Exactly-interned shared nodes carry ONE value (Δ = 0), so no
# planar-distance floor is needed — a same-coordinate pair with Δalt > the
# step floor is a stacked bare wall and MUST be flagged.
STRIP_SEAM_TEAR_MIN_GRADE = 0.5
STRIP_SEAM_TEAR_MIN_DISTANCE_M = 0.01  # grade denominator clamp (stacked walls)
STRIP_SEAM_ROLE = "graded_strip"


def _check_strip_seam_tears(
    vertices: List[Vertex],
    ways: List[Way],
    radius_m: float = STRIP_SEAM_TEAR_RADIUS_M,
    min_step_m: float = STRIP_SEAM_TEAR_MIN_STEP_M,
    min_distance_m: float = STRIP_SEAM_TEAR_MIN_DISTANCE_M,
) -> List[Violation]:
    """DEM-free SEAM-tear sentinel BETWEEN two different ``graded_strip``
    shapes — the cross-shape twin of ``_check_adjacent_ground_edges``.

    A single strip drapes terrain and carries no lawful grade cap, so a
    steep edge WITHIN one strip is not provably wrong DEM-free.  But two
    distinct strips that meet along a seam must AGREE in elevation where
    their nodes fall near-adjacent: a metre-plus altitude step across a
    sub-``radius_m`` gap is a clip / weld discontinuity (the in-sim "sharp
    cliff"), which no lawful terrain drape produces (real terracing steps
    between neighbouring strips stay ~0.3 m — see the module constants).

    A pair qualifies when the two nodes belong to DIFFERENT strip ways
    (same-way pairs are the within-shape check's business), their planar
    distance is under ``radius_m``, their absolute altitude difference is
    over ``min_step_m``, AND the implied grade exceeds
    ``STRIP_SEAM_TEAR_MIN_GRADE`` (steep-relief airports hold lawful
    metre-plus deltas between strips several metres apart — hillside
    drape — while true seam cliffs run far past 50 %; an exactly-interned
    shared node carries one value, so a same-coordinate pair with a real
    delta is a stacked bare WALL and is flagged).  Ways that carry the same
    ``shapeID`` are treated as one shape (a strip emitted as several ways)
    and skipped.

    WALL-SPANNED EXEMPTION (no-stacked-nodes unit, 2026-07-19): a level
    change between two strips rendered as DELIBERATE wall geometry — a
    ``retaining_wall`` way referencing BOTH endpoints (its top row welds
    the upper strip's chain, its bottom row the retreated lower strip)
    — is the ruling's sanctioned form, not a bare tear; the face fills
    the gap the bare-seam reading assumes empty.

    Runs in ~linear time via a spatial grid over the strip nodes (never an
    O(n^2) all-pairs scan — airports reach ~50k strip nodes).  Returns
    ``Violation`` rows (``grade_pct`` = the seam's near-vertical grade),
    worst-step first."""
    # Restrict to strip vertices carrying a known elevation, keeping a map
    # back to the global vertex index only for readability of intent.
    strip_vertices: List[Vertex] = [
        v for v in vertices
        if v.elev is not None
        and ways[v.way_idx].tags.get("role") == STRIP_SEAM_ROLE
    ]
    if len(strip_vertices) < 2:
        return []
    # Wall-vertex registry for the wall-spanned exemption: coordinate
    # key -> set of retaining_wall way indices referencing it.  Wall
    # rows are emitted at the exact canonical coordinates of the chains
    # they weld, so exact (cm-rounded) matching suffices.
    wall_keys: Dict[Tuple[int, int], set] = defaultdict(set)
    for v in vertices:
        if ways[v.way_idx].tags.get("role") == "retaining_wall":
            wall_keys[(round(v.x * 100), round(v.y * 100))].add(v.way_idx)

    def _wall_spans(a: Vertex, b: Vertex) -> bool:
        wa = wall_keys.get((round(a.x * 100), round(a.y * 100)))
        if not wa:
            return False
        wb = wall_keys.get((round(b.x * 100), round(b.y * 100)))
        return bool(wb) and bool(wa & wb)

    cell = max(radius_m, 0.5)
    grid = _bucket_vertices(strip_vertices, cell)
    out: List[Violation] = []
    for v_local, v in enumerate(strip_vertices):
        cx = int(math.floor(v.x / cell))
        cy = int(math.floor(v.y / cell))
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                bucket = grid.get((cx + dcx, cy + dcy))
                if not bucket:
                    continue
                for u_local in bucket:
                    if u_local <= v_local:
                        continue  # each unordered pair considered once
                    u = strip_vertices[u_local]
                    if u.way_idx == v.way_idx:
                        continue  # same strip — within-shape check owns it
                    way_v = ways[v.way_idx]
                    way_u = ways[u.way_idx]
                    shape_v = way_v.tags.get("shapeID")
                    shape_u = way_u.tags.get("shapeID")
                    if shape_v is not None and shape_v == shape_u:
                        continue  # one strip emitted as several ways
                    d = math.hypot(v.x - u.x, v.y - u.y)
                    if d >= radius_m:
                        continue  # too far apart to be a seam
                    de = abs(v.elev - u.elev)
                    if de <= min_step_m:
                        continue  # lawful terrace step / rounding noise
                    grade = de / max(d, min_distance_m)
                    if grade < STRIP_SEAM_TEAR_MIN_GRADE:
                        continue  # steep-terrain drape, not a cliff
                    if _wall_spans(v, u):
                        continue  # deliberate retaining_wall face
                    out.append(Violation(
                        grade_pct=grade * 100,
                        excess_pct=grade * 100,
                        distance_m=d,
                        de_m=de,
                        way_a=way_v, way_b=way_u,
                        pt_a=(v.x, v.y), pt_b=(u.x, u.y),
                        elev_a=v.elev, elev_b=u.elev))
    out.sort(key=lambda v: -v.de_m)
    return out


# ── Stacked-node invariant ──────────────────────────────────────
# OWNER RULING 2026-07-19: nodes can NEVER be stacked — two distinct
# OSM node ids at the same coordinate are illegal regardless of their
# elevations ("if they are in the same spot they must be merged and
# share the same elevation").  A genuine level change must be emitted
# as HORIZONTAL wall geometry (two node columns offset in plan — the
# retaining_wall machinery), never as coincident nodes with different
# elevations (those render as bare near-vertical mesh tears: the CYXY
# d=0.00 audit pairs, the SPLP seam site).  Cap is ZERO.
#
# The emitter's canonical-point registry spaces distinct points by its
# 0.5 m proximity tolerance, and stacked twins are emitted at the
# IDENTICAL canonical lat/lon — so anything under this radius is "the
# same spot" with wide margin, not a near-adjacent pair (those belong
# to the proximity / seam checks).
STACKED_NODE_XY_TOL_M = 0.05
# Two nodes at one spot violate the invariant when their ELEVATIONS
# differ — "they must be merged and share the same elevation".  A
# same-value coordinate twin is a legal OSM-encoding artifact (an OSM
# ring cannot reference one node id twice, so a figure-8 revisit emits
# a twin carrying identical claims; the mesh welds by coordinates into
# ONE vertex with one elevation — no tear exists).  Emitted altitudes
# are rounded to 0.1 m, so anything above this noise floor is a real
# disagreement.
STACKED_NODE_ALT_TOL_M = 0.05


def _check_stacked_nodes(
    vertices: List[Vertex],
    ways: List[Way],
    xy_tol_m: float = STACKED_NODE_XY_TOL_M,
    alt_tol_m: float = STACKED_NODE_ALT_TOL_M,
) -> List[Violation]:
    """STRUCTURAL stacked-node detector (owner invariant 2026-07-19).

    Flags every pair of DISTINCT node ids lying within ``xy_tol_m`` of
    each other whose claimed elevations disagree by more than
    ``alt_tol_m`` — one Violation row per node pair.  Same-value
    coordinate twins (the figure-8 OSM-encoding artifact) and
    elevation-less pairs (both drape onto the DEM — one surface) are
    NOT violations: the invariant is about the rendered surface, and
    those weld into one mesh vertex.  Works identically on test-DEM
    and production-DEM builds: the invariant is structural, so it
    needs no per-airport calibration and no DEM.

    Linear-time via the shared spatial grid; never an all-pairs scan.
    """
    # One entry per node id: representative coordinate, every elevation
    # claimed for it across referencing ways, a representative way.
    by_nid: Dict[str, list] = {}
    for v in vertices:
        entry = by_nid.get(v.nid)
        if entry is None:
            by_nid[v.nid] = [v.x, v.y, [] if v.elev is None else [v.elev],
                             v.way_idx]
        elif v.elev is not None:
            entry[2].append(v.elev)
    entries = [(nid, e[0], e[1], e[2], e[3])
               for nid, e in by_nid.items()]
    if len(entries) < 2:
        return []
    cell = 0.5
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, (_nid, x, y, _elevs, _w) in enumerate(entries):
        grid[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(i)
    out: List[Violation] = []
    for i, (nid_a, xa, ya, elevs_a, way_a) in enumerate(entries):
        cx = int(math.floor(xa / cell))
        cy = int(math.floor(ya / cell))
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                for j in grid.get((cx + dcx, cy + dcy), ()):
                    if j <= i:
                        continue  # each unordered pair once
                    (nid_b, xb, yb, elevs_b, way_b) = entries[j]
                    d = math.hypot(xa - xb, ya - yb)
                    if d > xy_tol_m:
                        continue
                    if not elevs_a or not elevs_b:
                        continue  # both drape the DEM — one surface
                    elev_a = sum(elevs_a) / len(elevs_a)
                    elev_b = sum(elevs_b) / len(elevs_b)
                    de = abs(elev_a - elev_b)
                    if de <= alt_tol_m:
                        continue  # same-value encoding twin — merged in mesh
                    grade = de / max(d, 0.01)
                    out.append(Violation(
                        grade_pct=grade * 100,
                        excess_pct=grade * 100,
                        distance_m=d,
                        de_m=de,
                        way_a=ways[way_a], way_b=ways[way_b],
                        pt_a=(xa, ya), pt_b=(xb, yb),
                        elev_a=elev_a, elev_b=elev_b))
    out.sort(key=lambda v: -v.de_m)
    return out


def _check_within_shape(ways: List[Way],
                        nodes: Dict[str, Tuple[float, float]],
                        ll_to_m,
                        max_grade: float,
                        seam_nids: Optional[set] = None,
                        taxi_axes: Optional[list] = None,
                        routes_ll: Optional[list] = None,
                        mesh_edges_m: Optional[list] = None,
                        crown_by_nid: Optional[Dict[str, float]] = None,
                        crown_centerline_nids: Optional[set] = None,
                        pair_caps_ll: Optional[list] = None,
                        ) -> List[Violation]:
    """Grade check between vertex pairs on the same way.  Consumes
    ``iter_shape_grade_constraints`` (the single source of constrained pairs)
    and flags any pair whose stored Δelev exceeds its allowance — a violation
    requires ``|de − crown_offset| > max(baked, cap*dist) + quant_noise`` (see
    ``_pair_grade_allowance`` — the flat-cap floor mirrors the plane-gradient
    law's ``cap*dist + noise``, and ``quant_noise`` is the shape's emit/weld
    envelope; crown offset is 0 for uncrowned pairs) so emit rounding and
    weld-insert micro-steps don't produce spurious sub-metre flags."""
    out: List[Violation] = []
    for c in iter_shape_grade_constraints(
            ways, nodes, ll_to_m, max_grade, seam_nids, taxi_axes, routes_ll,
            mesh_edges_m=mesh_edges_m, crown_by_nid=crown_by_nid,
            crown_centerline_nids=crown_centerline_nids,
            pair_caps_ll=pair_caps_ll):
        de = abs((c.ea - c.eb) - c.offset)
        if de <= c.allowance:
            continue
        grade = de / c.dist
        out.append(Violation(
            grade_pct=grade * 100,
            excess_pct=(grade - c.cap) * 100,
            distance_m=c.dist,
            de_m=de,
            way_a=c.way, way_b=c.way,
            pt_a=(c.xa, c.ya), pt_b=(c.xb, c.yb),
            elev_a=c.ea, elev_b=c.eb))
    return out


# Roles excluded from the route-band check: anchors themselves (runway /
# runway-interpolated crossings) and groundside surfaces (wall-separated from
# the airside network by design — they have no taxi route to a runway).
_ROUTE_BAND_SKIP_ROLES = {"runway", "runway_crossing"}


SHARED_NID_TOLERANCE_M = 0.15  # rounding-precision step at a
                                # shared OSM node (1-decimal elev)


def _check_cross_shape_proximity(
    vertices: List[Vertex],
    ways: List[Way],
    proximity_m: float,
    max_grade: float,
) -> List[Violation]:
    """For every pair of vertices on DIFFERENT ways within
    ``proximity_m`` of each other, verify ``|de| / dist <= grade``.
    For sub-metre distances this is essentially "shared corners
    must agree on elevation".

    When two ways reference the SAME OSM node id, the vertices are
    geometrically identical — any non-zero elevation difference is
    a desync (we tolerate up to SHARED_NID_TOLERANCE_M for one-
    decimal rounding noise, then flag).
    """
    out: List[Violation] = []
    cell = max(proximity_m, 0.5)
    grid = _bucket_vertices(vertices, cell)
    for v_idx, v in enumerate(vertices):
        if v.elev is None:
            continue
        cx = int(math.floor(v.x / cell))
        cy = int(math.floor(v.y / cell))
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                bucket = grid.get((cx + dcx, cy + dcy))
                if not bucket:
                    continue
                for u_idx in bucket:
                    if u_idx <= v_idx:
                        continue
                    u = vertices[u_idx]
                    if u.way_idx == v.way_idx:
                        continue
                    if u.elev is None:
                        continue
                    d = math.hypot(v.x - u.x, v.y - u.y)
                    if d > proximity_m:
                        continue
                    way_v = ways[v.way_idx]
                    way_u = ways[u.way_idx]
                    # Airside <-> groundside is separated by a clearance gap +
                    # retaining/vertical wall (user 2026-05-28): the two are NOT
                    # meant to be flush and may differ by several metres.  The
                    # STEP checks already skip this boundary; the cross-shape
                    # proximity check (same continuity assumption) must too.
                    if _airside_groundside_pair(way_v, way_u):
                        continue
                    grade_cap = _pair_grade_limit(
                        way_v, way_u, max_grade)
                    if grade_cap is None:
                        continue
                    de = abs(v.elev - u.elev)
                    # Same OSM node referenced by two ways: the
                    # only valid step is rounding noise.  Don't
                    # apply the grade rule (denominator is zero).
                    if v.nid == u.nid or d < 0.05:
                        if de <= SHARED_NID_TOLERANCE_M:
                            continue
                        out.append(Violation(
                            grade_pct=float("inf"),
                            excess_pct=float("inf"),
                            distance_m=d,
                            de_m=de,
                            way_a=way_v,
                            way_b=way_u,
                            pt_a=(v.x, v.y), pt_b=(u.x, u.y),
                            elev_a=v.elev, elev_b=u.elev))
                        continue
                    allowance = grade_cap * d + ELEV_ROUNDING_NOISE_M
                    if de <= allowance:
                        continue
                    grade = de / d
                    out.append(Violation(
                        grade_pct=grade * 100,
                        excess_pct=(grade - grade_cap) * 100,
                        distance_m=d,
                        de_m=de,
                        way_a=way_v,
                        way_b=way_u,
                        pt_a=(v.x, v.y), pt_b=(u.x, u.y),
                        elev_a=v.elev, elev_b=u.elev))
    return out


def _check_vertex_to_edge_step(
    vertices: List[Vertex],
    edges: List[Edge],
    ways: List[Way],
    edge_search_m: float,
    edge_step_m: float,
    contact_tol_m: Optional[float] = None,
    pair_ok=None,
) -> List[EdgeStep]:
    """For each vertex, find the closest edge of ANY OTHER way
    within ``edge_search_m``.  Project the vertex onto the edge,
    compute interpolated elevation along the edge at that point,
    and report a violation if the vertex's own elevation differs
    by more than ``edge_step_m``.

    ``contact_tol_m`` (default ``_STEP_CONTACT_TOL_M``): a vertex is
    treated as touching the edge only when its perpendicular distance is
    within this bound — beyond it the two shapes are gapped and a height
    difference is allowed.  ``pair_ok(way_v, way_e) -> bool`` (default
    None = allow all): an extra predicate to restrict the pairs checked
    (e.g. the airside-only mid-edge gate); the standard role/groundside
    skips still apply on top of it."""
    ctol = _STEP_CONTACT_TOL_M if contact_tol_m is None else contact_tol_m
    out: List[EdgeStep] = []
    cell = max(edge_search_m, 1.0)
    edge_grid = _bucket_edges(edges, cell)
    for v in vertices:
        if v.elev is None:
            continue
        way_v = ways[v.way_idx]
        if _role_grade_limit(way_v, 1.0) is None:
            continue  # vertex's role is on the skip-list
        cx = int(math.floor(v.x / cell))
        cy = int(math.floor(v.y / cell))
        best_d2 = edge_search_m * edge_search_m
        best: Optional[Tuple[Edge, float, float, float]] = None
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                bucket = edge_grid.get((cx + dcx, cy + dcy))
                if not bucket:
                    continue
                for e_idx in bucket:
                    e = edges[e_idx]
                    if e.way_idx == v.way_idx:
                        continue
                    way_e = ways[e.way_idx]
                    if _role_grade_limit(way_e, 1.0) is None:
                        continue  # edge's role is on the skip-list
                    if _airside_groundside_pair(way_v, way_e):
                        continue  # wall-separated boundary — step by design
                    if pair_ok is not None and not pair_ok(way_v, way_e):
                        continue  # caller's pair restriction (airside gate)
                    ax, ay = e.a
                    bx, by = e.b
                    dx = bx - ax
                    dy = by - ay
                    seg2 = dx * dx + dy * dy
                    if seg2 < 0.04:
                        continue
                    t = ((v.x - ax) * dx + (v.y - ay) * dy) / seg2
                    if t < 0.0:
                        t = 0.0
                    elif t > 1.0:
                        t = 1.0
                    px = ax + t * dx
                    py = ay + t * dy
                    d2 = (v.x - px) * (v.x - px) + (v.y - py) * (v.y - py)
                    if d2 < best_d2:
                        best_d2 = d2
                        best = (e, t, px, py)
        if best is None:
            continue
        if best_d2 > ctol * ctol:
            continue  # gap, not a shared edge — height difference allowed
        e, t, px, py = best
        e_proj = e.ea + t * (e.eb - e.ea)
        step = abs(v.elev - e_proj)
        if step > edge_step_m + 1e-5:
            out.append(EdgeStep(
                step_m=step,
                distance_m=math.sqrt(best_d2),
                way_v=ways[v.way_idx],
                way_e=ways[e.way_idx],
                vert_pt=(v.x, v.y),
                proj_pt=(px, py),
                elev_v=v.elev,
                elev_proj=e_proj))
    return out


def _check_edge_midpoint_step(
    edges: List[Edge],
    ways: List[Way],
    edge_search_m: float,
    edge_step_m: float,
    samples_per_edge: int = 5,
    contact_tol_m: Optional[float] = None,
    pair_ok=None,
) -> List[EdgeStep]:
    """For every edge, sample at ``samples_per_edge`` points
    (including the midpoint), compute the edge's interpolated
    elevation at that sample, then find the closest edge of any
    OTHER way and compare its interpolated elevation at the
    projected point.

    This catches the "two parallel edges drift apart in elevation"
    case that vertex-only checks miss: endpoints may agree but a
    mid-edge sample can still have a visible step if the two
    edges aren't exactly coincident (e.g. a junction edge running
    0.3 m alongside a sloped rect's long edge, with the junction's
    other endpoint dragging the midpoint elevation off the rect's
    slope at that point).

    ``contact_tol_m`` / ``pair_ok`` mirror ``_check_vertex_to_edge_step``:
    override the touch tolerance and restrict the checked pairs (the
    airside-only mid-edge gate uses a wider tolerance so a wedge whose
    steep edge runs ~1 m from the neighbour is still caught).
    """
    ctol = _STEP_CONTACT_TOL_M if contact_tol_m is None else contact_tol_m
    out: List[EdgeStep] = []
    cell = max(edge_search_m, 1.0)
    edge_grid = _bucket_edges(edges, cell)
    for e1 in edges:
        way_e1 = ways[e1.way_idx]
        if _role_grade_limit(way_e1, 1.0) is None:
            continue  # this edge's role is on the skip-list
        ax, ay = e1.a
        bx, by = e1.b
        dx = bx - ax
        dy = by - ay
        seg_len = math.hypot(dx, dy)
        if seg_len < 1.0:
            continue
        # Sample at interior t = 1/(N+1), 2/(N+1), ... N/(N+1).
        for k in range(1, samples_per_edge + 1):
            t = k / (samples_per_edge + 1)
            sx = ax + t * dx
            sy = ay + t * dy
            s_elev = e1.ea + t * (e1.eb - e1.ea)
            # Find closest OTHER-way edge to this sample.
            cx = int(math.floor(sx / cell))
            cy = int(math.floor(sy / cell))
            best_d2 = edge_search_m * edge_search_m
            best: Optional[Tuple[Edge, float, float, float]] = None
            for dcx in (-1, 0, 1):
                for dcy in (-1, 0, 1):
                    bucket = edge_grid.get((cx + dcx, cy + dcy))
                    if not bucket:
                        continue
                    for e2_idx in bucket:
                        e2 = edges[e2_idx]
                        if e2.way_idx == e1.way_idx:
                            continue
                        way_e2 = ways[e2.way_idx]
                        if _role_grade_limit(way_e2, 1.0) is None:
                            continue  # other edge's role on skip-list
                        if _airside_groundside_pair(way_e1, way_e2):
                            continue  # wall-separated boundary — step by design
                        if pair_ok is not None and not pair_ok(way_e1, way_e2):
                            continue  # caller's pair restriction (airside gate)
                        e2ax, e2ay = e2.a
                        e2bx, e2by = e2.b
                        e2dx = e2bx - e2ax
                        e2dy = e2by - e2ay
                        e2seg2 = e2dx * e2dx + e2dy * e2dy
                        if e2seg2 < 0.04:
                            continue
                        tt = ((sx - e2ax) * e2dx
                              + (sy - e2ay) * e2dy) / e2seg2
                        if tt < 0.0:
                            tt = 0.0
                        elif tt > 1.0:
                            tt = 1.0
                        px = e2ax + tt * e2dx
                        py = e2ay + tt * e2dy
                        d2 = (sx - px) * (sx - px) + (sy - py) * (sy - py)
                        if d2 < best_d2:
                            best_d2 = d2
                            best = (e2, tt, px, py)
            if best is None:
                continue
            if best_d2 > ctol * ctol:
                continue  # gap, not a shared edge — height difference allowed
            e2, tt, px, py = best
            e2_elev = e2.ea + tt * (e2.eb - e2.ea)
            step = abs(s_elev - e2_elev)
            if step > edge_step_m + 1e-5:
                out.append(EdgeStep(
                    step_m=step,
                    distance_m=math.sqrt(best_d2),
                    way_v=ways[e1.way_idx],
                    way_e=ways[e2.way_idx],
                    vert_pt=(sx, sy),
                    proj_pt=(px, py),
                    elev_v=s_elev,
                    elev_proj=e2_elev))
    return out


# ── Reporting ───────────────────────────────────────────────────

def _label(w: Way) -> str:
    base = f"{w.role or '?'}/{w.ref or w.wid}"
    sid = w.tags.get("shapeID")
    return f"{base} [#{sid}]" if sid else base


def _print_violations(title: str, vios: List[Violation], top_n: int):
    n = len(vios)
    print(f"\n{title}: {n} violation{'s' if n != 1 else ''}")
    if not vios:
        return
    vios.sort(key=lambda v: -v.grade_pct)
    print(f"  worst {min(top_n, n)}:")
    for v in vios[:top_n]:
        print(f"    {v.grade_pct:6.2f}% (excess {v.excess_pct:+5.2f}%) "
              f"d={v.distance_m:6.2f}m |de|={v.de_m:5.2f}m  "
              f"{_label(v.way_a)} ({v.elev_a:.1f}) -> "
              f"{_label(v.way_b)} ({v.elev_b:.1f})")
    # Bucket distribution by excess.
    band_caps = [0.5, 1.0, 2.0, 5.0]
    band_counts = [0] * (len(band_caps) + 1)
    for v in vios:
        for bi, cap in enumerate(band_caps):
            if v.excess_pct < cap:
                band_counts[bi] += 1
                break
        else:
            band_counts[-1] += 1
    print("  excess distribution:")
    last = 0.0
    for bi, cap in enumerate(band_caps):
        print(f"    {last:.1f} → {cap:.1f}% over: {band_counts[bi]}")
        last = cap
    print(f"    {last:.1f}% → ∞ over: {band_counts[-1]}")


def _print_steps(title: str, steps: List[EdgeStep], top_n: int,
                 step_threshold_m: float):
    n = len(steps)
    print(f"\n{title}: {n} step{'s' if n != 1 else ''} > {step_threshold_m}m")
    if not steps:
        return
    steps.sort(key=lambda s: -s.step_m)
    print(f"  worst {min(top_n, n)}:")
    for s in steps[:top_n]:
        print(f"    step={s.step_m:5.2f}m  d={s.distance_m:5.2f}m  "
              f"vert={_label(s.way_v)} ({s.elev_v:.1f}) -> "
              f"edge={_label(s.way_e)} (proj {s.elev_proj:.1f})")


def _check_spine_curvature(ways, nodes, ll_to_m, taxi_axes,
                           noise_m: float = 0.03,
                           min_seg_m: float = 5.0):
    """Grade-CHANGE rate along each taxi axis' emitted profile —
    ``|g2 − g1| ≤ TAXIWAY_MAX_GRADE_CHANGE_PER_M·(L1+L2)/2`` plus a
    2-decimal-emit noise allowance ``noise_m·(1/L1 + 1/L2)`` (on short
    segments rounding alone swamps the rate; the rule is meaningful at
    vertical-curve scale).  The SAME constant the solver's
    ``_fair_spine_chains`` enforces.  Returns ``(count, worst_excess)``.
    """
    try:
        from auto_patch.config import TAXIWAY_MAX_GRADE_CHANGE_PER_M
    except Exception:                                  # pragma: no cover
        return 0, 0.0
    k_rate = TAXIWAY_MAX_GRADE_CHANGE_PER_M
    _AIR = {"apron", "junction", "service_junction", "runway",
            "runway_crossing", "primary_parallel", "secondary_parallel",
            "stub", "cross_connector"}
    # emitted airside vertices with elevation, deduped by position
    pts = {}
    for w in ways:
        if w.tags.get("role") not in _AIR:
            continue
        ring = (w.nids[:-1] if len(w.nids) > 1 and w.nids[0] == w.nids[-1]
                else w.nids)
        for k, nid in enumerate(ring):
            if nid not in nodes or k >= len(w.elevs):
                continue
            e = w.elevs[k]
            if e is None:
                continue
            lat, lon = nodes[nid]
            x, y = ll_to_m(lat, lon)
            pts[(round(x, 1), round(y, 1))] = (x, y, float(e))
    pt_list = list(pts.values())
    n_kinks = 0
    worst = 0.0
    for entry in taxi_axes:
        poly = entry[0]
        if len(poly) < 2:
            continue
        # cumulative arc + per-point projection onto the axis
        arcs = [0.0]
        for i in range(1, len(poly)):
            arcs.append(arcs[-1] + math.hypot(poly[i][0] - poly[i - 1][0],
                                              poly[i][1] - poly[i - 1][1]))
        on_axis = []
        for (x, y, e) in pt_list:
            best_d = 0.6
            best_arc = None
            for i in range(len(poly) - 1):
                x1, y1 = poly[i]
                x2, y2 = poly[i + 1]
                dx, dy = x2 - x1, y2 - y1
                seg2 = dx * dx + dy * dy
                if seg2 < 1e-9:
                    continue
                t = ((x - x1) * dx + (y - y1) * dy) / seg2
                t = min(1.0, max(0.0, t))
                px, py = x1 + t * dx, y1 + t * dy
                d = math.hypot(x - px, y - py)
                if d < best_d:
                    best_d = d
                    best_arc = arcs[i] + t * math.sqrt(seg2)
            if best_arc is not None:
                on_axis.append((best_arc, e))
        if len(on_axis) < 3:
            continue
        on_axis.sort()
        # dedupe coincident arc positions
        prof = []
        for (a, e) in on_axis:
            if prof and a - prof[-1][0] < 0.5:
                continue
            prof.append((a, e))
        for t in range(1, len(prof) - 1):
            l1 = prof[t][0] - prof[t - 1][0]
            l2 = prof[t + 1][0] - prof[t][0]
            if l1 < min_seg_m or l2 < min_seg_m:
                continue
            g1 = (prof[t][1] - prof[t - 1][1]) / l1
            g2 = (prof[t + 1][1] - prof[t][1]) / l2
            allowance = (k_rate * 0.5 * (l1 + l2)
                         + noise_m * (1.0 / l1 + 1.0 / l2))
            ex = abs(g2 - g1) - allowance
            if ex > 0:
                n_kinks += 1
                rate_ex = ex / (0.5 * (l1 + l2))
                if rate_ex > worst:
                    worst = rate_ex
    return n_kinks, worst


# ── Main ────────────────────────────────────────────────────────

def run_checks(
    osm_path: Path,
    max_grade_pct: float = 1.5,
    proximity_m: float = SHARED_VERTEX_TOL_M,
    edge_search_m: float = 5.0,
    edge_step_m: float = 0.5,
    top_n: int = 10,
    taxi_axes_ll: Optional[list] = None,
    routes_ll: Optional[list] = None,
    quiet: bool = False,
    anchor: Optional[Tuple[float, float]] = None,
    seam_pins_ll: Optional[list] = None,
    break_nodes_ll: Optional[list] = None,
    mesh_edges_ll: Optional[list] = None,
    crown_drops_ll: Optional[list] = None,
    crown_centerline_ll: Optional[list] = None,
    pair_caps_ll: Optional[list] = None,
) -> Tuple[List[Violation], List[Violation], List[EdgeStep]]:
    """``taxi_axes_ll`` (the builder's APT.DAT taxi centerlines as
    ``[(latlon_points, cL, cT), …]``) supplies the within-shape grade graph's
    CENTERLINES (spine membership + per-letter cap), sourced from
    ``layout.apt_taxi_centerlines`` and passed as lat/lon so the audit's
    mean-centred meter frame matches; ``routes_ll`` supplies the chained ROUTES
    for the anisotropic spine-arc decomposition (``grade_graph``).  Re-deriving
    centerlines from the OSM would diverge from the apt.dat geometry the builder
    actually used — do not.

    ``seam_pins_ll`` (sidecar ``seam_pins``, ``[[lat, lon], …]``): the exact
    tile-seam DEM-pin vertices the solver graded to.  When given, ONLY nodes
    coincident with a pin are seam-flagged — pin↔pin pairs skip, pin↔free
    pairs check at the shape's body cap — matching the solver's law reading
    (user 2026-07-04, "treat the seam like a runway edge or building").
    Without it the legacy 400 m blanket zone applies (old patches).

    ``mesh_edges_ll`` (sidecar ``mesh_edges``, ``[[[lat, lon], [lat, lon]],
    …]``): the SOLVER's junction triangle-mesh edges, consumed 1:1 for the
    JUNCTION MESH RULE so emit-time ring repairs can't make this reader's
    Delaunay differ from the one the solver graded to.  Without it the mesh
    is re-triangulated from the emitted ring (old patches — stricter, and
    cm-noisy where emit repaired a junction ring).
    """
    nodes, ways = _parse_osm(osm_path)
    ll_to_m = _ll_to_m_factory(nodes, anchor=anchor)
    vertices, edges = _build_vertex_edge_tables(nodes, ways, ll_to_m)
    max_grade = max_grade_pct / 100.0
    if seam_pins_ll is not None:
        seam_nids = _seam_nids_from_pins(nodes, seam_pins_ll)
    else:
        seam_nids = _seam_nids(nodes)

    # Convert apt.dat centerlines (lat/lon) into the audit's meter frame.
    taxi_axes = None
    if taxi_axes_ll:
        taxi_axes = []
        for entry in taxi_axes_ll:
            latlon_pts, cL, cT = entry[0], entry[1], entry[2]
            poly = [ll_to_m(lat, lon) for (lat, lon) in latlon_pts]
            if len(poly) >= 2:
                # keep the builder's route ordinal (4th element) when present
                taxi_axes.append((poly, cL, cT) if len(entry) < 4
                                 else (poly, cL, cT, entry[3]))

    def _pv(*a, **k):
        if not quiet:
            _print_violations(*a, **k)

    def _ps(*a, **k):
        if not quiet:
            _print_steps(*a, **k)

    if not quiet:
        print(f"=== Grade validation: {osm_path} ===")
        n_with_elev = sum(1 for v in vertices if v.elev is not None)
        print(f"  ways: {len(ways)} | vertices: {len(vertices)} "
              f"({n_with_elev} with elevation) | edges: {len(edges)} "
              f"| seam vertices: {len(seam_nids)}")

    # Sidecar mesh edges arrive as lat/lon; convert to this audit's meter
    # frame (both endpoints of a shared solver vertex serialize to the same
    # rounded lat/lon, so vertex identity survives the conversion).
    mesh_edges_m = None
    if mesh_edges_ll:
        mesh_edges_m = [(ll_to_m(*edge[0]), ll_to_m(*edge[1]))
                        for edge in mesh_edges_ll]

    # SPINE CROWN drop field (sidecar ``crown_drops``, part 30): the
    # within-shape law re-centres every pair's budget on the designed
    # crown target (grade_law.crown_pair_offset) — the SAME field the
    # solver built to.  Absent ⇒ offsets 0 (uncrowned/old patches).
    crown_by_nid = _crown_drops_by_nid(nodes, crown_drops_ll or [])
    if crown_by_nid and not quiet:
        print(f"  crown drop field: {len(crown_by_nid)} node(s) crowned")
    # CROWN CENTERLINE nids (Phase 0 hotfix): runway ridge vertices the
    # interior cross-edge crown inserted — skipped from the runway within-shape
    # all-pairs plane law (governed by the spine profile check instead).
    crown_centerline_nids = _crown_centerline_nids(
        nodes, crown_centerline_ll or [])
    if crown_centerline_nids and not quiet:
        print(f"  crown centerline: {len(crown_centerline_nids)} ridge "
              f"node(s) exempt from the runway plane all-pairs check")

    within = _check_within_shape(
        ways, nodes, ll_to_m, max_grade, seam_nids=seam_nids,
        taxi_axes=taxi_axes, routes_ll=routes_ll,
        mesh_edges_m=mesh_edges_m, crown_by_nid=crown_by_nid,
        crown_centerline_nids=crown_centerline_nids,
        pair_caps_ll=pair_caps_ll)
    # BREAK-REGION split (user 2026-07-05): pairs touching a node the
    # SOLVER declared broken (genuine anchor contradiction, rendered as
    # the contained distance-weighted blend) are the pocket's designed
    # over-cap ramp — reported HONESTLY in their own section, excluded
    # from the actionable within-shape count.  Sidecar ``break_nodes``.
    break_region: List[Violation] = []
    if break_nodes_ll:
        break_points_m = [ll_to_m(la, lo) for (la, lo) in break_nodes_ll]

        def _touches_break_node(violation):
            for (px, py) in (violation.pt_a, violation.pt_b):
                for (bx, by) in break_points_m:
                    if (abs(px - bx) <= SHARED_VERTEX_TOL_M
                            and abs(py - by) <= SHARED_VERTEX_TOL_M
                            and math.hypot(px - bx, py - by)
                            <= SHARED_VERTEX_TOL_M):
                        return True
            return False

        kept = []
        for violation in within:
            (break_region if _touches_break_node(violation)
             else kept).append(violation)
        within = kept
    _pv(f"WITHIN-SHAPE vertex-pair grade > {max_grade_pct}%",
        within, top_n)

    plane = _check_plane_gradient(
        ways, nodes, ll_to_m, max_grade, seam_nids=seam_nids,
        crown_by_nid=crown_by_nid)
    # A triangle whose vertex the solver declared broken is the pocket's
    # designed blend — quarantine it exactly like the vertex-pair split
    # above (the triangle-plane law exports unfixable triangles).
    if break_nodes_ll:
        kept_plane = []
        for violation in plane:
            (break_region if _touches_break_node(violation)
             else kept_plane).append(violation)
        plane = kept_plane
    if break_nodes_ll is not None and not quiet:
        print(f"\nBREAK-REGION over-cap (solver-declared infeasible "
              f"pocket, contained blend — by design): "
              f"{len(break_region)} pair(s)")
    _pv(f"PLANE GRADIENT (triangle surface) > {max_grade_pct}%",
        plane, top_n)
    within = within + plane

    # SPINE-PROFILE VERTICAL CURVE (task 3, user 2026-07-04): the same
    # grade-change rate the solver's fairing pass enforces
    # (``config.TAXIWAY_MAX_GRADE_CHANGE_PER_M``), validated on the
    # emitted profile along each sidecar axis.  Reporter-only (not part
    # of the returned violation lists yet — counts calibrate first).
    if taxi_axes and not quiet:
        n_kinks, worst_kink = _check_spine_curvature(
            ways, nodes, ll_to_m, taxi_axes)
        print(f"\nSPINE PROFILE grade-change (vertical curve, "
              f"noise-aware): {n_kinks} kink(s)"
              + (f", worst excess {worst_kink:.4f}/m" if n_kinks else ""))

    # ROUTE-BAND: NOT checked on the OSM patch.  route_field (a parallel
    # per-vertex band on a SEPARATE centerline graph) was retired; the
    # route-band rule is now confirmed in-memory on the ONE graph G by
    # grade_graph_validate.route_band_violations (see
    # docs/grade_law_consolidation_handover.md).  Reconstructing G from the
    # shipped OSM to confirm it here is the remaining "purist OSM-path" follow-up.

    skirt_edges = _check_runway_end_skirt_edges(ways, nodes, ll_to_m)
    _pv("RUNWAY-END SKIRT edge grade > law max down-grade",
        skirt_edges, top_n)
    within = within + skirt_edges

    adjacent_edges = _check_adjacent_ground_edges(ways, nodes, ll_to_m)
    _pv("ADJACENT-GROUND graded-strip TEAR (sub-metre near-vertical edge)",
        adjacent_edges, top_n)
    within = within + adjacent_edges

    strip_seam_tears = _check_strip_seam_tears(vertices, ways)
    _pv(f"ADJACENT-GROUND strip SEAM tear (cross-shape step, "
        f"> {STRIP_SEAM_TEAR_MIN_STEP_M:.1f}m at "
        f"> {STRIP_SEAM_TEAR_MIN_GRADE * 100:.0f}% within "
        f"{STRIP_SEAM_TEAR_RADIUS_M:.1f}m)",
        strip_seam_tears, top_n)
    within = within + strip_seam_tears

    stacked = _check_stacked_nodes(vertices, ways)
    _pv("STACKED NODES (distinct node ids at one coordinate, values "
        "disagree — owner invariant 2026-07-19, cap 0)",
        stacked, top_n)
    within = within + stacked

    cross = _check_cross_shape_proximity(
        vertices, ways, proximity_m, max_grade)
    _pv(f"CROSS-SHAPE proximity (≤ {proximity_m}m) "
        f"grade > {max_grade_pct}%",
        cross, top_n)

    steps = _check_vertex_to_edge_step(
        vertices, edges, ways, edge_search_m, edge_step_m)
    mid_steps = _check_edge_midpoint_step(
        edges, ways, edge_search_m, edge_step_m)
    # BREAK-REGION split for steps (user 2026-07-06): a step whose
    # location touches a solver-declared break node is the same pocket's
    # cross-shape rendering — quarantined exactly like the pocket's
    # vertex pairs (HECA #578↔#64: contradictory welded anchors 1 m
    # apart; the DEM-follow blend contains the deficit by design).
    n_break_steps = 0
    if break_nodes_ll:
        _STEP_BREAK_TOL_M = 2.0

        def _step_touches_break(step):
            for pt in (step.vert_pt, step.proj_pt):
                if pt is None:
                    continue
                px, py = pt
                for (bx, by) in break_points_m:
                    if (abs(px - bx) <= _STEP_BREAK_TOL_M
                            and abs(py - by) <= _STEP_BREAK_TOL_M):
                        return True
            return False
        kept_steps = [s for s in steps if not _step_touches_break(s)]
        kept_mid = [s for s in mid_steps if not _step_touches_break(s)]
        n_break_steps = (len(steps) - len(kept_steps)
                         + len(mid_steps) - len(kept_mid))
        steps = kept_steps
        mid_steps = kept_mid
    _ps(f"VERTEX-TO-EDGE step (within {edge_search_m}m of "
        f"another shape)",
        steps, top_n, edge_step_m)

    _ps(f"MID-EDGE step (sample along each edge, compare to "
        f"nearest other-shape edge)",
        mid_steps, top_n, edge_step_m)
    if n_break_steps and not quiet:
        print(f"  ({n_break_steps} step(s) inside solver-declared break "
              f"region(s) quarantined)")

    # Attach a geographic location (lat, lon) to each finding so callers
    # can point a user at the spot in their apt.dat / DSF.  nodes maps
    # nid -> (lat, lon); use the centroid of the offending way's ring.
    def _way_latlon(way):
        lls = [nodes[n] for n in way.nids if n in nodes]
        if not lls:
            return (None, None)
        return (sum(p[0] for p in lls) / len(lls),
                sum(p[1] for p in lls) / len(lls))

    for v in within + cross:
        v.lat, v.lon = _way_latlon(v.way_a)
    for s in steps + mid_steps:
        s.lat, s.lon = _way_latlon(s.way_v)

    return within, cross, steps + mid_steps


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("osm", type=Path,
                   help="Path to an X-Plane patch.osm file.")
    p.add_argument("--max-grade", type=float, default=1.5,
                   help="Max permitted grade in %% (default 1.5)")
    p.add_argument("--proximity-m", type=float, default=SHARED_VERTEX_TOL_M,
                   help="Cross-shape proximity radius (defaults to the solver's "
                        "SHARED_VERTEX_TOL_M weld tolerance — vertices farther "
                        "apart are independent solver nodes, not a grade pair)")
    p.add_argument("--edge-search-m", type=float, default=5.0,
                   help="Vertex-to-edge search radius (default 5.0 m)")
    p.add_argument("--edge-step-m", type=float, default=0.5,
                   help="Max permitted vertex-to-edge step in m "
                        "(default 0.5)")
    p.add_argument("--top-n", type=int, default=10,
                   help="Show this many worst violations per check")
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 if any check has any violation.")
    args = p.parse_args(argv)
    # AXES SIDECAR (2026-07-02): ``layout.to_osm`` writes the taxi axes +
    # chained routes to ``<patch>.axes.json`` so the STANDALONE check can
    # apply the SAME within-shape law the solver and the suite use (spine
    # membership, per-letter caps, anisotropic Δs∥ credit).  Auto-loaded
    # when present; without it the check is context-free and over-flags
    # every spine/blend-relaxed pair.
    taxi_axes_ll = routes_ll = anchor = seam_pins_ll = None
    break_nodes_ll = mesh_edges_ll = crown_drops_ll = None
    crown_centerline_ll = pair_caps_ll = None
    sidecar = Path(str(args.osm) + ".axes.json")
    if sidecar.exists():
        try:
            import json as _json
            _data = _json.loads(sidecar.read_text())
            _exact = _data.get("axes_exact") or None
            if _exact:
                # exact build_context mirror: (pts, seg_caps, route_ordinal)
                taxi_axes_ll = [(pts, caps, None, ridx)
                                for (pts, caps, ridx) in _exact]
                routes_ll = _data.get("routes_exact") or None
            else:
                taxi_axes_ll = _data.get("axes") or None
                routes_ll = _data.get("routes") or None
            anchor = _data.get("anchor") or None
            seam_pins_ll = _data.get("seam_pins")
            break_nodes_ll = _data.get("break_nodes")
            mesh_edges_ll = _data.get("mesh_edges") or None
            crown_drops_ll = _data.get("crown_drops") or None
            crown_centerline_ll = _data.get("crown_centerline") or None
            pair_caps_ll = _data.get("pair_caps") or None
            print(f"  (axes sidecar loaded: {len(taxi_axes_ll or [])} axes"
                  + (" [exact]" if _exact else "")
                  + f", {len(routes_ll or [])} routes"
                  + (", builder anchor frame" if anchor else "")
                  + (f", {len(seam_pins_ll)} seam pins" if seam_pins_ll
                     is not None else "")
                  + (f", {len(mesh_edges_ll)} solver mesh edges"
                     if mesh_edges_ll else "")
                  + (f", {len(crown_drops_ll)} crown drops"
                     if crown_drops_ll else "")
                  + " — law-true check)")
        except Exception as ex:
            print(f"  (axes sidecar unreadable, context-free check: {ex})")
    within, cross, steps = run_checks(
        args.osm,
        max_grade_pct=args.max_grade,
        proximity_m=args.proximity_m,
        edge_search_m=args.edge_search_m,
        edge_step_m=args.edge_step_m,
        top_n=args.top_n,
        taxi_axes_ll=taxi_axes_ll,
        routes_ll=routes_ll,
        anchor=tuple(anchor) if anchor else None,
        seam_pins_ll=seam_pins_ll,
        break_nodes_ll=break_nodes_ll,
        mesh_edges_ll=mesh_edges_ll,
        crown_drops_ll=crown_drops_ll,
        crown_centerline_ll=crown_centerline_ll,
        pair_caps_ll=pair_caps_ll,
    )
    if args.strict and (within or cross or steps):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

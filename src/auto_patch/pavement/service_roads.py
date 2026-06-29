"""Ground-vehicle ``service_road`` network builder (session 47).

Builds a 4 %-grade rect + junction network for ground-vehicle routes —
apt.dat 1206 truck routes and OSM small roads — but ONLY where a route is
a dedicated strip OUTSIDE aircraft pavement.  Where a route instead
crosses an aircraft movement area (apron / taxiway / runway), nothing is
emitted: that surface's stricter aircraft grade rules already apply (per
user 2026-05-24).

Most service roads have no apt.dat / DSF pavement polygon, so a standard
corridor width is synthesised (``config.SERVICE_ROAD_WIDTH_M``).  The
network mirrors the taxiway model: ``service_road`` rects along straight
runs (graded along their axis at 4 %) + ``service_junction`` fill polygons
at bends / intersections (all-direction 4 %).  Cars handle steeper terrain
than aircraft, so these also double as apron↔DEM transition ramps.
"""
from __future__ import annotations

import math
import os as _os

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.ops import unary_union

from ..layout import ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION

_GEOM_EXC = (ValueError, TypeError, GEOSException, TopologicalError, IndexError)

# A route vertex within this distance of aircraft pavement counts as
# "on the movement area" and is excluded from the service-road network.
_PAV_CLEAR_TOL_M = 1.0
# Turn angle (deg) above which a polyline is split into a new straight run.
_BEND_ANGLE_DEG = 25.0
# Minimum service_junction fill-polygon area to keep (drop slivers).
_MIN_JUNCTION_AREA_M2 = 2.0


def _as_linestrings(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return [g for g in geom.geoms if not g.is_empty]
    return []


def _as_polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if not g.is_empty]
    return []


def _split_at_bends(coords: list[tuple[float, float]]
                    ) -> list[list[tuple[float, float]]]:
    """Split a polyline into straight-ish runs at vertices whose turn
    angle exceeds ``_BEND_ANGLE_DEG``."""
    if len(coords) < 2:
        return []
    runs: list[list[tuple[float, float]]] = []
    cur = [coords[0], coords[1]]
    cos_tol = math.cos(math.radians(_BEND_ANGLE_DEG))
    for k in range(1, len(coords) - 1):
        ax, ay = coords[k - 1]
        bx, by = coords[k]
        cx, cy = coords[k + 1]
        v1x, v1y = bx - ax, by - ay
        v2x, v2y = cx - bx, cy - by
        n1 = math.hypot(v1x, v1y)
        n2 = math.hypot(v2x, v2y)
        straight = (n1 > 1e-6 and n2 > 1e-6
                    and (v1x * v2x + v1y * v2y) / (n1 * n2) >= cos_tol)
        if straight:
            cur.append((cx, cy))
        else:
            runs.append(cur)
            cur = [(bx, by), (cx, cy)]
    runs.append(cur)
    return runs


def _rect_from_endpoints(ax: float, ay: float, bx: float, by: float,
                         width: float) -> tuple[Polygon, LineString] | None:
    """A 4-corner rect of ``width`` centred on A→B, corners in canonical
    ``[hi-left, lo-left, lo-right, hi-right]`` order, plus its axis."""
    dx, dy = bx - ax, by - ay
    L = math.hypot(dx, dy)
    if L < 1e-6:
        return None
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    h = width / 2.0
    try:
        poly = Polygon([
            (ax + nx * h, ay + ny * h),
            (bx + nx * h, by + ny * h),
            (bx - nx * h, by - ny * h),
            (ax - nx * h, ay - ny * h),
        ])
        axis = LineString([(ax, ay), (bx, by)])
    except _GEOM_EXC:
        return None
    if poly.is_empty or not poly.is_valid:
        return None
    return poly, axis


def build_service_road_network(
        centerlines: list[tuple[LineString, str]],
        pav_union: Polygon | None,
        *,
        width: float,
        min_len: float,
) -> tuple[list[tuple[Polygon, LineString, str, str]],
           list[tuple[Polygon, str, str]]]:
    """Build the ground-vehicle network from ``centerlines``
    (``[(LineString_m, name)]``).

    Returns ``(rects, junctions)``:
      * ``rects``     = ``[(rect, axis, ROLE_SERVICE_ROAD, name)]``
      * ``junctions`` = ``[(polygon, ROLE_SERVICE_JUNCTION, name)]``

    Only the portions of each route OUTSIDE aircraft pavement
    (``pav_union``) contribute.  Rects cover straight runs (trimmed back
    from bends / ends so they never overlap); junctions are the corridor
    residue (``corridor − rects``) at bends and intersections.  Width is
    the synthesised ``config.SERVICE_ROAD_WIDTH_M`` (most service roads
    have no pavement polygon).
    """
    rects: list[tuple[Polygon, LineString, str, str]] = []
    junctions: list[tuple[Polygon, str, str]] = []
    if not centerlines:
        return rects, junctions

    pav_buf = None
    pav_prep = None
    if pav_union is not None and not pav_union.is_empty:
        try:
            from shapely.prepared import prep
            pav_buf = pav_union.buffer(_PAV_CLEAR_TOL_M)
            pav_prep = prep(pav_buf)
        except _GEOM_EXC:
            pav_buf = None
            pav_prep = None

    # External (off-pavement) centerline pieces.  Most service roads are
    # already entirely off aircraft pavement — skip the expensive
    # difference() unless the road actually touches it (prepared check).
    ext: list[tuple[LineString, str]] = []
    for line, name in ((c.line, c.name) if hasattr(c, "line") else c
                       for c in centerlines):
        if line is None or line.is_empty:
            continue
        if pav_buf is not None and pav_prep.intersects(line):
            g = line.difference(pav_buf)
        else:
            g = line
        for piece in _as_linestrings(g):
            if piece.length >= 1.0:
                ext.append((piece, name))
    if not ext:
        return rects, junctions

    half = width / 2.0

    # Corridor = standard-width buffer of every external piece, clipped
    # to stay off aircraft pavement.  Flat caps / mitre joins keep it
    # tight to the routes.
    try:
        corridor = unary_union(
            [p.buffer(half, cap_style=2, join_style=2) for p, _ in ext])
        if pav_buf is not None:
            corridor = corridor.difference(pav_buf)
        if not corridor.is_valid:
            corridor = corridor.buffer(0)
    except _GEOM_EXC:
        return rects, junctions
    if corridor.is_empty:
        return rects, junctions

    # Rects on straight runs, trimmed back from each end by ``half`` so
    # adjacent runs / crossing roads don't overlap (the gap becomes
    # junction fill).  Skip a rect that would touch aircraft pavement or
    # an already-kept rect.
    kept_polys: list[Polygon] = []
    for piece, name in ext:
        coords = list(piece.coords)
        for run in _split_at_bends(coords):
            if len(run) < 2:
                continue
            ax, ay = run[0]
            bx, by = run[-1]
            seg_len = math.hypot(bx - ax, by - ay)
            if seg_len <= 2.0 * half:
                continue
            ux, uy = (bx - ax) / seg_len, (by - ay) / seg_len
            ax2, ay2 = ax + ux * half, ay + uy * half      # trim start
            bx2, by2 = bx - ux * half, by - uy * half      # trim end
            if math.hypot(bx2 - ax2, by2 - ay2) < min_len:
                continue
            built = _rect_from_endpoints(ax2, ay2, bx2, by2, width)
            if built is None:
                continue
            rect, axis = built
            try:
                if pav_buf is not None and rect.intersects(pav_buf):
                    continue                       # pokes into aircraft pavement
                if any(rect.intersection(kp).area > 1.0 for kp in kept_polys):
                    continue                       # overlaps a kept rect
            except _GEOM_EXC:
                continue
            rects.append((rect, axis, ROLE_SERVICE_ROAD, name))
            kept_polys.append(rect)

    # Junctions = corridor − rects, keeping pieces above the sliver floor.
    try:
        residue = corridor
        if kept_polys:
            residue = corridor.difference(unary_union(kept_polys))
    except _GEOM_EXC:
        residue = corridor
    for poly in _as_polygons(residue):
        if poly.is_empty or not poly.is_valid:
            continue
        if poly.area >= _MIN_JUNCTION_AREA_M2:
            junctions.append((poly, ROLE_SERVICE_JUNCTION, "service"))

    return rects, junctions


# ──────────────────────────────────────────────────────────────────────
# (s79) ON-PAVEMENT road detection — docs/service_road_carve.md §2
# ──────────────────────────────────────────────────────────────────────
def detect_road_runs(
    routes: "list[tuple[LineString, str]]",
    pav_union,
    terminal_polys=None,
    runway_union=None,
    source_polys=None,
    *,
    max_width_m: float | None = None,
    terminal_clear_m: float | None = None,
    sample_m: float | None = None,
    min_run_m: float | None = None,
) -> "list[tuple[LineString, float, str]]":
    """Qualifying ROAD runs along apt.dat 1206 truck routes.

    ★ USER RULINGS (2026-06-11): roads are the 1206 LINES only; only
    pavement narrower than the cross-section cap is classified; nothing
    near a terminal.  A sample point on a merged route qualifies when

      (a) it lies ON pavement (``pav_union``),
      (b) EITHER the PERPENDICULAR pavement cross-section through it is
          ≤ ``ROAD_CARVE_MAX_WIDTH_M`` — a dedicated strip, not an apron
          interior (calibrated at the HECA #198 switchback: legs
          8.2-9.4 m / 12.2 m, the U-turn bulge 11-24 m stays junction
          territory) — OR the sample lies inside a NARROW SOURCE
          pavement polygon (mean width 2A/P ≤ the same cap): a road
          drawn as its own strip polygon that runs ALONG the edge of
          apron pavement blends into the fused union, so the chord
          reads road+apron (CYXY "New Taxiway 40" pav[1]: 11.5 m strip,
          chords 16-34 m — user-confirmed ROAD 2026-06-11),
      (c) it is ≥ ``ROAD_CARVE_TERMINAL_CLEAR_M`` from every terminal
          pad, and
      (d) it is outside the runway footprint + 3 m halo (the s69 carve
          lesson).

    Consecutive qualifying samples spanning ≥ ``ROAD_CARVE_MIN_RUN_M``
    become one run.  Returns ``[(run_centerline, median_width_m,
    route_name)]``; run centerlines are exact substrings of the input
    routes (bends preserved — the shared ``split_merged_centerline``
    downstream cuts them into straight rect axes + junction territory
    exactly like a taxiway).
    """
    from shapely.ops import substring
    from shapely.prepared import prep

    from ..config import (
        ROAD_CARVE_EDGE_HUG_MAX_M,
        ROAD_CARVE_MAX_WIDTH_M,
        ROAD_CARVE_MIN_RUN_M,
        ROAD_CARVE_SAMPLE_M,
        ROAD_CARVE_TERMINAL_CLEAR_M,
        ROAD_CARVE_TERMINAL_PARA_DEG,
        ROAD_CARVE_TERMINAL_RIM_M,
    )
    max_w = ROAD_CARVE_MAX_WIDTH_M if max_width_m is None else max_width_m
    term_clear = (ROAD_CARVE_TERMINAL_CLEAR_M
                  if terminal_clear_m is None else terminal_clear_m)
    step = ROAD_CARVE_SAMPLE_M if sample_m is None else sample_m
    min_run = ROAD_CARVE_MIN_RUN_M if min_run_m is None else min_run_m

    if pav_union is None or pav_union.is_empty or not routes:
        return []
    pav_prep = prep(pav_union)
    pav_boundary = pav_union.boundary

    # Terminal "ALONGSIDE" guard (user round 3): keep the terminal
    # polygons (not just a fused zone) so a flagged sample can compare
    # its route direction to the nearest terminal EDGE — only locally
    # parallel routes are curb roads to drop.
    term_list = [tp for tp in (terminal_polys or ())
                 if tp is not None and not tp.is_empty]
    term_zone = None
    if term_list:
        try:
            term_zone = unary_union(term_list).buffer(term_clear)
        except _GEOM_EXC:
            term_zone = None
    term_prep = prep(term_zone) if term_zone is not None \
        and not term_zone.is_empty else None

    def _alongside_terminal(p, dx, dy,
                            radius: float | None = None) -> bool:
        """True when ``p`` is within ``radius`` of a terminal AND the
        route runs locally parallel to the nearest terminal edge.
        Default radius = the close-in clear zone; mode C passes the
        larger TERMINAL_RIM radius (rim roads along the terminal row
        absorb into the apron — user round 4)."""
        if not term_list:
            return False
        if radius is None:
            if term_prep is None or not term_prep.contains(p):
                return False
        best_tp, best_d = None, float("inf")
        for tp in term_list:
            d9 = tp.exterior.distance(p)
            if d9 < best_d:
                best_d, best_tp = d9, tp
        if best_tp is None or (radius is not None and best_d > radius):
            return False
        # RADIAL test (robust on blobby pad rings, unlike a local ring
        # tangent): "alongside" = the route moves PERPENDICULAR to the
        # direction toward the terminal (i.e. parallel to its front).
        # A road heading at/away from the terminal (the HECA corner →
        # junction #168 section) is radial → keep.
        ring = best_tp.exterior
        q = ring.interpolate(ring.project(p))
        rx, ry = q.x - p.x, q.y - p.y
        rl = math.hypot(dx, dy) * math.hypot(rx, ry)
        if rl < 1e-9:
            return True          # degenerate: keep the old (drop) rule
        ang = math.degrees(math.acos(
            min(1.0, abs(dx * rx + dy * ry) / rl)))
        return ang >= (90.0 - ROAD_CARVE_TERMINAL_PARA_DEG)

    rwy_halo = None
    if runway_union is not None and not runway_union.is_empty:
        try:
            rwy_halo = runway_union.buffer(3.0)
        except _GEOM_EXC:
            rwy_halo = None
    rwy_prep = prep(rwy_halo) if rwy_halo is not None else None

    # Mode-B membership: NARROW source pavement polygons (a road drawn
    # as its own strip, mean width ≤ the cap).  Wide aprons never enter.
    narrow_srcs = []
    for sp in (source_polys or ()):
        if sp is None or sp.is_empty or sp.geom_type != "Polygon":
            continue
        try:
            if (sp.area >= 500.0
                    and 2.0 * sp.area / max(sp.boundary.length, 1e-9)
                    <= max_w):
                narrow_srcs.append((prep(sp), sp))
        except _GEOM_EXC:
            continue

    # Perpendicular probe half-length: a chord fully inside pavement
    # longer than 2*reach reads as "wide" regardless — keep reach just
    # above the width cap so the test stays cheap and unambiguous.
    reach = max_w + 4.0

    # APRON-CONNECTOR exemption (user 2026-06-26): a 1206 truck route that CONNECTS
    # the apron to a groundside lot is an SVC road CONNECTOR and must be carved (and
    # cut at both mouths), even where the alongside-terminal guard would drop it as a
    # curb road — "curbside is just groundside, it doesn't need its own class; a
    # truck route that never touches the apron just stays groundside."  A route is a
    # connector iff it CROSSES WIDE aircraft pavement (the apron: a perpendicular
    # chord through pavement ≫ a road width); a pure off-apron curb/parking road
    # never does, so it still drops to groundside.  The exemption only affects narrow
    # samples the terminal guard would otherwise drop (wide samples still fail the
    # width test), so it is safe.
    _connector_keep = _os.environ.get("O4_ROAD_CONNECTOR_KEEP", "1") == "1"
    _apron_reach = 1.5 * max_w + 4.0

    def _touches_apron(ls) -> bool:
        nn = max(1, int(ls.length // step))
        for tt in range(nn + 1):
            dd = min(tt * step, ls.length)
            pp = ls.interpolate(dd)
            if not pav_prep.contains(pp):
                continue
            a1 = ls.interpolate(max(dd - 2.0, 0.0))
            a2 = ls.interpolate(min(dd + 2.0, ls.length))
            adx, ady = a2.x - a1.x, a2.y - a1.y
            ah = math.hypot(adx, ady) or 1.0
            apx, apy = -ady / ah, adx / ah
            try:
                ac = LineString([(pp.x - _apron_reach * apx, pp.y - _apron_reach * apy),
                                 (pp.x + _apron_reach * apx, pp.y + _apron_reach * apy)])
                ai = ac.intersection(pav_union)
            except _GEOM_EXC:
                continue
            for pc in ([ai] if ai.geom_type == "LineString"
                       else [g for g in getattr(ai, "geoms", ())
                             if g.geom_type == "LineString"]):
                if pc.distance(pp) < 0.5 and pc.length > 1.5 * max_w:
                    return True
        return False

    out: "list[tuple[LineString, float, str]]" = []
    for ls, name in ((c.line, c.name) if hasattr(c, "line") else c
                     for c in routes):
        L = ls.length
        if L < min_run:
            continue
        connector = _connector_keep and _touches_apron(ls)
        n = max(1, int(L // step))
        flags: list[bool] = []
        widths: list[float] = []
        dists: list[float] = []
        offpav: list[bool] = []
        for t in range(n + 1):
            d0 = min(t * step, L)
            p = ls.interpolate(d0)
            p1 = ls.interpolate(max(d0 - 2.0, 0.0))
            p2 = ls.interpolate(min(d0 + 2.0, L))
            dx, dy = p2.x - p1.x, p2.y - p1.y
            ok = False
            w = float("inf")
            if (pav_prep.contains(p)
                    and (connector or not _alongside_terminal(p, dx, dy))
                    and (rwy_prep is None or not rwy_prep.contains(p))):
                h = math.hypot(dx, dy) or 1.0
                px, py = -dy / h, dx / h
                try:
                    chord = LineString([
                        (p.x - reach * px, p.y - reach * py),
                        (p.x + reach * px, p.y + reach * py)])
                    inter = chord.intersection(pav_union)
                except _GEOM_EXC:
                    inter = None
                pieces = []
                if inter is not None and not inter.is_empty:
                    pieces = ([inter] if inter.geom_type == "LineString"
                              else [g for g in getattr(inter, "geoms", ())
                                    if g.geom_type == "LineString"])
                for pc in pieces:
                    if pc.distance(p) < 0.5:
                        w = pc.length
                        ok = w <= max_w
                        break
                if not ok:
                    # Mode B: inside a narrow source strip polygon
                    # (edge-blended road — see docstring (b)).
                    for sprep, sp in narrow_srcs:
                        if sprep.contains(p):
                            w = min(w, 2.0 * sp.area
                                    / max(sp.boundary.length, 1e-9))
                            ok = True
                            break
                if not ok:
                    # Mode C: EDGE-HUGGING — near the airside rim the
                    # road is "not surrounded by apron" even when the
                    # cross-section blends wide (user round 3) — UNLESS
                    # it runs along the terminal row (rim radius test,
                    # user round 4: those absorb into the apron).
                    try:
                        gap = p.distance(pav_boundary)
                    except _GEOM_EXC:
                        gap = float("inf")
                    if (gap <= ROAD_CARVE_EDGE_HUG_MAX_M
                            and not _alongside_terminal(
                                p, dx, dy,
                                radius=ROAD_CARVE_TERMINAL_RIM_M)):
                        ok = True
                        w = min(w, max_w)
            dists.append(d0)
            flags.append(ok)
            widths.append(w)
            offpav.append(not pav_prep.contains(p))

        t0 = None
        for t in range(n + 2):
            on = t <= n and flags[t]
            if on and t0 is None:
                t0 = t
            elif not on and t0 is not None:
                d_start, d_end = dists[t0], dists[min(t - 1, n)]
                # CONNECTOR DEAD-END EXTENSION (user 2026-06-26): a connector run
                # that ends where the route exits pavement INTO the groundside
                # (the lot was subtracted from pav_union before the carve) stops ~a
                # road-width short, leaving a GAP to the groundside.  Extend the run
                # to the first off-pav sample (the groundside boundary) so the SVC
                # rect reaches it — the overlap-clip then makes the SVC road SHARE
                # an edge with the groundside (a cut, not a gap).  Only into off-pav
                # (the lot), never into a wide-apron interior.
                if connector:
                    if t <= n and offpav[t]:
                        d_end = dists[min(t, n)]
                    if t0 - 1 >= 0 and offpav[t0 - 1]:
                        d_start = dists[t0 - 1]
                if d_end - d_start >= min_run:
                    try:
                        run = substring(ls, d_start, d_end)
                    except _GEOM_EXC:
                        run = None
                    if run is not None and run.geom_type == "LineString" \
                            and run.length >= min_run:
                        ws = sorted(widths[t0:min(t - 1, n) + 1])
                        out.append((run, ws[len(ws) // 2] if ws else max_w, name))
                t0 = None

    # ── STRIP EXTENSION (user round 3): a 1206 road that ENTERS a
    # narrow apt.dat strip polygon continues as a road along the
    # strip's own medial axis even where the 1206 polyline stops
    # (CYXY "New Taxiway 40" ramp: 'Crew cars' drives ~97 m into the
    # 11.5 m strip and turns around; the ramp itself runs another
    # ~250 m).  Roads keep 1206 provenance — the strip must carry
    # ≥ 15 m of route to extend.
    if narrow_srcs:
        try:
            from .discovered_taxiways import (
                _flatten_lines, _medial_segments, _prune)
            from shapely.ops import linemerge
        except Exception:                              # pragma: no cover
            narrow_srcs = []
        run_union = None
        if out:
            try:
                run_union = unary_union([r for (r, _w, _n) in out])
            except _GEOM_EXC:
                run_union = None
        for sprep, sp in narrow_srcs:
            inside = 0.0
            touch_name = ""
            for ls, name in ((c.line, c.name) if hasattr(c, "line") else c
                             for c in routes):
                try:
                    li = ls.intersection(sp).length
                except _GEOM_EXC:
                    continue
                if li > inside:
                    inside, touch_name = li, name
            if inside < 15.0:
                continue
            mean_w = 2.0 * sp.area / max(sp.boundary.length, 1e-9)
            try:
                segs = _medial_segments(sp, 2.0, max_w / 2.0 + 2.0, 2.5)
                if not segs:
                    continue
                lanes = _prune(_flatten_lines(
                    linemerge(unary_union(segs))), 15.0)
            except _GEOM_EXC:
                continue
            if not lanes:
                continue
            merged9 = unary_union(lanes)
            try:
                merged9 = linemerge(merged9)
            except ValueError:
                pass                     # already a single LineString
            lane_cands = _flatten_lines(merged9)
            if not lane_cands:
                continue
            lane = max(lane_cands, key=lambda g: g.length)
            if lane.length < min_run:
                continue
            # drop the part already covered by a qualifying run
            ext = lane
            if run_union is not None:
                try:
                    rem = lane.difference(run_union.buffer(mean_w))
                    pieces9 = [g for g in _flatten_lines(rem)
                               if g.length >= min_run]
                    if not pieces9:
                        continue
                    ext = max(pieces9, key=lambda g: g.length)
                except _GEOM_EXC:
                    pass
            # terminal / runway guards still apply at the midpoint
            mid = ext.interpolate(0.5, normalized=True)
            c1 = ext.interpolate(max(ext.project(mid) - 2.0, 0.0))
            c2 = ext.interpolate(min(ext.project(mid) + 2.0, ext.length))
            if _alongside_terminal(mid, c2.x - c1.x, c2.y - c1.y):
                continue
            if rwy_prep is not None and rwy_prep.contains(mid):
                continue
            out.append((ext, mean_w, f"{touch_name}+strip"))
    return out

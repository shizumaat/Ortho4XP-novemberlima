"""Curve-native spine v2, Phase 1 — the GLOBAL slice.

Instead of manufacturing straight taxi rects and slicing junctions out of
the residue, cut the REAL ``pav_union`` (already following every true
curve, fillet and width change) by the recognized curved centerlines in
ONE global polygonize arrangement.  Each resulting face is a grading cell
that carries a spine edge; because every face is born from the same
grid-snapped, re-noded arrangement, the faces share EXACT edges — so the
result is conformant (no T-junctions) by construction, with no per-junction
weld/repair pass.

This module is pure geometry (no elevations, no roles beyond a placeholder).
It is consumed behind the ``O4_CURVE_NATIVE_SPINE`` gate.  See
docs/curve_native_spine_v2_plan.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from shapely import union_all
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from ..config import SPINE_STEP_M

# Same polygonize-noding grid + min-piece area as the per-junction slice
# (``junction_spine``) so the two produce comparable geometry.
_GRID_SIZE = 0.01
_MIN_PIECE_AREA = 0.25
# A face vertex within this of a centerline lies ON that centerline (the
# solver's ``grade_graph.SPINE_PERP_TOL_M``).
_ON_TOL_M = 1.0

# ── Dead-end keyhole ─────────────────────────────────────────────────
# A centerline that terminates INSIDE the pavement is a dangling cut.
# ``polygonize`` keeps only minimal CYCLES and drops bridge edges, so a
# bare dead-end (or a dead-end reaching a detached ring) never becomes a
# face boundary — no spine nodes at the tip.  A medial line only becomes a
# face edge when it SEPARATES two regions, i.e. reaches a boundary.  So the
# keyhole spurs the tip to the nearest pavement boundary (the corridor
# side-rail, or a stand's building-pad hole): the short spur + the
# centerline then run boundary-to-boundary and split off a face, putting
# spine nodes on the centerline to its tip.  The spur is the "tiny keyhole
# in the pavement" — a hairline cut to the nearest edge, kept short by the
# cap below.  The centerlines fed here are recognized, route-ridden lines
# (recognition already dropped stray paint), so every free interior end is
# a real taxiway terminus.
#
# An endpoint farther than this from the pavement boundary is INTERIOR.
_DEADEND_BOUNDARY_TOL_M = 3.0
# Another centerline within this of an endpoint means it JOINS the graph
# there (a junction node), not a free dead-end.
_JOIN_TOL_M = 3.0
# Do not spur a tip whose nearest boundary is farther than this (a taxi
# corridor is at most ~this wide; a longer spur would seam an open apron).
_KEYHOLE_MAX_SPUR_M = 40.0


@dataclass
class SliceFace:
    """One polygonized grading cell from the global slice."""
    polygon: Polygon
    # Indices (into the input centerline list) whose line touches this face.
    centerline_ids: list[int] = field(default_factory=list)
    # Phase-2 classification (set by ``classify_faces``): "corridor",
    # "junction", or "apron"; ``axis`` is the centerline arc a corridor face
    # runs along (its spine), else None.
    kind: str = ""
    axis: LineString | None = None


# A single-centerline face wider than this (mean width = area / shared-edge
# length) is an APRON, not a taxi CORRIDOR.  ~ICAO-F taxiway + fillet slack.
_CORRIDOR_MAX_WIDTH_M = 50.0
# A face with ≥2 centerlines but larger than this is a big open pavement an
# aircraft crosses (an APRON), NOT a tight taxiway junction — grade it with the
# gentle apron body model, not junction all-pair at 1.5%.  Matches the rect
# model, where junctions are small residue pieces and aprons are the big blobs.
_JUNCTION_MAX_AREA_M2 = 2500.0
# Route-territory test for BIG multi-centerline faces: the fraction of the
# face's area within a taxi-corridor half-width of its own centerlines.  A
# junction complex (however large) is mostly within reach of the routes that
# cross it; a stand/terminal apron with one taxilane along its edge is not.
# Half-width ~ICAO-E/F corridor (route-arc ways carry no per-face width here).
_ROUTE_TERRITORY_HALF_W_M = 25.0
_ROUTE_TERRITORY_MIN_FRAC = 0.55


def classify_faces(faces: list[SliceFace], centerlines: list[LineString]
                   ) -> None:
    """Tag each face corridor / junction / apron from spine topology, in place.

    * **corridor** — the face is narrow (mean width = area / shared-edge
      length ≤ ``_CORRIDOR_MAX_WIDTH_M``); ``axis`` is the longest touching
      centerline.  Piece COUNT is deliberately not used: a noded route graph
      (route-arc spine) borders one corridor with many consecutive fragments.
    * **junction** — small multi-centerline face (≤ ``_JUNCTION_MAX_AREA_M2``)
      OR a big face that is ROUTE TERRITORY (≥ ``_ROUTE_TERRITORY_MIN_FRAC``
      of its area within ``_ROUTE_TERRITORY_HALF_W_M`` of its centerlines) —
      an aircraft-movement surface graded with the taxi law, not the 1 %
      stand-apron law.
    * **apron** — everything else (no centerlines, or open stand/terminal
      pavement beyond route reach)."""
    for face in faces:
        ids = face.centerline_ids
        if not ids:
            face.kind = "apron"
            face.axis = None
            continue
        cls = [centerlines[i] for i in ids]
        shared = 0.0
        try:
            buf = unary_union([c.buffer(_ON_TOL_M, cap_style=2) for c in cls])
            shared = face.polygon.exterior.intersection(buf).length
        except Exception:
            shared = 0.0
        width = (face.polygon.area / shared) if shared > 1.0 else 1e9
        if width <= _CORRIDOR_MAX_WIDTH_M:
            face.kind = "corridor"
            face.axis = max(cls, key=lambda c: c.length)
            continue
        if face.polygon.area <= _JUNCTION_MAX_AREA_M2:
            face.kind = "junction"
            face.axis = None
            continue
        frac = 0.0
        try:
            reach = unary_union(
                [c.buffer(_ROUTE_TERRITORY_HALF_W_M) for c in cls])
            frac = face.polygon.intersection(reach).area / face.polygon.area
        except Exception:
            frac = 0.0
        face.kind = ("junction" if frac >= _ROUTE_TERRITORY_MIN_FRAC
                     else "apron")
        face.axis = None


# Straight spine runs take a WIDER node step (user 2026-07-03, node-count
# optimization): nodes exist to carry the VERTICAL profile, and on a straight
# run 24 m still resolves a 1.5 % grade to ~0.36 m per segment — while curves
# keep the tight step so arcs stay arcs.  59 % of SPJC's airside ring vertices
# were near-collinear at the uniform 12 m step; every dropped node removes
# solver edges (superlinear: within-face pair candidates) AND mesh triangles.
# Measured at 24 m: SPJC build 86.8→77.3 s, law-true 178→155 (fewer sub-noise
# pairs), airside verts −8.5%.  DEFAULT ON at 24 m since item B closed
# (2026-07-03): the CYXY apron-#120 rests_on_source failure this was banked
# behind was ``_enforce_runway_1to1_sharing``'s off-source carve falling back
# to the UNCARVED ring whenever the carve split the junction — fixed with
# split-keep (largest part stays, real-pavement extras become their own
# junctions); 0 off-source shapes at step 24, probe point lands in clearance.
# 0 disables (uniform ``step`` everywhere).
_STRAIGHT_STEP_M = float(os.environ.get("O4_SPINE_STEP_STRAIGHT_M", "24"))
# a vertex deflecting less than this is "straight" for step selection.
_STRAIGHT_DEFLECT_DEG = 3.0


def _resample(line: LineString, step: float) -> LineString:
    """Densify ``line`` to ≤``step`` m node spacing PRESERVING every original
    vertex (shapely ``segmentize``).  The old even-respacing MOVED original
    vertices (bends / arc points) off the design line unless a sample landed
    exactly on them — the skeleton no longer translated cleanly to the
    emitted spine (user 2026-07-03: an input spine node ended up 10 m from
    any emitted node).

    ADAPTIVE step: a segment whose BOTH endpoints are straight-through
    vertices (deflection < ``_STRAIGHT_DEFLECT_DEG``) densifies at the wider
    ``_STRAIGHT_STEP_M``; segments at/next to bends keep ``step``.  Original
    vertices are always preserved either way."""
    L = line.length
    if L <= min(step, _STRAIGHT_STEP_M or step) or step <= 0:
        return line
    try:
        import math as _m
        coords = list(line.coords)
        wide = _STRAIGHT_STEP_M
        if wide <= step:                     # gate off / misconfigured
            import shapely
            return shapely.segmentize(line, step)

        def _deflect(i):
            """Deflection angle (deg) at interior vertex ``i``.  Endpoints
            carry no curvature information → 0 (straight), so a plain
            2-point straight segment takes the wide step."""
            if i <= 0 or i >= len(coords) - 1:
                return 0.0
            ax, ay = coords[i - 1]
            bx, by = coords[i]
            cx, cy = coords[i + 1]
            v1x, v1y = bx - ax, by - ay
            v2x, v2y = cx - bx, cy - by
            n1 = _m.hypot(v1x, v1y)
            n2 = _m.hypot(v2x, v2y)
            if n1 < 1e-9 or n2 < 1e-9:
                return 0.0
            d = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (n1 * n2)))
            return _m.degrees(_m.acos(d))

        out = [coords[0]]
        for k in range(len(coords) - 1):
            (ax, ay), (bx, by) = coords[k], coords[k + 1]
            seg = _m.hypot(bx - ax, by - ay)
            s = (wide if _deflect(k) < _STRAIGHT_DEFLECT_DEG
                 and _deflect(k + 1) < _STRAIGHT_DEFLECT_DEG else step)
            n = int(_m.ceil(seg / s)) if seg > s else 1
            for t in range(1, n):
                f = t / n
                out.append((ax + f * (bx - ax), ay + f * (by - ay)))
            out.append((bx, by))
        return LineString(out)
    except Exception:
        try:
            import shapely
            return shapely.segmentize(line, step)
        except Exception:
            return line


def _as_lines(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms if not g.is_empty]
    # GeometryCollection etc. — keep only 1-D parts.
    out = []
    for g in getattr(geom, "geoms", ()):
        if isinstance(g, (LineString, MultiLineString)):
            out.extend(_as_lines(g))
    return out


# Two centerlines running within this of each other are the SAME physical
# line (a recognized curve can ride a straight route offset up to ~7 m, so
# a painted line and the route it swapped can co-exist over the pavement) —
# keep only one, else the buried duplicate carries no spine nodes.
_DEDUP_TOL_M = 3.5
_DEDUP_MIN_KEEP_M = 4.0


def dedup_centerlines(lines: list[LineString], tol: float = _DEDUP_TOL_M
                      ) -> list[LineString]:
    """Greedily drop stretches of each line that run coincident with an
    already-kept line, so no two centerlines overlap (which would bury the
    duplicate inside a face with no nodes).  Distinct parallel taxiways
    (> ``tol`` apart) are untouched — they never bury each other."""
    order = sorted(range(len(lines)),
                   key=lambda i: lines[i].length if lines[i] else 0.0,
                   reverse=True)
    kept: list[LineString] = []
    kept_buf = None
    for i in order:
        cl = lines[i]
        if cl is None or cl.is_empty or cl.length < 1.0:
            continue
        remain = cl
        if kept_buf is not None:
            try:
                remain = cl.difference(kept_buf)
            except Exception:
                remain = cl
        for piece in _as_lines(remain):
            if piece.length < _DEDUP_MIN_KEEP_M:
                continue
            kept.append(piece)
        try:
            buf = cl.buffer(tol, cap_style=2)
            kept_buf = buf if kept_buf is None else kept_buf.union(buf)
        except Exception:
            pass
    return kept


def _boundary_lines(poly: Polygon) -> list[LineString]:
    """Exterior + hole boundaries of a Polygon / MultiPolygon as lines."""
    out: list[LineString] = []
    geoms = getattr(poly, "geoms", None) or [poly]
    for g in geoms:
        if g.is_empty or g.geom_type != "Polygon":
            continue
        out.append(LineString(g.exterior.coords))
        for hole in g.interiors:
            out.append(LineString(hole.coords))
    return out


def build_global_slice_faces(
    pav_union: Polygon,
    centerlines: list[LineString],
    *,
    runway_union: Polygon | None = None,
    step: float = SPINE_STEP_M,
    keyholes: bool = True,
    dedup: bool = True,
    extra_cuts: list[LineString] | None = None,
    collect_spurs: list | None = None,
    debug_pts: list | None = None,
) -> list[SliceFace]:
    """Cut ``pav_union`` by ``centerlines`` into conformant grading faces.

    ``centerlines`` are continuous aircraft-taxi lines in the layout meter
    frame.  When ``dedup`` is set, coincident/overlapping lines are reduced to
    one representative first (else the buried duplicate carries no nodes).
    When ``keyholes`` is set, each free interior dead-end is spurred to the
    nearest boundary so its centerline grades to its tip.  ``extra_cuts`` are
    additional caller-supplied cut lines.  Returns the faces that lie on
    pavement, each tagged with the (effective, post-dedup) centerline indices
    it touches; ``effective_centerlines`` returns that same list.
    """
    def _dbg(tag, geom):
        if not debug_pts:
            return
        try:
            for (dx, dy) in debug_pts:
                print(f"  [slice-dbg] {tag}: ({dx:.0f},{dy:.0f}) "
                      f"covered={geom.intersects(Point(dx, dy).buffer(0.2))}")
        except Exception as _e:
            print(f"  [slice-dbg] {tag}: ERROR {_e!r}")

    def _polygonal_parts(geometry):
        """Reduce a GeometryCollection to its polygonal parts.

        difference()/intersection() return a GeometryCollection carrying
        line/point crumbs wherever the operands are exactly tangent
        (CYUL: pavement tangent to the runway union).  The slice only
        ever means AREA pavement — and shapely's boundary of a
        collection is None, which would crash the keyhole/spur passes.
        """
        if geometry.geom_type != "GeometryCollection":
            return geometry
        return unary_union([part for part in geometry.geoms
                            if part.geom_type in ("Polygon",
                                                  "MultiPolygon")])

    if pav_union is None or pav_union.is_empty:
        return []
    pav = _polygonal_parts(pav_union)
    _dbg("input-pav", pav)
    if runway_union is not None and not runway_union.is_empty:
        pav = _polygonal_parts(pav.difference(runway_union))
    if pav.is_empty:
        return []
    _dbg("pav-minus-runway", pav)

    if dedup:
        centerlines = dedup_centerlines(centerlines)

    # Clip each centerline to the pavement (recognition feeds them un-clipped)
    # and resample to even node spacing, so the shared edges carry ~step nodes.
    cut_lines: list[LineString] = list(_boundary_lines(pav))
    clipped: list[LineString] = []          # index-aligned to ``centerlines``
    for cl in centerlines:
        if cl is None or cl.is_empty or cl.length < 1.0:
            clipped.append(None)
            continue
        try:
            inside = cl.intersection(pav)
        except Exception:
            clipped.append(None)
            continue
        parts = _as_lines(inside)
        if not parts:
            clipped.append(None)
            continue
        merged = unary_union(parts) if len(parts) > 1 else parts[0]
        line = merged if isinstance(merged, LineString) else None
        # Keep the longest piece as the representative for tagging/coverage;
        # feed ALL pieces as cuts.
        rep = None
        for p in _as_lines(merged) if line is None else [line]:
            rp = _resample(p, step)
            cut_lines.append(rp)
            if rep is None or rp.length > rep.length:
                rep = rp
        clipped.append(rep)

    # Dead-end keyholes: spur each free interior terminus to the nearest
    # pavement boundary so the centerline separates a face (nodes to the tip).
    if keyholes:
        from shapely.ops import nearest_points
        pav_bnd = pav.boundary
        for i, rep in enumerate(clipped):
            if rep is None:
                continue
            rc = list(rep.coords)
            for end in (Point(rc[0]), Point(rc[-1])):
                if pav_bnd.distance(end) <= _DEADEND_BOUNDARY_TOL_M:
                    continue                       # reaches a pavement edge
                joined = False
                for j, other in enumerate(clipped):
                    if j == i or other is None:
                        continue
                    if other.distance(end) <= _JOIN_TOL_M:
                        joined = True
                        break
                if joined:
                    continue                       # a junction node, not a tip
                _, bpt = nearest_points(end, pav_bnd)
                if end.distance(bpt) > _KEYHOLE_MAX_SPUR_M:
                    continue                       # too far — would seam apron
                spur = LineString([(end.x, end.y), (bpt.x, bpt.y)])
                cut_lines.append(spur)
                if collect_spurs is not None:
                    collect_spurs.append(("deadend", spur))

    # ── HOLE KEYHOLES (user 2026-07-03) ──────────────────────────────
    # polygonize assigns an unconnected hole ring as an INTERIOR ring of
    # the surrounding face; the OSM emit drops interior rings (rect-era
    # X-Plane compat — the rects that used to punch the holes covered
    # them), and the rect-era hole decomposer runs in junction_emit,
    # which the slice bypasses.  ONE cut from each hole ring to the
    # nearest SPINE line (else the outer boundary) splits every annulus
    # into SIMPLE faces that wrap the hole — the hole survives as
    # unpaved ground and the surrounding pavement emits correctly.
    from shapely.ops import nearest_points as _np2
    _live_cl = [c for c in clipped if c is not None]
    try:
        _cl_union = unary_union(_live_cl) if _live_cl else None
    except Exception:
        _cl_union = None
    def _hole_spur(_hole_ls, _from_pt, _piece):
        """Shortest in-pavement connection from ``_from_pt`` (on the hole
        ring) to the nearest spine line, else the piece exterior."""
        for _target in (_cl_union, _piece.exterior):
            if _target is None or _target.is_empty:
                continue
            try:
                _b = _np2(_from_pt, _target)[1]
            except Exception:
                continue
            if _from_pt.distance(_b) <= 0.05:
                return False                      # already touches/noded
            _cand = LineString([(_from_pt.x, _from_pt.y), (_b.x, _b.y)])
            try:
                if _piece.buffer(0.05).covers(_cand):
                    return _cand
            except Exception:
                continue
        return None

    for _piece in (pav.geoms if pav.geom_type == "MultiPolygon" else [pav]):
        if _piece.geom_type != "Polygon":
            continue
        for _hole in _piece.interiors:
            try:
                _hole_ls = LineString(_hole.coords)
            except Exception:
                continue
            # TWO spurs per hole, from (near-)opposite sides of the ring:
            # a single cut opens the annulus into a SLIT polygon whose
            # doubled zero-width edge collapses under vertex dedup and
            # paves the hole over again (measured: 19 SPJC holes covered
            # by "simple" slit junctions).  Two cuts split it into two
            # clean simple faces — same as the rect-era guillotine.
            try:
                _a1 = _np2(_hole_ls,
                           _cl_union if _cl_union is not None
                           and not _cl_union.is_empty
                           else _piece.exterior)[0]
            except Exception:
                continue
            _s1 = _hole_spur(_hole_ls, _a1, _piece)
            if _s1 is False:
                # ring already touches the arrangement once; still add the
                # SECOND cut so no slit forms.
                _s1 = None
            elif _s1 is not None:
                cut_lines.append(_s1)
                if collect_spurs is not None:
                    collect_spurs.append(("hole", _s1))
            # antipodal point along the ring from the first attachment
            try:
                _arc0 = _hole_ls.project(_a1)
                _a2 = _hole_ls.interpolate(
                    (_arc0 + 0.5 * _hole_ls.length) % _hole_ls.length)
            except Exception:
                continue
            _s2 = _hole_spur(_hole_ls, _a2, _piece)
            if _s2 not in (None, False):
                cut_lines.append(_s2)
                if collect_spurs is not None:
                    collect_spurs.append(("hole", _s2))

    if extra_cuts:
        cut_lines.extend(c for c in extra_cuts if c is not None and not c.is_empty)

    def _polygonize(cuts):
        arrangement = union_all(cuts, grid_size=_GRID_SIZE)
        out = []
        for f in polygonize(arrangement):
            if f.is_empty or f.area < _MIN_PIECE_AREA:
                continue
            if not pav.contains(f.representative_point()):
                continue
            out.append(f)
        return out

    # Pass 1.  A "bridge" centerline (a line whose interior lies wholly inside
    # one wide face, separating nothing — a deep-interior connector or cluster)
    # is dropped by polygonize, leaving no nodes on it.  Detect those and spur
    # their interior endpoints to the nearest boundary so they become separating
    # edges in pass 2 (the keyhole generalised from dead-ends to any buried end).
    if keyholes:
        from shapely.ops import nearest_points, unary_union as _uu
        raw = _polygonize(cut_lines)
        face_bnd = _uu([f.exterior for f in raw]) if raw else None
        pav_bnd = pav.boundary
        extra = []
        if face_bnd is not None:
            for rep in clipped:
                if rep is None:
                    continue
                mid = rep.interpolate(0.5, normalized=True)
                if face_bnd.distance(mid) <= _ON_TOL_M:
                    continue                       # already an edge somewhere
                rc = list(rep.coords)
                for end in (Point(rc[0]), Point(rc[-1])):
                    if pav_bnd.distance(end) <= _DEADEND_BOUNDARY_TOL_M:
                        continue
                    _, bpt = nearest_points(end, pav_bnd)
                    if end.distance(bpt) > _KEYHOLE_MAX_SPUR_M:
                        continue
                    spur = LineString([(end.x, end.y), (bpt.x, bpt.y)])
                    extra.append(spur)
                    if collect_spurs is not None:
                        collect_spurs.append(("bridge", spur))
        cut_lines.extend(extra)

    # Final grid-snapped, re-noded arrangement → conformant faces by construction.
    faces: list[SliceFace] = [
        SliceFace(polygon=f, centerline_ids=[]) for f in _polygonize(cut_lines)]
    if debug_pts:
        from shapely.ops import unary_union as _uu2
        _dbg("final-faces", _uu2([f.polygon for f in faces]))

    # Tag each face with the centerlines that run along its boundary.
    for face in faces:
        ring = face.polygon.exterior
        for ci, cl in enumerate(clipped):
            if cl is None:
                continue
            # A centerline touches the face when a run of the face's boundary
            # lies within _ON_TOL_M of it (shared edge), not merely a crossing.
            if ring.distance(cl) <= _ON_TOL_M and cl.distance(face.polygon) <= _ON_TOL_M:
                face.centerline_ids.append(ci)

    return faces


def _osm_write(layout, entries, path: str) -> None:
    """Write ``entries`` (list of (geometry, {tag:val})) to a JOSM-readable OSM
    file, converting the layout's meter frame to lat/lon via ``m_to_ll`` so it
    overlays the emitted patch exactly.  Polygons emit their exterior + each
    hole as separate closed ways; LineStrings emit as open ways."""
    nid = [0]
    nodes: list[str] = []
    ways: list[str] = []

    def _ring(coords, tags):
        ids = []
        for (x, y) in coords:
            nid[0] -= 1
            lat, lon = layout.m_to_ll(x, y)
            nodes.append(f"  <node id='{nid[0]}' visible='true' "
                         f"lat='{lat:.9f}' lon='{lon:.9f}'/>")
            ids.append(nid[0])
        nid[0] -= 1
        wid = nid[0]
        nds = "".join(f"    <nd ref='{i}'/>\n" for i in ids)
        tg = "".join(f"    <tag k='{k}' v='{v}'/>\n" for k, v in tags.items())
        ways.append(f"  <way id='{wid}' visible='true'>\n{nds}{tg}  </way>")

    for geom, tags in entries:
        if geom is None or geom.is_empty:
            continue
        polys = getattr(geom, "geoms", None)
        if geom.geom_type in ("Polygon", "MultiPolygon"):
            for g in (polys or [geom]):
                if g.is_empty or g.geom_type != "Polygon":
                    continue
                _ring(list(g.exterior.coords), {**tags, "part": "outline"})
                for h in g.interiors:
                    _ring(list(h.coords), {**tags, "part": "hole"})
        else:
            for ln in _as_lines(geom):
                _ring(list(ln.coords), tags)

    with open(path, "w") as f:
        f.write("<?xml version='1.0' encoding='UTF-8'?>\n"
                "<osm version='0.6' generator='global_slice'>\n")
        f.write("\n".join(nodes))
        f.write("\n")
        f.write("\n".join(ways))
        f.write("\n</osm>\n")


def dump_slice_inputs_osm(layout, pav, centerlines, prefix: str,
                          *, runway_union=None) -> None:
    """Write two JOSM layers: ``<prefix>_pavement.osm`` (the pav_union outline +
    holes actually fed to the slice) and ``<prefix>_spine.osm`` (the centerlines
    + the dead-end / bridge keyhole spurs the slice will add)."""
    pav_eff = pav
    if runway_union is not None and not runway_union.is_empty:
        try:
            pav_eff = pav.difference(runway_union)
        except Exception:
            pav_eff = pav
    _osm_write(layout, [(pav_eff, {"layer": "pav_union"})],
               f"{prefix}_pavement.osm")
    # Re-derive the exact cut lines (centerlines + spurs) the slice will apply.
    spurs: list = []
    faces = build_global_slice_faces(
        pav, centerlines, runway_union=runway_union, dedup=False,
        collect_spurs=spurs)
    entries = []
    for i, cl in enumerate(centerlines):
        if cl is not None and not cl.is_empty:
            entries.append((cl, {"layer": "spine", "cl": str(i)}))
    _osm_write(layout, entries, f"{prefix}_spine.osm")
    _osm_write(layout, [(s, {"layer": "spur", "kind": k}) for k, s in spurs],
               f"{prefix}_spur.osm")
    print(f"[global_slice] dumped {prefix}_pavement.osm ({len(centerlines)} "
          f"centerline(s)) + {prefix}_spine.osm + {prefix}_spur.osm "
          f"({len(spurs)} spur(s)); faces={len(faces)}")


def slice_coverage(
    faces: list[SliceFace],
    centerlines: list[LineString],
    *,
    step: float = SPINE_STEP_M,
) -> tuple[float, float]:
    """(covered_len, total_len) of ``centerlines`` by face vertices within
    ``_ON_TOL_M`` — the same metric ``tools/spine_coverage.py`` reports, but
    computed directly from the faces (no full build)."""
    verts: list[tuple[float, float]] = []
    for face in faces:
        verts.extend(face.polygon.exterior.coords)
        for hole in face.polygon.interiors:
            verts.extend(hole.coords)
    cover_r = 0.75 * step
    total = 0.0
    covered = 0.0
    for cl in centerlines:
        if cl is None or cl.is_empty or cl.length < 1.0:
            continue
        L = cl.length
        total += L
        intervals = []
        for (x, y) in verts:
            p = Point(x, y)
            if cl.distance(p) > _ON_TOL_M:
                continue
            a = cl.project(p)
            intervals.append((max(0.0, a - cover_r), min(L, a + cover_r)))
        intervals.sort()
        if not intervals:
            continue
        ca, cb = intervals[0]
        for a, b in intervals[1:]:
            if a <= cb:
                cb = max(cb, b)
            else:
                covered += cb - ca
                ca, cb = a, b
        covered += cb - ca
    return covered, total

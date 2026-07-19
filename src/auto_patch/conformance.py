"""Boundary-conformance invariant for the emitted patch.

DESIGN REQUIREMENT (user, long-standing): the emitted pavement shapes must
form a CONFORMING planar partition — adjacent shapes share *identical*
vertices along common edges, and no two shapes' edges cross.  "No area
overlap" (``test_no_self_overlap``) is necessary but NOT sufficient: two
shapes can be area-disjoint yet share a boundary non-conformingly (a
T-junction — one shape has a vertex mid-edge that its neighbour lacks).

Why it matters: Ortho4XP feeds the patch to Triangle4XP as constraint
edges.  A T-junction or crossing forces the constrained triangulation to
node the arrangement, spraying near-degenerate sub-cm² sliver triangles
along the seam.  At HECA this turned ~119k healthy airport triangles into
~2.36M (94 % sub-1 m²) and a 9m40s X-Plane load.  Area-overlap checks are
blind to it because a T-junction's intersection is a zero-area line.

This module both DETECTS violations (the runtime invariant + test gate)
and ENFORCES conformance (insert each neighbour vertex that lies on a
shape's edge, so shared boundaries become vertex-identical).  Run at the
end of the pipeline for EVERY airport, not just baselines.
"""
from __future__ import annotations

import math
from collections import defaultdict

import O4_UI_Utils as UI
from shapely.geometry import LineString
from shapely.strtree import STRtree

from .layout import (
    BuiltShape,
    PavementLayout,
    SHARED_VERTEX_TOL_M,
    VERTEX_ALT_MERGE_TOL_M,
    corner_alts_from_high_low,
)

__all__ = [
    "find_conformance_violations",
    "enforce_conformance",
    "planarize_airside",
    "CONFORMANCE_TOL_M",
]

# Perpendicular distance under which a neighbour vertex is considered "on"
# a shape's edge (a T-junction to be inserted).  Matches the shared-vertex
# snap tolerance so a point already treated as a shared corner elsewhere is
# treated consistently here.
CONFORMANCE_TOL_M = SHARED_VERTEX_TOL_M

# Refs whose footprints intentionally OVERLAY other pavement rather than
# tiling with it, so they are exempt from the conformance partition.  The
# DEM bridge is a wide transition strip laid alongside/over the perimeter
# band; it is trimmed against pavement (no area overlap) but is not part
# of the airside constraint partition in the same way.
#
# NOTE (user 2026-05-22): the airport-boundary RIBBON (``ref ==
# "airport_boundary"``) is NO LONGER exempt.  It now lies entirely inside
# row-130 and pavement is clipped back to its inner edge
# (``_clip_pavement_to_boundary_interior``), so the ribbon and pavement
# must form a conforming partition — sharing seam nodes bidirectionally —
# or Triangle4XP nodes the seam into slivers.
# Wingtip / RESA clearance cuts (``ref == "surface_clearance"``) are
# terrain-grading overlays laid alongside pavement with a built-in gap
# (they share no edge with pavement), so — like the DEM bridge — they
# are not part of the airside conforming partition.
_OVERLAY_REFS = {"boundary_dem_bridge", "surface_clearance"}


def _open_ring(poly):
    """Exterior ring coords without the closing duplicate, or None."""
    if poly is None or poly.is_empty or poly.geom_type != "Polygon":
        return None
    coords = list(poly.exterior.coords)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return coords if len(coords) >= 3 else None


def _vertex_alts(shape, n):
    """Per-(open-ring-)vertex altitudes for ``shape`` (length ``n``), or
    None if the shape carries no usable altitude model.

    Used so an inserted vertex can be given the linearly-interpolated
    altitude along its edge, and the shape re-emitted as ``node_altitudes``
    when it was a sloped quad (which only supports exactly 4 corners)."""
    na = shape.node_altitudes
    if na is not None:
        a = list(na)
        if len(a) == n + 1:      # includes closing repeat
            a = a[:-1]
        if len(a) == n:
            return a
        return None
    if (shape.altitude_high is not None
            and shape.altitude_low is not None and n == 4):
        return corner_alts_from_high_low(
            shape.altitude_high, shape.altitude_low)
    if shape.altitude is not None:
        return [shape.altitude] * n
    return None


def _make_insert_altitude(layout, elig):
    """Build THE altitude rule for a vertex INSERTED on a shape's edge —
    shared by all three insert passes (``enforce_conformance``,
    ``_resolve_edge_crossings``, ``_resolve_yielding_tjunctions``).

    The historical rule was a plain lerp of the host edge's emitted
    altitudes.  That lerp is CROWN-UNAWARE: the spine crown (part 30)
    emits ``z = z′ − c`` where ``c`` is a designed per-node drop, so a
    host edge whose endpoints carry DIFFERENT drops is not linear in
    emitted z — lerping across it re-derives a value the solver never
    produced.  Measured at HECA (de-seg residual A2): the final weld
    re-inserted a junction vertex the solver had placed at 136.298 into a
    neighbour's crown-discontinuous edge, lerped 136.415, and the emit
    consensus averaged the two claims to 136.36 — a 3.57 % within-shape
    pair against the crowned runway-edge vertex 2.52 m away.

    Resolution order:

    1. COINCIDENT-ADOPT — the receiver is a SOFT airside shape (the
       law's ``grade_graph.SOFT_VISIBILITY_ROLES`` family, the shapes the
       solver value-welds) and the insert point interns — through the
       canonical-point registry's own radius rule, the SAME rule
       ``layout.to_osm`` assigns OSM node identity by — to a canonical
       node carrying an already-emitted altitude on another eligible
       ring: adopt that altitude, so the welded node keeps ONE value
       (generalizes the overlay ``donor_alt`` path — a T-junction insert
       IS another shape's vertex by construction).  Guards, measured at
       KCLT (within 9 → 25 + runway_grade 0 → 5 without them):
         * soft receivers only — a runway / rect / skirt ring is a value
           AUTHORITY (profile plane / band law); it must keep its own
           edge interpolation, never a neighbour's claim;
         * a donor farther in VALUE than ``VERTEX_ALT_MERGE_TOL_M`` from
           the edge's own interpolation is a deliberate wall/cliff (the
           emitter's node-split rule) and is never adopted.
    2. CROWN-AWARE INTERPOLATION — the host edge's endpoints carry
       different crown drops: interpolate in uncrowned space
       ``z′ = z + c`` and subtract the insert point's own drop (the same
       transform ``crown.extend_field_to_new_ring_nodes`` applies to
       post-solve ring inserts).  ALL drop lookups are EXACT-canonical
       (a point that isn't a registered canonical node reads 0):
         * ``z′`` lerp minus a linearly-INTERPOLATED drop is
           algebraically the plain z lerp, so the transform only means
           something when the insert IS a node with its OWN field entry;
         * the validator assigns the emitted nid that same field value
           (``check_grade._crown_drops_by_nid``), so exact lookups keep
           the two readers consistent, while a radius lookup
           (``crown_drop_at``'s 0.5 m nearest) stamps a neighbouring
           node's FULL drop onto a mid-edge insert — measured at KCLT as
           0.1–0.2 m dips on the 18L/36R ring near the crown taper.
    3. Otherwise the plain lerp, expression-identical to the historical
       code path — when the adopt guards don't all hold and the endpoint
       drops are equal (``c`` cancels), the emit is byte-identical to the
       pre-fix behaviour by construction.
    """
    # No import cycle: grade_graph never imports conformance.
    from .grade_graph import SOFT_VISIBILITY_ROLES

    soft_roles = frozenset(SOFT_VISIBILITY_ROLES)
    registry = getattr(layout, "canonical_points", None)
    donor_memo: dict = {"map": None}

    def _donor_values():
        """Canonical point → list of already-emitted altitudes at ring
        vertices interning to it.  Built lazily (only when a pass
        actually inserts a vertex).  Keyed through the registry's OWN
        radius rule — the emit interns node identity the same way
        (``layout.to_osm`` → ``registry.get_or_add``), so vertices that
        will become ONE OSM node pool their claims here."""
        if donor_memo["map"] is None:
            by_cp: dict = {}
            if registry is not None:
                for s2 in elig:
                    ring2 = _open_ring(s2.polygon)
                    if ring2 is None:
                        continue
                    alts2 = _vertex_alts(s2, len(ring2))
                    if alts2 is None:
                        continue
                    for (dx, dy), da in zip(ring2, alts2):
                        if da is None:
                            continue
                        cp = registry.find_nearest(dx, dy, registry.tol_m)
                        if cp is not None:
                            by_cp.setdefault(cp, []).append(float(da))
            donor_memo["map"] = by_cp
        return donor_memo["map"]

    def _is_canonical(x, y):
        """True when (x, y) IS a registered canonical node (exact
        identity — no radius matching; see the aliasing note)."""
        return (registry is not None
                and registry.find_nearest(x, y, registry.tol_m) == (x, y))

    def insert_altitude(receiver_role, ax, ay, alt_a, bx, by, alt_b,
                        t, px, py):
        lerp = alt_a + t * (alt_b - alt_a)
        # 1. coincident-adopt (soft receiver; node identity through the
        # registry radius — the same rule the emit interns node ids by,
        # so the adopted claim and the insert become ONE emitted node).
        if receiver_role in soft_roles and registry is not None:
            cp = registry.find_nearest(px, py, registry.tol_m)
            if cp is not None:
                donors = _donor_values().get(cp)
                if donors:
                    adopted = min(donors, key=lambda v: abs(v - lerp))
                    if abs(adopted - lerp) <= VERTEX_ALT_MERGE_TOL_M:
                        return adopted
        # 2. crown-aware interpolation across a drop discontinuity — ONLY
        # when all three points are provably canonical nodes, the one
        # case where their field drops are exact for both readers.  Any
        # non-canonical point falls through to the plain lerp (a radius
        # drop lookup stamps a neighbouring node's FULL drop onto a
        # mid-edge insert — measured at KCLT as 0.1-0.2 m dips on the
        # 18L/36R ring near the crown taper; a false 0-drop reading
        # would flip the discontinuity test the other way).
        field = getattr(layout, "_crown_drop_key", None)
        if (field and _is_canonical(ax, ay) and _is_canonical(bx, by)
                and _is_canonical(px, py)):
            c_a = field.get((ax, ay), 0.0)
            c_b = field.get((bx, by), 0.0)
            if c_a != c_b:
                c_p = field.get((px, py), 0.0)
                return (alt_a + c_a) + t * ((alt_b + c_b)
                                            - (alt_a + c_a)) - c_p
        # 3. plain lerp — byte-identical to the historical behaviour.
        return lerp

    return insert_altitude


def _eligible(shape):
    p = getattr(shape, "polygon", None)
    if p is None or p.is_empty or p.geom_type != "Polygon":
        return False
    return getattr(shape, "ref", None) not in _OVERLAY_REFS


def _build_vertex_index(shapes):
    """Return (cell_size, grid) where grid maps a coarse cell to the list
    of (x, y) vertices in it, for fast 'points near an edge' queries."""
    cell = 5.0  # metres
    grid = defaultdict(list)
    for s in shapes:
        ring = _open_ring(s.polygon)
        if ring is None:
            continue
        for x, y in ring:
            grid[(int(x / cell), int(y / cell))].append((x, y))
    return cell, grid


def _points_near_edge(grid, cell, ax, ay, bx, by, tol):
    """Yield distinct (x, y) grid vertices within ``tol`` of segment a-b's
    bounding band (a cheap superset; precise test done by the caller)."""
    minx, maxx = (ax, bx) if ax <= bx else (bx, ax)
    miny, maxy = (ay, by) if ay <= by else (by, ay)
    i0 = int((minx - tol) / cell)
    i1 = int((maxx + tol) / cell)
    j0 = int((miny - tol) / cell)
    j1 = int((maxy + tol) / cell)
    seen = set()
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            for pt in grid.get((i, j), ()):
                if pt not in seen:
                    seen.add(pt)
                    yield pt


def _tjunctions_on_edge(ax, ay, bx, by, candidates, tol):
    """Return [(t, (px, py)), ...] for candidate points lying on the OPEN
    segment a-b (perpendicular dist < tol, projection strictly interior,
    not within tol of either endpoint)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return []
    L = math.sqrt(L2)
    out = []
    for px, py in candidates:
        t = ((px - ax) * dx + (py - ay) * dy) / L2
        if t <= 0.0 or t >= 1.0:
            continue
        # perpendicular distance
        perp = abs((px - ax) * dy - (py - ay) * dx) / L
        if perp >= tol:
            continue
        # not coincident with an endpoint
        if t * L < tol or (1.0 - t) * L < tol:
            continue
        out.append((t, (px, py)))
    out.sort()
    return out


def find_conformance_violations(shapes, tol=CONFORMANCE_TOL_M):
    """Detect conformance violations among emitted shapes.

    Returns ``(t_junctions, crossings)`` where each is a list of
    ``(x, y)`` locations: a T-junction is a vertex of one shape lying on
    the interior of another shape's edge; a crossing is two shapes' edges
    intersecting at an interior point of both.  An empty result means the
    patch is a conforming partition (the invariant holds).
    """
    elig = [s for s in shapes if _eligible(s)]
    cell, grid = _build_vertex_index(elig)
    own = []          # set of own-ring vertices per shape (to exclude)
    rings = []
    for s in elig:
        ring = _open_ring(s.polygon)
        rings.append(ring)
        own.append(set(ring) if ring else set())

    t_junctions = []
    for ring, ownset in zip(rings, own):
        if ring is None:
            continue
        n = len(ring)
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            cands = [pt for pt in _points_near_edge(
                        grid, cell, ax, ay, bx, by, tol)
                     if pt not in ownset]
            for _, pt in _tjunctions_on_edge(ax, ay, bx, by, cands, tol):
                t_junctions.append(pt)

    # Crossings via edge STRtree.
    edges = []
    for ring in rings:
        if ring is None:
            continue
        n = len(ring)
        for i in range(n):
            a = ring[i]
            b = ring[(i + 1) % n]
            if a != b:
                edges.append((a, b))
    lines = [LineString([a, b]) for a, b in edges]
    crossings = []
    if lines:
        tree = STRtree(lines)
        for i, ln in enumerate(lines):
            for j in tree.query(ln):
                if j <= i:
                    continue
                a0, a1 = edges[i]
                b0, b1 = edges[j]
                if {a0, a1} & {b0, b1}:
                    continue          # share an endpoint: not a crossing
                inter = ln.intersection(lines[j])
                if inter.geom_type == "Point":
                    px = (inter.x, inter.y)
                    if px not in (a0, a1, b0, b1):
                        crossings.append(px)
    return t_junctions, crossings


def enforce_conformance(layout: "PavementLayout",
                        tol=CONFORMANCE_TOL_M,
                        owner_roles: "set[str] | None" = None,
                        include_overlay_refs: bool = False,
                        ) -> tuple[int, int]:
    """Make the emitted shapes a conforming partition by inserting, into
    each shape's edges, every NEIGHBOUR vertex that lies on that edge
    (a T-junction).  The inserted vertex takes the edge's linearly
    interpolated altitude; a sloped-quad shape that gains a vertex is
    converted to ``node_altitudes`` (it can no longer be a 4-corner quad).

    ``owner_roles``: when given, only shapes whose role is in this set may
    RECEIVE inserted vertices (every eligible shape still contributes its
    vertices as candidates).  Used for the PRE-SOLVE pass: conform only
    apron/junction edges so abutting aprons share a canonical node and the
    solver grades them to match — WITHOUT inserting vertices into taxi-rect
    sloping edges (which would break their 4-corner planar form before the
    solver assigns altitudes).

    Returns ``(shapes_modified, vertices_inserted)``.  Idempotent: a second
    call inserts nothing.  Overlay refs (DEM bridge / clearance) are skipped
    unless ``include_overlay_refs`` — their "built-in gap" premise does not
    always hold (a bridge vertex CAN land exactly on a pavement edge, and an
    unwelded on-edge node tears Triangle4XP's triangulation), so the FINAL
    post-solve weld pass includes them.
    """
    elig = [s for s in layout.shapes
            if (_eligible(s) or (include_overlay_refs
                                 and getattr(s, "polygon", None) is not None
                                 and not s.polygon.is_empty
                                 and s.polygon.geom_type == "Polygon"))]
    cell, grid = _build_vertex_index(elig)
    # Donor altitudes, for OVERLAY receivers only: a vertex welded into a
    # DEM-bridge / clearance edge must ADOPT the donor's altitude (the two
    # coincident nodes would otherwise emit metres apart — the very tear the
    # weld exists to fix).  Airside receivers keep edge interpolation
    # (surface-neutral; the solver is authoritative there).
    donor_alt: dict = {}
    if include_overlay_refs:
        for s2 in elig:
            ring2 = _open_ring(s2.polygon)
            if ring2 is None:
                continue
            alts2 = _vertex_alts(s2, len(ring2))
            if alts2 is None:
                continue
            for (dx, dy), da in zip(ring2, alts2):
                if da is not None:
                    donor_alt[(dx, dy)] = float(da)
    shapes_modified = 0
    vertices_inserted = 0
    insert_altitude = _make_insert_altitude(layout, elig)
    from shapely.geometry import Polygon

    for s in elig:
        if owner_roles is not None and (s.role or "") not in owner_roles:
            continue
        ring = _open_ring(s.polygon)
        if ring is None:
            continue
        n = len(ring)
        ownset = set(ring)
        alts = _vertex_alts(s, n)
        # A shape emitted with a SINGLE ``altitude`` (no high/low, no
        # node_altitudes) is flat: every corner sits at that level, so a
        # vertex inserted on an edge between two equal-altitude corners is
        # also at that level — the shape stays flat.  Keep the single
        # ``altitude`` instead of converting to ``node_altitudes`` (which
        # for a TERMINAL would violate H26's flat-only rule — the HECA
        # terminal10 case — and is redundant for any other flat shape).
        flat_single_alt = (s.node_altitudes is None
                           and s.altitude_high is None
                           and s.altitude_low is None
                           and s.altitude is not None)
        # Build the new ring edge by edge, inserting T-junction points.
        new_ring = []
        new_alts = [] if alts is not None else None
        inserted_here = 0
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            new_ring.append((ax, ay))
            if new_alts is not None:
                new_alts.append(alts[i])
            cands = [pt for pt in _points_near_edge(
                        grid, cell, ax, ay, bx, by, tol)
                     if pt not in ownset]
            tjs = _tjunctions_on_edge(ax, ay, bx, by, cands, tol)
            _recv_overlay = getattr(s, "ref", None) in _OVERLAY_REFS
            for t, (px, py) in tjs:
                # A candidate near a shallow corner can qualify on TWO
                # edges of this ring; inserting it twice self-touches
                # the ring, the rebuild goes invalid, and the bail
                # below used to discard EVERY insertion for the shape
                # (the immortal-T-vertex class: dense welded rings
                # never conformed).  First edge wins.
                if (px, py) in ownset:
                    continue
                ownset.add((px, py))
                new_ring.append((px, py))
                if new_alts is not None:
                    _da = (donor_alt.get((px, py))
                           if _recv_overlay else None)
                    if _da is not None:
                        new_alts.append(_da)
                    else:
                        new_alts.append(insert_altitude(
                            s.role, ax, ay, alts[i],
                            bx, by, alts[(i + 1) % n],
                            t, px, py))
                inserted_here += 1
        if not inserted_here:
            continue
        # Rebuild the polygon; bail (leave shape untouched) if invalid —
        # LOUDLY: a bailed shape keeps every T-vertex it should have
        # welded, and the un-welded nodes Ruppert-explode the tile mesh.
        # Interior rings MUST ride along: an exterior-only rebuild fills
        # the shape's holes, silently covering whatever shape occupies
        # them (SPJC: gap_pit_floor over an adjacent_ground strip inside
        # its hole, 31.86 m² — the zero-tolerance self-overlap invariant).
        try:
            new_poly = Polygon(new_ring, [list(r.coords)
                                          for r in s.polygon.interiors])
            if not new_poly.is_valid or new_poly.is_empty:
                import O4_UI_Utils as UI
                UI.vprint(1,
                    f"  [conformance] WARN: {s.role}/"
                    f"{getattr(s, 'ref', None)}: rebuilt ring invalid "
                    f"after {inserted_here} T-vertex insert(s) — shape "
                    f"left UNWELDED (mesh-sliver risk).")
                continue
        except Exception:
            continue
        s.polygon = new_poly
        if new_alts is not None and not flat_single_alt:
            # node_altitudes carries the closing repeat.
            s.node_altitudes = new_alts + [new_alts[0]]
            s.altitude_high = None
            s.altitude_low = None
        # flat_single_alt: leave s.altitude as-is (the new vertex inherits
        # it); the shape stays flat and keeps the single-altitude model.
        shapes_modified += 1
        vertices_inserted += inserted_here
    return shapes_modified, vertices_inserted


def _param_on_edge(ax, ay, bx, by, px, py) -> float:
    """Parameter t∈[0,1] of the foot of ``(px,py)`` on segment a→b."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return -1.0
    return ((px - ax) * dx + (py - ay) * dy) / L2


def _resolve_edge_crossings(layout: "PavementLayout") -> int:
    """Insert each edge–edge intersection point as a shared vertex in BOTH
    crossing shapes.

    A "crossing" is two shapes' edges meeting at an interior point of both.
    The intersection point lies EXACTLY on both edges, so inserting it splits
    each edge there without moving it (shape-preserving, no bend, no area
    change) — and because both shapes now have a vertex at that point, it is a
    shared endpoint, not an interior crossing, so the conformance invariant
    stops flagging it.  The ≤noise-area sliver of a near-tangent crossing stays
    below the overlap-clip's floor.  Returns the number of vertices inserted."""
    from shapely.geometry import Polygon

    elig = [s for s in layout.shapes if _eligible(s)]
    rings = [_open_ring(s.polygon) for s in elig]
    edges: list[tuple[tuple, tuple]] = []
    meta: list[tuple[int, int]] = []           # (shape_idx, edge_idx_in_ring)
    for si, ring in enumerate(rings):
        if ring is None:
            continue
        n = len(ring)
        for i in range(n):
            a, b = ring[i], ring[(i + 1) % n]
            if a != b:
                edges.append((a, b))
                meta.append((si, i))
    if not edges:
        return 0
    lines = [LineString([a, b]) for a, b in edges]
    tree = STRtree(lines)
    # inserts[shape_idx][edge_idx] -> [(t, point)]
    inserts: dict = defaultdict(lambda: defaultdict(list))
    for ei, ln in enumerate(lines):
        for ej in tree.query(ln):
            if ej <= ei:
                continue
            a0, a1 = edges[ei]
            b0, b1 = edges[ej]
            if {a0, a1} & {b0, b1}:
                continue                       # share an endpoint: not a crossing
            try:
                inter = ln.intersection(lines[ej])
            except Exception:
                continue
            if inter.geom_type != "Point":
                continue
            X = (inter.x, inter.y)
            if X in (a0, a1, b0, b1):
                continue
            si, i = meta[ei]
            sj, j = meta[ej]
            ta = _param_on_edge(a0[0], a0[1], a1[0], a1[1], X[0], X[1])
            tb = _param_on_edge(b0[0], b0[1], b1[0], b1[1], X[0], X[1])
            if 0.0 < ta < 1.0:
                inserts[si][i].append((ta, X))
            if 0.0 < tb < 1.0:
                inserts[sj][j].append((tb, X))
    if not inserts:
        return 0

    n_inserted = 0
    insert_altitude = _make_insert_altitude(layout, elig)
    for si, by_edge in inserts.items():
        shape = elig[si]
        ring = rings[si]
        if ring is None:
            continue
        n = len(ring)
        alts = _vertex_alts(shape, n)
        flat_single_alt = (shape.node_altitudes is None
                           and shape.altitude_high is None
                           and shape.altitude_low is None
                           and shape.altitude is not None)
        new_ring: list = []
        new_alts: list | None = [] if alts is not None else None
        added_here = 0
        for i in range(n):
            new_ring.append(ring[i])
            if new_alts is not None:
                new_alts.append(alts[i])
            if i in by_edge:
                seen: set = set()
                a_i = alts[i] if alts is not None else None
                a_j = alts[(i + 1) % n] if alts is not None else None
                (ax, ay) = ring[i]
                (bx, by) = ring[(i + 1) % n]
                for t, X in sorted(by_edge[i]):
                    key = round(t, 4)
                    if key in seen:
                        continue
                    seen.add(key)
                    new_ring.append(X)
                    if new_alts is not None:
                        new_alts.append(insert_altitude(
                            shape.role, ax, ay, a_i, bx, by, a_j,
                            t, X[0], X[1]))
                    added_here += 1
        if not added_here:
            continue
        try:
            # Interior rings ride along (exterior-only fills the holes).
            new_poly = Polygon(new_ring + [new_ring[0]],
                               [list(r.coords)
                                for r in shape.polygon.interiors])
            if not new_poly.is_valid or new_poly.is_empty:
                continue
        except Exception:
            continue
        shape.polygon = new_poly
        if new_alts is not None and not flat_single_alt:
            shape.node_altitudes = new_alts + [new_alts[0]]
            shape.altitude_high = None
            shape.altitude_low = None
        n_inserted += added_here
    return n_inserted


# A T-junction whose vertex sits within this perpendicular distance of the edge
# is COLLINEAR (a clip ``difference()`` plants vertices exactly on the edge):
# inserting it bends the edge by ≤ this, so it is effectively shape-preserving
# and cannot create a >noise overlap.  Vertices farther off the edge are real
# near-misses left alone (inserting them would bend an edge into a neighbour —
# the 13.5 m² overlap the 0.5 m insert produced).
_PLANARIZE_INSERT_TOL_M = float(__import__("os").environ.get(
    "O4_PLANARIZE_INSERT_TOL_M", "0.05"))


# Overlap-priority tiers (mirrors elevation._drop_overlap_against_fixed_shapes):
# a LOWER number yields to nothing; a shape may conform (bend) to a vertex of a
# STRICTLY-higher-priority shape with no overlap risk, because that vertex sits
# on the higher shape's fixed boundary.  Bending toward a SAME/lower-tier peer
# can push an edge into it (the 3 m² junction×junction overlap), so those are
# left to the collinear-only insert.
_OVERLAP_TIER = {
    "runway": 0, "runway_crossing": 0, "building": 1,
    "primary_parallel": 2, "secondary_parallel": 2, "stub": 2,
    "cross_connector": 2, "service_road": 2,
    "junction": 3, "apron": 3, "service_junction": 3, "boundary": 4,
}


def _tier(role) -> int:
    return _OVERLAP_TIER.get(role or "", 3)


def _resolve_yielding_tjunctions(layout: "PavementLayout", tol: float) -> int:
    """Insert, into each shape B's edge, every nearby vertex of a STRICTLY
    higher-priority shape A (``tier(A) < tier(B)``) within ``tol`` — B yields
    (conforms) to A's fixed boundary, so the bend cannot overlap A.  This clears
    runway/rect-vertex-on-junction near-misses that the collinear insert leaves.
    Returns the number of vertices inserted."""
    from shapely.geometry import Polygon
    elig = [s for s in layout.shapes if _eligible(s)]
    rings = [_open_ring(s.polygon) for s in elig]
    cell, grid = _build_vertex_index(elig)
    # coord -> highest priority (lowest tier) among shapes owning it
    vtx_tier: dict = {}
    for s, ring in zip(elig, rings):
        if not ring:
            continue
        t = _tier(s.role)
        for v in ring:
            if t < vtx_tier.get(v, 99):
                vtx_tier[v] = t
    n_inserted = 0
    insert_altitude = _make_insert_altitude(layout, elig)
    for si, (shape, ring) in enumerate(zip(elig, rings)):
        if ring is None:
            continue
        tb = _tier(shape.role)
        n = len(ring)
        ownset = set(ring)
        alts = _vertex_alts(shape, n)
        flat_single_alt = (shape.node_altitudes is None
                           and shape.altitude_high is None
                           and shape.altitude_low is None
                           and shape.altitude is not None)
        new_ring: list = []
        new_alts: list | None = [] if alts is not None else None
        added = 0
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            new_ring.append((ax, ay))
            if new_alts is not None:
                new_alts.append(alts[i])
            cands = [pt for pt in _points_near_edge(grid, cell, ax, ay, bx, by, tol)
                     if pt not in ownset and vtx_tier.get(pt, 99) < tb]
            for t, (px, py) in _tjunctions_on_edge(ax, ay, bx, by, cands, tol):
                new_ring.append((px, py))
                if new_alts is not None:
                    new_alts.append(insert_altitude(
                        shape.role, ax, ay, alts[i],
                        bx, by, alts[(i + 1) % n],
                        t, px, py))
                added += 1
        if not added:
            continue
        try:
            # Interior rings ride along (exterior-only fills the holes).
            new_poly = Polygon(new_ring + [new_ring[0]],
                               [list(r.coords)
                                for r in shape.polygon.interiors])
            if not new_poly.is_valid or new_poly.is_empty:
                continue
        except Exception:
            continue
        shape.polygon = new_poly
        if new_alts is not None and not flat_single_alt:
            shape.node_altitudes = new_alts + [new_alts[0]]
            shape.altitude_high = None
            shape.altitude_low = None
        n_inserted += added
    return n_inserted


def planarize_airside(layout: "PavementLayout", icao: str = "",
                      max_iters: int = 6) -> tuple[int, int]:
    """Drive the conformance invariant toward ZERO, SHAPE-PRESERVINGLY, so it is
    safe as the FINAL (post-solve) geometry pass.

    Iterates two insert-only steps until stable:
      * ``enforce_conformance`` with a TIGHT tolerance — inserts only COLLINEAR
        T-junction vertices (≤ ``_PLANARIZE_INSERT_TOL_M`` off the edge), so no
        edge bends into a neighbour (no overlap), and
      * ``_resolve_edge_crossings`` — inserts each edge-intersection point on
        both edges (the point lies exactly on both, so zero bend).
    Both interpolate inserted-vertex altitudes from the edge, so running this
    AFTER the solve introduces no cliffs.  Returns the residual
    ``(t_junctions, crossings)`` at the CHECK tolerance."""
    tj_total = cr_total = 0
    for _ in range(max_iters):
        _, nv = enforce_conformance(layout, tol=_PLANARIZE_INSERT_TOL_M)
        # A lower-tier shape may also conform to a strictly-higher-tier shape's
        # vertex at the full CHECK tolerance (it bends onto a fixed boundary, so
        # no overlap) — clears runway/rect-on-junction near-misses.
        nv += _resolve_yielding_tjunctions(layout, tol=CONFORMANCE_TOL_M)
        nc = _resolve_edge_crossings(layout)
        tj_total += nv
        cr_total += nc
        if nv == 0 and nc == 0:
            break
    tj, cr = find_conformance_violations(layout.shapes)
    if tj_total or cr_total:
        try:
            UI.vprint(1,
                f"  [pav-builder] {icao}: planarized airside — inserted "
                f"{tj_total} T-junction + {cr_total} crossing vertex(es); "
                f"residual {len(tj)} T-junction(s), {len(cr)} crossing(s).")
        except Exception:
            pass
    return len(tj), len(cr)


def densify_long_edges(layout, roles, max_edge_m: float = 60.0) -> int:
    """Insert mid-edge vertices on over-long exterior-ring edges of the
    given roles BEFORE the solve (user in-sim finding 2026-07-09: a
    construction-born 1,279 m junction edge gave the solver nothing to
    hold the pavement edge with, and the mesh sagged between the distant
    nodes toward the neighbouring graded strips).  Pre-solve: inserted
    vertices become solver nodes, so the edge profile is LAW-solved, not
    interpolated.  ``node_altitudes``, when present, gain the linear
    interpolation to stay index-aligned.  Returns vertices inserted."""
    import math as _math
    from shapely.geometry import Polygon as _Polygon
    inserted = 0
    for s in layout.shapes:
        if (s.role not in roles or s.polygon is None
                or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        ring = list(s.polygon.exterior.coords)
        closed = bool(ring) and ring[0] == ring[-1]
        if closed:
            ring = ring[:-1]
        n = len(ring)
        if n < 3:
            continue
        alts = None
        if s.node_altitudes and len(s.node_altitudes) >= n:
            alts = list(s.node_altitudes[:n])
        new_ring = []
        new_alts = [] if alts is not None else None
        changed = False
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            new_ring.append((ax, ay))
            if new_alts is not None:
                new_alts.append(alts[i])
            L = _math.hypot(bx - ax, by - ay)
            if L <= max_edge_m:
                continue
            cuts = int(_math.ceil(L / max_edge_m))
            for k in range(1, cuts):
                t = k / cuts
                new_ring.append((ax + (bx - ax) * t,
                                 ay + (by - ay) * t))
                if new_alts is not None:
                    a0 = alts[i]
                    a1 = alts[(i + 1) % n]
                    new_alts.append(
                        a0 + (a1 - a0) * t
                        if a0 is not None and a1 is not None
                        else a0)
                inserted += 1
                changed = True
        if not changed:
            continue
        try:
            # Interior rings ride along (exterior-only fills the holes).
            poly = _Polygon(new_ring, [list(r.coords)
                                       for r in s.polygon.interiors])
            if not poly.is_valid or poly.is_empty:
                continue
        except Exception:
            continue
        s.polygon = poly
        if new_alts is not None:
            s.node_altitudes = new_alts + [new_alts[0]]
    return inserted

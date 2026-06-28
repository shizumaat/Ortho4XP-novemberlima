"""THE single within-shape grade-constraint graph (solver AND validator).

This module is the ONE place that decides *which* vertex pairs of a soft airside
shape are grade-constrained and *at what cap*.  Both the elevation solver
(pre-emit, from ``layout.shapes``) and the grade validator (post-emit, from the
OSM ways) build the same representation-agnostic input and call
:func:`build_grade_constraints` — so the surface we *build* and the surface we
*check* can never drift again (see ``docs/single_grade_graph.md``).

It is deliberately self-contained and clean-room: it does NOT import the legacy
``check_grade.iter_shape_grade_constraints`` / per-axis machinery.

Model (user, authoritative 2026-06-23) — a soft airside shape is **spine + body**:

* **spine**  = taxi centerline(s) through the shape (after ``junction_spine``
  slicing the centerline is a real shared edge with nodes ON it).  A pair of
  spine nodes on a COMMON centerline is graded at the **taxiway per-letter cap**
  (A/B 3 %, C–F 1.5 %) — the centerline is a taxiway even inside an apron/junction.
* **body**  = every other mutually-visible pair, graded at the shape's **body
  cap**:
    - apron   → ``APRON_MAX_GRADE`` (1 %),
    - junction → the taxiway per-letter cap of its spine (so a junction is uniform
      at the taxiway cap; a junction with NO spine inherits the cap from the
      nearest connected taxiway-sized shape),
    - service_junction → ``SERVICE_ROAD_MAX_GRADE`` (4 %).

Rects (4-corner sloping planes), terminals (flat pads), runways (FAA profile) and
groundside (DEM) are NOT handled here — the solver keeps their plane/flat/profile
models (a correct planar rect already satisfies the convex all-pair check, so
they are not a lockstep gap) and the validator keeps its own per-role handling for
them.  This module owns the apron/junction visibility graph only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Hashable, Optional, Sequence

from . import grade_law as GL
from .config import (
    APRON_BACK_EDGE_GRADE,
    APRON_MAX_GRADE,
    APRON_TAXI_BLEND,
    APRON_TAXI_TRANSITION_M,
    ELEV_ROUNDING_NOISE_M,
    GRADE_VISIBILITY_BUFFER_M as _VIS_BUF,
    SERVICE_ROAD_MAX_GRADE,
    TAXI_MAX_GRADE,
    taxi_grade_cap_for_letter,
)

# Roles this module owns (the visibility-graph soft airside shapes).
APRON_ROLE = "apron"
JUNCTION_ROLES = ("junction", "service_junction")
SOFT_VISIBILITY_ROLES = (APRON_ROLE,) + JUNCTION_ROLES

# A ring vertex counts as a SPINE node of a centerline when it lies within this
# perpendicular distance of it.  Post-slice the spine nodes sit exactly on the
# line, so this is tight (it only has to absorb float/round noise, not width).
SPINE_PERP_TOL_M = 1.0

# The per-pair eligibility/cap decision (min-pair-dist, apron body-chord max,
# seam/building/spine/visibility skips, cap selection) is THE LAW — it lives in
# ``grade_law`` so the solver and the grade test share one source.  This module
# is the solver-side reader: it builds a ``grade_law.PairContext`` per pair and
# calls ``grade_law.classify_pair``.


@dataclass
class Centerline:
    """One taxi route centerline through the airport, in the SAME meter frame as
    the shape rings the caller passes.  ``cap`` is the taxiway per-letter
    longitudinal grade cap (from ``taxi_grade_cap_for_letter``)."""
    pts: Sequence[tuple[float, float]]
    cap: float
    # cumulative arc length at each pt (filled lazily)
    _arc: Optional[list[float]] = None

    def arc(self) -> list[float]:
        if self._arc is None:
            a = [0.0]
            for i in range(1, len(self.pts)):
                a.append(a[-1] + math.hypot(self.pts[i][0] - self.pts[i - 1][0],
                                            self.pts[i][1] - self.pts[i - 1][1]))
            self._arc = a
        return self._arc


@dataclass
class GradeShape:
    """One soft airside shape, representation-agnostic.

    ``ring``  open ring (no repeated closing vertex), LOCAL meter coords.
    ``keys``  stable per-vertex key parallel to ``ring`` (OSM nid | solver idx).
    ``role``  apron | junction | service_junction.
    """
    role: str
    ring: list[tuple[float, float]]
    keys: list[Hashable]


@dataclass
class GradeContext:
    """Shared context every caller builds once from its own representation."""
    centerlines: list[Centerline]
    seam_keys: frozenset = frozenset()
    # cap to use for a junction that has NO spine of its own — the caller resolves
    # the nearest connected taxiway-sized shape's cap and passes a lookup keyed by
    # the shape's identity (id(shape) for the solver, way id for the validator).
    inherited_junction_cap: Callable[[GradeShape], float] = (
        lambda s: TAXI_MAX_GRADE)
    # node keys that sit on a BUILDING pad.  An apron/junction edge with BOTH
    # endpoints on a building is the inter-pad FRONTAGE = a building↔building
    # step (allowed by the model — adjacent pads may sit at different levels with
    # a facade/step between them), NOT an apron grade path, so it is not graded.
    # Mirrors the validator's building↔building step exemption.
    building_keys: frozenset = frozenset()


@dataclass
class ShapeConstraints:
    """The grade constraints of ONE shape: undirected edges ``(key_a, key_b,
    cap)`` plus the spine chains (ordered spine node keys) for the connecting
    solve's smooth-profile handling."""
    role: str
    edges: list[tuple[Hashable, Hashable, float]] = field(default_factory=list)
    spine_chains: list[list[Hashable]] = field(default_factory=list)


def _open_ring(coords):
    """Open ring (drop the repeated closing vertex)."""
    c = list(coords)
    return c[:-1] if c and c[0] == c[-1] else c


def build_context(layout, bucket_to_idx=None) -> "GradeContext":
    """THE single shared grade-graph context (solver + spine + validator).

    Builds the taxi centerlines (LOCAL meters, per-letter caps), the spine-less
    junction cap-inheritance lookup (nearest connected taxiway-sized rect), and
    the building-pad key set.  The route-profile solver (via
    ``build_unified_graph``) and the validator
    (``grade_graph_validate.within_violations``) both call this, so the
    centerlines, caps and inheritance can never drift (docs/single_grade_graph.md).

    ``bucket_to_idx`` selects the BUILDING-KEY space (the one place the two
    representations diverge):

      * given (solver / spine, pre-emit) → SOLVER NODE INDICES
        (``bucket_to_idx[canonical_points.get_or_add(x, y)]``), matching the
        node-idx ``keys`` those callers put on their ``GradeShape``s;
      * ``None`` (validator, post-emit) → ROUNDED-COORD tuples
        ``(round(x, 3), round(y, 3))`` — the validator keys its shapes by ring
        index and matches buildings by coordinate.
    """
    from .elevation_per_surface.solver_primitives import (
        SLOPING_RECT_ROLES, _shape_grade)
    from .layout import ROLE_BUILDING

    letters = getattr(layout, "apt_taxi_letters", {}) or {}
    cls: list[Centerline] = []
    for ln, name in (getattr(layout, "apt_taxi_centerlines", []) or []):
        if ln is None or getattr(ln, "is_empty", True):
            continue
        if name and str(name).upper().startswith("SVC"):
            continue            # service roads are NOT taxi spines (own role)
        try:
            pts = list(ln.coords)
        except Exception:
            continue
        if len(pts) >= 2:
            cls.append(Centerline(
                pts=pts, cap=taxi_grade_cap_for_letter(letters.get(name))))

    # taxiway-sized rect node coords -> cap (a junction with NO spine inherits
    # the cap of the nearest CONNECTED taxiway = a rect it shares a node with).
    rect_cap_at: dict = {}
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        cap = _shape_grade(layout, s)
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            k = (round(x, 3), round(y, 3))
            if rect_cap_at.get(k, -1.0) < cap:
                rect_cap_at[k] = cap

    def _inherited(shape):
        best = None
        for (x, y) in shape.ring:
            c = rect_cap_at.get((round(x, 3), round(y, 3)))
            if c is not None and (best is None or c > best):
                best = c
        return best if best is not None else TAXI_MAX_GRADE

    cps = getattr(layout, "canonical_points", None)
    bld_keys: set = set()
    for s in layout.shapes:
        if (s.role == ROLE_BUILDING and s.polygon is not None
                and not s.polygon.is_empty):
            for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
                if bucket_to_idx is not None and cps is not None:
                    i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                    if i is not None:
                        bld_keys.add(i)
                else:
                    bld_keys.add((round(x, 3), round(y, 3)))
    return GradeContext(centerlines=cls, inherited_junction_cap=_inherited,
                        building_keys=frozenset(bld_keys))


# ── visibility ──────────────────────────────────────────────────────────────

def _visibility_predicate(ring: list[tuple[float, float]]):
    """Return ``vis(xa,ya,xb,yb)->bool``: True iff the chord stays inside the
    ring grown by ``_VIS_BUF``.  ``None`` if shapely is unavailable / the polygon
    is degenerate (caller falls back to plain all-pair)."""
    try:
        from shapely.geometry import LineString, Polygon
        from shapely.prepared import prep
    except ImportError:  # pragma: no cover
        return None
    try:
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        poly = poly.buffer(_VIS_BUF)
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


# ── spine membership ────────────────────────────────────────────────────────

def _project(cl: Centerline, x: float, y: float):
    """Return ``(arc_pos, perp_dist)`` of (x,y) onto centerline ``cl``."""
    best_d = float("inf")
    best_a = 0.0
    arc = cl.arc()
    for i in range(len(cl.pts) - 1):
        ax, ay = cl.pts[i]
        bx, by = cl.pts[i + 1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 <= 1e-12:
            continue
        t = ((x - ax) * dx + (y - ay) * dy) / seg2
        t = max(0.0, min(1.0, t))
        px, py = ax + t * dx, ay + t * dy
        d = math.hypot(x - px, y - py)
        if d < best_d:
            best_d = d
            best_a = arc[i] + t * math.sqrt(seg2)
    return best_a, best_d


def _spine_membership(shape: GradeShape, ctx: GradeContext
                      ) -> dict[int, list[tuple[int, float]]]:
    """For each ring index, the list of (centerline-index, arc_pos) it lies on
    (within ``SPINE_PERP_TOL_M``)."""
    out: dict[int, list[tuple[int, float]]] = {}
    for ri, (x, y) in enumerate(shape.ring):
        hits = []
        for ci, cl in enumerate(ctx.centerlines):
            a, d = _project(cl, x, y)
            if d <= SPINE_PERP_TOL_M:
                hits.append((ci, a))
        if hits:
            out[ri] = hits
    return out


def _spine_crossing_predicate(shape: GradeShape, ctx: GradeContext,
                              membership: dict):
    """Return ``crosses(xa,ya,xb,yb)->bool``: True iff the chord crosses one of
    the shape's spine centerlines (so the real grade path between the two sides
    is via the spine, not the direct diagonal).  ``None`` if shapely is
    unavailable or the shape has no spine."""
    if not membership:
        return None
    cl_idx = {c for hits in membership.values() for (c, _a) in hits}
    if not cl_idx:
        return None
    try:
        from shapely.geometry import LineString
    except ImportError:  # pragma: no cover
        return None
    geoms = []
    for ci in cl_idx:
        pts = ctx.centerlines[ci].pts
        if len(pts) >= 2:
            try:
                geoms.append(LineString(pts))
            except Exception:
                pass
    if not geoms:
        return None

    def _crosses(xa, ya, xb, yb):
        try:
            ch = LineString(((xa, ya), (xb, yb)))
        except Exception:
            return False
        for g in geoms:
            if ch.crosses(g):
                return True
        return False

    return _crosses


def _shared_centerline(mi, mj) -> bool:
    """True iff ring indices i,j lie on a COMMON centerline (a spine pair)."""
    ci = {c for (c, _a) in mi}
    cj = {c for (c, _a) in mj}
    return bool(ci & cj)


# ── caps ────────────────────────────────────────────────────────────────────

def _spine_cap(membership: dict, ctx: GradeContext) -> float:
    """The taxiway cap to use for this shape's spine (max per-letter cap over the
    centerlines crossing it — the steeper code governs the corridor here)."""
    caps = [ctx.centerlines[c].cap
            for hits in membership.values() for (c, _a) in hits]
    return max(caps) if caps else TAXI_MAX_GRADE


def _body_cap(shape: GradeShape, ctx: GradeContext, membership: dict) -> float:
    if shape.role == APRON_ROLE:
        return APRON_MAX_GRADE
    if shape.role == "service_junction":
        return SERVICE_ROAD_MAX_GRADE
    # junction: taxiway cap of its spine, else inherited from the nearest
    # connected taxiway-sized shape.
    if membership:
        return _spine_cap(membership, ctx)
    return ctx.inherited_junction_cap(shape)


def _nearest_centerline(x: float, y: float, ctx: GradeContext):
    """``(dist, cap, (tx, ty))`` — the nearest taxi centerline to ``(x, y)``: its
    perpendicular distance, per-letter cap, and unit tangent at the foot point."""
    best_d, best_cap, best_t = float("inf"), APRON_MAX_GRADE, (1.0, 0.0)
    for cl in ctx.centerlines:
        pts = cl.pts
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            if seg2 <= 1e-12:
                continue
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(x - px, y - py)
            if d < best_d:
                L = math.sqrt(seg2)
                best_d, best_cap, best_t = d, cl.cap, (dx / L, dy / L)
    return best_d, best_cap, best_t


def _apron_edge_cap(xi, yi, xj, yj, ni, nj, body_cap, twist):
    """Apron cap near a taxi route (user 2026-06-25): an apron edge earns the
    route's (looser) cap as it nears the route, decaying to ``body_cap`` past
    ``APRON_TAXI_TRANSITION_M``.  ``ni``/``nj`` = ``_nearest_centerline`` at each
    endpoint.

    ``twist`` (the edge touches a building frontage): the apron WARPS to blend the
    flat pad into the climbing route — its corners slope ± to meet the route — so
    the looser cap applies in ALL directions (isotropic).  Elsewhere only the
    ALONG-route component earns it (the apron still grades ``body_cap``
    perpendicular, from its edges to the spine)."""
    d, route_cap, tan = (ni if ni[0] <= nj[0] else nj)
    # The frontage warp needs MORE than the route cap (the route's climb along the
    # pad is compressed into the apron depth), so the twist target is the
    # back-edge ramp grade; elsewhere the apron blends toward the route cap.
    target = max(route_cap, APRON_BACK_EDGE_GRADE) if twist else route_cap
    if target <= body_cap or d >= APRON_TAXI_TRANSITION_M:
        return body_cap
    dist_factor = 1.0 - d / APRON_TAXI_TRANSITION_M
    if twist:
        infl = dist_factor                               # isotropic (the warp)
    else:
        ex, ey = xj - xi, yj - yi
        el = math.hypot(ex, ey) or 1e-9
        along = abs(ex * tan[0] + ey * tan[1]) / el      # 0 (perp) .. 1 (along)
        infl = along * dist_factor
    return body_cap + (target - body_cap) * infl


# ── main ────────────────────────────────────────────────────────────────────

def shape_constraints(shape: GradeShape, ctx: GradeContext) -> ShapeConstraints:
    """The grade constraints of ONE soft airside shape (apron / junction)."""
    sc = ShapeConstraints(role=shape.role)
    ring = shape.ring
    keys = shape.keys
    n = len(ring)
    if n < 3:
        return sc
    membership = _spine_membership(shape, ctx)
    body_cap = _body_cap(shape, ctx, membership)
    vis = _visibility_predicate(ring)
    # The shape's spine centerline geometries (those it has nodes on) — a body
    # chord that CROSSES one is NOT a real grade path: the climb between the two
    # sides is carried by the SPINE at the taxiway cap (the apron grades 1% to
    # its local spine, plan §2), so the straight 1%-diagonal across the spine
    # would falsely declare a wide apron infeasible.  Drop it; the constraint
    # holds transitively through the spine.
    crosses_spine = _spine_crossing_predicate(shape, ctx, membership)
    seam = ctx.seam_keys
    bld = ctx.building_keys

    # APRON↔taxi blend: per-ring-node nearest centerline (dist, cap, tangent), so
    # an apron body edge's ALONG-route component earns the route's looser cap as
    # it nears a taxiway running through the apron (user 2026-06-25).
    near = None
    if (APRON_TAXI_BLEND and shape.role == APRON_ROLE
            and ctx.centerlines and body_cap < TAXI_MAX_GRADE):
        near = [_nearest_centerline(x, y, ctx) for (x, y) in ring]

    # Build the representation-agnostic PairContext for each pair and apply THE
    # LAW (``grade_law.classify_pair``).  The expensive visibility / spine-cross
    # predicates and the apron blend cap are passed as thunks so the law evaluates
    # them lazily (only for pairs surviving the cheap skips) — the same
    # short-circuiting the legacy in-line loop had.  ``classify_pair`` returns an
    # ``Allowance``; every current rule is isotropic, so ``flat_cap()`` recovers
    # the legacy scalar ``(key_a, key_b, cap)`` edge exactly.  The per-edge spine
    # cap (a taxi route keeps its own per-letter cap inside a junction) and the
    # per-letter blend are encoded as the ``spine_caps`` / ``blend_cap_fn`` inputs.
    for i in range(n):
        xi, yi = ring[i]
        ki = keys[i]
        mi = membership.get(i)
        ki_bld = ki in bld
        for j in range(i + 1, n):
            kj = keys[j]
            if ki == kj:
                continue
            xj, yj = ring[j]
            d = math.hypot(xi - xj, yi - yj)
            ring_adjacent = (j == i + 1) or (i == 0 and j == n - 1)
            mj = membership.get(j)
            shared = (({c for (c, _a) in mi} & {c for (c, _a) in mj})
                      if (mi is not None and mj is not None) else set())
            spine_caps = tuple(ctx.centerlines[c].cap for c in shared)
            kj_bld = kj in bld

            visible_fn = (None if vis is None
                          else (lambda _a=xi, _b=yi, _c=xj, _d=yj:
                                vis(_a, _b, _c, _d)))
            crosses_fn = None
            if (crosses_spine is not None and not spine_caps
                    and not ring_adjacent):
                crosses_fn = (lambda _a=xi, _b=yi, _c=xj, _d=yj:
                              crosses_spine(_a, _b, _c, _d))
            blend_fn = None
            if near is not None:
                blend_fn = (lambda _a=xi, _b=yi, _c=xj, _d=yj, _ni=near[i],
                            _nj=near[j], _kb=(ki_bld or kj_bld):
                            _apron_edge_cap(_a, _b, _c, _d, _ni, _nj,
                                            body_cap, _kb))

            allow = GL.classify_pair(GL.PairContext(
                role=shape.role, dist=d, ring_adjacent=ring_adjacent,
                a_seam=ki in seam, b_seam=kj in seam,
                a_building=ki_bld, b_building=kj_bld,
                spine_caps=spine_caps, body_cap=body_cap,
                visible_fn=visible_fn, crosses_spine_fn=crosses_fn,
                blend_cap_fn=blend_fn))
            if allow is None:
                continue
            sc.edges.append((ki, kj, allow.flat_cap()))

    sc.spine_chains = _build_spine_chains(shape, ctx, membership)
    return sc


def _build_spine_chains(shape: GradeShape, ctx: GradeContext,
                        membership: dict) -> list[list[Hashable]]:
    """Ordered spine node-key chains (one per centerline crossing the shape),
    sorted by arc position — the smooth-profile handle for the connecting
    solve."""
    by_cl: dict[int, list[tuple[float, Hashable]]] = {}
    for ri, hits in membership.items():
        for (ci, a) in hits:
            by_cl.setdefault(ci, []).append((a, shape.keys[ri]))
    chains = []
    for ci, lst in by_cl.items():
        lst.sort(key=lambda t: t[0])
        chain = [k for (_a, k) in lst]
        if len(chain) >= 2:
            chains.append(chain)
    return chains


def build_grade_constraints(shapes: Sequence[GradeShape], ctx: GradeContext
                            ) -> list[ShapeConstraints]:
    """Constraints for every soft airside shape (apron / junction)."""
    out = []
    for s in shapes:
        if s.role in SOFT_VISIBILITY_ROLES:
            out.append(shape_constraints(s, ctx))
    return out


# ── THE ONE GRAPH (solver sets on it, validator checks it) ───────────────────

@dataclass
class UnifiedGraph:
    """THE single grade graph on GEOMETRY NODES (node indices via
    ``bucket_to_idx``) — the SAME nodes the solver sets elevations on and the
    validator checks (docs/goal_merge_one_graph.md).

    * ``pos``           — ``{node_idx: (x, y)}`` local-meter position.
    * ``edges``         — every undirected grade edge ``(a, b, cap, is_spine)``:
      apron/junction within-shape (body + spine) + sloping-rect/cap all-pair.
      ``is_spine`` marks the taxi-spine pairs (apron/junction spine chains + the
      rect/cap pairs) the strict spine gate covers.
    * ``spine_adj``     — ``{i: [(j, budget), ...]}`` over the SMOOTH-PROFILE
      spine subgraph (centerline-consecutive apron/junction pairs + rect axis +
      rect-cap continuation), ``budget = cap·dist``.  This is what the spine
      solve smooths; a 1-D feasible chain.
    * ``runway_anchor`` — ``{node_idx: local_runway_elev}`` for every geometry
      node a taxi spine joins the runway at (the single hard anchor; the building
      floor yields to it).
    """
    pos: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    spine_adj: dict = field(default_factory=dict)
    runway_anchor: dict = field(default_factory=dict)

    def spine_edge_set(self):
        """The undirected spine pairs ``{(min(a,b), max(a,b))}`` (is_spine)."""
        return {(min(a, b), max(a, b))
                for (a, b, _c, sp) in self.edges if sp}

    def spine_nodes(self):
        s = set()
        for (a, b, _c, sp) in self.edges:
            if sp:
                s.add(a)
                s.add(b)
        return s


def build_unified_graph(layout, bucket_to_idx) -> "UnifiedGraph":
    """Assemble THE one graph on geometry node indices.

    This is the SINGLE graph the route-profile solver sets elevations on and the
    validator (``grade_graph_validate.within_violations``) checks — the same
    nodes, edges, per-letter caps and runway anchors, so build and validate can
    never drift (the whole point of docs/goal_merge_one_graph.md).
    """
    from .layout import taxi_shape_code_letter
    from .junction_rules import SLOPING_RECT_ROLES
    from .config import taxi_grade_cap_for_letter

    cps = layout.canonical_points
    ctx = build_context(layout, bucket_to_idx)
    G = UnifiedGraph()

    def _idx(x, y):
        return bucket_to_idx.get(cps.get_or_add(float(x), float(y)))

    # ── apron / junction within-shape edges (body + spine), node-index keyed ──
    for s in layout.shapes:
        if (s.role not in SOFT_VISIBILITY_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) < 3:
            continue
        idx = [_idx(x, y) for (x, y) in ring]
        keys = [i if i is not None else ("_n", p) for p, i in enumerate(idx)]
        for p, i in enumerate(idx):
            if i is not None:
                G.pos[i] = ring[p]
        gs = GradeShape(role=s.role, ring=list(ring), keys=keys)
        sc = shape_constraints(gs, ctx)
        spine_pairs = set()
        for chain in sc.spine_chains:
            for u, v in zip(chain, chain[1:]):
                if isinstance(u, int) and isinstance(v, int):
                    spine_pairs.add((min(u, v), max(u, v)))
        for (a, b, cap) in sc.edges:
            if not isinstance(a, int) or not isinstance(b, int):
                continue
            is_spine = (min(a, b), max(a, b)) in spine_pairs
            G.edges.append((a, b, cap, is_spine))

    # ── sloping-rect + cap all-pair edges (the taxi spine as tilted planes) ───
    rect_caps = []                          # (corner_idx_set, cap)
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) < 3:
            continue
        cap = float(taxi_grade_cap_for_letter(taxi_shape_code_letter(layout, s)))
        idxs = []
        for (x, y) in ring:
            i = _idx(x, y)
            if i is not None:
                G.pos[i] = (x, y)
            idxs.append(i)
        rect_caps.append(({i for i in idxs if i is not None}, cap))
        _all_pair(G, idxs, cap)
    cap_shapes = []           # (cap_corner_idxs, parent_corner_set, cap)
    for s in layout.shapes:
        if (not getattr(s, "is_rect_cap", False) or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) < 3:
            continue
        idxs = []
        for (x, y) in ring:
            i = _idx(x, y)
            if i is not None:
                G.pos[i] = (x, y)
            idxs.append(i)
        ckeys = {i for i in idxs if i is not None}
        cap, best, parent = None, 0, frozenset()
        for (rkeys, rcap) in rect_caps:
            sh = len(rkeys & ckeys)
            if sh > best:
                best, cap, parent = sh, rcap, rkeys
        if cap is None:
            cap = float(taxi_grade_cap_for_letter(None))
        _all_pair(G, idxs, cap)
        cap_shapes.append(([i for i in idxs if i is not None], parent, cap))

    # ── GLOBAL spine chains: per centerline, all on-line geometry nodes ordered
    # by arc and linked consecutive (budget = cap·arc-gap).  This connects the
    # spine ACROSS shape boundaries (junction→apron→junction) and SPANS each rect
    # (the rect interior has no on-line node, so its two flanking junction nodes
    # are consecutive at budget cap·rect-length) — one connected, ≤cap profile,
    # exactly what the route graph gave but on the geometry nodes themselves.
    _build_global_spine(G, ctx)

    # ── weave each sloping RECT into the spine network as a connected sub-chain
    # (flat ends + axial + links to the flanking on-line nodes) so the ONE spine
    # solve produces rect-end elevations consistent with the junctions they abut.
    _add_rects_to_spine(G, layout, bucket_to_idx)

    # ── weave each end-CAP in too: its INNER corners (shared with the parent rect)
    # link to its OUTER corners at the cap rate, and the outer corners (junction
    # spine nodes) ride the same profile — so a cap can't be a steep plane that
    # conflicts with the junction it abuts.
    _add_caps_to_spine(G, cap_shapes)

    # ── runway anchors: every geometry node a taxi spine joins the runway at ──
    _runway_anchors(layout, G, bucket_to_idx)
    return G


def _add_caps_to_spine(G, cap_shapes):
    """Weave each end-cap into ``G.spine_adj``: its INNER corners (shared with the
    parent rect) link to its OUTER corners at the cap rate (budget ``cap·dist``),
    so the cap rides the rect→junction profile instead of being a free plane that
    can conflict with the junction node it shares an outer corner with."""
    for (idxs, parent, cap) in cap_shapes:
        present = [i for i in idxs if i in G.pos]
        inner = [i for i in present if i in parent]
        outer = [i for i in present if i not in parent]
        if not inner or not outer:
            continue
        # each outer corner links to its nearest inner corner at the cap rate.
        for o in outer:
            po = G.pos[o]
            ni = min(inner, key=lambda i: _dist(po, G.pos[i]))
            d = _dist(po, G.pos[ni])
            _spine_link(G.spine_adj, o, ni, cap * max(d, 1e-3))
        # keep the outer corners flat to each other (a clean cap end).
        for a, b in zip(outer, outer[1:]):
            _spine_link(G.spine_adj, a, b, 1e-3)


def _add_rects_to_spine(G, layout, bucket_to_idx):
    """Add each sloping taxi rect to ``G.spine_adj`` as a connected sub-chain:
    the two short-end corner pairs are flat (budget ``cap·width``), the ends are
    joined along the axis (budget ``cap·axis_len``), and each end links to the
    nearest on-line spine node (the flanking junction/apron centerline node) so
    the rect rides the same continuous profile."""
    from .layout import taxi_shape_code_letter
    from .junction_rules import SLOPING_RECT_ROLES
    from .config import taxi_grade_cap_for_letter
    cps = layout.canonical_points

    def _idx(x, y):
        return bucket_to_idx.get(cps.get_or_add(float(x), float(y)))

    spine_pts = [(i, G.pos[i]) for i in set(G.spine_adj)]
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) < 4:
            continue
        idx = [_idx(x, y) for (x, y) in ring]
        if any(i is None for i in idx):
            continue            # (a 5+-corner stub has a split end — handled below)
        cap = float(taxi_grade_cap_for_letter(taxi_shape_code_letter(layout, s)))
        endA, endB, ext = _rect_ends(ring)
        if endA is None:
            continue
        # FLAT ends (budget ~0): every corner at an axis extreme stays equal, so
        # the rect emits as a clean tilted plane (the validator checks all-pair —
        # a non-flat end makes a diagonal exceed cap).
        for grp in (endA, endB):
            for u, v in zip(grp, grp[1:]):
                _spine_link(G.spine_adj, idx[u], idx[v], 1e-3)
        # axial link end-A ↔ end-B at the per-letter cap over the axis extent.
        _spine_link(G.spine_adj, idx[endA[0]], idx[endB[0]],
                    cap * max(ext, 1e-3))
        # connect each end to the nearest on-line spine node (the flanking junction
        # centerline node) so the rect rides the same continuous profile.
        for grp in (endA, endB):
            mx = sum(ring[k][0] for k in grp) / len(grp)
            my = sum(ring[k][1] for k in grp) / len(grp)
            gi = {idx[k] for k in grp}
            best, bd = None, 15.0 * 15.0
            for (j, (jx, jy)) in spine_pts:
                if j in gi:
                    continue
                d2 = (jx - mx) ** 2 + (jy - my) ** 2
                if d2 < bd:
                    bd, best = d2, j
            if best is not None:
                _spine_link(G.spine_adj, idx[grp[0]], best,
                            cap * max(math.sqrt(bd), 1e-3))


def _rect_ends(ring):
    """Split a sloping-rect ring into its two axis-END corner groups (handles
    4-corner rects AND 5+-corner stubs whose short end is split).  Returns
    ``(endA_indices, endB_indices, axis_extent)`` or ``(None, None, 0)``."""
    n = len(ring)
    elens = [math.hypot(ring[(k + 1) % n][0] - ring[k][0],
                        ring[(k + 1) % n][1] - ring[k][1]) for k in range(n)]
    le = max(range(n), key=lambda k: elens[k])      # longest edge = axis dir
    ax = ring[(le + 1) % n][0] - ring[le][0]
    ay = ring[(le + 1) % n][1] - ring[le][1]
    al = math.hypot(ax, ay) or 1.0
    ax, ay = ax / al, ay / al
    ts = [(ring[k][0] - ring[0][0]) * ax + (ring[k][1] - ring[0][1]) * ay
          for k in range(n)]
    tmin, tmax = min(ts), max(ts)
    ext = tmax - tmin
    if ext < 1e-6:
        return None, None, 0.0
    tol = 0.25 * ext
    endA = [k for k in range(n) if ts[k] <= tmin + tol]
    endB = [k for k in range(n) if ts[k] >= tmax - tol]
    if not endA or not endB:
        return None, None, 0.0
    return endA, endB, ext


def _build_global_spine(G, ctx):
    """Order every on-line geometry node along each centerline by arc position and
    link consecutive ones into ``G.spine_adj`` at the centerline's per-letter cap.
    A node may lie on several centerlines (a junction crossing) — it is linked on
    each, so the chains fuse into one connected spine network."""
    items = list(G.pos.items())
    for cl in ctx.centerlines:
        on_line = []
        for (i, (x, y)) in items:
            a, d = _project(cl, x, y)
            if d <= SPINE_PERP_TOL_M:
                on_line.append((a, i))
        if len(on_line) < 2:
            continue
        on_line.sort(key=lambda t: t[0])
        for (a0, i0), (a1, i1) in zip(on_line, on_line[1:]):
            if i0 == i1:
                continue
            gap = abs(a1 - a0)
            d = _dist(G.pos.get(i0), G.pos.get(i1))
            budget = cl.cap * max(gap, d, 1e-3)
            _spine_link(G.spine_adj, i0, i1, budget)


def _dist(pa, pb):
    if pa is None or pb is None:
        return 1e-3
    return math.hypot(pa[0] - pb[0], pa[1] - pb[1])


def _spine_link(spine_adj, a, b, budget):
    spine_adj.setdefault(a, [])
    spine_adj.setdefault(b, [])
    if all(j != b for (j, _w) in spine_adj[a]):
        spine_adj[a].append((b, budget))
    if all(j != a for (j, _w) in spine_adj[b]):
        spine_adj[b].append((a, budget))


def _all_pair(G, idxs, cap):
    """All-corner-pair spine edges of a rect/cap (a tilted plane: every pair is a
    spine constraint)."""
    valid = [i for i in idxs if i is not None]
    for p in range(len(valid)):
        for q in range(p + 1, len(valid)):
            a, b = valid[p], valid[q]
            if a == b:
                continue
            G.edges.append((a, b, cap, True))


def _runway_anchors(layout, G, bucket_to_idx):
    """Record ``{geometry_node_idx: local_runway_elev}`` for every node where a
    taxi centerline joins a runway (mirrors the validator's runway-join check).
    The runway is the single hard anchor; this is what the spine solve pins to."""
    from shapely.geometry import Point
    from .layout import ROLE_RUNWAY
    from .pavement.runways import _sample_runway_segment_elev
    from .config import taxi_grade_cap_for_letter

    cps = layout.canonical_points
    _CONTACT_M = 12.0
    _NEAR_M = 18.0
    runways = [s for s in layout.shapes
               if s.role == ROLE_RUNWAY and s.polygon is not None
               and not s.polygon.is_empty]
    if not runways:
        return
    # Candidate nodes = EVERY emitted non-runway vertex (the SAME set the
    # validator's runway-join picks its nearest node from — including
    # runway_crossing nodes), so we anchor the exact node it checks.  Each is
    # mapped to its geometry index; a node off the solve graph is skipped.
    nx = []
    seen = set()
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty):
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is None or i in seen:
                continue
            seen.add(i)
            nx.append((i, (x, y)))
            G.pos.setdefault(i, (x, y))
    if not nx:
        return
    for entry in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = entry[0] if isinstance(entry, (tuple, list)) else entry
        ref = entry[1] if (isinstance(entry, (tuple, list))
                           and len(entry) > 1) else None
        if ln is None or ln.is_empty or str(ref or "").upper().startswith("SVC"):
            continue
        cs = list(ln.coords)
        for (ex, ey) in (cs[0], cs[-1]):
            P = Point(ex, ey)
            rwy = min(runways, key=lambda r: r.polygon.distance(P))
            if rwy.polygon.distance(P) > _CONTACT_M:
                continue
            re = _sample_runway_segment_elev(rwy, ex, ey)
            if re is None:
                continue
            # nearest graph node to the contact = the spine node that anchors
            best_i, best_d2 = None, _NEAR_M * _NEAR_M
            for (i, (x, y)) in nx:
                d2 = (x - ex) ** 2 + (y - ey) ** 2
                if d2 < best_d2:
                    best_d2, best_i = d2, i
            if best_i is not None:
                G.runway_anchor[best_i] = float(re)


def flatten_pairs(constraints: Sequence[ShapeConstraints],
                  noise: float = ELEV_ROUNDING_NOISE_M):
    """Flatten to validator pairs ``(key_a, key_b, cap, allowance)`` where
    ``allowance = cap * dist + noise`` — but dist is unknown here, so the
    validator recomputes it; we return ``(key_a, key_b, cap)`` and let the caller
    add dist/allowance.  Kept as a thin helper so the validator has one entry."""
    for sc in constraints:
        for (a, b, cap) in sc.edges:
            yield (a, b, cap)

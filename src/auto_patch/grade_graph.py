"""THE single within-shape grade-constraint graph (solver AND validator).

This module is the ONE place that decides *which* vertex pairs of a soft airside
shape are grade-constrained and *at what cap*.  Both the elevation solver
(pre-emit, from ``layout.shapes``) and the grade validator (post-emit, from the
OSM ways) build the same representation-agnostic input and call
:func:`build_grade_constraints` — so the surface we *build* and the surface we
*check* can never drift again (see ``docs/single_grade_graph.md``).

It is deliberately self-contained and clean-room: it does NOT import the legacy
``unified_jacobi._visible_grade_edges`` / ``check_grade.iter_shape_grade_
constraints`` / per-axis machinery.  Those are retired once this is wired in.

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

# Pairs closer than this are ring/relative noise — not a grade constraint.
_MIN_PAIR_DIST_M = 0.5


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
    the building-pad key set.  Both the elevation solver
    (``unified_jacobi._build_shape_constraints``), the spine
    (``route_profile/spine.spine_adjacency``) and the validator
    (``grade_graph_validate.within_violations``) call this, so the centerlines,
    caps and inheritance can never drift (docs/single_grade_graph.md).

    ``bucket_to_idx`` selects the BUILDING-KEY space (the one place the two
    representations diverge):

      * given (solver / spine, pre-emit) → SOLVER NODE INDICES
        (``bucket_to_idx[canonical_points.get_or_add(x, y)]``), matching the
        node-idx ``keys`` those callers put on their ``GradeShape``s;
      * ``None`` (validator, post-emit) → ROUNDED-COORD tuples
        ``(round(x, 3), round(y, 3))`` — the validator keys its shapes by ring
        index and matches buildings by coordinate.
    """
    from .elevation_per_surface.unified_jacobi import (
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

    for i in range(n):
        xi, yi = ring[i]
        ki = keys[i]
        mi = membership.get(i)
        ki_bld = ki in bld
        for j in range(i + 1, n):
            kj = keys[j]
            if ki == kj:
                continue
            if ki in seam or kj in seam:
                continue            # seam-anchored endpoint — DEM controls
            if ki_bld and kj in bld:
                continue            # inter-pad frontage = building↔building step
            xj, yj = ring[j]
            d = math.hypot(xi - xj, yi - yj)
            if d < _MIN_PAIR_DIST_M:
                continue
            ring_adjacent = (j == i + 1) or (i == 0 and j == n - 1)
            if vis is not None and not ring_adjacent:
                if not vis(xi, yi, xj, yj):
                    continue        # chord leaves the pavement — not a path
            mj = membership.get(j)
            shared = (({c for (c, _a) in mi} & {c for (c, _a) in mj})
                      if (mi is not None and mj is not None) else set())
            spine_pair = bool(shared)
            if (not spine_pair and not ring_adjacent
                    and crosses_spine is not None
                    and crosses_spine(xi, yi, xj, yj)):
                continue            # path is via the spine, not this diagonal
            # PER-EDGE spine cap (user 2026-06-24): a taxi route keeps ITS OWN
            # per-letter cap along its whole length, INCLUDING inside a junction
            # — a 3% taxiway grades at 3% up to the edge of a 1.5% taxiway it
            # meets, not at the junction-wide max.  So a spine edge is capped by
            # the centerline(s) IT lies on (looser of them, mirroring the
            # seater's per-centerline edge cap), NOT the shape-wide _spine_cap.
            if spine_pair:
                cap = max(ctx.centerlines[c].cap for c in shared)
            elif near is not None:
                cap = _apron_edge_cap(xi, yi, xj, yj, near[i], near[j],
                                      body_cap, ki_bld or (kj in bld))
            else:
                cap = body_cap
            sc.edges.append((ki, kj, cap))

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


def flatten_pairs(constraints: Sequence[ShapeConstraints],
                  noise: float = ELEV_ROUNDING_NOISE_M):
    """Flatten to validator pairs ``(key_a, key_b, cap, allowance)`` where
    ``allowance = cap * dist + noise`` — but dist is unknown here, so the
    validator recomputes it; we return ``(key_a, key_b, cap)`` and let the caller
    add dist/allowance.  Kept as a thin helper so the validator has one entry."""
    for sc in constraints:
        for (a, b, cap) in sc.edges:
            yield (a, b, cap)

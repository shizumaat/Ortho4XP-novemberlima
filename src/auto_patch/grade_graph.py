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
    APRON_MAX_GRADE,
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
    spine_cap = _spine_cap(membership, ctx) if membership else body_cap
    vis = _visibility_predicate(ring)
    seam = ctx.seam_keys
    bld = ctx.building_keys

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
            spine_pair = mi is not None and mj is not None \
                and _shared_centerline(mi, mj)
            cap = spine_cap if spine_pair else body_cap
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

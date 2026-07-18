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
    - service_junction → ``SERVICE_ROAD_MAX_GRADE`` (5 %).

Rects (4-corner sloping planes), terminals (flat pads), runways (FAA profile) and
groundside (DEM) are NOT handled here — the solver keeps their plane/flat/profile
models (a correct planar rect already satisfies the convex all-pair check, so
they are not a lockstep gap) and the validator keeps its own per-role handling for
them.  This module owns the apron/junction visibility graph only.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Callable, Hashable, Optional, Sequence

from shapely.errors import GEOSException, TopologicalError

from . import grade_law as GL

# Shapely-domain failures a triangulation / geometry op may raise (never catch
# built-ins broadly — a KeyError etc. is a real bug, not a bad polygon).
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)
from .config import (
    ANISO_EDGES,
    APRON_BACK_EDGE_GRADE,
    APRON_MAX_GRADE,
    APRON_TAXI_BLEND,
    APRON_TAXI_TRANSITION_M,
    GRADE_VISIBILITY_BUFFER_M as _VIS_BUF,
    JUNCTION_MESH_CONSTRAINTS,
    SERVICE_ROAD_MAX_GRADE,
    SERVICE_ROAD_MAX_TRANSVERSE,
    SVC_SPINE_FIRST,
    TAXI_MAX_GRADE,
    TAXI_MAX_GRADE_NARROW,
    TAXI_MAX_TRANSVERSE_NARROW,
    taxi_grade_cap_for_letter,
)

# Roles this module owns (the visibility-graph soft airside shapes).
# ``JUNCTION_ROLES`` is defined by THE LAW (``grade_law`` — the junction mesh
# rule applies to these roles) and re-exported here for the readers.
# SPINE-FIRST service roads (config.SVC_SPINE_FIRST, part 30m):
# ``service_road`` joins the soft set so the road body gets within-shape LAW
# edges on BOTH readers (this graph and the validator, which import the same
# tuple) — previously a service_road emitted ZERO within-shape edges (not
# soft, not a ``junction_rules.SLOPING_RECT_ROLES`` rect since it carries
# per-node altitudes), so its two long edges could bind to different anchor
# regimes with no law between them (the CYXY 2.49 m cross-road tear).  Its
# pairs resolve through the SAME ``classify_pair``/``_bake_edge`` path as
# service_junction: body cap SERVICE_ROAD_MAX_GRADE longitudinally,
# SERVICE_ROAD_MAX_TRANSVERSE across the route — the cross-road tear becomes
# unrepresentable, not merely illegal.
APRON_ROLE = "apron"
JUNCTION_ROLES = GL.JUNCTION_ROLES
SOFT_VISIBILITY_ROLES = ((APRON_ROLE,) + JUNCTION_ROLES
                         + (("service_road",) if SVC_SPINE_FIRST else ()))

# A ring vertex counts as a SPINE node of a centerline when it lies within this
# perpendicular distance of it.  Post-slice the spine nodes sit exactly on the
# line, so this is tight (it only has to absorb float/round noise, not width).
SPINE_PERP_TOL_M = 1.0

# Apron↔taxi-route CONTACT allowance (user 2026-06-30): an apron ring edge welded
# to a taxi-route pavement earns the taxi cap in its own direction (the contact
# ramp from the apron body down/up to the route), instead of the flat apron cap
# that false-flags it.  ``_ROUTE_CONTACT_TOL_M`` buffers the route union so a
# node ON the shared boundary reads as inside.  Gate off ⇒ prior blend behaviour.
APRON_ROUTE_CONTACT = os.environ.get("O4_APRON_ROUTE_CONTACT", "1") == "1"
_ROUTE_CONTACT_TOL_M = 0.5

# The per-pair eligibility/cap decision (min-pair-dist, apron body-chord max,
# seam/building/spine/visibility skips, cap selection) is THE LAW — it lives in
# ``grade_law`` so the solver and the grade test share one source.  This module
# is the solver-side reader: it builds a ``grade_law.PairContext`` per pair and
# calls ``grade_law.classify_pair``.


@dataclass
class Centerline:
    """One taxi route centerline through the airport, in the SAME meter frame as
    the shape rings the caller passes.  ``seg_caps`` is the PER-SEGMENT taxiway
    longitudinal grade cap (one per ``pts`` segment, from the route's per-segment
    ICAO size); a route may change width along its length, so the cap is resolved
    locally via :meth:`cap_at`.  ``cap`` is the tightest cap on the route, for the
    scalar within-shape-body consumers."""
    pts: Sequence[tuple[float, float]]
    seg_caps: list = field(default_factory=list)
    # cumulative arc length at each pt (filled lazily)
    _arc: Optional[list[float]] = None
    # Index into ``GradeContext.routes`` of the WHOLE route this bend-split piece
    # belongs to (the chained-route spine-arc frame, see :class:`RouteChain`).
    # ``-1`` ⇒ no chained route attached (legacy / piece is its own route).
    route_idx: int = -1

    @property
    def cap(self) -> float:
        return min(self.seg_caps) if self.seg_caps else TAXI_MAX_GRADE

    def cap_at(self, s: float) -> float:
        """Per-segment cap at arc-length ``s`` along the centerline."""
        if not self.seg_caps:
            return TAXI_MAX_GRADE
        a = self.arc()
        for i in range(len(a) - 1):
            if s <= a[i + 1] + 1e-9:
                return self.seg_caps[min(i, len(self.seg_caps) - 1)]
        return self.seg_caps[-1]

    def arc(self) -> list[float]:
        if self._arc is None:
            a = [0.0]
            for i in range(1, len(self.pts)):
                a.append(a[-1] + math.hypot(self.pts[i][0] - self.pts[i - 1][0],
                                            self.pts[i][1] - self.pts[i - 1][1]))
            self._arc = a
        return self._arc


@dataclass
class RouteChain:
    """A WHOLE taxi route (the continuous parent polyline of a set of bend-split
    :class:`Centerline` pieces), in LOCAL meters.

    It exists to give an off-spine pair a single continuous spine-ARC frame: a
    climbing route that curves through a junction is bend-split into short pieces,
    so projecting a body vertex onto one piece resets the arc at every bend and the
    curve never earns its full Δs∥.  Projecting onto the chained route instead
    credits the route's true arc length (``docs/anisotropic_edge_handling_plan.md``
    §3d).  Geometry only — per-letter caps still travel on the ``Centerline``
    pieces; the route supplies the (Δs∥, Δs⊥) decomposition frame, not the cap."""
    pts: Sequence[tuple[float, float]]
    _arc: Optional[list[float]] = None

    def arc(self) -> list[float]:
        if self._arc is None:
            a = [0.0]
            for i in range(1, len(self.pts)):
                a.append(a[-1] + math.hypot(self.pts[i][0] - self.pts[i - 1][0],
                                            self.pts[i][1] - self.pts[i - 1][1]))
            self._arc = a
        return self._arc

    def project(self, x: float, y: float) -> tuple[float, float]:
        """``(arc_pos, perp_dist)`` of ``(x, y)`` onto this chained route."""
        a, d, _ = _project(self, x, y)
        return a, d


@dataclass
class GradeShape:
    """One soft airside shape, representation-agnostic.

    ``ring``  open ring (no repeated closing vertex), LOCAL meter coords.
    ``keys``  stable per-vertex key parallel to ``ring`` (OSM nid | solver idx).
    ``role``  apron | junction | service_junction.
    ``adopts_apron_grade``  USER RULING 2026-07-06: a service road /
    service junction sharing an edge with an apron follows the APRON
    grading rules.  Layout reader: from ``BuiltShape.adopts_apron_grade``;
    OSM reader: from the ``o4_grade_law='apron'`` way tag.

    ``adopts_taxi_grade`` / ``adopted_taxi_letter``  USER RULING 2026-07-07
    (STATUS part 29 item 4): a service-road portion inside/alongside a
    TAXIWAY follows the taxiway grade law (1.5 %, letter-aware via the
    adjacent taxiway's code letter).  Layout reader: from
    ``BuiltShape.adopts_taxi_grade`` / ``.adopted_taxi_letter``; OSM reader:
    from the ``o4_grade_law='taxi'`` way tag (+ ``code_letter``).  Apron
    (1 %) is more limiting than taxi (1.5 %); when both are set apron wins.
    """
    role: str
    ring: list[tuple[float, float]]
    keys: list[Hashable]
    adopts_apron_grade: bool = False
    adopts_taxi_grade: bool = False
    adopted_taxi_letter: str | None = None
    # Runway DE-SEGMENTATION (O4_RUNWAY_SINGLE_POLY): this is the ONE ring
    # per runway ref whose FAA profile stations are interior long-edge
    # vertices.  ``plane_constraints`` scopes such a ring's within-shape
    # pair domain to LATERAL + same/adjacent-station (user ruling
    # 2026-07-08); a segmented sub-rect leaves this False and keeps its full
    # all-pair check (its short/wide axis would over-segment).  Layout
    # reader: ``BuiltShape.from_single_poly``; OSM reader: the
    # ``o4_single_poly='1'`` way tag.
    single_poly: bool = False


@dataclass
class GradeContext:
    """Shared context every caller builds once from its own representation."""
    centerlines: list[Centerline]
    # The WHOLE routes (chained parent polylines); ``Centerline.route_idx`` indexes
    # this list.  An off-spine pair decomposes against its centerline's route here
    # so a curving route earns its full spine ARC as Δs∥ (anisotropic edge law).
    routes: list[RouteChain] = field(default_factory=list)
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
    # PREPARED geometry of the service-road carve zone (road shapes unioned and
    # buffered by ``ROAD_FRONTAGE_TOL_M``), in the caller's meter frame.  A
    # soft-shape pair with BOTH endpoints inside it descends at the road cap (the
    # carve corners lie on the host ring).  ``None`` ⇒ no road carves.
    road_zone: object = None
    # PREPARED union of the taxi-ROUTE pavements (junction / parallels / stub /
    # cross-connector), buffered a hair.  An apron ring node inside it is welded to
    # a taxi route it abuts — so its ring edges are the apron's CONTACT with that
    # route and earn the taxi cap in their own climbing direction (they must drop
    # from the apron body to the lower/higher route), not the flat apron cap.  Keys
    # off the route PAVEMENT, so it fires even at a wide junction whose painted
    # centerline is far from the contact.  ``None`` ⇒ off.
    route_zone: object = None
    # EXACT-MESH sidecar (user 2026-07-05): the SOLVER's junction triangle-mesh
    # edge set (a ``MeshEdgesExact``), consumed 1:1 by the emitted-OSM reader so
    # emit-time ring repairs (buffer(0), needle-vertex removal, canonical-point
    # interning) cannot mint a DIFFERENT Delaunay than the one the solver graded
    # to (the SPJC cm-noise junction class).  ``None`` ⇒ the reader triangulates
    # its own ring (the solver path, and legacy sidecars).
    mesh_edges_exact: object = None


@dataclass
class ShapeConstraints:
    """The grade constraints of ONE shape: undirected edges ``(key_a, key_b,
    allowance)`` — where ``allowance`` is a :class:`grade_law.Allowance`
    (anisotropic ``cL·Δs∥ + cT·Δs⊥``; evaluate with ``allowance.at(Δs∥, Δs⊥)``,
    today flat) — plus the spine chains (ordered spine node keys) for the
    connecting solve's smooth-profile handling."""
    role: str
    edges: list[tuple[Hashable, Hashable, "GL.Allowance"]] = field(
        default_factory=list)
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

    cls: list[Centerline] = []
    routes: list[RouteChain] = []
    route_key_to_idx: dict = {}
    # GLOBAL-SLICE spine (user 2026-07-02): service ROADS are spines too —
    # narrow truck routes are sliced like taxiways and their faces grade
    # LONGITUDINALLY along the road at the road cap ("two roughly parallel
    # spines with the right grade cap").  Rect model keeps the legacy skip
    # (service roads have their own 4-corner rect role there).
    from .config import (CURVE_NATIVE_SPINE as _CNS, ROUTE_ARC_SPINE as _RAS,
                         SERVICE_ROAD_MAX_GRADE as _SVC_CAP)
    _svc_spines = _CNS or _RAS
    for tcl in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = getattr(tcl, "line", tcl)
        if ln is None or getattr(ln, "is_empty", True):
            continue
        _is_svc = getattr(tcl, "is_service", False)
        if _is_svc and not _svc_spines:
            continue            # rect model: service roads are NOT taxi spines
        try:
            pts = list(ln.coords)
        except Exception:
            continue
        if _is_svc and len(pts) >= 2:
            # road spine: own cap, own chain (no per-letter table).
            seg_caps = [_SVC_CAP] * (len(pts) - 1)
            rline = getattr(tcl, "route_line", None)
            rkey = id(rline) if rline is not None else ("self", id(ln))
            ridx = route_key_to_idx.get(rkey)
            if ridx is None:
                try:
                    rpts = list(rline.coords) if rline is not None else pts
                except Exception:
                    rpts = pts
                ridx = len(routes)
                routes.append(RouteChain(pts=rpts))
                route_key_to_idx[rkey] = ridx
            cls.append(Centerline(pts=pts, seg_caps=seg_caps, route_idx=ridx))
            continue
        if len(pts) >= 2:
            # Per-segment cap from the route's per-segment ICAO size (no name→
            # letter table); pad to one cap per segment.
            sizes = list(getattr(tcl, "seg_sizes", []) or [])
            seg_caps = [taxi_grade_cap_for_letter(sizes[i]) if i < len(sizes)
                        else taxi_grade_cap_for_letter(sizes[-1] if sizes else None)
                        for i in range(len(pts) - 1)]
            # Chain this piece to its WHOLE route.  Pieces bend-split from the same
            # parent share the SAME ``route_line`` object (or fall back to their own
            # ``line``) — dedupe by identity so each distinct route appears once in
            # ``routes`` and every piece points at it via ``route_idx``.
            rline = getattr(tcl, "route_line", None)
            rkey = id(rline) if rline is not None else ("self", id(ln))
            ridx = route_key_to_idx.get(rkey)
            if ridx is None:
                try:
                    rpts = list(rline.coords) if rline is not None else pts
                except Exception:
                    rpts = pts
                ridx = len(routes)
                routes.append(RouteChain(pts=rpts))
                route_key_to_idx[rkey] = ridx
            cls.append(Centerline(pts=pts, seg_caps=seg_caps, route_idx=ridx))

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
    # Service-road carve zone — a soft-shape pair on a road carve descends at the
    # road cap (the carve corners lie on the host ring).  Built ONCE here so the
    # solver and the validator regulate it identically (the law, not a fudge).
    road_zone = None
    road_polys = [s.polygon for s in layout.shapes
                  if s.role in ("service_road", "service_junction")
                  and s.polygon is not None and not s.polygon.is_empty]
    if road_polys:
        try:
            from shapely.ops import unary_union as _uu
            from shapely.prepared import prep as _prep
            from .config import ROAD_FRONTAGE_TOL_M
            road_zone = _prep(_uu(road_polys).buffer(ROAD_FRONTAGE_TOL_M))
        except Exception:                                     # pragma: no cover
            road_zone = None

    # Taxi-ROUTE pavement zone — apron edges that contact it are contact ramps and
    # earn the taxi cap (user 2026-06-30).  Keyed off the pavement (not the far
    # centerline) so it fires at wide-junction contacts.  Gate O4_APRON_ROUTE_CONTACT.
    route_zone = None
    if APRON_ROUTE_CONTACT:
        route_polys = [s.polygon for s in layout.shapes
                       if s.role in ("junction", "primary_parallel",
                                     "secondary_parallel", "stub", "cross_connector")
                       and s.polygon is not None and not s.polygon.is_empty]
        if route_polys:
            try:
                from shapely.ops import unary_union as _uu
                from shapely.prepared import prep as _prep
                route_zone = _prep(_uu(route_polys).buffer(_ROUTE_CONTACT_TOL_M))
            except Exception:                                 # pragma: no cover
                route_zone = None

    # Tile-seam pin keys (user 2026-07-04, "treat the seam like a runway
    # edge or building"): the solver-side law was running with NO seam
    # concept (empty default) while the validator zone-flagged 400 m —
    # the one place the two readers disagreed on the LAW itself.  The
    # solver's key space is node indices, so the pin set published by
    # ``solver_primitives._seed_elevations`` (which runs before every
    # ``build_context`` call in the solve) is used verbatim; callers
    # without a solve in flight (no attribute) get an empty set = the old
    # behaviour.  The validator builds its own nid-space set from the
    # sidecar's ``seam_pins`` export.
    seam_pin_idx = getattr(layout, "_seam_pin_idx", None) or ()

    return GradeContext(centerlines=cls, routes=routes,
                        inherited_junction_cap=_inherited,
                        building_keys=frozenset(bld_keys), road_zone=road_zone,
                        route_zone=route_zone,
                        seam_keys=frozenset(seam_pin_idx))


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

def _project(cl, x: float, y: float):
    """Return ``(arc_pos, perp_dist, (foot_x, foot_y))`` of ``(x, y)`` onto the
    polyline ``cl`` (any object exposing ``.pts`` + ``.arc()`` — a
    :class:`Centerline` piece or a whole :class:`RouteChain`)."""
    best_d = float("inf")
    best_a = 0.0
    best_foot = (x, y)
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
            best_foot = (px, py)
    return best_a, best_d, best_foot


def _polyline_tree(ctx: "GradeContext", which: str):
    """Lazy STRtree over ``ctx.centerlines`` (`which='cl'`) or ``ctx.routes``
    (`which='routes'`), cached ON the ctx object and invalidated when the
    list length changes (the blend builds a filtered shallow copy).

    The linear nearest-scan was O(vertices x lines x line_pts): with the
    route-arc global slice there are ~500 UNCHAINED lines, and profiling
    showed 25M ``_project`` calls / ~90 s per SPJC build in these lookups.
    Returns ``(tree, idx_list, geom_list)`` (tree None when no geometry)."""
    from shapely.geometry import LineString
    from shapely.strtree import STRtree
    items = ctx.centerlines if which == "cl" else ctx.routes
    cache_attr = "_tree_" + which
    n_attr = cache_attr + "_n"
    cached = getattr(ctx, cache_attr, None)
    if cached is not None and getattr(ctx, n_attr, -1) == len(items):
        return cached
    geoms, idxs = [], []
    for i, it in enumerate(items):
        if len(it.pts) >= 2:
            try:
                geoms.append(LineString(it.pts))
                idxs.append(i)
            except Exception:
                continue
    cached = (STRtree(geoms) if geoms else None, idxs, geoms)
    try:
        setattr(ctx, cache_attr, cached)
        setattr(ctx, n_attr, len(items))
    except Exception:
        pass
    return cached


def ds_decompose(pa: tuple[float, float], pb: tuple[float, float],
                 route) -> tuple[float, float]:
    """Decompose the separation of two points into ``(Δs∥, Δs⊥)`` w.r.t. a route
    (a :class:`RouteChain` or :class:`Centerline`):

    * ``Δs∥`` = the CHORD between the two projection foot points — the pair's
      along-route component measured on the SURFACE;
    * ``Δs⊥`` = the residual transverse offset, ``√(max(0, sep² − long_chord²))``
      — so ``Δs∥² + Δs⊥² = sep²`` exactly: the decomposition is a rotation of
      the direct pair separation, never an inflation.

    ⚠ Δs∥ was originally the along-route ARC (``|arc_a − arc_b|``, "a climbing
    turn earns its full longitudinal budget").  MEASURED WRONG (user JOSM/sim
    review 2026-07-03): near curves two physically-CLOSE points project far
    apart along the route, so the arc form granted budgets far beyond any
    surface cap — 7,040 SPJC pairs steeper than 1.5 % were "legal" (worst
    12.5 % over 5.2 m ruled legal at a nominal 1.5 % cap): visible cliffs
    perpendicular to the spine and >1 % terminal-frontage ramps at ZERO
    reported violations.  The pavement between two nearby points is
    continuous — the surface gradient between them is what the standards
    regulate, so the budget must be built from the direct separation, only
    ROTATED into (∥, ⊥) so ``cL``/``cT`` anisotropy still applies.

    THE single decomposition primitive — the anisotropic allowance is then
    ``Allowance.at(Δs∥, Δs⊥) = cL·Δs∥ + cT·Δs⊥`` (``grade_law``); the solver and
    validator both call it, so the built and checked surfaces use identical math.
    For a STRAIGHT route this returns ``(sep, 0)`` (the isotropic ``cap·dist``
    case), so straight taxiways/aprons are unaffected."""
    _arc_a, _da, qa = _project(route, pa[0], pa[1])
    _arc_b, _db, qb = _project(route, pb[0], pb[1])
    sep = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
    long_chord = math.hypot(qa[0] - qb[0], qa[1] - qb[1])
    ds_par = min(long_chord, sep)
    ds_perp = math.sqrt(max(0.0, sep * sep - ds_par * ds_par))
    return ds_par, ds_perp


def _spine_membership(shape: GradeShape, ctx: GradeContext
                      ) -> dict[int, list[tuple[int, float]]]:
    """For each ring index, the list of (centerline-index, arc_pos) it lies on
    (within ``SPINE_PERP_TOL_M``)."""
    out: dict[int, list[tuple[int, float]]] = {}
    tree, idxs, _geoms = _polyline_tree(ctx, "cl")
    if tree is None:
        return out
    from shapely.geometry import Point as _Pt
    for ri, (x, y) in enumerate(shape.ring):
        hits = []
        # bbox candidates within the tolerance, exact test via _project
        for k in tree.query(_Pt(x, y).buffer(SPINE_PERP_TOL_M)):
            ci = idxs[int(k)]
            a, d, _ = _project(ctx.centerlines[ci], x, y)
            if d <= SPINE_PERP_TOL_M:
                hits.append((ci, a))
        if hits:
            hits.sort()
            out[ri] = hits
    return out


# An intersection point within this distance of a chord endpoint is
# CONTACT, not a crossing (see _crosses): far enough from the mm-scale
# solver-frame vs emitted-lat/lon differences that both readers give the
# same verdict, small enough that a genuine crossing a metre into the
# chord still skips it.
_CROSS_ENDPOINT_CLEARANCE_M = 0.5


def _spine_crossing_predicate(shape: GradeShape, ctx: GradeContext,
                              membership: dict):
    """Return ``crosses(xa,ya,xb,yb)->bool``: True iff the chord crosses a
    spine centerline (so the real grade path between the two sides is via the
    spine, not the direct diagonal).  ``None`` if shapely is unavailable or
    the context has no centerlines.

    Tested against ALL context centerlines, not only the shape's MEMBER ones
    (user 2026-07-03): the two law readers carry the same spine geometry
    SPLIT DIFFERENTLY (the solver has whole polylines; the validator's
    sidecar axes are split per segment-cap letter), so membership-gated geoms
    diverged — a chord crossing a non-member PIECE of a line whose other
    piece held the members was skipped by one reader and flagged by the
    other (the SPJC ≥1% residual tail).  The union of all centerlines is
    identical on both sides regardless of splitting, and the rule's physics
    ("the climb between the two sides is carried by the spine") holds for
    any spine the chord crosses, member or not."""
    if not ctx.centerlines:
        return None
    _ = membership          # kept in the signature for call-site stability
    try:
        from shapely.geometry import LineString
    except ImportError:  # pragma: no cover
        return None
    # ALL-centerline geoms + STRtree, built once per CONTEXT (cached): the
    # per-shape member subset used to keep this list short; the full set
    # needs the tree to stay cheap.
    cached = getattr(ctx, "_crossing_tree", None)
    if cached is None:
        geoms = []
        for cl in ctx.centerlines:
            if len(cl.pts) >= 2:
                try:
                    geoms.append(LineString(cl.pts))
                except Exception:
                    pass
        tree = None
        if geoms:
            try:
                from shapely.strtree import STRtree
                tree = STRtree(geoms)
            except Exception:               # pragma: no cover
                tree = None
        cached = (geoms, tree)
        try:
            ctx._crossing_tree = cached
        except Exception:                   # pragma: no cover
            pass
    geoms, tree = cached
    if not geoms:
        return None

    def _crosses(xa, ya, xb, yb):
        # ⚠ MEASURED DEAD END (2026-07-03, do not retry as-is): trimming ~1 m
        # off the chord ends (with either ``crosses`` or ``intersects``) to fix
        # the endpoint-contact instability made SPJC WORSE (178→325): the trim
        # flips verdicts for the common chords that START next to a spine cut
        # node, and the two readers' mm-different inputs then diverge on MORE
        # pairs, not fewer.  The real fix is upstream: give both readers
        # IDENTICAL inputs (sidecar carries the solver's exact spine geometry /
        # frame), not a more forgiving predicate.
        try:
            ch = LineString(((xa, ya), (xb, yb)))
        except Exception:
            return False
        # INTERIOR-CLEARANCE crossing (2026-07-03, replaces both the bare
        # ``crosses`` parity AND the short-lived endpoint-on-spine skip):
        # the chord crosses a spine iff SOME intersection point lies at
        # least ``_CROSS_ENDPOINT_CLEARANCE_M`` from BOTH chord endpoints.
        #   * endpoint CONTACT is not a crossing — a chord touching the
        #     spine at its own endpoint (a spine cut/junction node on the
        #     ring) stays IN the law, so side-to-spine and pad-frontage
        #     differentials remain regulated (the blanket endpoint skip
        #     let faces tilt steeply perpendicular to the spine and waived
        #     terminal-frontage chords — user-visible violations at 0
        #     reported).  The verdict is DISTANCE-thresholded, so the two
        #     readers' mm-different frames agree (bare ``crosses`` flipped
        #     on epsilon endpoint contact — the SPJC 122 m pad-chord class).
        #   * ANY hit point counts, including one AT a centerline endpoint
        #     — split-agnostic (``crosses`` needed an interior hit on the
        #     line side too, so a chord passing exactly through a sidecar
        #     split node was invisible to one reader).
        def _hit_points(inter):
            stack = [inter]
            while stack:
                q = stack.pop()
                if q.is_empty:
                    continue
                gt = q.geom_type
                if gt == "Point":
                    yield (q.x, q.y)
                elif gt in ("LineString", "LinearRing"):
                    # collinear overlap: its midpoint stands in for the run
                    m = q.interpolate(0.5, normalized=True)
                    yield (m.x, m.y)
                elif hasattr(q, "geoms"):
                    stack.extend(q.geoms)

        def _crosses_one(g):
            if not ch.intersects(g):
                return False
            try:
                inter = ch.intersection(g)
            except Exception:
                return False
            for (px, py) in _hit_points(inter):
                da = math.hypot(px - xa, py - ya)
                db = math.hypot(px - xb, py - yb)
                if min(da, db) > _CROSS_ENDPOINT_CLEARANCE_M:
                    return True
            return False

        if tree is not None:
            try:
                for k in tree.query(ch):
                    if _crosses_one(geoms[int(k)]):
                        return True
                return False
            except Exception:               # pragma: no cover
                pass
        for g in geoms:
            if _crosses_one(g):
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
    # USER RULING 2026-07-06: a service road / junction sharing an edge
    # with an apron follows the apron grading rules.
    if shape.adopts_apron_grade:
        return APRON_MAX_GRADE
    # USER RULING 2026-07-07: a service road / junction inside or alongside
    # a taxiway follows the taxiway cap (1.5 %, letter-aware).  Apron (1 %)
    # is more limiting, so the apron branch above wins if both are set.
    if getattr(shape, "adopts_taxi_grade", False):
        return float(taxi_grade_cap_for_letter(
            getattr(shape, "adopted_taxi_letter", None)))
    # ``service_road`` reaches here only under SVC_SPINE_FIRST (it joins
    # SOFT_VISIBILITY_ROLES there) — same road cap as service_junction.
    # Without the explicit branch it would fall through to the junction
    # spine/inheritance logic and could inherit a TAXI cap from a welded
    # neighbour, which is not the road's law.
    if shape.role in ("service_junction", "service_road"):
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
    tree, idxs, geoms = _polyline_tree(ctx, "cl")
    if tree is None:
        return best_d, best_cap, best_t
    from shapely.geometry import Point as _Pt
    k = tree.nearest(_Pt(x, y))
    cands = [ctx.centerlines[idxs[int(k)]]] if k is not None else []
    for cl in cands:
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


def _apron_edge_cap(xi, yi, xj, yj, ni, nj, body_cap, twist, boundary=False,
                    contact=False):
    """Apron cap near a taxi route (user 2026-06-25): an apron edge earns the
    route's (looser) cap as it nears the route, decaying to ``body_cap`` past
    ``APRON_TAXI_TRANSITION_M``.  ``ni``/``nj`` = ``_nearest_centerline`` at each
    endpoint.

    ``twist`` (the edge touches a building frontage): the apron WARPS to blend the
    flat pad into the climbing route — its corners slope ± to meet the route — so
    the looser cap applies in ALL directions (isotropic).

    ``boundary`` (user 2026-06-30): a RING-ADJACENT apron edge that runs along a
    taxi route is the apron's CONTACT with the route — it must drop from the apron
    body down to the (lower/higher) route it abuts, so like a frontage warp it
    grades the route cap in its OWN direction (isotropic), not only parallel to the
    centerline.  This is what lets an apron↔taxiway contact ramp exceed the flat
    apron cap instead of being false-flagged as an apron body violation.

    ``contact`` (an endpoint is welded to a taxi-route pavement, via ``route_zone``)
    forces the FULL route cap regardless of centerline distance — the corner of a
    wide junction is metres from its own painted centerline, but it is still the
    apron's contact with that route, so the along-centerline decay must not shrink
    the allowance to nothing there.

    Otherwise only the ALONG-route component earns it (the apron still grades
    ``body_cap`` perpendicular, from its edges to the spine)."""
    d, route_cap, tan = (ni if ni[0] <= nj[0] else nj)
    # The frontage warp needs MORE than the route cap (the route's climb along the
    # pad is compressed into the apron depth), so the twist target is the
    # back-edge ramp grade; elsewhere the apron blends toward the route cap.
    target = max(route_cap, APRON_BACK_EDGE_GRADE) if twist else route_cap
    if target <= body_cap or (d >= APRON_TAXI_TRANSITION_M and not contact):
        return body_cap
    dist_factor = 1.0 if contact else 1.0 - d / APRON_TAXI_TRANSITION_M
    if twist or boundary or contact:
        infl = dist_factor                               # isotropic (warp/contact)
    else:
        ex, ey = xj - xi, yj - yi
        el = math.hypot(ex, ey) or 1e-9
        along = abs(ex * tan[0] + ey * tan[1]) / el      # 0 (perp) .. 1 (along)
        infl = along * dist_factor
    return body_cap + (target - body_cap) * infl


# ── anisotropic edge decomposition (O4_ANISO_EDGES) ──────────────────────────

def _nearest_route(x: float, y: float, ctx: GradeContext):
    """``(route_idx, perp)`` — the nearest chained route to ``(x, y)`` and its
    perpendicular distance.  ``(-1, inf)`` if there are no routes."""
    tree, idxs, geoms = _polyline_tree(ctx, "routes")
    if tree is None:
        return -1, float("inf")
    from shapely.geometry import Point as _Pt
    pt = _Pt(x, y)
    k = tree.nearest(pt)
    if k is None:
        return -1, float("inf")
    k = int(k)
    return idxs[k], geoms[k].distance(pt)


def _edge_route(role, shared, ctx, vr_i, vr_j, di_perp, dj_perp):
    """The route a pair decomposes against (§3c), or ``None`` to stay isotropic.

    * SPINE pair (shares a centerline) → that route (the looser-cap centerline's
      chained route) — the climbing curve earns its full arc as Δs∥.
    * JUNCTION body pair → the NEAREST route, but only when BOTH endpoints share
      the same nearest route (same Voronoi crotch cell); spanning the convergence
      (different nearest routes) stays isotropic / is already skipped.
    * APRON pair → only in the BLEND zone (both endpoints within
      ``APRON_TAXI_TRANSITION_M`` of the one shared route); apron body far from any
      route keeps its isotropic 1 % (no arc credit for a far interior chord)."""
    if shared:
        c_star = max(shared, key=lambda c: ctx.centerlines[c].cap)
        ridx = ctx.centerlines[c_star].route_idx
        if 0 <= ridx < len(ctx.routes):
            return ctx.routes[ridx]
        return ctx.centerlines[c_star]
    ri, rj = vr_i, vr_j
    if ri < 0 or ri != rj:
        return None
    if role == APRON_ROLE and (di_perp > APRON_TAXI_TRANSITION_M
                               or dj_perp > APRON_TAXI_TRANSITION_M):
        return None
    return ctx.routes[ri]


def _bake_edge(allow, role, pa, pb, shared, ctx, vr_i, vr_j):
    """Replace a live ``Allowance`` with its route-decomposed BAKED budget (when
    the pair has a route, §3c); otherwise return it unchanged (isotropic).

    The budget is the anisotropic ``√((cL·Δs∥)² + (cT·Δs⊥)²)`` against the
    pair's route — the max |Δz| in an oblique direction on a surface with
    principal gradient limits ``cL`` along the route and ``cT`` across it.
    (Two former inflations, both measured wrong 2026-07-03: Δs∥ used to be
    the along-route ARC — near curves physically-close pairs earned budgets
    far beyond any surface cap — and the L1 sum ``cL·Δs∥ + cT·Δs⊥``
    over-allowed diagonals by up to √2.)"""
    route = _edge_route(role, shared, ctx, vr_i[0], vr_j[0], vr_i[1], vr_j[1])
    if route is None:
        return allow
    dp, dt = ds_decompose(pa, pb, route)
    cL = allow.cL
    # Transverse cap: A/B taxiways (cL == narrow 3 %) earn the tighter 2 %
    # transverse (ICAO Annex 14 Table 3-2), and SERVICE-ROAD-rate pairs
    # (cL == 5 %) earn the AASHTO 2 % normal-crown transverse (user crown
    # ruling 2026-07-07 — laterally a road may not tilt at its
    # longitudinal cap: 25 cm across a 5 m road was the visible
    # ridge/valley budget).  Every other cap (C–F 1.5 %, apron 1 %,
    # apron-blend gradients) stays isotropic cT == cL.
    if abs(cL - TAXI_MAX_GRADE_NARROW) < 1e-9:
        cT = TAXI_MAX_TRANSVERSE_NARROW
    elif abs(cL - SERVICE_ROAD_MAX_GRADE) < 1e-9:
        cT = SERVICE_ROAD_MAX_TRANSVERSE
    else:
        cT = cL
    return GL.Allowance.baked(
        cL, cT, math.hypot(cL * dp, cT * dt))


# ── junction mesh edges (O4_JUNCTION_MESH_CONSTRAINTS) ───────────────────────

def mesh_edge_keys(ring: Sequence[tuple[float, float]],
                   keys: Sequence[Hashable]) -> set:
    """The triangle-mesh EDGE set of a shape, as ``frozenset({key_a, key_b})``
    pairs — the edges a constrained-Delaunay triangulation of the ring facets
    (what X-Plane's mesh approximates).  Includes the perimeter (ring-adjacent)
    edges and the interior/cross-slope edges; excludes long chords across the
    shape.  A junction's real grade paths are these edges plus its spine; the
    remaining ``O(n²)`` chords are phantom (see ``config.JUNCTION_MESH_CONSTRAINTS``).

    SINGLE SOURCE both the solver (``shape_constraints``) and the validator use,
    so they cannot drift.  Deterministic (GEOS Delaunay is order-stable).  Falls
    back to ring-adjacent-only if the polygon is degenerate / triangulation fails
    (never raises — a bad triangulation must not abort a build)."""
    from shapely.geometry import Polygon as _Poly
    n = len(ring)
    # ring-adjacent perimeter edges are always mesh edges (the polygon boundary).
    out = {frozenset((keys[i], keys[(i + 1) % n])) for i in range(n)}
    if n < 4:
        return out
    idx = {(round(x, 3), round(y, 3)): keys[i] for i, (x, y) in enumerate(ring)}
    try:
        poly = _Poly(ring)
        if (not poly.is_valid) or poly.is_empty or poly.area <= 0.0:
            return out
        from shapely.ops import triangulate as _tri
        for t in _tri(poly):
            # keep only triangles inside the (possibly concave) polygon.
            if not poly.contains(t.centroid):
                continue
            corners = list(t.exterior.coords)[:-1]
            tk = [idx.get((round(x, 3), round(y, 3))) for (x, y) in corners]
            for a in range(3):
                u, v = tk[a], tk[(a + 1) % 3]
                if u is not None and v is not None and u != v:
                    out.add(frozenset((u, v)))
    except _GEOM_EXC:
        return out
    return out


class MeshEdgesExact:
    """The SOLVER's junction triangle-mesh edge set (sidecar ``mesh_edges``),
    indexed so an emitted-OSM reader can consume the solver's mesh 1:1 instead
    of re-triangulating the EMITTED ring.

    Why: ``layout.to_osm`` repairs rings at emit (buffer(0), needle-vertex
    removal, ~0.5 m canonical-point interning), so the emitted junction ring
    can differ from the ring the solver triangulated — GEOS Delaunay then
    facets it DIFFERENTLY, and the validator checks mesh chords the solver
    never constrained (SPJC 2026-07-05: 44 genuine mesh/ring pairs a median
    1.8 cm over allowance).  With this structure the validator asks "was this
    pair a SOLVER mesh edge" by matching each emitted ring vertex to the
    nearest exported mesh vertex within ``SHARED_VERTEX_TOL_M`` (the one
    canonical node identity, 2026-06-30).

    An emitted vertex with NO solver counterpart within tolerance (e.g. a
    buffer(0) self-touch vertex minted at emit) matches nothing, so its body
    chords skip as phantom — the solver never constrained them, and checking
    them against a mesh the solver never built is exactly the noise class this
    removes.  Ring-adjacent pairs are unaffected (the law never consults the
    mesh for them).

    Vertex identity is the exact meter-coordinate tuple: both endpoints of a
    shared solver vertex serialize to the same rounded lat/lon, so they
    convert to bit-identical meters."""

    def __init__(self, edge_endpoints_m):
        from .layout import SHARED_VERTEX_TOL_M
        self._match_tolerance_m = float(SHARED_VERTEX_TOL_M)
        self._vertex_ordinal: dict = {}      # exact (x, y) → ordinal
        self._grid_cells: dict = {}          # grid cell → [(x, y, ordinal)]
        self.edge_pairs: set = set()         # frozenset({ordinal_a, ordinal_b})
        for (point_a, point_b) in edge_endpoints_m:
            ordinal_a = self._intern(point_a)
            ordinal_b = self._intern(point_b)
            if ordinal_a != ordinal_b:
                self.edge_pairs.add(frozenset((ordinal_a, ordinal_b)))

    def _intern(self, point) -> int:
        key = (float(point[0]), float(point[1]))
        ordinal = self._vertex_ordinal.get(key)
        if ordinal is None:
            ordinal = len(self._vertex_ordinal)
            self._vertex_ordinal[key] = ordinal
            cell = (int(math.floor(key[0] / self._match_tolerance_m)),
                    int(math.floor(key[1] / self._match_tolerance_m)))
            self._grid_cells.setdefault(cell, []).append(
                (key[0], key[1], ordinal))
        return ordinal

    def _nearest_vertex(self, x: float, y: float):
        """Nearest exported mesh vertex within the match tolerance, or None.
        Deterministic: ties break to the lowest ordinal."""
        cell_x = int(math.floor(x / self._match_tolerance_m))
        cell_y = int(math.floor(y / self._match_tolerance_m))
        best_ordinal = None
        best_distance = self._match_tolerance_m
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (vx, vy, ordinal) in self._grid_cells.get(
                        (cell_x + dx, cell_y + dy), ()):
                    distance = math.hypot(x - vx, y - vy)
                    if (distance < best_distance
                            or (distance == best_distance
                                and best_ordinal is not None
                                and ordinal < best_ordinal)):
                        best_distance = distance
                        best_ordinal = ordinal
        return best_ordinal

    def mesh_keys_for_ring(self, ring, keys) -> set:
        """The solver-mesh membership set for one emitted ring, in the ring's
        own key space — same contract as :func:`mesh_edge_keys`."""
        matched = [self._nearest_vertex(x, y) for (x, y) in ring]
        out: set = set()
        n = len(ring)
        for i in range(n):
            ordinal_i = matched[i]
            if ordinal_i is None:
                continue
            for j in range(i + 1, n):
                ordinal_j = matched[j]
                if ordinal_j is None or ordinal_j == ordinal_i:
                    continue
                if frozenset((ordinal_i, ordinal_j)) in self.edge_pairs:
                    out.add(frozenset((keys[i], keys[j])))
        return out


# ── main ────────────────────────────────────────────────────────────────────

def shape_constraints(shape: GradeShape, ctx: GradeContext,
                      ring_only: bool = False) -> ShapeConstraints:
    """The grade constraints of ONE soft airside shape (apron / junction).

    ``ring_only`` (user 2026-07-05 flatness tier): generate ONLY the
    ring-adjacent pairs — the O(n) physical boundary edges — through the SAME
    ``classify_pair`` path, so ring budgets are identical to the full run's.
    Used exclusively by ``solver_primitives._build_shape_constraints`` for
    shapes holding a flatness certificate (their O(n²) body pairs are
    satisfied at the DEM seed and are generated lazily the first time any of
    the shape's nodes moves off it — see ``one_solve.feasibility_project``).
    The mesh / visibility / spine-crossing predicates only ever gate
    NON-ring pairs (``grade_law.classify_pair`` never consults them for a
    ring-adjacent pair), so skipping their setup here cannot change a ring
    budget."""
    sc = ShapeConstraints(role=shape.role)
    ring = shape.ring
    keys = shape.keys
    n = len(ring)
    if n < 3:
        return sc
    membership = _spine_membership(shape, ctx)
    body_cap = _body_cap(shape, ctx, membership)
    vis = None if ring_only else _visibility_predicate(ring)
    # JUNCTION MESH CONSTRAINTS (O4_JUNCTION_MESH_CONSTRAINTS): the RULE — a
    # junction's only real grade paths are the spine + the triangle-mesh edges,
    # the remaining body chords are phantom — lives in ``grade_law.classify_pair``
    # (the JUNCTION MESH RULE skip).  This reader only computes the mesh-edge key
    # set and supplies the per-pair lazy membership thunk (``mesh_member_fn``),
    # mirroring the visibility / spine-crossing predicates.  APRONS keep their
    # full visibility graph (the geodesic flatness model catches aggregate slope a
    # mesh edge misses), so this is junction/service_junction only.
    # ``ctx.mesh_edges_exact`` (exact-mesh sidecar) supplies the SOLVER's mesh
    # 1:1; without it the reader triangulates its own ring (the solver path).
    mesh_keys = None
    if (JUNCTION_MESH_CONSTRAINTS and not ring_only
            and shape.role in JUNCTION_ROLES):
        mesh_keys = (ctx.mesh_edges_exact.mesh_keys_for_ring(ring, keys)
                     if ctx.mesh_edges_exact is not None
                     else mesh_edge_keys(ring, keys))
    # The shape's spine centerline geometries (those it has nodes on) — a body
    # chord that CROSSES one is NOT a real grade path: the climb between the two
    # sides is carried by the SPINE at the taxiway cap (the apron grades 1% to
    # its local spine, plan §2), so the straight 1%-diagonal across the spine
    # would falsely declare a wide apron infeasible.  Drop it; the constraint
    # holds transitively through the spine.
    crosses_spine = (None if ring_only
                     else _spine_crossing_predicate(shape, ctx, membership))
    seam = ctx.seam_keys
    bld = ctx.building_keys

    # APRON↔taxi blend: per-ring-node nearest centerline (dist, cap, tangent), so
    # an apron body edge's ALONG-route component earns the route's looser cap as
    # it nears a taxiway running through the apron (user 2026-06-25).
    near = None
    if (APRON_TAXI_BLEND and shape.role == APRON_ROLE
            and ctx.centerlines and body_cap < TAXI_MAX_GRADE):
        # SERVICE roads never blend an apron: a truck route's 5 % cap
        # belongs to its own strip faces, not to the apron around it
        # (service lines entered ctx.centerlines as road-cap spines with
        # the global slice, 2026-07-02).
        from .config import SERVICE_ROAD_MAX_GRADE as _SVC_CAP_BL
        _blend_ctx = ctx
        if any(c.cap >= _SVC_CAP_BL - 1e-9 for c in ctx.centerlines):
            import copy as _copy
            _blend_ctx = _copy.copy(ctx)
            _blend_ctx.centerlines = [
                c for c in ctx.centerlines if c.cap < _SVC_CAP_BL - 1e-9]
        near = [_nearest_centerline(x, y, _blend_ctx) for (x, y) in ring]

    # Per-vertex service-road-carve membership (O(n) once; the pair rule is then
    # ``both endpoints on a carve`` → road cap, via grade_law.classify_pair).
    road_vert = None
    if ctx.road_zone is not None:
        from shapely.geometry import Point as _RPt
        road_vert = [ctx.road_zone.contains(_RPt(x, y)) for (x, y) in ring]

    # Per-vertex taxi-route-pavement contact (apron only): a ring node welded to a
    # junction/parallel/stub pavement makes its ring edges contact ramps → taxi cap.
    route_vert = None
    if ctx.route_zone is not None and shape.role == APRON_ROLE and near is not None:
        from shapely.geometry import Point as _RPt2
        route_vert = [ctx.route_zone.contains(_RPt2(x, y)) for (x, y) in ring]

    # ANISOTROPIC EDGES (O4_ANISO_EDGES): per-vertex nearest chained route, so a
    # surviving spine / junction-body / apron-blend pair can be decomposed against
    # its route (Δs∥ = spine arc) and its budget BAKED into the Allowance.  Off ⇒
    # ``vert_route`` is None and every edge stays the legacy isotropic cap·dist.
    aniso = ANISO_EDGES and bool(ctx.routes)
    vert_route = ([_nearest_route(x, y, ctx) for (x, y) in ring]
                  if aniso else None)

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
            ring_adjacent = (j == i + 1) or (i == 0 and j == n - 1)
            if ring_only and not ring_adjacent:
                continue
            kj = keys[j]
            if ki == kj:
                continue
            xj, yj = ring[j]
            d = math.hypot(xi - xj, yi - yj)
            mj = membership.get(j)
            shared = (({c for (c, _a) in mi} & {c for (c, _a) in mj})
                      if (mi is not None and mj is not None) else set())
            spine_caps = tuple(ctx.centerlines[c].cap for c in shared)
            kj_bld = kj in bld

            # Junction mesh membership thunk for THE LAW's JUNCTION MESH RULE
            # (``grade_law.classify_pair`` skips a non-spine, non-ring,
            # non-mesh junction body chord as a phantom).  Supplied only where
            # the rule can apply, like ``crosses_fn`` below.
            mesh_fn = None
            if mesh_keys is not None and not spine_caps and not ring_adjacent:
                mesh_fn = (lambda _k=frozenset((ki, kj)), _m=mesh_keys:
                           _k in _m)

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
                _ct = bool(route_vert and ring_adjacent
                           and (route_vert[i] or route_vert[j]))
                blend_fn = (lambda _a=xi, _b=yi, _c=xj, _d=yj, _ni=near[i],
                            _nj=near[j], _kb=(ki_bld or kj_bld),
                            _ra=(ring_adjacent and APRON_ROUTE_CONTACT), _cn=_ct:
                            _apron_edge_cap(_a, _b, _c, _d, _ni, _nj,
                                            body_cap, _kb, boundary=_ra, contact=_cn))

            both_road = bool(road_vert and road_vert[i] and road_vert[j])
            allow = GL.classify_pair(GL.PairContext(
                role=shape.role, dist=d, ring_adjacent=ring_adjacent,
                a_seam=ki in seam, b_seam=kj in seam,
                a_building=ki_bld, b_building=kj_bld,
                spine_caps=spine_caps, body_cap=body_cap,
                visible_fn=visible_fn, crosses_spine_fn=crosses_fn,
                mesh_member_fn=mesh_fn,
                blend_cap_fn=blend_fn, both_road=both_road))
            if allow is None:
                continue
            # NEVER bake a route-arc budget into a BUILDING-endpoint pair
            # (user 2026-07-03, extending the 2026-07-02 ruling that already
            # excludes building pairs from the blend and the road carve:
            # buildings are the HEAVIEST constraint).  The arc credit
            # (Δs∥ = route arc ≫ chord) legalised pad-frontage chords at
            # 2-3× the flat 1 %·d — the SPJC residual-178 class: the solver
            # graph was satisfied at the baked budgets while the validator's
            # flat reading (correctly) flagged the same chords.
            if vert_route is not None and not (ki_bld or kj_bld):
                allow = _bake_edge(allow, shape.role, (xi, yi), (xj, yj),
                                   shared, ctx, vert_route[i], vert_route[j])
            sc.edges.append((ki, kj, allow))

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


def plane_constraints(shape: GradeShape, ctx: GradeContext,
                      cap: float) -> ShapeConstraints:
    """Within-shape constraints for a PLANE shape — a sloping taxi rect, a runway
    segment, or a flat terminal pad — via the SAME law as the soft shapes
    (:func:`grade_law.classify_pair`).

    A plane's pairwise grade IS the plane's slope along that chord, so the rule is
    simply ALL vertex pairs at the shape's ``cap`` (no spine / blend / visibility
    gating — these shapes are convex 4-corner; terminals are checked all-pair as
    before).  The seam and road-carve rules still apply (a plane vertex on a road
    carve descends at the road cap).  ``cap`` is the shape's within-shape cap the
    caller resolves (per-letter for a taxi rect, the runway/terminal cap
    otherwise).  This is the single rule source for plane shapes: both the
    in-memory validator and the OSM grade test build a ``GradeShape`` and call it,
    so they cannot drift from each other or from the law."""
    sc = ShapeConstraints(role=shape.role)
    ring = shape.ring
    keys = shape.keys
    n = len(ring)
    if n < 3:
        return sc
    seam = ctx.seam_keys
    road_vert = None
    if ctx.road_zone is not None:
        from shapely.geometry import Point as _RPt
        road_vert = [ctx.road_zone.contains(_RPt(x, y)) for (x, y) in ring]
    # RUNWAY within-shape LATERAL scoping (user 2026-07-08): a de-segmented
    # runway emits ONE ring whose FAA profile stations live as interior long-edge
    # vertices, so its all-pair within-shape check conflates the LATERAL law
    # (this check's real domain) with the LONGITUDINAL profile law
    # (``check_runway_profile`` + the spine-profile check).  Scope the runway
    # ring's pairs to SAME-/ADJACENT-station (``grade_law.runway_within_pair_in_
    # domain``); a pair spanning 2+ stations leaves this domain to the profile
    # law.  ONE predicate in ``grade_law``, applied HERE — ``plane_constraints``
    # is the single plane-rule source both the OSM grade test (``check_grade``'s
    # runway path) and any solver plane-edge build (``_add_plane_edges``) call —
    # so BUILD and CHECK scope in lockstep.  ONLY the de-segmented single-poly
    # ring (``shape.single_poly``) is scoped: its length ≫ width so the
    # longest-pair ref axis IS the runway axis and same-cross-end vertices
    # cluster to one station.  A legacy segmented sub-rect is left alone — it
    # keeps its full all-pair check (a short/wide rect's longest pair is a
    # DIAGONAL, which would spuriously split its two cross-ends into >2
    # stations and drop real pairs), so gate-off stays byte-identical.
    station_of = (GL.runway_axis_station_indices(ring)
                  if (shape.role == "runway" and shape.single_poly)
                  else None)
    for i in range(n):
        xi, yi = ring[i]
        ki = keys[i]
        for j in range(i + 1, n):
            kj = keys[j]
            if ki == kj:
                continue
            if (station_of is not None
                    and not GL.runway_within_pair_in_domain(
                        station_of[i], station_of[j])):
                continue
            xj, yj = ring[j]
            d = math.hypot(xi - xj, yi - yj)
            both_road = bool(road_vert and road_vert[i] and road_vert[j])
            allow = GL.classify_pair(GL.PairContext(
                role=shape.role, dist=d,
                ring_adjacent=(j == i + 1) or (i == 0 and j == n - 1),
                a_seam=ki in seam, b_seam=kj in seam,
                a_building=False, b_building=False,
                spine_caps=(), body_cap=cap, both_road=both_road))
            if allow is None:
                continue
            sc.edges.append((ki, kj, allow))
    return sc


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
    * ``runway_anchor_sample`` — ``{node_idx: (sample_x, sample_y, shape)}``
      the exact point (and owning runway/crossing SHAPE) the anchor value was
      sampled at.  The crown writeback (user ruling 2026-07-16: taxi joins
      anchor to the RUNWAY EDGE value — the crowned edge, never the
      centerline/crown profile) re-samples that shape's EMITTED edge at THIS
      point and assigns the join node the drop that lands it exactly there.
    """
    pos: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    spine_adj: dict = field(default_factory=dict)
    runway_anchor: dict = field(default_factory=dict)
    runway_anchor_sample: dict = field(default_factory=dict)

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


def shape_constraints_cached(polygon_key, gs: GradeShape,
                             ctx: GradeContext,
                             ring_only: bool = False) -> "ShapeConstraints":
    """Memoised :func:`shape_constraints` — keyed by ``(polygon_key, role,
    ring_only)`` on the CONTEXT, so the two per-solve law consumers
    (``solver_primitives._build_shape_constraints`` and
    :func:`build_unified_graph`, which construct identical ``GradeShape``s
    from the same polygons) run the expensive pair generation ONCE when they
    share a ctx (measured ~11 s/solve of duplicate work at SPJC).  Results
    are shared, never mutated by either consumer.

    ``ring_only`` is part of the key (user 2026-07-05 flatness tier): the
    certified-lazy branch's ring-only result and ``build_unified_graph``'s
    FULL result (which feeds the validator-parity ``u_edges`` projection and
    the reach fields, so it must never be thinned) coexist without either
    consumer seeing the other's set."""
    memo = getattr(ctx, "_sc_memo", None)
    if memo is None:
        memo = {}
        ctx._sc_memo = memo
    key = (polygon_key, gs.role, ring_only)
    sc = memo.get(key)
    if sc is None:
        sc = shape_constraints(gs, ctx, ring_only=ring_only)
        memo[key] = sc
    return sc


def build_unified_graph(layout, bucket_to_idx, ctx=None, *,
                        skip_edge_shape_ids=None,
                        include_spine=True) -> "UnifiedGraph":
    """Assemble THE one graph on geometry node indices.

    This is the SINGLE graph the route-profile solver sets elevations on and the
    validator (``grade_graph_validate.within_violations``) checks — the same
    nodes, edges, per-letter caps and runway anchors, so build and validate can
    never drift (the whole point of docs/goal_merge_one_graph.md).

    ``ctx``: optionally a prebuilt :func:`build_context` — pass the SAME one
    ``solver_primitives._build_shape_constraints`` used so the per-shape law
    memo (:func:`shape_constraints_cached`) is shared instead of recomputed.

    SCOPED FINAL PROJECTION (user 2026-07-05, ``O4_SCOPED_FINAL_PROJECTION``):
    ``skip_edge_shape_ids`` — apron/junction shapes (by ``id(s)``) whose
    within-shape ``G.edges`` contribution is SKIPPED (their positions are
    still registered).  Only ``final_grade_projection`` passes this, for
    shapes it proved unchanged since the solve — their identical pair set is
    carried by that caller's lazy entries instead, so law coverage is
    unchanged.  ``include_spine=False`` additionally skips the global spine /
    rect-weave / cap-weave / runway-anchor stages (``spine_adj`` and
    ``runway_anchor`` consumers), which that caller never reads — it uses
    ``G.edges`` only.  Defaults reproduce the full graph exactly.
    """
    from .layout import taxi_shape_code_letter
    from .junction_rules import SLOPING_RECT_ROLES
    from .config import taxi_grade_cap_for_letter

    cps = layout.canonical_points
    if ctx is None:
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
        if skip_edge_shape_ids is not None and id(s) in skip_edge_shape_ids:
            continue    # scoped projection: pairs live in the caller's lazy entry
        gs = GradeShape(role=s.role, ring=list(ring), keys=keys,
                        adopts_apron_grade=getattr(
                            s, "adopts_apron_grade", False),
                        adopts_taxi_grade=getattr(
                            s, "adopts_taxi_grade", False),
                        adopted_taxi_letter=getattr(
                            s, "adopted_taxi_letter", None))
        sc = shape_constraints_cached(id(s.polygon), gs, ctx)
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
        # LOCKSTEP BAKE EXPORT (2026-07-17): persist THIS shape's baked
        # decomposition in RING-POSITION space so the validator
        # (``grade_graph_validate._iter_checked_pairs``) consumes the
        # identical allowances instead of re-baking with a context it
        # cannot reconstruct (its ``build_context(layout)`` has no
        # ``bucket_to_idx``; measured CYXY: 29 of 9,915 shared edges
        # resolved a marginally different route-arc allowance on
        # re-bake).  Keyed by ``id(s)`` and guarded by (role, ring
        # signature): a post-solve-mutated ring misses the guard and
        # the validator re-bakes fresh — correct, its geometry changed.
        # Repeat builds (the scoped final projection) re-bake only the
        # shapes they re-run; skipped shapes keep the solve-time entry,
        # whose ring is unchanged by definition of the skip.
        position_of_key = {key: p for p, key in enumerate(keys)}
        ring_signature = tuple(
            (round(x, 6), round(y, 6)) for (x, y) in ring)
        baked_edges = []
        for (a, b, cap) in sc.edges:
            pa = position_of_key.get(a)
            pb = position_of_key.get(b)
            if pa is not None and pb is not None:
                baked_edges.append((pa, pb, cap))
        baked_spine = set()
        for chain in sc.spine_chains:
            for u, v in zip(chain, chain[1:]):
                pu = position_of_key.get(u)
                pv = position_of_key.get(v)
                if pu is not None and pv is not None:
                    baked_spine.add((min(pu, pv), max(pu, pv)))
        bake_store = getattr(layout, "_lockstep_shape_bake", None)
        if bake_store is None:
            bake_store = {}
            layout._lockstep_shape_bake = bake_store
        bake_store[id(s)] = (
            s.role, ring_signature, baked_edges, baked_spine)

    # ── sloping-rect + cap all-pair edges (the taxi spine as tilted planes) ───
    rect_caps = []                          # (corner_idx_set, cap)
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) < 3:
            continue
        if getattr(s, "adopts_apron_grade", False):
            cap = float(APRON_MAX_GRADE)   # user 2026-07-06 apron-edge rule
        elif getattr(s, "adopts_taxi_grade", False):
            cap = float(taxi_grade_cap_for_letter(   # user 2026-07-07
                getattr(s, "adopted_taxi_letter", None)))
        else:
            cap = float(taxi_grade_cap_for_letter(
                taxi_shape_code_letter(layout, s)))
        idxs = []
        for (x, y) in ring:
            i = _idx(x, y)
            if i is not None:
                G.pos[i] = (x, y)
            idxs.append(i)
        rect_caps.append(({i for i in idxs if i is not None}, cap))
        _add_plane_edges(G, s.role, ring, idxs, ctx, cap)
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
        _add_plane_edges(G, getattr(s, "role", "rect_cap"), ring, idxs, ctx, cap)
        cap_shapes.append(([i for i in idxs if i is not None], parent, cap))

    if include_spine:
        # ── GLOBAL spine chains: per centerline, all on-line geometry nodes
        # ordered by arc and linked consecutive (budget = cap·arc-gap).  This
        # connects the spine ACROSS shape boundaries (junction→apron→junction)
        # and SPANS each rect (the rect interior has no on-line node, so its
        # two flanking junction nodes are consecutive at budget
        # cap·rect-length) — one connected, ≤cap profile, exactly what the
        # route graph gave but on the geometry nodes themselves.
        _build_global_spine(G, ctx)

        # ── weave each sloping RECT into the spine network as a connected
        # sub-chain (flat ends + axial + links to the flanking on-line nodes)
        # so the ONE spine solve produces rect-end elevations consistent with
        # the junctions they abut.
        _add_rects_to_spine(G, layout, bucket_to_idx)

        # ── weave each end-CAP in too: its INNER corners (shared with the
        # parent rect) link to its OUTER corners at the cap rate, and the
        # outer corners (junction spine nodes) ride the same profile — so a
        # cap can't be a steep plane that conflicts with the junction it
        # abuts.
        _add_caps_to_spine(G, cap_shapes)

        # ── runway anchors: every geometry node a taxi spine joins the runway
        # at ──
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
    # Spatial prefilter (CYUL: the naive centerlines × nodes double loop was
    # 52 M ``_project`` calls / 140 s — 2,473 fragmented route pieces × 21 k
    # nodes).  Only nodes inside the centerline's tolerance-inflated bbox can
    # be on it; everything else skips the exact projection.
    node_tree = None
    try:
        from shapely.geometry import Point as _NPt, box as _nbox
        from shapely.strtree import STRtree as _NTree
        node_tree = _NTree([_NPt(x, y) for (_i, (x, y)) in items])
    except Exception:                                  # pragma: no cover
        node_tree = None
    for cl in ctx.centerlines:
        if node_tree is not None:
            xs = [p[0] for p in cl.pts]
            ys = [p[1] for p in cl.pts]
            q = _nbox(min(xs) - SPINE_PERP_TOL_M, min(ys) - SPINE_PERP_TOL_M,
                      max(xs) + SPINE_PERP_TOL_M, max(ys) + SPINE_PERP_TOL_M)
            cand = [items[int(k)] for k in node_tree.query(q)]
        else:                                          # pragma: no cover
            cand = items
        on_line = []
        for (i, (x, y)) in cand:
            a, d, _ = _project(cl, x, y)
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
            # Per-segment cap at the midpoint between the two on-line nodes (a
            # route may change width along its length).
            budget = cl.cap_at(0.5 * (a0 + a1)) * max(gap, d, 1e-3)
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


def _add_plane_edges(G, role, ring, idxs, ctx, cap):
    """Add a plane shape's (rect / cap) all-pair spine edges to the unified graph
    via the SHARED plane rule (:func:`plane_constraints` → ``grade_law``), so the
    SOLVER enforces the exact pairs/caps the validator checks for plane shapes —
    not a separate inline all-pair.  Every plane pair is a spine constraint
    (is_spine=True)."""
    pts = [(ring[p], i) for p, i in enumerate(idxs) if i is not None]
    if len(pts) < 3:
        return
    gs = GradeShape(role=role, ring=[c for (c, _i) in pts],
                    keys=[i for (_c, i) in pts])
    for (a, b, c) in plane_constraints(gs, ctx, cap).edges:
        G.edges.append((a, b, c, True))


def _runway_anchors(layout, G, bucket_to_idx):
    """Record ``{geometry_node_idx: local_runway_elev}`` for every node where a
    taxi centerline joins a runway (mirrors the validator's runway-join check).
    The runway is the single hard anchor; this is what the spine solve pins to."""
    from shapely.geometry import Point
    from .layout import ROLE_RUNWAY, ROLE_RUNWAY_CROSSING
    from .pavement.runways import _sample_runway_segment_elev
    from .config import taxi_grade_cap_for_letter

    import os
    cps = layout.canonical_points
    _CONTACT_M = GL.RUNWAY_CONTACT_M
    _NEAR_M = GL.RUNWAY_JOIN_NEAR_M
    _spine_edge_anchor = (
        os.environ.get("O4_RUNWAY_CONTACT_ANCHOR", "1") == "1")
    _edge_contact = (
        os.environ.get("O4_RUNWAY_EDGE_CONTACT", "1") == "1")
    # ANCHOR TARGET SET (user 2026-07-16, KBNA 13/31 defect H): include the
    # ROLE_RUNWAY_CROSSING slab pieces alongside the true runways.  A taxi /
    # junction join that TERMINATES on the crossing slab (which replaced the
    # runway surface where two runways intersect) finds no ROLE_RUNWAY within
    # RUNWAY_CONTACT_M, so it got no runway anchor and stepped off the slab
    # edge (KBNA 13/31: junction 376 stepped 0.31 m vs the crossing slab
    # edge).  The slab is runway-derived surface carrying per-vertex node
    # altitudes, so ``_sample_runway_segment_elev`` reads its edge value at
    # the contact exactly as for a runway.  Gate O4_RUNWAY_CROSSING_ANCHOR=0
    # reverts to true-runway-only targets.
    _crossing_anchor = (
        os.environ.get("O4_RUNWAY_CROSSING_ANCHOR", "1") == "1")
    _anchor_roles = ((ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)
                     if _crossing_anchor else (ROLE_RUNWAY,))
    runways = [s for s in layout.shapes
               if s.role in _anchor_roles and s.polygon is not None
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
    contact_endpoints = []   # centerline endpoints that TERMINATE at a runway
    for entry in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = (entry.line if hasattr(entry, "line")
              else (entry[0] if isinstance(entry, (tuple, list)) else entry))
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
            # The contact NODE sits where the centerline meets the runway EDGE, not
            # at the deep-interior centerline endpoint (a taxi route joins the
            # runway CENTERLINE, ~half-width inside on a wide runway).  Resolve it
            # through the shared law so the nearest-node search reaches the emitted
            # taxiway↔runway node.  O4_RUNWAY_EDGE_CONTACT=0 reverts to the endpoint.
            if _edge_contact:
                c = GL.runway_join_contact(ln, (ex, ey), rwy.polygon)
                cx, cy = c if c is not None else (ex, ey)
            else:
                cx, cy = ex, ey
            re = _sample_runway_segment_elev(rwy, cx, cy)
            if re is None:
                continue
            sample_x, sample_y = cx, cy
            contact_endpoints.append((cx, cy))
            # nearest graph node to the contact = the spine node that anchors
            best_i, best_d2 = None, _NEAR_M * _NEAR_M
            for (i, (x, y)) in nx:
                d2 = (x - cx) ** 2 + (y - cy) ** 2
                if d2 < best_d2:
                    best_d2, best_i = d2, i
            if best_i is not None:
                # Runway DE-SEGMENTATION (O4_RUNWAY_SINGLE_POLY): on a
                # single-poly ring, sample the runway surface at the
                # ANCHORED NODE's own boundary projection instead of the
                # contact's station.  The legacy per-piece sampler
                # effectively quantised the anchor onto the local piece's
                # corner values, keeping a near-edge anchor consistent
                # with the welded edge vertex beside it; the ring's
                # whole-profile interpolation returns the value 5-15 m
                # up-axis at the contact, and pinning THAT on a node
                # 2.5 m from the weld is unlawful over the junction's
                # within-shape budget (HECA 05R: 9 cm over 2.51 m =
                # 3.57 %).  The node-projection value satisfies both
                # readers by construction: it differs from the contact
                # sample by ≤ profile-grade × d(contact, node) — inside
                # the validator's join budget — and from any welded edge
                # vertex by ≤ profile-grade × their separation.
                if getattr(rwy, "from_single_poly", False):
                    node_x, node_y = G.pos.get(best_i, (cx, cy))
                    try:
                        boundary = rwy.polygon.exterior
                        q = boundary.interpolate(
                            boundary.project(Point(node_x, node_y)))
                        re_node = _sample_runway_segment_elev(
                            rwy, q.x, q.y)
                    except _GEOM_EXC:
                        re_node = None
                    if re_node is not None:
                        re = re_node
                        sample_x, sample_y = q.x, q.y
                if os.environ.get("O4_DESEG_DEBUG") == "1":
                    _la, _lo = layout.m_to_ll(*G.pos.get(best_i, (cx, cy)))
                    print(f"  [deseg-dbg] runway_anchor node@{_la:.7f},"
                          f"{_lo:.7f} = {float(re):.3f} "
                          f"(contact {cx:.1f},{cy:.1f})")
                G.runway_anchor[best_i] = float(re)
                G.runway_anchor_sample[best_i] = (
                    float(sample_x), float(sample_y), rwy)

    # ── SPINE nodes ON a runway edge where a route TERMINATES (user 2026-06-28) ─
    # A taxiway that ENDS at the runway already has its contact materialised as a
    # SHARED node: the runway sub-rect corner, the abutting junction vertex and
    # the taxi SPINE node are welded to ONE canonical point on the runway edge
    # (the segmenter cut it; ``_unify_airside_geometry`` welded it).  That node IS
    # the taxiway↔runway contact and must sit at the runway surface — yet the
    # endpoint search above misses it when the taxiway's rect ends short of the
    # centerline's runway-edge endpoint (CYXY taxiway A: the centerline endpoint
    # is the runway-edge MIDLINE at (-345,914), but the welded spine/runway nodes
    # are its two SIDE contacts 22.6 m away — past _NEAR_M=18 — so A was left
    # reach-detoured ~700 m and its served building over-credited 706 vs ~699).
    # So anchor every spine node that coincides with a runway vertex AND sits near
    # a TERMINATING contact endpoint (within _EDGE_REACH_M) — the route ends at the
    # runway there, exactly like a junction pavement edge.  The endpoint gate
    # excludes a taxiway that merely CROSSES / runs ALONG a runway mid-centerline
    # (SPJC taxiway F at (2136,-1677), 36 m from F's nearest endpoint) — those are
    # handled by the runway-crossing reconciliation, and hard-anchoring them
    # over-constrains the adjacent junction (a 0.16 m floor flag).  ``setdefault``
    # keeps any centerline-based anchor set above.  Gate O4_RUNWAY_CONTACT_ANCHOR=0.
    _EDGE_REACH_M = 30.0
    if _spine_edge_anchor and contact_endpoints:
        er2 = _EDGE_REACH_M * _EDGE_REACH_M
        for s in runways:
            for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
                i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                if i is None or i not in G.spine_adj or i in G.runway_anchor:
                    continue
                if all((x - ex) ** 2 + (y - ey) ** 2 > er2
                       for (ex, ey) in contact_endpoints):
                    continue
                re = _sample_runway_segment_elev(s, x, y)
                if re is not None:
                    if os.environ.get("O4_DESEG_DEBUG") == "1":
                        _la, _lo = layout.m_to_ll(x, y)
                        print(f"  [deseg-dbg] spine-edge anchor "
                              f"node@{_la:.7f},{_lo:.7f} = {float(re):.3f}")
                    G.runway_anchor[i] = float(re)
                    G.runway_anchor_sample[i] = (
                        float(x), float(y), s)

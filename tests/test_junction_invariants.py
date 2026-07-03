"""Junction polygon invariants — recurring-regression guards.

The user's authoritative shape rules (memory:
``feedback_shape_rules.md``) say:

* Junction polygons are the *residue* between rects / aprons /
  runways / terminals.  They are inherently small — incoming-corner
  count plus ≤ 4 trace points per arc between consecutive incoming
  corners.  Above ~5,000 m² the residue is apron-sized and should
  be classified as ``role=apron`` instead.
* "Coverage invariants → Shared vertices exact": every neighbour
  vertex that lands on a junction's perimeter must coincide with
  one of the junction's own ring vertices.  Otherwise X-Plane sees
  a free elevation gap at the kiss point and renders a cliff.

The CYXY ``-10070`` regression that prompted these tests had all
three failure modes at once: 32,000 m² area, 80 ring vertices, and
only 3 of its perimeter points shared with neighbouring shapes.

Each test parametrises across the standard test airports.  A
session-level layout cache avoids rebuilding the same airport for
every test.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from conftest import (
    airports_under_test, baseline_airports,
    is_tile_seam_vertex, xplane_available, xplane_root,
)


def _test_airports() -> list:
    """Union of baseline airports (always-run) + env-gated airports.

    Per user 2026-05-16: invariant tests must run on every canonical
    baseline airport unconditionally so geometry regressions can't
    slip past CI without being noticed.  ``O4_TEST_AIRPORTS=...``
    still extends the set for ad-hoc coverage of additional ICAOs.
    """
    seen = set()
    out = []
    for ic in list(baseline_airports()) + list(airports_under_test()):
        if ic not in seen:
            seen.add(ic)
            out.append(ic)
    return out

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _xplane_root() -> str:
    return xplane_root()


def _xplane_available() -> bool:
    return xplane_available()


from auto_patch.config import ROUTE_ARC_SPINE as _ROUTE_ARC_SPINE

pytestmark = [
    pytest.mark.skipif(
        not _xplane_available(),
        reason="X-Plane install not found (set XPLANE_ROOT to override)",
    ),
    # RECT-MODEL invariants: these rules describe junctions as the RESIDUE
    # between manufactured taxi rects (small, centerline-hugging, corners
    # shared with rect neighbours).  Under the route-arc GLOBAL SLICE
    # (default 2026-07-02) no rects are emitted and faces are conformant
    # by construction, so the residue rules no longer apply.  Kept for
    # the legacy path (O4_ROUTE_ARC_SPINE=0).
    pytest.mark.skipif(
        _ROUTE_ARC_SPINE,
        reason="rect-residue junction invariants — no taxi rects under "
               "the route-arc global slice (O4_ROUTE_ARC_SPINE=1)",
    ),
]


# Hard invariant thresholds — global, no per-airport relaxation.
# Per user 2026-04-30 / 2026-05-18: junction validity is a question
# of geometry, not area.  A 6-way mega-intersection is a valid
# junction.  Threshold = 55 m to accommodate normal taxiway fillet
# curves (apex sits ~22–50 m from the apt.dat straight-line
# centerline at large airports).  Boundary points beyond that are
# apron territory — there's no centerline running through them.
# Must match
# ``auto_patch.junction_repair._APRON_RECLASSIFY_MAX_DISTANCE_M``
# so reclassification and the invariant test agree.
MAX_BOUNDARY_TO_CENTERLINE_M = 55.0
BOUNDARY_SAMPLE_STEP_M = 5.0

# Orphan neighbour vertices: zero, hard.  A neighbour vertex
# touching a junction's perimeter line must coincide with one of
# the junction's own ring vertices.
MAX_ORPHAN_NEIGHBOUR_VERTICES = 0


# ── Per-airport regression baselines (calibrated 2026-05-01) ──
#
# Three tests in this file (vertex-count-bounded,
# boundary-near-centerline, taxi-rects-not-alongside-apron) fire
# heavily at SPJC because of pre-existing geometry-quality bugs:
# our junction polygons are vastly more sprawling than the
# ``SPJC_target.osm`` ground truth (target max=36 verts, we
# produce up to 336; target boundaries fit close to centerlines,
# ours stray as much as 566 m).  Once those bugs are fixed the
# baselines should drop toward zero and eventually be deleted.
#
# Each baseline records the WORST observed value at HEAD as a
# regression ceiling: tests fail only if a future change exceeds
# the recorded ceiling.  Lower recorded numbers → tighter gate.
# When you fix something, run the test and lower the baseline.
#
# Airports without an explicit baseline use the default tight
# value (zero offenders / hard cap) — those airports are still
# fully gated by the original invariant.
# SPJC baseline 2026-06-20: junction #192 (a large 3,918 m² intersection)
# has one boundary point 59.4 m from the nearest centerline — just over the
# 55 m fillet cap, terrain/geometry-benign (verified good in X-Plane).
JUNCTION_BOUNDARY_DISTANCE_REGRESSION_BASELINE: Dict[str, dict] = {
    "SPJC": {"max_offenders": 1, "max_distance_m": 60.0},
}

# Per user 2026-05-16: the shared-sloping-edge rule is universal —
# no airport-specific exemptions.  A sloping rect's sloping edge
# must never be shared by a junction/apron polygon's perimeter.

# Per user 2026-05-21: no airport-specific orphan allowances.  The
# bridge corner-snap (_snap_bridge_vertices_to_runway_corners) +
# junction-contact-insert (_insert_bridge_contacts_into_junctions)
# passes drove SPJC's former 5 boundary-vs-junction orphans (and CYXY's
# runway-corner-arc orphans) to zero, so this baseline is empty — every
# airport is enforced at the hard cap (MAX_ORPHAN_NEIGHBOUR_VERTICES = 0).
ORPHAN_NEIGHBOUR_VERTEX_REGRESSION_BASELINE: Dict[str, int] = {}


# A neighbour vertex within this distance of a junction's perimeter
# line is considered "kissing" and required to be shared.
ORPHAN_NEAR_PERIMETER_M = 1.0
# Coincidence tolerance: shared vertices need not be bit-identical
# (float-precision drift through unary_union etc.) but must agree
# at sub-decimetre level.
ORPHAN_SAME_VERTEX_TOL_M = 0.10


def _build_layout(icao: str):
    # Shared session cache (conftest) — built once per airport per run.
    from conftest import cached_airport_layout
    return cached_airport_layout(icao)


def _aeroway_centerlines_m(layout):
    """Delegate to the pipeline's centerlines helper so the test
    and the reclassification pass agree on the centerline set.
    """
    from auto_patch.junction_repair import _aeroway_centerlines_union
    return _aeroway_centerlines_union(layout)


def _shape_label(layout, idx: int, s) -> str:
    """Stable, human-readable identifier for a shape in failure
    messages.  ``layout.shapes`` index is order-dependent but
    matches the OSM emit order, so it's the most useful pointer
    when inspecting the emitted .osm."""
    ref = getattr(s, "ref", "") or ""
    return f"#{idx}({s.role}{('/' + ref) if ref else ''})"


@pytest.mark.parametrize("icao", _test_airports())
def test_junction_boundary_near_centerline(icao):
    """Per user 2026-04-30: a valid junction's pavement edge is
    always "relatively close" to a converging taxiway / runway
    centerline.  No matter how many taxiways meet at a junction, the
    surrounding apt.dat pavement edge sits at most one taxi
    half-width (+ a little fillet) from the local centerline.

    A junction whose boundary strays farther than
    ``MAX_BOUNDARY_TO_CENTERLINE_M`` from any centerline contains
    apron-territory pavement (no centerline running through it) and
    should be re-classified as ``role=apron`` or split.

    Area alone is NOT the test — a 6-way mega-intersection can be
    legitimately large.  The geometric invariant is what matters.

    Per-airport regression baseline:
    Airports listed in
    ``JUNCTION_BOUNDARY_DISTANCE_REGRESSION_BASELINE`` have a
    known-bad ceiling (count + worst distance); the test fails
    only if either exceeds the recorded value.  Other airports
    are gated tightly (zero offenders).
    """
    layout = _build_layout(icao)
    centers = _aeroway_centerlines_m(layout)
    if centers is None or centers.is_empty:
        pytest.skip(f"{icao}: no aeroway centerlines extractable")
    cap = MAX_BOUNDARY_TO_CENTERLINE_M
    offenders = []
    for idx, s in enumerate(layout.shapes):
        if s.role != "junction":
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        bnd = s.polygon.boundary
        L = bnd.length
        n_steps = max(2, int(L / BOUNDARY_SAMPLE_STEP_M) + 1)
        max_d = 0.0
        max_pt = (0.0, 0.0)
        for i in range(n_steps):
            u = min(L, i * BOUNDARY_SAMPLE_STEP_M)
            p = bnd.interpolate(u)
            d = centers.distance(p)
            if d > max_d:
                max_d = d
                max_pt = (p.x, p.y)
        if max_d > cap:
            offenders.append((
                max_d, _shape_label(layout, idx, s),
                s.polygon.area, max_pt))
    offenders.sort(reverse=True)
    summary = "; ".join(
        f"{lbl} max_d={d:.1f}m at ({mp[0]:.0f},{mp[1]:.0f}) "
        f"area={a:,.0f} m²"
        for d, lbl, a, mp in offenders[:5])

    baseline = JUNCTION_BOUNDARY_DISTANCE_REGRESSION_BASELINE.get(icao)
    if baseline:
        worst_d = offenders[0][0] if offenders else 0.0
        n = len(offenders)
        assert n <= baseline["max_offenders"], (
            f"{icao}: {n} junctions exceed {cap:.0f} m centerline "
            f"distance — exceeds known-bad baseline of "
            f"{baseline['max_offenders']}.  Top: {summary}.")
        assert worst_d <= baseline["max_distance_m"] + 1.0, (
            f"{icao}: worst boundary distance {worst_d:.1f} m "
            f"exceeds known-bad baseline of "
            f"{baseline['max_distance_m']:.1f} m.  Top: {summary}.")
    else:
        assert not offenders, (
            f"{icao}: {len(offenders)} junction polygon(s) have "
            f"boundary points > {cap:.0f} m from nearest "
            f"taxi/runway centerline.  Top: {summary}.")


@pytest.mark.parametrize("icao", _test_airports())
def test_junction_vertices_have_source(icao):
    """Every junction vertex must originate from a geometric
    source:

    * A corner of an adjacent rect (sloping or runway), apron,
      terminal, groundside, or boundary polygon, OR
    * An apt.dat row-110 pavement polygon vertex — junctions are
      built as ``pav_union.difference(rects)`` and inherit row-110
      perimeter vertices structurally (see junction_emit.py).

    Vertices without a source are orphans added by densification
    or buffer rounding and must be eliminated.

    This is the dual of ``test_junction_neighbour_corners_shared``.

    Per user 2026-05-18: no airport-specific exemptions, no
    densification of junction perimeters.
    """
    from auto_patch.layout import SHARED_VERTEX_TOL_M

    layout = _build_layout(icao)

    # Source shapes: anything that contributes a real geometric
    # corner that a junction can legitimately anchor on.  Other
    # junctions are excluded — two junctions sharing a vertex
    # doesn't ground it in source geometry.  ROLE_RUNWAY_CROSSING
    # IS included because its corners come from runway-segment
    # union geometry (runway-derived, authoritative source — same
    # category as ROLE_RUNWAY corners).
    SOURCE_ROLES = {
        "runway", "primary_parallel", "secondary_parallel",
        "stub", "cross_connector",
        "apron", "building", "groundside_pavement", "boundary",
        "tunnel_ramp", "retaining_wall",
        "runway_crossing",
    }
    source_corners: List[Tuple[float, float]] = []
    for s in layout.shapes:
        if s.role not in SOURCE_ROLES:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except Exception:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        source_corners.extend(
            (float(c[0]), float(c[1])) for c in coords)
    # apt.dat row-110 pavement polygon vertices: junction perimeters
    # following row-110 inherit these exactly via the residue
    # subtraction.  Captured on the layout at pavement-union build
    # time so the test sees the same source the builder did.
    source_corners.extend(
        getattr(layout, "apt_pavement_vertices", []) or [])

    if not source_corners:
        pytest.skip(
            f"{icao}: no source-shape corners to anchor junctions")

    tol = SHARED_VERTEX_TOL_M
    tol_sq = tol * tol
    # apt.dat row-110 boundary line: a junction vertex that sits
    # ON a row-110 edge (between two row-110 vertices) is also a
    # legitimate inheritance from the pavement union, not an
    # orphan from densification / buffer drift.  Accept any
    # junction vertex within ``BOUNDARY_TOL`` of the boundary line.
    # Per user 2026-05-19: row-110 boundary tolerance is wider than
    # the shared-vertex tolerance because shapely's ``difference()``
    # / ``buffer(0)`` rounding can place a difference-derived vertex
    # ~0.7 m off the source LineString even when its underlying
    # canonical point IS on the boundary in JOSM rendering.  1 m
    # covers that drift; orphans from real densification still flag.
    BOUNDARY_TOL = 1.0
    pav_boundary = getattr(layout, "apt_pavement_boundary", None)
    # Groundside-clearance anchoring: a junction ring that runs along
    # a groundside cut inherits the PRE-separation cut line; the
    # groundside polygon itself is then pushed GROUNDSIDE_CLEARANCE_M
    # (1.0 m) back by _separate_groundside_from_airside (user
    # 2026-05-22 — groundside shares no node/edge with airside).
    # Such vertices ARE source-anchored — to the cut — but sit
    # exactly one clearance gap from the moved groundside edge.
    # Accept any junction vertex within clearance + shared-vertex
    # tolerance of a groundside boundary.  (First exercised by s81
    # hangar pads, whose groundside lots can abut a junction face.)
    from auto_patch.groundside import GROUNDSIDE_CLEARANCE_M
    gs_boundary = None
    _gs_polys = [s.polygon for s in layout.shapes
                 if s.role == "groundside_pavement"
                 and s.polygon is not None and not s.polygon.is_empty]
    if _gs_polys:
        from shapely.ops import unary_union as _uu
        gs_boundary = _uu([p.boundary for p in _gs_polys])
    GS_TOL = GROUNDSIDE_CLEARANCE_M + SHARED_VERTEX_TOL_M

    # Spine source (user ruling 2026-06-17, docs/junction_centerline_-
    # spine.md): the junction-centerline-spine feature (O4_JCT_SPINE)
    # places INTERIOR nodes ON each crossing route-graph taxi centerline
    # so the corridor profile renders through the junction.  Such a vertex
    # IS source-anchored — to the centerline — though it is neither a
    # neighbour corner nor a row-110 perimeter vertex.  Accept any junction
    # vertex within CL_TOL of a route-graph centerline.  No-op gate-off:
    # ring junctions carry no interior centerline vertices.
    cl_geoms = []
    for _csrc in ((getattr(layout, "apt_taxi_centerlines", []) or []),
                  (getattr(layout, "_discovered_centerlines", []) or [])):
        for _it in _csrc:
            _ln = _it[0] if isinstance(_it, tuple) else _it
            if _ln is not None and not _ln.is_empty:
                cl_geoms.append(_ln)
    cl_union = None
    if cl_geoms:
        from shapely.ops import unary_union as _uu_cl
        cl_union = _uu_cl(cl_geoms)
    CL_TOL = 1.0

    orphans: List[str] = []
    for idx, s in enumerate(layout.shapes):
        if s.role != "junction":
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        for v_idx, (vx, vy) in enumerate(coords):
            best_d_sq = min(
                (cx - vx) ** 2 + (cy - vy) ** 2
                for cx, cy in source_corners)
            if best_d_sq <= tol_sq:
                continue
            # Tile-cut seam vertex: sourced by the slice, not an
            # apt.dat corner (tile_cut places it ~half_width off the
            # integer tile line).  See conftest.is_tile_seam_vertex.
            if is_tile_seam_vertex(layout, vx, vy):
                continue
            # Fall back to row-110 boundary distance.
            d = math.sqrt(best_d_sq)
            if pav_boundary is not None:
                try:
                    d_b = pav_boundary.distance(Point(vx, vy))
                except Exception:
                    d_b = float("inf")
                if d_b <= BOUNDARY_TOL:
                    continue
                d = min(d, d_b)
            # Groundside-clearance anchoring (see GS_TOL above).
            if gs_boundary is not None:
                try:
                    d_g = gs_boundary.distance(Point(vx, vy))
                except Exception:
                    d_g = float("inf")
                if d_g <= GS_TOL:
                    continue
                d = min(d, d_g)
            # Spine source: a node ON a route-graph centerline (see
            # CL_TOL above).
            if cl_union is not None:
                try:
                    d_c = cl_union.distance(Point(vx, vy))
                except Exception:
                    d_c = float("inf")
                if d_c <= CL_TOL:
                    continue
                d = min(d, d_c)
            orphans.append(
                f"{_shape_label(layout, idx, s)} "
                f"vertex#{v_idx} at ({vx:.1f},{vy:.1f}) — "
                f"nearest source / pavement edge {d:.2f} m away")

    assert not orphans, (
        f"{icao}: {len(orphans)} junction vertex(es) have no "
        f"source-shape corner within {tol:.2f} m or pavement "
        f"edge within {BOUNDARY_TOL:.2f} m.  First 5:\n  "
        + "\n  ".join(orphans[:5]))


@pytest.mark.parametrize("icao", _test_airports())
def test_junction_neighbour_corners_shared(icao):
    """Coverage invariant: for every junction polygon, every vertex
    of a neighbouring shape (rect / runway / terminal / another
    junction) that lies within ``ORPHAN_NEAR_PERIMETER_M`` of the
    junction's perimeter must coincide (within
    ``ORPHAN_SAME_VERTEX_TOL_M``) with one of the junction's own
    ring vertices.

    Failures are the "adjacent-but-not-shared" pattern that
    produces visible elevation cliffs in X-Plane (CYXY -10070, the
    HECA junction-cluster issue).
    """
    layout = _build_layout(icao)
    junctions = [
        (idx, s) for idx, s in enumerate(layout.shapes)
        if s.role == "junction"
        and s.polygon is not None
        and not s.polygon.is_empty]
    others = [
        (idx, s) for idx, s in enumerate(layout.shapes)
        if s.role != "junction"
        # Groundside pavement NEVER shares nodes with airside BY
        # DESIGN (user 2026-05-22): _separate_groundside_from_airside
        # holds it exactly GROUNDSIDE_CLEARANCE_M (1.0 m) off every
        # airside ring — right at this test's proximity band — so its
        # vertices can never legally coincide with a junction's and
        # the shared-corner invariant does not apply to it.  (First
        # exercised by s81 hangar pads, whose groundside lots can
        # abut a junction face; terminal groundside always abutted
        # aprons/terminals, which this test doesn't scan.)
        and s.role != "groundside_pavement"
        and s.polygon is not None
        and not s.polygon.is_empty]
    if not junctions or not others:
        return

    # Pre-extract neighbour exterior vertices (skip closing repeat).
    nbr_pts = []
    for n_idx, n_s in others:
        for ox, oy in list(n_s.polygon.exterior.coords)[:-1]:
            nbr_pts.append((n_idx, n_s, ox, oy))

    orphans = []  # (miss_dist, j_label, nbr_label, ox, oy)
    for j_idx, j_s in junctions:
        bnd = j_s.polygon.boundary
        j_coords = list(j_s.polygon.exterior.coords)[:-1]
        j_xs = [c[0] for c in j_coords]
        j_ys = [c[1] for c in j_coords]
        # Quick AABB to skip far-away neighbours.
        x_min, y_min, x_max, y_max = j_s.polygon.bounds
        pad = ORPHAN_NEAR_PERIMETER_M + ORPHAN_SAME_VERTEX_TOL_M
        for n_idx, n_s, ox, oy in nbr_pts:
            if (ox < x_min - pad or ox > x_max + pad
                    or oy < y_min - pad or oy > y_max + pad):
                continue
            d_perim = bnd.distance(Point(ox, oy))
            if d_perim > ORPHAN_NEAR_PERIMETER_M:
                continue
            # Vertex is on the junction's perimeter — must coincide
            # with one of the junction's own vertices.
            d_min = min(
                math.hypot(ox - jx, oy - jy)
                for jx, jy in zip(j_xs, j_ys))
            if d_min > ORPHAN_SAME_VERTEX_TOL_M:
                orphans.append((
                    d_min,
                    _shape_label(layout, j_idx, j_s),
                    _shape_label(layout, n_idx, n_s),
                    ox, oy))

    cap = ORPHAN_NEIGHBOUR_VERTEX_REGRESSION_BASELINE.get(
        icao, MAX_ORPHAN_NEIGHBOUR_VERTICES)
    orphans.sort()
    summary = "; ".join(
        f"{j_lbl} ⟂ {n_lbl} at ({ox:.1f},{oy:.1f}) miss={d:.2f}m"
        for d, j_lbl, n_lbl, ox, oy in orphans[:5])
    assert len(orphans) <= cap, (
        f"{icao}: {len(orphans)} neighbour vertex(es) sit within "
        f"{ORPHAN_NEAR_PERIMETER_M:.1f} m of a junction's "
        f"perimeter but more than {ORPHAN_SAME_VERTEX_TOL_M:.2f} m "
        f"from any junction vertex (cap {cap}).  Top: {summary}.")

# (session 51) test_taxi_rects_not_alongside_apron was REMOVED:
# it encoded the OLD absorption-dissolves-everything model (a sloping
# rect must NEVER share its sloping edge with a junction/apron) and
# was already @skip.  The real, narrower invariant — no junction
# vertex on a rect's sloping edge interior, only the two endpoint
# corners may be shared — is C9 (test_junction_no_long_edge_proximity).
# See docs/pipeline_invariants.md.

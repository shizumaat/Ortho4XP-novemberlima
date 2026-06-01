"""Geometry regression tests for the pavement builder.

Skipped automatically unless an X-Plane install is available (the
builder needs apt.dat + DSF + DEM tiles).  When run, builds each
test airport and asserts:

* **No self-overlap**: emitted pavement shapes must not overlap each
  other beyond a small tolerance.  Catches the SPJC regression where
  DSF visual overlays (e.g. ``zannespol/Asphalt_2_Green_T80.pol``
  with ``LAYER_GROUP taxiways +1``) duplicated the apt.dat row-110
  pavement coverage and produced 21 overlapping shape pairs covering
  ~15K m² of doubled area.
* **Coverage envelope**: total emitted pavement area must not exceed
  the source pavement (apt.dat row-110 ⊕ runway corners ⊕ surviving
  DSF) by more than a small fraction.  Catches the regression where
  a single DSF overlay polygon contributed 1.45M m² of "pavement"
  that wasn't pavement at all — bulk over-coverage of grass/decor
  areas.

Both checks would have caught the in-flight Phase 1 regression at
SPJC immediately.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import (
    airports_under_test, baseline_airports,
    xplane_available, xplane_root,
)


def _test_airports() -> list:
    """Union of baseline airports (always-run) + env-gated airports.
    Per user 2026-05-16: invariant tests run on every canonical
    baseline airport unconditionally."""
    seen = set()
    out = []
    for ic in list(baseline_airports()) + list(airports_under_test()):
        if ic not in seen:
            seen.add(ic)
            out.append(ic)
    return out

_HERE = Path(__file__).resolve().parent
_TOOLS = _HERE.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))


def _xplane_root() -> str:
    return xplane_root()


def _xplane_available() -> bool:
    return xplane_available()


pytestmark = pytest.mark.skipif(
    not _xplane_available(),
    reason="X-Plane install not found (set XPLANE_ROOT to override)",
)


# Self-overlap: zero, universal, no per-airport exceptions (user
# 2026-05-31).  Any two emitted pavement shapes overlapping is a hard
# invariant violation — X-Plane mesh generation can't handle it.  The
# check itself lives in ``auto_patch.verification.check_self_overlap``
# (shared with the Ortho4XP build-time verification); the test below
# just calls it and asserts zero.

# Coverage is now checked per-shape and source-relative
# (test_pavement_rests_on_source → verification.check_source_adjacency),
# replacing the old whole-airport area-ratio with its per-airport caps.


def _build_layout(icao: str):
    # Shared session cache (conftest) — built once per airport per run.
    from conftest import cached_airport_layout
    return cached_airport_layout(icao)


def _no_self_overlap_airports():
    """Union of canonical baseline airports + any env-driven extras
    (de-duplicated, baseline-first order).  Merges the previously-
    separate env-gated and baseline variants of this test."""
    seen, out = set(), []
    for icao in tuple(baseline_airports()) + tuple(_test_airports()):
        if icao in seen:
            continue
        seen.add(icao)
        out.append(icao)
    return out


@pytest.mark.parametrize("icao", _no_self_overlap_airports())
def test_no_self_overlap(icao):
    """Invariant A1 (single-solve, see docs/pipeline_invariants.md):
    every paved metre belongs to exactly one shape — NO two emitted
    pavement shapes may overlap, ever.  No floating-point allowance
    except where ``SELF_OVERLAP_BASELINE_M2`` documents a known cap.

    Catches: KPHX taxi-bridge overlap, SPJC DSF ``zannespol``
    duplicate coverage, any future absorption / clip pass that fails
    to remove an absorbed sub-rect.

    (session 51) Merged the env-gated and baseline-only variants of
    this test into one parametrization over the union of the two
    airport sets.
    """
    from auto_patch.verification import check_self_overlap, describe_shape, build_taxi_index
    layout = _build_layout(icao)
    overlap_pairs = check_self_overlap(layout)
    overlap_area = sum(a for a, _, _, _ in overlap_pairs)
    ti = build_taxi_index(layout)
    summary = "; ".join(
        f"{a:.4f} m² @ {loc}: {describe_shape(layout, ia, ti)} ∩ "
        f"{describe_shape(layout, ib, ti)}"
        for a, ia, ib, loc in overlap_pairs[:5])
    assert not overlap_pairs, (
        f"{icao}: {len(overlap_pairs)} overlapping shape pair(s), "
        f"total {overlap_area:,.4f} m² (zero tolerance, no per-airport "
        f"exceptions).  Worst: {summary}.")


@pytest.mark.parametrize("icao", _test_airports())
def test_no_vertex_on_sloping_rect_edge(icao):
    """Per user 2026-04-28 invariant: a junction (or any non-rect)
    polygon vertex can only land on a sloping rect's CORNER, never
    on the interior of one of its four edges.  Edge-interior
    coincidence injects an extra elevation constraint at a non-
    corner location and breaks the rect's straight-line slope.

    Caught the CYXY runway-crossing-junction regression where
    ``_resolve_runway_crossings``'s ``unary_union`` plus the
    downstream 2 m runway-shrink ``difference`` were placing 4
    junction vertices 2–5 m along surviving runway segments' long
    edges (near corners but not at them).
    """
    from auto_patch.verification import (
        check_vertex_on_sloping_edge, describe_shape, build_taxi_index)
    layout = _build_layout(icao)
    violations = check_vertex_on_sloping_edge(layout)
    ti = build_taxi_index(layout)
    summary = "; ".join(
        f"{describe_shape(layout, idx, ti)} — {detail} @ {loc}"
        for idx, detail, loc in violations[:5])
    assert not violations, (
        f"{icao}: {len(violations)} sloping-rect invariant violation(s).  "
        f"Sloping rects must have exactly 4 corners; junction/apron "
        f"polygons may share only CORNERS with sloping rects, never edge "
        f"interiors.  First {min(5, len(violations))}: {summary}.")


def _rect_flat_edges_from_shape(shape):
    """The two FLAT edges of a 4-corner rect — perpendicular to
    ``source_axis`` (where altitude is constant along the edge).
    Mirror of ``_rect_sloping_edges_from_shape`` (in
    test_junction_rules.py) but selects the bottom-2-dot-product
    indices.  Returns [] if shape isn't a 4-corner rect.
    """
    import math
    poly = shape.polygon
    coords = list(poly.exterior.coords)
    if not coords:
        return []
    if coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) != 4:
        return []
    edges = [(coords[i], coords[(i + 1) % 4]) for i in range(4)]
    sa = getattr(shape, "source_axis", None)
    if sa is not None and not sa.is_empty:
        ax_pts = list(sa.coords)
        if len(ax_pts) >= 2:
            axdx = ax_pts[-1][0] - ax_pts[0][0]
            axdy = ax_pts[-1][1] - ax_pts[0][1]
            axlen = math.hypot(axdx, axdy)
            if axlen >= 1e-6:
                aux, auy = axdx / axlen, axdy / axlen
                dots = []
                for a, b in edges:
                    ex, ey = b[0] - a[0], b[1] - a[1]
                    elen = math.hypot(ex, ey)
                    if elen < 1e-6:
                        dots.append(0.0)
                        continue
                    dots.append(abs(ex * aux + ey * auy) / elen)
                flat_idx = sorted(range(4), key=lambda i: dots[i])[:2]
                return [edges[i] for i in flat_idx]
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in edges]
    short_idx = sorted(range(4), key=lambda i: lengths[i])[:2]
    return [edges[i] for i in short_idx]


@pytest.mark.parametrize("icao", _test_airports())
def test_sloping_rect_slopes_only_along_axis(icao):
    """A canonical sloping taxi rect may slope ONLY along its centerline
    (``source_axis``): its two AXIS-END edges (perpendicular to ``source_axis``)
    must each be FLAT — both endpoints at the same elevation.  A non-flat
    axis-end means the rect slopes ACROSS its centerline (X-Plane renders a
    perpendicular tilt).  Per user 2026-05-22: "taxi rects can only slope along
    the axis of their taxi centerline."

    Scope: ONLY canonical sloping rects (``altitude_high``/``altitude_low``
    set).  ``node_altitudes`` shapes are EXEMPT — a tile/seam slice legitimately
    produces irregular per-vertex node_altitudes polygons whose edges are not
    expected to be flat (user 2026-05-22: a sliced node_altitudes shape "is a
    reasonable shape given the slice").  Hi/lo rects satisfy the invariant by
    the ``[H, L, L, H]`` convention; the guard catches a future path that sets
    altitude_high/low on a non-canonically-ordered ring.  Tolerance 0.3 m.
    """
    from auto_patch.layout import corner_alts_from_high_low
    layout = _build_layout(icao)
    sloping_roles = {
        "primary_parallel", "secondary_parallel", "stub",
        "cross_connector", "service_road",
    }
    TOL = 0.3
    violations = []
    for s in layout.shapes:
        if s.role not in sloping_roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        # node_altitudes shapes are slice-conforming and exempt.
        if s.node_altitudes:
            continue
        if s.altitude_high is None or s.altitude_low is None:
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        alts = corner_alts_from_high_low(s.altitude_high, s.altitude_low)
        cmap = {(round(c[0], 3), round(c[1], 3)): alts[i]
                for i, c in enumerate(coords)}
        for a, b in _rect_flat_edges_from_shape(s):
            za = cmap.get((round(a[0], 3), round(a[1], 3)))
            zb = cmap.get((round(b[0], 3), round(b[1], 3)))
            if za is None or zb is None:
                continue
            if abs(za - zb) > TOL:
                c = s.polygon.centroid
                violations.append((s.ref, c.x, c.y, abs(za - zb)))
                break
    assert not violations, (
        f"{icao}: {len(violations)} taxi rect(s) slope ACROSS their "
        f"centerline (axis-end edge not flat — perpendicular tilt). "
        f"Worst: " + ", ".join(
            f"{r}@({x:.0f},{y:.0f}) Δ{d:.2f}m"
            for r, x, y, d in sorted(violations, key=lambda v: -v[3])[:5]))


@pytest.mark.parametrize("icao", _test_airports())
def test_no_vertex_on_sloping_rect_flat_edge(icao):
    """A sloping rect's FLAT (cross/short) edge — the side
    perpendicular to ``source_axis`` — is where the rect meets a
    junction (or runway, or another rect).  That meeting must be
    1:1 vertex sharing: only the rect's 2 flat-edge CORNERS are
    legal shared vertices, never a node on the edge interior.

    A third node mid-flat-edge constrains the rect's slope at a
    non-corner location, producing a step where the junction's
    elevation diverges from the rect's linear-corner slope.

    Tolerance 1.0 m: a vertex within 1 m perpendicular of the flat
    edge that isn't within 1 m of either corner is flagged.  This
    catches the post-elevation ``_push_junction_vertices_outside_-
    pavement`` behaviour that nudges junction verts ~1 m off the
    rect — at exactly the threshold the original 0.5 m all-edge
    test misses.
    """
    from auto_patch.verification import (
        check_vertex_on_flat_edge, describe_shape, build_taxi_index)
    layout = _build_layout(icao)
    violations = check_vertex_on_flat_edge(layout)
    ti = build_taxi_index(layout)
    summary = "; ".join(
        f"{describe_shape(layout, idx, ti)} — {detail} @ {loc}"
        for idx, detail, loc in violations[:8])
    assert not violations, (
        f"{icao}: {len(violations)} sloping-rect flat-edge invariant "
        f"violation(s).  Sloping rects must share flat (cross) edges 1:1 "
        f"— only the 2 corners are legal shared vertices.  First "
        f"{min(8, len(violations))}: {summary}.")


@pytest.mark.parametrize("icao", _test_airports())
def test_rect_short_edges_connect(icao):
    """Per user 2026-04-29: a sloping rect (primary_parallel,
    secondary_parallel, stub, cross_connector) has TWO short
    edges, and each short edge represents an end where the
    corridor meets something else — a junction, a runway, a
    terminal, or another rect via shared vertices.  A short edge
    with both corners *un*shared with any other shape means the
    rect is ending in the middle of nowhere — usually an
    artifact of the long-edge-adjacent absorption clipping the
    rect mid-corridor and not extending the surrounding junction
    polygon to share the new clip-boundary corners.

    For each rect, check that EACH of its two short edges has at
    least one corner shared (within ``CORNER_SHARE_TOL_M``) with
    a non-rect-self vertex.  An entirely-disconnected short edge
    is the failure case.
    """
    import math
    CORNER_SHARE_TOL_M = 0.5
    layout = _build_layout(icao)
    rect_roles = {"primary_parallel", "secondary_parallel",
                  "stub", "cross_connector"}

    # A short edge that lies on a tile-cut boundary (the integer
    # lat/lon line the tile slice runs along, ~``half_width`` m away)
    # legitimately connects to nothing on this side — the neighbour
    # tile's geometry + X-Plane's terrain mesh bridge it.  The tile-cut
    # clip-back (``tile_cut._clip_sloping_rect_piece``) ends a clipped
    # taxiway rect / its node_altitudes filler on exactly such an edge,
    # so exclude edges whose both corners sit within ``TILE_EDGE_TOL_M``
    # of an integer lat or lon line.
    TILE_EDGE_TOL_M = 8.0
    lat0, lon0 = layout.anchor

    def _on_tile_edge(x, y):
        lat, lon = layout.m_to_ll(x, y)
        dlat_m = abs(lat - round(lat)) * 111195.0
        dlon_m = (abs(lon - round(lon)) * 111195.0
                  * math.cos(math.radians(lat)))
        return dlat_m < TILE_EDGE_TOL_M or dlon_m < TILE_EDGE_TOL_M
    # Collect every vertex from every shape with its source shape
    # id, then for each rect check both short-edge corners against
    # all OTHER shapes' vertices.
    all_vertices: list = []  # list of (x, y, shape_index)
    for si, s in enumerate(layout.shapes):
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except Exception:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        for x, y in coords:
            all_vertices.append((x, y, si))
    failures = []
    tol2 = CORNER_SHARE_TOL_M * CORNER_SHARE_TOL_M
    for ri, r in enumerate(layout.shapes):
        if r.role not in rect_roles:
            continue
        if r.polygon is None or r.polygon.is_empty:
            continue
        try:
            rc = list(r.polygon.exterior.coords)
        except Exception:
            continue
        if rc and rc[0] == rc[-1]:
            rc = rc[:-1]
        if len(rc) != 4:
            continue
        # Short edges per ``_rect_from_axis_extended`` convention:
        #   short edge A = corners 0 + 3 (one end)
        #   short edge B = corners 1 + 2 (other end)
        for end_label, (i_a, i_b) in (("end_A", (0, 3)),
                                        ("end_B", (1, 2))):
            ax, ay = rc[i_a]
            bx, by = rc[i_b]
            shared_a = False
            shared_b = False
            for vx, vy, vsi in all_vertices:
                if vsi == ri:
                    continue
                if (not shared_a
                        and (vx - ax) ** 2 + (vy - ay) ** 2 <= tol2):
                    shared_a = True
                if (not shared_b
                        and (vx - bx) ** 2 + (vy - by) ** 2 <= tol2):
                    shared_b = True
                if shared_a and shared_b:
                    break
            if not shared_a and not shared_b:
                # Tile-cut boundary edge — connects via the neighbour
                # tile, not within this layout.
                if _on_tile_edge(ax, ay) and _on_tile_edge(bx, by):
                    continue
                # Discovered (medial-axis) lanes may legitimately DEAD-END at
                # the pavement boundary (user 2026-05-28, SPJC TX20): unlike a
                # referenced taxiway, a "TX" lane carved from unreferenced
                # pavement can terminate at a real pavement tip with nothing to
                # connect to.  Exempt such an end ONLY when (a) the rect is a
                # discovered lane, (b) it connects at its OTHER short edge (so
                # it's a dead-end lane, not a fully-floating sliver), and (c)
                # the dangling end is GENUINELY ISOLATED — both corners far
                # (> ``DEAD_END_ISOLATION_M``) from any other shape's vertex.
                # A near-miss gap (something close, e.g. SPJC TX15 ~10 m from a
                # junction) is a MISSING CONNECTION, not a dead-end, so it stays
                # flagged.  Two signatures of a legitimate dead-end:
                #   * GENUINELY ISOLATED — both corners far (> DEAD_END_
                #     ISOLATION_M) from any other vertex (SPJC TX20); OR
                #   * PAVEMENT TIP — the dangling short edge lies ON the apt+DSF
                #     pavement boundary, i.e. the lane reaches the physical edge
                #     of the pavement and stops (HECA TX52).  The near-miss case
                #     (TX15) terminates in the pavement INTERIOR short of a
                #     junction, so it is not on the boundary and stays flagged.
                DEAD_END_ISOLATION_M = 25.0
                TIP_BOUNDARY_TOL_M = 1.0
                if (r.ref or "").startswith("TX"):
                    # other short edge of this rect
                    o_a, o_b = ((1, 2) if end_label == "end_A" else (0, 3))
                    oax, oay = rc[o_a]
                    obx, oby = rc[o_b]
                    other_shared = False
                    near_iso2 = DEAD_END_ISOLATION_M ** 2
                    min_a2 = min_b2 = float("inf")
                    for vx, vy, vsi in all_vertices:
                        if vsi == ri:
                            continue
                        if not other_shared and (
                                (vx - oax) ** 2 + (vy - oay) ** 2 <= tol2
                                or (vx - obx) ** 2 + (vy - oby) ** 2 <= tol2):
                            other_shared = True
                        da2 = (vx - ax) ** 2 + (vy - ay) ** 2
                        db2 = (vx - bx) ** 2 + (vy - by) ** 2
                        if da2 < min_a2:
                            min_a2 = da2
                        if db2 < min_b2:
                            min_b2 = db2
                    isolated = (min_a2 > near_iso2 and min_b2 > near_iso2)
                    on_tip = False
                    pav_b = getattr(layout, "apt_pavement_boundary", None)
                    if pav_b is not None:
                        from shapely.geometry import Point as _P
                        try:
                            on_tip = (
                                pav_b.distance(_P(ax, ay)) <= TIP_BOUNDARY_TOL_M
                                and pav_b.distance(_P(bx, by))
                                <= TIP_BOUNDARY_TOL_M)
                        except Exception:
                            on_tip = False
                    if other_shared and (isolated or on_tip):
                        continue        # genuine discovered dead-end
                failures.append({
                    "ref": r.ref or "?",
                    "role": r.role,
                    "end": end_label,
                    "corner_a": (ax, ay),
                    "corner_b": (bx, by),
                })
    if failures:
        summary = "; ".join(
            f"{f['role']}({f['ref']}) {f['end']}: "
            f"({f['corner_a'][0]:.1f},{f['corner_a'][1]:.1f}) and "
            f"({f['corner_b'][0]:.1f},{f['corner_b'][1]:.1f}) "
            f"both unshared"
            for f in failures[:5])
        msg = (f"{icao}: {len(failures)} rect short edge(s) with "
               f"both corners disconnected from any other shape.  "
               f"A taxi rect's short edge always meets something "
               f"(junction / runway / terminal / other rect).  "
               f"First {min(5, len(failures))}: {summary}.")
        assert False, msg


@pytest.mark.parametrize("icao", _test_airports())
def test_pavement_rests_on_source(icao):
    """Every emitted PAVEMENT shape must rest on real source pavement
    (apt.dat row-110 ∪ DSF ∪ runway) — per-shape and source-relative,
    no per-airport ratio.  Replaces the old whole-airport coverage-ratio
    test: that needed a hand-tuned per-airport cap and only fired if a
    baseline's source data changed.  This catches the same failure
    (pavement emitted where no source exists — a spurious synthesis or a
    non-pavement polygon tagged as pavement) on ANY airport, and names
    the exact offending shape + lat/lon.

    Shares ``auto_patch.verification.check_source_adjacency`` with the
    Ortho4XP build-time verification.
    """
    from auto_patch.verification import check_source_adjacency, describe_shape, build_taxi_index
    layout = _build_layout(icao)
    offenders = check_source_adjacency(layout)
    ti = build_taxi_index(layout)
    summary = "; ".join(
        f"{describe_shape(layout, idx, ti)} {area:.0f} m² "
        f"({frac*100:.0f}% on source @ {loc})"
        for idx, area, frac, loc in offenders[:5])
    assert not offenders, (
        f"{icao}: {len(offenders)} emitted pavement shape(s) rest on no "
        f"apt.dat/DSF source (zero tolerance).  Likely a spurious "
        f"synthesis or a non-pavement source polygon tagged as pavement.  "
        f"First {min(5, len(offenders))}: {summary}.")


@pytest.mark.parametrize("icao", _test_airports())
def test_terminal_strictly_flat(icao):
    """Invariant H26 (single-solve, see docs/pipeline_invariants.md):
    terminals move as a WHOLE UNIT — a single ``altitude`` tag, no
    per-node deviation.  Equivalent: terminal shapes must not carry
    ``node_altitudes`` or ``altitude_high``/``altitude_low`` (those
    encode per-vertex / two-end variation that would tilt the pad).

    This is the strictest of the role-specific solver-freedom rules
    (H26-H28).  Junctions and aprons may carry per-vertex
    ``node_altitudes``; sloping rects may carry independent
    ``altitude_high``/``altitude_low``; terminals are flat-only.
    """
    from auto_patch.verification import check_terminal_flat, describe_shape
    layout = _build_layout(icao)
    violations = check_terminal_flat(layout)
    summary = "; ".join(
        f"{describe_shape(layout, idx)} {detail}"
        for idx, detail, _loc in violations[:5])
    assert not violations, (
        f"{icao}: {len(violations)} terminal flatness violation(s) "
        f"(H26 — terminals are a single flat altitude).  {summary}"
        + (f"  ...and {len(violations)-5} more"
           if len(violations) > 5 else ""))


@pytest.mark.parametrize("icao", _test_airports())
def test_boundary_ribbon_inside_row130(icao):
    """Invariant F18 (single-solve, see docs/pipeline_invariants.md):
    the airport boundary ribbon lies INSIDE the row-130 line (so the
    ribbon is the transition strip and pavement clips to its inner
    edge).  Equivalent: every ribbon-polygon vertex is inside (or on
    the boundary of) ``layout.airport_boundary``.

    Excludes the ``boundary_dem_bridge`` overlay, which legitimately
    extends OUTSIDE the airport boundary into surrounding terrain.
    """
    from auto_patch.layout import ROLE_BOUNDARY
    from shapely.geometry import Point as _Point
    layout = _build_layout(icao)
    row130 = layout.airport_boundary
    if row130 is None or row130.is_empty:
        pytest.skip(f"{icao}: layout has no airport_boundary")
    # Allow a small float tolerance for vertices that sit ON the
    # row-130 line itself (the ribbon's outer edge often coincides
    # with row-130 by design).
    ON_BOUNDARY_TOL_M = 0.5
    violations = []
    for s_idx, s in enumerate(layout.shapes):
        if s.role != ROLE_BOUNDARY:
            continue
        if s.ref == "boundary_dem_bridge":
            continue  # legitimately outside row-130
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        for v_idx, (vx, vy) in enumerate(coords):
            p = _Point(vx, vy)
            if row130.contains(p):
                continue
            d = p.distance(row130.boundary)
            if d <= ON_BOUNDARY_TOL_M:
                continue
            violations.append(
                f"ribbon#{s_idx} (ref={s.ref}) vertex#{v_idx} at "
                f"({vx:.2f},{vy:.2f}) is OUTSIDE row-130 at "
                f"distance {d:.2f}m")
    assert not violations, (
        f"{icao}: {len(violations)} ribbon vertex(es) outside the "
        f"row-130 boundary line.  First 5: "
        + "; ".join(violations[:5])
        + (f"  ...and {len(violations)-5} more"
           if len(violations) > 5 else ""))

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
    # SPJC=1 baseline (2026-06-20, user-accepted as visually fine in X-Plane):
    # one junction vertex lands on runway 16L/34R's sloping edge (t=0.911,
    # d=0.00 m) from the spine-slice/cap geometry.  ⚠ candidate to fix; not
    # seam-related.
    _SLOPING_EDGE_BASELINE = {"SPJC": 1}
    cap = _SLOPING_EDGE_BASELINE.get(icao, 0)
    assert len(violations) <= cap, (
        f"{icao}: {len(violations)} sloping-rect invariant violation(s) "
        f"(baseline {cap}).  Sloping rects must have exactly 4 corners; "
        f"junction/apron polygons may share only CORNERS with sloping rects, "
        f"never edge interiors.  First {min(5, len(violations))}: {summary}.")


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
    from auto_patch.verification import (
        check_sloping_rect_axis, describe_shape, build_taxi_index)
    layout = _build_layout(icao)
    violations = check_sloping_rect_axis(layout)
    ti = build_taxi_index(layout)
    summary = "; ".join(
        f"{describe_shape(layout, idx, ti)} — {detail} @ {loc}"
        for idx, detail, loc in violations[:5])
    assert not violations, (
        f"{icao}: {len(violations)} taxi rect(s) slope ACROSS their "
        f"centerline (axis-end edge not flat — perpendicular tilt).  "
        f"Worst: {summary}")


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
    from auto_patch.verification import (
        check_rect_short_edges, describe_shape, build_taxi_index)
    layout = _build_layout(icao)
    failures = check_rect_short_edges(layout)
    ti = build_taxi_index(layout)
    summary = "; ".join(
        f"{describe_shape(layout, idx, ti)} — {detail} @ {loc}"
        for idx, detail, loc in failures[:5])
    assert not failures, (
        f"{icao}: {len(failures)} rect short edge(s) disconnected from "
        f"any other shape.  A taxi rect's short edge always meets "
        f"something (junction / runway / terminal / other rect).  First "
        f"{min(5, len(failures))}: {summary}.")


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

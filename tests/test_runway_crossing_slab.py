"""Unit tests for the runway-crossing slab bracket (the KBNA 02L/20R+13/31
wedge fix) and the airside mid-edge STEP gate.

The bug: ``_single_poly_station_slab`` snapped the crossing slab bracket
OUTWARD to the nearest kept profile station.  With a sparse profile (physical
ends + a tight crossing-anchor cluster) the overlap projects just outside the
cluster, so the bracket snapped all the way to the physical ends — the slab
ballooned across the ENTIRE runway and the crossing junction inherited both
runways' full profile range (KBNA: an 8.6 m / 731 % step between a 174 m
crossing vertex and the 165 m runway edge 1.18 m away).  The fix clamps the
slab to the overlap's own extent, snapping to a station only within a tight
tolerance.
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from shapely.geometry import Polygon

from auto_patch import elevation as EL


def _runway_candidate(ref, ax, ay, bx, by, width, fractions, elevs):
    """Build a single-poly runway candidate ``(poly, alts, geometry)`` with a
    given sparse profile, matching ``_build_single_poly_runway_ring``'s output
    shape but constructed directly so the test controls the station list."""
    length = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / length, (by - ay) / length
    px, py = -uy * width / 2.0, ux * width / 2.0
    stations = list(zip(fractions, elevs))
    ring, alts = [], []
    for f, e in stations:
        x = ax + f * (bx - ax)
        y = ay + f * (by - ay)
        ring.append((x + px, y + py))
        alts.append(round(e, 2))
    for f, e in reversed(stations):
        x = ax + f * (bx - ax)
        y = ay + f * (by - ay)
        ring.append((x - px, y - py))
        alts.append(round(e, 2))
    poly = Polygon(ring)
    geometry = {
        "axis_a": (ax, ay), "axis_b": (bx, by),
        "length_m": length, "width_m": width,
        "unit": (ux, uy), "perp": (px, py),
        "stations": stations,
        "fractions": list(fractions), "elevs": list(elevs),
    }
    return poly, alts + [alts[0]], geometry


# A long runway A along +x with a SPARSE profile: physical ends at 180 / 168
# and a tight 174 m crossing-anchor cluster near mid-runway.  A perpendicular
# runway C crosses it at (1000, 0), also 174 m there.
_A = _runway_candidate("A/B", 0.0, 0.0, 2000.0, 0.0, 60.0,
                       [0.0, 0.49, 0.51, 1.0], [180.0, 174.0, 174.0, 168.0])
_C = _runway_candidate("C/D", 1000.0, -800.0, 1000.0, 800.0, 50.0,
                       [0.0, 0.48, 0.52, 1.0], [160.0, 174.0, 174.0, 190.0])
_OVERLAP = _A[0].intersection(_C[0])


def test_slab_clamped_to_overlap_not_ballooned():
    """The slab spans only the overlap's own axial extent, not the whole
    runway — the direct guard on the KBNA ballooning cause."""
    slab = EL._single_poly_station_slab(_A[2], _OVERLAP)
    assert slab is not None
    # Overlap on runway A spans x in ~[975, 1025] (fraction ~0.49-0.51).  A
    # clamped slab stays inside a small band around the crossing; the OLD
    # code snapped to the physical ends and covered x in [0, 2000].
    minx, _miny, maxx, _maxy = slab.bounds
    axial = maxx - minx
    assert axial < 120.0, f"slab ballooned: axial extent {axial:.0f} m"
    # It must still cover the physical crossing (the overlap).
    assert slab.buffer(1.0).contains(_OVERLAP.buffer(-1.0))


def test_crossing_junction_is_grade_compliant():
    """The carved crossing junction sits at the reconciled crossing elevation
    (~174 m) throughout — it does NOT inherit either runway's profile
    extremes (180/168/160/190), so no within-shape wedge forms."""
    specs, _pieces = EL._carve_single_poly_crossings(
        {"A/B": _A, "C/D": _C}, [("A/B", "C/D", _OVERLAP)])
    assert len(specs) == 1
    _poly, closed_alts, ref = specs[0]
    assert ref == "A/B+C/D"
    alts = closed_alts[:-1]  # drop the closing repeat
    assert max(alts) - min(alts) < 3.0, (
        f"crossing spans {max(alts) - min(alts):.1f} m — profile-extreme "
        f"bleed (alts={alts})")
    assert all(172.0 <= a <= 176.0 for a in alts), (
        f"crossing vertex off the reconciled 174 m level: {alts}")


def test_slab_snaps_to_a_nearby_station():
    """When a kept station sits right at the overlap edge (within the snap
    tolerance) the slab reuses it — the tight-snap branch still works."""
    # Overlap flush against runway A's 0.49 station edge (x=980) so the low
    # edge is ~1 m from that station.
    ov = Polygon([(979.0, -30.0), (1021.0, -30.0),
                  (1021.0, 30.0), (979.0, 30.0)])
    slab = EL._single_poly_station_slab(_A[2], ov)
    assert slab is not None
    minx, _a, maxx, _b = slab.bounds
    # 0.49*2000 = 980 m is within _XING_SLAB_SNAP_M of x=979 → low edge snaps
    # to 980, not clamped to 979.
    assert abs(minx - 980.0) <= EL._XING_SLAB_SNAP_M + 1e-6


def test_midedge_gate_contact_tolerance_and_pair_filter():
    """check_grade's mid-edge step logic (reused by the verify gate) catches a
    wedge whose steep edge runs 1.5 m from the neighbour only at the gate's
    2 m contact tolerance, and the airside pair filter can exclude a pair."""
    import check_grade as CG

    # A short high edge (way V) 1.5 m from a long low edge (way E).
    wv = CG.Way(wid="-1", role="runway_crossing", ref="X", aeroway="runway",
                nids=["1", "2", "3"], elevs=[174.0, 174.0, 174.0], tags={})
    we = CG.Way(wid="-2", role="runway", ref="Y", aeroway="runway",
                nids=["4", "5", "6"], elevs=[165.0, 165.0, 165.0], tags={})
    wv.tags = {"role": "runway_crossing"}
    we.tags = {"role": "runway"}
    ways = [wv, we]
    verts = [CG.Vertex(way_idx=0, nid="1", x=10.0, y=1.5, elev=174.0)]
    edges = [CG.Edge(way_idx=1, a=(0.0, 0.0), b=(20.0, 0.0), ea=165.0,
                     eb=165.0)]

    # 1 m contact tol: 1.5 m gap → missed.
    miss = CG._check_vertex_to_edge_step(verts, edges, ways,
                                         edge_search_m=5.0, edge_step_m=2.5,
                                         contact_tol_m=1.0)
    assert miss == []
    # 2 m contact tol: 1.5 m gap → caught, ~9 m step.
    hit = CG._check_vertex_to_edge_step(verts, edges, ways,
                                        edge_search_m=5.0, edge_step_m=2.5,
                                        contact_tol_m=2.0)
    assert len(hit) == 1 and hit[0].step_m > 8.0
    # pair filter can exclude it.
    filt = CG._check_vertex_to_edge_step(
        verts, edges, ways, edge_search_m=5.0, edge_step_m=2.5,
        contact_tol_m=2.0, pair_ok=lambda a, b: False)
    assert filt == []

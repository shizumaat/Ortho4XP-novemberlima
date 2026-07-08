"""Unit tests: the PLANE GRADIENT check evaluates in UNCROWNED space.

The plane check (``tools/check_grade.py::_check_plane_gradient``) was the one
crown-blind reader of the three (solver / within-shape pair / plane): a lawful
crowned triangle spanning a ridge node and dropped edge nodes false-flagged
whenever its raw resultant tilt (longitudinal grade ⊕ transverse crown)
exceeded the role cap — SPJC junction #141 read 2.30 % raw vs 1.10 % designed.
The fix lifts each vertex to the solver's uncrowned space ``z' = z +
crown_drop`` (the SAME space ``grade_law.crown_pair_offset`` re-centres the
within-shape pair check to) before computing the plane normal.

Three properties, one test each:

* a crowned triangle whose RAW plane exceeds the cap but whose crown-lifted
  plane is sub-cap is NOT flagged;
* a genuinely steep triangle with no crown drops is STILL flagged (the lift
  cannot legalise a real defect — its nodes are off the field);
* an EMPTY crown field leaves ``z' = z``, byte-identical to the unlifted
  check (uncrowned / old patches keep firing on the same triangle).
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
for _p in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_grade  # noqa: E402


def _identity_meters(lat, lon):
    """Unit-test frame: node 'lat/lon' ARE local meters (y, x)."""
    return (lon, lat)


def _junction_triangle(elevations):
    """A 3-vertex junction ring: A=(0,0), B=(0,30), C=(25,15) meters,
    with the given per-vertex ``elevations`` (A, B, C)."""
    ea, eb, ec = elevations
    nodes = {"-1": (0.0, 0.0), "-2": (30.0, 0.0), "-3": (15.0, 25.0)}
    way = check_grade.Way(
        wid="-10", role="junction", ref="", aeroway="taxiway",
        nids=["-1", "-2", "-3", "-1"],
        elevs=[ea, eb, ec, ea],
        tags={"role": "junction"},
    )
    return nodes, [way]


# The crowned case: A and B are dropped EDGE nodes (crown drop 0.30 m),
# C is the ridge (no drop).  Raw plane: Δz = 0.5 m over 25 m ⇒ 2.0 % > 1.5 %
# (allowance 0.015·25 + 0.03 = 0.405 < 0.5 → would flag).  Crown-lifted:
# A'=B'=10.3, C'=10.5 ⇒ Δz' = 0.2 m over 25 m = 0.8 % → sub-cap.
_CROWNED_ELEVATIONS = (10.0, 10.0, 10.5)
_CROWN_FIELD = {"-1": 0.3, "-2": 0.3}  # C ("-3") is the ridge: no drop


def test_crowned_triangle_not_flagged_in_uncrowned_space():
    nodes, ways = _junction_triangle(_CROWNED_ELEVATIONS)
    violations = check_grade._check_plane_gradient(
        ways, nodes, _identity_meters, 0.015,
        seam_nids=set(), crown_by_nid=_CROWN_FIELD)
    assert violations == [], (
        "designed crown (raw 2.0 %, lifted 0.8 %) must not flag: "
        + "; ".join(f"{v.grade_pct:.2f}%" for v in violations))


def test_genuinely_steep_triangle_still_flagged():
    # Δz = 1.0 m over 25 m = 4 % with NO crown drops on its nodes — the
    # crown field being present must not shield a real defect.
    nodes, ways = _junction_triangle((10.0, 10.0, 11.0))
    off_triangle_field = {"-99": 0.3}  # non-empty, misses this triangle
    violations = check_grade._check_plane_gradient(
        ways, nodes, _identity_meters, 0.015,
        seam_nids=set(), crown_by_nid=off_triangle_field)
    assert len(violations) == 1
    assert violations[0].grade_pct > 1.5


def test_empty_crown_field_is_byte_identical_to_unlifted_check():
    # The SAME crowned triangle flags when the crown field is empty
    # (uncrowned / old patches): z' = z, the pre-fix behaviour.
    nodes, ways = _junction_triangle(_CROWNED_ELEVATIONS)
    for empty_field in ({}, None):
        violations = check_grade._check_plane_gradient(
            ways, nodes, _identity_meters, 0.015,
            seam_nids=set(), crown_by_nid=empty_field)
        assert len(violations) == 1, (
            "without the crown field the raw 2.0 % plane must still flag")
        assert abs(violations[0].grade_pct - 2.0) < 0.1

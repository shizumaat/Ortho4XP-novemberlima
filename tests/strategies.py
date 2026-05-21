"""Hypothesis strategies for auto_patch pure-function property tests.

Each strategy models the valid input space for a function or group of
functions, named after the domain concept it represents.  All inputs
are values the production function is designed to handle (happy path) —
error-path inputs are tested separately, not with ``@given``.

Sections:
  * Atomic — metre-space coordinates, points, tolerances.
  * canonical_points — points and near/separated point sets for
    ``CanonicalPointRegistry``.
  * runway_redistribute — (fractions, elevs) profiles for
    ``_interp_profile``.
  * geometry — points + segments for the junction_rules distance
    helpers.
"""
from __future__ import annotations

import math

from hypothesis import strategies as st


# ── Atomic ────────────────────────────────────────────────────────────

# A finite coordinate in the layout's LOCAL METRE frame.  Bounded to a
# realistic airport-local extent (±200 km of the anchor) and away from
# float extremes so geometry math stays well-conditioned (no inf/NaN,
# no catastrophic cancellation).
coordinate = st.floats(
    min_value=-2.0e5, max_value=2.0e5,
    allow_nan=False, allow_infinity=False,
)

# Tighter coordinate for distance geometry (±10 km) — keeps hypot/sub
# round-off small enough that the property tolerances below are tight.
geo_coordinate = st.floats(
    min_value=-1.0e4, max_value=1.0e4,
    allow_nan=False, allow_infinity=False,
)

# A 2-D point in metre space.
point = st.tuples(coordinate, coordinate)
geo_point = st.tuples(geo_coordinate, geo_coordinate)

# A line segment (a, b) for the point-to-segment distance helpers.
segment = st.tuples(geo_point, geo_point)


# ── canonical_points.CanonicalPointRegistry ─────────────────────────────

# Registry merge radius (metres).  Matches production usage (default
# 0.5 m); kept > 0.1 because the registry floors its spatial-index cell
# size at 0.1 m, and bounded so generated near-points don't overflow.
merge_tol = st.floats(min_value=0.2, max_value=5.0)


@st.composite
def point_and_near_point(draw):
    """``(tol, base, near)`` where ``near`` lies STRICTLY inside ``tol``
    of ``base`` (offset magnitude ≤ 0.9·tol, with margin for float
    error).  Models the contract that two points within ``tol`` must
    resolve to the same canonical entry.
    """
    tol = draw(merge_tol)
    base = draw(point)
    r = draw(st.floats(min_value=0.0, max_value=tol * 0.9,
                       allow_nan=False, allow_infinity=False))
    theta = draw(st.floats(min_value=0.0, max_value=2.0 * math.pi,
                           allow_nan=False, allow_infinity=False))
    near = (base[0] + r * math.cos(theta),
            base[1] + r * math.sin(theta))
    return tol, base, near


@st.composite
def well_separated_points(draw):
    """``(tol, points)`` where every pair of points is ≥ 3·tol apart
    (placed on distinct grid cells of spacing 3·tol), so the registry
    must keep each as its own canonical entry — none merge.
    """
    tol = draw(st.floats(min_value=0.2, max_value=2.0,
                         allow_nan=False, allow_infinity=False))
    spacing = tol * 3.0
    n = draw(st.integers(min_value=1, max_value=12))
    cells = draw(st.lists(
        st.tuples(st.integers(min_value=-50, max_value=50),
                  st.integers(min_value=-50, max_value=50)),
        min_size=n, max_size=n, unique=True))
    pts = [(cx * spacing, cy * spacing) for (cx, cy) in cells]
    return tol, pts


# A sequence of arbitrary points to feed a registry (for determinism /
# within-tol / registered / size-bound properties).
point_sequence = st.lists(point, min_size=1, max_size=40)


# ── runway_redistribute._interp_profile ─────────────────────────────────

@st.composite
def profile(draw):
    """``(fractions, elevs)`` for ``_interp_profile``.

    ``fractions`` is STRICTLY increasing (built from a start + positive
    gaps ≥ 0.01, so consecutive nodes never collide — keeps node-exact
    interpolation clean) and ``elevs`` is a same-length list of finite
    altitudes.  Models the runway sample grid the function interpolates.
    """
    n = draw(st.integers(min_value=1, max_value=20))
    start = draw(st.floats(min_value=-10.0, max_value=10.0,
                           allow_nan=False, allow_infinity=False))
    gaps = draw(st.lists(
        st.floats(min_value=1e-2, max_value=5.0,
                  allow_nan=False, allow_infinity=False),
        min_size=n - 1, max_size=n - 1))
    fractions = [start]
    for g in gaps:
        fractions.append(fractions[-1] + g)
    elevs = draw(st.lists(
        st.floats(min_value=-500.0, max_value=9000.0,
                  allow_nan=False, allow_infinity=False),
        min_size=n, max_size=n))
    return fractions, elevs


# A query position along the profile axis — spans below the first node,
# between nodes, and past the last node.
profile_t = st.floats(min_value=-20.0, max_value=20.0,
                      allow_nan=False, allow_infinity=False)

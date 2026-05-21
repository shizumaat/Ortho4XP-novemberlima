"""Geometry helpers wrapping shapely operations that emit spurious
numpy floating-point warnings.

``Polygon.minimum_rotated_rectangle`` (GEOS ``oriented_envelope``)
emits ``RuntimeWarning: invalid value encountered in oriented_envelope``
for some perfectly valid polygons — the rotated-rectangle RESULT is
correct (verified: a 264 k m² 148-vertex apron piece yields a clean
810 k m² rectangle), but an internal GEOS/numpy step trips the
invalid/divide floating-point flags.  Wrapping the call in
``numpy.errstate`` silences the cosmetic warning without changing the
result.  Used by every ``minimum_rotated_rectangle`` call site across
the pavement builder so tile generation doesn't spew the warning.
"""
import numpy as np

__all__ = ["min_rotated_rect"]


def min_rotated_rect(geom):
    """Return ``geom.minimum_rotated_rectangle`` without leaking the
    spurious numpy ``oriented_envelope`` floating-point warning."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return geom.minimum_rotated_rectangle

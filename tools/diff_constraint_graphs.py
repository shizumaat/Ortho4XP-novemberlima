#!/usr/bin/env python3
"""Diff the two within-shape constraint-pair generators (M4 measurement).

Set A = ``grade_graph`` model — the pairs the SOLVER enforces and the build's
own WARN (``grade_graph_validate._iter_checked_pairs``) checks.
Set B = ``tools/check_grade.iter_shape_grade_constraints`` — the pairs the grade
TEST checks on the emitted OSM.

They are independent reimplementations that cross-disagree; M4
(docs/cleanup_consolidation_plan.md) narrows B to the model.  This tool measures
the gap so the narrowing can be verified.  Pairs are keyed frame-independently by
their endpoints' lat/lon (A via ``layout.m_to_ll``; B via the OSM node lat/lon),
rounded to 6 dp (~0.1 m), so the two meter frames don't matter.

    venv/bin/python tools/diff_constraint_graphs.py <ICAO>

Reports |A|, |B|, |A∩B|, |A∖B|, |B∖A| overall, per soft role (apron/junction),
and split by distance band.  The M4 acceptance is: the A△B set restricted to
soft-airside pairs ≤60 m is EMPTY.
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

SOFT_ROLES = {"apron", "junction"}


def _key(latlon_a, latlon_b):
    a = (round(latlon_a[0], 6), round(latlon_a[1], 6))
    b = (round(latlon_b[0], 6), round(latlon_b[1], 6))
    return frozenset((a, b))


def _set_a(layout):
    """grade_graph model pairs: {key: (role, dist)} for soft roles."""
    from auto_patch.grade_graph_validate import _iter_checked_pairs
    out = {}
    for (role, _is_spine, (xa, ya), _za, (xb, yb), _zb, _cap) in \
            _iter_checked_pairs(layout):
        if role not in SOFT_ROLES:
            continue
        la = layout.m_to_ll(xa, ya)
        lb = layout.m_to_ll(xb, yb)
        d = math.hypot(xa - xb, ya - yb)
        out[_key(la, lb)] = (role, d)
    return out


def _set_b(layout, osm_path):
    """check_grade pairs: {key: (role, dist)} for soft roles, invoked exactly as
    the grade test does (taxi_axes from the build, seam nids from the OSM)."""
    import check_grade as CG
    from auto_patch.verification import taxi_axes_ll as _taxi_axes_ll
    nodes, ways = CG._parse_osm(Path(osm_path))
    ll_to_m = CG._ll_to_m_factory(nodes)
    seam = CG._seam_nids(nodes)
    axes_ll = _taxi_axes_ll(layout)
    taxi_axes = None
    if axes_ll:
        taxi_axes = []
        for latlon_pts, cL, cT in axes_ll:
            poly = [ll_to_m(lat, lon) for (lat, lon) in latlon_pts]
            if len(poly) >= 2:
                taxi_axes.append((poly, cL, cT))
    out = {}
    for c in CG.iter_shape_grade_constraints(
            ways, nodes, ll_to_m, 0.015, seam, taxi_axes):
        role = c.way.tags.get("role")
        if role not in SOFT_ROLES:
            continue
        la = nodes[c.nid_a]
        lb = nodes[c.nid_b]
        out[_key(la, lb)] = (role, c.dist)
    return out


def _report(A, B):
    a_keys, b_keys = set(A), set(B)
    inter = a_keys & b_keys
    only_a = a_keys - b_keys
    only_b = b_keys - a_keys

    def by_band(keys, src):
        le60 = sum(1 for k in keys if src[k][1] <= 60.0)
        return le60, len(keys) - le60

    print(f"|A| (grade_graph model) = {len(a_keys)}")
    print(f"|B| (check_grade test)  = {len(b_keys)}")
    print(f"|A∩B| = {len(inter)}   |A∖B| = {len(only_a)}   |B∖A| = {len(only_b)}")
    print()
    for label, keys, src in (("A∖B (model has, test lacks)", only_a, A),
                             ("B∖A (test has, model lacks)", only_b, B)):
        le60, gt60 = by_band(keys, src)
        roles = {}
        for k in keys:
            roles[src[k][0]] = roles.get(src[k][0], 0) + 1
        print(f"{label}: {len(keys)}  (≤60m={le60}, >60m={gt60})  by-role={roles}")
    print()
    sym_le60 = ([k for k in only_a if A[k][1] <= 60.0]
                + [k for k in only_b if B[k][1] <= 60.0])
    print(f"*** ACCEPTANCE: |A△B ∩ (soft-airside ≤60m)| = {len(sym_le60)} "
          f"(target 0) ***")
    return len(sym_le60)


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    icao = sys.argv[1]
    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement
    layout = build_airport_pavement(icao, xplane_root(), compute_elevations=True)
    with tempfile.NamedTemporaryFile(suffix=".osm", delete=False) as tf:
        osm_path = tf.name
    layout.to_osm(osm_path)
    A = _set_a(layout)
    B = _set_b(layout, osm_path)
    print(f"=== constraint-graph diff: {icao} ===")
    _report(A, B)


if __name__ == "__main__":
    main()

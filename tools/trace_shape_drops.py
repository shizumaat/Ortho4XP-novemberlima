"""Trace WHICH pipeline pass removes or shrinks pavement shapes.

When a junction / stub / apron goes missing from the final patch, this
tool pinpoints the culprit.  It wraps the pipeline's shape-removing and
shape-rewriting passes; for each, it measures the LOST COVERAGE — pavement
(of the tracked roles) that was present BEFORE the pass and is absent
AFTER — and reports each lost region's area and location.

Lost coverage is computed geometrically (``before_union.difference(
after_union)``), so it is robust to whether a pass deletes a shape, shrinks
it in place, or rebuilds it as a new object: only genuinely-vanished area
is reported.  A pass that merely reshapes without losing area shows
nothing.  An amputation (e.g. ``_enforce_runway_1to1_sharing`` chording
across a connector) shows as a lost wedge with its true size + location.

Usage:
    venv/bin/python tools/trace_shape_drops.py HECA
    venv/bin/python tools/trace_shape_drops.py CYXY --roles junction,stub,cross_connector
    venv/bin/python tools/trace_shape_drops.py SPLP --min-area 200

The list of instrumented passes lives in ``SUSPECT_PASSES`` below — add to
it as new shape-dropping passes are written.  Each entry is
``(module_import_path, function_name)`` and the function must take the
``layout`` as its first positional argument.
"""
from __future__ import annotations

import argparse
import importlib

from shapely.ops import unary_union

import _diag

# (module path, function name) — passes that remove or reshape shapes.
# The function must take ``layout`` as its first positional arg.
SUSPECT_PASSES = [
    ("auto_patch.junction_rules", "_enforce_runway_1to1_sharing"),
    ("auto_patch.junction_repair", "_absorb_rects_at_junction_perimeters"),
    ("auto_patch.junction_repair", "_merge_sliver_junctions_into_neighbours"),
    ("auto_patch.junction_repair", "_drop_thin_orphan_slivers"),
    ("auto_patch.junction_repair", "_drop_floating_orphan_junctions"),
    ("auto_patch.groundside", "_reclassify_groundside_orphan_junctions"),
]


def _coverage(layout, roles):
    """Union of all tracked-role shape polygons (in layout metres)."""
    polys = []
    for s in layout.shapes:
        role = getattr(s, "role", "")
        if roles and not any(t in role for t in roles):
            continue
        p = getattr(s, "polygon", None)
        if p is None or p.is_empty:
            continue
        polys.append(p if p.is_valid else p.buffer(0))
    return unary_union(polys) if polys else None


def _lost_pieces(before, after, min_area):
    """List of (area, centroid) for regions in ``before`` not in ``after``,
    above ``min_area`` m² (hairline numeric seams trimmed)."""
    if before is None or before.is_empty:
        return []
    lost = before if after is None else before.difference(after)
    if lost.is_empty:
        return []
    lost = lost.buffer(-0.3).buffer(0.3)  # drop numeric edge slivers
    out = []
    for poly in _diag.iter_polys(lost):
        if poly.area >= min_area:
            out.append((poly.area, poly.centroid))
    out.sort(key=lambda t: -t[0])
    return out


def _install(mod_path, func_name, roles, min_area):
    def make_wrapper(orig):
        def wrapped(layout, *a, **k):
            before = _coverage(layout, roles)
            res = orig(layout, *a, **k)
            after = _coverage(layout, roles)
            pieces = _lost_pieces(before, after, min_area)
            if pieces:
                total = sum(a for a, _ in pieces)
                print(f"\n>>> {func_name}: lost {total:,.0f} m² "
                      f"of tracked coverage in {len(pieces)} region(s)")
                for area, c in pieces:
                    lat, lon = layout.m_to_ll(c.x, c.y)
                    print(f"    LOST {area:9,.0f} m²  @ {lat:.5f},{lon:.5f}")
            return res
        return wrapped
    # Patch the defining module AND call-site modules (finalize/pipeline
    # import some passes by value), so finalize-stage passes are visible.
    return _diag.patch_pass(mod_path, func_name, make_wrapper)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    _diag.add_common_args(ap)
    ap.add_argument("--roles", default="junction,stub",
                    help="comma-separated role substrings to track "
                         "(default: junction,stub)")
    ap.add_argument("--min-area", type=float, default=50.0,
                    help="min lost-region area (m²) to report (default: 50)")
    args = ap.parse_args(argv)
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]

    # Importing the suspect modules and installing wrappers must happen
    # AFTER pipeline import (circular-import order); _diag.build does the
    # pipeline import, but we need wrappers in place BEFORE the build runs,
    # so import pipeline explicitly first.
    importlib.import_module("auto_patch.pipeline")
    restores = [_install(mod_path, func_name, roles, args.min_area)
                for mod_path, func_name in SUSPECT_PASSES]

    print(f"Building {args.icao} with drop tracing "
          f"(roles={roles})...", flush=True)
    try:
        layout = _diag.build(args.icao, args.xplane)
    finally:
        for r in restores:
            r()
    print("\n=== FINAL counts ===")
    _diag.print_role_counts(layout, "final shapes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

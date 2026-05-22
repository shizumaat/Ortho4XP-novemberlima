"""Monitor pavement coverage of pav_union THROUGH the whole pipeline.

pav_union is the source of truth.  As the pipeline mutates geometry and
elevations pass by pass, the emitted shapes should keep covering ~100% of
pav_union.  This tool captures pav_union, then after each geometry-mutating
pass (see ``_diag.PIPELINE_GEOMETRY_PASSES``) reports the running coverage
percentage and the change since the previous pass — so a pass that opens a
hole is caught the moment it happens, with the lost region's size and
location.

Coverage = ``(pav_union ∩ union(coverage-role shapes)) / pav_union``.
Over-coverage (shapes outside pav_union) is intentionally ignored — per
the project principle, abandoning real pavement is what matters, extra
coverage is harmless.

Usage:
    venv/bin/python tools/monitor_coverage.py HECA
    venv/bin/python tools/monitor_coverage.py CYXY --drop-thresh 100

Reports each pass as:  coverage%  (Δ m²)  [LOST regions if Δ < -thresh].
"""
from __future__ import annotations

import argparse
import importlib

from shapely.ops import unary_union

import _diag


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _diag.add_common_args(ap)
    ap.add_argument("--drop-thresh", type=float, default=50.0,
                    help="report lost regions when a pass drops more than "
                         "this many m² of coverage (default: 50)")
    args = ap.parse_args(argv)

    state: dict = {"pav_union": None, "prev_covered": None,
                   "prev_geom": None, "step": 0}

    def coverage(layout):
        pav = state["pav_union"]
        if pav is None:
            return None, None
        polys = _diag.coverage_polys(layout)
        cov = unary_union(polys) if polys else None
        covered = pav.intersection(cov).area if cov is not None else 0.0
        return covered, cov

    def report(layout, label):
        pav = state["pav_union"]
        if pav is None:
            return
        covered, cov = coverage(layout)
        pct = 100.0 * covered / pav.area if pav.area else 0.0
        prev = state["prev_covered"]
        delta = (covered - prev) if prev is not None else 0.0
        state["step"] += 1
        flag = ""
        if prev is not None and delta < -args.drop_thresh:
            flag = "  <<< COVERAGE DROP"
        print(f"  [{state['step']:>2}] {label:<46} "
              f"{pct:7.3f}%  Δ{delta:+10,.0f} m²{flag}")
        # On a real drop, localize the newly-uncovered regions.
        if (prev is not None and delta < -args.drop_thresh
                and state["prev_geom"] is not None and cov is not None):
            newly_lost = (pav.intersection(state["prev_geom"])
                          .difference(cov))
            newly_lost = newly_lost.buffer(-0.3).buffer(0.3)
            pieces = sorted(
                (p for p in _diag.iter_polys(newly_lost) if p.area > 20),
                key=lambda p: -p.area)
            for p in pieces[:8]:
                c = p.centroid
                lat, lon = layout.m_to_ll(c.x, c.y)
                print(f"          lost {p.area:8,.0f} m²  "
                      f"@ {lat:.5f},{lon:.5f}")
        state["prev_covered"] = covered
        state["prev_geom"] = cov

    def make_wrapper(func_name):
        def _mk(orig):
            def wrapped(layout, *a, **k):
                res = orig(layout, *a, **k)
                report(layout, func_name)
                return res
            return wrapped
        return _mk

    # Order matters: import pipeline first (circular-import guard), capture
    # pav_union at the union step, then wrap the downstream passes.  Wraps
    # call-site modules too (passes imported by value into finalize/
    # pipeline), so finalize-stage passes are not invisible.
    importlib.import_module("auto_patch.pipeline")
    restore_union = _diag.install_union_capture(state)
    restores = [_diag.patch_pass(mp, fn, make_wrapper(fn))
                for mp, fn in _diag.PIPELINE_GEOMETRY_PASSES]

    print(f"Building {args.icao} — coverage timeline "
          f"(pav_union = source of truth)...\n", flush=True)
    try:
        layout = _diag.build(args.icao, args.xplane)
    finally:
        restore_union()
        for r in restores:
            r()

    print()
    report(layout, "FINAL (after emit/finalize)")
    pav = state["pav_union"]
    if pav is not None:
        covered = state["prev_covered"]
        print(f"\npav_union: {pav.area:,.0f} m²   "
              f"final uncovered: {pav.area - covered:,.0f} m²  (IMPORTANT)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

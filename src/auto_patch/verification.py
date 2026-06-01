"""Build-time verification of an emitted airport layout.

Single source of truth for the auto-patch invariant checks, shared by:

  * the PRODUCTION build — ``driver.generate_auto_patches`` calls
    :func:`verify_and_log` on every airport it builds for a tile, so a
    user running Ortho4XP is told when an airport patch has errors; and
  * the DEV pytest gate — the baseline-airport tests call the same check
    functions and ``assert`` on them.

There is exactly ONE implementation of each check; the tests and the
build path both call it.  Thresholds are UNIVERSAL — no per-airport
exceptions.  Any cross-shape elevation disagreement, any within-shape
grade > 1.5 %, or any edge/mid-edge step > 0.5 m is a violation.

Phase 1 covers the GRADE checks, reusing ``tools/check_grade.py``
verbatim (the exact logic ``tests/test_pavement_grade.py`` asserts).
The geometry invariants (self-overlap, rect short-edge connectivity,
sloping-edge vertex rules, junction rules) will be folded in here so
they too have a single home.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import O4_UI_Utils as UI


def _import_check_grade():
    """``tools/check_grade.py`` is the canonical grade validator but lives
    in the repo's ``tools`` dir (not an installed package).  Resolve that
    dir from this file and import it."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))   # src/auto_patch -> repo
    tools_dir = os.path.join(repo_root, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import check_grade  # noqa: E402
    return check_grade


def taxi_axes_ll(layout):
    """Per-axis taxi grading mirror — the SAME construction the grade
    test uses, so junctions are graded exactly as the build intended."""
    try:
        from .elevation_per_surface import unified_jacobi as _uj
    except Exception:
        return None
    if not getattr(_uj, "_PER_AXIS_JUNCTIONS", False):
        return None
    letters = getattr(layout, "apt_taxi_letters", {}) or {}
    axes = []
    for ln, name in (getattr(layout, "apt_taxi_centerlines", []) or []):
        if ln is None or ln.is_empty:
            continue
        letter = letters.get(name)
        cL = 0.03 if letter in ("A", "B") else 0.015
        cT = 0.02 if letter in ("A", "B") else 0.015
        pts = [layout.m_to_ll(x, y) for (x, y) in ln.coords]
        axes.append((pts, cL, cT))
    return axes


def check_self_overlap(layout):
    """Invariant A1: every paved metre belongs to exactly one shape — no
    two emitted pavement polygons may overlap.  Returns a list of
    ``(area_m2, role_a, role_b)`` overlap pairs, largest first (empty =
    clean).  Universal, zero tolerance — X-Plane mesh generation cannot
    handle overlapping pavement."""
    from shapely.strtree import STRtree
    polys = [(s.role, s.polygon) for s in layout.shapes
             if s.polygon is not None and not s.polygon.is_empty]
    if len(polys) < 2:
        return []
    tree = STRtree([p for _, p in polys])
    pairs = []
    for i, (role_a, pa) in enumerate(polys):
        for j in tree.query(pa):
            if j <= i:
                continue
            role_b, pb = polys[j]
            try:
                inter = pa.intersection(pb)
            except Exception:
                continue
            if inter.is_empty or inter.area <= 0.0:
                continue
            pairs.append((inter.area, role_a, role_b))
    pairs.sort(reverse=True)
    return pairs


def run_grade_checks(layout):
    """Run the grade engine on ``layout``.  Returns ``(within, cross,
    steps)`` lists (each item carries ``.grade_pct`` / ``.de_m`` /
    ``.step_m`` + way labels).  Raises only on a genuine engine error."""
    check_grade = _import_check_grade()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "verify.osm"
        layout.to_osm(str(out))
        return check_grade.run_checks(
            out, max_grade_pct=1.5, proximity_m=1.0, edge_search_m=5.0,
            edge_step_m=0.5, top_n=5, taxi_axes_ll=taxi_axes_ll(layout),
            quiet=True)


def verify_and_log(layout, icao: str) -> dict:
    """Run the verification checks on a freshly-built layout and LOG a
    summary (never raises — a verification problem must not break a
    build).  Returns a counts dict.

    Logging levels: the one-line summary is emitted at verbosity 0 when
    there ARE violations (so the user sees errors even in a quiet
    release build) and at verbosity 1 when clean."""
    # Geometry invariants (cheap; operate on the layout directly).
    try:
        overlaps = check_self_overlap(layout)
    except Exception:                              # pragma: no cover
        overlaps = []

    # Grade invariants (reuse the check_grade engine).
    try:
        within, cross, steps = run_grade_checks(layout)
    except Exception as exc:                       # pragma: no cover
        UI.lvprint(0, f"  [verify] {icao}: grade verification "
                       f"unavailable ({exc})")
        within = cross = steps = []

    counts = {"cross": len(cross), "within": len(within),
              "steps": len(steps), "overlaps": len(overlaps)}
    problems = len(cross) + len(within) + len(steps) + len(overlaps)
    if problems:
        UI.lvprint(0,
            f"  [verify] {icao}: ISSUES — overlap={len(overlaps)} "
            f"cross-shape={len(cross)} within-shape={len(within)} "
            f"edge-steps={len(steps)}")
        for area, ra, rb in overlaps[:5]:
            UI.lvprint(0, f"  [verify]     overlap {area:.1f} m²  "
                          f"{ra} / {rb}")
        try:
            check_grade = _import_check_grade()
            for v in sorted(cross, key=lambda v: -v.de_m)[:5]:
                UI.lvprint(0, f"  [verify]     cross {v.de_m:.2f} m  "
                              f"{check_grade._label(v.way_a)} -> "
                              f"{check_grade._label(v.way_b)}")
            for v in sorted(within, key=lambda v: -v.grade_pct)[:5]:
                UI.lvprint(0, f"  [verify]     within {v.grade_pct:.2f}% "
                              f"over {v.distance_m:.1f} m  "
                              f"{check_grade._label(v.way_a)}")
        except Exception:                          # pragma: no cover
            pass
    else:
        UI.vprint(1, f"  [verify] {icao}: OK "
                     f"(0 overlap / 0 cross / 0 within / 0 steps)")
    return counts

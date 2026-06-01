"""Build-time verification of an emitted airport layout.

Single source of truth for the auto-patch invariant checks, shared by:

  * the PRODUCTION build — ``driver.generate_auto_patches`` calls
    :func:`verify_and_log` on every airport it builds for a tile, so a
    user running Ortho4XP is told when an airport patch has errors; and
  * the DEV pytest gate — the baseline-airport tests call the same check
    functions and ``assert`` on them.

There is exactly ONE implementation of each check.  Thresholds are
UNIVERSAL — no per-airport exceptions.

Diagnostics: the user's only fix lever is the source data (apt.dat /
DSF), so every reported violation says WHAT, WHERE — the ``shapeID`` to
open in the patch, a lat/lon, and (for junctions/aprons) the taxiways
that meet there, e.g. "junction [#375] where taxiways A, M meet" — a
likely CAUSE, and a suggested FIX.  Once the elevation solver is
complete a grade violation almost always means a geometry / source
problem, so the hints lean that way.

``shapeID`` == the shape's index in ``layout.shapes`` (the same value
``layout.to_osm`` writes as the ``shapeID`` tag), so a reported id maps
directly to the way in the emitted patch.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import O4_UI_Utils as UI

_TAXI_ROLES = ("primary_parallel", "secondary_parallel",
               "stub", "cross_connector")

# Roles that are NOT airside pavement built from apt.dat row-110 / DSF —
# excluded from the source-adjacency check.
_NON_SOURCE_PAVEMENT_ROLES = frozenset({
    "boundary", "taxiway_clearance", "runway_clearance",
    "retaining_wall", "tunnel_ramp", "groundside_pavement",
    "service_road", "service_junction", "terminal",
})

_HINTS = {
    "overlap": ("two pavement shapes share footprint — likely a duplicate "
                "DSF .pol overlay over apt.dat row-110, or two row-110 "
                "polygons covering the same area. Fix: remove the redundant "
                "overlay / merge the duplicate source pavement."),
    "source":  ("emitted pavement with NO apt.dat/DSF source beneath it — a "
                "spurious synthesis or a non-pavement polygon (grass/decor) "
                "tagged as pavement. Fix: correct or remove that source "
                "polygon."),
    "within":  ("the surface cannot stay within its grade cap — terrain "
                "(DEM) too steep across it, or incompatible anchor "
                "elevations. Fix: check the apt.dat geometry isn't spanning "
                "a real slope and the CIFP runway-threshold elevations are "
                "right (with a complete elevation solver, a residual here is "
                "a geometry/source problem)."),
    "cross":   ("adjacent surfaces meet at incompatible elevations (e.g. "
                "flat areas at different levels sharing a corner). Fix: check "
                "the apt.dat pavement layout where these shapes meet."),
    "steps":   ("a surface meets a neighbour at a vertical step — usually "
                "resolves once the grade issues above are fixed."),
}


def _ll(layout, x, y) -> str:
    """Format a layout-meter point as a ``lat,lon`` string."""
    try:
        lat, lon = layout.m_to_ll(x, y)
        return f"{lat:.5f},{lon:.5f}"
    except Exception:
        return "?,?"


def build_taxi_index(layout):
    """STRtree of taxi-rect polygons + parallel ref list, for naming the
    taxiways adjacent to a junction/apron.  Returns ``(tree, geoms,
    refs)`` or ``(None, [], [])``."""
    from shapely.strtree import STRtree
    geoms, refs = [], []
    for s in layout.shapes:
        if (s.role in _TAXI_ROLES and s.polygon is not None
                and not s.polygon.is_empty and (s.ref or "").strip()):
            geoms.append(s.polygon)
            refs.append((s.ref or "").strip())
    if not geoms:
        return (None, [], [])
    return (STRtree(geoms), geoms, refs)


def _neighbour_taxi_refs(poly, taxi_index, tol_m: float = 1.0):
    """Distinct taxiway refs whose rect touches ``poly`` (within
    ``tol_m``).  Sub-refs are collapsed to their base letter so
    "A, A3, M" reads "A, M"."""
    tree, geoms, refs = taxi_index
    if tree is None or poly is None or poly.is_empty:
        return []
    out = set()
    try:
        cand = tree.query(poly)
    except Exception:
        return []
    for j in cand:
        try:
            if poly.distance(geoms[j]) <= tol_m:
                r = refs[j]
                base = r[0] if r and r[0].isalpha() else r
                out.add(base)
        except Exception:
            continue
    return sorted(out)


def describe_shape(layout, idx, taxi_index=None) -> str:
    """Human description of ``layout.shapes[idx]``: role, ref, the
    ``shapeID`` to open in the patch, and — for a junction/apron — the
    taxiways that meet there."""
    try:
        s = layout.shapes[idx]
    except (IndexError, TypeError):
        return f"shape [#{idx}]"
    role = s.role or "?"
    ref = (s.ref or "").strip()
    head = f"{role} {ref}".strip() if ref else role
    out = f"{head} [#{idx}]"
    if role in ("junction", "apron") and taxi_index is not None:
        nb = _neighbour_taxi_refs(s.polygon, taxi_index)
        if len(nb) >= 2:
            out += f" where taxiways {', '.join(nb[:4])} meet"
        elif len(nb) == 1:
            out += f" off taxiway {nb[0]}"
    return out


def _import_check_grade():
    """``tools/check_grade.py`` is the canonical grade validator but lives
    in the repo's ``tools`` dir (not an installed package)."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    tools_dir = os.path.join(repo_root, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import check_grade  # noqa: E402
    return check_grade


# ── Geometry invariants ─────────────────────────────────────────────
def check_self_overlap(layout):
    """Invariant A1: no two emitted pavement polygons may overlap.
    Returns ``[(area_m2, idx_a, idx_b, "lat,lon"), …]`` largest first."""
    from shapely.strtree import STRtree
    polys = [(i, s.polygon) for i, s in enumerate(layout.shapes)
             if s.polygon is not None and not s.polygon.is_empty]
    if len(polys) < 2:
        return []
    tree = STRtree([p for _, p in polys])
    pairs = []
    for k, (idx_a, pa) in enumerate(polys):
        for q in tree.query(pa):
            if q <= k:
                continue
            idx_b, pb = polys[q]
            try:
                inter = pa.intersection(pb)
            except Exception:
                continue
            if inter.is_empty or inter.area <= 0.0:
                continue
            c = inter.representative_point()
            pairs.append((inter.area, idx_a, idx_b, _ll(layout, c.x, c.y)))
    pairs.sort(key=lambda r: r[0], reverse=True)
    return pairs


def check_source_adjacency(layout, min_on_source_frac: float = 0.5):
    """Invariant: every emitted PAVEMENT shape must rest on real source
    pavement (apt.dat row-110 ∪ DSF ∪ runway) by ≥ ``min_on_source_frac``
    of its own area.  Source-relative, per-shape, no per-airport ratio.
    Returns ``[(idx, area_m2, on_frac, "lat,lon"), …]`` largest first;
    ``[]`` when no source union was recorded."""
    src = getattr(layout, "source_pavement_union", None)
    if src is None or src.is_empty:
        return []
    rwy = getattr(layout, "runway_union", None)
    if rwy is not None and not rwy.is_empty:
        try:
            src = src.union(rwy)
        except Exception:
            pass
    out = []
    for i, s in enumerate(layout.shapes):
        if s.polygon is None or s.polygon.is_empty:
            continue
        if (s.role or "") in _NON_SOURCE_PAVEMENT_ROLES:
            continue
        area = s.polygon.area
        if area <= 1.0:
            continue
        try:
            on = s.polygon.intersection(src).area
        except Exception:
            continue
        frac = on / area if area > 0 else 1.0
        if frac < min_on_source_frac:
            c = s.polygon.representative_point()
            out.append((i, area, frac, _ll(layout, c.x, c.y)))
    out.sort(key=lambda r: r[1], reverse=True)
    return out


# ── Grade invariants (reuse the check_grade engine) ─────────────────
def taxi_axes_ll(layout):
    """Per-axis taxi grading mirror — the SAME construction the grade
    test uses."""
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


def run_grade_checks(layout):
    """Run the grade engine on ``layout``.  Returns ``(within, cross,
    steps)`` with ``.lat`` / ``.lon`` + way labels populated."""
    check_grade = _import_check_grade()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "verify.osm"
        layout.to_osm(str(out))
        return check_grade.run_checks(
            out, max_grade_pct=1.5, proximity_m=1.0, edge_search_m=5.0,
            edge_step_m=0.5, top_n=5, taxi_axes_ll=taxi_axes_ll(layout),
            quiet=True)


# ── Build-time entry point ──────────────────────────────────────────
def verify_and_log(layout, icao: str) -> dict:
    """Run every verification check on a freshly-built layout and LOG a
    diagnostic summary (never raises).  Returns a counts dict.  Problems
    log at verbosity 0; a clean airport at verbosity 1."""
    overlaps = source = within = cross = steps = []
    try:
        overlaps = check_self_overlap(layout)
    except Exception:                              # pragma: no cover
        pass
    try:
        source = check_source_adjacency(layout)
    except Exception:                              # pragma: no cover
        pass
    try:
        within, cross, steps = run_grade_checks(layout)
    except Exception as exc:                       # pragma: no cover
        UI.lvprint(0, f"  [verify] {icao}: grade verification "
                       f"unavailable ({exc})")
        within = cross = steps = []

    counts = {"overlap": len(overlaps), "source": len(source),
              "cross": len(cross), "within": len(within),
              "steps": len(steps)}
    if not sum(counts.values()):
        UI.vprint(1, f"  [verify] {icao}: OK — no overlap / source / "
                     f"grade issues.")
        return counts

    taxi_index = build_taxi_index(layout)
    try:
        glabel_id = _import_check_grade()
    except Exception:                              # pragma: no cover
        glabel_id = None

    def _gdesc(way):
        """Describe a grade-violation way: prefer the layout shapeID (gives
        adjacency); fall back to check_grade's label."""
        sid = way.tags.get("shapeID") if getattr(way, "tags", None) else None
        if sid is not None:
            try:
                return describe_shape(layout, int(sid), taxi_index)
            except Exception:
                pass
        return glabel_id._label(way) if glabel_id else "?"

    UI.lvprint(0,
        f"  [verify] {icao}: PATCH ISSUES — overlap={counts['overlap']} "
        f"off-source={counts['source']} cross-shape={counts['cross']} "
        f"within-shape={counts['within']} edge-steps={counts['steps']}. "
        f"Likely apt.dat / DSF source problems — details below.")

    if overlaps:
        for area, ia, ib, loc in overlaps[:5]:
            UI.lvprint(0, f"  [verify]   OVERLAP {area:.1f} m² @ {loc}: "
                          f"{describe_shape(layout, ia, taxi_index)} ∩ "
                          f"{describe_shape(layout, ib, taxi_index)}")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['overlap']}")
    if source:
        for idx, area, frac, loc in source[:5]:
            UI.lvprint(0, f"  [verify]   OFF-SOURCE {area:.0f} m² "
                          f"({frac*100:.0f}% on source) @ {loc}: "
                          f"{describe_shape(layout, idx, taxi_index)}")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['source']}")
    if cross:
        for v in sorted(cross, key=lambda v: -v.de_m)[:5]:
            loc = f"{v.lat:.5f},{v.lon:.5f}" if v.lat is not None else "?,?"
            UI.lvprint(0, f"  [verify]   CROSS-SHAPE {v.de_m:.2f} m @ {loc}: "
                          f"{_gdesc(v.way_a)} ↔ {_gdesc(v.way_b)}")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['cross']}")
    if within:
        for v in sorted(within, key=lambda v: -v.grade_pct)[:5]:
            loc = f"{v.lat:.5f},{v.lon:.5f}" if v.lat is not None else "?,?"
            UI.lvprint(0, f"  [verify]   WITHIN-SHAPE {v.grade_pct:.1f}% over "
                          f"{v.distance_m:.1f} m @ {loc}: {_gdesc(v.way_a)}")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['within']}")
    if steps:
        UI.lvprint(0, f"  [verify]   EDGE-STEPS: {len(steps)} vertical "
                      f"step(s) > 0.5 m between adjacent surfaces.")
        UI.lvprint(0, f"  [verify]     ↳ {_HINTS['steps']}")
    return counts

"""Shared helpers for auto-patch diagnostic tools.

Centralizes the boilerplate every diagnostic script needs so individual
tools stay short and consistent:

  * ``sys.path`` setup (adds ``src/`` and repo root)
  * an X-Plane root default + ``--xplane`` arg helper
  * building an airport layout (``build`` / ``build_capturing_union``)
  * dumping a shapely meter-space geometry to JOSM-readable OSM
  * shape signatures + role tallies for before/after comparisons

Coordinate conventions (see ``layout.py``): layout shape polygons are in
LOCAL METRES anchored at ``layout.anchor``; ``layout.m_to_ll(x, y)``
returns geographic ``(lat, lon)``.  All OSM dumps here go through
``m_to_ll`` so they overlay correctly in JOSM.

Import note: always import ``auto_patch.pipeline`` BEFORE
``auto_patch.junction_repair`` (junction_repair <-> elevation have a
circular import that only resolves via the normal pipeline import order).
``build``/``build_capturing_union`` import pipeline first, so callers that
go through them are safe.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_XPLANE = "/Users/noah/X-Plane 12"

for _p in (os.path.join(REPO, "src"), REPO, os.path.join(REPO, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def add_common_args(ap: argparse.ArgumentParser) -> None:
    """Add the standard ``icao`` / ``--xplane`` / ``--out`` arguments."""
    ap.add_argument("icao", help="ICAO of the airport to build")
    ap.add_argument("--xplane", default=DEFAULT_XPLANE,
                    help=f"X-Plane root (default: {DEFAULT_XPLANE})")
    ap.add_argument("--out", default=None,
                    help="output OSM path (default: /tmp/<ICAO>_<tool>.osm)")


def build(icao: str, xplane: str = DEFAULT_XPLANE, **kw):
    """Build and return a pavement layout (full pipeline by default)."""
    from auto_patch.pipeline import build_airport_pavement
    return build_airport_pavement(icao, xplane, **kw)


def build_capturing_union(icao: str, xplane: str = DEFAULT_XPLANE):
    """Build an airport and capture the pipeline's real ``pav_union``.

    Returns ``(layout, pav_union)`` where ``pav_union`` is the merged +
    seam-cleaned + simplified pavement coverage the pipeline uses for
    rect/junction construction — captured at the union step (after
    ``_close_open_clean`` and the matching ``simplify(tol=2.0)``), BEFORE
    runway / groundside subtraction.  This is the geometry a reviewer
    looks at to judge "is the union clean".

    Implemented by spying on ``union_helpers._close_open_clean`` (pipeline
    imports it locally at call time, so patching the module attribute is
    picked up).  If the pipeline's union step is ever renamed, update the
    spy target here in ONE place.
    """
    from auto_patch import pipeline  # noqa: F401 (ordering: import first)
    from auto_patch.pavement import union_helpers

    cap: dict = {}
    orig = union_helpers._close_open_clean

    def _spy(geom, *a, **k):
        result = orig(geom, *a, **k)
        # Reproduce the pipeline's next step so the captured geometry
        # matches the union actually used downstream.
        cap["union"] = union_helpers._simplify_pavement_polygon(
            result, tol=2.0)
        return result

    union_helpers._close_open_clean = _spy
    try:
        layout = build(icao, xplane)
    finally:
        union_helpers._close_open_clean = orig
    return layout, cap.get("union")


# Ordered geometry-mutating passes between pav_union construction and the
# final emit.  (module path, function name).  Pavement coverage of
# pav_union should hold ~100% across all of these; a drop pinpoints the
# pass that erased pavement.  Some passes run more than once in the
# pipeline — instrumentation fires on each call, so the timeline shows
# every invocation in order.  Add new geometry passes here as they appear.
PIPELINE_GEOMETRY_PASSES = [
    ("auto_patch.junction_rules", "_align_rect_slope_to_axis"),
    ("auto_patch.junction_rules", "_snap_to_sloping_edge_corners"),
    ("auto_patch.junction_rules",
     "_snap_junction_vertices_to_rect_flat_edge_corners"),
    ("auto_patch.junction_rules", "_enforce_runway_1to1_sharing"),
    ("auto_patch.junction_rules", "widen_junctions_to_runway_corners"),
    ("auto_patch.junction_rules", "stitch_pavement_to_flat_runways"),
    ("auto_patch.seam_anchors", "split_pavement_at_seams"),
    ("auto_patch.junction_rules", "stitch_pavement_to_terminals"),
    ("auto_patch.junction_rules", "stitch_pavement_polygons"),
    ("auto_patch.junction_repair", "_absorb_rects_at_junction_perimeters"),
    ("auto_patch.junction_repair", "_merge_sliver_junctions_into_neighbours"),
    ("auto_patch.junction_repair", "_drop_thin_orphan_slivers"),
    ("auto_patch.junction_repair", "_drop_floating_orphan_junctions"),
    ("auto_patch.groundside", "_reclassify_groundside_orphan_junctions"),
]

# Modules that re-import pipeline passes BY VALUE (``from .x import f``).
# A pass wrapped only on its defining module is invisible to these
# callers, so instrumentation must patch their references too (this is
# why the coverage monitor originally missed the finalize-stage groundside
# pass).  ``patch_pass`` below patches every module that has the name.
_CALLSITE_MODULES = ("auto_patch.finalize", "auto_patch.pipeline")


def patch_pass(mod_path: str, func_name: str, make_wrapper):
    """Wrap ``mod_path.func_name`` AND every call-site module that imported
    it by value.  ``make_wrapper(orig)`` returns the wrapped callable.
    Returns a ``restore()`` callable that undoes all patches.  If the
    function isn't found anywhere, returns a no-op restore.
    """
    import importlib
    targets = []
    try:
        defining = importlib.import_module(mod_path)
    except ImportError:
        defining = None
    orig = getattr(defining, func_name, None) if defining else None
    if orig is None:
        # Fall back to a call-site module's copy as the canonical original.
        for m in _CALLSITE_MODULES:
            try:
                cand = getattr(importlib.import_module(m), func_name, None)
            except ImportError:
                cand = None
            if cand is not None:
                orig = cand
                break
    if orig is None:
        return lambda: None
    wrapper = make_wrapper(orig)
    for m in ((mod_path,) + _CALLSITE_MODULES):
        try:
            mod = importlib.import_module(m)
        except ImportError:
            continue
        if getattr(mod, func_name, None) is not None:
            setattr(mod, func_name, wrapper)
            targets.append(mod)

    def restore():
        for mod in targets:
            setattr(mod, func_name, orig)
    return restore


def is_coverage_role(role: str) -> bool:
    """True if a shape role contributes PAVEMENT coverage (i.e. should sit
    inside pav_union).  Boundary ribbon / DEM bridges / retaining walls /
    tunnel ramps legitimately live OUTSIDE pav_union and are excluded — so
    coverage unions stay cheap and measure only real pavement."""
    r = (role or "").lower()
    if r in ("boundary", "retaining_wall"):
        return False
    if "bridge" in r or "tunnel" in r:
        return False
    return True


def coverage_polys(layout):
    """Valid polygons of all coverage-role shapes (for a coverage union)."""
    out = []
    for s in layout.shapes:
        if not is_coverage_role(getattr(s, "role", "")):
            continue
        p = getattr(s, "polygon", None)
        if p is None or p.is_empty:
            continue
        out.append(p if p.is_valid else p.buffer(0))
    return out


def install_union_capture(state: dict):
    """Patch the pipeline's union step to stash the real ``pav_union`` into
    ``state['pav_union']`` as the build runs.  Returns a restore callable.

    Lets a tool capture pav_union AND instrument later passes in the SAME
    build (``build_capturing_union`` can't, since it owns the build).  Same
    capture point as ``build_capturing_union`` — keep them in sync.
    """
    from auto_patch import pipeline  # noqa: F401 (import order)
    from auto_patch.pavement import union_helpers
    orig = union_helpers._close_open_clean

    def _spy(geom, *a, **k):
        result = orig(geom, *a, **k)
        state["pav_union"] = union_helpers._simplify_pavement_polygon(
            result, tol=2.0)
        return result

    union_helpers._close_open_clean = _spy

    def restore():
        union_helpers._close_open_clean = orig
    return restore


def iter_polys(geom):
    """Yield component Polygons of a Polygon / MultiPolygon (else none)."""
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type == "MultiPolygon":
        yield from geom.geoms


def geom_to_osm(geom, m_to_ll, path: str, tag_k: str = "diag",
                tag_v: str = "yes") -> tuple[int, int]:
    """Write a shapely meter-space (Multi)Polygon to a JOSM OSM file.

    Each exterior and interior ring becomes a closed ``<way>``.  Returns
    ``(n_polys, n_rings)``.  ``m_to_ll`` is ``layout.m_to_ll``.
    """
    polys = list(iter_polys(geom))
    nid = [-1]
    wid = [-1000000]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<osm version="0.6" generator="auto_patch.diag">']

    def emit_ring(coords, hole=False):
        ids = []
        for x, y in coords:
            lat, lon = m_to_ll(x, y)
            lines.append(f'  <node id="{nid[0]}" lat="{lat:.9f}" '
                         f'lon="{lon:.9f}" visible="true"/>')
            ids.append(nid[0])
            nid[0] -= 1
        lines.append(f'  <way id="{wid[0]}" visible="true">')
        for n in ids:
            lines.append(f'    <nd ref="{n}"/>')
        lines.append(f'    <tag k="{tag_k}" v="{tag_v}"/>')
        if hole:
            # An interior ring is a HOLE — without this tag it renders in
            # JOSM exactly like a pavement outline and reads as coverage
            # (an authored apt.dat hole was mistaken for union pavement).
            lines.append('    <tag k="hole" v="yes"/>')
        lines.append('  </way>')
        wid[0] -= 1

    nrings = 0
    for poly in polys:
        emit_ring(list(poly.exterior.coords))
        nrings += 1
        for interior in poly.interiors:
            emit_ring(list(interior.coords), hole=True)
            nrings += 1
    lines.append('</osm>')
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return len(polys), nrings


def seam_swath(layout, pav_union, half_width_m: float = 5.0):
    """Geometry of the tile-seam swath (±``half_width_m`` around each
    integer-degree lon/lat line crossing the airport) intersected with
    ``pav_union`` — the pavement that ``tile_cut`` legitimately removes as
    the ~10 m gap along the tile boundary.

    Empty (area 0) for airports with no seam crossing (the common case).
    Lets a tool compute the EXACT achievable coverage target: 100% minus
    the seam swath's share of pav_union.  Returns a shapely geometry (may
    be empty) or None.
    """
    import math
    from shapely.geometry import box
    from shapely.ops import unary_union
    if pav_union is None or pav_union.is_empty:
        return None
    minx, miny, maxx, maxy = pav_union.bounds
    # lon/lat extent of the pavement.
    lons, lats = [], []
    for x, y in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)):
        la, lo = layout.m_to_ll(x, y)
        lats.append(la)
        lons.append(lo)
    strips = []
    pad = half_width_m + 5.0
    # Integer LON lines (vertical strips: x ≈ const in this projection).
    for ln in range(math.ceil(min(lons)), math.floor(max(lons)) + 1):
        xl, _ = layout.ll_to_m(lats[0], float(ln))
        strips.append(box(xl - half_width_m, miny - pad,
                          xl + half_width_m, maxy + pad))
    # Integer LAT lines (horizontal strips: y ≈ const).
    for lt in range(math.ceil(min(lats)), math.floor(max(lats)) + 1):
        _, yl = layout.ll_to_m(float(lt), lons[0])
        strips.append(box(minx - pad, yl - half_width_m,
                          maxx + pad, yl + half_width_m))
    if not strips:
        return None
    try:
        return unary_union(strips).intersection(pav_union)
    except Exception:
        return None


def role_counts(layout) -> Counter:
    """Counter of shape roles for shapes with a non-empty polygon."""
    return Counter(
        s.role for s in layout.shapes
        if getattr(s, "polygon", None) is not None
        and not s.polygon.is_empty)


def print_role_counts(layout, label: str = "shapes") -> None:
    rc = role_counts(layout)
    print(f"{label}: {sum(rc.values())}")
    for r in sorted(rc):
        print(f"    {r:<24} {rc[r]}")

"""Find pavement present in ``pav_union`` but MISSING from the final patch.

Builds an airport ONCE, captures the real ``pav_union`` (the input
coverage) and unions every emitted shape's polygon (the output coverage),
then reports ``pav_union − emitted`` — the pavement that exists in the
source union but no emitted shape covers.  Ranks the gaps by area and
dumps them to an OSM layer you can overlay on the patch in JOSM.

This is the "where did pavement go" overview; to pin the gap on a specific
pipeline pass, follow up with ``tools/trace_shape_drops.py``.

Usage:
    venv/bin/python tools/find_missing_pavement.py HECA
    venv/bin/python tools/find_missing_pavement.py CYXY --min-area 200 --top 40

Large-bbox / small-area pieces are thin tiling-residue ribbons (rects not
perfectly tiling the union); compact pieces are solid drops.
"""
from __future__ import annotations

import argparse

from shapely.geometry import Polygon
from shapely.ops import unary_union

import _diag


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _diag.add_common_args(ap)
    ap.add_argument("--min-area", type=float, default=5.0,
                    help="min gap area (m²) to report/dump (default: 5)")
    ap.add_argument("--top", type=int, default=25,
                    help="how many gaps to print (default: 25)")
    args = ap.parse_args(argv)
    out = args.out or f"/tmp/{args.icao}_missing.osm"

    print(f"Building {args.icao} ...", flush=True)
    layout, pav_union = _diag.build_capturing_union(args.icao, args.xplane)
    if pav_union is None:
        print("ERROR: pav_union was not captured (see _diag).")
        return 1

    emitted = unary_union([
        s.polygon if s.polygon.is_valid else s.polygon.buffer(0)
        for s in layout.shapes
        if getattr(s, "polygon", None) is not None and not s.polygon.is_empty])

    # pav_union is the SOURCE OF TRUTH.  The quality score is how much of
    # it the emitted shapes cover.  Asymmetric weighting (user 2026-05-21):
    #   * UNCOVERED pav_union pavement = a gap = IMPORTANT (we erased
    #     pavement that should be there).
    #   * Emitted area OUTSIDE pav_union = over-coverage = LOW PRIORITY
    #     (a shape we didn't need; harmless-ish, not pavement loss).
    covered = pav_union.intersection(emitted).area
    coverage_pct = 100.0 * covered / pav_union.area if pav_union.area else 0.0
    over = emitted.difference(pav_union).area
    # Exact achievable target: 100% for most airports, minus the tile-seam
    # swath (tile_cut removes a ~10 m gap along any integer-degree boundary
    # the pavement crosses).  Anything missing BEYOND the seam is a bug.
    swath = _diag.seam_swath(layout, pav_union)
    seam_area = swath.area if swath is not None and not swath.is_empty else 0.0
    target_pct = 100.0 * (pav_union.area - seam_area) / pav_union.area \
        if pav_union.area else 0.0
    print(f"pav_union (source of truth): {pav_union.area:,.0f} m²")
    print(f"emitted: {len(layout.shapes)} shapes, {emitted.area:,.0f} m²")
    print(f"COVERAGE of pav_union: {coverage_pct:.3f}%  "
          f"({pav_union.area - covered:,.0f} m² uncovered ← IMPORTANT)")
    if seam_area > 0:
        print(f"TARGET coverage: {target_pct:.3f}%  "
              f"(100% − {seam_area:,.0f} m² tile-seam swath)")
    else:
        print("TARGET coverage: 100.000%  (no tile seam)")
    shortfall = (pav_union.area - covered) - seam_area
    # Tolerance = vertex-snap residue noise floor (sub-meter seams between
    # abutting shapes accumulate during mutation).  Anything beyond this is
    # a structural gap worth tracing with monitor_coverage / the list below.
    tol = max(500.0, 0.0002 * pav_union.area)
    verdict = "MET ✓" if shortfall <= tol \
        else f"SHORT by {shortfall:,.0f} m² ← trace with monitor_coverage"
    print(f"vs target: {verdict}  (tolerance {tol:,.0f} m²)")
    print(f"over-coverage outside pav_union: {over:,.0f} m²  (low priority)")

    missing = pav_union.difference(emitted)
    # Trim hairline numeric edge slivers so the list is signal.
    missing = missing.buffer(-0.5).buffer(0.5)
    pieces = [p for p in _diag.iter_polys(missing) if p.area >= args.min_area]
    pieces.sort(key=lambda p: p.area, reverse=True)
    total = sum(p.area for p in pieces)
    # Coverage-role shapes for the per-gap "nearest" annotation, so each
    # gap is classified: ENCLOSED (a residue hole between shapes) vs an
    # isolated/edge region, and what kind of pavement abuts it.
    nbr = [(s.role, s.polygon)
           for s in layout.shapes
           if _diag.is_coverage_role(getattr(s, "role", ""))
           and getattr(s, "polygon", None) is not None
           and not s.polygon.is_empty]
    print(f"\nMISSING (in pav_union, not covered by any emitted shape): "
          f"{total:,.0f} m² across {len(pieces)} piece(s) ≥ {args.min_area:g} m²")
    print("  enclosed% = fraction of the gap's perimeter abutting emitted "
          "pavement (high → residue hole; low → isolated/over-subtracted)")
    for i, p in enumerate(pieces[:args.top]):
        c = p.centroid
        lat, lon = layout.m_to_ll(c.x, c.y)
        w = p.bounds[2] - p.bounds[0]
        h = p.bounds[3] - p.bounds[1]
        enclosed = 100.0 * (p.boundary.intersection(emitted.buffer(0.6)).length
                            / max(1.0, p.boundary.length))
        near_role, near_d = ("-", float("inf"))
        if nbr:
            near_role, near_d = min(((r, poly.distance(c)) for r, poly in nbr),
                                    key=lambda t: t[1])
        print(f"  #{i + 1:<2} {p.area:>9,.0f} m²  bbox {w:.0f}x{h:.0f}m  "
              f"enclosed~{enclosed:>3.0f}%  near={near_role}@{near_d:.0f}m  "
              f"@ {lat:.6f},{lon:.6f}")

    dump = unary_union(pieces) if pieces else Polygon()
    npoly, nrings = _diag.geom_to_osm(
        dump, layout.m_to_ll, out, tag_k="missing", tag_v="yes")
    print(f"\nWrote {out}: {npoly} piece(s), {nrings} ring(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rasterized reach-band field (Tier 3 wave 2a).

The reach band is the lower/upper envelope of cones of slope ``cap`` seeded at
the runway anchors, measured INSIDE the pavement-with-holes:

    ceiling(x) = min over anchors a ( value_a + cap · d_pavement(x, a) )
    floor(x)   = max over anchors a ( value_a − cap · d_pavement(x, a) )

The legacy :func:`building_feasibility.reach_band_unified` answers this per query
with a nearest-visible-centerline scan (~77 % of a KBNA build) plus an off-net
skeleton fallback (~74 ms/point).  This module answers it ONCE per airport with a
precomputed raster field and then O(1) nearest-cell reads.

Method (research survey candidate S1 / the FH generalized-distance-transform
entry):

1. Rasterize the pavement-with-holes union into a boolean mask at
   ``RASTER_REACH_BAND_CELL_M``; erode conservatively by ½ cell so the discrete
   domain ⊆ the true pavement and discrete geodesics never UNDER-estimate the
   true in-pavement distance.
2. Paint a per-cell longitudinal ``cap`` field: the taxi cap (1.5 %) as the base
   (the reach climbs along the taxi ROUTE at taxi cap, which the legacy band also
   credits — an apron 1 % discount spuriously tightens apron ceilings and shifts
   the solve fixpoint), and the narrow code-A/B cap (3 %) inside the corridors of
   narrow taxi centerlines — the same per-letter credit the legacy band gives.
3. Snap every (de-crowned) runway anchor to its nearest paved cell and seed it at
   its value.
4. Two multi-source Dijkstra passes over the masked grid graph (edge weight =
   mean-cap · Euclidean step) via a virtual super-source
   (``scipy.sparse.csgraph.dijkstra``) settle the ceiling and floor fields — the
   exact additively-weighted cone envelope on the grid graph.
5. ``band(x, y)`` reads the nearest cell; an off-mask point reads the nearest
   paved cell within a bounded radius (via a distance transform), widened by
   ``APRON_MAX_GRADE × offset``, else returns ``None`` (off-net).

This is a DELIBERATE SEMANTIC REPLACEMENT, gated ``O4_RASTER_REACH_BAND``
(default ON since Tier 3 wave 2b, 2026-07-18 — the adjacent-ground tear classes
the tighter band opened are reconciled; see ``config.py`` and
``adjacent_ground._heal_emitted_band_tears``).  It is NOT byte-identical to the
legacy band.  The solve and the validator both build the band through
:func:`reach_band_unified`, so gating there keeps them on the same producer.
"""

from __future__ import annotations

import math
import os
from typing import Callable, Optional, Tuple

import numpy as np

# Per-cell cap sentinels are the real grade caps (rise/run); imported lazily in
# the builder so the module stays import-light.

_INF = float("inf")


def _local_cap_grids(layout, cxs, cys, paved, taxi_cap):
    """Per-cell longitudinal cap field over the paved cells.

    ``cxs``/``cys`` are the 1-D cell-centre coordinate axes; ``paved`` is the
    ``[nrows, ncols]`` boolean mask.  The BASE cap is the taxi cap (1.5 %): the
    reach ceiling at any pavement point is the runway value plus the climb along
    the taxi ROUTE to it (taxi cap), which the legacy ``_band_via`` also credits
    — a per-cell apron discount (1 %) instead makes the min-plus envelope prefer
    cheap apron shortcuts and read a spuriously tight apron ceiling that shifts
    the solve fixpoint (measured at CYXY: apron-1 % steered the solve to +40
    route_band; taxi-cap base gave −57).  Narrow (code-A/B) taxi-centerline
    corridors are then painted at the narrow cap (3 %) — the same per-letter
    credit the legacy band gives.  ``O4_RASTER_APRON_CAP`` overrides the base cap
    (experiment knob).  Returns ``float64[nrows, ncols]`` (0.0 off-mask)."""
    import shapely as _sh
    from shapely.ops import unary_union
    from auto_patch.config import (
        TAXI_MAX_GRADE_NARROW, taxi_grade_cap_for_letter)

    nrows, ncols = paved.shape
    base_cap = float(os.environ.get("O4_RASTER_APRON_CAP", taxi_cap))
    cap = np.where(paved, base_cap, 0.0)

    # Mesh of paved cell centres for the vectorised contains tests.
    gx, gy = np.meshgrid(cxs, cys)                 # [nrows, ncols]
    flat_x = gx[paved]
    flat_y = gy[paved]
    paved_ij = np.argwhere(paved)                  # rows of (r, c)

    def _paint(geom, value):
        if geom is None or geom.is_empty:
            return
        try:
            _sh.prepare(geom)
            hit = _sh.contains_xy(geom, flat_x, flat_y)
        except Exception:                          # pragma: no cover
            return
        rr = paved_ij[hit, 0]
        cc = paved_ij[hit, 1]
        cap[rr, cc] = value

    # Narrow (code-A/B) taxi corridors: paint per-SEGMENT so a route that
    # changes width along its length credits 3 % only where it is narrow (the
    # same ``cl.size_at_arc`` cap the legacy band's foot climb reads).
    narrow_bufs = []
    half_w = 7.5                                   # taxiway half-width corridor
    for cl in (getattr(layout, "apt_taxi_centerlines", None) or []):
        line = getattr(cl, "line", None)
        if line is None or line.is_empty or getattr(cl, "is_service", False):
            continue
        coords = list(line.coords)
        acc = 0.0
        for i in range(len(coords) - 1):
            (x0, y0), (x1, y1) = coords[i], coords[i + 1]
            seg_len = math.hypot(x1 - x0, y1 - y0)
            mid = acc + 0.5 * seg_len
            acc += seg_len
            try:
                letter = cl.size_at_arc(mid)
            except Exception:                      # pragma: no cover
                letter = ""
            if taxi_grade_cap_for_letter(letter) != TAXI_MAX_GRADE_NARROW:
                continue
            seg = _sh.LineString([(x0, y0), (x1, y1)])
            narrow_bufs.append(seg.buffer(half_w))
    if narrow_bufs:
        try:
            _paint(unary_union(narrow_bufs), TAXI_MAX_GRADE_NARROW)
        except Exception:                          # pragma: no cover
            pass

    return cap


def _domain_geom(layout):
    """The reach-field PROPAGATION domain: the airside pavement the band governs
    (taxi rects, aprons, junctions, cross-connectors, stubs), the runway and its
    crossings, and building pads — buffered ½ m to bridge weld seams.  The runway
    IS included: measured across SPJC/CYXY the extra cross-runway connectivity
    lowers CYXY route_band by a further ~28 (−57 vs −29) with no SPJC change, and
    it lets the geodesic route through runway-crossing regions the way the legacy
    spine graph does.  Returns ``(prepared, raw)`` or ``(None, None)``."""
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from auto_patch.elevation_per_surface.building_feasibility import _VIS_BUFFER_M
    from auto_patch.layout import (
        ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
        ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB)
    domain_roles = frozenset({
        ROLE_APRON, ROLE_JUNCTION, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_CROSS_CONNECTOR,
        ROLE_RUNWAY, ROLE_RUNWAY_CROSSING, ROLE_BUILDING})
    polys = [s.polygon for s in layout.shapes
             if s.role in domain_roles and s.polygon is not None
             and not s.polygon.is_empty]
    if not polys:
        return None, None
    try:
        raw = unary_union(polys).buffer(_VIS_BUFFER_M)
    except Exception:                                      # pragma: no cover
        try:
            raw = unary_union([p.buffer(0) for p in polys]).buffer(_VIS_BUFFER_M)
        except Exception:
            return None, None
    if raw.is_empty:
        return None, None
    return prep(raw), raw


def _anchor_seeds(layout, G):
    """``{node_idx: de-crowned_value}`` — the runway-reach anchor set for the
    field: the centerline→runway JOINS ``G.runway_anchor``, lifted into the ONE
    uncrowned profile space the band lives in (the EXACT anchor set the legacy
    :func:`reach_band_unified` seeds).  The raster propagates over the whole
    pavement mask — runways included — so these joins reach every connected
    pavement cell (a no-centerline taxiway crossing a runway routes to a join
    through the runway pavement); the legacy skeleton fallback's extra runway-ring
    seeds are unnecessary and would over-permit the ceiling versus the legacy
    centerline band."""
    from auto_patch.elevation_per_surface.building_feasibility import (
        _decrowned_anchor_seeds)
    return dict(_decrowned_anchor_seeds(
        layout, G, getattr(G, "runway_anchor", {}) or {}))


def solve_reach_fields(paved, cap, seed_min, seed_max, cell, connectivity=8):
    """Settle the ceiling and floor reach fields on a masked grid.

    ``paved`` (``bool[nrows, ncols]``), ``cap`` (``float[nrows, ncols]`` local
    longitudinal cap, 0 off-mask), ``seed_min`` / ``seed_max`` (``{cell_id:
    value}`` — the min / max anchor seed value per paved cell, cell_id being the
    row-major rank among paved cells), ``cell`` (metres), ``connectivity`` (8 or
    16).  Returns ``(ceiling, floor)`` full-grid ``float64[nrows, ncols]`` arrays
    (``inf`` on non-paved / unreachable cells for the ceiling; the floor mirrors).

    ceiling[c] = min over anchors a ( value_a + Σ mean-cap · Euclidean-step )
    floor[c]   = max over anchors a ( value_a − Σ mean-cap · Euclidean-step )

    computed exactly on the grid graph via a virtual super-source
    (``scipy.sparse.csgraph.dijkstra``): the ceiling super-source edge to anchor
    cell ``a`` weighs ``value_a − Vmin`` (≥ 0) so the settled cost is
    ``min_a(value_a − Vmin + d)`` ⇒ ceiling = cost + Vmin; the floor edge weighs
    ``Vmax − value_a`` ⇒ floor = Vmax − cost.  ``directed=False`` treats the four
    unique grid offsets as undirected 8-neighbour edges."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra

    nrows, ncols = paved.shape
    cell_id = np.full((nrows, ncols), -1, dtype=np.int64)
    paved_flat = paved.ravel()
    n_paved = int(paved_flat.sum())
    if n_paved == 0 or not seed_min:
        return (np.full((nrows, ncols), _INF), np.full((nrows, ncols), -_INF))
    cell_id.ravel()[paved_flat] = np.arange(n_paved, dtype=np.int64)

    offsets = [(0, 1, 1.0), (1, 0, 1.0),
               (1, 1, math.sqrt(2.0)), (1, -1, math.sqrt(2.0))]
    knight = [(1, 2), (2, 1), (2, -1), (1, -2)]
    rows_all, cols_all, data_all = [], [], []

    def _add_edges(dr, dc, step, require_mids=None):
        rs = slice(max(0, -dr), nrows - max(0, dr))
        cs = slice(max(0, -dc), ncols - max(0, dc))
        src = cell_id[rs, cs]
        dst = cell_id[rs.start + dr:rs.stop + dr, cs.start + dc:cs.stop + dc]
        both = (src >= 0) & (dst >= 0)
        if require_mids is not None:
            for (mr, mc) in require_mids:
                both = both & paved[rs.start + mr:rs.stop + mr,
                                    cs.start + mc:cs.stop + mc]
        cap_s = cap[rs, cs][both]
        cap_d = cap[rs.start + dr:rs.stop + dr,
                    cs.start + dc:cs.stop + dc][both]
        rows_all.append(src[both])
        cols_all.append(dst[both])
        data_all.append(0.5 * (cap_s + cap_d) * (step * cell))

    for (dr, dc, step) in offsets:
        _add_edges(dr, dc, step)
    if int(connectivity) >= 16:
        for (dr, dc) in knight:
            step = math.sqrt(dr * dr + dc * dc)
            mids = [(int(np.sign(dr)) if abs(dr) == 2 else 0,
                     int(np.sign(dc)) if abs(dc) == 2 else 0),
                    (dr - (int(np.sign(dr)) if abs(dr) == 2 else 0),
                     dc - (int(np.sign(dc)) if abs(dc) == 2 else 0))]
            _add_edges(dr, dc, step, require_mids=mids)

    grid_rows = (np.concatenate(rows_all) if rows_all
                 else np.empty(0, dtype=np.int64))
    grid_cols = (np.concatenate(cols_all) if cols_all
                 else np.empty(0, dtype=np.int64))
    grid_data = (np.concatenate(data_all) if data_all
                 else np.empty(0, dtype=float))

    src_cells = np.fromiter(sorted(seed_min), dtype=np.int64, count=len(seed_min))
    vmin = min(seed_min.values())
    vmax = max(seed_max.values())
    ceil_seed = np.array([seed_min[int(c)] - vmin for c in src_cells])
    floor_seed = np.array([vmax - seed_max[int(c)] for c in src_cells])
    super_id = n_paved
    n_nodes = n_paved + 1

    def _field(seed_weights, transform):
        rows = np.concatenate(
            [grid_rows, np.full(len(src_cells), super_id, dtype=np.int64)])
        cols = np.concatenate([grid_cols, src_cells])
        data = np.concatenate([grid_data, seed_weights])
        M = csr_matrix((data, (rows, cols)), shape=(n_nodes, n_nodes))
        dist = dijkstra(M, directed=False, indices=super_id)
        out = np.full(nrows * ncols, _INF)
        out[paved_flat] = transform(dist[:n_paved])
        return out.reshape(nrows, ncols)

    ceiling = _field(ceil_seed, lambda d: d + vmin)
    floor = _field(floor_seed, lambda d: vmax - d)
    return (ceiling, floor)


def build_raster_reach_band(layout, G) -> Optional[Callable[
        [float, float], "Tuple[float, float] | None"]]:
    """Build the rasterized reach band ``band(x, y) -> (floor, ceiling) | None``.

    Returns ``None`` when the field cannot be built (no anchors, no pavement,
    empty mask, or the grid exceeds ``RASTER_REACH_BAND_MAX_CELLS``) — the caller
    then falls through to the legacy band."""
    from scipy.ndimage import distance_transform_edt
    import shapely as _sh
    from auto_patch.config import (
        APRON_MAX_GRADE, TAXI_MAX_GRADE, RASTER_REACH_BAND_CELL_M,
        RASTER_REACH_BAND_CONNECTIVITY, RASTER_REACH_BAND_OFFNET_RADIUS_M,
        RASTER_REACH_BAND_MAX_CELLS)

    if not getattr(G, "pos", None):
        return None
    seeds = _anchor_seeds(layout, G)
    if not seeds:
        return None

    # Pavement-with-holes PROPAGATION domain (runway interior excluded — see
    # :func:`_domain_geom`).
    _prep, geom = _domain_geom(layout)
    if geom is None or geom.is_empty:
        return None

    cell = float(RASTER_REACH_BAND_CELL_M)
    minx, miny, maxx, maxy = geom.bounds
    # Fixed grid origin (determinism): snap the origin DOWN to a cell multiple so
    # the same airport always rasterizes onto the same lattice.
    x0 = math.floor(minx / cell) * cell - cell
    y0 = math.floor(miny / cell) * cell - cell
    ncols = int(math.ceil((maxx - x0) / cell)) + 2
    nrows = int(math.ceil((maxy - y0) / cell)) + 2
    if ncols <= 0 or nrows <= 0:
        return None
    if ncols * nrows > RASTER_REACH_BAND_MAX_CELLS:
        try:
            import O4_UI_Utils as _UI
            _UI.vprint(1, f"  [raster-reach-band] grid {nrows}x{ncols} "
                          f"({nrows * ncols} cells) exceeds cap "
                          f"{RASTER_REACH_BAND_MAX_CELLS}; legacy band used")
        except Exception:                          # pragma: no cover
            pass
        return None

    cxs = x0 + (np.arange(ncols) + 0.5) * cell
    cys = y0 + (np.arange(nrows) + 0.5) * cell

    # Raw paved mask: cell centre inside the union.  One vectorised call over the
    # whole grid (chunked to bound peak memory of the coordinate arrays).
    gx, gy = np.meshgrid(cxs, cys)                 # [nrows, ncols]
    try:
        _sh.prepare(geom)
        raw = _sh.contains_xy(geom, gx.ravel(), gy.ravel()).reshape(nrows, ncols)
    except Exception:                              # pragma: no cover
        return None
    if not raw.any():
        return None

    # Conservative ½-cell erosion (discrete domain ⊆ true pavement): a cell stays
    # paved only if the union still contains its centre after shrinking inward by
    # ½ cell.  A thin corridor (≥3 cells wide by construction) survives.
    try:
        eroded_geom = geom.buffer(-0.5 * cell)
    except Exception:                              # pragma: no cover
        eroded_geom = None
    if eroded_geom is not None and not eroded_geom.is_empty:
        try:
            _sh.prepare(eroded_geom)
            paved = raw & _sh.contains_xy(
                eroded_geom, gx.ravel(), gy.ravel()).reshape(nrows, ncols)
        except Exception:                          # pragma: no cover
            paved = raw
    else:
        paved = raw
    if not paved.any():
        paved = raw                                # erosion closed everything

    # Per-cell cap field.
    cap = _local_cap_grids(layout, cxs, cys, paved, TAXI_MAX_GRADE)

    # Contiguous paved-cell indexing (row-major, deterministic).
    cell_id = np.full((nrows, ncols), -1, dtype=np.int64)
    paved_flat = paved.ravel()
    n_paved = int(paved_flat.sum())
    cell_id.ravel()[paved_flat] = np.arange(n_paved, dtype=np.int64)

    # Snap each anchor to its nearest paved cell, aggregating per cell the min and
    # max seed value (min feeds the ceiling super-source, max the floor).
    # ``distance_transform_edt`` gives, for EVERY cell, the nearest paved cell —
    # so an anchor landing just off the mask still seeds the pavement it abuts.
    inv = ~paved
    edt_cells, edt_idx = distance_transform_edt(
        inv, return_indices=True) if inv.any() else (
            np.zeros((nrows, ncols)), None)
    seed_min: dict = {}                            # cell_id -> min value
    seed_max: dict = {}                            # cell_id -> max value
    n_seeded = 0
    for k in sorted(seeds):                        # sorted() for determinism
        p = G.pos.get(k)
        if p is None:
            continue
        val = float(seeds[k])
        ci = int(round((p[0] - x0) / cell - 0.5))
        ri = int(round((p[1] - y0) / cell - 0.5))
        if not (0 <= ri < nrows and 0 <= ci < ncols):
            continue
        if not paved[ri, ci]:
            if edt_idx is None:
                continue
            ri2 = int(edt_idx[0, ri, ci])
            ci2 = int(edt_idx[1, ri, ci])
            if not paved[ri2, ci2]:
                continue
            ri, ci = ri2, ci2
        cid = int(cell_id[ri, ci])
        if cid < 0:
            continue
        seed_min[cid] = min(seed_min.get(cid, _INF), val)
        seed_max[cid] = max(seed_max.get(cid, -_INF), val)
        n_seeded += 1
    if not seed_min:
        return None

    ceiling, floor = solve_reach_fields(
        paved, cap, seed_min, seed_max, cell,
        connectivity=int(RASTER_REACH_BAND_CONNECTIVITY))

    # Off-mask nearest-paved lookup (bounded radius) via the distance transform
    # already computed.  ``edt_cells`` is in CELL units.
    off_radius = float(RASTER_REACH_BAND_OFFNET_RADIUS_M)
    apron_cap = float(APRON_MAX_GRADE)

    def band(x, y):
        ci = int(round((x - x0) / cell - 0.5))
        ri = int(round((y - y0) / cell - 0.5))
        if not (0 <= ri < nrows and 0 <= ci < ncols):
            return None
        if paved[ri, ci]:
            c = ceiling[ri, ci]
            if not math.isfinite(c):
                return None                        # paved but unreachable → None
            return (float(floor[ri, ci]), float(c))
        if edt_idx is None:
            return None
        off = float(edt_cells[ri, ci]) * cell
        if off > off_radius:
            return None
        nr = int(edt_idx[0, ri, ci])
        nc = int(edt_idx[1, ri, ci])
        c = ceiling[nr, nc]
        if not math.isfinite(c):
            return None
        slack = apron_cap * off
        return (float(floor[nr, nc]) - slack, float(c) + slack)

    # Diagnostics on the closure (opt-in probes; also read by the profile A/B).
    band.raster_meta = {                           # type: ignore[attr-defined]
        "nrows": nrows, "ncols": ncols, "cells": nrows * ncols,
        "paved_cells": n_paved, "cell_m": cell,
        "connectivity": int(RASTER_REACH_BAND_CONNECTIVITY),
        "anchors_seeded": n_seeded,
        "grid_bytes": int((ceiling.nbytes + floor.nbytes + paved.nbytes
                           + cap.nbytes + cell_id.nbytes)),
    }
    if os.environ.get("O4_RASTER_REACH_BAND_QUIET") != "1":
        try:
            import O4_UI_Utils as _UI
            m = band.raster_meta
            _UI.vprint(1, f"  [raster-reach-band] {nrows}x{ncols} @ {cell:.1f} m "
                          f"({n_paved} paved / {m['cells']} cells), "
                          f"{n_seeded} anchor(s), conn-"
                          f"{m['connectivity']}, ~{m['grid_bytes'] // (1 << 20)} MB")
        except Exception:                          # pragma: no cover
            pass
    return band

"""Per-building route-feasibility elevation (user model, 2026-06-22).

A building that touches airside pavement is seated FLAT at the elevation
closest to its DEM that keeps it reachable WITHIN GRADE from EVERY runway
threshold along the real taxi route — the "heaviest anchor" the rest of the
network then grades to (docs/taxi_centerline_grading_plan.md §9).

The route to a building (user's definition, validated on CYXY):

  1. a PERPENDICULAR from the building centroid to the NEAREST taxi
     centerline (named or not — geometry only).  The part of that
     perpendicular inside the taxiway-width corridor climbs at the
     taxiway's own cap; the part beyond (real apron) at the apron cap (1%).
  2. from the foot point, the taxi-route to each threshold along the
     centerline graph at the PER-EDGE per-letter caps (narrow A/B = 3 %,
     wide C–F = 1.5 %) — including the partial first edge from the foot
     point to its graph node (the bit that snapping to the node would drop).

The feasibility BAND is the intersection over ALL thresholds:

  ceiling = min_t ( thr_elev_t + climb_t )
  floor   = max_t ( thr_elev_t − climb_t )

and the seated level is ``clamp(DEM, floor, ceiling)`` — closest to DEM,
could be above or below it.  Buildings NOT touching airside pavement are
omitted (the caller leaves them at their DEM).

A narrow code-A/B arm contributes its 3 % cap.  The ICAO size travels
PER-SEGMENT on each ``apt_dat_reader.TaxiCenterline`` (``seg_sizes``), so the
per-letter cap at the foot is read off the geometry — no name→letter table.
"""

from __future__ import annotations

import heapq
import math
import os
from typing import Callable, Dict, List, Tuple

from shapely.errors import GEOSException

from auto_patch.config import (
    ANISO_EDGES,
    BUILDING_AIRSIDE_CONTACT_MIN_COMPONENT_M2,
    BUILDING_FULL_FRONTAGE,
    BUILDING_FULL_FRONTAGE_AREA_M2,
    BUILDING_SEAT_FLATNESS_TOLERANCE_M,
    FLAT_CERTIFICATE_COVERAGE,
    TAXI_MAX_GRADE, VISIBLE_CHORD_CONNECT,
)
from auto_patch.grade_law import APRON_MAX_GRADE, BUILDING_REACH_CORRIDOR_M
from auto_patch.layout import (
    ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
)

__all__ = ["building_feasible_levels", "reach_band_unified",
           "runway_edge_anchors"]

# Pavement a building must touch to count as airside-served (else → DEM).
_AIRSIDE_ROLES = frozenset({
    ROLE_APRON, ROLE_JUNCTION, ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
    ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_CROSS_CONNECTOR,
})
_TAXI_HALF_W_M = 7.5       # taxiway half-width corridor (perp split point)
_TOUCH_TOL_M = 2.0         # building↔airside distance to count as "touching"
_MULTI_ROUTE_M = 30.0      # junction-band: widen ceiling over routes within this
_INF = float("inf")
_UNSET = object()          # build-wide-cache sentinel (distinguishes None result)


_VIS_BUFFER_M = 0.5        # bridge weld-seam slivers between abutting shapes
_VIS_ON_PAV_FRAC = 0.97    # chord counts as visible if ≥ this fraction is paved
# Minimum paved mid-cell samples (of n) for float(paved/n) >= _VIS_ON_PAV_FRAC —
# the integer form of the _paved_frac accept comparison, replicated exactly so
# the two-stage sampler in _accept_flags can early-reject without changing any
# accept decision.
_ACCEPT_MIN_PAVED = {
    n: next(p for p in range(n + 1) if (p / n) >= _VIS_ON_PAV_FRAC)
    for n in range(1, 97)
}
# A reach binding is PHANTOM only when the serving centerline is BOTH far AND
# only reachable across grass.  The CYXY south runway-crossing junction binds a
# centerline 367 m away over 77 % grass; a building/apron vertex a few tens of m
# from its serving centerline across a small grass sliver is NOT phantom (forcing
# IT onto the skeleton band shifts building seats — building12's pad).  The perp
# gate isolates the egregious case without disturbing normal apron-frontage reach.
_PHANTOM_MIN_PERP_M = 100.0


def _pavement_visibility(layout):
    """Prepared airside-pavement geometry (∪ building pads) for the visible-chord
    test — a building→taxiway connection is legal only if its chord stays within
    pavement (the user's rule: never taxi across grass / a service road; a spine
    is a centerline, so apron pavement counts).  Building pads are included so a
    chord may start inside the building's own pad.  Buffered slightly to bridge
    numerical weld-seam slivers between abutting shapes.

    PERF (build-wide cache): the prepared union is a pure function of the airside
    pavement + building 2D geometry, which is FROZEN after phase-1 layout — the
    solve moves only elevations and the emit only appends ``graded_strip`` pieces
    (not in ``_AIRSIDE_ROLES``), so ``vis`` is byte-identical across the
    construct / solve / emit ``reach_band_unified`` calls.  The union+buffer+prep
    was rebuilt on every call (≥3×/build); cache it on the layout so it is built
    once.  Same object each call ⇒ no output change."""
    cached = getattr(layout, "_pav_vis_cache", _UNSET)
    if cached is not _UNSET:
        return cached
    from shapely.ops import unary_union
    from shapely.prepared import prep
    polys = [s.polygon for s in layout.shapes
             if (s.role in _AIRSIDE_ROLES or s.role == ROLE_BUILDING)
             and s.polygon is not None and not s.polygon.is_empty]
    vis = None
    if polys:
        try:
            u = unary_union(polys).buffer(_VIS_BUFFER_M)
            vis = prep(u)
        except Exception:                                  # pragma: no cover
            vis = None
    try:
        layout._pav_vis_cache = vis
    except Exception:                                      # pragma: no cover
        pass
    return vis


def _cl_by_distance(c, cls, tree=None, max_r=None):
    """Iterate centerlines in true-distance order from ``c`` — STRtree-backed
    when a ``tree`` (``STRtree(cls)``) is given, so a query touches the
    handful of nearby lines instead of distance-scanning the whole list (the
    reach band runs ~10k queries × ~500 lines; the full scans were ~half the
    solve time).  With ``max_r`` only lines within that distance are yielded.
    Without a tree this is the plain full sort, so behaviour is identical."""
    if tree is None:
        for L in sorted(cls, key=lambda L: L.distance(c)):
            if max_r is not None and L.distance(c) > max_r:
                break
            yield L
        return
    if max_r is not None:
        try:
            idxs = tree.query(c.buffer(max_r))
        except Exception:                                  # pragma: no cover
            idxs = range(len(cls))
        cand = sorted((cls[int(k)].distance(c), int(k)) for k in idxs)
        for d, k in cand:
            if d <= max_r:
                yield cls[k]
        return
    # expanding rings: everything within r is in the candidate set, so the
    # ≤r prefix of each round is exact global distance order.
    seen: set = set()
    r = 60.0
    while True:
        try:
            idxs = tree.query(c.buffer(r))
        except Exception:                                  # pragma: no cover
            for L in sorted(cls, key=lambda L: L.distance(c)):
                yield L
            return
        cand = sorted((cls[int(k)].distance(c), int(k))
                      for k in idxs if int(k) not in seen)
        for d, k in cand:
            if d <= r:
                seen.add(k)
                yield cls[k]
        if r > 1e5:                    # exhausted: yield any stragglers
            rest = sorted((cls[k].distance(c), k)
                          for k in range(len(cls)) if k not in seen)
            for _d, k in rest:
                yield cls[k]
            return
        r *= 4.0



def _paved_frac(chord, vis) -> float:
    """Fraction of ``chord`` on pavement, by VECTORIZED point sampling
    (``shapely.contains_xy`` on the prepared pavement — one C call) instead
    of an exact line∩polygon overlay (~0.5 ms each).  The overlay was 60 %
    of the whole CYUL build: the visible-chord walk tries ~50 candidates
    per node on a fragmented centerline network and paid it on every miss
    (1.27 M calls, 650 s).  Per-point *Python* shapely calls are no cheaper
    than the overlay (call overhead dominates) — the batch call is.
    Sampling at ≤1 m (capped 96 points) resolves ``_VIS_ON_PAV_FRAC``
    comfortably (a 3 % gap on a 30 m chord is ~1 m)."""
    import numpy as _np
    import shapely as _sh
    coords = list(chord.coords)
    (ax, ay), (bx, by) = coords[0], coords[-1]
    L = chord.length
    if L < 1e-9:
        return 1.0
    n = min(96, max(8, int(L)))
    t = (_np.arange(n) + 0.5) / n
    geom = getattr(vis, "context", vis)
    try:
        _sh.prepare(geom)          # idempotent; cached on the geometry
        hits = _sh.contains_xy(geom, ax + (bx - ax) * t, ay + (by - ay) * t)
        return float(hits.mean())
    except Exception:              # pragma: no cover — old shapely fallback
        from shapely.geometry import Point as _P
        hit = sum(1 for k in range(n)
                  if vis.contains(_P(ax + (bx - ax) * t[k],
                                     ay + (by - ay) * t[k])))
        return hit / n


def _paved_fracs(chords, vis):
    """Vectorised :func:`_paved_frac` over a LIST of chord LineStrings — the
    seam-gap paved fraction for each, returned as a ``list[float]`` in input
    order.  Every chord's sample points (the SAME ``(arange(n)+0.5)/n`` mid-cell
    sampling, same ``n = min(96, max(8, int(L)))``) are concatenated into ONE
    ``shapely.contains_xy`` call, then the per-chord mean is sliced back out —
    bit-identical to calling :func:`_paved_frac` on each chord, but paying the
    numpy/GEOS call overhead ONCE per batch instead of once per candidate (the
    phantom reach-band tail ran this ~2 M times, one candidate at a time)."""
    import numpy as _np
    import shapely as _sh
    geom = getattr(vis, "context", vis)
    _sh.prepare(geom)
    m = len(chords)
    fracs = [1.0] * m
    if m == 0:
        return fracs
    chords_arr = (chords if isinstance(chords, _np.ndarray)
                  else _np.asarray(chords, dtype=object))
    # Endpoints + lengths in ONE vectorised call each (each chord is a 2-point
    # LineString ``[foot, c]``): coords rows are [foot0, c, foot1, c, ...].
    cc = _sh.get_coordinates(chords_arr)
    a = cc[0::2]                    # feet  (chord start)
    b = cc[1::2]                    # c     (chord end)
    Ls = _sh.length(chords_arr)
    segs = []                      # (out_index, start, n)
    xs_parts = []
    ys_parts = []
    total = 0
    for idx in range(m):
        L = float(Ls[idx])
        if L < 1e-9:
            continue               # frac stays 1.0 (matches _paved_frac)
        n = min(96, max(8, int(L)))
        t = (_np.arange(n) + 0.5) / n
        ax, ay = a[idx, 0], a[idx, 1]
        bx, by = b[idx, 0], b[idx, 1]
        xs_parts.append(ax + (bx - ax) * t)
        ys_parts.append(ay + (by - ay) * t)
        segs.append((idx, total, n))
        total += n
    if total:
        hits = _sh.contains_xy(geom, _np.concatenate(xs_parts),
                               _np.concatenate(ys_parts))
        for (idx, start, n) in segs:
            fracs[idx] = float(hits[start:start + n].mean())
    return fracs


def _nearest_visible_centerline(c, cls, vis, tree=None, cache=None,
                                cls_arr=None):
    """The nearest centerline to point ``c`` whose connecting chord stays within
    pavement (``vis``).  Falls back to the straight-line nearest if none is
    visible (e.g. a building wholly off pavement — the caller's touch test has
    already gated that out).  ``tree``: optional ``STRtree(cls)`` (see
    :func:`_cl_by_distance`).

    ``cache`` (build-wide reach-band memo, optional ``dict``): the result is a
    pure function of ``(c, cls, vis)`` — the taxi-centerline set and the airside
    pavement union, both FROZEN 2D geometry for the whole build — so the same
    query point yields the same serving centerline in the construct, the solve,
    and the emit.  Keyed on the exact float coordinates ⇒ identical result ⇒ no
    output change.  The returned ``ln`` is one of the persistent ``cl.line``
    objects (stable across the per-stage ``cls`` rebuilds), so downstream
    ``id(ln)`` lookups stay valid.  Callers that do NOT pass a cache (e.g. the
    gated building-frontage-spine anchor) are byte-identical to before.

    PERF (vectorised candidate scan): this walk was ~77 % of a KBNA build
    (cProfile) — the per-vertex reach-band query tests centerlines in distance
    order until a ≥ ``_VIS_ON_PAV_FRAC`` visible chord is found, and the
    "phantom" vertices (no visible centerline; isolated apron/pavement pieces)
    march the WHOLE centerline set.  The old loop paid a full stack of Python
    shapely wrappers PER candidate (``nearest_points`` → ``shortest_line``,
    ``LineString`` construction, ``vis.contains``): ~35 M wrapped calls, ~100 s
    of the 175 s CYXY replay were wrapper overhead alone.  Pull the
    distance-ordered generator in GROWING chunks and evaluate each chunk with
    ONE vectorised ``shapely.shortest_line`` + ONE prepared ``shapely.contains``
    (``vis.contains(x)`` is exactly ``shapely.contains(vis.context, x)``), then
    fall to the per-chord ``_paved_frac`` seam-gap test only for the chords that
    fail the exact test — in the SAME distance order, returning the SAME first
    acceptable centerline.  ``shapely.shortest_line(ln, c)`` returns the chord
    ``[foot_on_ln, c]`` — the reverse of the old ``[c, foot]`` — but chord length,
    prepared containment and ``_paved_frac`` (whose mid-point sample set is
    symmetric about the chord centre) are all orientation-invariant, so the
    accept/reject decision is bit-identical.  Growing the chunk keeps the common
    median-1-candidate query cheap while the phantom tail is batched."""
    import shapely as _sh
    import numpy as _np
    from itertools import islice as _islice
    key = None
    if cache is not None:
        key = (c.x, c.y)
        hit = cache.get(key, _UNSET)
        if hit is not _UNSET:
            return hit

    def _cache_and_return(result):
        if key is not None:
            cache[key] = result
        return result

    vis_ctx = getattr(vis, "context", vis)
    # ENDPOINT-MARGIN PRUNE (perf 2026-07-15, KBNA profile): the query point's
    # distance to the pavement union, ``m_c``, is an exact lower bound on every
    # candidate chord's unpaved length — the chord ENDS at ``c``, and no point
    # within ``m_c`` of ``c`` is on pavement.  Two bit-exact consequences used
    # in the accept loops below:
    #   * exact contains is ALWAYS False when ``m_c > 0`` (the endpoint is
    #     strictly outside the polygon), so the GEOS line-contains test is
    #     skipped wholesale — profiling showed 1.5 M scalar contains calls on
    #     the deep walkers;
    #   * the sampled paved fraction of a chord of length L has at least
    #     ``floor(n·m_c/L)`` off-pavement mid-cell samples, so any candidate
    #     with ``(n - (floor(n·m_c/L) - 1)) / n < _VIS_ON_PAV_FRAC`` cannot
    #     accept and is rejected WITHOUT sampling (the -1 absorbs alignment
    #     and float-rounding slack; the 1/n quantum dwarfs any ULP effect).
    # A phantom point 40 m out in grass thus rejects every candidate nearer
    # than ~1 km with plain numpy arithmetic — this was ~85 % of the reach-band
    # wall (170 M contains_xy samples per KBNA build).
    m_c = float(_sh.distance(vis_ctx, c))

    def _viable_mask(lens):
        if m_c <= 0.0:
            return None
        n_s = _np.minimum(96, _np.maximum(8, lens.astype(_np.int64)))
        u_safe = _np.maximum(
            _np.floor(n_s * (m_c / _np.maximum(lens, 1e-12))) - 1.0, 0.0)
        return ((n_s - u_safe) / n_s) >= _VIS_ON_PAV_FRAC

    def _accept_flags(chords, lens, count):
        """Per-candidate boolean of ``paved_frac >= _VIS_ON_PAV_FRAC`` — the
        only thing the accept loop consumes — computed with two bit-exact
        volume cuts over :func:`_paved_fracs`:

        * candidates failing the endpoint-margin prune are False outright;
        * the fraction test on n mid-cell samples is equivalent to the
          INTEGER test ``paved_count >= _ACCEPT_MIN_PAVED[n]`` (the table
          replicates the float ``mean() >= 0.97`` comparison exactly), so a
          chord is rejected as soon as its unpaved count exceeds
          ``n - _ACCEPT_MIN_PAVED[n]`` (≤ 3 samples at n = 96).  Stage 1
          evaluates only the first 16 mid-cells of the SAME sample set —
          enough to reject nearly every grass-crossing chord — and only the
          undecided minority pays for the remaining samples.  Same sample
          points, same total counts, same accept set.

        Returns ``(flags, needs_exact)``.  ``needs_exact`` (``m_c == 0``
        queries only, else None) marks the rejected chords for which the
        exact line-contains RESCUE could still differ from the sampled
        verdict: containment requires every chord point in the polygon's
        closure, so a rejected chord with ANY unpaved sample strictly
        outside the closure is provably not contained — and a prepared
        point-``intersects`` on each rejected chord's first unpaved sample
        (one batched C call; on-boundary ⟺ True) separates the two cases
        exactly.  Only boundary-degenerate chords (in practice none) keep
        ``needs_exact`` and pay the expensive line-contains, which on long
        walker chords measures ~2.4 ms even prepared.
        """
        viable = _viable_mask(lens)
        kept = (list(range(count)) if viable is None
                else [j for j in range(count) if viable[j]])
        flags = [False] * count
        track_exact = (m_c == 0.0)
        reject_pts = []
        if not kept:
            return flags, ([False] * count if track_exact else None)
        cc = _sh.get_coordinates(chords[kept] if len(kept) < count
                                 else chords)
        a, b = cc[0::2], cc[1::2]
        geom = vis_ctx
        _sh.prepare(geom)
        n_per = []
        for jj, j in enumerate(kept):
            L = float(lens[j])
            if L < 1e-9:
                flags[j] = True         # _paved_frac returns 1.0 for these
                n_per.append(0)
                continue
            n_per.append(min(96, max(8, int(L))))
        # Stage 1: first min(16, n) mid-cells of each kept chord, one batch.
        xs, ys, segs = [], [], []
        for jj, j in enumerate(kept):
            n = n_per[jj]
            if n == 0:
                continue
            k1 = min(16, n)
            t = (_np.arange(k1) + 0.5) / n
            xs.append(a[jj, 0] + (b[jj, 0] - a[jj, 0]) * t)
            ys.append(a[jj, 1] + (b[jj, 1] - a[jj, 1]) * t)
            segs.append((jj, j, k1))
        undecided = []
        if segs:
            hits = _sh.contains_xy(geom, _np.concatenate(xs),
                                   _np.concatenate(ys))
            off = 0
            for si, (jj, j, k1) in enumerate(segs):
                n = n_per[jj]
                h1 = hits[off:off + k1]
                paved1 = int(h1.sum())
                off += k1
                min_paved = _ACCEPT_MIN_PAVED[n]
                if (k1 - paved1) > n - min_paved:
                    if track_exact:     # first unpaved sample of the reject
                        u = int(h1.argmin())
                        reject_pts.append((j, xs[si][u], ys[si][u]))
                    continue            # early exact reject
                if k1 == n:
                    flags[j] = paved1 >= min_paved
                    if track_exact and not flags[j]:
                        u = int(h1.argmin())
                        reject_pts.append((j, xs[si][u], ys[si][u]))
                else:
                    undecided.append((jj, j, k1, paved1,
                                      (int(h1.argmin()), xs[si], ys[si])
                                      if paved1 < k1 else None))
        # Stage 2: remaining mid-cells of the undecided chords, one batch.
        if undecided:
            xs2, ys2 = [], []
            for (jj, j, k1, _p, _u1) in undecided:
                n = n_per[jj]
                t = (_np.arange(k1, n) + 0.5) / n
                xs2.append(a[jj, 0] + (b[jj, 0] - a[jj, 0]) * t)
                ys2.append(a[jj, 1] + (b[jj, 1] - a[jj, 1]) * t)
            hits = _sh.contains_xy(geom, _np.concatenate(xs2),
                                   _np.concatenate(ys2))
            off = 0
            for si, (jj, j, k1, paved1, u1) in enumerate(undecided):
                n = n_per[jj]
                h2 = hits[off:off + n - k1]
                paved = paved1 + int(h2.sum())
                off += n - k1
                flags[j] = paved >= _ACCEPT_MIN_PAVED[n]
                if track_exact and not flags[j]:
                    if u1 is not None:  # first unpaved was in stage 1
                        ui, uxs, uys = u1
                        reject_pts.append((j, uxs[ui], uys[ui]))
                    else:               # all of stage 1 paved → in stage 2
                        u = int(h2.argmin())
                        reject_pts.append((j, xs2[si][u], ys2[si][u]))
        needs_exact = None
        if track_exact:
            needs_exact = [False] * count
            if reject_pts:
                plaus = _sh.intersects(
                    geom, _sh.points([q[1] for q in reject_pts],
                                     [q[2] for q in reject_pts]))
                for k, (j, _x, _y) in enumerate(reject_pts):
                    if plaus[k]:
                        needs_exact[j] = True
        return flags, needs_exact

    gen = _cl_by_distance(c, cls, tree)
    first = None
    chunk_size = 4
    tested = 0
    while True:
        # VECTORIZED DEEP WALK (perf 2026-07-15, KBNA profile): a query still
        # rejecting after its first ~28 candidates is a long-tail walker (a
        # phantom point or a far seam station), and the per-candidate Python
        # overhead of the generator walk — not the GEOS predicates, which
        # measure ~1-8 µs each — is what made these walks ~12 ms.  The
        # expanding-ring generator yields candidates in exact global
        # ``(distance, index)`` order (see _cl_by_distance), so ONE stable
        # argsort over ONE vectorized ``shapely.distance`` call reproduces
        # that order bit-for-bit; resume at position ``tested`` and stride
        # through the tail in large blocks with the same per-candidate accept
        # rule.  Same order, same rule, same fallback ⇒ identical results.
        if tested >= 28 and tree is not None and cls_arr is not None:
            dists = _sh.distance(cls_arr, c)
            order = _np.argsort(dists, kind="stable")
            pos, stride, n_all = tested, 64, len(order)
            while pos < n_all:
                take = order[pos:pos + stride]
                chords = _sh.shortest_line(cls_arr[take], c)
                lens = _sh.length(chords)
                try:
                    acc, needs_exact = _accept_flags(chords, lens, len(take))
                except Exception:                          # pragma: no cover
                    acc = needs_exact = None
                if acc is None:                            # pragma: no cover
                    exact = _sh.contains(vis_ctx, chords)
                    for j in range(len(take)):
                        if lens[j] < 1e-6 or exact[j]:
                            return _cache_and_return(cls[int(take[j])])
                else:
                    for j in range(len(take)):
                        if (lens[j] < 1e-6 or acc[j]
                                or (needs_exact is not None
                                    and needs_exact[j]
                                    and _sh.contains(vis_ctx, chords[j]))):
                            return _cache_and_return(cls[int(take[j])])
                pos += stride
                stride = min(stride * 4, 1024)
            break                       # exhausted: fall through to fallback
        chunk = list(_islice(gen, chunk_size))
        if not chunk:
            break
        if first is None:
            first = chunk[0]
        tested += len(chunk)
        arr = _np.empty(len(chunk), dtype=object)
        for _i, _ln in enumerate(chunk):
            arr[_i] = _ln
        chords = _sh.shortest_line(arr, c)          # [foot_on_ln, c] per candidate
        lens = _sh.length(chords)
        # The accept is the FIRST candidate (distance order) that is coincident
        # (zero-length chord) OR exact-visible (contained) OR ≥ _VIS_ON_PAV_FRAC
        # paved — a plain OR per candidate, so evaluation order is free.
        # PAVED-FRACTION FIRST (perf 2026-07-15, KBNA profile): the sampled
        # fraction (≤96 ``contains_xy`` points, ~tens of µs) is ~20× cheaper
        # than an exact prepared line-contains on the huge airside union
        # (~ms), and on pavement it accepts almost every candidate the exact
        # test would.  Batch the fraction over the WHOLE chunk, then pay the
        # expensive exact test ONLY for candidates the sampling rejects — a
        # chord grazing the polygon boundary can sample a boundary point as
        # outside yet still be exactly contained, so the exact test stays as
        # the rescue.  Same accept set in the same distance order ⇒ the same
        # centerline is returned, bit-identically.
        try:
            acc, needs_exact = _accept_flags(chords, lens, len(chunk))
        except Exception:                                  # pragma: no cover
            acc = needs_exact = None
        if acc is None:                                    # pragma: no cover
            exact = _sh.contains(vis_ctx, chords)
            for j in range(len(chunk)):
                if lens[j] < 1e-6 or exact[j]:
                    return _cache_and_return(chunk[j])
        else:
            for j in range(len(chunk)):
                if (lens[j] < 1e-6 or acc[j]
                        or (needs_exact is not None and needs_exact[j]
                            and _sh.contains(vis_ctx, chords[j]))):
                    return _cache_and_return(chunk[j])
        chunk_size = min(chunk_size * 2, 512)
    return _cache_and_return(
        first if first is not None else min(
            cls, key=lambda L: L.distance(c)))


def _chord_on_pavement(c, foot, vis):
    """True when the straight chord from ``c`` to its centerline ``foot`` stays on
    pavement (≥ ``_VIS_ON_PAV_FRAC`` paved) — the SAME visible-chord rule
    :func:`_nearest_visible_centerline` uses to accept a connection.  A chord that
    mostly crosses grass is a PHANTOM route (you cannot taxi it), so the reach band
    must not be bound through it."""
    from shapely.geometry import LineString
    chord = LineString([(c.x, c.y), (foot.x, foot.y)])
    if chord.length < 1e-6:
        return True
    # Paved-fraction first (perf 2026-07-15) — same OR of the same two tests
    # as before, with the ~20× cheaper sampled test promoted ahead of the
    # exact prepared contains; see _nearest_visible_centerline.
    try:
        if _paved_frac(chord, vis) >= _VIS_ON_PAV_FRAC:
            return True
    except Exception:                                      # pragma: no cover
        pass
    return vis.contains(chord)


def _decrowned_anchor_seeds(layout, G, anchor_elev):
    """Lift CROWNED runway-edge anchor values into the ONE uncrowned profile
    space the reach band is documented to solve in.

    The runway-join anchors are VALUE-DERIVED from the EMITTED runway edge
    (2026-07-16 edge-anchor ruling), so on a wide runway each carries the
    profile MINUS the transverse crown drop (``RUNWAY_CROWN_TRANSVERSE`` ×
    half-width).  The band, however, is the in-solve profile field and its
    consumer ``grade_graph_validate.route_band_violations`` de-crowns each
    vertex by ``+crown_drop`` before comparing — the ONE uncrowned space the
    space invariant declares.  Adding the crown drop back at each anchor's
    own position puts the seed in that same space (``crown_drop_at`` returns
    0.0 with no crown field, so non-crowned airports stay byte-identical).

    ``anchor_elev`` is ``{node_index: crowned_elev}``; returns a new dict with
    the same keys and de-crowned values."""
    from auto_patch.crown import crown_drop_at
    out = {}
    for k, ae in anchor_elev.items():
        p = G.pos.get(k)
        out[k] = float(ae) + (crown_drop_at(layout, p[0], p[1]) if p else 0.0)
    return out


def _build_skeleton_band(layout, G):
    """EDGE-SKELETON reach for NO-CENTERLINE pavement (user 2026-06-28).

    A taxiway/apron that has no taxi centerline never enters the centerline spine,
    so :func:`reach_band_unified`'s centerline path returns ``None`` for it and the
    apron it feeds becomes an unreachable island (no band → ``route_reach`` flags,
    feeders land at incompatible DEM levels).  This builds a SECOND reach over the
    pavement's WELDED EDGE SKELETON — ``G.edges`` (abutting shapes share EXACT node
    indices, so connectivity needs no perpendicular tolerance) unioned with the
    centerline ``spine_adj`` — anchored at BOTH the centerline→runway joins
    (``G.runway_anchor``) AND every pavement node COINCIDENT with a runway segment
    (a no-centerline taxiway reaches the runway through a CROSSING the
    centerline-endpoint anchor misses).  Returns ``band(x, y) -> (floor, ceiling)
    | None`` = the nearest skeleton node's reach interval, widened by the apron cap
    over the offset to it.  Used ONLY where the centerline band is ``None``, so
    centerlined airports are unchanged."""
    from shapely.geometry import Point
    from shapely.strtree import STRtree
    from auto_patch.layout import ROLE_RUNWAY
    from auto_patch.grade_graph_validate import _shape_elevs, _open_ring

    def _d(a, b):
        pa, pb = G.pos.get(a), G.pos.get(b)
        return math.hypot(pa[0] - pb[0], pa[1] - pb[1]) if pa and pb else 1e-3

    # Augmented adjacency: centerline spine ∪ welded edge skeleton.
    adj: dict = {}

    def _add(a, b, w):
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))

    for u, nbrs in G.spine_adj.items():
        for (v, b) in nbrs:
            _add(u, v, b)
    for (a, b, cap, _sp) in G.edges:
        if a in G.pos and b in G.pos:
            _add(a, b, cap.at(_d(a, b), 0.0))

    # Anchors: centerline→runway joins + runway-coincident pavement nodes.
    # All seeds are de-crowned into the ONE uncrowned profile space the band
    # is documented to solve in (see :func:`_decrowned_anchor_seeds` and the
    # matching de-crown in reach_band_unified): the runway-join anchors AND
    # the runway-ring vertices both carry the EMITTED crowned edge value, but
    # route_band_violations compares against a de-crowned vertex, so seeding
    # the reach field crowned depresses the whole field by the edge drop.
    from auto_patch.crown import crown_drop_at
    anchor_elev = _decrowned_anchor_seeds(layout, G, G.runway_anchor)
    pos_to_idx = {(round(x, 3), round(y, 3)): i for (i, (x, y)) in G.pos.items()}
    for s in layout.shapes:
        if (s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        elevs = _shape_elevs(s, len(ring))
        if elevs is None:
            continue
        for (x, y), e in zip(ring, elevs):
            i = pos_to_idx.get((round(x, 3), round(y, 3)))
            if i is not None and e is not None and i not in anchor_elev:
                anchor_elev[i] = float(e) + crown_drop_at(layout, x, y)
    if not anchor_elev:
        return lambda x, y: None

    # Value-seeded reach FIELDS (perf 2026-07-04): the per-anchor loop
    # ran a full Dijkstra PER anchor (550 at KDFW → 140 s of the build,
    # cProfile) yet only ever consumed ``min(ae + dist)`` /
    # ``max(ae − dist)`` across anchors.  ONE multi-source pass per
    # field — every anchor seeded at its own elevation — settles each
    # node at its optimal (anchor, path) pair: the same min/max by
    # commutation.  ``dist`` is accumulated separately and the field
    # value formed as ``ae + dist`` (one addition, exactly the
    # per-anchor expression) so patches stay byte-identical.  Strict
    # settled-set pop guard — see the lazy-Dijkstra re-expand hang.
    def _anchor_value_field(sign):
        best: dict = {}
        pq = [((ae if sign > 0 else -ae), 0.0, ae, k)
              for k, ae in anchor_elev.items() if k in adj]
        heapq.heapify(pq)
        while pq:
            _key, dd, ae, u = heapq.heappop(pq)
            if u in best:
                continue
            best[u] = (ae + dd) if sign > 0 else (ae - dd)
            for (v, budget) in adj.get(u, ()):
                if v in best:
                    continue
                nd = dd + budget
                heapq.heappush(
                    pq, (((ae + nd) if sign > 0 else -(ae - nd)),
                         nd, ae, v))
        return best

    node_ceil = _anchor_value_field(+1)
    node_floor = _anchor_value_field(-1)
    if not node_ceil:
        return lambda x, y: None

    bidx = list(node_ceil.keys())
    bpts = [Point(*G.pos[i]) for i in bidx if i in G.pos]
    bidx = [i for i in bidx if i in G.pos]
    if not bpts:
        return lambda x, y: None
    tree = STRtree(bpts)

    def band(x, y):
        try:
            j = bidx[int(tree.nearest(Point(x, y)))]
        except Exception:                                     # pragma: no cover
            return None
        off = math.hypot(G.pos[j][0] - x, G.pos[j][1] - y)
        slack = APRON_MAX_GRADE * off
        return (node_floor[j] - slack, node_ceil[j] + slack)

    return band


def reach_band_unified(layout, G):
    """The reach band computed on THE unified grade graph — the SAME graph the
    spine solves on and the validator checks (user 2026-06-27, "stop building the
    same thing in different ways").

    Reachability is a cap-Dijkstra over ``G.spine_adj`` from ``G.runway_anchor``
    (the exact nodes + elevations the spine solve pins), so the ceiling is the
    spine's ACHIEVABLE level and is cap-consistent along the spine BY CONSTRUCTION
    — no separate route graph, no per-node inconsistency, no ceiling-consistency
    bridge.  Geometry of the perpendicular foot still uses the taxi centerlines (an
    accurate perp), but the foot's reachable elevation comes from the nearest
    unified spine node.  The band contract is
    ``band(x, y) -> (floor, ceiling) | None``."""
    import heapq
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    if not getattr(G, "runway_anchor", None) or not getattr(G, "spine_adj", None):
        return lambda x, y: None

    # RASTER REACH FIELD (Tier 3 wave 2a, ``O4_RASTER_REACH_BAND``, default OFF):
    # replace the per-query nearest-visible-centerline evaluation below with a
    # precomputed grid field (one masked multi-source Dijkstra per direction, O(1)
    # nearest-cell reads).  This is a SEMANTIC replacement — the true min-plus cone
    # envelope in the grid metric, not byte-identical to the legacy band (spec §3.5
    # "Wave 1 outcome") — consumed by BOTH the solve and the validator through this
    # one producer, so they stay aligned.  A None return (no pavement / empty grid /
    # over the cell cap) falls through to the legacy band below.  Gate off restores
    # the legacy nvc band byte-identically.
    from auto_patch.config import RASTER_REACH_BAND
    # Runtime env overrides the import-time default (so an A/B harness or a test
    # can flip the gate after import); unset ⇒ the config default (off).
    _rrb_env = os.environ.get("O4_RASTER_REACH_BAND")
    _rrb_on = (_rrb_env == "1") if _rrb_env is not None else RASTER_REACH_BAND
    if _rrb_on:
        try:
            from auto_patch.elevation_per_surface.raster_reach_band import (
                build_raster_reach_band)
            _raster = build_raster_reach_band(layout, G)
        except Exception:                                      # pragma: no cover
            _raster = None
        if _raster is not None:
            # OFF-NET POLICY (mission item 3): the raster field answers a query
            # inside the mask directly and a point just off the mask via the
            # nearest paved cell within ``RASTER_REACH_BAND_OFFNET_RADIUS_M``
            # (widened by the apron-cap slack); a point beyond that radius, or a
            # paved component unreachable from any anchor, returns None (off-net —
            # the local within-shape law governs it, exactly as the zone-node skip
            # already yields None).  We deliberately do NOT fall back to the legacy
            # skeleton band for far points: that ~74 ms/point path is precisely the
            # cost this field eliminates (mission: "the off-net fallback path
            # disappearing is a big part of the win"), and a point tens of metres
            # from every paved cell is legitimately off the taxi network.
            return _raster

    # De-crown the runway-join anchor SEEDS into the ONE uncrowned profile
    # space this band is documented to live in (space invariant — see
    # crown.crown_drop_at / the flex_slack_at note; grade_graph_validate.
    # route_band_violations de-crowns each vertex by ``+crown_drop`` before
    # the band test).  ``G.runway_anchor`` is VALUE-DERIVED from the EMITTED
    # runway edge (2026-07-16 edge-anchor ruling), which on a wide runway
    # carries the profile MINUS the transverse crown drop; seeding the field
    # with that crowned value depresses the whole ceiling/floor field by the
    # edge drop, so a de-crowned airside vertex reads its own designed crown
    # drop above the ceiling (SPJC: 216 phantom ``ceil`` flags, ~0.30 m at a
    # ~30 m half-width edge).  ``crown_drop_at`` is 0.0 with no crown field,
    # so non-crowned airports stay byte-identical.
    anchor_seeds = _decrowned_anchor_seeds(layout, G, G.runway_anchor)

    # Value-seeded reach FIELDS (perf 2026-07-04, same collapse as the
    # skeleton band above): the per-anchor Dijkstras were only ever
    # consumed as ``min over anchors (ae + dist + extras)`` /
    # ``max over anchors (ae − dist − extras)`` with anchor-independent
    # extras, so ONE multi-source pass per field replaces |anchors|
    # full-graph passes.  Each field stores the winning ``(dist, ae)``
    # PAIR so ``_band_via`` can form its candidate values with exactly
    # the original float expression (ae + ((dist + foot) + perp)) —
    # patches stay byte-identical.
    def _runway_value_field(sign):
        best: dict = {}
        pq = [((ae if sign > 0 else -ae), 0.0, ae, k)
              for (k, ae) in anchor_seeds.items()]
        heapq.heapify(pq)
        while pq:
            _key, dd, ae, u = heapq.heappop(pq)
            if u in best:
                continue
            best[u] = (dd, ae)
            for (v, budget) in G.spine_adj.get(u, ()):
                if v in best:
                    continue
                nd = dd + budget
                heapq.heappush(
                    pq, (((ae + nd) if sign > 0 else -(ae - nd)),
                         nd, ae, v))
        return best

    ceiling_field = _runway_value_field(+1)
    floor_field = _runway_value_field(-1)
    if not ceiling_field:
        return lambda x, y: None

    # Unified spine-node positions for the perp-foot → reachable-node lookup.
    sidx = [i for i in G.spine_adj if i in G.pos]
    if not sidx:
        return lambda x, y: None
    spts = [Point(*G.pos[i]) for i in sidx]
    tree = STRtree(spts)

    def _nn(pt):
        try:
            return sidx[int(tree.nearest(Point(pt[0], pt[1])))]
        except Exception:                                     # pragma: no cover
            return None

    # Per-centerline longitudinal cap from its OWN ICAO code letter (apt.dat
    # row-1202) — the SAME per-letter cap the spine edges carry.  Used for the
    # foot climb (along the serving centerline + the taxiway-width perp) so the
    # band credits a code-A/B taxiway at its real 3 %, CONSISTENTLY at every query
    # point.  The old spine-edge lookup read the cap off the edge between the two
    # nearest spine nodes to the foot's centerline segment, falling back to 1.5 %
    # whenever those nodes were not a DIRECT edge — which happens on any long
    # apt.dat segment — so the SAME taxiway was credited 3 % at a building
    # frontage but only 1.5 % at an apron 60 m further along it: the building seat
    # and the route-band check then disagreed though both go through this one band
    # (CYXY A2 = code B, a 457 m segment → 118 false apron ceil flags).
    from auto_patch.config import taxi_grade_cap_for_letter
    # PER-SEGMENT cap: keep the TaxiCenterline so the foot climb credits the cap of
    # the SEGMENT the foot lands on (no name→letter table; a route may change width
    # along its length).  ``cap_of`` maps a line's identity to its TaxiCenterline.
    cls_tcl = [cl for cl in (getattr(layout, "apt_taxi_centerlines", None) or [])
               if cl.line is not None and not cl.line.is_empty
               and not cl.is_service]
    if not cls_tcl:
        return lambda x, y: None
    cls = [cl.line for cl in cls_tcl]
    # Object ndarray over the same lines, built ONCE per factory call — the
    # vectorized deep walk in _nearest_visible_centerline needs one and
    # filling it per query would cost ~0.5 µs × |cls| × every walker.
    import numpy as _np_arr
    cls_arr = _np_arr.empty(len(cls), dtype=object)
    for _i, _ln in enumerate(cls):
        cls_arr[_i] = _ln
    cap_of = {id(cl.line): cl for cl in cls_tcl}
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
    # STRtree over the centerlines: every band query needs them in distance
    # order (nearest-visible + the multi-route widening); the naive per-query
    # full sort was ~half the solve time (~10k queries × ~500 lines).
    try:
        from shapely.strtree import STRtree as _STRtree
        cl_tree = _STRtree(cls) if len(cls) > 8 else None
    except Exception:                                      # pragma: no cover
        cl_tree = None

    # ANISOTROPIC EDGES: a JUNCTION is graded UNIFORMLY at the taxi cap (its body
    # is the spine's per-letter cap, not 1 %), so a junction foot point beyond the
    # taxiway corridor must climb at the taxi cap too — NOT drop to APRON_MAX_GRADE.
    # Without this the band under-credits the junction interior and false-flags the
    # very junction-body points the new edge law lets climb (docs/anisotropic_edge_
    # handling_plan.md Phase 4).  Prepared junction union; tested per query.  Gate
    # OFF ⇒ ``junc_zone`` is None and the foot climb is byte-identical (apron drop).
    junc_zone = None
    if ANISO_EDGES:
        try:
            from shapely.ops import unary_union
            from shapely.prepared import prep
            jpolys = [s.polygon for s in layout.shapes
                      if s.role == ROLE_JUNCTION and s.polygon is not None
                      and not s.polygon.is_empty]
            if jpolys:
                junc_zone = prep(unary_union(jpolys))
        except Exception:                                     # pragma: no cover
            junc_zone = None

    # EDGE-SKELETON fallback (user 2026-06-28): no-centerline pavement returns
    # None below; the skeleton band reaches it over the welded edge graph +
    # runway-contact anchors.  Used ONLY where the centerline path is None, so
    # centerlined airports are unchanged.  Gate O4_SKELETON_REACH=0 disables.
    _skel_band = (_build_skeleton_band(layout, G)
                  if os.environ.get("O4_SKELETON_REACH", "1") == "1" else None)

    def _fallback(x, y):
        return _skel_band(x, y) if _skel_band is not None else None

    def _band_via(c, x, y, ln, in_junc):
        """``(floor, ceil)`` reachable for point ``c`` SERVED by centerline ``ln``,
        or None if ``ln`` cannot serve it (no anchored spine node).  ``in_junc`` ⇒
        the climb beyond the taxiway corridor stays at the taxi cap (uniform
        junction), not the apron cap."""
        perp = c.distance(ln)
        coords = list(ln.coords)
        sp = ln.project(c)
        acc = 0.0
        A = B = None
        for i in range(len(coords) - 1):
            seg_len = math.hypot(coords[i + 1][0] - coords[i][0],
                                 coords[i + 1][1] - coords[i][1])
            if acc - 1e-6 <= sp <= acc + seg_len + 1e-6:
                A = (coords[i], sp - acc)
                B = (coords[i + 1], (acc + seg_len) - sp)
                break
            acc += seg_len
        if A is None:
            A = (coords[0], 0.0)
            B = (coords[-1], ln.length)
        kA = _nn(A[0])
        kB = _nn(B[0])
        if kA is None and kB is None:
            return None
        _tcl = cap_of.get(id(ln))
        ecap = (taxi_grade_cap_for_letter(_tcl.size_at_arc(sp))
                if _tcl is not None else TAXI_MAX_GRADE)
        beyond_cap = ecap if in_junc else APRON_MAX_GRADE
        perp_climb = (ecap * min(perp, _TAXI_HALF_W_M)
                      + beyond_cap * max(0.0, perp - _TAXI_HALF_W_M))
        # Candidate values from the collapsed fields: each field entry is
        # the winning (dist, ae) pair for its node, and for fixed foot
        # extras the same anchor wins with the extras added — so forming
        # ``ae + ((dist + foot) + perp)`` here reproduces the per-anchor
        # loop's minima to the float bit.
        floor, ceil = -_INF, _INF
        for (k, foot) in ((kA, A[1]), (kB, B[1])):
            fc = ceiling_field.get(k)
            if fc is not None:
                dd, ae = fc
                ceil = min(ceil, ae + ((dd + ecap * foot) + perp_climb))
            ff = floor_field.get(k)
            if ff is not None:
                dd, ae = ff
                floor = max(floor, ae - ((dd + ecap * foot) + perp_climb))
        if ceil >= _INF:
            return None
        return (floor, ceil)

    # Build-wide serving-centerline memo (see _nearest_visible_centerline): the
    # scan is a pure function of the frozen 2D geometry, so the construct, the
    # solve and the emit share ONE cache keyed by exact query point.
    _nvc_cache = layout.__dict__.setdefault("_reach_nvc_cache", {})

    def _serving_line(c):
        """The serving centerline for point ``c`` — the nearest-visible
        centerline (memoized) when visibility is on, else the plain nearest."""
        if vis is not None:
            return _nearest_visible_centerline(c, cls, vis, tree=cl_tree,
                                               cache=_nvc_cache, cls_arr=cls_arr)
        return next(_cl_by_distance(c, cls, cl_tree),
                    min(cls, key=lambda L: L.distance(c)))

    def _band_for_line(x, y, c, ln):
        """The band ``(floor, ceil) | None`` for ``c`` given its serving line
        ``ln`` — everything ``band`` does AFTER the serving-line lookup (phantom
        fallback, ``_band_via``, junction multi-route widening).  Splitting the
        serving-line lookup out lets the cluster batch reuse a proven-shared
        line without re-running the expensive nearest-visible scan, while this
        tail (and therefore the returned value) stays bit-identical to
        ``band``."""
        perp = c.distance(ln)
        if vis is not None and perp > _PHANTOM_MIN_PERP_M:
            from shapely.ops import nearest_points
            foot = nearest_points(ln, c)[0]
            if not _chord_on_pavement(c, foot, vis):
                # PHANTOM binding: the only nearby centerline is FAR and only
                # reachable ACROSS GRASS (CYXY south runway-crossing junction →
                # centerline D, 367 m, 23 % paved).  Binding the reach band to it
                # pins the floor far above the node's real route (3.3 m high → a
                # 36.7 % body cliff).  Reach it over the welded-edge SKELETON
                # instead — that taxiway over its OWN pavement.
                return _fallback(x, y)
        in_junc = junc_zone is not None and junc_zone.contains(c)
        prim = _band_via(c, x, y, ln, in_junc)
        if prim is None:
            return _fallback(x, y)
        floor, ceil = prim
        # ANISOTROPIC EDGES: the within-shape law lets a point be reached from ANY
        # nearby converging spine route, but the band above used the NEAREST
        # centerline only — under-crediting the ceiling and false-flagging the
        # junction/apron interiors the new law lets climb.  Widen to the
        # most-reachable route over the other on-pavement centerlines within a
        # BOUNDED reach (so a genuinely-too-high point still flags — no far route
        # reaches it) and only across a VISIBLE on-pavement chord, so band and law
        # agree without over-loosening.
        if junc_zone is not None:
            from shapely.ops import nearest_points
            others = [L for L in _cl_by_distance(c, cls, cl_tree,
                                                 max_r=_MULTI_ROUTE_M)
                      if L is not ln][:3]
            for L in others:
                if vis is not None:
                    foot = nearest_points(L, c)[0]
                    if not _chord_on_pavement(c, foot, vis):
                        continue
                r = _band_via(c, x, y, L, in_junc)
                if r is not None:
                    floor = min(floor, r[0])
                    ceil = max(ceil, r[1])
        return (floor, ceil)

    def band(x, y):
        c = Point(x, y)
        return _band_for_line(x, y, c, _serving_line(c))

    def _confirms_line(c, ln):
        """True iff ``_serving_line(c)`` provably equals ``ln`` — a cheap
        SUFFICIENT condition (never a false positive): ``ln`` is the nearest
        centerline to ``c`` AND, under visibility, its chord is on pavement.
        ``_nearest_visible_centerline`` walks candidates in true distance order
        and returns the first whose chord is accepted, so when the nearest line
        itself is accepted it is the serving line — reproducing that decision
        without the multi-candidate walk.  A False result (nearest is some other
        line, or the nearest's chord is not on pavement) is inconclusive, so the
        caller falls back to the exact per-point ``band``.  Bit-identical: a
        confirmed member takes ``_band_for_line(.., ln)`` = ``band`` exactly."""
        first = next(_cl_by_distance(c, cls, cl_tree), None)
        if first is not ln:
            return False
        if vis is None:
            return True
        from shapely.ops import nearest_points
        foot = nearest_points(ln, c)[0]
        return _chord_on_pavement(c, foot, vis)

    def _batch(nodes, limit):
        """Cluster-amortized EXACT band evaluation (Tier 3 wave 1,
        ``O4_REACH_BAND_CLUSTERS``): bucket the first ``limit`` query points
        into ``REACH_BAND_CLUSTER_SIZE_M`` grid cells, run the expensive
        nearest-visible serving-line scan ONCE per cell (at the cell
        representative), and give every other member the SAME line WITHOUT the
        scan when :func:`_confirms_line` proves that line is also the member's
        serving line — then compute its band through the identical
        ``_band_for_line`` tail.  A member the shared line does not provably
        serve takes the exact per-point ``band``.  The result is bit-identical
        to ``[band(x, y) for (x, y) in nodes[:limit]]`` (plus ``None`` past
        ``limit``); the only thing amortized is the serving-line scan for
        members that share the representative's line."""
        from auto_patch.config import REACH_BAND_CLUSTER_SIZE_M
        size = REACH_BAND_CLUSTER_SIZE_M
        n = len(nodes)
        out = [None] * n
        lim = n if limit is None else min(limit, n)
        if lim <= 0:
            return out
        buckets: dict = {}
        for i in range(lim):
            x, y = nodes[i]
            buckets.setdefault((int(math.floor(x / size)),
                                int(math.floor(y / size))), []).append(i)
        n_rep = n_shared = n_exact = 0
        for key in sorted(buckets):
            members = buckets[key]
            cx = (key[0] + 0.5) * size
            cy = (key[1] + 0.5) * size
            rep = min(members,
                      key=lambda i: ((nodes[i][0] - cx) ** 2
                                     + (nodes[i][1] - cy) ** 2, i))
            rx, ry = nodes[rep]
            c_rep = Point(rx, ry)
            ln_rep = _serving_line(c_rep)
            out[rep] = _band_for_line(rx, ry, c_rep, ln_rep)
            n_rep += 1
            for i in members:
                if i == rep:
                    continue
                x, y = nodes[i]
                c = Point(x, y)
                if _confirms_line(c, ln_rep):
                    out[i] = _band_for_line(x, y, c, ln_rep)
                    n_shared += 1
                else:
                    out[i] = band(x, y)
                    n_exact += 1
        if os.environ.get("O4_REACH_BAND_CLUSTER_QUIET") != "1":
            try:
                import O4_UI_Utils as _UI
                _UI.vprint(1, f"  [reach-band-clusters] {len(buckets)} bucket(s): "
                              f"{n_rep} rep scan(s) + {n_exact} exact, "
                              f"{n_shared} line-shared (scan skipped) "
                              f"of {lim} node(s)")
            except Exception:                                  # pragma: no cover
                pass
        return out

    band.batch = _batch                                        # type: ignore
    return band


def _has_visible_corridor(px, py, cls, vis, max_m):
    """True when a taxi corridor lies within ``max_m`` of ``(px, py)`` AND a chord
    from the point to that corridor's spine stays on pavement (a VISIBLE chord —
    the user's gate, 2026-06-27).  ``vis`` None (visibility disabled) → distance
    gate only."""
    from shapely.geometry import Point, LineString
    from shapely.ops import nearest_points
    c = Point(px, py)
    cand = [L for L in cls if L.distance(c) <= max_m]
    if not cand:
        return False
    if vis is None:
        return True
    for ln in sorted(cand, key=lambda L: L.distance(c)):
        foot = nearest_points(ln, c)[0]
        chord = LineString([(px, py), (foot.x, foot.y)])
        if chord.length < 1e-6 or vis.contains(chord):
            return True
        try:                                 # tolerate tiny weld-seam gaps
            if _paved_frac(chord, vis) >= _VIS_ON_PAV_FRAC:
                return True
        except Exception:                                  # pragma: no cover
            pass
    return False


def _frontage_band(poly, band, cls, vis, max_corridor_m):
    """Intersect the route-feasibility ``band`` over the building's ENTIRE
    qualifying FRONTAGE (user 2026-06-27, large buildings only).

    A frontage SAMPLE (every ring vertex + each edge midpoint) qualifies when a
    taxi corridor lies within ``max_corridor_m`` AND a visible on-pavement chord
    reaches that corridor's spine (:func:`_has_visible_corridor`) — i.e. every
    SIDE flanked by a taxi route, not only the apron the building abuts.  The
    band is sampled at every qualifying point and the feasible interval is the
    INTERSECTION — ``(max floor, min ceiling)`` — so the seated flat level keeps
    EVERY such frontage point gradeable to the spine at ≤1 % (not just the central
    chord).  Returns ``(floor, ceiling)`` or ``None`` when no side qualifies
    (caller falls back to the central chord)."""
    ring = list(poly.exterior.coords)
    floor, ceil = -_INF, _INF
    got = False
    for i in range(len(ring) - 1):
        ax, ay = ring[i]
        bx, by = ring[i + 1]
        mx, my = 0.5 * (ax + bx), 0.5 * (ay + by)
        for (px, py) in ((ax, ay), (mx, my), (bx, by)):
            if not _has_visible_corridor(px, py, cls, vis, max_corridor_m):
                continue
            bb = band(px, py)
            if bb is None:
                continue
            floor = max(floor, bb[0])
            ceil = min(ceil, bb[1])
            got = True
    return (floor, ceil) if got else None


def _footprint_dem_relief(poly, dem_sampler):
    """DEM mean and relief (``max − min``) over a building footprint — its ring
    vertices plus its centroid — through the SAME ``dem_sampler`` the seat path
    uses (so the mean is bit-identical to what the band path would sample).

    Returns ``(mean, relief)`` or ``None`` on ANY sampling gap (off-tile, DEM
    error, NaN) — the seat certificate then refuses and the normal band path
    runs, failing toward correctness (spec §2.2)."""
    values = []
    centroid = poly.centroid
    ring = list(poly.exterior.coords)
    for (x, y) in ring + [(centroid.x, centroid.y)]:
        value = dem_sampler(x, y)
        if value is None or value != value:
            return None
        values.append(float(value))
    if not values:
        return None
    return (sum(values) / len(values), max(values) - min(values))


def _footprint_radius(poly, centroid):
    """Largest distance from ``centroid`` to any footprint ring vertex (the
    reach the band-margin soundness guard must cover)."""
    radius = 0.0
    for (x, y) in poly.exterior.coords:
        distance = math.hypot(x - centroid.x, y - centroid.y)
        if distance > radius:
            radius = distance
    return radius


def building_feasible_levels(
        layout,
        runway_pts_xyz: List[Tuple[float, float, float]],
        dem_sampler: Callable[[float, float], "float | None"],
        band=None,
) -> Dict[int, float]:
    """Return ``{id(building_shape): seated_level_m}`` for every
    ``ROLE_BUILDING`` that touches airside pavement.

    ``runway_pts_xyz``: ``[(x_m, y_m, elev_m)]`` every runway ring vertex at its
    solved elevation (the runway-edge anchors — see :func:`runway_edge_anchors`).
    ``dem_sampler(x, y)``: DEM (m) at a layout-local point, or None.  The level
    is ``clamp(DEM, floor, ceiling)`` from the shared route-feasibility band — if
    DEM is not reachable within grade from every runway route, the level is
    pulled into the band (the building is adjusted to be feasible).  Buildings
    not touching airside pavement are omitted (the caller keeps them at DEM).

    ``band``: the pre-built unified-graph band from :func:`reach_band_unified`
    (required) — buildings are placed on the SAME graph the spine is graded on
    (the single graph; they then agree by construction)."""
    from shapely.ops import unary_union

    if band is None:
        raise ValueError(
            "building_feasible_levels requires a prebuilt band "
            "(reach_band_unified); the legacy route-graph sampler was removed")
    polys = [s.polygon for s in layout.shapes
             if s.role in _AIRSIDE_ROLES and s.polygon is not None
             and not s.polygon.is_empty]
    if not polys:
        return {}
    try:
        airside = unary_union(polys)
    except GEOSException:
        # GEOS can hit a non-noded intersection on nearly-collinear
        # sliver edges (KDFW 2026-07-10, two ~0.1 m-apart parallel
        # segments).  buffer(0) renodes each input; the union of the
        # repaired polygons is geometrically the same airside region.
        airside = unary_union([p.buffer(0) for p in polys])
    # Significance gate (user 2026-07-17, KBNA SE lot): a pad only
    # counts as airside-served when the airside COMPONENT it touches is
    # big enough to serve aircraft.  KBNA building23 touched an
    # ISOLATED 66 m² apron scrap and inherited the runway reach floor,
    # emitting 11.6 m above its own ground.  Components come from the
    # union (touching/overlapping shapes merge), so a connected apron
    # complex passes regardless of how the slice fragmented it, while
    # an isolated scrap fails alone.
    airside_components = [
        part for part in (airside.geoms
                          if airside.geom_type == "MultiPolygon"
                          else [airside])
        if not part.is_empty
        and part.area >= BUILDING_AIRSIDE_CONTACT_MIN_COMPONENT_M2]
    if not airside_components:
        return {}
    airside = unary_union(airside_components)

    # Buildings ≥ this footprint must clear their ENTIRE frontage, not just a
    # single central chord (user 2026-06-27); small buildings keep the centroid
    # chord.  Gate off → central chord for every building (legacy, byte-identical).
    full_frontage = (BUILDING_FULL_FRONTAGE
                     and os.environ.get("O4_BUILDING_FULL_FRONTAGE", "1") == "1")
    # Taxi corridors + pavement-visibility for the frontage qualifier (a side
    # grades at 1 % only when a corridor is within range AND visibly chord-reachable).
    cls = vis = None
    if full_frontage:
        cls = [cl.line for cl in
               (getattr(layout, "apt_taxi_centerlines", None) or [])
               if cl.line is not None and not cl.line.is_empty
               and not cl.is_service]
        vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None

    # SEAT CERTIFICATE (Tier 1, spec §3.2 + §2.4 "buildings are FLAT"): a
    # building whose whole footprint DEM relief fits the seat flatness tolerance
    # is feasible FLAT at its DEM mean by inspection, so it skips the expensive
    # per-building reach-band frontage construction and records that mean as its
    # seated level.  Sound guard: the DEM mean must also sit inside the central
    # reach band with a margin ≥ ``footprint_radius · APRON_MAX_GRADE`` (so the
    # whole flat footprint stays reachable — a building near a constraining, and
    # thus tight, route band refuses and takes the normal clamp).  Gated by
    # ``O4_FLAT_CERTIFICATE_COVERAGE`` (read at call time); OFF → byte-identical.
    seat_certificate_enabled = (
        FLAT_CERTIFICATE_COVERAGE
        and os.environ.get("O4_FLAT_CERTIFICATE_COVERAGE", "1") != "0")
    try:
        from auto_patch.elevation_per_surface.solver_primitives import (
            _record_flat_certificate, _report_flat_certificate_counts)
    except Exception:                                      # pragma: no cover
        _record_flat_certificate = None
        _report_flat_certificate_counts = None

    def _record(outcome):
        if _record_flat_certificate is not None:
            _record_flat_certificate(layout, "seat", outcome)

    out: Dict[int, float] = {}
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty):
            continue
        if airside.is_empty or s.polygon.distance(airside) > _TOUCH_TOL_M:
            continue                            # not airside-served → DEM
        c = s.polygon.centroid
        # Seat certificate: a flat footprint seats at its DEM mean, skipping the
        # frontage band, when the mean is comfortably inside the central band.
        if seat_certificate_enabled:
            _record("candidate")
            relief = _footprint_dem_relief(s.polygon, dem_sampler)
            certified_seat = None
            if relief is not None and relief[1] <= BUILDING_SEAT_FLATNESS_TOLERANCE_M:
                seat_mean = relief[0]
                central = band(c.x, c.y)
                if central is not None:
                    floor_c, ceil_c = central
                    margin = APRON_MAX_GRADE * _footprint_radius(s.polygon, c)
                    if floor_c + margin <= seat_mean <= ceil_c - margin:
                        certified_seat = seat_mean
            if certified_seat is not None:
                _record("certified")
                out[id(s)] = certified_seat
                continue
            _record("refused")
        # LARGE building → intersect the band over the whole frontage; SMALL (or no
        # qualifying frontage side) → the single central chord from the centroid.
        b = None
        if (full_frontage and cls
                and s.polygon.area >= BUILDING_FULL_FRONTAGE_AREA_M2):
            b = _frontage_band(s.polygon, band, cls, vis,
                               BUILDING_REACH_CORRIDOR_M)
        if b is None:
            b = band(c.x, c.y)
        if b is None:
            continue
        floor, ceil = b
        de = dem_sampler(c.x, c.y)
        if de is None:
            continue
        if floor > ceil:                        # infeasible → midpoint (adjust)
            out[id(s)] = 0.5 * (floor + ceil)
        else:
            out[id(s)] = min(max(de, floor), ceil)
    # One per-airport certificate summary line (spec §2 item 7), printed once —
    # after the building-seat pass, so rect/apron/junction counts from the
    # preceding constraint build and the seat counts here are all present.
    if (_report_flat_certificate_counts is not None
            and not getattr(layout, "_flat_certificate_reported", False)):
        try:
            _report_flat_certificate_counts(layout, getattr(layout, "icao", ""))
            layout._flat_certificate_reported = True
        except Exception:                                  # pragma: no cover
            pass
    return out

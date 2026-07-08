"""Pre-solve geometry guard (dev instrumentation).

Enforces the invariant of the pre-solve-geometry refactor
(``docs/presolve_geometry_refactor.md``): every geometry change to a
**solver-graded (airside)** shape must happen BEFORE ``per_surface_solve``.
After the solve, only *altitude* is assigned (to non-graded terrain
features) and new *non-airside* shapes are added (clearance).  No airside
vertex may be moved, inserted, welded, snapped, or clipped post-solve.

Usage (env-gated, no behaviour change):

    from .geom_guard import snapshot_airside_geometry, report_post_solve_changes
    snap = snapshot_airside_geometry(layout)        # right before the solve
    ...                                             # solve + post-solve passes
    report_post_solve_changes(layout, snap, icao)   # at emit

The guard is active only when ``O4_GEOM_GUARD=1``.  ``snapshot_airside_geometry``
returns ``None`` (and stamps nothing) when disabled, and
``report_post_solve_changes`` is then a no-op.

Identity tracking: each airside shape is stamped with a unique token at
snapshot time.  Passes that mutate ``shape.polygon`` in place keep the
token (so we compare ring hashes); passes that REPLACE shapes with fresh
``BuiltShape`` objects, drop shapes, or reclassify them out of airside lose
the token — all of which are reported as post-solve geometry changes.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import O4_UI_Utils as UI

from .layout import (
    ROLE_APRON,
    ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_SERVICE_JUNCTION,
    ROLE_STUB,
    ROLE_BUILDING,
)

if TYPE_CHECKING:
    from .layout import BuiltShape, PavementLayout


# Roles the per-surface solver grades — the only shapes whose geometry must
# be final before the solve.  Matches the refactor doc's invariant list.
_AIRSIDE_ROLES = frozenset({
    ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION, ROLE_SERVICE_JUNCTION,
    ROLE_APRON, ROLE_BUILDING,
})

# Round ring coords to this many metres when hashing, so float jitter from
# re-projecting identical geometry does not register as a change while a
# genuine weld/snap (≥ 1 mm) or vertex insert does.
_HASH_ROUND_M = 3

_ENABLED = os.environ.get("O4_GEOM_GUARD", "0") == "1"


def _canonical_ring(coords) -> tuple:
    """Rotation- and reflection-invariant canonical form of a ring's rounded
    vertices.  A ring's VERTEX SET + cyclic adjacency is the geometry; the
    starting vertex and winding direction are not — the solver / emit may
    rotate a rect's ring (e.g. to the [high, low, low, high] convention) when
    it assigns altitudes, which is NOT a geometry change.  Canonicalising by
    the lexicographically smallest rotation (over both directions) makes the
    guard immune to that re-ordering while still detecting a real insert /
    move / drop (which changes the vertex set or count)."""
    pts = [(round(x, _HASH_ROUND_M), round(y, _HASH_ROUND_M)) for x, y in coords]
    if pts and pts[0] == pts[-1]:
        pts = pts[:-1]
    n = len(pts)
    if n == 0:
        return ()
    best = None
    for seq in (pts, pts[::-1]):
        for i in range(n):
            rot = tuple(seq[i:] + seq[:i])
            if best is None or rot < best:
                best = rot
    return best


def _ring_hash(shape: BuiltShape) -> int:
    """Hash of a shape's 2-D ring geometry (exterior + holes), ignoring
    altitude AND ring start/winding (see :func:`_canonical_ring`).  Vertex
    count, set, and cyclic adjacency all contribute."""
    poly = shape.polygon
    if poly is None or poly.is_empty:
        return 0
    parts: list = []
    geoms = poly.geoms if poly.geom_type == "MultiPolygon" else (poly,)
    for g in geoms:
        rings = [g.exterior] + list(g.interiors)
        for ring in rings:
            parts.append(_canonical_ring(ring.coords))
        parts.append(None)  # geom separator
    return hash(tuple(parts))


def snapshot_airside_geometry(layout: PavementLayout) -> dict | None:
    """Stamp every airside shape with a unique token and record its ring
    hash.  Returns the ``{token: (role, ring_hash)}`` snapshot, or ``None``
    when the guard is disabled."""
    if not _ENABLED:
        return None
    snap: dict[int, tuple[str, int]] = {}
    for i, s in enumerate(layout.shapes):
        if s.role not in _AIRSIDE_ROLES:
            continue
        token = i
        s._geom_guard_token = token  # type: ignore[attr-defined]
        snap[token] = (s.role, _ring_hash(s))
    UI.vprint(1,
        f"  [geom-guard] snapshot: {len(snap)} airside shape(s) "
        f"recorded pre-solve.")
    return snap


def report_post_solve_changes(layout: PavementLayout, snapshot: dict | None,
                              icao: str) -> int:
    """Compare current airside geometry against the pre-solve snapshot and
    log how many airside shapes changed geometry post-solve (the metric the
    refactor drives to 0).  Returns that count.  No-op when disabled."""
    if not _ENABLED or snapshot is None:
        return 0

    seen: set[int] = set()
    changed_hash = 0           # same object, ring geometry mutated in place
    new_airside = 0            # airside shape created/replaced post-solve
    changed_by_role: dict[str, int] = {}

    def _bump(role: str) -> None:
        changed_by_role[role] = changed_by_role.get(role, 0) + 1

    for s in layout.shapes:
        if s.role not in _AIRSIDE_ROLES:
            continue
        token = getattr(s, "_geom_guard_token", None)
        if token is None or token not in snapshot:
            new_airside += 1
            _bump(f"{s.role}(new)")
            continue
        seen.add(token)
        old_role, old_hash = snapshot[token]
        if _ring_hash(s) != old_hash:
            changed_hash += 1
            _bump(s.role)

    # Tokens in the snapshot no longer present as airside shapes: dropped or
    # reclassified out of airside (a geometry/role change either way).
    removed = 0
    for token, (old_role, _h) in snapshot.items():
        if token not in seen:
            removed += 1
            _bump(f"{old_role}(removed)")

    total = changed_hash + new_airside + removed
    if total:
        detail = ", ".join(
            f"{role}:{n}" for role, n in sorted(changed_by_role.items()))
        UI.vprint(1,
            f"  [geom-guard] {icao}: {total} airside shape(s) changed "
            f"geometry POST-SOLVE "
            f"(mutated={changed_hash}, new={new_airside}, removed={removed}) "
            f"[{detail}]")
    else:
        UI.vprint(1,
            f"  [geom-guard] {icao}: 0 airside shapes changed geometry "
            f"post-solve — invariant HOLDS.")
    return total


# ── Coverage probe (env O4_COVERAGE_PROBE, debug aid) ────────────────
def coverage_probe(layout, tag: str) -> None:
    """Print which pavement shapes own each probe point, labelled ``tag``.

    ``O4_COVERAGE_PROBE="lat,lon;lat,lon"`` — call sites sprinkle this
    after each post-slice pipeline pass, so a point that LOSES its owner
    between two tags names the pass that deleted the coverage (the SPJC
    service-strip loss took a day to bisect by hand).  No-op without the
    env var; never raises.
    """
    spec = os.environ.get("O4_COVERAGE_PROBE")
    if not spec:
        return
    try:
        from shapely.geometry import Point
        from .layout import _projection
        to_m = _projection(layout.anchor)
        _ROLES = ("apron", "junction", "service_junction", "service_road",
                  "building", "groundside_pavement", "runway",
                  "runway_crossing", "terminal", "stub", "primary_parallel",
                  "secondary_parallel", "cross_connector")
        out = []
        for part in spec.split(";"):
            la, lo = (float(v) for v in part.split(","))
            x, y = to_m(lo, la)
            pt = Point(x, y)
            owners = [
                f"{s.role}#{i}"
                for i, s in enumerate(layout.shapes)
                if s.role in _ROLES and s.polygon is not None
                and not s.polygon.is_empty and s.polygon.contains(pt)]
            out.append(f"({la:.5f},{lo:.5f})→{owners or ['LOST']}")
        print(f"  [coverage-probe] {tag}: " + " | ".join(out))
    except Exception as _e:                          # pragma: no cover
        print(f"  [coverage-probe] {tag}: ERROR {_e!r}")


def insert_probe_nodes(layout, spec: str, radius_m: float = 10.0) -> int:
    """Insert DIAGNOSTIC ring vertices near probe points (user 2026-07-07).

    ``O4_PROBE_NODES="lat,lon;lat,lon"`` — for each probe point, every
    pavement ring EDGE passing within ``radius_m`` gains a vertex at the
    point's perpendicular projection, with the altitude LINEARLY
    INTERPOLATED along that edge — elevation-neutral by construction
    (the rendered surface is unchanged; the mesh merely gains a
    constraint there), but the emitted patch then carries an explicit
    node + alt_abs at the spot, so long node-free straightaways become
    verifiable in JOSM / in-sim (CYXY service-road "ridge" report: the
    nearest emitted vertices were 71-107 m away — nothing to inspect).

    Runs at the very END of the build (after decimation, projection,
    and skirts — a lerped point on a straight edge is exactly the
    3D-collinear class emit decimation removes, so it must be inserted
    after).  Both shapes sharing an edge get the same XY and the same
    lerp, so the emit-time consensus merges them into one node.
    No-op without the env var; never raises.
    """
    try:
        from shapely.geometry import Polygon as _Poly
        from .layout import _projection
        to_m = _projection(layout.anchor)
        pts = []
        for part in spec.split(";"):
            la, lo = (float(v) for v in part.split(","))
            pts.append(to_m(lo, la))
        n_inserted = 0
        for s in layout.shapes:
            poly = s.polygon
            if (poly is None or poly.is_empty
                    or poly.geom_type != "Polygon"):
                continue
            ring = list(poly.exterior.coords)      # closed
            alts = (list(s.node_altitudes)
                    if s.node_altitudes is not None else None)
            if alts is not None and len(alts) != len(ring):
                continue                            # malformed; skip
            insertions = []                         # (seg_idx, (x,y), alt)
            for (px, py) in pts:
                for i in range(len(ring) - 1):
                    ax, ay = ring[i]
                    bx, by = ring[i + 1]
                    ex, ey = bx - ax, by - ay
                    L2 = ex * ex + ey * ey
                    if L2 < 4.0:                    # short seg: has nodes
                        continue
                    t = ((px - ax) * ex + (py - ay) * ey) / L2
                    if not (0.05 < t < 0.95):       # off-end: node nearby
                        continue
                    qx, qy = ax + t * ex, ay + t * ey
                    dx, dy = px - qx, py - qy
                    if dx * dx + dy * dy > radius_m * radius_m:
                        continue
                    a = None
                    if alts is not None:
                        a = alts[i] + t * (alts[i + 1] - alts[i])
                    insertions.append((i, (qx, qy), a))
            if not insertions:
                continue
            for i, q, a in sorted(insertions, reverse=True):
                ring.insert(i + 1, q)
                if alts is not None:
                    alts.insert(i + 1, a)
            try:
                new_poly = _Poly(ring)
                if new_poly.is_empty or not new_poly.is_valid:
                    continue
            except Exception:
                continue
            s.polygon = new_poly
            if alts is not None:
                s.node_altitudes = alts
            n_inserted += len(insertions)
        if n_inserted:
            print(f"  [probe-nodes] inserted {n_inserted} diagnostic "
                  f"vertex(es) at {len(pts)} probe point(s).")
        return n_inserted
    except Exception as _e:                          # pragma: no cover
        print(f"  [probe-nodes] ERROR {_e!r}")
        return 0

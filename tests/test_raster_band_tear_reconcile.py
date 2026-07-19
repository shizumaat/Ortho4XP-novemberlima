"""Tier 3 wave 2b — adjacent-ground / raster-reach-band tear reconciliation.

When the rasterized reach band (``O4_RASTER_REACH_BAND``) clamps an apron or
junction ~2 m down to its tighter, CORRECT ceiling, the adjacent-ground graded
strips bridging the resulting step used to emit a sub-metre near-vertical TEAR
(the ``check_grade`` adjacent-ground sentinel).  Two mechanisms reconcile it:

  1. ``adjacent_ground._heal_emitted_band_tears`` collapses a WITHIN-strip
     pinch (a host-weld row and a terrain row pinched sub-metre apart) by
     dropping the terrain-side vertex — the ruled ``_heal_band_tears`` doctrine
     applied to the final emitted rings.
  2. ``layout.to_osm``'s nid-level weld twins a soft strip-vs-strip seam node
     whose foreign value would tear the receiving strip, so each strip keeps
     its own value at the shared coordinate (the mesh still welds by
     coordinate — no Ruppert sliver).

These are hermetic unit tests (no X-Plane data, no network).
"""
import math
import os
import re
import tempfile
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from auto_patch import adjacent_ground as AG
from auto_patch.layout import (
    BuiltShape, PavementLayout, ROLE_GRADED_STRIP, ROLE_JUNCTION)


class _FakeLayout:
    """Minimal ``layout`` for ``_heal_emitted_band_tears`` (needs ``.shapes``
    for the value-donor pavement exteriors)."""

    def __init__(self, shapes):
        self.shapes = shapes


def _open_ring(poly):
    return list(poly.exterior.coords)[:-1]


def _sub_metre_tears(ring, alts):
    """Sub-metre ring edges with a >1 m altitude jump (the tear sentinel)."""
    n = len(ring)
    out = []
    for i in range(n):
        j = (i + 1) % n
        d = math.hypot(ring[j][0] - ring[i][0], ring[j][1] - ring[i][1])
        de = abs(alts[i] - alts[j])
        if d < 1.0 and de > 1.0:
            out.append((i, j, d, de))
    return out


# ── 1. within-strip pinch heal ───────────────────────────────────────────

def test_within_strip_pinch_is_healed():
    """A strip welded to a (dropped) donor junction at 100 m with a terrain
    spike at 103 m pinched 0.6 m off the weld carries a >100 % tear; the final
    heal drops the terrain spike, leaving no sub-metre near-vertical edge."""
    junction = BuiltShape(
        polygon=Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
        role=ROLE_JUNCTION,
        node_altitudes=[100.0, 100.0, 100.0, 100.0, 100.0])
    # Strip below the junction: two weld vertices ON the junction's y=0 edge,
    # a terrain spike 0.6 m out at +3 m (the pinch), then a terrain vertex.
    ring = [(2.0, 0.0), (2.3, -0.5), (8.0, -3.0), (8.0, 0.0)]
    alts = [100.0, 103.0, 103.0, 100.0]
    strip = BuiltShape(
        polygon=Polygon(ring), role=ROLE_GRADED_STRIP, ref="adjacent_ground",
        node_altitudes=alts + [alts[0]])

    assert _sub_metre_tears(ring, alts), "fixture must start with a tear"
    healed = AG._heal_emitted_band_tears([strip], _FakeLayout([junction, strip]))
    assert healed == 1

    new_ring = _open_ring(strip.polygon)
    new_alts = list(strip.node_altitudes[:len(new_ring)])
    assert not _sub_metre_tears(new_ring, new_alts), (
        "the pinch survived the heal")
    # The weld vertices (on the donor junction) are preserved.
    assert any(math.hypot(x - 2.0, y) < 1e-6 for (x, y) in new_ring)
    assert any(math.hypot(x - 8.0, y) < 1e-6 for (x, y) in new_ring)


def test_heal_keeps_lawful_wide_terrain_edges():
    """A strip whose terrain edges span a full station (wide, lawfully riding a
    hillside) is NOT collapsed — only the sub-metre pinch class is."""
    ring = [(0.0, 0.0), (6.0, -1.0), (6.0, -6.0), (0.0, -6.0)]
    alts = [100.0, 104.0, 104.0, 100.0]      # 4 m over 6 m edges — lawful
    strip = BuiltShape(
        polygon=Polygon(ring), role=ROLE_GRADED_STRIP, ref="adjacent_ground",
        node_altitudes=alts + [alts[0]])
    healed = AG._heal_emitted_band_tears([strip], _FakeLayout([strip]))
    assert healed == 0
    assert _open_ring(strip.polygon) == ring


# ── 2. gate flag ─────────────────────────────────────────────────────────

def test_raster_reach_band_active_respects_env(monkeypatch):
    monkeypatch.setenv("O4_RASTER_REACH_BAND", "1")
    assert AG._raster_reach_band_active() is True
    monkeypatch.setenv("O4_RASTER_REACH_BAND", "0")
    assert AG._raster_reach_band_active() is False


# ── 3. cross-strip seam twin (to_osm) ────────────────────────────────────

def _emit_two_strips_and_read():
    """Two graded strips sharing the y=0 seam at references 3 m apart, each
    with a vertex landing mid-edge on the other (the weld-splice site).  Emit
    and return, per strip way, its ``[(x, y, alt)]`` ring in local metres."""
    layout = PavementLayout(icao="KFAKE", anchor=(40.0, -100.0))
    # Strip A (below, flat 100 m); its top edge (3,0)->(10,0) receives B's
    # (3.5,0) vertex 0.5 m from A's own (3,0) vertex.
    a_ring = [(0.0, 0.0), (3.0, 0.0), (10.0, 0.0), (10.0, -3.0), (0.0, -3.0)]
    a_alt = [100.0] * len(a_ring)
    layout.shapes.append(BuiltShape(
        polygon=Polygon(a_ring), role=ROLE_GRADED_STRIP, ref="adjacent_ground",
        node_altitudes=a_alt + [a_alt[0]]))
    # Strip B (above, flat 103 m); its top edge (0,0)->(3.5,0) receives A's
    # (3,0) vertex.
    b_ring = [(0.0, 0.0), (3.5, 0.0), (10.0, 0.0), (10.0, 3.0), (0.0, 3.0)]
    b_alt = [103.0] * len(b_ring)
    layout.shapes.append(BuiltShape(
        polygon=Polygon(b_ring), role=ROLE_GRADED_STRIP, ref="adjacent_ground",
        node_altitudes=b_alt + [b_alt[0]]))

    with tempfile.NamedTemporaryFile(suffix=".osm", delete=False) as f:
        path = f.name
    try:
        layout.to_osm(path)
        text = Path(path).read_text()
    finally:
        Path(path).unlink()

    nodes, alt = {}, {}
    for m in re.finditer(
            r"<node id='(-?\d+)'[^>]*lat='([^']+)' lon='([^']+)'", text):
        nodes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
    for m in re.finditer(
            r"<node id='(-?\d+)'[^>]*?>\s*<tag k='alt_abs' v='([^']+)'",
            text, re.DOTALL):
        alt[int(m.group(1))] = float(m.group(2))
    lat0, lon0 = 40.0, -100.0
    R = 6378137.0
    cos0 = math.cos(math.radians(lat0))

    def to_m(la, lo):
        return (math.radians(lo - lon0) * R * cos0,
                math.radians(la - lat0) * R)

    strips = []
    way_blocks = re.findall(r"<way id='-?\d+'[^>]*>(.*?)</way>", text,
                            flags=re.DOTALL)
    for body in way_blocks:
        tags = dict(re.findall(r"<tag k='([^']+)' v='([^']+)'", body))
        if tags.get("ref") != "adjacent_ground":
            continue
        nds = [int(x) for x in re.findall(r"<nd ref='(-?\d+)'", body)]
        ring = [(*to_m(*nodes[n]), alt.get(n)) for n in nds if n in nodes]
        strips.append(ring)
    return strips


def _way_has_tear(ring_xyz):
    for i in range(len(ring_xyz) - 1):
        xa, ya, ea = ring_xyz[i]
        xb, yb, eb = ring_xyz[i + 1]
        if ea is None or eb is None:
            continue
        d = math.hypot(xb - xa, yb - ya)
        if d < 1.0 and abs(ea - eb) > 1.0:
            return True
    return False


def test_cross_strip_seam_hard_merge_no_stacked_nodes():
    """NO-STACKED-NODES (owner ruling 2026-07-19): the former nid-level
    value twin is GONE in every gate state — ``to_osm`` hard-merges each
    coincident claim into ONE node with ONE consensus value.  The bare
    emit of two conflicting flat strips therefore carries the splice
    step as a within-strip edge (the pipeline resolves it upstream via
    the seam blend + ``emit_stacked_conflict_walls``, exercised below);
    what to_osm itself must guarantee is the structural invariant: no
    two node ids at one coordinate with different values."""
    strips = _emit_two_strips_and_read()
    assert strips, "no adjacent_ground ways emitted"
    # Rebuild the node table from the emit and scan for stacked pairs.
    seen: dict = {}
    for ring in strips:
        for (x, y, e) in ring:
            if e is None:
                continue
            key = (round(x, 2), round(y, 2))
            for other in seen.get(key, ()):  # values at this coordinate
                assert abs(other - e) <= 0.05, (
                    f"stacked values {other} vs {e} at {key}")
            seen.setdefault(key, []).append(e)


def test_stacked_conflict_walls_resolve_two_strip_step():
    """The ruling's resolution for an all-anchored strip-vs-strip level
    change: the LOWER strip retreats and a ``retaining_wall`` face
    spans the vacated band (top row on the upper chain, bottom row on
    the retreated edge) — the level change survives as deliberate,
    horizontally-extended geometry and neither strip tears."""
    from auto_patch.adjacent_ground import emit_stacked_conflict_walls
    from auto_patch.canonical_points import CanonicalPointRegistry
    from auto_patch.layout import ROLE_RETAINING_WALL, SHARED_VERTEX_TOL_M

    # Anchor OFF the integer lat/lon lines: the wall pass rightly
    # refuses to move tile-seam-band vertices (cross-tile contract),
    # and an integer-line anchor would put the whole synthetic seam
    # chain inside that band.
    layout = PavementLayout(icao="KFAKE", anchor=(40.37, -100.21))
    a_ring = [(0.0, 0.0), (3.0, 0.0), (10.0, 0.0), (10.0, -3.0), (0.0, -3.0)]
    a_alt = [100.0] * len(a_ring)
    layout.shapes.append(BuiltShape(
        polygon=Polygon(a_ring), role=ROLE_GRADED_STRIP,
        ref="adjacent_ground", node_altitudes=a_alt + [a_alt[0]]))
    b_ring = [(0.0, 0.0), (3.5, 0.0), (10.0, 0.0), (10.0, 3.0), (0.0, 3.0)]
    b_alt = [103.0] * len(b_ring)
    layout.shapes.append(BuiltShape(
        polygon=Polygon(b_ring), role=ROLE_GRADED_STRIP,
        ref="adjacent_ground", node_altitudes=b_alt + [b_alt[0]]))
    registry = CanonicalPointRegistry(tol_m=SHARED_VERTEX_TOL_M)
    for s in layout.shapes:
        for (x, y) in list(s.polygon.exterior.coords):
            registry.get_or_add(float(x), float(y))
    layout.canonical_points = registry

    emitted = emit_stacked_conflict_walls(layout)
    assert emitted >= 1, "expected a retaining_wall face for the 3 m step"
    walls = [s for s in layout.shapes if s.role == ROLE_RETAINING_WALL]
    assert walls and all(
        s.ref == "stacked_conflict_wall" for s in walls)
    # The LOWER strip (A, 100 m) retreated: its seam-row vertices moved
    # off y=0; the upper strip (B, 103 m) keeps its chain.
    strip_a = layout.shapes[0]
    a_seam_ys = [y for (x, y) in
                 list(strip_a.polygon.exterior.coords)[:-1]
                 if abs(y) < 1e-6]
    assert not a_seam_ys, "lower strip still touches the seam line"
    strip_b = layout.shapes[1]
    assert any(abs(y) < 1e-6 for (x, y) in
               list(strip_b.polygon.exterior.coords)[:-1])
    # Wall spans: top row on y=0 at 103, bottom row retreated at 100.
    wall = walls[0]
    wall_ring = list(wall.polygon.exterior.coords)[:-1]
    wall_alts = wall.node_altitudes[:len(wall_ring)]
    assert any(abs(y) < 1e-6 and abs(a - 103.0) < 0.6
               for (x, y), a in zip(wall_ring, wall_alts))
    assert any(abs(y) > 1e-6 and abs(a - 100.0) < 0.6
               for (x, y), a in zip(wall_ring, wall_alts))
    # Neither strip carries a sub-metre near-vertical edge afterwards.
    for s in (strip_a, strip_b):
        ring = list(s.polygon.exterior.coords)[:-1]
        alts = s.node_altitudes[:len(ring)]
        assert not _way_has_tear(
            [(x, y, a) for (x, y), a in zip(ring, alts)])

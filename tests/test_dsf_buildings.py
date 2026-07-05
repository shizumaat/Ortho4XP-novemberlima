"""DSF building-facade reading + term_bridge grouping (20260614-02).

Covers:
* ``_building_role_for_def`` — terminal / hangar / bridge / None.
* the depth-5 facade bezier parse fix: a curved ``term_building_*.fac``
  is authored as ``(lon, lat, wall_param, ctrl_lon, ctrl_lat)`` per
  point; reading a fixed ``tok[3],tok[4]`` grabbed the wall param as the
  control lon (≈ a small integer), exploding the ring to continental
  scale.  The fix reads the LAST TWO planes, keeping the ring local.
* ``_cluster_dsf_building_facades`` connectors: a term_bridge joining two
  building footprints unions them into ONE pad; a bridge linking nothing
  is dropped; the no-connector path is unchanged.
"""
import os

from shapely.geometry import Polygon

import auto_patch.dsf_reader as D
from auto_patch.dsf_reader import _building_role_for_def
from auto_patch.terminals import _cluster_dsf_building_facades


def test_building_role_for_def():
    base = "lib/airport/Modern_Airports/Terminal_kit/"
    assert _building_role_for_def(base + "term_building_Ground_01.fac") \
        == "terminal"
    assert _building_role_for_def(base + "term_bridge_01.fac") == "bridge"
    assert _building_role_for_def(base + "term_roof_level_01.fac") is None
    assert _building_role_for_def(
        "lib/airport/Common_Elements/Hangars/Blue_Hangar.fac") == "hangar"
    # Non-facade resources never classify, even with a matching substring.
    assert _building_role_for_def("foo/term_building_Ground.pol") is None
    assert _building_role_for_def("foo/term_bridge_thing.obj") is None


def _write_fake_dsf(tmp_path, body):
    """Create a fake .dsf + a fresh .dsf.text cache so ``_read_dsf_polys``
    parses our synthetic text without invoking DSFTool."""
    dsf = tmp_path / "fake.dsf"
    dsf.write_text("binary-placeholder")
    txt = tmp_path / "fake.dsf.text"
    txt.write_text(body)
    # Ensure the cache is NEWER than the dsf so no re-conversion is tried.
    now = os.path.getmtime(txt)
    os.utime(dsf, (now - 10, now - 10))
    return str(dsf)


_DEF = ("POLYGON_DEF lib/airport/Modern_Airports/Terminal_kit/"
        "term_building_Ground_01.fac\n")


def test_depth5_facade_ring_stays_local(tmp_path, monkeypatch):
    # Pretend DSFTool exists (we pre-seed the .text cache, so it is never
    # actually run).
    monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
    # A small square facade authored at depth 5: each point is
    # (lon, lat, wall_param, ctrl_lon, ctrl_lat).  All corners here are
    # plain (ctrl == anchor), wall_param = 3.  The buggy reader would
    # treat (3.0, lon) as the bezier handle and blow the ring up to
    # lon ≈ 3.
    pts = [(31.4000, 30.1000), (31.4010, 30.1000),
           (31.4010, 30.1010), (31.4000, 30.1010)]
    lines = ["BEGIN_POLYGON 0 6 5", "BEGIN_WINDING"]
    for lon, lat in pts:
        lines.append(
            f"POLYGON_POINT {lon:.6f} {lat:.6f} 3.000000 "
            f"{lon:.6f} {lat:.6f}")
    lines += ["END_WINDING", "END_POLYGON"]
    body = _DEF + "\n".join(lines) + "\n"
    dsf = _write_fake_dsf(tmp_path, body)

    polys = D._read_dsf_polys(
        dsf, lambda p: p.lower().endswith(".fac"), cache_dir=str(tmp_path))
    assert len(polys) == 1
    outer, holes, path = polys[0]
    lons = [lon for lon, _ in outer]
    lats = [lat for _, lat in outer]
    # The ring must stay inside the authored ~0.001° box — NOT explode to
    # lon ≈ 3 (the wall param) as the pre-fix reader did.
    assert min(lons) >= 31.39 and max(lons) <= 31.41
    assert min(lats) >= 30.09 and max(lats) <= 30.11


def test_depth5_facade_bezier_is_curved(tmp_path, monkeypatch):
    # A depth-5 point whose ctrl differs from the anchor is a real bezier
    # handle (cols 4-5) and must produce extra interpolated vertices.
    monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
    pts = [
        # (lon, lat, param, ctrl_lon, ctrl_lat)
        (31.4000, 30.1000, 0.0, 31.4000, 30.1000),   # corner
        (31.4010, 30.1000, 0.0, 31.4014, 30.1004),   # handle near anchor
        (31.4010, 30.1010, 0.0, 31.4010, 30.1010),   # corner
        (31.4000, 30.1010, 0.0, 31.4000, 30.1010),   # corner
    ]
    lines = ["BEGIN_POLYGON 0 6 5", "BEGIN_WINDING"]
    for lon, lat, p, cl, ca in pts:
        lines.append(
            f"POLYGON_POINT {lon:.6f} {lat:.6f} {p:.6f} {cl:.6f} {ca:.6f}")
    lines += ["END_WINDING", "END_POLYGON"]
    body = _DEF + "\n".join(lines) + "\n"
    dsf = _write_fake_dsf(tmp_path, body)
    outer, _, _ = D._read_dsf_polys(
        dsf, lambda p: p.lower().endswith(".fac"),
        cache_dir=str(tmp_path))[0]
    # More than the 4 raw corners (the curved edge was tessellated), and
    # still local.
    assert len(outer) > 4
    lons = [lon for lon, _ in outer]
    assert min(lons) >= 31.39 and max(lons) <= 31.41


def _sq(x0, y0, w, h):
    return Polygon([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h),
                    (x0, y0 + h)])


def test_cluster_bridge_merges_two_buildings():
    # Two 20x20 buildings, 10 m apart — comfortably past the proximity-merge
    # reach (2 × DSF_FACADE_MERGE_GAP_M = 4 m; the old 4 m gap sat EXACTLY on
    # that threshold and flapped with the buffer arithmetic).  A bridge slab
    # spanning the gap is admitted into the SAME facade pool (the caller's
    # gate decides) and unions the run into one flat group.
    a = _sq(0, 0, 20, 20)
    b = _sq(30, 0, 20, 20)
    bridge = _sq(19, 8, 12, 4)   # spans the 10 m gap, overlaps both
    assert len(_cluster_dsf_building_facades([a, b])) == 2     # gap → 2
    assert len(_cluster_dsf_building_facades([a, b, bridge])) == 1


def test_cluster_freestanding_bridge_slab_is_a_pad():
    # A term_bridge slab that IS the concourse floor (no abutting
    # term_building) is still a real building component → its own pad
    # (≥ min_area).  Complex buildings union ALL their facade classes.
    a = _sq(0, 0, 20, 20)
    slab = _sq(200, 200, 40, 40)   # 1600 m², free-standing
    out = _cluster_dsf_building_facades([a, slab])
    assert len(out) == 2


def test_cluster_separate_buildings_unchanged():
    a = _sq(0, 0, 20, 20)
    b = _sq(100, 0, 20, 20)
    assert len(_cluster_dsf_building_facades([a, b])) == 2


from shapely.ops import unary_union as _uunion

from auto_patch.terminals import _close_building_outline
from auto_patch.config import BUILDING_OUTLINE_FILL_GATE_M as _GATE


def _fill_ratio(p):
    return p.area / p.convex_hull.area


def _comb(gap, depth=240, n=4, tooth=30):
    # Spine + ``n`` deep fingers separated by ``gap`` m — a stylised
    # finger-pier terminal with a low (deeply-concave) fill-ratio.
    spine = _sq(0, 0, (n - 1) * gap + n * tooth, 20)
    parts = [spine]
    x = 0
    for _ in range(n):
        parts.append(_sq(x, 20, tooth, depth))
        x += tooth + gap
    return _uunion(parts)


def test_close_absorbs_narrow_stands():
    # A deep comb (gaps 80 m < 2×GATE) is narrow-filled: the stands fill in
    # out to the tooth tips → one simpler, more solid pad.
    comb = _comb(80)
    out = _close_building_outline(comb)
    assert len(out) == 1
    closed = out[0]
    assert closed.area > comb.area
    assert _fill_ratio(closed) > _fill_ratio(comb)


def test_close_noop_on_convex():
    sq = _sq(0, 0, 200, 200)
    out = _close_building_outline(sq)
    assert len(out) == 1 and abs(out[0].area - sq.area) < 1.0


def test_close_preserves_wide_concavity():
    # A wide U (opening WIDER than 2×GATE) keeps its concavity — narrow-fill
    # bridges only the narrow stands, never the genuine reentrant shape.
    wide = 4 * _GATE + 60                 # > 2×GATE
    u = _sq(0, 0, wide + 100, 300).difference(_sq(50, 90, wide, 300))
    out = _close_building_outline(u)
    assert len(out) == 1
    assert _fill_ratio(out[0]) < 0.95


def test_close_fills_teeth_but_not_wide_centre():
    # The HECA case: a finger comb whose tooth gaps are NARROW (absorbed)
    # but whose open centre is WIDE (preserved).  Two outer-toothed piers
    # joined by a spine, with a wide gap between them.  Narrow-fill must
    # grow the area (teeth absorbed) yet stay well below the convex hull
    # (the wide centre is NOT bridged).
    centre_gap = 6 * _GATE                # >> 2×GATE → stays open
    pier_w, pier_h, tooth = 30.0, 240.0, 25.0
    left = _comb(40, depth=pier_h, n=1, tooth=pier_w)   # toothless pier core
    # left pier with outer teeth
    parts = [_sq(0, 0, pier_w, pier_h)]
    for k in range(4):
        parts.append(_sq(-tooth, 10 + k * 60, tooth, 25))   # teeth to the left
    rx = pier_w + centre_gap
    parts.append(_sq(rx, 0, pier_w, pier_h))
    for k in range(4):
        parts.append(_sq(rx + pier_w, 10 + k * 60, tooth, 25))  # teeth right
    parts.append(_sq(0, pier_h, rx + pier_w, 20))           # top spine
    comb = _uunion(parts)
    out = _close_building_outline(comb)
    merged = _uunion(out)
    assert merged.area > comb.area                  # teeth absorbed
    assert merged.area < 0.85 * comb.convex_hull.area   # centre NOT filled

"""Acceptance tests for ``O4_Default_Terrain_Map.DefaultTerrainMap``
(texture-mode feature, work package 1 — ``docs/specs/texture-mode-spec.md``
section 4.1 / 5).

All tests are headless: no network, no X-Plane install, no live DSFTool.
The parse path is exercised over hand-written DSFTool-text fixtures via a
fake ``.dsf`` + pre-seeded, mtime-backdated ``.dsf.text`` sidecar (the same
harness pattern ``tests/test_dsf_object_buildings.py`` uses), with
``dsf_reader._dsftool_path`` monkeypatched so no real binary is invoked.
"""
import os

import pytest

import O4_Default_Terrain_Map as DTM
import O4_Overlay_Utils as OVL
from O4_Default_Terrain_Map import DefaultTerrainMap, _primitive_triangles
from auto_patch import dsf_reader as D

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SYNTHETIC_TERRAIN = os.path.join(FIXTURE_DIR, "synthetic_default_terrain.txt")


def _signed_area(tri) -> float:
    (x0, y0), (x1, y1), (x2, y2) = tri
    return 0.5 * ((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))


# ── sanity: importing the module under test resolves to the worktree ────

def test_module_resolves_to_worktree():
    """Guard against conftest/sys.path leaking the main checkout's src."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.abspath(DTM.__file__).startswith(here), (
        f"O4_Default_Terrain_Map imported from {DTM.__file__!r}, "
        f"expected under the worktree {here!r}")


# ── primitive expansion (known winding) ─────────────────────────────────

def test_independent_triangles_expansion():
    verts = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0),
             (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    tris = _primitive_triangles(0, verts)
    assert tris == [
        ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        ((1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
    ]


def test_strip_expansion_known_winding():
    verts = [(1.0, 0.0), (1.0, 1.0), (2.0, 0.0), (2.0, 1.0)]
    tris = _primitive_triangles(1, verts)
    # Strip triangle N = (N, N+1, N+2), with the first two vertices swapped
    # on odd N so every triangle winds the same way.
    assert tris == [
        ((1.0, 0.0), (1.0, 1.0), (2.0, 0.0)),
        ((2.0, 0.0), (1.0, 1.0), (2.0, 1.0)),
    ]
    signs = [_signed_area(t) for t in tris]
    assert all(s < 0 for s in signs) or all(s > 0 for s in signs), (
        "strip triangles must share a consistent winding")


def test_fan_expansion():
    verts = [(0.5, 1.5), (0.0, 1.0), (1.0, 1.0), (1.0, 2.0), (0.0, 2.0)]
    tris = _primitive_triangles(2, verts)
    assert tris == [
        ((0.5, 1.5), (0.0, 1.0), (1.0, 1.0)),
        ((0.5, 1.5), (1.0, 1.0), (1.0, 2.0)),
        ((0.5, 1.5), (1.0, 2.0), (0.0, 2.0)),
    ]


def test_degenerate_triangles_dropped():
    # A strip that collapses to a line yields no triangles.
    verts = [(0.0, 0.0), (0.0, 0.0), (1.0, 0.0)]
    assert _primitive_triangles(1, verts) == []


# ── full parse over the synthetic fixture ───────────────────────────────

@pytest.fixture
def synthetic_map(tmp_path, monkeypatch):
    """Build a ``DefaultTerrainMap`` from the synthetic fixture through the
    public ``from_dsf`` entry point, using a fake DSF whose ``.dsf.text``
    sidecar is pre-seeded so DSFTool is never run."""
    pack = tmp_path / "X-Plane 12 Global Scenery"
    end = pack / "Earth nav data" / "+00+000"
    end.mkdir(parents=True)
    dsf = end / "+00+000.dsf"
    dsf.write_bytes(b"7z-fake-dsf")
    text = end / "+00+000.dsf.text"
    with open(SYNTHETIC_TERRAIN, "r", encoding="utf-8") as src:
        text.write_text(src.read())
    # Backdate the DSF so ensure_dsf_text_path treats the sidecar as fresh.
    now = os.path.getmtime(text)
    os.utime(str(dsf), (now - 100, now - 100))
    # A binary need not exist; the tool-present check just must pass.
    monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
    # from_dsf now caches dumps under Default_dsf_cache_dir; point it at the
    # directory where this harness pre-seeded the .dsf.text sidecar.
    monkeypatch.setattr(DTM.FNAMES, "Default_dsf_cache_dir", str(end))
    # Clear any in-process line cache from other tests.
    D._DSF_LINES_CACHE.clear()
    return DefaultTerrainMap.from_dsf(str(dsf))


def test_terrain_table_index_order(synthetic_map):
    assert synthetic_map.terrain_paths == [
        "lib/g10/terrain10/grass_tmp_flat.ter",
        "lib/g10/terrain10/asphalt.ter",
        "lib/g10/terrain10/rock.ter",
    ]


def test_physical_only_triangle_count(synthetic_map):
    # 2 (independent) + 2 (strip) + 3 (fan) physical triangles; the overlay
    # patch (flag 2) contributes none.
    assert len(synthetic_map._triangles) == 7
    # No triangle should carry an overlay vertex (lon >= 5).
    for tri in synthetic_map._triangles:
        assert all(lon < 5 for (lon, _lat) in tri)


def test_terrain_index_at_interior_points(synthetic_map):
    # asphalt square lon[0,1] lat[0,1] -> index 1
    assert synthetic_map.terrain_index_at(0.5, 0.3) == 1
    # rock strip square lon[1,2] lat[0,1] -> index 2
    assert synthetic_map.terrain_index_at(1.5, 0.5) == 2
    # grass fan region lon[0,1] lat[1,2] -> index 0
    assert synthetic_map.terrain_index_at(0.5, 1.2) == 0


def test_terrain_path_at_interior(synthetic_map):
    assert synthetic_map.terrain_path_at(1.5, 0.5) == \
        "lib/g10/terrain10/rock.ter"


def test_terrain_index_at_shared_edge(synthetic_map):
    # The lon=1 line is shared between the asphalt square (index 1) and the
    # rock strip (index 2); any incident triangle is acceptable.
    assert synthetic_map.terrain_index_at(1.0, 0.5) in (1, 2)


def test_terrain_index_at_nearest_fallback(synthetic_map):
    # A point far outside all coverage falls back to the nearest triangle
    # (a valid terrain index), rather than raising.
    idx = synthetic_map.terrain_index_at(10.0, 10.0)
    assert idx in (0, 1, 2)


def test_overlay_region_not_contained(synthetic_map):
    # A point inside the ignored overlay patch resolves only by nearest
    # fallback -- no physical triangle actually covers it.
    from shapely.geometry import Point
    hits = synthetic_map._tree.query(Point(5.4, 5.2), predicate="intersects")
    assert len(hits) == 0
    # But the lookup still returns a valid physical terrain index.
    assert synthetic_map.terrain_index_at(5.4, 5.2) in (0, 1, 2)


# ── is_projected ────────────────────────────────────────────────────────

def test_is_projected_none_when_unresolvable(synthetic_map):
    # tmp_path pack sits under ".../X-Plane 12 Global Scenery" whose parent
    # is NOT a recognised scenery container, and no pack-relative .ter
    # exists, so projection state is unknowable -> None.
    assert synthetic_map.is_projected(1) is None
    # Out-of-range index -> None as well.
    assert synthetic_map.is_projected(99) is None


def test_is_projected_reads_pack_relative_ter(tmp_path, monkeypatch):
    """When a pack-relative ``.ter`` resolves, its ``PROJECTED`` token is
    read.  Exercises resolution without a real X-Plane install."""
    pack = tmp_path / "Custom Scenery" / "zzz_pack"
    end = pack / "Earth nav data" / "+00+000"
    end.mkdir(parents=True)
    dsf = end / "+00+000.dsf"
    dsf.write_bytes(b"7z-fake-dsf")
    text = end / "+00+000.dsf.text"
    with open(SYNTHETIC_TERRAIN, "r", encoding="utf-8") as src:
        text.write_text(src.read())
    now = os.path.getmtime(text)
    os.utime(str(dsf), (now - 100, now - 100))
    monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
    # from_dsf now caches dumps under Default_dsf_cache_dir; point it at the
    # directory where this harness pre-seeded the .dsf.text sidecar.
    monkeypatch.setattr(DTM.FNAMES, "Default_dsf_cache_dir", str(end))
    D._DSF_LINES_CACHE.clear()

    # Lay down pack-relative .ter resources matching the terrain paths.
    ter_dir = pack / "lib" / "g10" / "terrain10"
    ter_dir.mkdir(parents=True)
    (ter_dir / "asphalt.ter").write_text("A\n800\nTEXTURE foo.png\nPROJECTED\n")
    (ter_dir / "rock.ter").write_text("A\n800\nTEXTURE bar.png\n")

    terrain_map = DefaultTerrainMap.from_dsf(str(dsf))
    assert terrain_map.is_projected(1) is True   # asphalt -> PROJECTED
    assert terrain_map.is_projected(2) is False  # rock -> no PROJECTED
    # grass (index 0) has no .ter file on disk -> unresolvable -> None.
    assert terrain_map.is_projected(0) is None


# ── from_tile soft failure ──────────────────────────────────────────────

def test_from_tile_returns_none_when_no_dsf(tmp_path, monkeypatch):
    empty = tmp_path / "no_scenery_here"
    empty.mkdir()
    monkeypatch.setattr(OVL, "custom_overlay_src", str(empty))
    monkeypatch.setattr(OVL, "custom_overlay_src_alternate", "")
    assert DefaultTerrainMap.from_tile(0, 0) is None


def test_from_tile_returns_none_when_src_unset(monkeypatch):
    monkeypatch.setattr(OVL, "custom_overlay_src", "")
    monkeypatch.setattr(OVL, "custom_overlay_src_alternate", "")
    assert DefaultTerrainMap.from_tile(45, -122) is None


# ── optional end-to-end over work-package-0's fixture, if present ────────

_WP0_FIXTURE = os.path.join(FIXTURE_DIR, "default_dsf_excerpt.txt")


@pytest.mark.skipif(
    not os.path.isfile(_WP0_FIXTURE),
    reason="work package 0 fixture tests/fixtures/default_dsf_excerpt.txt "
           "not present")
def test_parse_work_package_0_excerpt(tmp_path, monkeypatch):
    pack = tmp_path / "X-Plane 12 Global Scenery"
    end = pack / "Earth nav data" / "+00+000"
    end.mkdir(parents=True)
    dsf = end / "+00+000.dsf"
    dsf.write_bytes(b"7z-fake-dsf")
    text = end / "+00+000.dsf.text"
    with open(_WP0_FIXTURE, "r", encoding="utf-8") as src:
        text.write_text(src.read())
    now = os.path.getmtime(text)
    os.utime(str(dsf), (now - 100, now - 100))
    monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
    # from_dsf now caches dumps under Default_dsf_cache_dir; point it at the
    # directory where this harness pre-seeded the .dsf.text sidecar.
    monkeypatch.setattr(DTM.FNAMES, "Default_dsf_cache_dir", str(end))
    D._DSF_LINES_CACHE.clear()

    terrain_map = DefaultTerrainMap.from_dsf(str(dsf))
    # A real excerpt should yield a non-empty terrain table and at least one
    # physical triangle, and every lookup must return a valid index.
    assert terrain_map.terrain_paths
    assert terrain_map._triangles
    (lon0, lat0), _b, _c = terrain_map._triangles[0]
    idx = terrain_map.terrain_index_at(lon0, lat0)
    assert 0 <= idx < len(terrain_map.terrain_paths)

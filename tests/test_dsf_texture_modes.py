"""Acceptance tests for the ``default_xplane`` texture mode in the DSF writer
(texture-mode feature, work package 2 -- ``docs/specs/texture-mode-spec.md``
sections 4.2 / 5).

All tests are headless: no network, no live X-Plane install.  ``build_dsf`` is
driven directly against a hand-written tiny synthetic ``.mesh`` fixture and a
stub tile object carrying exactly the attributes the writer reads.  The
default-terrain lookup is a synthetic :class:`DefaultTerrainMap` built in-test
(``from_tile`` monkeypatched), and ``extract_elevation_and_bathymetry_data`` is
stubbed so no real Global Scenery DSF is touched.

The emitted DSF is decoded with ``tools/decode_dsf_terrain_table.py`` (which
shells out to the bundled DSFTool); those assertions ``skipif`` when the
DSFTool binary is absent.
"""
import os
import queue
import sys

import pytest

from shapely.geometry import MultiPolygon, Polygon

import O4_Airport_Fade_Masks as FADE
import O4_DSF_Utils as DSF
import O4_Default_Terrain_Map as DTM
import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO

# Make tools/ importable for the decode helper.
_TOOLS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "tools"))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import decode_dsf_terrain_table as DECODE  # noqa: E402

_DSFTOOL = pytest.mark.skipif(
    not DECODE.dsftool_available(),
    reason="bundled DSFTool binary not present")


# ── module-resolves-to-worktree guard ───────────────────────────────────

def test_module_resolves_to_worktree():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.abspath(DSF.__file__).startswith(here), (
        f"O4_DSF_Utils imported from {DSF.__file__!r}, expected under the "
        f"worktree {here!r}")


# ── synthetic-tile harness ──────────────────────────────────────────────

_MESH_HEADER = """MeshVersionFormatted 2
Dimension 3

Vertices
{nbr_nodes}
{vertices}

Normals
{nbr_nodes}
{normals}

Triangles
{nbr_tris}
{triangles}
"""


def _write_mesh(build_dir, vertices, triangles):
    """Write a minimal Triangle4XP-style ``.mesh`` file.

    ``vertices`` is a list of ``(lon, lat)`` (elevation is fixed tiny);
    ``triangles`` a list of ``(i, j, k, attr)`` with 1-based node indices and
    the raw attribute column (0 = land, 2 = sea, per ``read_mesh_file`` +
    ``build_dsf``'s remap).
    """
    vlines = "\n".join(
        f"{lon:.9f} {lat:.9f} 0.001000000 0" for (lon, lat) in vertices)
    nlines = "\n".join("0.00 0.00 0" for _ in vertices)
    tlines = "\n".join(
        f"{i} {j} {k} {attr}" for (i, j, k, attr) in triangles)
    text = _MESH_HEADER.format(
        nbr_nodes=len(vertices), vertices=vlines, normals=nlines,
        nbr_tris=len(triangles), triangles=tlines)
    mesh_path = FNAMES.mesh_file(build_dir, 50, 10)
    os.makedirs(os.path.dirname(mesh_path), exist_ok=True)
    with open(mesh_path, "w") as handle:
        handle.write(text)
    return mesh_path


def _make_tile(build_dir, texture_mode):
    """A stub tile carrying exactly the attributes ``build_dsf`` reads."""
    tile = type("Tile", (), {})()
    tile.lat = 50
    tile.lon = 10
    tile.build_dir = build_dir
    tile.mesh_zl = 17
    tile.default_zl = 16
    tile.default_website = "BI"
    tile.cover_zl = 18
    tile.cover_extent = 1
    tile.cover_airports_with_highres = "False"
    tile.zone_list = []
    tile.use_masks_for_inland = False
    tile.water_tech = "XP12"
    tile.normal_map_strength = 1
    tile.imprint_masks_to_dds = False
    tile.ratio_water = 0.3
    tile.ratio_bathy = 1
    tile.terrain_casts_shadows = True
    tile.use_decal_on_terrain = False
    tile.overlay_lod = 40000
    tile.mask_zl = 14
    tile.texture_mode = texture_mode
    tile.airport_ortho_fade_width = 1000.0
    tile.grouped = False
    return tile


def _prepare_build_dir(tmp_path):
    build_dir = str(tmp_path / "build")
    os.makedirs(os.path.join(build_dir, "Earth nav data", "+50+010"))
    os.makedirs(os.path.join(build_dir, "textures"))
    return build_dir


def _emitted_dsf(build_dir):
    tmp = os.path.join(
        build_dir, "Earth nav data", "+50+010", "+50+010.dsf.tmp")
    assert os.path.isfile(tmp), "build_dsf did not emit a DSF"
    return tmp


# Land square lon[10.1,10.3] lat[10.1,10.3] as two triangles, plus a
# disjoint sea triangle -- the two land triangles straddle the lon=10.2 split
# so they pick up two different default terrains.
_LAND_AND_SEA_VERTS = [
    (10.10, 50.10), (10.30, 50.10), (10.30, 50.30), (10.10, 50.30),
    (10.60, 50.60), (10.90, 50.60), (10.75, 50.90),
]
_LAND_AND_SEA_TRIS = [
    (1, 2, 3, 0),   # land, centroid lon ~10.23 -> asphalt
    (1, 3, 4, 0),   # land, centroid lon ~10.17 -> grass
    (5, 6, 7, 2),   # sea
]


def _split_terrain_map(paths=("lib/g10/terrain10/grass.ter",
                              "lib/g10/terrain10/asphalt.ter")):
    """A DefaultTerrainMap: grass west of lon 10.2, asphalt east of it."""
    tris = [
        ((10.0, 50.0), (10.2, 50.0), (10.2, 50.5)),
        ((10.0, 50.0), (10.2, 50.5), (10.0, 50.5)),
        ((10.2, 50.0), (10.5, 50.0), (10.5, 50.5)),
        ((10.2, 50.0), (10.5, 50.5), (10.2, 50.5)),
    ]
    return DTM.DefaultTerrainMap(list(paths), tris, [0, 0, 1, 1])


@pytest.fixture
def stub_elevation(monkeypatch):
    monkeypatch.setattr(
        DSF, "extract_elevation_and_bathymetry_data",
        lambda lat, lon: (b"", b""))
    # ``build_dsf`` goes through the DISPATCHER, not the stub above —
    # and with ``dsf_bathymetry`` defaulting to "auto" and no global-
    # scenery donor in the headless environment, the coastal-bathymetry
    # synthesize branch (commit 7e45fef) reaches a LIVE Overpass
    # coastline download inside these "no network" tests (observed:
    # identical code passing in 13.8 s or hanging > 90 s purely on
    # Overpass server timing).  Stub the dispatcher so the whole
    # elevation/bathymetry step is hermetic.
    monkeypatch.setattr(
        DSF, "elevation_and_bathymetry_data",
        lambda tile: (b"", b""))


# ── test 1: default_xplane emission ─────────────────────────────────────

@_DSFTOOL
def test_default_xplane_terrain_table_and_patches(
        tmp_path, monkeypatch, stub_elevation):
    build_dir = _prepare_build_dir(tmp_path)
    _write_mesh(build_dir, _LAND_AND_SEA_VERTS, _LAND_AND_SEA_TRIS)
    terrain_map = _split_terrain_map()
    monkeypatch.setattr(
        DTM.DefaultTerrainMap, "from_tile",
        classmethod(lambda cls, lat, lon: terrain_map))

    tile = _make_tile(build_dir, "default_xplane")
    download_queue = queue.Queue()
    rc = DSF.build_dsf(tile, download_queue)
    assert rc == 1

    # No orthophoto downloads were queued.
    assert download_queue.qsize() == 0

    # No .ter files were generated under the tile's terrain directory.
    terrain_dir = os.path.join(build_dir, "terrain")
    generated = (
        [f for f in os.listdir(terrain_dir) if f.endswith(".ter")]
        if os.path.isdir(terrain_dir) else [])
    assert generated == [], f"unexpected generated .ter files: {generated}"

    dump = DECODE.decode_dsf(_emitted_dsf(build_dir))

    # Terrain table = terrain_Water (index 0) + the two library paths.
    assert dump.terrain_paths[0] == "terrain_Water"
    assert set(dump.terrain_paths[1:]) == {
        "lib/g10/terrain10/grass.ter", "lib/g10/terrain10/asphalt.ter"}

    # Land patches are physical (flag 1), 5-plane, library-path terrains.
    land_patches = [
        p for p in dump.patches if p.terrain_path.startswith("lib/g10/")]
    assert land_patches, "no default-terrain land patches emitted"
    for patch in land_patches:
        assert patch.flags == 1, f"land patch not physical: {patch}"
        assert patch.plane_count == 5, f"land patch not 5-plane: {patch}"

    # The sea triangle took the plain terrain_Water path (physical, 7-plane).
    water_patches = [
        p for p in dump.patches if p.terrain_path == "terrain_Water"]
    assert water_patches, "sea triangle did not route to terrain_Water"
    for patch in water_patches:
        assert patch.flags == 1
        assert patch.plane_count == 7


# ── test 2: full_ortho behavioural regression ───────────────────────────

@_DSFTOOL
def test_full_ortho_still_queues_ortho(tmp_path, stub_elevation):
    build_dir = _prepare_build_dir(tmp_path)
    _write_mesh(build_dir, _LAND_AND_SEA_VERTS, _LAND_AND_SEA_TRIS)

    tile = _make_tile(build_dir, "full_ortho")
    download_queue = queue.Queue()
    rc = DSF.build_dsf(tile, download_queue)
    assert rc == 1

    # Classic behaviour: the land triangles queue an orthophoto download and
    # a generated ``terrain/*.ter`` is emitted for them.
    assert download_queue.qsize() >= 1

    terrain_dir = os.path.join(build_dir, "terrain")
    generated = [f for f in os.listdir(terrain_dir) if f.endswith(".ter")]
    assert generated, "full_ortho should generate .ter terrain files"

    dump = DECODE.decode_dsf(_emitted_dsf(build_dir))
    # Ortho land terrains are referenced as generated ``terrain/...`` paths,
    # not library paths.
    assert any(p.startswith("terrain/") for p in dump.terrain_paths), (
        f"full_ortho terrain table missing generated terrains: "
        f"{dump.terrain_paths}")
    assert not any(p.startswith("lib/g10/") for p in dump.terrain_paths)


# ── test 3: full_ortho byte-identity vs the base commit ─────────────────

def _load_base_module(tmp_path):
    """Import ``O4_DSF_Utils`` from the base commit (26ea8ee) as a distinct
    module, so its ``build_dsf`` can be run side-by-side with the edited one.
    No working-tree mutation (no stash): the base source is read straight out
    of git."""
    import importlib.util
    import subprocess

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        base_src = subprocess.check_output(
            ["git", "-C", here, "show", "26ea8ee:src/O4_DSF_Utils.py"],
            text=True)
    except (subprocess.SubprocessError, OSError):
        return None
    base_path = str(tmp_path / "O4_DSF_Utils_base.py")
    with open(base_path, "w") as handle:
        handle.write(base_src)
    spec = importlib.util.spec_from_file_location(
        "O4_DSF_Utils_base", base_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_full_ortho_byte_identical_to_base(tmp_path, monkeypatch):
    """The full_ortho path must be byte-for-byte unchanged by this feature.

    Builds the same synthetic tile with both the edited ``build_dsf`` and the
    base-commit ``build_dsf`` and compares the emitted DSF bytes.  (A separate
    real-tile A/B is the lead's integration check; this guards the synthetic
    path here.)
    """
    os.environ["PYTHONHASHSEED"] = "0"
    base = _load_base_module(tmp_path)
    if base is None:
        pytest.skip("could not load base-commit O4_DSF_Utils (git show failed)")

    def _build(module, sub):
        build_dir = _prepare_build_dir(tmp_path / sub)
        _write_mesh(build_dir, _LAND_AND_SEA_VERTS, _LAND_AND_SEA_TRIS)
        monkeypatch.setattr(
            module, "extract_elevation_and_bathymetry_data",
            lambda lat, lon: (b"", b""))
        tile = _make_tile(build_dir, "full_ortho")
        rc = module.build_dsf(tile, queue.Queue())
        assert rc == 1
        with open(_emitted_dsf(build_dir), "rb") as handle:
            return handle.read()

    (tmp_path / "edited").mkdir()
    (tmp_path / "base").mkdir()
    edited_bytes = _build(DSF, "edited")
    base_bytes = _build(base, "base")
    assert edited_bytes == base_bytes, (
        "full_ortho DSF bytes changed vs base commit "
        f"(edited={len(edited_bytes)}B, base={len(base_bytes)}B)")


# ── test 4: non-projected fallback substitution (decision 9) ────────────

_SUBST_VERTS = [
    (10.05, 50.05), (10.15, 50.05), (10.15, 50.15), (10.05, 50.15),
    (10.30, 50.05), (10.40, 50.05), (10.35, 50.15),
]
_SUBST_TRIS = [
    (1, 2, 3, 0),   # grass (projected)
    (1, 3, 4, 0),   # grass (projected)
    (5, 6, 7, 0),   # asphalt (non-projected -> substituted)
]


@_DSFTOOL
def test_non_projected_terrain_is_substituted(
        tmp_path, monkeypatch, stub_elevation):
    build_dir = _prepare_build_dir(tmp_path)
    _write_mesh(build_dir, _SUBST_VERTS, _SUBST_TRIS)

    terrain_map = _split_terrain_map()
    # grass (index 0) projected, asphalt (index 1) NOT projected.
    terrain_map._projected_cache = {0: True, 1: False}
    monkeypatch.setattr(
        DTM.DefaultTerrainMap, "from_tile",
        classmethod(lambda cls, lat, lon: terrain_map))

    tile = _make_tile(build_dir, "default_xplane")
    rc = DSF.build_dsf(tile, queue.Queue())
    assert rc == 1

    dump = DECODE.decode_dsf(_emitted_dsf(build_dir))
    # The non-projected asphalt terrain must have been replaced by the
    # running-majority projected grass; asphalt never reaches the table.
    assert "lib/g10/terrain10/asphalt.ter" not in dump.terrain_paths
    assert "lib/g10/terrain10/grass.ter" in dump.terrain_paths
    # Every land patch is grass, still physical + 5-plane.
    land_patches = [
        p for p in dump.patches if p.terrain_path.startswith("lib/g10/")]
    assert land_patches
    for patch in land_patches:
        assert patch.terrain_path == "lib/g10/terrain10/grass.ter"
        assert patch.flags == 1
        assert patch.plane_count == 5


# ── test 5: missing default DSF fails loudly ────────────────────────────

def test_default_xplane_hard_errors_without_map(
        tmp_path, monkeypatch, stub_elevation):
    build_dir = _prepare_build_dir(tmp_path)
    _write_mesh(build_dir, _LAND_AND_SEA_VERTS, _LAND_AND_SEA_TRIS)
    monkeypatch.setattr(
        DTM.DefaultTerrainMap, "from_tile",
        classmethod(lambda cls, lat, lon: None))
    tile = _make_tile(build_dir, "default_xplane")
    with pytest.raises(RuntimeError) as excinfo:
        DSF.build_dsf(tile, queue.Queue())
    assert "custom_overlay_src" in str(excinfo.value)


# ── test 6: airport_ortho physical base + ortho overlay (work package 3) ─

def _covered_land_geometry():
    """Airport geometry covering the land square (lon/lat local [0.05,0.35])
    but not the disjoint sea triangle (local ~0.7, 0.75)."""
    square = MultiPolygon([Polygon([
        (0.05, 0.05), (0.35, 0.05), (0.35, 0.35), (0.05, 0.35)])])
    return FADE.AirportOrthoGeometry(
        square, 1000.0, tile_lon=10, tile_lat=50, ref_lat=50.5)


def _covered_land_download_set(tile):
    """The exact texture tiles the two covered land-triangle centroids map to
    (recomputed from the writer's own ortho dico), for the download-queue
    assertion."""
    dico = DSF.zone_list_to_ortho_dico(tile)
    expected = set()
    for (i, j, k, attr) in _LAND_AND_SEA_TRIS:
        if attr != 0:
            continue
        verts = [_LAND_AND_SEA_VERTS[idx - 1] for idx in (i, j, k)]
        bary_lon = sum(v[0] for v in verts) / 3
        bary_lat = sum(v[1] for v in verts) / 3
        expected.add(
            dico[GEO.wgs84_to_orthogrid(bary_lat, bary_lon, tile.mesh_zl)])
    return expected


@_DSFTOOL
def test_airport_ortho_physical_base_and_overlay(
        tmp_path, monkeypatch, stub_elevation):
    build_dir = _prepare_build_dir(tmp_path)
    _write_mesh(build_dir, _LAND_AND_SEA_VERTS, _LAND_AND_SEA_TRIS)

    terrain_map = _split_terrain_map()
    monkeypatch.setattr(
        DTM.DefaultTerrainMap, "from_tile",
        classmethod(lambda cls, lat, lon: terrain_map))
    geometry = _covered_land_geometry()
    monkeypatch.setattr(
        DSF.FADE, "build_airport_ortho_geometry", lambda tile: geometry)

    tile = _make_tile(build_dir, "airport_ortho")
    expected_downloads = _covered_land_download_set(tile)

    download_queue = queue.Queue()
    rc = DSF.build_dsf(tile, download_queue)
    assert rc == 1

    # The download queue holds exactly the covered-land texture tiles and
    # nothing else (the sea triangle is uncovered -> no ortho download).
    queued = set()
    while not download_queue.empty():
        queued.add(download_queue.get())
    assert queued == expected_downloads
    assert queued, "airport_ortho queued no ortho downloads for covered land"

    # A fade-mask PNG was written for each covered texture tile.
    for attributes in expected_downloads:
        mask_name = FNAMES.airport_fade_mask_name(*attributes)
        assert os.path.isfile(
            os.path.join(build_dir, "textures", mask_name)), (
            f"fade mask {mask_name} not written")

    dump = DECODE.decode_dsf(_emitted_dsf(build_dir))

    # Physical base: default-landclass library terrains, flag 1, 5-plane.
    physical_land = [
        p for p in dump.patches if p.terrain_path.startswith("lib/g10/")]
    assert physical_land, "no physical default-terrain land patches emitted"
    for patch in physical_land:
        assert patch.flags == 1, f"physical land not flag 1: {patch}"
        assert patch.plane_count == 5, f"physical land not 5-plane: {patch}"

    # Overlay ortho: generated ``terrain/..._overlay.ter``, flag 2, 9-plane.
    overlay_ortho = [
        p for p in dump.patches
        if p.terrain_path.startswith("terrain/")
        and p.terrain_path.endswith("_overlay.ter")]
    assert overlay_ortho, "no ortho overlay patches emitted over the airport"
    for patch in overlay_ortho:
        assert patch.flags == 2, f"ortho overlay not flag 2: {patch}"
        assert patch.plane_count == 9, f"ortho overlay not 9-plane: {patch}"

    # The uncovered sea triangle took the plain terrain_Water path.
    water = [p for p in dump.patches if p.terrain_path == "terrain_Water"]
    assert water, "sea triangle did not route to terrain_Water"
    for patch in water:
        assert patch.flags == 1
        assert patch.plane_count == 7

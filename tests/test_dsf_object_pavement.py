"""DSF OBJ8 ground-paint objects as pavement (HECA Tai Models pack).

Scenery packs draw an airport's base pavement as draped-only ``.obj``
texture pages (one object per texture, carrying the whole airport's
geometry for that page).  Four cooperating pieces admit them as pavement:

* ``obj8_reader.load_object_file`` parses ``ATTR_layer_group_draped`` into
  ``ObjectGeometry.draped_layer_group`` — the base-vs-decal ordering
  signal (test group A).
* ``dsf_reader._is_pavement_object`` classifies a resource as base
  pavement — draped-only, a pavement layer group at a low offset, no
  decorative name token (test group B).
* ``object_footprints.draped_pavement_patches`` unions one placement's
  draped triangles into ALL disjoint patches, interior holes preserved,
  slivers dropped (test group C).
* ``dsf_reader.read_dsf_object_pavements`` walks the DSF placements,
  resolves + parses each ``.obj``, classifies, and emits the pavement
  tuples, sidecar-cached (test groups D, E).

Harness patterns are lifted verbatim from
``tests/test_dsf_object_buildings.py``: the equirectangular projection
double (``fake_projection``), the pre-seeded mtime-backdated ``.dsf.text``
(``_write_fake_dsf``), the ``ORTHO4XP_DATA_ROOT`` sandbox that keeps
sidecars out of the checkout, and the ``O4_OBJECT_FOOTPRINT_CACHE=0``
gate.  Every test is headless and ``tmp_path``-based (no network, no
X-Plane install).
"""
import math
import os

import pytest
from shapely.geometry import Polygon

from auto_patch import config
from auto_patch import dsf_reader as D
from auto_patch import obj8_reader
from auto_patch import object_footprints

METRES_PER_DEGREE_LATITUDE = obj8_reader.METRES_PER_DEGREE_LATITUDE

ANCHOR_LATITUDE = 35.0
ANCHOR_LONGITUDE = -80.0


@pytest.fixture(autouse=True)
def sandbox_ortho4xp_data_root(tmp_path, monkeypatch):
    """Pin the Ortho4XP data root under ``tmp_path`` so any sidecar the
    pavement reader writes lands in the sandbox, never in the checkout
    (mirrors ``tests/test_dsf_object_buildings.py``)."""
    monkeypatch.setenv("ORTHO4XP_DATA_ROOT",
                       str(tmp_path / "o4_data_root"))


# ── shared projection / placement doubles (from the buildings suite) ──

def equirectangular_local_offset_to_lonlat(
        anchor_latitude, anchor_longitude, heading_degrees,
        local_x, local_z):
    """Test double for ``obj8_reader.local_offset_to_lonlat``, mirroring
    its documented convention (local +x = east, +z = south; heading
    clockwise from north)."""
    heading = math.radians(heading_degrees)
    east = local_x * math.cos(heading) - local_z * math.sin(heading)
    south = local_x * math.sin(heading) + local_z * math.cos(heading)
    metres_per_degree_longitude = (
        METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(anchor_latitude)))
    return (anchor_latitude - south / METRES_PER_DEGREE_LATITUDE,
            anchor_longitude + east / metres_per_degree_longitude)


@pytest.fixture()
def fake_projection(monkeypatch):
    monkeypatch.setattr(obj8_reader, "local_offset_to_lonlat",
                        equirectangular_local_offset_to_lonlat)


def make_placement(resource_path="pavement.obj",
                   longitude=ANCHOR_LONGITUDE,
                   latitude=ANCHOR_LATITUDE,
                   heading_degrees=0.0,
                   definition_index=0):
    return obj8_reader.ObjectPlacement(
        definition_index=definition_index,
        resource_path=resource_path,
        longitude=longitude,
        latitude=latitude,
        heading_degrees=heading_degrees,
    )


def make_geometry(vertices, draped_triangles=(), solid_triangles=(),
                  draped_layer_group=None):
    """An ``ObjectGeometry`` with draped and/or solid triangles and an
    optional ``draped_layer_group`` (constructed directly — no file
    parse — for the classifier and union tests)."""
    return obj8_reader.ObjectGeometry(
        vertices=list(vertices),
        solid_triangles=list(solid_triangles),
        draped_triangles=list(draped_triangles),
        positional_commands=[],
        animation_block_count=0,
        level_of_detail_count=0,
        vertex_line_indices=list(range(len(vertices))),
        draped_layer_group=draped_layer_group,
    )


def ring_to_local_metres(ring,
                         anchor_latitude=ANCHOR_LATITUDE,
                         anchor_longitude=ANCHOR_LONGITUDE):
    """Invert the equirectangular projection back to heading-0 local
    ``(x = east, z = south)`` metres for geometric assertions."""
    metres_per_degree_longitude = (
        METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(anchor_latitude)))
    return [
        ((longitude - anchor_longitude) * metres_per_degree_longitude,
         -(latitude - anchor_latitude) * METRES_PER_DEGREE_LATITUDE)
        for longitude, latitude in ring
    ]


def ring_metric_extent(ring, anchor_latitude=ANCHOR_LATITUDE):
    """``(east_span_m, south_span_m)`` of a lon/lat ring's bounding box,
    at the anchor's local equirectangular scale."""
    longitudes = [longitude for longitude, _latitude in ring]
    latitudes = [latitude for _longitude, latitude in ring]
    metres_per_degree_longitude = (
        METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(anchor_latitude)))
    return ((max(longitudes) - min(longitudes)) * metres_per_degree_longitude,
            (max(latitudes) - min(latitudes)) * METRES_PER_DEGREE_LATITUDE)


def _add_rectangle(vertices, triangles, x0, z0, x1, z1):
    """Append a y=0 axis-aligned rectangle (4 fresh vertices, 2 draped
    triangles) — the union primitive for the patch-shape tests."""
    base = len(vertices)
    vertices.extend([(x0, 0.0, z0), (x1, 0.0, z0),
                     (x1, 0.0, z1), (x0, 0.0, z1)])
    triangles.extend([(base, base + 1, base + 2),
                      (base, base + 2, base + 3)])


# ── OBJ8 synthetic-file author (parser + end-to-end reader tests) ────

def _write_obj8(path, vertices, *, draped_triangles=(), solid_triangles=(),
                layer_group_lines=()):
    """Author a minimal but real OBJ8 file.

    Draped triangles are emitted under ``ATTR_draped`` and solid under
    ``ATTR_no_draped``; ``layer_group_lines`` are inserted verbatim after
    ``TEXTURE_DRAPED`` so the parser sees exactly the declarations the
    test wants (including malformed and repeated ones)."""
    lines = ["I", "800", "OBJ", "", "TEXTURE_DRAPED base.png"]
    lines.extend(layer_group_lines)
    indices = []
    for triangle in list(draped_triangles) + list(solid_triangles):
        indices.extend(triangle)
    lines.append(f"POINT_COUNTS {len(vertices)} 0 0 {len(indices)}")
    for x, y, z in vertices:
        lines.append(f"VT {x} {y} {z} 0 1 0 0 0")
    for start in range(0, len(indices), 10):
        chunk = indices[start:start + 10]
        lines.append("IDX10 " + " ".join(str(index) for index in chunk))
    draped_index_count = len(draped_triangles) * 3
    solid_index_count = len(solid_triangles) * 3
    if draped_index_count:
        lines.append("ATTR_draped")
        lines.append(f"TRIS 0 {draped_index_count}")
    if solid_index_count:
        lines.append("ATTR_no_draped")
        lines.append(f"TRIS {draped_index_count} {solid_index_count}")
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")


# A single y=0 draped square, 10 x 10 m (100 m2), reused by the parser
# tests where only ``draped_layer_group`` is under test.
_SQUARE_VERTICES = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0),
                    (10.0, 0.0, 10.0), (0.0, 0.0, 10.0)]
_SQUARE_DRAPED = [(0, 1, 2), (0, 2, 3)]


def _write_square_obj(path, layer_group_lines=()):
    _write_obj8(path, _SQUARE_VERTICES,
                draped_triangles=_SQUARE_DRAPED,
                layer_group_lines=layer_group_lines)


# ── group A: ATTR_layer_group_draped parsing ─────────────────────────

class TestLayerGroupParsing:
    def test_declared_with_offset(self, tmp_path):
        path = tmp_path / "a.obj"
        _write_square_obj(path,
                          ["ATTR_layer_group_draped runways 1"])
        geometry = obj8_reader.load_object_file(str(path))
        assert geometry.draped_layer_group == ("runways", 1)

    def test_declared_without_offset_is_zero(self, tmp_path):
        path = tmp_path / "a.obj"
        _write_square_obj(path,
                          ["ATTR_layer_group_draped taxiways"])
        geometry = obj8_reader.load_object_file(str(path))
        assert geometry.draped_layer_group == ("taxiways", 0)

    def test_not_declared_is_none(self, tmp_path):
        path = tmp_path / "a.obj"
        _write_square_obj(path, [])
        geometry = obj8_reader.load_object_file(str(path))
        assert geometry.draped_layer_group is None

    def test_non_numeric_offset_is_zero(self, tmp_path):
        path = tmp_path / "a.obj"
        _write_square_obj(path,
                          ["ATTR_layer_group_draped runways high"])
        geometry = obj8_reader.load_object_file(str(path))
        assert geometry.draped_layer_group == ("runways", 0)

    def test_last_declaration_wins(self, tmp_path):
        path = tmp_path / "a.obj"
        _write_square_obj(path, [
            "ATTR_layer_group_draped runways 1",
            "ATTR_layer_group_draped taxiways 3",
        ])
        geometry = obj8_reader.load_object_file(str(path))
        assert geometry.draped_layer_group == ("taxiways", 3)

    def test_group_is_lowercased(self, tmp_path):
        path = tmp_path / "a.obj"
        _write_square_obj(path,
                          ["ATTR_layer_group_draped RunWays 1"])
        geometry = obj8_reader.load_object_file(str(path))
        assert geometry.draped_layer_group == ("runways", 1)

    def test_malformed_line_leaves_field_unchanged(self, tmp_path):
        # A bare keyword (no group token) must not clobber a prior valid
        # declaration.
        path = tmp_path / "a.obj"
        _write_square_obj(path, [
            "ATTR_layer_group_draped runways 1",
            "ATTR_layer_group_draped",
        ])
        geometry = obj8_reader.load_object_file(str(path))
        assert geometry.draped_layer_group == ("runways", 1)


# ── group B: _is_pavement_object classification ──────────────────────

class TestIsPavementObject:
    def test_draped_only_pavement_layer_is_admitted(self):
        geometry = make_geometry(
            _SQUARE_VERTICES, draped_triangles=_SQUARE_DRAPED,
            draped_layer_group=("runways", 1))
        assert D._is_pavement_object("base_asphalt.obj", geometry) is True

    def test_solid_triangles_present_is_refused(self):
        geometry = make_geometry(
            _SQUARE_VERTICES,
            draped_triangles=_SQUARE_DRAPED,
            solid_triangles=[(0, 1, 2)],
            draped_layer_group=("runways", 1))
        assert D._is_pavement_object("base_asphalt.obj", geometry) is False

    def test_no_layer_group_is_refused(self):
        geometry = make_geometry(
            _SQUARE_VERTICES, draped_triangles=_SQUARE_DRAPED,
            draped_layer_group=None)
        assert D._is_pavement_object("base_asphalt.obj", geometry) is False

    def test_markings_group_is_refused(self):
        geometry = make_geometry(
            _SQUARE_VERTICES, draped_triangles=_SQUARE_DRAPED,
            draped_layer_group=("markings", 1))
        assert D._is_pavement_object("base_asphalt.obj", geometry) is False

    def test_offset_above_max_is_refused(self):
        geometry = make_geometry(
            _SQUARE_VERTICES, draped_triangles=_SQUARE_DRAPED,
            draped_layer_group=("runways", 2))
        # Default DSF_OBJECT_PAVEMENT_MAX_LAYER_OFFSET is 1.
        assert D._is_pavement_object("base_asphalt.obj", geometry) is False

    def test_offset_admitted_when_max_raised(self, monkeypatch):
        # Proves the function-local ``from .config import ...`` idiom: the
        # classifier reads the constant at CALL time, so monkeypatching
        # the config module changes its verdict.
        geometry = make_geometry(
            _SQUARE_VERTICES, draped_triangles=_SQUARE_DRAPED,
            draped_layer_group=("runways", 2))
        monkeypatch.setattr(
            config, "DSF_OBJECT_PAVEMENT_MAX_LAYER_OFFSET", 2)
        assert D._is_pavement_object("base_asphalt.obj", geometry) is True

    def test_decorative_basename_is_vetoed(self):
        geometry = make_geometry(
            _SQUARE_VERTICES, draped_triangles=_SQUARE_DRAPED,
            draped_layer_group=("runways", 1))
        # "taxi_lines.obj" carries the decorative "line" token.
        assert D._is_pavement_object("taxi_lines.obj", geometry) is False

    def test_veto_checks_basename_not_directory(self):
        geometry = make_geometry(
            _SQUARE_VERTICES, draped_triangles=_SQUARE_DRAPED,
            draped_layer_group=("runways", 1))
        # A decorative token in a PARENT directory must not veto a clean
        # basename.
        assert D._is_pavement_object(
            "lines_pack/asphalt.obj", geometry) is True


# ── group C: draped_pavement_patches ─────────────────────────────────

class TestDrapedPavementPatches:
    def test_two_disjoint_squares_yield_two_patches(self, fake_projection):
        vertices, triangles = [], []
        _add_rectangle(vertices, triangles, 0.0, 0.0, 10.0, 10.0)
        _add_rectangle(vertices, triangles, 100.0, 0.0, 110.0, 10.0)
        geometry = make_geometry(vertices, draped_triangles=triangles)
        patches = object_footprints.draped_pavement_patches(
            geometry, make_placement(), 20.0)
        assert len(patches) == 2
        for outer_ring, hole_rings in patches:
            assert hole_rings == []
            assert outer_ring[0] != outer_ring[-1]
            polygon = Polygon(ring_to_local_metres(outer_ring))
            assert polygon.area == pytest.approx(100.0, abs=1.0)

    def test_square_with_hole_preserves_interior_ring(self, fake_projection):
        # A 30 x 30 annulus with a 10 x 10 hole at (10..20, 10..20),
        # tessellated from four boundary bands.
        vertices, triangles = [], []
        _add_rectangle(vertices, triangles, 0.0, 0.0, 30.0, 10.0)    # bottom
        _add_rectangle(vertices, triangles, 0.0, 20.0, 30.0, 30.0)   # top
        _add_rectangle(vertices, triangles, 0.0, 10.0, 10.0, 20.0)   # left
        _add_rectangle(vertices, triangles, 20.0, 10.0, 30.0, 20.0)  # right
        geometry = make_geometry(vertices, draped_triangles=triangles)
        patches = object_footprints.draped_pavement_patches(
            geometry, make_placement(), 20.0)
        assert len(patches) == 1
        outer_ring, hole_rings = patches[0]
        assert len(hole_rings) == 1
        # The exterior ring encloses the full 30 x 30 (900 m2); the hole
        # is the 10 x 10 (100 m2) interior, so the net paved area is 800.
        outer_local = ring_to_local_metres(outer_ring)
        hole_local = ring_to_local_metres(hole_rings[0])
        assert Polygon(outer_local).area == pytest.approx(900.0, abs=5.0)
        assert Polygon(hole_local).area == pytest.approx(100.0, abs=5.0)
        assert Polygon(outer_local, [hole_local]).area == pytest.approx(
            800.0, abs=5.0)

    def test_below_minimum_patch_dropped_larger_kept(self, fake_projection):
        vertices, triangles = [], []
        _add_rectangle(vertices, triangles, 0.0, 0.0, 10.0, 10.0)    # 100 m2
        _add_rectangle(vertices, triangles, 200.0, 0.0, 203.0, 3.0)  # 9 m2
        geometry = make_geometry(vertices, draped_triangles=triangles)
        patches = object_footprints.draped_pavement_patches(
            geometry, make_placement(), 20.0)
        # The 9 m2 sliver is below the 20 m2 floor; only the 100 m2 patch
        # survives.
        assert len(patches) == 1
        outer_ring, _hole_rings = patches[0]
        polygon = Polygon(ring_to_local_metres(outer_ring))
        assert polygon.area == pytest.approx(100.0, abs=1.0)

    def test_heading_rotates_the_patch(self, fake_projection):
        # A 20 (east) x 5 (south) rectangle: heading 90 clockwise swaps
        # the axes, so the east span and south span exchange.
        vertices, triangles = [], []
        _add_rectangle(vertices, triangles, 0.0, 0.0, 20.0, 5.0)
        geometry = make_geometry(vertices, draped_triangles=triangles)

        north_up = object_footprints.draped_pavement_patches(
            geometry, make_placement(heading_degrees=0.0), 1.0)
        assert len(north_up) == 1
        east_span, south_span = ring_metric_extent(north_up[0][0])
        assert east_span == pytest.approx(20.0, abs=0.5)
        assert south_span == pytest.approx(5.0, abs=0.5)

        rotated = object_footprints.draped_pavement_patches(
            geometry, make_placement(heading_degrees=90.0), 1.0)
        assert len(rotated) == 1
        east_span, south_span = ring_metric_extent(rotated[0][0])
        assert east_span == pytest.approx(5.0, abs=0.5)
        assert south_span == pytest.approx(20.0, abs=0.5)

    def test_no_draped_triangles_returns_empty(self, fake_projection):
        geometry = make_geometry(
            _SQUARE_VERTICES, draped_triangles=[],
            solid_triangles=_SQUARE_DRAPED)
        assert object_footprints.draped_pavement_patches(
            geometry, make_placement(), 20.0) == []


# ── group D + E: read_dsf_object_pavements end-to-end + sidecar ──────

def _write_fake_dsf(tmp_path, body):
    """A fake ``.dsf`` plus a pre-seeded, mtime-backdated ``.dsf.text`` so
    ``_load_dsf_text`` serves the text and DSFTool never runs — laid out
    as ``<pack>/Earth nav data/<group>/<tile>.dsf`` so
    ``_pack_root_for_dsf`` resolves the pack (harness pattern from
    ``tests/test_dsf_object_buildings.py``)."""
    pack_root = tmp_path / "Fake Scenery Pack"
    dsf_directory = pack_root / "Earth nav data" / "+30-090"
    dsf_directory.mkdir(parents=True)
    dsf = dsf_directory / "+35-081.dsf"
    dsf.write_text("binary-placeholder")
    text = dsf_directory / "+35-081.dsf.text"
    text.write_text(body)
    now = os.path.getmtime(text)
    os.utime(dsf, (now - 10, now - 10))
    return str(dsf), str(pack_root)


# 40 x 40 m base pavement (1600 m2, well over the 20 m2 floor), centred
# on the placement anchor so the real projection lands it cleanly.
_BASE_PAVEMENT_VERTICES = [(-20.0, 0.0, -20.0), (20.0, 0.0, -20.0),
                           (20.0, 0.0, 20.0), (-20.0, 0.0, 20.0)]
_BASE_PAVEMENT_DRAPED = [(0, 1, 2), (0, 2, 3)]


def _build_pavement_pack(tmp_path):
    """A fake pack with three placed objects: an admitted base-pavement
    page, a refused markings-layer page, and a refused solid object."""
    body = "\n".join([
        "OBJECT_DEF base_pavement.obj",
        "OBJECT_DEF overlay.obj",
        "OBJECT_DEF hangar.obj",
        "OBJECT 0 -80.930000 35.210000 0.000000",
        "OBJECT 1 -80.931000 35.211000 0.000000",
        "OBJECT 2 -80.932000 35.212000 0.000000",
    ]) + "\n"
    dsf_path, pack_root = _write_fake_dsf(tmp_path, body)

    _write_obj8(os.path.join(pack_root, "base_pavement.obj"),
                _BASE_PAVEMENT_VERTICES,
                draped_triangles=_BASE_PAVEMENT_DRAPED,
                layer_group_lines=["ATTR_layer_group_draped runways 1"])
    # Draped, but a non-pavement layer group → refused by the classifier.
    _write_obj8(os.path.join(pack_root, "overlay.obj"),
                _BASE_PAVEMENT_VERTICES,
                draped_triangles=_BASE_PAVEMENT_DRAPED,
                layer_group_lines=["ATTR_layer_group_draped markings 1"])
    # Solid geometry (even with a pavement layer group) → refused.
    _write_obj8(os.path.join(pack_root, "hangar.obj"),
                _BASE_PAVEMENT_VERTICES,
                solid_triangles=_BASE_PAVEMENT_DRAPED,
                layer_group_lines=["ATTR_layer_group_draped runways 1"])
    return dsf_path, pack_root


class TestReadDsfObjectPavements:
    def test_only_base_pavement_object_is_admitted(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
        monkeypatch.setenv("O4_OBJECT_FOOTPRINT_CACHE", "0")
        dsf_path, _pack_root = _build_pavement_pack(tmp_path)

        pavements = D.read_dsf_object_pavements(dsf_path, xplane_root=None)
        # The markings page and the solid object are both refused; only
        # the base-pavement page contributes patches.
        assert len(pavements) >= 1
        for outer_ring, holes, def_path in pavements:
            assert def_path == "base_pavement.obj"
            assert holes == []
            assert len(outer_ring) >= 3
            assert outer_ring[0] != outer_ring[-1]
        # The admitted 40 x 40 m page is one ~1600 m2 patch anchored at
        # its placement.
        assert len(pavements) == 1
        polygon = Polygon(pavements[0][0])
        centroid = polygon.centroid
        assert centroid.x == pytest.approx(-80.930, abs=2e-5)
        assert centroid.y == pytest.approx(35.210, abs=2e-5)
        metres_per_degree_longitude = (
            METRES_PER_DEGREE_LATITUDE
            * math.cos(math.radians(35.210)))
        area_square_metres = (polygon.area
                              * METRES_PER_DEGREE_LATITUDE
                              * metres_per_degree_longitude)
        assert area_square_metres == pytest.approx(1600.0, rel=0.02)

    def test_no_object_placements_returns_empty(self, tmp_path,
                                                monkeypatch):
        monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
        monkeypatch.setenv("O4_OBJECT_FOOTPRINT_CACHE", "0")
        dsf_path, _pack_root = _write_fake_dsf(
            tmp_path, "POLYGON_DEF lib/airport/pavement/asphalt.pol\n")
        assert D.read_dsf_object_pavements(
            dsf_path, xplane_root=None) == []


class TestObjectPavementSidecarCache:
    """The pavement sidecar (``o4_object_pavements_<stem>.cache`` under
    the data root's ``Airport_mod_cache/<pack>/``) round-trips: the first
    call computes and writes it, the second serves it without re-running
    the patch union."""

    def _sidecar_path(self, tmp_path, dsf_path, pack_root):
        data_root = str(tmp_path / "o4_data_root")
        pack_name = os.path.basename(os.path.abspath(pack_root))
        dsf_stem = os.path.splitext(os.path.basename(dsf_path))[0]
        return os.path.join(
            data_root, "Airport_mod_cache", pack_name,
            f"o4_object_pavements_{dsf_stem}.cache")

    def test_warm_hit_returns_cached_and_skips_recompute(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
        monkeypatch.setenv("O4_OBJECT_FOOTPRINT_CACHE", "1")
        dsf_path, pack_root = _build_pavement_pack(tmp_path)

        first = D.read_dsf_object_pavements(dsf_path, xplane_root=None)
        assert first  # the base page produced at least one patch
        sidecar = self._sidecar_path(tmp_path, dsf_path, pack_root)
        assert os.path.isfile(sidecar)
        assert sidecar.startswith(
            os.path.join(str(tmp_path / "o4_data_root"),
                         "Airport_mod_cache"))

        # Poison the union step: a genuine recompute would now raise, so a
        # clean identical return proves the second call served the cache.
        def exploding_patches(*arguments, **keyword_arguments):
            raise AssertionError(
                "draped_pavement_patches must not run on a warm cache hit")

        monkeypatch.setattr(object_footprints, "draped_pavement_patches",
                            exploding_patches)
        second = D.read_dsf_object_pavements(dsf_path, xplane_root=None)
        assert second == first

    def test_gate_zero_disables_read_and_write(self, tmp_path,
                                               monkeypatch):
        monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
        monkeypatch.setenv("O4_OBJECT_FOOTPRINT_CACHE", "0")
        dsf_path, pack_root = _build_pavement_pack(tmp_path)
        D.read_dsf_object_pavements(dsf_path, xplane_root=None)
        # With the gate off no sidecar is written.
        assert not os.path.isfile(
            self._sidecar_path(tmp_path, dsf_path, pack_root))


# ── F. vehicle-pavement admission classifier ─────────────────────────

class TestIsVehiclePavementPatch:
    """``object_footprints.is_vehicle_pavement_patch`` — the opening-ratio
    test that keeps painted service roads / drainage channels (HECA Tai
    Models ``road.obj``: ~6 m corridors over kilometres) out of the
    aircraft pavement union at admission.  Width/ratio arguments are
    passed explicitly, mirroring the pipeline call site."""

    WIDTH_M = 11.0
    RATIO = 0.35

    def _classify(self, polygon):
        return object_footprints.is_vehicle_pavement_patch(
            polygon, self.WIDTH_M, self.RATIO)

    def test_narrow_road_corridor_is_vehicle(self):
        # A 6 m x 800 m straight road: erosion by 5.5 m leaves nothing.
        road = Polygon([(0, 0), (800, 0), (800, 6), (0, 6)])
        assert self._classify(road) is True

    def test_wide_apron_sheet_is_not_vehicle(self):
        apron = Polygon([(0, 0), (200, 0), (200, 60), (0, 60)])
        assert self._classify(apron) is False

    def test_road_network_with_wide_pockets_is_still_vehicle(self):
        # The HECA road.obj failure mode for plain erosion-to-empty: a
        # long 6 m corridor with an occasional wide plaza.  The plaza
        # survives erosion (so ``buffer(-5.5).is_empty`` is False), but
        # the OPENED area is a small fraction of the whole patch.
        road = Polygon([(0, 0), (1500, 0), (1500, 6), (0, 6)])
        plaza = Polygon([(700, -12), (730, -12), (730, 18), (700, 18)])
        network = road.union(plaza)
        assert network.geom_type == "Polygon"
        eroded = network.buffer(-0.5 * self.WIDTH_M)
        assert not eroded.is_empty  # the pocket survives plain erosion
        assert self._classify(network) is True

    def test_taxiway_width_corridor_is_kept(self):
        # A 23 m corridor (code-C taxiway with shoulders) opens to
        # nearly its full area — aircraft pavement, kept.
        taxiway = Polygon([(0, 0), (400, 0), (400, 23), (0, 23)])
        assert self._classify(taxiway) is False

    def test_geometry_error_fails_open(self):
        class ExplodingPolygon:
            area = 100.0

            def buffer(self, *_arguments, **_keyword_arguments):
                raise ValueError("bad geometry")

        assert object_footprints.is_vehicle_pavement_patch(
            ExplodingPolygon(), self.WIDTH_M, self.RATIO) is False


class TestAbuttingContactRatio:
    """``object_footprints.abutting_contact_ratio`` — the edge-contact
    measure that readmits painted taxiway SHOULDERS (which fail the
    width test exactly like roads) into the pavement union."""

    def test_abutting_shoulder_scores_about_one(self):
        taxiway = Polygon([(0, 0), (400, 0), (400, 23), (0, 23)])
        # 6 m shoulder sharing the taxiway's full north edge.
        shoulder = Polygon([(0, 23), (400, 23), (400, 29), (0, 29)])
        ratio = object_footprints.abutting_contact_ratio(
            shoulder, [taxiway])
        assert 0.9 <= ratio <= 1.2

    def test_sandwiched_strip_scores_about_two(self):
        south = Polygon([(0, 0), (400, 0), (400, 23), (0, 23)])
        north = Polygon([(0, 29), (400, 29), (400, 60), (0, 60)])
        strip = Polygon([(0, 23), (400, 23), (400, 29), (0, 29)])
        ratio = object_footprints.abutting_contact_ratio(
            strip, [south, north])
        assert ratio >= 1.8

    def test_offset_drainage_or_road_scores_low(self):
        taxiway = Polygon([(0, 0), (400, 0), (400, 23), (0, 23)])
        # Strip 3 m clear of the pavement edge (grass verge between).
        offset = Polygon([(0, 26), (400, 26), (400, 32), (0, 32)])
        ratio = object_footprints.abutting_contact_ratio(
            offset, [taxiway])
        assert ratio < 0.1

    def test_no_neighbours_scores_zero(self):
        strip = Polygon([(0, 0), (400, 0), (400, 6), (0, 6)])
        assert object_footprints.abutting_contact_ratio(strip, []) == 0.0

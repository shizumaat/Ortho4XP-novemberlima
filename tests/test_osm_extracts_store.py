"""Regional-extract store: region selection, wanted list, entry point
(docs/specs/osm-regional-extracts-spec.md).

Headless: STORE_DIRECTORY is monkeypatched to a tmp_path, the Geofabrik
index is synthetic, and the filter is stubbed — no network, no pbf.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import O4_OSM_Extracts as EXTRACTS  # noqa: E402


def _region_feature(region_id, parent, lon_min, lat_min, lon_max, lat_max):
    return {
        "type": "Feature",
        "properties": {
            "id": region_id,
            "parent": parent,
            "urls": {"pbf": "https://example.invalid/%s.pbf" % region_id},
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lon_min, lat_min], [lon_max, lat_min],
                [lon_max, lat_max], [lon_min, lat_max],
                [lon_min, lat_min],
            ]],
        },
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = str(tmp_path / "extracts")
    monkeypatch.setattr(EXTRACTS, "STORE_DIRECTORY", directory)
    monkeypatch.setattr(EXTRACTS, "extracts_enabled", lambda: True)
    EXTRACTS._leaf_regions.cache = None
    # Synthetic Geofabrik index: europe is a parent (excluded), portugal
    # and spain are adjacent leaves meeting at lon -7.4.
    index = {
        "type": "FeatureCollection",
        "features": [
            _region_feature("europe", None, -12, 34, 5, 62),
            _region_feature("portugal", "europe", -10, 36, -7.4, 43),
            _region_feature("spain", "europe", -7.4, 35, 4, 44),
        ],
    }
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "index-v1.json"), "w") as index_file:
        json.dump(index, index_file)
    yield directory
    EXTRACTS._leaf_regions.cache = None


class TestCoveringRegions:
    def test_interior_tile_maps_to_one_leaf(self, store):
        regions = EXTRACTS.covering_regions((38, -9.5, 39, -8.5))
        assert [region_id for (region_id, _u) in regions] == ["portugal"]

    def test_border_tile_maps_to_both_leaves(self, store):
        regions = EXTRACTS.covering_regions((37, -8, 38, -7))
        assert sorted(region_id for (region_id, _u) in regions) \
            == ["portugal", "spain"]

    def test_parent_region_is_never_selected(self, store):
        regions = EXTRACTS.covering_regions((38, -9.5, 39, -8.5))
        assert "europe" not in [region_id for (region_id, _u) in regions]

    def test_uncovered_ocean_returns_none(self, store):
        assert EXTRACTS.covering_regions((20, -40, 21, -39)) is None

    def test_largely_uncovered_bbox_returns_none(self, store):
        # Straddles the index's western edge with a ~67 percent hole:
        # a residue this large means a missing region, not ocean.
        assert EXTRACTS.covering_regions((38, -11, 39, -9.5)) is None

    def test_small_ocean_residue_still_serves(self, store):
        # Pokes ~6 percent of pure ocean beyond the index (the Strait
        # of Gibraltar margined-query case): no leaf covers it, so the
        # extracts are complete for every feature that can exist.
        regions = EXTRACTS.covering_regions((36, -10.05, 37, -9.2))
        assert regions is not None
        assert [region_id for (region_id, _u) in regions] == ["portugal"]

    def test_no_index_returns_none(self, store):
        os.remove(os.path.join(store, "index-v1.json"))
        EXTRACTS._leaf_regions.cache = None
        assert EXTRACTS.covering_regions((38, -9.5, 39, -8.5)) is None


class TestWantedList:
    def test_record_merges_and_deduplicates(self, store):
        EXTRACTS.record_wanted_regions(["portugal"])
        EXTRACTS.record_wanted_regions(["spain", "portugal"])
        wanted = EXTRACTS._read_json(
            os.path.join(store, "wanted.json"))
        assert wanted == ["portugal", "spain"]

    def test_consume_clears(self, store):
        EXTRACTS.record_wanted_regions(["portugal"])
        assert EXTRACTS._consume_wanted_regions() == ["portugal"]
        assert EXTRACTS._consume_wanted_regions() == []


class TestEntryPoint:
    BBOX = (38, -9.5, 39, -8.5)

    def test_missing_extract_queues_and_falls_back(self, store):
        result = EXTRACTS.osm_xml_from_local_extracts(
            ['way["natural"="water"]'], self.BBOX)
        assert result is None
        wanted = EXTRACTS._read_json(os.path.join(store, "wanted.json"))
        assert wanted == ["portugal"]

    def test_present_extract_serves_locally(self, store, monkeypatch):
        with open(EXTRACTS._region_file("portugal"), "wb") as pbf:
            pbf.write(b"pbf")
        sentinel = b"<osm/>"
        import types
        fake_filter = types.SimpleNamespace(
            filter_extracts_to_osm_xml=lambda paths, statements, bbox:
                sentinel)
        monkeypatch.setitem(
            sys.modules, "O4_OSM_Extract_Filter", fake_filter)
        result = EXTRACTS.osm_xml_from_local_extracts(
            ['way["natural"="water"]'], self.BBOX)
        assert result == sentinel

    def test_disabled_backend_returns_none(self, store, monkeypatch):
        monkeypatch.setattr(EXTRACTS, "extracts_enabled", lambda: False)
        assert EXTRACTS.osm_xml_from_local_extracts(
            ['way["natural"="water"]'], self.BBOX) is None

    def test_uncovered_bbox_never_queues(self, store):
        assert EXTRACTS.osm_xml_from_local_extracts(
            ['way["natural"="water"]'], (20, -40, 21, -39)) is None
        assert EXTRACTS._read_json(
            os.path.join(store, "wanted.json")) in (None, [])


class TestRefreshPolicy:
    def test_stale_and_missing_extracts_are_flagged(self, store,
                                                    monkeypatch):
        monkeypatch.setattr(
            EXTRACTS, "_extract_refresh_days", lambda: 14.0)
        now = time.time()
        state = {
            "fresh": {"downloaded_at": now - 86400, "url": "u1"},
            "stale": {"downloaded_at": now - 30 * 86400, "url": "u2"},
            "gone": {"downloaded_at": now - 86400, "url": "u3"},
        }
        EXTRACTS._write_json_atomic(
            os.path.join(store, "state.json"), state)
        for region_id in ("fresh", "stale"):
            with open(EXTRACTS._region_file(region_id), "wb") as pbf:
                pbf.write(b"pbf")
        stale = dict(EXTRACTS._regions_to_refresh())
        assert set(stale) == {"stale", "gone"}


class TestOverpassWiring:
    """The extract backend substitutes at the single get_overpass_data
    call site; Overpass is never contacted when extracts serve."""

    # One element per line: OSM_layer.update_dicosm is a line parser.
    XML = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<osm version="0.6" generator="test">\n'
        b'<node id="1" lat="38.5" lon="-9.1" version="1"/>\n'
        b'<node id="2" lat="38.5" lon="-9.0" version="1"/>\n'
        b'<way id="10" version="1">\n'
        b'<nd ref="1"/>\n'
        b'<nd ref="2"/>\n'
        b'<tag k="natural" v="water"/>\n'
        b'</way>\n'
        b'</osm>\n'
    )

    def test_extract_response_feeds_layer_and_cache(
            self, store, tmp_path, monkeypatch):
        import O4_OSM_Utils as OSM

        monkeypatch.setattr(
            EXTRACTS, "osm_xml_from_local_extracts",
            lambda statements, bbox, request_description="": self.XML)

        def _never(*args, **kwargs):
            raise AssertionError("Overpass must not be contacted")

        monkeypatch.setattr(OSM, "get_overpass_data", _never)
        (tmp_path / "cache").mkdir()
        cache_file = str(tmp_path / "cache" / "+38-010_water.osm.bz2")
        monkeypatch.setattr(
            OSM.FNAMES, "osm_cached",
            lambda lat, lon, suffix: cache_file)
        layer = OSM.OSM_layer()
        ok = OSM.OSM_queries_to_OSM_layer(
            ['way["natural"="water"]'], layer, 38, -10,
            ["name"], cached_suffix="water", cache_schema="test-1")
        assert ok == 1
        # The parser remaps OSM ids to internal ones: assert structure.
        assert len(layer.dicosmfirst["w"]) == 1
        wayid = next(iter(layer.dicosmfirst["w"]))
        assert layer.dicosmtags["w"][wayid]["natural"] == "water"
        assert len(layer.dicosmw[wayid]) == 2
        assert os.path.isfile(cache_file)
        # The written cache carries the schema marker, so recycling works.
        assert OSM._cached_osm_schema_matches(cache_file, "test-1")


class TestDownloadCancellation:
    """Pressing Stop must silence extract downloads promptly: the chunk
    loop aborts, the partial file is removed, and the region is
    re-queued as wanted so a later rescan retries it."""

    class _FakeResponse:
        def __init__(self, red_flag_after):
            self._red_flag_after = red_flag_after

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, _chunk_bytes):
            import O4_UI_Utils as UI

            for index in range(100):
                if index == self._red_flag_after:
                    UI.red_flag = True
                yield b"x" * 1024

    def test_red_flag_aborts_removes_partial_and_requeues(
            self, store, monkeypatch):
        import O4_UI_Utils as UI

        monkeypatch.setattr(
            EXTRACTS.requests, "get",
            lambda url, stream, timeout: self._FakeResponse(
                red_flag_after=3))
        monkeypatch.setattr(UI, "red_flag", False)
        ok = EXTRACTS._download_extract(
            "portugal", "https://example.invalid/portugal.pbf")
        assert ok is False
        assert not os.path.isfile(
            EXTRACTS._region_file("portugal") + ".tmp")
        assert not os.path.isfile(EXTRACTS._region_file("portugal"))
        with open(os.path.join(store, "wanted.json")) as wanted_file:
            assert json.load(wanted_file) == ["portugal"]

    def test_without_red_flag_download_completes(self, store, monkeypatch):
        import O4_UI_Utils as UI

        monkeypatch.setattr(
            EXTRACTS.requests, "get",
            lambda url, stream, timeout: self._FakeResponse(
                red_flag_after=None))
        monkeypatch.setattr(UI, "red_flag", False)
        ok = EXTRACTS._download_extract(
            "portugal", "https://example.invalid/portugal.pbf")
        assert ok is True
        assert os.path.isfile(EXTRACTS._region_file("portugal"))

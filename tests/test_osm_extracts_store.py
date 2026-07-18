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

# A minimal byte string that passes the store's pbf content probe
# (the real format opens with a header blob naming "OSMHeader").
_PBF_HEADER = b"\x00\x00\x00\x0e\x0a\x09OSMHeader\x18\xb0\x01"


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
    # and spain are adjacent leaves meeting at lon -7.4.  iberia is an
    # AGGREGATE that also passes the leaf test because nothing declares
    # it as a parent — the real index does exactly this (every United
    # States state's parent is north-america, so the 11 GB "us"
    # aggregate reads as a leaf); it duplicates portugal + spain and
    # additionally covers a southern band (lat 34-36 west of -7.4)
    # that neither of them reaches.
    index = {
        "type": "FeatureCollection",
        "features": [
            _region_feature("europe", None, -12, 34, 5, 62),
            _region_feature("portugal", "europe", -10, 36, -7.4, 43),
            _region_feature("spain", "europe", -7.4, 35, 4, 44),
            _region_feature("iberia", "europe", -10, 34, 4, 44),
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

    def test_duplicate_aggregate_leaf_is_pruned(self, store):
        # The border tile is fully served by portugal + spain; the
        # iberia aggregate covering both must not be selected on top
        # (the CYXY 2026-07-18 stall: "us" + "us-pacific" selected
        # alongside "us/alaska").
        regions = EXTRACTS.covering_regions((37, -8, 38, -7))
        assert regions is not None
        assert sorted(region_id for (region_id, _u) in regions) \
            == ["portugal", "spain"]

    def test_aggregate_kept_where_it_alone_covers(self, store):
        # Only iberia reaches the lat 34-36 band west of lon -7.4, so
        # pruning must keep it there — and drop the finer leaves it
        # duplicates.
        regions = EXTRACTS.covering_regions((35, -9, 36, -8))
        assert regions is not None
        assert [region_id for (region_id, _u) in regions] == ["iberia"]

    def test_touching_neighbour_without_contribution_is_pruned(
            self, store):
        # The bbox's eastern edge sits exactly on the portugal/spain
        # border: spain intersects it as a line, contributes no area,
        # and must not drag a whole extra extract into every query.
        regions = EXTRACTS.covering_regions((38, -8.4, 39, -7.4))
        assert regions is not None
        assert [region_id for (region_id, _u) in regions] == ["portugal"]


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


class TestLocalCoverPredicate:
    BBOX = (38, -9.5, 39, -8.5)

    def test_covered_when_extract_stored(self, store):
        with open(EXTRACTS._region_file("portugal"), "wb") as pbf:
            pbf.write(_PBF_HEADER)
        assert EXTRACTS.local_extracts_cover(self.BBOX) is True

    def test_not_covered_when_extract_missing(self, store):
        assert EXTRACTS.local_extracts_cover(self.BBOX) is False

    def test_not_covered_outside_index(self, store):
        assert EXTRACTS.local_extracts_cover((20, -40, 21, -39)) is False

    def test_not_covered_when_disabled(self, store, monkeypatch):
        with open(EXTRACTS._region_file("portugal"), "wb") as pbf:
            pbf.write(_PBF_HEADER)
        monkeypatch.setattr(EXTRACTS, "extracts_enabled", lambda: False)
        assert EXTRACTS.local_extracts_cover(self.BBOX) is False

    def test_border_tile_needs_both_extracts(self, store):
        border_box = (37, -8, 38, -7)
        with open(EXTRACTS._region_file("portugal"), "wb") as pbf:
            pbf.write(_PBF_HEADER)
        assert EXTRACTS.local_extracts_cover(border_box) is False
        with open(EXTRACTS._region_file("spain"), "wb") as pbf:
            pbf.write(_PBF_HEADER)
        assert EXTRACTS.local_extracts_cover(border_box) is True


class TestPbfContentValidation:
    """A poisoned store entry (an HTML page served with HTTP 200 and
    saved as a .pbf — the live enfield.osm.pbf, 2026-07-17) must be
    detected, deleted, and treated as missing so every consumer falls
    back to Overpass instead of erroring on it forever."""

    BBOX = (38, -9.5, 39, -8.5)
    HTML = b"<!DOCTYPE html>\n<html><body>not found</body></html>"

    def test_poisoned_file_reads_as_missing_and_is_deleted(self, store):
        path = EXTRACTS._region_file("portugal")
        with open(path, "wb") as fake_pbf:
            fake_pbf.write(self.HTML)
        assert EXTRACTS.local_extracts_cover(self.BBOX) is False
        assert not os.path.isfile(path), "the poisoned file is removed"

    def test_poisoned_file_requeues_and_falls_back(self, store):
        with open(EXTRACTS._region_file("portugal"), "wb") as fake_pbf:
            fake_pbf.write(self.HTML)
        result = EXTRACTS.osm_xml_from_local_extracts(
            ['way["natural"="water"]'], self.BBOX)
        assert result is None
        wanted = EXTRACTS._read_json(os.path.join(store, "wanted.json"))
        assert wanted == ["portugal"]

    def test_download_rejects_non_pbf_payload(self, store, monkeypatch):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, _chunk_bytes):
                yield TestPbfContentValidation.HTML

        import types
        monkeypatch.setattr(
            EXTRACTS, "requests",
            types.SimpleNamespace(get=lambda *a, **k: _FakeResponse()))
        assert EXTRACTS._download_extract(
            "portugal", "https://example.invalid/portugal.pbf") is False
        assert not os.path.isfile(EXTRACTS._region_file("portugal"))
        state = EXTRACTS._read_json(os.path.join(store, "state.json"))
        assert not state or "portugal" not in state

    def test_download_accepts_real_pbf_payload(self, store, monkeypatch):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, _chunk_bytes):
                yield _PBF_HEADER

        import types
        monkeypatch.setattr(
            EXTRACTS, "requests",
            types.SimpleNamespace(get=lambda *a, **k: _FakeResponse()))
        assert EXTRACTS._download_extract(
            "portugal", "https://example.invalid/portugal.pbf") is True
        assert os.path.isfile(EXTRACTS._region_file("portugal"))


class TestEntryPoint:
    BBOX = (38, -9.5, 39, -8.5)

    def test_missing_extract_queues_and_falls_back(self, store, monkeypatch):
        # Lazy mode (foreground download off): record wanted, use Overpass.
        monkeypatch.setattr(
            EXTRACTS, "foreground_download_enabled", lambda: False)
        result = EXTRACTS.osm_xml_from_local_extracts(
            ['way["natural"="water"]'], self.BBOX)
        assert result is None
        wanted = EXTRACTS._read_json(os.path.join(store, "wanted.json"))
        assert wanted == ["portugal"]

    def test_present_extract_serves_locally(self, store, monkeypatch):
        with open(EXTRACTS._region_file("portugal"), "wb") as pbf:
            pbf.write(_PBF_HEADER)
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

    def test_multi_box_unions_regions_and_serves_one_pass(
        self, store, monkeypatch
    ):
        for region in ("portugal", "spain"):
            with open(EXTRACTS._region_file(region), "wb") as pbf:
                pbf.write(_PBF_HEADER)
        captured = {}
        import types
        fake_filter = types.SimpleNamespace(
            filter_extracts_to_osm_xml=lambda paths, statements, bbox:
                captured.update(paths=list(paths), bbox=bbox) or b"<osm/>")
        monkeypatch.setitem(
            sys.modules, "O4_OSM_Extract_Filter", fake_filter)
        # One box in portugal, one in spain: the region union carries both
        # extracts and the filter receives the full LIST of boxes.
        boxes = [(38, -9.5, 39, -8.5), (40, 0.5, 41, 1.5)]
        result = EXTRACTS.osm_xml_from_local_extracts(
            ['way["building"]'], boxes)
        assert result == b"<osm/>"
        assert captured["bbox"] == [tuple(box) for box in boxes]
        assert sorted(
            os.path.basename(path) for path in captured["paths"]
        ) == ["portugal.osm.pbf", "spain.osm.pbf"]

    def test_multi_box_shared_region_deduplicates(self, store, monkeypatch):
        with open(EXTRACTS._region_file("portugal"), "wb") as pbf:
            pbf.write(_PBF_HEADER)
        captured = {}
        import types
        fake_filter = types.SimpleNamespace(
            filter_extracts_to_osm_xml=lambda paths, statements, bbox:
                captured.update(paths=list(paths)) or b"<osm/>")
        monkeypatch.setitem(
            sys.modules, "O4_OSM_Extract_Filter", fake_filter)
        # Two disjoint boxes inside the SAME region: one extract, once.
        boxes = [(38, -9.5, 38.4, -9.0), (38.6, -9.5, 39, -9.0)]
        assert EXTRACTS.osm_xml_from_local_extracts(
            ['way["building"]'], boxes) == b"<osm/>"
        assert [
            os.path.basename(path) for path in captured["paths"]
        ] == ["portugal.osm.pbf"]

    def test_multi_box_any_uncovered_box_falls_back(self, store):
        # Second box is open ocean: the whole batched request reads as
        # not extract-servable (the caller then goes per-box/Overpass).
        boxes = [(38, -9.5, 39, -8.5), (20, -40, 21, -39)]
        assert EXTRACTS.osm_xml_from_local_extracts(
            ['way["building"]'], boxes) is None


class TestRefreshPolicy:
    def test_stale_extracts_are_flagged_deleted_ones_forgotten(
            self, store, monkeypatch):
        """An aged extract is refreshed; a deleted one is treated as a
        deliberate user deletion — its state entry is dropped and it is
        never re-downloaded (covering-region pruning makes big
        aggregates like us.osm.pbf obsolete)."""
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
                pbf.write(_PBF_HEADER)
        stale = dict(EXTRACTS._regions_to_refresh())
        assert set(stale) == {"stale"}
        remaining = EXTRACTS._read_json(
            os.path.join(store, "state.json"))
        assert set(remaining) == {"fresh", "stale"}

    def test_deleted_stale_extract_is_forgotten_not_refreshed(
            self, store, monkeypatch):
        """Deletion wins even when the entry is also past the refresh
        age — the file's absence is the user's decision."""
        monkeypatch.setattr(
            EXTRACTS, "_extract_refresh_days", lambda: 14.0)
        state = {
            "gone_and_old": {
                "downloaded_at": time.time() - 30 * 86400, "url": "u1"},
        }
        EXTRACTS._write_json_atomic(
            os.path.join(store, "state.json"), state)
        assert EXTRACTS._regions_to_refresh() == []
        assert EXTRACTS._read_json(
            os.path.join(store, "state.json")) == {}


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

            # A pbf-shaped first chunk: the completed-download path now
            # content-validates before installing.
            yield _PBF_HEADER
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


class TestForegroundDownload:
    """First build in a region downloads the extract NOW instead of
    falling back to Overpass (owner ruling 2026-07-18)."""

    BBOX = (38, -9.5, 39, -8.5)
    SENTINEL = b"<osm-foreground/>"

    @pytest.fixture(autouse=True)
    def _foreground_environment(self, store, monkeypatch):
        import types

        import O4_UI_Utils as UI

        monkeypatch.setattr(UI, "red_flag", False)
        monkeypatch.setattr(EXTRACTS, "FOREGROUND_POLL_SECONDS", 0.01)
        monkeypatch.setitem(
            sys.modules, "O4_OSM_Extract_Filter",
            types.SimpleNamespace(
                filter_extracts_to_osm_xml=lambda paths, statements, bbox:
                    self.SENTINEL))
        self.store = store

    def _fake_successful_downloader(self, calls):
        def _download(region_id, url, foreground=False):
            calls.append((region_id, foreground))
            with open(EXTRACTS._region_file(region_id), "wb") as pbf:
                pbf.write(_PBF_HEADER)
            return True
        return _download

    def test_missing_extract_downloads_foreground_and_serves(
            self, monkeypatch):
        calls = []
        monkeypatch.setattr(EXTRACTS, "_download_extract",
                            self._fake_successful_downloader(calls))
        result = EXTRACTS.osm_xml_from_local_extracts(
            ['way["building"]'], self.BBOX)
        assert result == self.SENTINEL
        assert calls == [("portugal", True)]
        wanted = EXTRACTS._read_json(os.path.join(self.store, "wanted.json"))
        assert wanted in (None, [])        # nothing left for maintenance

    def test_download_failure_falls_back_to_overpass_and_queues(
            self, monkeypatch):
        monkeypatch.setattr(
            EXTRACTS, "_download_extract",
            lambda region_id, url, foreground=False: False)
        result = EXTRACTS.osm_xml_from_local_extracts(
            ['way["building"]'], self.BBOX)
        assert result is None
        wanted = EXTRACTS._read_json(os.path.join(self.store, "wanted.json"))
        assert wanted == ["portugal"]

    def test_red_flag_skips_foreground_download(self, monkeypatch):
        import O4_UI_Utils as UI

        monkeypatch.setattr(UI, "red_flag", True)

        def _must_not_download(region_id, url, foreground=False):
            raise AssertionError("no download may start under red_flag")
        monkeypatch.setattr(EXTRACTS, "_download_extract",
                            _must_not_download)
        assert EXTRACTS.osm_xml_from_local_extracts(
            ['way["building"]'], self.BBOX) is None

    def test_waits_for_live_concurrent_downloader(self, monkeypatch):
        import threading

        def _must_not_download(region_id, url, foreground=False):
            raise AssertionError(
                "a live concurrent download must be awaited, not raced")
        monkeypatch.setattr(EXTRACTS, "_download_extract",
                            _must_not_download)
        # A FRESH sibling .tmp = another process is streaming this region.
        concurrent_tmp = EXTRACTS._region_file("portugal") + ".tmp-9999-1"
        with open(concurrent_tmp, "wb") as tmp_file:
            tmp_file.write(b"partial")

        def _finish_download():
            time.sleep(0.05)
            with open(EXTRACTS._region_file("portugal"), "wb") as pbf:
                pbf.write(_PBF_HEADER)
        finisher = threading.Thread(target=_finish_download)
        finisher.start()
        try:
            result = EXTRACTS.osm_xml_from_local_extracts(
                ['way["building"]'], self.BBOX)
        finally:
            finisher.join()
        assert result == self.SENTINEL

    def test_stale_tmp_is_ignored_removed_and_download_proceeds(
            self, monkeypatch):
        calls = []
        monkeypatch.setattr(EXTRACTS, "_download_extract",
                            self._fake_successful_downloader(calls))
        stale_tmp = EXTRACTS._region_file("portugal") + ".tmp-1234-5"
        with open(stale_tmp, "wb") as tmp_file:
            tmp_file.write(b"crashed download residue")
        stale_by = EXTRACTS.CONCURRENT_TMP_FRESH_SECONDS + 60
        os.utime(stale_tmp, (time.time() - stale_by, time.time() - stale_by))
        result = EXTRACTS.osm_xml_from_local_extracts(
            ['way["building"]'], self.BBOX)
        assert result == self.SENTINEL
        assert calls == [("portugal", True)]
        assert not os.path.exists(stale_tmp)

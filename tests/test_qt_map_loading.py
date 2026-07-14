"""Headless tests for the live map's progressive, cancellable tile loading.

Verifies the Google-Maps-style contract:
- pyramid fill: coarse levels are queued before the view's actual zoom level
- the base world layer is always part of the wanted set (instant fallback)
- moving the view rebuilds the queue and drops out-of-view tiles (cancel)
- a worker abandons a dequeued tile that is no longer wanted (no fetch)
- a wanted tile fetches, lands in the scene, and renders above coarse tiles
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import O4_Imagery_Utils as IMG  # noqa: E402
import O4_Qt_Map as QTMAP  # noqa: E402

FAKE = "FAKETMS"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def view(qapp, tmp_path, monkeypatch):
    monkeypatch.setitem(
        IMG.providers_dict,
        FAKE,
        {
            "code": FAKE,
            "grid_type": "webmercator",
            "request_type": "tms",
            "max_zl": 18,
            "url_template": "http://invalid.test/{zoom}/{x}/{y}",
        },
    )
    monkeypatch.setattr(
        QTMAP, "livemap_cache_dir", lambda: str(tmp_path / "livemap")
    )
    v = QTMAP.MapView()
    v.resize(800, 600)
    # Keep workers off so tests can inspect the queue deterministically.
    v._start_workers = lambda: None
    v.set_provider(FAKE)
    v._update_timer.stop()
    yield v
    v.deleteLater()


def fetch_recorder(monkeypatch, calls):
    from PIL import Image

    def fake_fetch(z, x, y, provider, session):
        calls.append((provider["code"], z, x, y))
        return 1, Image.new("RGB", (256, 256), (40, 90, 60))

    monkeypatch.setattr(IMG, "get_wmts_image", fake_fetch)


def settle(view, lat, lon, zoom):
    view.center_on_tile(lat, lon, zoom=zoom)
    view._update_timer.stop()
    view._refresh_tiles()


def queued(view):
    with view._fetch_lock:
        return list(view._fetch_queue)


def test_pyramid_coarse_first_and_base_layer(view):
    settle(view, 48, -6, zoom=9)
    q = queued(view)
    assert q, "queue should not be empty after refresh"
    zs = [key[1] for key in q]
    assert zs == sorted(zs), "coarse levels must be queued before fine ones"
    assert zs[0] == QTMAP.BASE_ZL, "base world layer queues first"
    assert max(zs) == view._fetch_zoom(), "finest level matches view zoom"
    base_count = 4 ** QTMAP.BASE_ZL
    with view._fetch_lock:
        base_wanted = [k for k in view._wanted if k[1] == QTMAP.BASE_ZL]
    assert len(base_wanted) == base_count, "whole world wanted at base ZL"


def test_view_move_cancels_out_of_view_tiles(view):
    settle(view, 48, -6, zoom=10)
    old_fine = {k for k in queued(view) if k[1] == view._fetch_zoom()}
    assert old_fine
    settle(view, -34, 151, zoom=10)  # other side of the planet
    new_queue = set(queued(view))
    with view._fetch_lock:
        wanted = set(view._wanted)
    assert not (old_fine & new_queue), "old fine tiles must leave the queue"
    assert not (old_fine & wanted), "old fine tiles must leave the wanted set"


def test_worker_abandons_unwanted_tile(view, monkeypatch):
    calls = []
    fetch_recorder(monkeypatch, calls)
    settle(view, 48, -6, zoom=10)
    unwanted = (FAKE, 10, 0, 0)  # far from the visible range
    with view._fetch_lock:
        assert unwanted not in view._wanted
    view._fetch_tile(*unwanted)
    assert calls == [], "cancelled tile must not hit the network"


def test_wanted_tile_fetches_and_lands_in_scene(view, qapp, monkeypatch):
    calls = []
    fetch_recorder(monkeypatch, calls)
    settle(view, 48, -6, zoom=9)
    key = queued(view)[0]
    view._fetch_tile(*key)
    assert calls and calls[0][0] == FAKE
    for _ in range(20):
        qapp.processEvents()
        if key in view._tiles:
            break
    assert key in view._tiles, "fetched tile must appear in the scene"
    item = view._tiles[key]
    assert item.zValue() == key[1], "finer tiles must stack above coarse"
    cache = view._cache_path(*key)
    assert os.path.isfile(cache), "fetched tile must be disk-cached"


def test_base_layer_survives_pruning(view):
    settle(view, 48, -6, zoom=9)

    class _FakeItem:
        def __init__(self):
            self.removed = False

    removed = []

    def fake_drop(key):
        removed.append(key)
        view._tiles.pop(key, None)
        view._tile_age.pop(key, None)

    view._drop_tile = fake_drop
    # Fill far beyond MAX_ITEMS with fake fine tiles + a base tile.
    base_key = (FAKE, QTMAP.BASE_ZL, 0, 0)
    view._tiles[base_key] = _FakeItem()
    view._tile_age[base_key] = 0  # oldest of all
    for i in range(QTMAP.MAX_ITEMS + 50):
        key = (FAKE, 10, i, 0)
        view._tiles[key] = _FakeItem()
        view._tile_age[key] = i + 1
    view._prune_tiles()
    assert base_key not in removed, "base world tiles must never be pruned"
    assert len(view._tiles) <= QTMAP.MAX_ITEMS + 1


# ----------------------------------------------------------------------
# Shared tile cache: builds reuse what the map downloaded
# ----------------------------------------------------------------------

def test_build_fetch_reuses_map_cache(view, tmp_path, monkeypatch):
    """get_wmts_image (the build pipeline's tile fetcher) must serve a tile
    the map already cached, without touching the network."""
    from PIL import Image

    monkeypatch.setattr(
        IMG, "shared_tile_cache_dir", str(tmp_path / "livemap")
    )
    path = IMG.shared_tile_cache_path(FAKE, 18, 130000, 90000)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (256, 256), (1, 2, 3)).save(path, "JPEG")

    def network_forbidden(*args, **kwargs):
        raise AssertionError("cache hit must not reach the network")

    monkeypatch.setattr(IMG, "http_request_to_image", network_forbidden)
    success, image = IMG.get_wmts_image(
        18, 130000, 90000, IMG.providers_dict[FAKE], None
    )
    assert success == 1
    assert image.size == (256, 256)


def test_map_and_build_cache_layouts_agree(view, monkeypatch, tmp_path):
    monkeypatch.setattr(
        QTMAP, "livemap_cache_dir", lambda: str(tmp_path / "livemap")
    )
    monkeypatch.setattr(
        IMG, "shared_tile_cache_dir", str(tmp_path / "livemap")
    )
    assert view._cache_path(FAKE, 18, 12, 34) == IMG.shared_tile_cache_path(
        FAKE, 18, 12, 34
    )


def test_cache_miss_falls_through(monkeypatch, tmp_path):
    """With the shared cache enabled but empty, get_wmts_image proceeds to
    the normal fetch path."""
    from PIL import Image

    monkeypatch.setattr(
        IMG, "shared_tile_cache_dir", str(tmp_path / "empty")
    )
    calls = []

    def fake_http(width, height, url, headers, session):
        calls.append(url)
        return 1, Image.new("RGB", (256, 256), (9, 9, 9))

    monkeypatch.setattr(IMG, "http_request_to_image", fake_http)
    provider = {
        "code": "MISS",
        "grid_type": "webmercator",
        "request_type": "tms",
        "max_zl": 18,
        "url_template": "http://invalid.test/{zoom}/{x}/{y}",
        "tile_size": 256,
        # get_wmts_image evaluates these replacement args unconditionally
        "resolutions": {10: 1.0},
        "top_left_corner": {10: [0.0, 0.0]},
    }
    success, image = IMG.get_wmts_image(10, 1, 2, provider, None)
    assert success == 1 and calls, "miss must fall through to the fetch path"


def test_map_prefers_build_imagery_cache(view, qapp, tmp_path, monkeypatch):
    """A view tile covered by an assembled orthophoto in Orthophotos/ (the
    build pipeline's cache) must be cropped from it — no network fetch."""
    from PIL import Image
    import O4_File_Names as FNAMES
    import O4_Geo_Utils as GEO

    monkeypatch.setitem(IMG.providers_dict[FAKE], "imagery_dir", "grouped")
    monkeypatch.setattr(FNAMES, "Imagery_dir", str(tmp_path / "Orthophotos"))

    # A texture over Brittany at ZL17: orthogrid-aligned origin.
    z = 17
    tx, ty = GEO.wgs84_to_orthogrid(48.5, -5.5, z)
    latc, lonc = GEO.gtile_to_wgs84(tx + 8, ty + 8, z)
    import math as m
    fdir = FNAMES.jpeg_file_dir_from_attributes(
        m.floor(latc), m.floor(lonc), z, IMG.providers_dict[FAKE]
    )
    os.makedirs(fdir, exist_ok=True)
    # 1024px texture -> 64px per view tile; a red patch marks the sub-tile
    # we request, proving the crop offset is right.
    big = Image.new("RGB", (1024, 1024), (10, 10, 10))
    red = Image.new("RGB", (64, 64), (200, 30, 30))
    dx, dy = 5, 9  # the sub-tile we will request
    big.paste(red, (dx * 64, dy * 64))
    big.save(
        os.path.join(
            fdir,
            FNAMES.jpeg_file_name_from_attributes(tx, ty, z, FAKE),
        ),
        "JPEG",
        quality=95,
    )

    def network_forbidden(*args, **kwargs):
        raise AssertionError("covered tile must not hit the network")

    monkeypatch.setattr(IMG, "get_wmts_image", network_forbidden)

    key = (FAKE, z, tx + dx, ty + dy)
    with view._fetch_lock:
        view._wanted = {key}
    view._fetch_tile(*key)
    for _ in range(20):
        qapp.processEvents()
        if key in view._tiles:
            break
    assert key in view._tiles, "cropped tile must land in the scene"
    crop = Image.open(view._cache_path(*key))
    r, g, b = crop.resize((1, 1)).getpixel((0, 0))
    assert r > 150 and g < 90 and b < 90, (
        "crop must come from the correct sub-region (got %s)" % ((r, g, b),)
    )

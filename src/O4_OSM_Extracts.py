"""Regional OpenStreetMap extracts: the local-first OSM data backend.

Serves the per-tile OSM layer caches from Geofabrik regional extracts
(daily ``.osm.pbf`` snapshots on a plain HTTPS CDN, no rate limits)
instead of querying public Overpass servers, which are shared query
infrastructure and throttle exactly the bulk-extraction workload a
batch tile build produces (docs/specs/osm-regional-extracts-spec.md).

Life cycle, designed so users never manage it by hand:

* Nothing downloads up front.  The first build touching a region
  RECORDS the region as wanted (``wanted.json``) and falls back to
  Overpass for that build; the background maintenance thread downloads
  the extract; later builds in the region are served locally.
* :func:`start_background_maintenance` (called once at application
  start — Qt or CLI, never by parallel-build worker children) refreshes
  the region index when stale, re-downloads extracts older than the
  ``osm_extract_refresh_days`` setting, and drains the wanted list on a
  rescan loop.  Worker children only ever append wants: a single
  downloader can never race itself over a multi-hundred-megabyte file.

Failure discipline: this backend is an accelerator, never a
dependency.  Every public entry point swallows its own errors and
returns ``None`` / no-ops, leaving the historic Overpass path exactly
as it was.  No GUI-toolkit imports (core-module rule).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Optional

import requests

import O4_File_Names as FNAMES
import O4_UI_Utils as UI

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
INDEX_REFRESH_DAYS = 7.0
DEFAULT_EXTRACT_REFRESH_DAYS = 14.0
WANTED_RESCAN_SECONDS = 60.0
DOWNLOAD_CHUNK_BYTES = 1 << 20
DOWNLOAD_PROGRESS_EVERY_BYTES = 50 << 20
HTTP_TIMEOUT_SECONDS = 60

# Module-level and mutable so tests monkeypatch it at a tmp_path.
STORE_DIRECTORY = os.path.join(FNAMES.OSM_dir, "_regional_extracts")

_maintenance_started = threading.Event()
_store_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def extracts_enabled() -> bool:
    """The ``osm_regional_extracts`` setting (True when unavailable).

    Read from ``O4_Config_Utils`` only when that module is ALREADY
    imported (it always is by build time — the Tile class lives there);
    importing it here would trigger its side effects (global config
    creation) from a passive check.
    """
    try:
        CFG = sys.modules.get("O4_Config_Utils")
        if CFG is None:
            return True
        return bool(getattr(CFG, "osm_regional_extracts", True))
    except Exception:
        return True


def _extract_refresh_days() -> float:
    try:
        CFG = sys.modules.get("O4_Config_Utils")
        if CFG is None:
            return DEFAULT_EXTRACT_REFRESH_DAYS
        return float(getattr(
            CFG, "osm_extract_refresh_days", DEFAULT_EXTRACT_REFRESH_DAYS))
    except Exception:
        return DEFAULT_EXTRACT_REFRESH_DAYS


# ---------------------------------------------------------------------------
# Store primitives (all JSON writes atomic: temp + os.replace)
# ---------------------------------------------------------------------------
def _store_path(*names: str) -> str:
    return os.path.join(STORE_DIRECTORY, *names)


def _read_json(path: str):
    try:
        with open(path) as json_file:
            return json.load(json_file)
    except Exception:
        return None


def _write_json_atomic(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = path + ".tmp"
    with open(temporary_path, "w") as json_file:
        json.dump(payload, json_file, indent=1)
    os.replace(temporary_path, path)


def _region_file(region_id: str) -> str:
    # Region ids may carry a path ("us/california"); flatten for storage.
    return _store_path(region_id.replace("/", "__") + ".osm.pbf")


def record_wanted_regions(region_ids) -> None:
    """Append regions to the wanted list (any process may call this)."""
    with _store_lock:
        wanted = _read_json(_store_path("wanted.json")) or []
        merged = sorted(set(wanted) | set(region_ids))
        if merged != sorted(wanted):
            _write_json_atomic(_store_path("wanted.json"), merged)


def _consume_wanted_regions() -> list:
    with _store_lock:
        wanted = _read_json(_store_path("wanted.json")) or []
        if wanted:
            _write_json_atomic(_store_path("wanted.json"), [])
    return list(wanted)


# ---------------------------------------------------------------------------
# Region index
# ---------------------------------------------------------------------------
def _index_path() -> str:
    return _store_path("index-v1.json")


def _index_is_stale() -> bool:
    try:
        age_seconds = time.time() - os.path.getmtime(_index_path())
        return age_seconds > INDEX_REFRESH_DAYS * 86400
    except OSError:
        return True


def _refresh_index() -> bool:
    try:
        UI.vprint(1, "   Refreshing the Geofabrik region index...")
        response = requests.get(INDEX_URL, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()          # validates before storing
        os.makedirs(STORE_DIRECTORY, exist_ok=True)
        temporary_path = _index_path() + ".tmp"
        with open(temporary_path, "w") as index_file:
            json.dump(payload, index_file)
        os.replace(temporary_path, _index_path())
        _leaf_regions.cache = None
        return True
    except Exception as error:
        UI.vprint(1, "   Geofabrik index refresh failed:", str(error))
        return False


def _leaf_regions() -> Optional[list]:
    """[(region_id, pbf_url, shapely geometry)] for index leaves, cached.

    Leaves are regions that are no other region's parent — the finest
    partition Geofabrik offers (states where they exist, countries
    elsewhere).  ``None`` when no index is stored yet.
    """
    cached = getattr(_leaf_regions, "cache", None)
    if cached is not None:
        return cached
    index = _read_json(_index_path())
    if not index:
        return None
    try:
        from shapely.geometry import shape

        features = index.get("features", [])
        parents = {
            (feature.get("properties") or {}).get("parent")
            for feature in features
        }
        leaves = []
        for feature in features:
            properties = feature.get("properties") or {}
            region_id = properties.get("id")
            pbf_url = (properties.get("urls") or {}).get("pbf")
            if not region_id or not pbf_url or region_id in parents:
                continue
            try:
                region_geometry = shape(feature["geometry"])
            except Exception:
                continue
            leaves.append((region_id, pbf_url, region_geometry))
        _leaf_regions.cache = leaves
        return leaves
    except Exception:
        return None


def covering_regions(bounding_box) -> Optional[list]:
    """[(region_id, pbf_url)] of leaves covering the bbox, or ``None``.

    ``bounding_box`` is (lat_min, lon_min, lat_max, lon_max).  ``None``
    means the tile is not extract-servable: no index yet, or the
    intersecting leaves do not jointly contain the bbox (open ocean,
    index gaps) — the caller keeps using Overpass.
    """
    leaves = _leaf_regions()
    if leaves is None:
        return None
    try:
        from shapely.geometry import box
        from shapely.ops import unary_union

        (lat_min, lon_min, lat_max, lon_max) = bounding_box
        bbox_polygon = box(lon_min, lat_min, lon_max, lat_max)
        intersecting = [
            (region_id, pbf_url, region_geometry)
            for (region_id, pbf_url, region_geometry) in leaves
            if region_geometry.intersects(bbox_polygon)
        ]
        if not intersecting:
            return None
        union = unary_union(
            [region_geometry for (_i, _u, region_geometry) in intersecting]
        )
        # Residue the leaves do not cover is OPEN OCEAN by construction
        # (Geofabrik regions jointly cover all land, with sea buffers),
        # and no extract exists for it — so a small residue must not
        # disqualify local serving (the Strait of Gibraltar tile's
        # margined queries poke ~0.02 deg2 of Atlantic).  A LARGE
        # residue means a hole in the index (a region missing) — keep
        # the Overpass fallback there rather than silently losing data.
        uncovered = bbox_polygon.difference(union.buffer(0.01))
        if uncovered.area > 0.10 * bbox_polygon.area:
            return None
        return [(region_id, pbf_url) for (region_id, pbf_url, _g)
                in intersecting]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The build-time entry point
# ---------------------------------------------------------------------------
# A pbf file's first blob is its header blob: a four-byte length, then
# a BlobHeader whose type string "OSMHeader" sits within the first few
# dozen bytes.  An HTML "not found" page served with HTTP 200 — the
# live enfield.osm.pbf case, 2026-07-17: the Geofabrik index lists
# regions whose download address serves a web page — has neither.
_PBF_MAGIC_PROBE_BYTES = 64


def _file_looks_like_pbf(path: str) -> bool:
    try:
        with open(path, "rb") as pbf_file:
            head = pbf_file.read(_PBF_MAGIC_PROBE_BYTES)
        return b"OSMHeader" in head
    except OSError:
        return False


def _stored_regions_missing(regions) -> list:
    """Region ids not usable from the store.

    Absent files are missing; a present file that is not actually pbf
    data (a poisoned download from before content validation) is
    DELETED on sight and reported missing, so the wanted/maintenance
    path can retry and every consumer falls back to Overpass instead
    of erroring on it forever.
    """
    missing = []
    for (region_id, _url) in regions:
        path = _region_file(region_id)
        if not os.path.isfile(path):
            missing.append(region_id)
            continue
        if not _file_looks_like_pbf(path):
            UI.vprint(
                1,
                "      Stored OSM extract", region_id,
                "is not valid pbf data; removing it.",
            )
            try:
                os.remove(path)
            except OSError:
                pass
            missing.append(region_id)
    return missing


def local_extracts_cover(bounding_box) -> bool:
    """True when every region covering the box is stored locally.

    An OSM request for such a box is served entirely from the local
    extracts — no Overpass involvement — so callers whose only purpose
    is sparing the Overpass servers (the parallel-run cache warmer)
    have nothing to do for it.  Never raises; any failure reads as
    "not covered".
    """
    try:
        if not extracts_enabled():
            return False
        regions = covering_regions(bounding_box)
        if regions is None:
            return False
        return not _stored_regions_missing(regions)
    except Exception:
        return False


def _bounding_boxes_list(bounding_box) -> list:
    """Normalize one ``(lat_min, lon_min, lat_max, lon_max)`` box or a
    list of such boxes into a list of boxes."""
    boxes = list(bounding_box)
    # Multi-box form: the first element is itself a box (a sequence),
    # not a coordinate scalar.  Sequence detection (rather than scalar
    # type checks) keeps numpy scalar coordinates classified correctly.
    if boxes and isinstance(boxes[0], (tuple, list)):
        return [tuple(box) for box in boxes]
    return [tuple(boxes)]


def osm_xml_from_local_extracts(statements, bounding_box,
                                request_description="") -> Optional[bytes]:
    """OSM XML bytes for the statements, served from local extracts.

    Drop-in stand-in for an Overpass response (same union + recursion
    semantics, see O4_OSM_Extract_Filter).  Returns ``None`` whenever
    the extracts cannot serve this request — backend disabled, region
    index absent, extracts not downloaded yet (they are recorded as
    wanted for the maintenance thread), or any failure — in which case
    the caller proceeds to Overpass exactly as before.

    ``bounding_box`` may also be a LIST of boxes: the pbf filtering cost
    is dominated by reading the whole extract file, so serving N disjoint
    areas in one filtering pass costs one read instead of N (the
    per-airport inset footprint queries batch this way).  Every box must
    be extract-servable, and each box's missing regions are queued for
    download exactly as in the single-box case.
    """
    try:
        if not extracts_enabled():
            return None
        boxes = _bounding_boxes_list(bounding_box)
        if not boxes:
            return None
        regions = []
        region_ids_seen = set()
        for box in boxes:
            box_regions = covering_regions(box)
            if box_regions is None:
                return None
            for region in box_regions:
                if region[0] not in region_ids_seen:
                    region_ids_seen.add(region[0])
                    regions.append(region)
        missing = _stored_regions_missing(regions)
        if missing:
            record_wanted_regions(missing)
            UI.vprint(
                1,
                "      Regional extract(s)",
                ", ".join(missing),
                "not stored yet; queued for background download —"
                " using Overpass this time.",
            )
            return None
        import O4_OSM_Extract_Filter as FILTER

        label = f" ({request_description})" if request_description else ""
        UI.vprint(
            1,
            f"      Filtering OSM data{label} from regional extract(s):",
            ", ".join(region_id for (region_id, _url) in regions),
        )
        return FILTER.filter_extracts_to_osm_xml(
            [_region_file(region_id) for (region_id, _url) in regions],
            statements,
            boxes,
        )
    except Exception as error:
        UI.vprint(
            1, "      Regional extract filtering failed",
            "(" + str(error) + "); using Overpass.",
        )
        return None


# ---------------------------------------------------------------------------
# Background maintenance (application process only)
# ---------------------------------------------------------------------------
def _download_extract(region_id: str, pbf_url: str) -> bool:
    """Stream one extract to the store (atomic; resumes are simple
    re-downloads — CDN throughput makes ranged resume not worth its
    edge cases)."""
    target = _region_file(region_id)
    temporary_path = target + ".tmp"
    try:
        UI.vprint(
            0,
            "   Downloading OSM regional extract", region_id,
            "in the background...",
        )
        received = 0
        next_report = DOWNLOAD_PROGRESS_EVERY_BYTES
        cancelled = False
        with requests.get(
            pbf_url, stream=True, timeout=HTTP_TIMEOUT_SECONDS
        ) as response:
            response.raise_for_status()
            os.makedirs(STORE_DIRECTORY, exist_ok=True)
            with open(temporary_path, "wb") as extract_file:
                for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
                    if UI.red_flag:
                        # The user pressed Stop: the network goes quiet
                        # NOW, background or not.  The region stays
                        # wanted, so the rescan loop retries once the
                        # flag clears (next build or next start).
                        cancelled = True
                        break
                    extract_file.write(chunk)
                    received += len(chunk)
                    if received >= next_report:
                        UI.vprint(
                            1,
                            "      ...", region_id,
                            "%d MB so far" % (received >> 20),
                        )
                        next_report += DOWNLOAD_PROGRESS_EVERY_BYTES
        if cancelled:
            os.remove(temporary_path)
            record_wanted_regions([region_id])
            UI.vprint(
                0,
                "   OSM regional extract download for", region_id,
                "stopped with the build; it will retry later.",
            )
            return False
        if not _file_looks_like_pbf(temporary_path):
            # Some indexed regions answer with an HTML page under HTTP
            # 200 (no extract published at that address).  Installing
            # it would poison the store: every later request covering
            # the region errors on it instead of using Overpass.
            os.remove(temporary_path)
            UI.vprint(
                0,
                "   The download for OSM regional extract", region_id,
                "returned something other than pbf data (no extract is"
                " published at its address); builds in this region use"
                " Overpass instead.",
            )
            return False
        os.replace(temporary_path, target)
        with _store_lock:
            state = _read_json(_store_path("state.json")) or {}
            state[region_id] = {
                "downloaded_at": time.time(), "url": pbf_url,
            }
            _write_json_atomic(_store_path("state.json"), state)
        UI.vprint(
            0,
            "   OSM regional extract", region_id,
            "ready (%d MB); future builds in this region skip Overpass."
            % (received >> 20),
        )
        return True
    except Exception as error:
        UI.vprint(
            1, "   Extract download for", region_id, "failed:", str(error),
        )
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        return False


def _regions_to_refresh() -> list:
    """[(region_id, url)] of stored extracts past the refresh age."""
    state = _read_json(_store_path("state.json")) or {}
    refresh_age_seconds = _extract_refresh_days() * 86400
    now = time.time()
    stale = []
    for region_id, entry in state.items():
        if not os.path.isfile(_region_file(region_id)):
            stale.append((region_id, entry.get("url")))
        elif now - float(entry.get("downloaded_at", 0)) \
                > refresh_age_seconds:
            stale.append((region_id, entry.get("url")))
    return stale


def _url_for_region(region_id: str) -> Optional[str]:
    for (leaf_id, pbf_url, _g) in (_leaf_regions() or []):
        if leaf_id == region_id:
            return pbf_url
    return None


def _maintenance_loop() -> None:
    if _index_is_stale():
        _refresh_index()
    for (region_id, pbf_url) in _regions_to_refresh():
        if UI.red_flag:
            # A stop is in flight: keep the network quiet.  Refreshes
            # resume at the next application start.
            break
        url = pbf_url or _url_for_region(region_id)
        if url:
            _download_extract(region_id, url)
    while True:
        # While a stop is in flight the wanted list is left untouched —
        # consuming it and aborting would drop the regions until some
        # later build re-recorded them.
        if not UI.red_flag:
            for region_id in _consume_wanted_regions():
                if os.path.isfile(_region_file(region_id)):
                    continue
                url = _url_for_region(region_id)
                if url is None:
                    # No index yet (first run): fetch it, then retry once.
                    if _refresh_index():
                        url = _url_for_region(region_id)
                if url:
                    _download_extract(region_id, url)
        time.sleep(WANTED_RESCAN_SECONDS)


def start_background_maintenance() -> None:
    """Start the extract maintenance thread (idempotent, never raises).

    Call once from the APPLICATION process (Qt window or CLI main) —
    never from parallel-build worker children, which only append wants.
    """
    try:
        if not extracts_enabled():
            return
        if _maintenance_started.is_set():
            return
        _maintenance_started.set()
        threading.Thread(
            target=_maintenance_loop,
            name="osm_extract_maintenance",
            daemon=True,
        ).start()
    except Exception:
        pass

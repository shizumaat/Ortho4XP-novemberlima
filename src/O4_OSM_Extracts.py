"""Regional OpenStreetMap extracts: the local-first OSM data backend.

Serves the per-tile OSM layer caches from Geofabrik regional extracts
(daily ``.osm.pbf`` snapshots on a plain HTTPS CDN, no rate limits)
instead of querying public Overpass servers, which are shared query
infrastructure and throttle exactly the bulk-extraction workload a
batch tile build produces (docs/specs/osm-regional-extracts-spec.md).

Life cycle, designed so users never manage it by hand:

* Nothing downloads up front.  The first build touching a region
  downloads its extract IN THE FOREGROUND (owner ruling 2026-07-18:
  the Geofabrik CDN serves a whole country in about the time ONE
  throttled Overpass query round takes — measured HECA: the lazy
  fallback spent 21 minutes on Overpass while egypt.osm.pbf landed 60
  seconds after the build needed it) and then serves the build locally.
  ``osm_extract_foreground_download=False`` restores the previous lazy
  behaviour: record the region as wanted (``wanted.json``), use
  Overpass this once, and let the maintenance thread download it.
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
DEFAULT_EXTRACT_REFRESH_DAYS = 180.0
WANTED_RESCAN_SECONDS = 60.0
DOWNLOAD_CHUNK_BYTES = 1 << 20
DOWNLOAD_PROGRESS_EVERY_BYTES = 50 << 20
HTTP_TIMEOUT_SECONDS = 60
# Foreground acquisition: how often a waiter re-checks for the extract
# while another downloader streams it, and how recent a sibling ``.tmp``
# must be to count as a live concurrent download (older = a crashed
# download's residue, safe to ignore and replace).
FOREGROUND_POLL_SECONDS = 2.0
CONCURRENT_TMP_FRESH_SECONDS = 30.0

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


def foreground_download_enabled() -> bool:
    """The ``osm_extract_foreground_download`` setting (True when
    unavailable); same passive ``sys.modules`` read as
    :func:`extracts_enabled`."""
    try:
        CFG = sys.modules.get("O4_Config_Utils")
        if CFG is None:
            return True
        return bool(getattr(CFG, "osm_extract_foreground_download", True))
    except Exception:
        return True


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


# Region-boundary tolerance for coverage tests, in degrees (~1 km):
# Geofabrik region polygons are simplified and sea-buffered, so
# adjacent regions' shared borders never coincide exactly.
_COVERAGE_BUFFER_DEGREES = 0.01
# A region whose unique contribution to the bbox is below this area
# (degrees squared, float-dust scale) duplicates the other selected
# regions and is pruned.
_REDUNDANT_COVER_AREA = 1e-9


def covering_regions(bounding_box) -> Optional[list]:
    """[(region_id, pbf_url)] of leaves covering the bbox, or ``None``.

    ``bounding_box`` is (lat_min, lon_min, lat_max, lon_max).  ``None``
    means the tile is not extract-servable: no index yet, or the
    intersecting leaves do not jointly contain the bbox (open ocean,
    index gaps) — the caller keeps using Overpass.

    The result is a MINIMAL cover, not every intersecting leaf: the
    Geofabrik index declares each United States state's parent as
    ``north-america`` rather than ``us``, so the aggregate ``us``
    extract (11 GB) and the grouping extracts (``us-pacific``, ...)
    pass the leaf test alongside the states that duplicate them (the
    same pattern covers ``great-britain`` over the English counties).
    Every selected region is read end-to-end on every query, so
    redundant covers are pruned, largest first: a region is kept only
    for bbox area no other kept region serves (a Whitehorse masks
    query, 2026-07-18, selected yukon + north-admreg + us/alaska AND
    the duplicate us + us-pacific — an eight-minute filtering stall
    and a 12 GB download for data the us/alaska extract already
    served).
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
        # All coverage arithmetic happens inside the bbox neighbourhood,
        # so clip once: region polygons carry whole coastlines the
        # repeated buffer/difference calls below must not chew through.
        bbox_neighbourhood = bbox_polygon.buffer(
            2 * _COVERAGE_BUFFER_DEGREES)
        clipped = {
            region_id: region_geometry.intersection(bbox_neighbourhood)
            for (region_id, _u, region_geometry) in intersecting
        }
        # Reverse-delete pruning, largest region first (geometry area is
        # the proxy for extract size), so aggregates fall before the
        # smaller regions that duplicate them.
        intersecting.sort(
            key=lambda entry: (-entry[2].area, entry[0]))
        kept = list(intersecting)
        for entry in intersecting:
            if len(kept) == 1:
                break
            (region_id, _url, _geometry) = entry
            others = unary_union([
                clipped[other_id]
                for (other_id, _u, _g) in kept if other_id != region_id
            ])
            unique = clipped[region_id].intersection(bbox_polygon) \
                .difference(others.buffer(_COVERAGE_BUFFER_DEGREES))
            if unique.area <= _REDUNDANT_COVER_AREA:
                kept.remove(entry)
        union = unary_union(
            [clipped[region_id] for (region_id, _u, _g) in kept]
        )
        # Residue the leaves do not cover is OPEN OCEAN by construction
        # (Geofabrik regions jointly cover all land, with sea buffers),
        # and no extract exists for it — so a small residue must not
        # disqualify local serving (the Strait of Gibraltar tile's
        # margined queries poke ~0.02 deg2 of Atlantic).  A LARGE
        # residue means a hole in the index (a region missing) — keep
        # the Overpass fallback there rather than silently losing data.
        uncovered = bbox_polygon.difference(
            union.buffer(_COVERAGE_BUFFER_DEGREES))
        if uncovered.area > 0.10 * bbox_polygon.area:
            return None
        return sorted(
            (region_id, pbf_url) for (region_id, pbf_url, _g) in kept
        )
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


_region_download_locks: dict = {}
_region_download_locks_guard = threading.Lock()


def _region_download_lock(region_id: str) -> threading.Lock:
    with _region_download_locks_guard:
        return _region_download_locks.setdefault(region_id,
                                                 threading.Lock())


def _extract_ready(region_id: str) -> bool:
    path = _region_file(region_id)
    return os.path.isfile(path) and _file_looks_like_pbf(path)


def _another_downloader_active(region_id: str) -> bool:
    """A live concurrent download of this region (fresh sibling ``.tmp``
    from the maintenance thread or another build process).  Stale
    temporaries — a crashed downloader's residue — do not count and are
    removed so they cannot accumulate."""
    prefix = os.path.basename(_region_file(region_id)) + ".tmp"
    try:
        names = os.listdir(STORE_DIRECTORY)
    except OSError:
        return False
    now = time.time()
    active = False
    for name in names:
        if not name.startswith(prefix):
            continue
        path = _store_path(name)
        try:
            if now - os.path.getmtime(path) < CONCURRENT_TMP_FRESH_SECONDS:
                active = True
            else:
                os.remove(path)
        except OSError:
            continue
    return active


def _ensure_extracts_foreground(regions) -> bool:
    """Make every ``(region_id, pbf_url)`` present in the store, waiting
    on (or starting) downloads in the FOREGROUND.  True only when all
    are ready; False on stop (red_flag), download failure, or a region
    with no usable url — callers fall back to Overpass exactly as with
    the lazy path."""
    for (region_id, pbf_url) in regions:
        lock = _region_download_lock(region_id)
        while not _extract_ready(region_id):
            if UI.red_flag:
                return False
            if lock.acquire(blocking=False):
                try:
                    if _extract_ready(region_id):
                        break
                    if not _another_downloader_active(region_id):
                        url = pbf_url or _url_for_region(region_id)
                        if url is None and _refresh_index():
                            url = _url_for_region(region_id)
                        if not (url and _download_extract(
                                region_id, url, foreground=True)):
                            return False
                finally:
                    lock.release()
            if not _extract_ready(region_id):
                time.sleep(FOREGROUND_POLL_SECONDS)
    return True


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
        if missing and foreground_download_enabled() and not UI.red_flag:
            # Owner ruling 2026-07-18: acquire the extract NOW rather
            # than falling back to Overpass for the whole first build in
            # a region (a country downloads from the CDN in about the
            # time one throttled Overpass round takes).
            missing_set = set(missing)
            if _ensure_extracts_foreground(
                    [r for r in regions if r[0] in missing_set]):
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
def _download_extract(region_id: str, pbf_url: str,
                      foreground: bool = False) -> bool:
    """Stream one extract to the store (atomic; resumes are simple
    re-downloads — CDN throughput makes ranged resume not worth its
    edge cases).  The temporary path is unique per downloader (pid +
    thread), so a concurrent download of the same region can never
    corrupt another's stream — last atomic rename wins."""
    target = _region_file(region_id)
    temporary_path = "%s.tmp-%d-%d" % (
        target, os.getpid(), threading.get_ident())
    try:
        UI.vprint(
            0,
            "   Downloading OSM regional extract", region_id,
            "(needed by this build)..." if foreground
            else "in the background...",
        )
        received = 0
        next_report = DOWNLOAD_PROGRESS_EVERY_BYTES
        cancelled = False
        with requests.get(
            pbf_url, stream=True, timeout=HTTP_TIMEOUT_SECONDS
        ) as response:
            response.raise_for_status()
            try:
                total_mb = int(getattr(response, "headers", {}).get(
                    "Content-Length", 0)) >> 20
            except (AttributeError, TypeError, ValueError):
                total_mb = 0
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
                            "%d%s MB so far" % (
                                received >> 20,
                                "/%d" % total_mb if total_mb else ""),
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
    """[(region_id, url)] of stored extracts past the refresh age.

    An entry whose pbf file is gone means the user deleted it (e.g. a
    multi-gigabyte aggregate made redundant by covering-region pruning):
    the state entry is dropped, never re-downloaded.  A build that
    genuinely needs the region again re-acquires it through the
    wanted-list / foreground-download path.
    """
    with _store_lock:
        state = _read_json(_store_path("state.json")) or {}
        deleted = [region_id for region_id in state
                   if not os.path.isfile(_region_file(region_id))]
        if deleted:
            for region_id in deleted:
                del state[region_id]
            _write_json_atomic(_store_path("state.json"), state)
            UI.vprint(
                1,
                "   Forgetting deleted OSM regional extract(s):",
                ", ".join(sorted(deleted)),
            )
    refresh_age_seconds = _extract_refresh_days() * 86400
    now = time.time()
    stale = []
    for region_id, entry in state.items():
        if now - float(entry.get("downloaded_at", 0)) \
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

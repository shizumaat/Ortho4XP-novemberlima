"""Coastal bathymetry band: measured seabed depth along a tile's shoreline.

Spec: ``docs/specs/coastal-bathymetry-spec.md`` (sections 3-5).  The band
mirrors the coastline elevation band (``O4_Elevation_Level``): the tile's
10 x 10 grid of 0.1 degree cells is filtered to the cells near the
OpenStreetMap coastline (and, best-effort, near large cached inland
water), each missing cell is fetched through the provider machinery from
a ``role=bathymetry`` elevation provider, and the fetched cells mosaic
into one band VRT consumed by the masks step (depth-graded water alpha)
and the DSF step (the X-Plane 12 ``sea_level`` raster).

Not to be confused with the legacy ``O4_Bathymetry`` module, which recuts
water triangles and grades their depth ratio from the distance masks.

Auto mode (``masks_use_DEM_too="auto"``) additionally gates the cells to
within ``bathymetry_airport_radius_km`` (default 20, 0 = whole
shoreline) of an apt.dat airport: measured depth matters when flying
low, and beyond the radius the masks keep the distance fade plus the
mapped shallow-water fallback.  Explicit ``True`` fetches the whole
shoreline band.

Failures never raise out of :func:`ensure_bathymetry_band`; they degrade
loudly through ``UI.vprint`` and return ``None`` (legacy behaviour).

Concurrency and progress (added 2026-07-16 after +37-009/PORTUGALTIDAL
exposed both gaps): the band directory is guarded by a stale-aware
``fetch.lock`` so two engine processes never race on cell files or
``index.json`` — the second fetcher waits and resumes from the first
one's cells; every cell/stamp/VRT write is temp-file + ``os.replace``
atomic; cached cells are integrity-checked before being trusted; and
cell completions report cells-done/total through ``UI.vprint`` and
``UI.progress_bar`` (foreground waits only) so the GUI's step progress
moves during multi-hour first fetches.
"""

import datetime
import json
import os
import socket
import threading
import time
from typing import Optional

try:
    from osgeo import gdal

    gdal.UseExceptions()
    has_gdal = True
except ImportError:
    has_gdal = False

import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO
import O4_UI_Utils as UI

# One flat resolution tier: mask pixels at zoomlevel 16 are ~2.4 m but a
# 10 m depth grid is beyond visually sufficient for water transparency
# and keeps a 0.1 degree cell around 4 MB (spec section 3).
BATHYMETRY_CELL_DEGREES = 0.1
BATHYMETRY_CELL_RESOLUTION_M = 10.0
# The band extends one cell ring BEYOND the 1 degree tile: mask squares
# and the DSF raster's post grid straddle tile edges, and a band clamped
# to the tile leaves each neighbour's copy of a shared mask square blind
# on the other side of the line (the 37N seam at the Ria Formosa,
# 2026-07-16).  Overhang cells resolve to the OWNING tile's canonical
# cell files, so two adjacent builds fetch each shared cell once.
BAND_OVERHANG_CELLS = 1

# Inland water bodies smaller than this (square kilometres) do not pull
# band cells; matches the masking intuition that only sea-sized water
# gets measured-depth treatment.
MINIMUM_INLAND_WATER_KM2 = 4.0

# Inland water pulls cells at this fixed tight reach, NOT the coastline's
# configurable bathymetry_band_km: terrain lidar carries no depths for
# rivers and reservoirs, so wide inland bands only multiply first-fetch
# time (Faro: 34 of 59 cells followed the Guadiana for no mask benefit,
# measured 2026-07-16).
INLAND_WATER_BAND_KM = 1.0

# Auto-mode airport gate (spec section 3, ruling 2026-07-16): measured
# depth matters when flying low, and flying low happens around airports,
# so "auto" only fetches shoreline cells within
# ``bathymetry_airport_radius_km`` (default below, 0 = the whole
# shoreline) of an enabled anchor.  Beyond the radius the masks keep the
# distance fade plus the mapped shallow-water fallback — fine from
# altitude.  Explicit ``True`` always fetches the whole shoreline band.
DEFAULT_AIRPORT_RADIUS_KM = 20.0

# Which apt.dat anchor types gate the band, each behind its own tile
# cfg checkbox (ruling 2026-07-16: users pick the anchors matching how
# they fly): (tile attribute, O4_Airport_Index category, default).
# Heliports default OFF — apt.dat carries ~7k of them (hospital pads,
# private platforms), each projecting a full radius disk, and they
# dilute the gate badly in dense regions (probe 2026-07-16: +37-009's
# kept cells 70 -> 90 of 100 from heliports alone).
ANCHOR_CATEGORY_SETTINGS = (
    ("bathymetry_near_icao_airports", "icao_airport", True),
    ("bathymetry_near_other_airports", "airport", True),
    ("bathymetry_near_seaplane_bases", "seaplane_base", True),
    ("bathymetry_near_heliports", "heliport", False),
)

# Concurrent cell fetches. The first missing cell fetches alone to warm
# the provider's authenticated session (avoiding a sign-in stampede);
# the rest fan out. The parallel build orchestrator classes steps as
# network/compute with caps so providers see bounded, polite load
# (docs/specs/parallel-tile-builds.md) — the band fan-out honours the
# same convention by dividing its workers among concurrent tile builds,
# exactly like the DDS conversion slots do.
CELL_FETCH_WORKERS = 8


def _cell_fetch_worker_count(cell_count: int) -> int:
    from O4_Parallel_Utils import parallel_sibling_count

    shared = max(2, CELL_FETCH_WORKERS // parallel_sibling_count())
    return min(shared, cell_count)

# masks_use_DEM_too="auto" only counts a provider as "good data" when its
# native resolution is at least this fine: the global fallbacks (GEBCO,
# 450 m) barely resolve the shoreline and would degrade every coastal
# mask on Earth if auto engaged on them.  Explicit True and the DSF
# sea_level raster synthesis use any provider.
AUTO_MODE_MAXIMUM_RESOLUTION_M = 50.0

NO_COVERAGE = "no_coverage"


def _read_band_stamp(stamp_path: str) -> dict:
    """The previous ``index.json`` content, or an empty dict."""
    try:
        with open(stamp_path, "r") as stamp_file:
            return json.load(stamp_file)
    except (OSError, ValueError):
        return {}


def _write_band_stamp(stamp_path: str, stamp: dict) -> None:
    """Persist the band stamp; the disk state both steps re-read.

    Written to a pid-suffixed sibling then :func:`os.replace`d so a
    concurrent reader (another engine process resuming the same band)
    never sees a half-written ``index.json``.
    """
    os.makedirs(os.path.dirname(stamp_path), exist_ok=True)
    temporary_path = "%s.part%d" % (stamp_path, os.getpid())
    with open(temporary_path, "w") as stamp_file:
        json.dump(stamp, stamp_file, indent=1)
    os.replace(temporary_path, stamp_path)


def _cell_file_state(cell_path: str) -> str:
    """Integrity of a band cell file: ``valid``, ``empty`` or ``unreadable``.

    ``valid`` means GDAL opens the raster and at least one pixel carries
    data; ``empty`` means a readable raster whose every pixel is nodata
    (the provider window holds no data there — a durable negative);
    ``unreadable`` means missing, truncated or not a raster at all
    (transient: refetch).  Before the band directory grew a lock,
    concurrent fetches raced on cell files and left fully-nodata ones
    behind (observed on +37-009, 2026-07-16) — cached cells are therefore
    never trusted on ``index.json`` alone.
    """
    try:
        if os.path.getsize(cell_path) == 0:
            return "unreadable"
    except OSError:
        return "unreadable"
    if not has_gdal:
        return "valid"
    try:
        dataset = gdal.Open(cell_path)
        if dataset is None:
            return "unreadable"
    except Exception:
        return "unreadable"
    try:
        # Raises exactly when every pixel is nodata.
        dataset.GetRasterBand(1).ComputeRasterMinMax(1)
    except Exception:
        return "empty"
    finally:
        dataset = None
    return "valid"


# =====================================================================
# Cross-process band-directory lock
# =====================================================================
# Two engine processes (parallel tile builds of neighbouring tiles, or a
# stray orphan engine) can want the same band at once; without exclusion
# they raced on cell files and index.json.  The lock lives inside the
# band directory, names its owner, and is refreshed after every fetched
# cell — so a waiter can tell a slow fetch (cells take minutes on tidal
# lidar servers) from a crashed one.
BAND_LOCK_FILE_NAME = "fetch.lock"
BAND_LOCK_STALE_SECONDS = 1800.0
BAND_LOCK_POLL_SECONDS = 2.0


def _band_lock_path(band_directory: str) -> str:
    return os.path.join(band_directory, BAND_LOCK_FILE_NAME)


def _band_lock_owner_is_alive(lock_path: str):
    """``True``/``False`` when determinable (owner on this machine),
    ``None`` when not (another machine via a shared drive, or an
    unreadable lock file)."""
    try:
        with open(lock_path, "r") as lock_file:
            owner = json.load(lock_file)
        owner_pid = int(owner["pid"])
        owner_host = owner.get("host")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if owner_host != socket.gethostname():
        return None
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


def _band_lock_is_stale(lock_path: str) -> bool:
    """A dead same-host owner is stale immediately; an undeterminable
    owner only after :data:`BAND_LOCK_STALE_SECONDS` without a refresh
    (the fetch loop touches the lock after every cell)."""
    owner_is_alive = _band_lock_owner_is_alive(lock_path)
    if owner_is_alive is not None:
        return not owner_is_alive
    try:
        lock_age_seconds = time.time() - os.path.getmtime(lock_path)
    except OSError:
        # The lock vanished under us: the owner just released it.
        return False
    return lock_age_seconds > BAND_LOCK_STALE_SECONDS


def _acquire_band_lock(
    band_directory: str, waiting_progress=None, wait: bool = True
) -> bool:
    """Take the band-directory fetch lock; ``True`` once held.

    Stale locks are stolen.  While another live fetch holds the lock the
    caller polls (``waiting_progress`` is invoked once per poll so the
    other fetch's cells can be surfaced as progress) and honours
    ``UI.red_flag`` — a cancelled build returns ``False`` promptly.
    ``wait=False`` returns ``False`` on first contention instead of
    polling (used for opportunistic stamp refreshes).
    """
    lock_path = _band_lock_path(band_directory)
    waiting_announced = False
    while True:
        if UI.red_flag:
            return False
        try:
            lock_descriptor = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            )
        except FileExistsError:
            try:
                lock_stat_before = os.stat(lock_path)
            except OSError:
                # Vanished under us: the owner just released it; retry.
                continue
            if _band_lock_is_stale(lock_path):
                UI.vprint(
                    1,
                    "   Bathymetry band: removing a stale fetch lock"
                    " (left by a crashed or killed build).",
                )
                try:
                    # Two waiters can both judge the same lock stale;
                    # only remove it if it is still the very file we
                    # judged — never the fresh lock the faster waiter
                    # may have re-created meanwhile.
                    lock_stat_now = os.stat(lock_path)
                    if (
                        lock_stat_now.st_ino,
                        lock_stat_now.st_mtime,
                    ) == (
                        lock_stat_before.st_ino,
                        lock_stat_before.st_mtime,
                    ):
                        os.remove(lock_path)
                except OSError:
                    pass
                continue
            if not wait:
                return False
            if not waiting_announced:
                UI.vprint(
                    1,
                    "   Bathymetry band: another Ortho4XP process is"
                    " already fetching this band; waiting for its cells"
                    " instead of downloading them twice.",
                )
                waiting_announced = True
            if waiting_progress is not None:
                try:
                    waiting_progress()
                except Exception:
                    pass
            time.sleep(BAND_LOCK_POLL_SECONDS)
            continue
        with os.fdopen(lock_descriptor, "w") as lock_file:
            json.dump(
                {"pid": os.getpid(), "host": socket.gethostname()},
                lock_file,
            )
        return True


def _refresh_band_lock(band_directory: str) -> None:
    try:
        os.utime(_band_lock_path(band_directory))
    except OSError:
        pass


def _release_band_lock(band_directory: str) -> None:
    try:
        os.remove(_band_lock_path(band_directory))
    except OSError:
        pass


# =====================================================================
# Progress reporting
# =====================================================================
# Cell completions drive the step progress bar only while a consumer is
# actually waiting on the fetch in the foreground (the masks or DSF
# step): the same fetch running as a background prefetch during the
# vector/mesh steps must not touch those steps' progress bars.
# ``UI.vprint`` cell counts are emitted unconditionally — they reach the
# log and console either way.
_foreground_wait = threading.Event()


def _report_band_progress(cells_done: int, cells_total: int) -> None:
    if cells_total <= 0:
        return
    if _foreground_wait.is_set():
        UI.progress_bar(1, int(100 * cells_done / cells_total))


def _band_geometry(tile):
    """Shoreline geometries driving cell selection, in tile-relative degrees.

    The OSM coastline arrives through the pipeline's shared cache (the
    vector step prefetches the same query).  Large cached inland water
    polygons are collected separately (they get the tight
    :data:`INLAND_WATER_BAND_KM` reach) and best-effort only: when the
    ``water`` query cache already exists on disk — the masks step must
    never trigger a fresh Overpass download for inland lakes.  Inland
    water is only collected AT ALL when the tile has a coastline: the
    reach serves coastal lagoons the water-class rulings keep
    inland-classed, and on a landlocked tile (no coastline ways at all)
    the band is skipped outright — no provider has lake bathymetry.
    Returns ``(coastline_geometry, inland_geometry_or_None)`` or
    ``None`` on a failed coastline download.
    """
    import O4_OSM_Utils as OSM
    from shapely.geometry import GeometryCollection

    coastline_layer = OSM.OSM_layer()
    if not OSM.OSM_queries_to_OSM_layer(
        ['way["natural"="coastline"]'],
        coastline_layer,
        tile.lat,
        tile.lon,
        [],
        cached_suffix="coastline",
    ):
        return None
    coastline = OSM.OSM_to_MultiLineString(
        coastline_layer, tile.lat, tile.lon
    )

    # Landlocked tiles stop here (owner direction 2026-07-18): the
    # inland reach below exists to serve inland-CLASSED water adjoining
    # a coast — tidal lagoons like the Ria Formosa, which the water-class
    # rulings deliberately keep inland — never freestanding lakes.  No
    # shipped provider measures lake or reservoir bathymetry, so on a
    # tile with no coastline every selected cell would be a wasted fetch
    # (CYXY: 46 cells probed around the Whitehorse lakes).  The large
    # sea-equivalent lakes that DO have measured depth (the Great Lakes,
    # the Caspian) are tagged natural=coastline in OSM and keep their
    # band through the coastline branch.
    if coastline.is_empty:
        return (coastline, None)

    inland = None
    water_cache = FNAMES.osm_cached(tile.lat, tile.lon, "water")
    if os.path.isfile(water_cache):
        try:
            water_layer = OSM.OSM_layer()
            if OSM.OSM_queries_to_OSM_layer(
                [
                    'way["natural"="water"]',
                    'relation["natural"="water"]',
                ],
                water_layer,
                tile.lat,
                tile.lon,
                [],
                cached_suffix="water",
            ):
                water_area = OSM.OSM_to_MultiPolygon(
                    water_layer, tile.lat, tile.lon
                )
                # Degrees-squared threshold at this latitude.
                square_km_per_square_degree = (
                    GEO.lon_to_m(tile.lat + 0.5) * GEO.lat_to_m / 1e6
                )
                minimum_area_degrees = (
                    MINIMUM_INLAND_WATER_KM2 / square_km_per_square_degree
                )
                large_water = [
                    polygon
                    for polygon in getattr(water_area, "geoms", [])
                    if polygon.area >= minimum_area_degrees
                ]
                if large_water:
                    inland = GeometryCollection(
                        [polygon.boundary for polygon in large_water]
                    )
        except Exception as error:
            UI.vprint(
                2,
                "   Bathymetry band: inland water geometry skipped:",
                str(error),
            )
    return (coastline, inland)


# ---------------------------------------------------------------------
# Auto-mode airport gate
# ---------------------------------------------------------------------
# The offline airport index (O4_Airport_Index, ~40k entries) is loaded
# once and shared between the prefetch and the consumer step; the key
# invalidates on a rebuilt cache.
_airport_index_guard = threading.Lock()
_airport_index_state = {"cache_key": None, "positions": None}


def _enabled_anchor_categories(tile) -> frozenset:
    """The anchor categories the tile's checkboxes enable
    (:data:`ANCHOR_CATEGORY_SETTINGS`)."""
    enabled = set()
    for (attribute, category, default) in ANCHOR_CATEGORY_SETTINGS:
        if str(getattr(tile, attribute, default)) == "True":
            enabled.add(category)
    return frozenset(enabled)


def _airports_near_tile(lat, lon, radius_km, anchor_categories):
    """(lat, lon) of enabled apt.dat anchors within ``radius_km`` of the tile.

    Reads the offline airport search index (built by the map window from
    X-Plane's Global Airports apt.dat) and keeps the entries whose
    category is in ``anchor_categories``.  Returns the matches inside
    the tile's 1 degree box expanded by the radius, ``[]`` when none are
    that close, or ``None`` when the index is unavailable (missing,
    unreadable or empty) — callers treat ``None`` as "the gate cannot be
    evaluated", never as "no airports".  Airports across the
    antimeridian are not matched (no such tile has a fine bathymetry
    provider today).
    """
    cache_file = FNAMES.airport_index_cache()
    try:
        cache_stat = os.stat(cache_file)
    except OSError:
        return None
    cache_key = (cache_file, cache_stat.st_mtime_ns, cache_stat.st_size)
    with _airport_index_guard:
        if _airport_index_state["cache_key"] != cache_key:
            import O4_Airport_Index as AIRPORT_INDEX

            try:
                entries = AIRPORT_INDEX.load_index(cache_file)
            except Exception:
                return None
            if not entries:
                return None
            _airport_index_state["cache_key"] = cache_key
            _airport_index_state["positions"] = [
                (entry.lat, entry.lon, entry.category)
                for entry in entries
            ]
        positions = _airport_index_state["positions"]
    radius_m = radius_km * 1000.0
    margin_lat = radius_m / GEO.lat_to_m
    # Never let the longitude margin blow up at polar latitudes.
    margin_lon = radius_m / max(GEO.lon_to_m(lat + 0.5), 1000.0)
    return [
        (airport_lat, airport_lon)
        for (airport_lat, airport_lon, category) in positions
        if category in anchor_categories
        and (lat - margin_lat) <= airport_lat <= (lat + 1 + margin_lat)
        and (lon - margin_lon) <= airport_lon <= (lon + 1 + margin_lon)
    ]


def _filter_cells_to_airport_reach(
    tile,
    anchor_categories,
    cell_indices,
    metres_per_degree_longitude,
    metres_per_degree_latitude,
    cell_half_diagonal_m,
):
    """Apply the auto-mode airport gate to the selected band cells.

    Keeps the cells whose centre lies within
    ``tile.bathymetry_airport_radius_km`` (plus the cell half-diagonal,
    the same any-part-inside slack the coastline reach uses) of an
    apt.dat anchor whose category is in ``anchor_categories``.  Returns
    ``(kept_cells, dropped_count, gate_engaged)``; the gate disengages —
    everything kept — when the radius is 0 or the airport index is
    unavailable (conservative: the full band is today's behaviour, and
    the INFO line says why).
    """
    radius_km = float(
        getattr(
            tile, "bathymetry_airport_radius_km", DEFAULT_AIRPORT_RADIUS_KM
        )
    )
    if radius_km <= 0:
        return (cell_indices, 0, False)
    airports = _airports_near_tile(
        tile.lat, tile.lon, radius_km, anchor_categories
    )
    if airports is None:
        UI.vprint(
            1,
            "   INFO: the offline airport index is unavailable, so the"
            " bathymetry band's airport-radius gate cannot engage;"
            " fetching the full shoreline band. (The map window builds"
            " the index when the X-Plane folder is set.)",
        )
        return (cell_indices, 0, False)
    reach_m = radius_km * 1000.0 + cell_half_diagonal_m
    reach_m_squared = reach_m * reach_m
    airport_positions_m = [
        (
            (airport_lon - tile.lon) * metres_per_degree_longitude,
            (airport_lat - tile.lat) * metres_per_degree_latitude,
        )
        for (airport_lat, airport_lon) in airports
    ]
    kept = []
    for (cell_column, cell_row) in cell_indices:
        centre_x = (
            (cell_column + 0.5)
            * BATHYMETRY_CELL_DEGREES
            * metres_per_degree_longitude
        )
        centre_y = (
            (cell_row + 0.5)
            * BATHYMETRY_CELL_DEGREES
            * metres_per_degree_latitude
        )
        for (airport_x, airport_y) in airport_positions_m:
            delta_x = airport_x - centre_x
            delta_y = airport_y - centre_y
            if delta_x * delta_x + delta_y * delta_y <= reach_m_squared:
                kept.append((cell_column, cell_row))
                break
    return (kept, len(cell_indices) - len(kept), True)


def warp_band_to_post_grid(band_vrt_path: str, lat: int, lon: int,
                           posts: int = 1201):
    """Resample the band VRT onto the DSF raster post grid.

    The X-Plane DSF rasters are post-centric ``posts x posts`` grids over
    the 1 degree tile, row 0 = SOUTH, west to east (verified against the
    X-Plane 12 Global Scenery donor, spec section 5).  Returns a float32
    numpy array in exactly that orientation with nodata at -32768, or
    ``None`` when GDAL is unavailable or the warp fails.
    """
    if not has_gdal:
        return None
    import numpy

    half_post_degrees = 1.0 / (2 * (posts - 1))
    try:
        warped = gdal.Warp(
            "",
            band_vrt_path,
            options=gdal.WarpOptions(
                format="MEM",
                outputType=gdal.GDT_Float32,
                dstSRS="EPSG:4326",
                outputBounds=(
                    lon - half_post_degrees,
                    lat - half_post_degrees,
                    lon + 1 + half_post_degrees,
                    lat + 1 + half_post_degrees,
                ),
                width=posts,
                height=posts,
                resampleAlg="bilinear",
                dstNodata=-32768.0,
            ),
        )
        if warped is None:
            return None
        values = warped.GetRasterBand(1).ReadAsArray()
        warped = None
    except Exception as error:
        UI.vprint(
            2, "   Bathymetry post-grid warp failed:", str(error)
        )
        return None
    if values is None:
        return None
    # GDAL returns row 0 = north; the DSF raster wants row 0 = south.
    return numpy.flipud(values)


# Prefetch bookkeeping: build steps that run BEFORE the band consumers
# (the vector step, spec section 3) can start the fetch on a background
# thread so it overlaps the mesh build; the consumers then join the
# in-flight fetch instead of paying for it serially.
_prefetch_futures_guard = threading.Lock()
_prefetch_futures = {}


def prefetch_bathymetry_band(tile) -> None:
    """Start the band fetch in the background when it will be wanted.

    Called at the start of the vector step (Step 1) so the network fetch
    overlaps the mesh build.  A no-op when the tile's mask settings do
    not call for the band, when no bathymetry provider covers the tile,
    or when a prefetch is already in flight.  Never raises; consumers
    surface any failure through :func:`ensure_bathymetry_band`.
    """
    if not has_gdal:
        return
    masks_dem_setting = str(getattr(tile, "masks_use_DEM_too", "False"))
    if masks_dem_setting not in ("auto", "True"):
        return
    fine_nearshore_only = masks_dem_setting == "auto"
    intertidal_ok = masks_dem_setting == "True"
    try:
        import O4_Airport_Elevation_Insets as INSETS

        if not INSETS.select_bathymetry_definitions(tile.lat, tile.lon):
            return
    except Exception:
        return
    key = (tile.lat, tile.lon, fine_nearshore_only, intertidal_ok)
    with _prefetch_futures_guard:
        if key in _prefetch_futures:
            return
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="bathymetry_prefetch"
        )
        _prefetch_futures[key] = executor.submit(
            _ensure_bathymetry_band_now,
            tile,
            fine_nearshore_only,
            intertidal_ok,
        )
        executor.shutdown(wait=False)
    UI.vprint(
        1,
        "   Bathymetry band prefetch started (overlaps the mesh build).",
    )


def ensure_bathymetry_band(
    tile, fine_nearshore_only: bool = False, intertidal_ok: bool = False
) -> Optional[str]:
    """Fetch (or recycle) the coastal bathymetry band for the tile.

    When a prefetch is in flight for the same tile and gating (started
    by :func:`prefetch_bathymetry_band` at Step 1), the caller joins it
    instead of fetching again; the future is consumed so a later build
    of the same tile re-evaluates fresh state.

    ``intertidal_ok`` (see :func:`_ensure_bathymetry_band_now`) is
    passed by the masks step for ``masks_use_DEM_too=True`` only; the
    DSF raster callers keep the default and never fetch intertidal-only
    sources.
    """
    key = (tile.lat, tile.lon, bool(fine_nearshore_only),
           bool(intertidal_ok))
    with _prefetch_futures_guard:
        future = _prefetch_futures.pop(key, None)
    # The caller now waits in the foreground: from here on, cell
    # completions (including those of a joined in-flight prefetch) may
    # drive the current step's progress bar.
    _foreground_wait.set()
    try:
        if future is not None:
            try:
                return future.result()
            except Exception as error:
                UI.vprint(
                    1,
                    "   WARNING: the bathymetry band prefetch failed (",
                    str(error),
                    "); fetching directly.",
                )
        return _ensure_bathymetry_band_now(
            tile, fine_nearshore_only, intertidal_ok
        )
    finally:
        _foreground_wait.clear()


def _resolve_band_cell(tile, cell_column, cell_row, code):
    """Path, stamp key and owner stamp for one (possibly overhang) cell.

    In-tile cells keep their historical stem (the file basename) so
    existing durable negatives stay valid.  Overhang cells (indices
    outside 0..9) resolve to the OWNING tile's canonical cell path —
    adjacent builds share one physical file, in whichever order they
    run — under a stamp key qualified by the owner tile (the basename
    alone would collide with this tile's own cell of the same local
    indices).
    """
    owner_lat = tile.lat + cell_row // 10
    owner_lon = tile.lon + cell_column // 10
    local_column = cell_column % 10
    local_row = cell_row % 10
    cell_path = FNAMES.bathymetry_band_cell(
        owner_lat,
        owner_lon,
        local_column,
        local_row,
        code,
        BATHYMETRY_CELL_RESOLUTION_M,
    )
    basename_stem = os.path.splitext(os.path.basename(cell_path))[0]
    if (owner_lat, owner_lon) == (tile.lat, tile.lon):
        return {
            "column": cell_column,
            "row": cell_row,
            "path": cell_path,
            "stem": basename_stem,
            "owner_stamp_path": None,
            "owner_stem": basename_stem,
        }
    return {
        "column": cell_column,
        "row": cell_row,
        "path": cell_path,
        "stem": "%s@%s" % (
            basename_stem,
            FNAMES.short_latlon(owner_lat, owner_lon),
        ),
        "owner_stamp_path": FNAMES.bathymetry_band_index(
            owner_lat, owner_lon
        ),
        "owner_stem": basename_stem,
    }


def _ensure_bathymetry_band_now(
    tile, fine_nearshore_only: bool = False, intertidal_ok: bool = False
) -> Optional[str]:
    """The actual band fetch (docstring contract on the public wrapper).

    Returns the band VRT path, or ``None`` when GDAL is missing, no
    ``role=bathymetry`` provider covers the tile, the tile has no
    shoreline, or nothing could be fetched.  Cached cells and durable
    per-cell no-coverage negatives are honoured exactly like the
    coastline elevation band.

    ``fine_nearshore_only`` is the ``masks_use_DEM_too="auto"`` contract:
    the band is only fetched when the covering provider's native
    resolution is at most :data:`AUTO_MODE_MAXIMUM_RESOLUTION_M` — the
    coarse global fallbacks stay available to explicit ``True`` and to
    the DSF raster synthesis, but never auto-engage the mask ramp.  Auto
    mode additionally applies the airport-radius gate
    (:func:`_filter_cells_to_airport_reach`): only shoreline cells
    within ``tile.bathymetry_airport_radius_km`` of an apt.dat airport
    are fetched.

    ``intertidal_ok`` admits ``intertidal=True`` sources (exposed-flats
    lidar that stops at the waterline).  Only ``masks_use_DEM_too=True``
    passes it: everywhere else their data is a binary "flats" layer the
    free OpenStreetMap fallback matches (and the DSF ``sea_level``
    raster's ``min(measured, elevation - 2)`` convention makes their
    centimetre depths a strict no-op), so the slow national-server
    fetch is not worth starting.
    """
    if not has_gdal:
        UI.vprint(
            1,
            "   INFO: the bathymetry band requires the GDAL python"
            " bindings; masks keep their distance-only water fade.",
        )
        return None

    import O4_Airport_Elevation_Insets as INSETS

    definitions = INSETS.select_bathymetry_definitions(tile.lat, tile.lon)
    if not intertidal_ok:
        intertidal = [
            definition
            for definition in definitions
            if definition.get("intertidal")
        ]
        definitions = [
            definition
            for definition in definitions
            if definition not in intertidal
        ]
        if intertidal and not definitions:
            UI.vprint(
                1,
                "   INFO: the covering bathymetry source(s)",
                ", ".join(d["code"] for d in intertidal),
                "only measure exposed tidal flats; the OpenStreetMap"
                " shallow-water fallback serves those for free — set"
                " masks_use_DEM_too=True to fetch the measured flats"
                " anyway.",
            )
    if fine_nearshore_only:
        coarse = [
            definition
            for definition in definitions
            if float(definition.get("native_resolution_m", 1e9))
            > AUTO_MODE_MAXIMUM_RESOLUTION_M
        ]
        definitions = [
            definition
            for definition in definitions
            if definition not in coarse
        ]
        if coarse and not definitions:
            UI.vprint(
                1,
                "   INFO: the covering bathymetry source(s)",
                ", ".join(d["code"] for d in coarse),
                "are too coarse for automatic depth-graded masks; set"
                " masks_use_DEM_too=True to use them anyway.",
            )
    if not definitions:
        UI.vprint(
            2,
            "   INFO: no bathymetry provider covers this tile; masks keep"
            " their distance-only water fade.",
        )
        return None

    geometries = _band_geometry(tile)
    if geometries is None:
        UI.lvprint(
            0,
            "   WARNING: the coastline download for the bathymetry band"
            " failed; the band is skipped and the build continues.",
        )
        return None
    (coastline, inland) = geometries
    if coastline.is_empty and inland is None:
        UI.vprint(
            1,
            "   INFO: this tile has no shoreline; the bathymetry band is"
            " skipped.",
        )
        return None

    from shapely.affinity import scale as _scale_geometry
    from shapely.geometry import Point

    metres_per_degree_longitude = GEO.lon_to_m(tile.lat + 0.5)
    metres_per_degree_latitude = GEO.lat_to_m

    def _metre_scaled(geometry):
        return _scale_geometry(
            geometry,
            xfact=metres_per_degree_longitude,
            yfact=metres_per_degree_latitude,
            origin=(0.0, 0.0),
        )

    scaled_coastline = None if coastline.is_empty else _metre_scaled(
        coastline
    )
    scaled_inland = None if inland is None else _metre_scaled(inland)
    cell_half_diagonal_m = 0.5 * (
        (BATHYMETRY_CELL_DEGREES * metres_per_degree_longitude) ** 2
        + (BATHYMETRY_CELL_DEGREES * metres_per_degree_latitude) ** 2
    ) ** 0.5
    coastline_reach_m = (
        float(getattr(tile, "bathymetry_band_km", 5.0)) * 1000.0
        + cell_half_diagonal_m
    )
    inland_reach_m = INLAND_WATER_BAND_KM * 1000.0 + cell_half_diagonal_m

    cell_indices = []
    cell_count = 10  # a 1 degree tile is a 10 x 10 grid of 0.1 degree cells
    # ... plus one overhang ring into the neighbouring tiles (indices -1
    # and 10): the coastline geometry holds the complete OSM ways that
    # touch this tile, so distance selection keeps working across the
    # edge.
    for cell_column in range(-BAND_OVERHANG_CELLS,
                             cell_count + BAND_OVERHANG_CELLS):
        for cell_row in range(-BAND_OVERHANG_CELLS,
                              cell_count + BAND_OVERHANG_CELLS):
            centre = Point(
                (cell_column + 0.5)
                * BATHYMETRY_CELL_DEGREES
                * metres_per_degree_longitude,
                (cell_row + 0.5)
                * BATHYMETRY_CELL_DEGREES
                * metres_per_degree_latitude,
            )
            selected = (
                scaled_coastline is not None
                and scaled_coastline.distance(centre) <= coastline_reach_m
            ) or (
                scaled_inland is not None
                and scaled_inland.distance(centre) <= inland_reach_m
            )
            if selected:
                cell_indices.append((cell_column, cell_row))

    if not cell_indices:
        UI.vprint(
            1,
            "   INFO: no tile cell lies within the bathymetry band; the"
            " band is skipped.",
        )
        return None

    if fine_nearshore_only:
        # Auto mode concentrates measured depth where flying happens low
        # (ruling 2026-07-16); explicit True keeps the whole shoreline.
        anchor_categories = _enabled_anchor_categories(tile)
        if not anchor_categories:
            UI.vprint(
                1,
                "   INFO: every bathymetry anchor type (airports,"
                " seaplane bases, heliports) is unchecked; the band is"
                " skipped and the masks keep the distance fade (plus the"
                " mapped shallow-water fallback).",
            )
            return None
        (cell_indices, dropped_count, gate_engaged) = (
            _filter_cells_to_airport_reach(
                tile,
                anchor_categories,
                cell_indices,
                metres_per_degree_longitude,
                metres_per_degree_latitude,
                cell_half_diagonal_m,
            )
        )
        if gate_engaged and not cell_indices:
            UI.vprint(
                1,
                "   INFO: no shoreline cell lies within",
                float(getattr(tile, "bathymetry_airport_radius_km",
                              DEFAULT_AIRPORT_RADIUS_KM)),
                "km of an enabled anchor (airport/seaplane"
                " base/heliport); the bathymetry band is skipped and the"
                " masks keep the distance fade (plus the mapped"
                " shallow-water fallback).",
            )
            return None
        if gate_engaged and dropped_count:
            UI.vprint(
                1,
                "   Airport-radius gate: keeping",
                len(cell_indices),
                "of",
                len(cell_indices) + dropped_count,
                "shoreline cells (within",
                float(getattr(tile, "bathymetry_airport_radius_km",
                              DEFAULT_AIRPORT_RADIUS_KM)),
                "km of an enabled anchor; masks_use_DEM_too=True fetches"
                " all).",
            )

    band_directory = FNAMES.bathymetry_band_directory(tile.lat, tile.lon)
    os.makedirs(band_directory, exist_ok=True)
    stamp_path = FNAMES.bathymetry_band_index(tile.lat, tile.lon)
    previous_stamp = _read_band_stamp(stamp_path)
    # Durable per-cell negatives survive across providers and runs (the
    # stems are provider-qualified).
    cell_outcomes = {
        stem: outcome
        for stem, outcome in previous_stamp.get("cells", {}).items()
        if outcome == NO_COVERAGE
    }

    # Walk the covering providers best-first: a provider whose coverage
    # claim exceeds its data (the Allen Coral Atlas library before any
    # package is downloaded) must not starve the ones behind it.
    for definition in definitions:
        if UI.red_flag:
            return None
        code = definition["code"]
        cells = [
            _resolve_band_cell(tile, cell_column, cell_row, code)
            for (cell_column, cell_row) in cell_indices
        ]
        cells_total = len(cells)

        # Overhang cells honour the OWNER tile's durable no-coverage
        # negatives (one stamp read per neighbour, on demand).
        owner_stamp_negatives = {}

        def _owner_recorded_no_coverage(cell):
            owner_stamp_path = cell["owner_stamp_path"]
            if not owner_stamp_path:
                return False
            if owner_stamp_path not in owner_stamp_negatives:
                owner_stamp_negatives[owner_stamp_path] = {
                    stem
                    for (stem, outcome) in _read_band_stamp(
                        owner_stamp_path
                    ).get("cells", {}).items()
                    if outcome == NO_COVERAGE
                }
            return (
                cell["owner_stem"] in owner_stamp_negatives[owner_stamp_path]
            )

        UI.vprint(
            1,
            "   Bathymetry band from",
            code,
            "-",
            cells_total,
            "cell(s) at",
            BATHYMETRY_CELL_RESOLUTION_M,
            "m.",
        )

        def _fetch_cell(cell):
            """Fetch one missing cell; returns (stem, outcome or None).

            The strategy writes into a pid-suffixed temporary path that
            is :func:`os.replace`d onto the final cell path only once it
            verifies — a concurrent reader (or a crash mid-download)
            never sees a partial cell file.
            """
            if UI.red_flag:
                # A cancelled build drains the fan-out quickly instead of
                # keeping the worker child alive on background fetches.
                return (cell["stem"], None)
            # Overhang cells live in the (possibly not yet created)
            # neighbour tile's band directory.
            os.makedirs(os.path.dirname(cell["path"]), exist_ok=True)
            temporary_path = "%s.part%d.tif" % (
                cell["path"],
                os.getpid(),
            )
            try:
                provenance = INSETS.fetch_inset(
                    definition,
                    (
                        tile.lon
                        + cell["column"] * BATHYMETRY_CELL_DEGREES,
                        tile.lat + cell["row"] * BATHYMETRY_CELL_DEGREES,
                        tile.lon
                        + (cell["column"] + 1) * BATHYMETRY_CELL_DEGREES,
                        tile.lat
                        + (cell["row"] + 1) * BATHYMETRY_CELL_DEGREES,
                    ),
                    BATHYMETRY_CELL_RESOLUTION_M,
                    temporary_path,
                )
            except Exception as error:
                # Transient (network/GDAL) failure: skipped, not recorded
                # as a durable negative.
                UI.vprint(
                    2,
                    "   Bathymetry band cell fetch error at",
                    cell["stem"],
                    ":",
                    str(error),
                )
                provenance = None
                outcome = None
            else:
                if provenance is None:
                    outcome = NO_COVERAGE
                else:
                    file_state = _cell_file_state(temporary_path)
                    if file_state == "valid":
                        os.replace(temporary_path, cell["path"])
                        return (cell["stem"], "ok")
                    if file_state == "empty":
                        # A readable raster with zero valid pixels: the
                        # provider has no data in this window — durable.
                        outcome = NO_COVERAGE
                    else:
                        # Truncated/unreadable download: transient.
                        UI.vprint(
                            2,
                            "   Bathymetry band cell",
                            cell["stem"],
                            "downloaded unreadable; will refetch on the"
                            " next build.",
                        )
                        outcome = None
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            return (cell["stem"], outcome)

        def _scan_cells(candidate_cells):
            """Split cells into (already-settled count, still missing).

            A cached cell only settles when its file verifies — a
            truncated or fully-nodata leftover (interrupted or formerly
            racing fetch) is deleted and refetched; ``index.json`` alone
            is never trusted.
            """
            settled = 0
            still_missing = []
            for cell in candidate_cells:
                if os.path.isfile(cell["path"]):
                    if _cell_file_state(cell["path"]) == "valid":
                        cell_outcomes[cell["stem"]] = "ok"
                        settled += 1
                        continue
                    UI.vprint(
                        1,
                        "   Bathymetry band: cached cell",
                        cell["stem"],
                        "is broken (interrupted earlier fetch);"
                        " refetching it.",
                    )
                    try:
                        os.remove(cell["path"])
                    except OSError:
                        pass
                if cell_outcomes.get(cell["stem"]) == NO_COVERAGE:
                    settled += 1
                    continue
                if _owner_recorded_no_coverage(cell):
                    cell_outcomes[cell["stem"]] = NO_COVERAGE
                    settled += 1
                    continue
                still_missing.append(cell)
            return (settled, still_missing)

        (cells_done, missing_cells) = _scan_cells(cells)

        if missing_cells:

            def _waiting_progress():
                """Progress while another process fetches: its cells
                appear on disk one by one."""
                cells_present = sum(
                    1
                    for cell in missing_cells
                    if os.path.isfile(cell["path"])
                )
                _report_band_progress(
                    cells_done + cells_present, cells_total
                )

            if not _acquire_band_lock(band_directory, _waiting_progress):
                UI.vprint(
                    1, "   Bathymetry band fetch cancelled."
                )
                return None
            try:
                # Another process may have fetched cells (and recorded
                # durable negatives) while we waited on its lock:
                # honour its stamp and rescan before downloading.
                for stem, outcome in _read_band_stamp(stamp_path).get(
                    "cells", {}
                ).items():
                    if outcome == NO_COVERAGE:
                        cell_outcomes.setdefault(stem, outcome)
                (resumed, missing_cells) = _scan_cells(missing_cells)
                cells_done += resumed
                _report_band_progress(cells_done, cells_total)

                def _consume_fetch_result(stem, outcome):
                    # Main thread only (results are consumed serially).
                    nonlocal cells_done
                    if outcome is not None:
                        cell_outcomes[stem] = outcome
                    cells_done += 1
                    UI.vprint(
                        1,
                        "   Bathymetry band:",
                        cells_done,
                        "/",
                        cells_total,
                        "cell(s) done.",
                    )
                    _report_band_progress(cells_done, cells_total)
                    _refresh_band_lock(band_directory)

                if missing_cells and not UI.red_flag:
                    # The first cell fetches alone: it warms the
                    # provider's authenticated session (one sign-in, not
                    # a stampede) and settles discovery caches; the rest
                    # fan out in parallel.
                    (stem, outcome) = _fetch_cell(missing_cells[0])
                    _consume_fetch_result(stem, outcome)
                    remaining_cells = missing_cells[1:]
                    if remaining_cells and not UI.red_flag:
                        from concurrent.futures import (
                            ThreadPoolExecutor,
                            as_completed,
                        )

                        with ThreadPoolExecutor(
                            max_workers=_cell_fetch_worker_count(
                                len(remaining_cells)
                            )
                        ) as fetch_pool:
                            fetch_futures = [
                                fetch_pool.submit(_fetch_cell, cell)
                                for cell in remaining_cells
                            ]
                            for fetch_future in as_completed(
                                fetch_futures
                            ):
                                (stem, outcome) = fetch_future.result()
                                _consume_fetch_result(stem, outcome)

                _write_band_stamp(
                    stamp_path,
                    {
                        "provider": code,
                        "cells": cell_outcomes,
                        "checked": datetime.date.today().isoformat(),
                    },
                )
            finally:
                _release_band_lock(band_directory)
        else:
            _report_band_progress(cells_done, cells_total)
            # Nothing to fetch: refresh the stamp only when it is out of
            # date, and only if no other process is mid-fetch (its final
            # write supersedes ours anyway).
            if (
                previous_stamp.get("provider") != code
                or previous_stamp.get("cells") != cell_outcomes
            ) and _acquire_band_lock(band_directory, wait=False):
                try:
                    _write_band_stamp(
                        stamp_path,
                        {
                            "provider": code,
                            "cells": cell_outcomes,
                            "checked": datetime.date.today().isoformat(),
                        },
                    )
                finally:
                    _release_band_lock(band_directory)

        existing_cells = [
            cell for cell in cells if os.path.isfile(cell["path"])
        ]
        if not existing_cells:
            UI.vprint(
                1,
                "   INFO:",
                code,
                "yielded no bathymetry for this tile; trying the next"
                " covering provider."
                if definition is not definitions[-1]
                else "yielded no bathymetry for this tile.",
            )
            continue

        vrt_path = FNAMES.bathymetry_band_vrt(tile.lat, tile.lon, code)
        # Built beside its final name then renamed into place: another
        # process may be reading the previous mosaic right now, and a
        # rename within the directory keeps the cell references valid.
        temporary_vrt_path = "%s.part%d" % (vrt_path, os.getpid())
        mosaic = gdal.BuildVRT(
            temporary_vrt_path,
            [cell["path"] for cell in existing_cells],
            options=gdal.BuildVRTOptions(
                resolution="highest",
                resampleAlg="bilinear",
                srcNodata=-32768,
                VRTNodata=-32768,
            ),
        )
        mosaic = None  # release the handle: flush the mosaic to disk
        os.replace(temporary_vrt_path, vrt_path)
        return vrt_path

    UI.lvprint(
        0,
        "   WARNING: no bathymetry band cell could be fetched for this"
        " tile; masks keep their distance-only water fade.",
    )
    return None

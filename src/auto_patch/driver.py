"""Auto-generate runway slope patches from CIFP/AIRAC aeronautical data.

This module parses ARINC 424 (CIFP) data files to extract precise runway
threshold elevations and coordinates, then generates .patch.osm files that
provide accurate runway slope profiles. These auto-patches replace the
default polynomial-fit altitude model with authoritative aeronautical data.

Auto-generated patches are named {ICAO}_auto.patch.osm and are given lower
priority than user-provided manual patches.
"""
from __future__ import annotations

import os
import re
from math import cos, sin, pi, sqrt, floor, atan2, acos

from shapely import geometry as shp_geom
from shapely import ops as shp_ops
from shapely.errors import GEOSException, TopologicalError

# Driver harness tuple — covers expected runtime failure modes for a
# per-airport pass.  Specifically OMITS NameError / AttributeError /
# ImportError so typos and broken imports propagate immediately
# rather than being silently logged and skipped.
_DRIVER_EXC = (OSError, ValueError, TypeError, KeyError,
               IndexError, RuntimeError,
               GEOSException, TopologicalError)

import O4_UI_Utils as UI
import O4_File_Names as FNAMES
from .cifp_reader import (
    airport_in_tile,
    discover_cifp_airports,
    find_aptdat,
    parse_cifp_file,
    xplane_root_from_cifp_path,
)
from .pavement.runway_geometry import (
    DEFAULT_RUNWAY_WIDTH,
    extend_point,
    pair_runways,
    parse_aptdat_runway_widths,
    runway_corners,
)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
FT_TO_M = 0.3048  # left over from the dead surface-patches code (slice 0)
# DEFAULT_RUNWAY_WIDTH imported from O4_Runway_Geometry above.

# FAA AC 150/5300-13B grade limits for Approach Category C-E airports.
MAX_TAXIWAY_GRADE = 0.015     # 1.5% max longitudinal grade for taxiways

# FAA vertical-curve rules — taxiway counterpart of the runway value.
# Value lives in config.py (single source of truth); re-exported here under
# the historical local name.
from .config import TAXIWAY_MAX_GRADE_CHANGE_PER_M as \
    MAX_TAXIWAY_GRADE_CHANGE_PER_M  # noqa: E402

DEFAULT_STEEPNESS = 2
# Maximum number of runway chunks for a single patch polygon
MAX_NODE_ID = -1  # will be decremented for each new node

# Debug toggle: when set to "1" via O4_DEBUG_TAXI_ONLY env var, the surface
# generator skips every non-taxi emission phase (apron, buildings, coverage
# fill, transition strips, junction triangles, boundary band, tunnel portals,
# drainage).  Runway segments still come out via generate_patch_osm.  This
# isolates the Phase C0 taxi rect output for visual debugging without the
# rest of the pipeline obscuring it.
DEBUG_TAXI_ONLY = os.environ.get("O4_DEBUG_TAXI_ONLY", "0") == "1"

# Phase C0 source toggle: when "1" (the default), Phase C0 emits taxi rects
# from OSM centerlines clipped against the apt.dat pavement union.
DEBUG_OSM_CENTERLINES = os.environ.get("O4_OSM_CENTERLINES", "1") == "1"

# New pavement model toggle.
DEBUG_NEW_MODEL = os.environ.get("O4_NEW_MODEL", "0") == "1"

# Strip-model toggle.
DEBUG_STRIP_MODEL = os.environ.get("O4_STRIP_MODEL", "0") == "1"

# True when ANY of the replacement pavement models is active.
DEBUG_REPLACE_LEGACY_PAVEMENT = DEBUG_NEW_MODEL or DEBUG_STRIP_MODEL


# ──────────────────────────────────────────────────────────────────────────────
# Runway-segment patch emission (re-exported from
# O4_Pavement_Runway_Segments)
# ──────────────────────────────────────────────────────────────────────────────
from .pavement.runway_segments import (
    DEFAULT_CELL_SIZE,
    DEFAULT_PROFILE,
    DEG_TO_M,
    GRADE_RELAX_ITERATIONS,
    MAX_RUNWAY_GRADE,
    MAX_RUNWAY_GRADE_CHANGE_PER_M,
    OVERRUN_EXTENSION,
    RUNWAY_MARGIN,
    RUNWAY_SEGMENT_LENGTH,
    generate_patch_osm,
)




# ──────────────────────────────────────────────────────────────────────────────
# Patch freshness — skip rebuilding when the existing auto-patch is current
# ──────────────────────────────────────────────────────────────────────────────
def _auto_patch_is_current(auto_patch_file: str, xp_root: str,
                           icao: str) -> bool:
    """True when an existing auto-patch can be reused as-is.

    A patch is current when the apt.dat that would be selected for
    this airport TODAY is the same file the patch was built from
    (path match — catches a newly installed Custom Scenery pack
    taking selection priority) AND that apt.dat is unmodified since
    the build (mtime match — catches an in-place airport update).

    Provenance comes from the ``o4_apt_dat`` / ``o4_apt_dat_mtime``
    attributes ``PavementLayout.to_osm`` stamps on the ``<osm>``
    root.  Patches that pre-date the stamp report not-current and
    rebuild once (getting stamped in the process).

    Set ``O4_AUTO_PATCH_REBUILD=1`` to force rebuilds regardless
    (e.g. after editing auto_patch source — code changes do NOT
    invalidate an existing patch on their own).
    """
    if os.environ.get("O4_AUTO_PATCH_REBUILD", "0") == "1":
        return False
    if not os.path.isfile(auto_patch_file):
        return False
    from .layout import read_patch_source
    meta = read_patch_source(auto_patch_file)
    if not meta:
        return False
    from .osm_load import _pick_best_apt_dat_against_osm
    apt_now = _pick_best_apt_dat_against_osm(xp_root, icao)
    if not apt_now:
        return False
    if os.path.realpath(apt_now) != os.path.realpath(meta["apt_dat"]):
        return False
    try:
        mtime_now = os.path.getmtime(apt_now)
    except OSError:
        return False
    stored = meta.get("apt_dat_mtime")
    if stored is None:
        # Stamp carries a path but no mtime (apt.dat was unreadable
        # at emit time): fall back to file-date ordering against the
        # patch itself.
        try:
            return mtime_now <= os.path.getmtime(auto_patch_file)
        except OSError:
            return False
    # Exact-match, not newer-than: replacing an airport with an OLDER
    # apt.dat (pack downgrade / restore) must also trigger a rebuild.
    return abs(mtime_now - stored) < 1e-6


# ──────────────────────────────────────────────────────────────────────────────
# Per-airport build worker (shared by the serial and parallel paths)
# ──────────────────────────────────────────────────────────────────────────────
# The tile DEM is the ONE big shared input across a tile's airports.  In the
# parallel path it is set once per worker by the ProcessPool initializer; in the
# serial path the driver sets it once in-process.  Keeping it out of the per-task
# payload avoids re-pickling ~50 MB for every airport.
_WORKER_DEM = None


def _set_worker_dem(dem) -> None:
    global _WORKER_DEM
    _WORKER_DEM = dem


def _init_worker(dem, progress_queue) -> None:
    """ProcessPool initializer: set the shared tile DEM AND route this worker's
    per-phase build progress to the shared queue the main process drains, so the
    Ortho4XP window keeps updating live while airports build in the background."""
    _set_worker_dem(dem)
    from . import progress as _progress
    _progress.set_worker_queue(progress_queue)


def _build_write_verify_one(task: dict) -> dict:
    """Build ONE airport, write its ``*_auto.patch.osm``, and verify it.

    A top-level (picklable) worker for the per-airport ProcessPool AND the serial
    path, so both run identical logic.  Reads the shared tile DEM from the module
    global ``_WORKER_DEM``.  Returns a status dict — the MAIN process does all
    console logging so parallel workers never interleave output.  ``task`` keys:
    icao, xp_root, taxiway_data, boundary, tile_lat, tile_lon, auto_patch_file,
    verify_log_path.
    """
    import time as _time
    from collections import Counter as _Counter
    icao = task["icao"]
    t_apt = _time.time()
    try:
        from .pipeline import build_airport_pavement
        layout = build_airport_pavement(
            icao, task["xp_root"],
            taxiway_data=task["taxiway_data"],
            tile_dem=_WORKER_DEM,
            airport_boundary=task["boundary"],
            current_tile_lat=task["tile_lat"],
            current_tile_lon=task["tile_lon"],
        )
    except _DRIVER_EXC as _e:
        return {"icao": icao, "ok": False, "stage": "build", "error": str(_e)}
    try:
        _pd = os.path.dirname(task["auto_patch_file"])
        if _pd and not os.path.exists(_pd):
            os.makedirs(_pd)
        layout.to_osm(task["auto_patch_file"])
    except _DRIVER_EXC as _e:
        return {"icao": icao, "ok": False, "stage": "write", "error": str(_e),
                "auto_patch_file": task["auto_patch_file"]}
    counts = _Counter(s.role for s in layout.shapes)
    summary = " + ".join("{} {}".format(n, r) for r, n in
                         sorted(counts.items(), key=lambda x: -x[1]))
    build_s = _time.time() - t_apt
    # Verify to a PER-AIRPORT log part (the main process concatenates them in
    # order) so parallel workers don't race on the shared verify debug log.
    t_v = _time.time()
    verify_err = None
    try:
        from .verification import verify_and_log
        verify_and_log(layout, icao, debug_log_path=task["verify_log_path"])
    except _DRIVER_EXC as _ve:
        verify_err = str(_ve)
    return {"icao": icao, "ok": True, "summary": summary, "build_s": build_s,
            "verify_s": _time.time() - t_v, "verify_err": verify_err,
            "verify_log_path": task["verify_log_path"]}


def _run_build_tasks(tasks: list, tile, auto_patched: list,
                     verify_debug_path: str) -> None:
    """Run the collected per-airport build tasks — in parallel across airports
    when ``O4_PARALLEL_AIRPORTS`` is set and there is more than one, otherwise
    serially (behaviourally identical to the old inline loop).  All console
    logging + the verify-log concatenation happen HERE (main process) so
    parallel workers never race or interleave.  Appends built ICAOs to
    ``auto_patched`` in task order for stable output."""
    if not tasks:
        return
    from . import config as _cfg
    dem = getattr(tile, "dem", None)
    # Truncate the shared verify debug log once per build pass.
    try:
        open(verify_debug_path, "w").close()
    except OSError:
        pass
    _set_worker_dem(dem)            # the serial path reads this module global too

    results: list[dict] = []
    if _cfg.PARALLEL_AIRPORTS and len(tasks) > 1:
        import concurrent.futures as _cf
        import multiprocessing as _mp
        import queue as _queue
        n = _cfg.parallel_airports_worker_count(len(tasks))
        UI.lvprint(0, "   Auto-patch: building", len(tasks), "airports (" +
                   ", ".join(t["icao"] for t in tasks) +
                   ") in parallel across", n, "workers.")
        mgr = None
        try:
            ctx = _mp.get_context("spawn")
            mgr = ctx.Manager()
            pq = mgr.Queue()

            def _drain_progress() -> None:
                # Print each worker's pending phase events on the MAIN thread
                # (same thread as the serial UI, so no GUI-thread hazard).  Keeps
                # the window alive while airports build in the background.
                while True:
                    try:
                        _icao, _step, _tot, _lab = pq.get_nowait()
                    except _queue.Empty:
                        break
                    except Exception:
                        break
                    UI.lvprint(0, "   Auto-patch: {} [{}/{}] {}".format(
                        _icao, _step, _tot, _lab))

            with _cf.ProcessPoolExecutor(
                    max_workers=n, mp_context=ctx,
                    initializer=_init_worker, initargs=(dem, pq)) as ex:
                futs = [ex.submit(_build_write_verify_one, t) for t in tasks]
                pending = set(futs)
                while pending:
                    done, pending = _cf.wait(
                        pending, timeout=0.5,
                        return_when=_cf.FIRST_COMPLETED)
                    _drain_progress()
                    for fut in done:
                        try:
                            results.append(fut.result())
                        except Exception as _e:         # a worker died hard
                            results.append({"icao": None, "ok": False,
                                            "stage": "worker", "error": str(_e)})
                _drain_progress()                        # flush trailing events
        except Exception as _e:      # pool/manager setup failed → serial fallback
            UI.lvprint(0, "   Auto-patch: parallel build unavailable (",
                       str(_e), ") — falling back to serial.")
            results = [_build_write_verify_one(t) for t in tasks]
        finally:
            if mgr is not None:
                try:
                    mgr.shutdown()
                except Exception:
                    pass
    else:
        results = [_build_write_verify_one(t) for t in tasks]

    # Process results in TASK order (stable logs / auto_patched ordering).
    by_icao = {r.get("icao"): r for r in results if r.get("icao")}
    for t in tasks:
        r = by_icao.get(t["icao"]) or {"icao": t["icao"], "ok": False,
                                       "stage": "missing", "error": "no result"}
        icao = t["icao"]
        if not r.get("ok"):
            stage = r.get("stage", "?")
            if stage == "write":
                UI.lvprint(0, "   Auto-patch: Failed to write",
                           t["auto_patch_file"], ":", r.get("error"))
            else:
                UI.lvprint(0, "   Auto-patch: Pavement builder failed for",
                           icao, "(", stage, "):", r.get("error"))
            continue
        UI.vprint(1, "   Auto-patch: Generated", icao,
                  "(" + r["summary"] + ")")
        auto_patched.append(icao)
        if r.get("verify_err"):
            UI.lvprint(0, "   Auto-patch: verification error for", icao,
                       ":", r["verify_err"])
        # Concatenate this airport's verify-log part into the shared log.
        part = r.get("verify_log_path")
        if part and os.path.exists(part):
            try:
                with open(part) as _pf, open(verify_debug_path, "a") as _lf:
                    _lf.write(_pf.read())
                os.remove(part)
            except OSError:
                pass
        UI.lvprint(0, "   Auto-patch:", icao,
                   f"took {r['build_s']:.1f}s (verify {r['verify_s']:.1f}s)")


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────────────
def generate_auto_patches(tile, cifp_path: str,
                          taxiway_data=None,
                          building_data=None,
                          dico_airports: dict | None = None,
                          road_data=None,
                          mode: str = "ICAO") -> list[str]:
    """Generate auto-patch files for all CIFP airports within a tile.

    Scans the CIFP directory for airport data files, parses runway threshold
    data, and writes {ICAO}_auto.patch.osm files into the tile's Patches
    directory.

    Auto-patches cover the full airport surface as a single non-overlapping
    mesh when building data is available:
    1. Runway slope patches from CIFP threshold elevations
    2. Building flattening (flat altitude=N footprints)
    3. Grade-limited transition triangles (taxiways, aprons, surrounding area)

    When no building data is available, falls back to runway-only patches
    using altitude_high/altitude_low rectangles.

    Args:
        tile: Tile object with .lat, .lon, and .dem attributes.
        cifp_path: Path to the CIFP data directory.
        taxiway_data: Optional dict from extract_taxiway_info(), or a
                      zero-arg callable returning one (invoked lazily —
                      see below).
        building_data: Optional dict from extract_building_info(), or a
                       zero-arg callable returning one.
        dico_airports: Optional dict with processed airport data (provides
                       apron geometry and boundaries).
        road_data: Optional dict from extract_road_info(), or a zero-arg
                   callable returning one.

    ``taxiway_data`` / ``building_data`` / ``road_data`` passed as
    callables are resolved only when the first airport survives every
    skip check (manual patch, not-in-tile, up-to-date auto-patch) and
    actually needs a rebuild.  A tile whose patches are all current
    therefore never pays for — or logs — the OSM taxiway/building/road
    extraction.
        mode: "ICAO" (default) only patches airports with a 4-letter ICAO
              code; "All" patches every CIFP airport regardless of code
              format. ("None" is handled at the call site by skipping this
              function entirely.)

    Returns:
        list: ICAO codes of airports for which auto-patches were generated.
    """
    if not cifp_path or not os.path.isdir(cifp_path):
        UI.vprint(
            1,
            "   Auto-patch: CIFP directory not found at",
            cifp_path,
            ", skipping.",
        )
        return []

    tile_lat = int(floor(tile.lat))
    tile_lon = int(floor(tile.lon))
    patch_dir = FNAMES.patch_dir(tile_lat, tile_lon)

    # Discover which manual patches already exist
    manual_patches = set()
    if os.path.exists(patch_dir):
        for fname in os.listdir(patch_dir):
            if fname.endswith(".patch.osm") and "_auto.patch.osm" not in fname:
                # Extract probable ICAO code from filename
                base = fname[:-10]  # strip .patch.osm
                # The ICAO prefix is the part before any underscore, or the
                # whole base name if no underscore
                icao_prefix = base.split("_")[0].upper()
                manual_patches.add(icao_prefix)
            elif os.path.isdir(os.path.join(patch_dir, fname)):
                manual_patches.add(fname.upper())

    # Scan all CIFP airports
    cifp_airports = discover_cifp_airports(cifp_path)
    auto_patched: list[str] = []
    reused: list[str] = []
    tasks: list[dict] = []          # per-airport build tasks, executed post-loop

    # Apply the auto-patch log-verbosity knob for the build (restored
    # after the loop).  Build-time verification still runs at every
    # level — only the chatter volume changes.
    from . import config as _cfg
    _saved_verbosity = UI.verbosity
    UI.verbosity = _cfg.LOG_VERBOSITY

    # Per-tile verify DEBUG log: the non-user-actionable verify findings
    # (overlap / off-source / within-shape grade — our geometry/solver bugs,
    # not anything the user can fix in the source) are written here instead
    # of being surfaced as [verify] chatter, for an engineer to track down.
    # Truncated once per build pass by _run_build_tasks, which then appends each
    # airport's verify-log part into it (safe under parallel builds).
    _verify_debug_path = os.path.join(patch_dir, "auto_patch_verify_debug.log")

    # Lazy tile-level inputs: callables resolve on the FIRST airport
    # that needs a rebuild, at the tile's own verbosity so their log
    # output matches the eager-path chatter exactly.  All-current tiles
    # never invoke them.
    _inputs_resolved = False

    def _resolve_lazy_inputs():
        nonlocal taxiway_data, building_data, road_data, _inputs_resolved
        if _inputs_resolved:
            return
        _inputs_resolved = True
        prev_verbosity = UI.verbosity
        UI.verbosity = _saved_verbosity
        try:
            if callable(taxiway_data):
                taxiway_data = taxiway_data()
            if callable(building_data):
                building_data = building_data()
            if callable(road_data):
                road_data = road_data()
        finally:
            UI.verbosity = prev_verbosity
        if taxiway_data is None:
            taxiway_data = {}
        if building_data is None:
            building_data = {}

    for icao, filepath in sorted(cifp_airports.items()):
        # In ICAO mode, only patch airports with a real 4-letter ICAO code
        # (skip 3-letter FAA codes and alphanumeric local-use codes like "1A2")
        if mode == "ICAO" and not (len(icao) == 4 and icao.isalpha()):
            UI.vprint(
                2,
                "   Auto-patch: Skipping",
                icao,
                "(non-ICAO code, mode=ICAO).",
            )
            continue
        # Skip if a manual patch already covers this airport
        if icao in manual_patches:
            UI.vprint(
                2,
                "   Auto-patch: Skipping",
                icao,
                "(manual patch exists).",
            )
            continue

        # Parse runway data
        runways = parse_cifp_file(filepath)
        if not runways:
            continue

        # Check if any runway falls within this tile
        if not airport_in_tile(runways, tile_lat, tile_lon):
            continue

        # Pair runways and generate patch
        pairs = pair_runways(runways)
        if not pairs:
            continue

        xp_root = xplane_root_from_cifp_path(cifp_path)
        if xp_root is None:
            UI.vprint(
                1, "   Auto-patch: Skipping", icao,
                "(cannot resolve X-Plane root from CIFP path).")
            continue

        # Reuse the existing auto-patch when it was built from the
        # apt.dat that would be selected today and that apt.dat is
        # unchanged since — runs BEFORE any expensive per-airport
        # work.  include_patches() picks the file up from disk either
        # way; nothing downstream needs the rebuild.
        auto_patch_file = os.path.join(
            patch_dir, "{}_auto.patch.osm".format(icao)
        )
        if _auto_patch_is_current(auto_patch_file, xp_root, icao):
            UI.lvprint(
                0, "   Auto-patch:", icao,
                "up to date (apt.dat unchanged), reusing existing patch.")
            reused.append(icao)
            continue

        # This airport WILL be rebuilt — now (and only now) pay for the
        # tile-level OSM extraction if it was deferred.
        _resolve_lazy_inputs()

        # Look up actual runway widths from apt.dat
        runway_widths = {}
        aptdat_path = find_aptdat(cifp_path)
        if aptdat_path:
            runway_widths = parse_aptdat_runway_widths(aptdat_path, icao)
            if runway_widths:
                UI.vprint(
                    2,
                    "   Auto-patch: Got runway widths from apt.dat for",
                    icao,
                )

        # Build runway pair data for elevation interpolation (shared by
        # taxiway and building patch generation)
        rwy_pairs_for_elev = []
        for desig_a, data_a, desig_b, data_b in pairs:
            if data_b is not None:
                rwy_pairs_for_elev.append({
                    "data_a": data_a,
                    "data_b": data_b,
                    "desig_a": desig_a,
                    "desig_b": desig_b,
                })

        has_dem = hasattr(tile, "dem") and tile.dem is not None

        # Collect taxiway and building data for this airport
        airport_taxiways = (
            taxiway_data.get(icao)
            or taxiway_data.get(icao.upper())
            or taxiway_data.get(icao.lower())
        ) if taxiway_data else None
        airport_buildings = (
            building_data.get(icao)
            or building_data.get(icao.upper())
            or building_data.get(icao.lower())
        ) if building_data else None

        # Look up the airport's processed data (aprons, boundary, etc.)
        dico_apt_entry = {}
        if dico_airports:
            dico_apt_entry = (
                dico_airports.get(icao)
                or dico_airports.get(icao.upper())
                or dico_airports.get(icao.lower())
                or {}
            )

        # ── Collect the build task ──────────────────────────────────────
        # The build + write + verify (identical logic for the serial and the
        # parallel paths) is done by ``_build_write_verify_one`` after the loop.
        # ``dico_apt_entry['boundary']`` is currently a reserved slot (auto_patch
        # derives its outline from apt.dat row-130 until a source-of-truth is
        # chosen).  ``current_tile_*`` is the CURRENT tile (not the airport anchor)
        # so tile_cut drops the right pieces for cross-tile airports.
        tasks.append({
            "icao": icao,
            "xp_root": xp_root,
            "taxiway_data": airport_taxiways,
            "boundary": (dico_apt_entry.get("boundary")
                         if dico_apt_entry else None),
            "tile_lat": tile_lat,
            "tile_lon": tile_lon,
            "auto_patch_file": auto_patch_file,
            "verify_log_path": _verify_debug_path + "." + icao + ".part",
        })

    # ── Execute the collected build tasks ────────────────────────────────
    # Each airport is independent, so with O4_PARALLEL_AIRPORTS they run across a
    # ProcessPool (the tile DEM shared once per worker via the initializer); the
    # MAIN process does all logging + the verify-log concatenation so nothing
    # races or interleaves.  Serial path (default) is behaviourally identical to
    # the old inline loop.
    _run_build_tasks(tasks, tile, auto_patched, _verify_debug_path)

    UI.verbosity = _saved_verbosity

    if auto_patched:
        UI.vprint(
            0,
            "   Auto-patch: Generated patches for {} airports.".format(
                len(auto_patched)
            ),
        )
    if reused:
        UI.vprint(
            0,
            "   Auto-patch: Reused {} up-to-date existing patches.".format(
                len(reused)
            ),
        )
    if not auto_patched and not reused:
        UI.vprint(2, "   Auto-patch: No airports with CIFP data in this tile.")

    return auto_patched



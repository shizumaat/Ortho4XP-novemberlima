"""Pytest configuration shared by all tests.

* Adds the project's ``src/`` directory to ``sys.path`` so tests can
  import the O4 modules directly without installing the package.
* Provides the airport-discovery + ship-mode toggle helpers used by
  the integration test suites (overlap, junction invariants, grade,
  geometry, …) so airports are not hard-coded in any test file.

Environment variables (per user 2026-04-30):
* ``O4_TEST_TILE=lat,lon`` — discover every airport whose runways
  fall in that 1°×1° tile via the project's CIFP scanner.  This
  matches "all airports in the tile being built".
* ``O4_TEST_AIRPORTS=ICAO1,ICAO2,…`` — explicit ICAO list, takes
  precedence over ``O4_TEST_TILE`` when both are set.
* ``O4_SHIP_MODE=1`` — skip every integration test (used at
  shipping time when tests should not run).  All test modules
  collect normally but each item is marked skip.
* ``XPLANE_ROOT`` — X-Plane install path used by every test that
  needs CIFP / DEM / apt.dat data.  Defaults to
  ``/Users/noah/X-Plane 12``.

When neither ``O4_TEST_TILE`` nor ``O4_TEST_AIRPORTS`` is set, the
discovery returns an empty list and parametrised integration tests
collect zero items.  This is intentional: the project should not
ship a hidden hard-coded list of canonical airports — every run
must be explicit about what it tests against.
"""
import os
import sys
from typing import List, Optional

# No test may reach the network: the parallel-build OpenStreetMap cache
# warmer (o4_engine.parallel) is disabled suite-wide; tests that exercise
# the warmer itself delete this variable and stub the download modules.
os.environ.setdefault("O4_DISABLE_OSM_WARMER", "1")

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def xplane_root() -> str:
    return os.environ.get("XPLANE_ROOT", "/Users/noah/X-Plane 12")


def xplane_available() -> bool:
    root = xplane_root()
    return (os.path.isdir(root)
            and os.path.isdir(os.path.join(root, "Custom Data", "CIFP")))


import functools


@functools.lru_cache(maxsize=None)
def _build_cached(icao: str, compute_elevations: bool,
                  tile_lat, tile_lon):
    """Build one airport layout, memoised for the worker's session.

    A full ``build_airport_pavement`` is the dominant cost of the suite
    (each airport ~30-60 s of geometry + elevation solve), and many
    test modules each need the SAME layout.  This single shared cache
    replaces the per-module ``_LAYOUT_CACHE`` dicts (and the modules
    that rebuilt per-test) so every (icao, params) combination is built
    AT MOST ONCE per worker process.  Combined with ``--dist loadgroup``
    + the per-airport ``xdist_group`` assigned in
    ``pytest_collection_modifyitems`` below, all of an airport's tests
    run on one worker → exactly one build per airport per run.

    The per-tile DEM is built INSIDE (so the cache key stays hashable) via
    ``_load_airport_dem(tile_center)`` — the SMOOTHED (apt_smoothing_pix=8)
    surface that production ships, the SAME one the grade test uses.
    Previously this used a RAW ``O4DEM(... fill_nodata='to zero')``, which
    produces a different surface (more terrain-extrema rect splits → e.g.
    SPLP 259 vs 250 shapes) than what X-Plane renders.  Unifying on the
    smoothed DEM makes every per-tile build (grade / compare_target /
    tile_cut) production-accurate AND identical for a given tile, so they
    all share ONE cached build per tile.

    NOTE: callers treat the returned layout as READ-ONLY — it is shared
    across every test for that airport.  Do not mutate it in place.
    """
    from auto_patch.pipeline import build_airport_pavement
    if tile_lat is not None and tile_lon is not None:
        from auto_patch.elevation import _load_airport_dem
        dem = _load_airport_dem(tile_lat + 0.5, tile_lon + 0.5)
        return build_airport_pavement(
            icao, xplane_root(), compute_elevations=compute_elevations,
            tile_dem=dem, current_tile_lat=tile_lat,
            current_tile_lon=tile_lon)
    return build_airport_pavement(
        icao, xplane_root(), compute_elevations=compute_elevations)


def cached_airport_layout(icao: str, *, compute_elevations: bool = True,
                          tile_lat=None, tile_lon=None):
    """Session-cached ``build_airport_pavement`` shared by all test
    modules.  See :func:`_build_cached`.  Treat the result as read-only.
    """
    return _build_cached(icao, compute_elevations, tile_lat, tile_lon)


def is_tile_seam_vertex(layout, x: float, y: float,
                        tol_m: Optional[float] = None) -> bool:
    """True if local-metre point ``(x, y)`` lies on a tile-cut seam.

    ``tile_cut`` slices every shape crossing an integer lat/lon tile
    boundary, buffering each integer line by ``half_width_m`` so the
    surviving (current-tile) shape edges land ~that far off the line
    (``_SEAM_LINE_TOL_M`` covers the offset).  Such a vertex is
    sourced by the tile cut — NOT by an apt.dat corner or pavement
    edge — so the junction-vertex source / push-outside invariants
    exempt it (the seam position is fixed by the cut and the seam
    altitude is terrain-pinned for cross-tile stitching).
    """
    import math
    from auto_patch.layout import R_EARTH
    from auto_patch.tile_cut import _SEAM_LINE_TOL_M
    if tol_m is None:
        tol_m = _SEAM_LINE_TOL_M
    lat, lon = layout.m_to_ll(x, y)
    d_lat_m = math.radians(abs(lat - round(lat))) * R_EARTH
    d_lon_m = (math.radians(abs(lon - round(lon)))
               * R_EARTH * math.cos(math.radians(lat)))
    return min(d_lat_m, d_lon_m) <= tol_m


# Baseline airports (user 2026-05-16): these run unconditionally on
# every invariant / grade / overlap test, providing CI coverage for
# the canonical geometry classes we need to support:
#
#   * SPJC — apron-heavy, multi-ref diagonal stubs, sloped runway,
#     hand-drawn target available for structural regression.
#   * SPLP — single runway with built-up threshold pad, multi-tile
#     output, sloped runway with seam anchors.  Target available.
#   * CYXY — multi-runway crossings (D crosses 14R/32L and 14L/32R),
#     chart-level E-D junction, parallel taxi E with diagonal stub
#     E and primary-parallel south extension into the apron.
#
# Adding KBNA / HECA to this list once Phase-1 baselines are stable
# would expand coverage to (4) terminal-heavy taxi network with
# multiple intersecting parallels, and (5) wide-runway desert
# airport with extensive aprons.
#
# Hand-drawn target comparison stays SPJC/SPLP-only — those are the
# regression-gate fixtures.  Invariant tests apply universally.
_BASELINE_AIRPORTS: tuple = ("SPJC", "SPLP", "CYXY")


def baseline_airports() -> tuple:
    """Return the canonical baseline airport list every invariant
    test should run against unconditionally.  See module-level
    ``_BASELINE_AIRPORTS`` for the rationale."""
    return _BASELINE_AIRPORTS


_AIRPORTS_CACHE: Optional[List[str]] = None


def airports_under_test() -> List[str]:
    """Resolved ICAO list for parametrised integration tests.

    Cached for the lifetime of the pytest session.  See module
    docstring for the env-var contract.
    """
    global _AIRPORTS_CACHE
    if _AIRPORTS_CACHE is not None:
        return _AIRPORTS_CACHE
    explicit = os.environ.get("O4_TEST_AIRPORTS", "").strip()
    if explicit:
        _AIRPORTS_CACHE = sorted({
            a.strip().upper()
            for a in explicit.split(",")
            if a.strip()})
        return _AIRPORTS_CACHE
    tile_str = os.environ.get("O4_TEST_TILE", "").strip()
    if tile_str:
        try:
            parts = [int(p.strip()) for p in tile_str.split(",")]
            assert len(parts) == 2
            lat, lon = parts
        except (ValueError, AssertionError):
            _AIRPORTS_CACHE = []
            return _AIRPORTS_CACHE
        _AIRPORTS_CACHE = _discover_airports_in_tile(lat, lon)
        return _AIRPORTS_CACHE
    _AIRPORTS_CACHE = []
    return _AIRPORTS_CACHE


def _discover_airports_in_tile(lat: int, lon: int) -> List[str]:
    """Return sorted ICAOs whose runways fall in the 1°×1° tile.

    Uses the same CIFP scanner the build pipeline uses
    (``O4_Cifp_Reader.discover_cifp_airports`` +
    ``parse_cifp_file`` + ``airport_in_tile``) to ensure tests run
    against the exact airport set the build pipeline would touch.
    """
    cifp_path = os.path.join(xplane_root(), "Custom Data", "CIFP")
    if not os.path.isdir(cifp_path):
        return []
    from auto_patch.cifp_reader import (
        discover_cifp_airports, parse_cifp_file, airport_in_tile)
    found: List[str] = []
    for icao, filepath in discover_cifp_airports(cifp_path).items():
        # Only test against true 4-letter ICAOs (mirror the build
        # pipeline's ICAO mode).
        if not (len(icao) == 4 and icao.isalpha()):
            continue
        runways = parse_cifp_file(filepath)
        if runways and airport_in_tile(runways, lat, lon):
            found.append(icao)
    return sorted(found)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Per-airport xdist grouping + optional ship-mode skip.

    MUST run before xdist's own ``pytest_collection_modifyitems`` (remote.py),
    which converts the ``xdist_group`` marker into the ``@group`` nodeid suffix
    the loadgroup scheduler keys on.  Without ``tryfirst`` our hook runs AFTER
    xdist's, so the marker isn't present when xdist reads it → no grouping → each
    airport rebuilds on every worker (CYXY/SPJC/SPLP were each built ~14×/run).

    Under ``--dist loadgroup`` (set in ``pytest.ini``), tests sharing an
    ``xdist_group`` run on the SAME worker.  Assigning every airport-
    parametrised test the group ``<icao>`` makes all of an airport's
    tests land on one worker, so the shared :func:`cached_airport_layout`
    builds each airport exactly once per run (instead of once per worker
    that happened to pick up one of its tests).
    """
    for item in items:
        icao = item.callspec.params.get("icao") if hasattr(
            item, "callspec") else None
        if isinstance(icao, str) and icao:
            item.add_marker(pytest.mark.xdist_group(icao))

    if os.environ.get("O4_SHIP_MODE", "0") == "1":
        skip_marker = pytest.mark.skip(
            reason="O4_SHIP_MODE=1 (tests disabled for shipping)")
        for item in items:
            item.add_marker(skip_marker)

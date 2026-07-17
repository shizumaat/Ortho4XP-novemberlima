"""Tests for the tile-wide elevation-level PROVIDER paths.

Covers the agent-authored portions of ``src/O4_Elevation_Level.py`` and the
surgical edits in ``src/O4_Airport_Elevation_Insets.py``
(``docs/specs/elevation-level-spec.md``):

  * wide-area overlay provider selection over a synthetic provider registry
    (resolution-then-priority ranking, the ``supports_wide_area`` filter,
    disabled/coverage/config filtering, empty registry);
  * ``finest_wide_area_resolution_m`` including the no-coverage case;
  * ``ensure_tile_overlay`` orchestration (recycle, cached no-coverage
    negative, successful fetch with provenance + index, exception safety);
  * ``parse_working_grid_arc_seconds`` extended to factors 6 and 9 with
    legacy-value regression;
  * ``resolve_working_grid_factor`` level override (auto byte-inertness,
    the max() rule, explicit-pin precedence, custom_dem uncapped factor).

All headless: synthetic in-memory registries, no network, ``tmp_path``.  The
selection and factor tests need no GDAL; the fetch-orchestration tests mock
``fetch_inset`` and touch only ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

import O4_Airport_Elevation_Insets as INSETS
import O4_Elevation_Level as ELEVATION_LEVEL
import O4_File_Names as FNAMES


# =====================================================================
# Synthetic provider registry helpers
# =====================================================================
class _WideAreaStrategy:
    """Stand-in for a windowed reader (eligible for whole-tile overlays)."""

    supports_wide_area = True


class _NarrowStrategy:
    """Stand-in for a tile-download strategy (inset-only)."""

    supports_wide_area = False


class _UnflaggedStrategy:
    """A strategy with no flag at all -- must default to inset-only."""


_SYNTHETIC_STRATEGIES = {
    "wide": _WideAreaStrategy,
    "narrow": _NarrowStrategy,
    "unflagged": _UnflaggedStrategy,
}


def _definition(
    code,
    access_strategy="wide",
    resolution_m=1.0,
    role="airport_inset",
    enabled=True,
    priority=0.0,
    coverage_bbox=None,
):
    """Build a synthetic ``.elv`` provider definition dictionary."""
    definition = {
        "code": code,
        "access_strategy": access_strategy,
        "role": role,
        "enabled": enabled,
        "priority": priority,
    }
    if resolution_m is not None:
        definition["native_resolution_m"] = resolution_m
    if coverage_bbox is not None:
        definition["coverage_bbox"] = coverage_bbox
    return definition


def _install_registry(monkeypatch, definitions):
    """Install a synthetic strategy table + provider registry on INSETS.

    Keyed by CODE, mirroring the real ``elevation_providers_dict``.  Also
    neutralises ``initialize_elevation_providers_dict`` so a helper that
    finds the dictionary "empty" never repopulates it from disk.
    """
    registry = {definition["code"]: definition for definition in definitions}
    monkeypatch.setattr(INSETS, "ACCESS_STRATEGIES", dict(_SYNTHETIC_STRATEGIES))
    monkeypatch.setattr(INSETS, "elevation_providers_dict", registry)
    monkeypatch.setattr(
        INSETS,
        "initialize_elevation_providers_dict",
        lambda *args, **kwargs: registry,
    )
    return registry


# A tile far from any narrow coverage box; the overlay bounding box is
# (lon, lat, lon + 1, lat + 1) = (-87, 36, -86, 37).
TILE_LAT = 36
TILE_LON = -87
FAR_AWAY_BBOX = (10.0, 10.0, 11.0, 11.0)


# =====================================================================
# select_tile_overlay_definition
# =====================================================================
def test_selection_ranks_finest_resolution_first(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("FINE", resolution_m=1.0, priority=1.0),
            _definition("MID", resolution_m=5.0, priority=9.0),
            _definition("COARSE", resolution_m=10.0, priority=9.0),
        ],
    )
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 10
    )
    assert winner is not None and winner["code"] == "FINE"


def test_selection_breaks_ties_by_priority_then_code(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("ALPHA", resolution_m=1.0, priority=5.0),
            _definition("BRAVO", resolution_m=1.0, priority=9.0),
            _definition("CHARLIE", resolution_m=1.0, priority=9.0),
        ],
    )
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 10
    )
    # Same resolution: higher priority wins; BRAVO and CHARLIE share the
    # top priority, so the code breaks the final tie (BRAVO < CHARLIE).
    assert winner["code"] == "BRAVO"


def test_selection_filters_non_wide_area_strategies(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("NARROWFINE", access_strategy="narrow", resolution_m=0.5),
            _definition("UNFLAGGED", access_strategy="unflagged", resolution_m=0.5),
            _definition("WIDECOARSE", access_strategy="wide", resolution_m=10.0),
        ],
    )
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 5
    )
    # The two finer sources are tile-download strategies and are excluded
    # even though they out-resolve the only wide-area candidate.
    assert winner["code"] == "WIDECOARSE"


def test_selection_filters_disabled_definitions(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("DISABLEDFINE", resolution_m=0.5, enabled=False),
            _definition("ENABLEDCOARSE", resolution_m=10.0),
        ],
    )
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 5
    )
    assert winner["code"] == "ENABLEDCOARSE"


def test_selection_filters_by_coverage_box(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("ELSEWHERE", resolution_m=0.5, coverage_bbox=FAR_AWAY_BBOX),
            _definition("HERE", resolution_m=10.0),
        ],
    )
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 5
    )
    assert winner["code"] == "HERE"


def test_selection_considers_base_role_with_full_coverage_test(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            # A base-role definition whose coverage box does NOT contain the
            # tile centre is rejected by base_definition_covers_tile.
            _definition(
                "BASEELSEWHERE",
                resolution_m=0.5,
                role="base",
                coverage_bbox=FAR_AWAY_BBOX,
            ),
            _definition("BASEHERE", resolution_m=3.0, role="base"),
            _definition("INSETHERE", resolution_m=10.0, role="airport_inset"),
        ],
    )
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 10
    )
    # Both roles are candidates; BASEHERE (3 m) out-resolves the inset and
    # BASEELSEWHERE is filtered by its coverage test.
    assert winner["code"] == "BASEHERE"


def test_selection_providers_config_pins_and_only_filters(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("FINE", resolution_m=1.0),
            _definition("COARSE", resolution_m=10.0),
        ],
    )
    # Restricting to COARSE excludes the finer FINE; the config filters, it
    # does not re-rank (COARSE is the only survivor).
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 10, providers_config="COARSE"
    )
    assert winner["code"] == "COARSE"
    # Listing COARSE first does NOT override the finest-first ranking.
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 10, providers_config="COARSE,FINE"
    )
    assert winner["code"] == "FINE"


def test_selection_empty_registry_returns_none(monkeypatch):
    _install_registry(monkeypatch, [])
    assert (
        ELEVATION_LEVEL.select_tile_overlay_definition(TILE_LAT, TILE_LON, 10)
        is None
    )


def test_selection_ranks_unknown_resolution_last(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("KNOWN", resolution_m=10.0),
            _definition("UNKNOWNRES", resolution_m=None),
        ],
    )
    winner = ELEVATION_LEVEL.select_tile_overlay_definition(
        TILE_LAT, TILE_LON, 10
    )
    # A declared 10 m resolution beats an undeclared one (ranked as inf).
    assert winner["code"] == "KNOWN"


# =====================================================================
# finest_wide_area_resolution_m
# =====================================================================
def test_finest_resolution_over_candidates(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("FINE", resolution_m=1.0),
            _definition("MID", resolution_m=5.0),
            _definition("NARROW", access_strategy="narrow", resolution_m=0.25),
        ],
    )
    assert ELEVATION_LEVEL.finest_wide_area_resolution_m(
        TILE_LAT, TILE_LON
    ) == 1.0


def test_finest_resolution_none_when_nothing_covers(monkeypatch):
    _install_registry(
        monkeypatch,
        [
            _definition("NARROW", access_strategy="narrow", resolution_m=0.25),
            _definition("DISABLED", resolution_m=0.5, enabled=False),
            _definition("ELSEWHERE", resolution_m=0.5, coverage_bbox=FAR_AWAY_BBOX),
        ],
    )
    assert (
        ELEVATION_LEVEL.finest_wide_area_resolution_m(TILE_LAT, TILE_LON)
        is None
    )


def test_finest_resolution_none_when_only_unknown_resolution(monkeypatch):
    _install_registry(
        monkeypatch,
        [_definition("UNKNOWNRES", resolution_m=None)],
    )
    # A wide-area candidate exists but declares no resolution, so the data
    # cap has nothing to work with.
    assert (
        ELEVATION_LEVEL.finest_wide_area_resolution_m(TILE_LAT, TILE_LON)
        is None
    )


# =====================================================================
# ensure_tile_overlay orchestration
# =====================================================================
def _overlay_plan(tmp_path, code="TESTWIDE", resolution=10.29):
    """A plan whose artefact lives directly in tmp_path (parent exists)."""
    stem = code.lower() + "_" + ("%.2f" % resolution).rstrip("0").rstrip(".")
    return {
        "definition": {"code": code},
        "factor": 3,
        "target_resolution_m": resolution,
        "path": str(tmp_path / (stem + ".tif")),
    }


def _overlay_tile():
    return SimpleNamespace(lat=TILE_LAT, lon=TILE_LON, elevation_level="10")


def test_ensure_recycles_existing_overlay(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    plan = _overlay_plan(tmp_path)
    with open(plan["path"], "w") as handle:
        handle.write("cached raster")
    monkeypatch.setattr(
        ELEVATION_LEVEL, "resolve_tile_overlay_plan", lambda _tile: plan
    )
    calls = []
    monkeypatch.setattr(
        INSETS,
        "fetch_inset",
        lambda *args, **kwargs: calls.append(args) or None,
    )
    result = ELEVATION_LEVEL.ensure_tile_overlay(_overlay_tile())
    assert result == plan["path"]
    assert calls == []  # a cached artefact is never re-fetched


def test_ensure_successful_fetch_writes_provenance_and_index(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    plan = _overlay_plan(tmp_path)
    monkeypatch.setattr(
        ELEVATION_LEVEL, "resolve_tile_overlay_plan", lambda _tile: plan
    )

    def fake_fetch(definition, bounding_box, resolution_m, destination):
        with open(destination, "w") as handle:
            handle.write("fetched raster")
        return {"provider": definition["code"], "source": "synthetic"}

    monkeypatch.setattr(INSETS, "fetch_inset", fake_fetch)

    result = ELEVATION_LEVEL.ensure_tile_overlay(_overlay_tile())
    assert result == plan["path"]
    assert os.path.isfile(plan["path"])

    provenance_path = FNAMES.tile_overlay_provenance(
        TILE_LAT, TILE_LON, "TESTWIDE", plan["target_resolution_m"]
    )
    assert os.path.isfile(provenance_path)
    with open(provenance_path) as handle:
        provenance = json.load(handle)
    assert provenance["provider"] == "TESTWIDE"
    assert "fetch_date" in provenance  # the fetch stamps the date

    index_path = FNAMES.tile_overlay_index(TILE_LAT, TILE_LON)
    with open(index_path) as handle:
        index = json.load(handle)
    stem = os.path.splitext(os.path.basename(plan["path"]))[0]
    assert index[stem] == "ok"


def test_ensure_no_coverage_negative_is_written_and_honoured(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    plan = _overlay_plan(tmp_path)
    monkeypatch.setattr(
        ELEVATION_LEVEL, "resolve_tile_overlay_plan", lambda _tile: plan
    )
    call_count = {"n": 0}

    def clean_no_coverage(*args, **kwargs):
        call_count["n"] += 1
        return None  # a clean "no usable coverage" answer

    monkeypatch.setattr(INSETS, "fetch_inset", clean_no_coverage)

    assert ELEVATION_LEVEL.ensure_tile_overlay(_overlay_tile()) is None
    index_path = FNAMES.tile_overlay_index(TILE_LAT, TILE_LON)
    with open(index_path) as handle:
        index = json.load(handle)
    stem = os.path.splitext(os.path.basename(plan["path"]))[0]
    assert index[stem] == INSETS.NO_COVERAGE

    # A second call honours the cached negative without re-querying.
    assert ELEVATION_LEVEL.ensure_tile_overlay(_overlay_tile()) is None
    assert call_count["n"] == 1


def test_ensure_fetch_exception_returns_none_without_poisoning_cache(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    plan = _overlay_plan(tmp_path)
    monkeypatch.setattr(
        ELEVATION_LEVEL, "resolve_tile_overlay_plan", lambda _tile: plan
    )

    def raising_fetch(*args, **kwargs):
        raise RuntimeError("simulated network/GDAL failure")

    monkeypatch.setattr(INSETS, "fetch_inset", raising_fetch)

    # Must swallow the failure and return None (build continues on base).
    assert ELEVATION_LEVEL.ensure_tile_overlay(_overlay_tile()) is None

    # A raised exception must NOT be recorded as a durable no-coverage
    # negative -- it may be a transient outage.
    index_path = FNAMES.tile_overlay_index(TILE_LAT, TILE_LON)
    if os.path.isfile(index_path):
        with open(index_path) as handle:
            index = json.load(handle)
        stem = os.path.splitext(os.path.basename(plan["path"]))[0]
        assert index.get(stem) != INSETS.NO_COVERAGE


def test_ensure_returns_none_when_plan_is_none(monkeypatch):
    monkeypatch.setattr(
        ELEVATION_LEVEL, "resolve_tile_overlay_plan", lambda _tile: None
    )
    assert ELEVATION_LEVEL.ensure_tile_overlay(_overlay_tile()) is None


# =====================================================================
# parse_working_grid_arc_seconds (factors 6 and 9)
# =====================================================================
@pytest.mark.parametrize(
    "value,expected",
    [
        # New finer pins.
        ("1/6", 6),
        ("6", 6),
        ("1/9", 9),
        ("9", 9),
        # Legacy values must keep their historic result.
        ("auto", "auto"),
        ("", "auto"),
        ("garbage", "auto"),
        ("1", 1),
        ("1/2", 2),
        ("0.5", 2),
        ("1/3", 3),
        ("3", 3),
    ],
)
def test_parse_working_grid_arc_seconds_extended(value, expected):
    assert INSETS.parse_working_grid_arc_seconds(value) == expected


# =====================================================================
# resolve_working_grid_factor level override
# =====================================================================
def _grid_tile(**overrides):
    tile = SimpleNamespace(
        lat=TILE_LAT,
        lon=TILE_LON,
        elevation_level="auto",
        custom_dem="",
        working_grid_arc_seconds="auto",
        airport_elevation_providers="auto",
    )
    for key, value in overrides.items():
        setattr(tile, key, value)
    return tile


def test_factor_auto_level_is_byte_inert(monkeypatch):
    # With elevation_level auto the wrapper must return exactly the historic
    # decision, whatever it is (byte-inert auto path, spec section 5).
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 2
    )
    assert INSETS.resolve_working_grid_factor(_grid_tile(), None) == 2


def test_factor_numeric_level_with_no_insets_applies_level_factor(monkeypatch):
    # No airport insets -> historic factor 1; the level override still lifts
    # the grid on the strength of the wide-area source.
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 1
    )
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    monkeypatch.setattr(
        ELEVATION_LEVEL,
        "finest_wide_area_resolution_m",
        lambda lat, lon, providers_config="auto": 1.0,
    )
    tile = _grid_tile(elevation_level="10")
    assert INSETS.resolve_working_grid_factor(tile, None) == 3


def test_factor_max_rule_when_both_apply(monkeypatch):
    # Historic already coarser/finer than the level factor -> max() wins.
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    monkeypatch.setattr(
        ELEVATION_LEVEL,
        "finest_wide_area_resolution_m",
        lambda lat, lon, providers_config="auto": 1.0,
    )
    # Historic 6, level "10" -> level factor 3 -> max(6, 3) = 6.
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 6
    )
    assert (
        INSETS.resolve_working_grid_factor(
            _grid_tile(elevation_level="10"), None
        )
        == 6
    )
    # Historic 2, level "5" -> level factor 6 -> max(2, 6) = 6.
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 2
    )
    assert (
        INSETS.resolve_working_grid_factor(
            _grid_tile(elevation_level="5"), None
        )
        == 6
    )


def test_factor_explicit_pin_wins_over_level(monkeypatch):
    # An explicit working-grid pin governs the grid outright: even a level
    # whose factor (9) exceeds the pinned factor does NOT override it.
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 2
    )
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    monkeypatch.setattr(
        ELEVATION_LEVEL,
        "finest_wide_area_resolution_m",
        lambda lat, lon, providers_config="auto": 1.0,
    )
    tile = _grid_tile(
        elevation_level="1", working_grid_arc_seconds="1/2"
    )
    assert INSETS.resolve_working_grid_factor(tile, None) == 2


def test_factor_custom_dem_uses_uncapped_level_factor(monkeypatch):
    # With custom_dem set the level factor is the full uncapped table value:
    # the user's raster is trusted to carry the detail, so a coarse
    # finest-source resolution does NOT cap the grid.
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 1
    )
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "finest_wide_area_resolution_m must not gate a custom_dem grid"
        )

    monkeypatch.setattr(
        ELEVATION_LEVEL, "finest_wide_area_resolution_m", fail_if_called
    )
    tile = _grid_tile(elevation_level="1", custom_dem="/pinned/raster.tif")
    # LEVEL_GRID_FACTORS[1] == 9, uncapped.
    assert INSETS.resolve_working_grid_factor(tile, None) == 9


def test_factor_level_none_without_gdal_returns_historic(monkeypatch):
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 2
    )
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", False)
    tile = _grid_tile(elevation_level="10")
    # GDAL missing -> the level cannot apply, historic stands.
    assert INSETS.resolve_working_grid_factor(tile, None) == 2

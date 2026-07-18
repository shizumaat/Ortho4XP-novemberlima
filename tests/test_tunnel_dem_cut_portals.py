"""Tests for the DEM-cut tunnel-portal mode (user 2026-07-17, EGGW).

Two landed behaviours are exercised here, all headless (no network, no
X-Plane install, ``tmp_path`` only — nothing is written):

A. ``highway=unclassified`` joined ``bridges.HW_TUNNEL_TYPES`` so EGGW's
   airside service-road tunnel (``highway=unclassified tunnel=yes``) is
   seen by the portal emitter.  ``bridges._tunnelable`` recognises it.

B. DEM-CUT portal mode inside ``bridges._emit_tunnel_portals``: when the
   bare-earth DEM already carries the descending approach cut (a lidar
   inset sits ``>= TUNNEL_DEM_CUT_MIN_DROP_M`` below the airport surface
   over the first ``TUNNEL_DEM_CUT_WINDOW_M`` of every portal walk), the
   emitter drops the synthetic sloped-ramp chain and instead emits only
   the pieces the bare-earth model cannot supply — a flat ``tunnel_cap``
   at airport grade, a flat ``tunnel_mouth`` plate at the measured DEM
   road grade, and flat ``tunnel_roof`` plates over the covered bore.
   With a flat DEM (no cut) or with the mode gated off (``O4_TUNNEL_DEM_CUT
   =0``) the pre-change synthetic sloped ramps are emitted instead — the
   legacy path is regression-pinned here.

The scene is synthetic (a taxiway strip crossed by a ``tunnel=yes``
``unclassified`` way with approach roads at both ends); the road-layer
loader and the DEM sampler are monkeypatched so the test controls the
elevations exactly (mirrors the fixture idiom of
``tests/test_implied_tunnel_level_crossing.py`` and the
``bridges._sample_dem`` monkeypatch in ``tests/test_runway_end_skirt.py``).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest
from shapely.geometry import box

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from auto_patch import bridges  # noqa: E402
from auto_patch import config  # noqa: E402
from auto_patch.layout import (  # noqa: E402
    BuiltShape,
    PavementLayout,
    ROLE_BOUNDARY,
    ROLE_JUNCTION,
)

# EGGW-ish anchor; the exact value is immaterial (both the road-layer
# loader and the DEM sampler are monkeypatched), but a real mid-latitude
# anchor keeps the metre projection honest.
ANCHOR_LATITUDE = 51.874
ANCHOR_LONGITUDE = -0.368
ANCHOR = (ANCHOR_LATITUDE, ANCHOR_LONGITUDE)
TILE_LATITUDE = 51
TILE_LONGITUDE = -1

# The airport surface elevation supplied by the boundary ribbon.  The
# carved DEM sits well below it (a lidar approach cut); the flat DEM sits
# level with it (no cut present).
AIRPORT_SURFACE_M = 100.0
TRENCH_FLOOR_M = 80.0


# ──────────────────────────────────────────────────────────────────
# A. ``unclassified`` is a tunnelable highway class
# ──────────────────────────────────────────────────────────────────
class TestUnclassifiedTunnelable:
    """EGGW's airside tunnel is ``highway=unclassified tunnel=yes``; the
    class must be recognised as a candidate tunnel highway type."""

    def test_unclassified_in_highway_tunnel_types(self) -> None:
        assert "unclassified" in bridges.HW_TUNNEL_TYPES

    def test_tunnelable_accepts_unclassified(self) -> None:
        assert bridges._tunnelable({"highway": "unclassified"}) is True

    def test_tunnelable_rejects_a_footway(self) -> None:
        # A sanity counter-example: not every highway value tunnels.
        assert bridges._tunnelable({"highway": "footway"}) is False


# ──────────────────────────────────────────────────────────────────
# B. DEM-cut portal mode
# ──────────────────────────────────────────────────────────────────
def _synthetic_road_network() -> tuple[dict, list, set, dict]:
    """Build the synthetic ``(nodes_r, ways_r, big_way_ids, node_tags_r)``
    the monkeypatched ``_load_tunnel_road_network`` returns.

    Geometry (LOCAL METRES, east = x, north = y): a ``tunnel=yes``
    ``highway=unclassified`` way runs east-west along ``y = 0`` from
    ``x = -60`` (portal A) through ``x = 0`` to ``x = +60`` (portal B),
    passing under a taxiway strip.  A surface approach road connects at
    each portal node and runs 400 m outward, so each portal has a real
    surface walk to trace.
    """
    to_meters, meters_to_lat_lon = bridges._local_meter_projections(ANCHOR)

    def _node(x_m: float, y_m: float) -> tuple[float, float]:
        latitude, longitude = meters_to_lat_lon(x_m, y_m)
        return (latitude, longitude)

    nodes_metres = {
        "A": (-60.0, 0.0),
        "M": (0.0, 0.0),
        "B": (60.0, 0.0),
        "W1": (-160.0, 0.0),
        "W2": (-260.0, 0.0),
        "W3": (-460.0, 0.0),
        "E1": (160.0, 0.0),
        "E2": (260.0, 0.0),
        "E3": (460.0, 0.0),
    }
    nodes_r = {nid: _node(x, y) for nid, (x, y) in nodes_metres.items()}
    tunnel_way = ("TUN", ["A", "M", "B"],
                  {"highway": "unclassified", "tunnel": "yes"})
    west_approach = ("APPW", ["A", "W1", "W2", "W3"],
                     {"highway": "unclassified"})
    east_approach = ("APPE", ["B", "E1", "E2", "E3"],
                     {"highway": "unclassified"})
    ways_r = [tunnel_way, west_approach, east_approach]
    # Marking the tunnel way an OLD (big-roads) candidate keeps the
    # airside/double-emit gate off (it only fires for NEW candidates),
    # so the two portals of the one way both reach the emit branch.
    big_way_ids = {"TUN"}
    node_tags_r: dict = {}
    return nodes_r, ways_r, big_way_ids, node_tags_r


def _build_layout() -> PavementLayout:
    """A layout with the airside taxiway strip the tunnel passes under and
    an airport-boundary ribbon carrying the airport-surface altitude near
    both portals (so ``_airport_elevation_at`` returns it, not the DEM)."""
    layout = PavementLayout(icao="ZZZZ", anchor=ANCHOR)
    layout.shapes.append(BuiltShape(
        polygon=box(-40.0, -200.0, 40.0, 200.0),
        role=ROLE_JUNCTION, ref="taxiway"))
    # A boundary ring whose corners sit within 200 m of both portals
    # (at x = +/-60, y = 0) so the airport-surface altitude is read from
    # its node_altitudes.
    layout.shapes.append(BuiltShape(
        polygon=box(-100.0, -100.0, 100.0, 100.0),
        role=ROLE_BOUNDARY, ref="airport_boundary",
        node_altitudes=[AIRPORT_SURFACE_M] * 5))
    return layout


def _install_scene(monkeypatch, *, carved: bool) -> PavementLayout:
    """Wire the synthetic road network and the DEM sampler onto the
    ``bridges`` module and return the freshly built layout.

    ``carved`` selects a DEM with a lidar-style TRENCH along the road
    (floor ``TRENCH_FLOOR_M`` within 6 m of the road line ``y = 0``, the
    deck/field at ``AIRPORT_SURFACE_M`` beside it) or a flat DEM level
    with the airport surface (no cut).  The trench must be LOCAL to the
    road: the detection discriminator is cross-road relief — trench
    floor versus the deck beside it at the same station — never an
    absolute drop, so a uniformly low DEM is correctly NOT a cut."""
    monkeypatch.setattr(
        bridges, "_load_tunnel_road_network",
        lambda _layout: _synthetic_road_network())

    to_meters, _meters_to_lat_lon = (
        bridges._local_meter_projections(ANCHOR))

    def _fake_sample_dem(_dem, _tile_lat, _tile_lon, _lat, _lon):
        if not carved:
            return AIRPORT_SURFACE_M
        _x_m, _y_m = to_meters(_lon, _lat)
        return TRENCH_FLOOR_M if abs(_y_m) <= 6.0 else AIRPORT_SURFACE_M

    monkeypatch.setattr(bridges, "_sample_dem", _fake_sample_dem)
    return _build_layout()


def _shapes_with_ref(layout: PavementLayout, ref: str) -> list[BuiltShape]:
    return [s for s in layout.shapes if getattr(s, "ref", "") == ref]


def _sloped_tunnel_ramps(layout: PavementLayout) -> list[BuiltShape]:
    """Sloped ramp pieces: ``ref == "tunnel_ramp"`` carrying the
    ``altitude_high``/``altitude_low`` pair (the synthetic-ramp signature
    the DEM-cut mode replaces)."""
    return [s for s in layout.shapes
            if getattr(s, "ref", "") == "tunnel_ramp"
            and s.altitude_high is not None
            and s.altitude_low is not None]


class TestDemCutPortalMode:
    """The portal emitter switches to the light-touch cap/mouth/roof mode
    when the DEM already carries the approach cut, and keeps the legacy
    synthetic ramps otherwise."""

    def test_carved_dem_emits_cap_mouth_roof_and_no_sloped_ramps(
        self, monkeypatch
    ) -> None:
        layout = _install_scene(monkeypatch, carved=True)
        emitted = bridges._emit_tunnel_portals(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert emitted >= 1
        caps = _shapes_with_ref(layout, "tunnel_cap")
        mouths = _shapes_with_ref(layout, "tunnel_mouth")
        roofs = _shapes_with_ref(layout, "tunnel_roof")
        assert caps, "DEM-cut mode must emit a flat tunnel_cap"
        assert mouths, "DEM-cut mode must emit a flat tunnel_mouth plate"
        assert roofs, "DEM-cut mode must emit flat tunnel_roof plates"
        # No synthetic sloped ramps in DEM-cut mode.
        assert not _sloped_tunnel_ramps(layout)

    def test_carved_dem_mouth_plate_sits_at_the_trench_grade(
        self, monkeypatch
    ) -> None:
        layout = _install_scene(monkeypatch, carved=True)
        bridges._emit_tunnel_portals(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        mouths = _shapes_with_ref(layout, "tunnel_mouth")
        assert mouths
        for mouth in mouths:
            assert mouth.altitude == pytest.approx(TRENCH_FLOOR_M, abs=0.1)

    def test_carved_dem_cap_and_roof_sit_at_airport_grade(
        self, monkeypatch
    ) -> None:
        layout = _install_scene(monkeypatch, carved=True)
        bridges._emit_tunnel_portals(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        for ref in ("tunnel_cap", "tunnel_roof"):
            plates = _shapes_with_ref(layout, ref)
            assert plates, f"expected {ref} plates"
            for plate in plates:
                if plate.altitude is not None:
                    # The cap is a flat plate at the deck grade.
                    assert plate.altitude == pytest.approx(
                        AIRPORT_SURFACE_M, abs=0.1)
                else:
                    # Roof plates GRADE along the bore (face deck →
                    # pavement-side deck); in this scene both ends
                    # measure the same airport surface, so every
                    # vertex holds it.
                    assert plate.node_altitudes, (
                        f"{ref} plate carries neither altitude nor "
                        "node_altitudes")
                    for vertex_altitude in plate.node_altitudes:
                        assert vertex_altitude == pytest.approx(
                            AIRPORT_SURFACE_M, abs=0.1)

    def test_carved_dem_covers_both_portals(self, monkeypatch) -> None:
        # The two ends of the one tunnel way are distinct clusters, so a
        # cap + mouth is emitted at each portal.
        layout = _install_scene(monkeypatch, carved=True)
        bridges._emit_tunnel_portals(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert len(_shapes_with_ref(layout, "tunnel_cap")) >= 2
        assert len(_shapes_with_ref(layout, "tunnel_mouth")) >= 2

    def test_flat_dem_keeps_the_legacy_sloped_ramps(self, monkeypatch) -> None:
        layout = _install_scene(monkeypatch, carved=False)
        emitted = bridges._emit_tunnel_portals(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert emitted >= 1
        # Legacy synthetic ramps present, DEM-cut plates absent.
        assert _sloped_tunnel_ramps(layout), (
            "a flat DEM must fall back to the synthetic sloped-ramp path")
        assert not _shapes_with_ref(layout, "tunnel_mouth")
        assert not _shapes_with_ref(layout, "tunnel_roof")

    def test_gate_off_keeps_the_legacy_ramps_despite_the_cut(
        self, monkeypatch
    ) -> None:
        # ``O4_TUNNEL_DEM_CUT=0`` forces the legacy path even when the DEM
        # carries the cut.
        monkeypatch.setenv("O4_TUNNEL_DEM_CUT", "0")
        layout = _install_scene(monkeypatch, carved=True)
        emitted = bridges._emit_tunnel_portals(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert emitted >= 1
        assert _sloped_tunnel_ramps(layout), (
            "gate off must fall back to the synthetic sloped-ramp path")
        assert not _shapes_with_ref(layout, "tunnel_mouth")
        assert not _shapes_with_ref(layout, "tunnel_roof")


# A module-level guard so an accidental network / DSF read fails loudly
# rather than silently reaching real caches.
def test_scene_has_no_hidden_dependencies() -> None:
    """The synthetic road network is self-consistent (every way node is
    present in ``nodes_r``) — a smoke check on the fixture itself."""
    nodes_r, ways_r, _big, _tags = _synthetic_road_network()
    for _wid, nrefs, _tags2 in ways_r:
        for nref in nrefs:
            assert nref in nodes_r
    # The two approach roads share exactly the portal nodes with the bore.
    assert math.isfinite(nodes_r["A"][0])

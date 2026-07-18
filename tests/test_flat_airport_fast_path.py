"""Whole-airport flat fast path — Tier 2 certificate (WP2).

Hermetic unit tests (no fixtures, no network) for
``elevation_per_surface.route_profile.flat_airport_fast_path`` per
docs/specs/flat-airport-fast-path-spec.md §3.3:

  * a synthetic flat layout CERTIFIES (returns a held
    ``FlatAirportCertificate`` whose seed carries the DEM value for soft nodes
    and the profile value for hard nodes);
  * a runway whose along-axis DEM relief exceeds the runway budget REFUSES;
  * the presence of a bridge / tunnel / crossing-terrain subsystem REFUSES;
  * a DEM sampling gap on a runway REFUSES;
  * an uncertified soft shape whose grade edge is over budget at the seed
    REFUSES;
  * a non-flat building footprint REFUSES;
  * the env gate off (``O4_FLAT_AIRPORT_FAST_PATH=0``) makes the certificate
    inert (refuses immediately, records the reason);
  * ``apply_flat_airport_fast_path`` writes the certificate seed onto ``elev``,
    pins the runway-join nodes hard, and prints ``fast-path=TAKEN``.

The DEM is sampled through ``auto_patch.elevation._sample_dem`` (stubbed) via
the layout's identity ``m_to_ll`` (so DEM samples key on the local metre
coordinates).
"""
import time

import auto_patch.config as config
import auto_patch.elevation as elevation_module
from auto_patch.elevation_per_surface import solver_primitives as SP
from auto_patch.elevation_per_surface.route_profile import (
    flat_airport_fast_path as FP)
from auto_patch.layout import (
    ROLE_BRIDGE_TRENCH, ROLE_BUILDING, ROLE_RUNWAY)

from shapely.geometry import Polygon


# ── fakes ────────────────────────────────────────────────────────────────────
class _FakeShape:
    def __init__(self, role, polygon, ref=""):
        self.role = role
        self.polygon = polygon
        self.ref = ref
        self.is_bridge = False


class _FakeGraph:
    def __init__(self, runway_anchor=None):
        self.runway_anchor = dict(runway_anchor or {})


class _FakeLayout:
    def __init__(self, shapes, icao="TEST"):
        self.shapes = shapes
        self.icao = icao

    def m_to_ll(self, x, y):
        return (x, y)


def _runway_shape():
    # 1000 m × 45 m runway rect; the farthest ring-vertex pair is a diagonal.
    return _FakeShape(ROLE_RUNWAY,
                      Polygon([(0.0, 0.0), (1000.0, 0.0),
                               (1000.0, 45.0), (0.0, 45.0)]), ref="09/27")


def _building_shape():
    return _FakeShape(ROLE_BUILDING,
                      Polygon([(100.0, 100.0), (120.0, 100.0),
                               (120.0, 120.0), (100.0, 120.0)]))


def _install_dem(monkeypatch, fn):
    """Stub ``_sample_dem(dem, tile_lat, tile_lon, lat, lon)`` with ``fn(lat,
    lon)`` (identity ``m_to_ll`` ⇒ lat = x, lon = y)."""
    monkeypatch.setattr(elevation_module, "_sample_dem",
                        lambda dem, tlat, tlon, lat, lon: fn(lat, lon))


def _flat_kwargs(runway_anchor=None):
    """Keyword artifacts for a two-node soft field with one certified and one
    satisfied uncertified constraint entry — the shape the solve would pass."""
    # nodes 0,1 soft (aprons at DEM); node 2 hard (a runway/seam seed).
    elev = [0.0, 0.0, 500.0]
    base_hard = [False, False, True]
    dem_elev = [10.0, 10.2, 10.0]
    shape_constraints = [
        {"lazy_certified": True, "edges": [(0, 1, 0.0)]},
        {"edges": [(0, 1, 5.0)]},          # satisfied: |10.0-10.2| ≤ 5.0
    ]
    return dict(
        nodes=[(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)],
        bucket_to_idx={},
        elev=elev, base_hard=base_hard, dem_elev=dem_elev,
        runway_nodes={2}, shape_constraints=shape_constraints,
        unified_graph=_FakeGraph(runway_anchor))


# ── holds ────────────────────────────────────────────────────────────────────
def test_certificate_holds_on_flat_layout(monkeypatch):
    _install_dem(monkeypatch, lambda lat, lon: 10.0)   # dead flat
    layout = _FakeLayout([_runway_shape(), _building_shape()])
    cert = FP.certify_flat_airport(layout, dem=object(), tile_lat=0,
                                   tile_lon=0, **_flat_kwargs())
    assert cert is not None
    assert cert.refusal_reason is None
    # Soft nodes take their DEM seed; the hard node keeps its profile value.
    assert cert.seed_elevation[0] == 10.0
    assert cert.seed_elevation[1] == 10.2
    assert cert.seed_elevation[2] == 500.0
    assert cert.certified_counts["runway"] == 1
    assert cert.certified_counts["building"] == 1
    assert getattr(layout, "_flat_airport_fast_path_reason", "sentinel") is None


def test_certificate_holds_pins_runway_join(monkeypatch):
    _install_dem(monkeypatch, lambda lat, lon: 10.0)
    layout = _FakeLayout([_runway_shape()])
    # A taxi-join anchor at node 0 pins it hard at the local runway elevation.
    cert = FP.certify_flat_airport(layout, dem=object(),
                                   **_flat_kwargs(runway_anchor={0: 12.5}))
    assert cert is not None
    assert cert.seed_elevation[0] == 12.5
    assert cert.join_indices == {0: 12.5}


# ── refusals ─────────────────────────────────────────────────────────────────
def test_refuses_runway_over_budget(monkeypatch):
    # 2 % along-axis relief far exceeds 0.6 · 1.5 % (mid) / 0.8 % (ends).
    _install_dem(monkeypatch, lambda lat, lon: 10.0 + 0.02 * lat)
    layout = _FakeLayout([_runway_shape()])
    cert = FP.certify_flat_airport(layout, dem=object(), **_flat_kwargs())
    assert cert is None
    assert "runway" in layout._flat_airport_fast_path_reason


def test_refuses_bridge_present(monkeypatch):
    _install_dem(monkeypatch, lambda lat, lon: 10.0)
    bridge = _FakeShape(ROLE_BRIDGE_TRENCH,
                        Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))
    layout = _FakeLayout([_runway_shape(), bridge])
    cert = FP.certify_flat_airport(layout, dem=object(), **_flat_kwargs())
    assert cert is None
    assert "bridge" in layout._flat_airport_fast_path_reason


def test_refuses_crossing_zone_present(monkeypatch):
    from auto_patch.crossing_terrain import (
        CROSSING_INFLUENCE_ZONE_UNION_ATTRIBUTE)
    _install_dem(monkeypatch, lambda lat, lon: 10.0)
    layout = _FakeLayout([_runway_shape()])
    setattr(layout, CROSSING_INFLUENCE_ZONE_UNION_ATTRIBUTE,
            Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]))
    cert = FP.certify_flat_airport(layout, dem=object(), **_flat_kwargs())
    assert cert is None
    assert "crossing" in layout._flat_airport_fast_path_reason


def test_refuses_runway_sampling_gap(monkeypatch):
    _install_dem(monkeypatch, lambda lat, lon: None)   # DEM has no data
    layout = _FakeLayout([_runway_shape()])
    cert = FP.certify_flat_airport(layout, dem=object(), **_flat_kwargs())
    assert cert is None
    assert "sampling gap" in layout._flat_airport_fast_path_reason


def test_refuses_uncertified_edge_over_budget(monkeypatch):
    _install_dem(monkeypatch, lambda lat, lon: 10.0)
    layout = _FakeLayout([_runway_shape()])
    kwargs = _flat_kwargs()
    # Soft nodes 1.0 m apart in value; an uncertified 0.05 m budget edge fails.
    kwargs["dem_elev"] = [10.0, 11.0, 10.0]
    kwargs["shape_constraints"] = [{"edges": [(0, 1, 0.05)]}]
    cert = FP.certify_flat_airport(layout, dem=object(), **kwargs)
    assert cert is None
    assert "over budget" in layout._flat_airport_fast_path_reason


def test_refuses_bumpy_building(monkeypatch):
    # Flat along the runway (lon < 100), bumpy over the building footprint.
    def _dem(lat, lon):
        if 100.0 <= lat <= 120.0 and 100.0 <= lon <= 120.0:
            return 10.0 + 0.02 * (lon - 100.0)     # 0.40 m relief > 0.30
        return 10.0
    _install_dem(monkeypatch, _dem)
    layout = _FakeLayout([_runway_shape(), _building_shape()])
    cert = FP.certify_flat_airport(layout, dem=object(), **_flat_kwargs())
    assert cert is None
    assert "seat tolerance" in layout._flat_airport_fast_path_reason


def test_gate_off_is_inert(monkeypatch):
    _install_dem(monkeypatch, lambda lat, lon: 10.0)
    monkeypatch.setattr(config, "FLAT_AIRPORT_FAST_PATH", False)
    layout = _FakeLayout([_runway_shape()])
    cert = FP.certify_flat_airport(layout, dem=object(), **_flat_kwargs())
    assert cert is None
    assert layout._flat_airport_fast_path_reason == "fast-path gate off"


def test_no_dem_refuses(monkeypatch):
    layout = _FakeLayout([_runway_shape()])
    cert = FP.certify_flat_airport(layout, dem=None, **_flat_kwargs())
    assert cert is None
    assert layout._flat_airport_fast_path_reason == "no DEM"


# ── apply ────────────────────────────────────────────────────────────────────
def test_apply_seeds_elev_and_reports(monkeypatch, capsys):
    monkeypatch.setenv("O4_SCOPED_FINAL_PROJECTION", "0")   # skip snapshot
    monkeypatch.setattr(SP, "_writeback", lambda layout, elev, b2i: (1, 2, 3))
    monkeypatch.setattr(SP, "_report",
                        lambda *args, **kwargs: None)
    layout = _FakeLayout([_runway_shape()])
    elev = [0.0, 0.0, 0.0]
    base_hard = [True, False, False]
    cert = FP.FlatAirportCertificate(
        certified_counts={}, runway_relief={},
        seed_elevation=[10.0, 20.0, 30.0], join_indices={2: 30.0})
    FP.apply_flat_airport_fast_path(
        layout, "TEST", nodes=[(0, 0), (1, 0), (2, 0)],
        bucket_to_idx={}, elev=elev, base_hard=base_hard,
        certificate=cert, t0=time.time())
    assert elev == [10.0, 20.0, 30.0]
    assert base_hard[2] is True            # join node pinned hard
    out = capsys.readouterr().out
    assert "fast-path=TAKEN" in out

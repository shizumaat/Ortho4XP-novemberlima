"""Wingtip / RESA terrain-clearance cut tests.

Validates ``auto_patch.clearance.emit_surface_clearance_cuts``:

* **Terrain envelope**: the clearance surface blends from the pavement
  edge to the natural DEM at the far edge, so no cut vertex rises above
  max(local DEM, nearby surface edge) — it never invents height above
  the terrain/pavement it sits between.
* **Presence on sloped terrain**: a plateau airport (CYXY) whose
  surrounds rise above the runway must produce clearance cuts.
* **Valid altitudes**: each cut is a node_altitudes polygon with one
  finite altitude per ring vertex.
"""
from __future__ import annotations

import math

import pytest
from shapely.geometry import Point, Polygon

from conftest import baseline_airports, xplane_available, xplane_root

pytestmark = pytest.mark.skipif(
    not xplane_available(),
    reason="X-Plane install (apt.dat/DSF/DEM) not available")


_CLEARANCE_ROLES = {"taxiway_clearance", "runway_clearance"}
# Taxiway clearance is traced from the centerline network and references
# whatever airside pavement borders it — including junctions/aprons — so
# the band invariant must consider those surfaces too.
_SURFACE_ROLES = {"runway", "runway_crossing", "primary_parallel",
                  "secondary_parallel", "stub", "cross_connector",
                  "junction", "apron"}


def _build(icao):
    # Shared session cache (conftest) — built once per airport per run.
    from conftest import cached_airport_layout
    return cached_airport_layout(icao)


def _open(poly):
    c = list(poly.exterior.coords)
    if c and c[0] == c[-1]:
        c = c[:-1]
    return c


def _surfaces(layout):
    out = []
    for s in layout.shapes:
        if s.role not in _SURFACE_ROLES:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        if (s.altitude is None
                and (s.altitude_high is None or s.altitude_low is None)
                and not s.node_altitudes):
            continue
        out.append(s)
    return out


@pytest.mark.parametrize("icao", baseline_airports())
def test_clearance_within_terrain_envelope(icao):
    """The clearance surface is a smooth BLEND from the pavement edge to
    the natural DEM at the far edge, so it must never rise above the
    terrain/pavement it sits between: every cut vertex altitude is at or
    below max(DEM-at-vertex, highest nearby surface edge) + tolerance.

    The strip has only an inner row (≈ pavement edge altitude) and an
    outer row (= DEM at that point), so:
      * inner vertices ≤ the pavement edge they tie to, and
      * outer vertices = the DEM at their own location,
    both bounded by the max above.  A vertex floating well above both
    the local terrain and the pavement would indicate a ramp/DEM bug.

    Runway-end SKIRTS are exempt: they are deliberate FILL above the
    natural terrain (the inverse-RESA down-slope law), so the cut
    envelope invariant does not apply to them.
    """
    import math as _m
    from auto_patch.config import CLEARANCE_MAX_REACH_M
    from auto_patch.elevation import _load_airport_dem, _sample_dem
    from auto_patch.layout import R_EARTH
    from auto_patch.pavement.runways import _sample_runway_segment_elev
    from shapely.ops import nearest_points

    layout = _build(icao)
    cuts = [s for s in layout.shapes if s.role in _CLEARANCE_ROLES
            and s.polygon is not None and not s.polygon.is_empty
            and s.node_altitudes and s.ref != "runway_end_skirt"]
    if not cuts:
        pytest.skip(f"{icao}: no clearance cuts emitted")
    lat0, lon0 = layout.anchor
    cos0 = _m.cos(_m.radians(lat0))
    dem = _load_airport_dem(lat0, lon0)
    if dem is None:
        pytest.skip(f"{icao}: no DEM")
    tl, tn = int(_m.floor(lat0)), int(_m.floor(lon0))

    def _dem(x, y):
        lat = lat0 + _m.degrees(y / R_EARTH)
        lon = lon0 + _m.degrees(x / (R_EARTH * cos0))
        return _sample_dem(dem, tl, tn, lat, lon)

    surfaces = _surfaces(layout)
    radius = max(CLEARANCE_MAX_REACH_M.values()) + 10.0
    TOL = 2.0  # rounding + resample interpolation + DEM smoothing
    violations, worst = 0, (0.0, None)
    for c in cuts:
        coords = _open(c.polygon)
        near = [s for s in surfaces
                if s.polygon.distance(c.polygon) <= radius]
        for (x, y), a in zip(coords, c.node_altitudes[:len(coords)]):
            pt = Point(x, y)
            ceil = _dem(x, y)
            ceil = -1e9 if ceil is None else ceil
            for s in near:
                if s.polygon.distance(pt) > radius:
                    continue
                try:
                    np_pt = nearest_points(s.polygon, pt)[0]
                    e = _sample_runway_segment_elev(s, np_pt.x, np_pt.y)
                except Exception:
                    e = None
                if e is not None and e > ceil:
                    ceil = e
            if ceil <= -1e8:
                continue
            if a - ceil > TOL:
                violations += 1
                if a - ceil > worst[0]:
                    worst = (a - ceil, (c.role, round(a, 1), round(ceil, 1)))
    assert violations == 0, (
        f"{icao}: {violations} clearance vertex(es) above the terrain/"
        f"pavement envelope; worst +{worst[0]:.2f} m {worst[1]}")


def test_clearance_present_on_plateau():
    """CYXY sits on a plateau ringed by higher ground — the runway
    graded strip must produce clearance cuts."""
    if "CYXY" not in baseline_airports():
        pytest.skip("CYXY not in baseline set")
    layout = _build("CYXY")
    cuts = [s for s in layout.shapes if s.role in _CLEARANCE_ROLES
            and s.polygon is not None and not s.polygon.is_empty]
    assert cuts, "CYXY: expected wingtip/RESA clearance cuts, got none"
    assert any(s.role == "runway_clearance" for s in cuts), (
        "CYXY: expected runway graded-strip cuts")


@pytest.mark.parametrize("icao", baseline_airports())
def test_clearance_cuts_have_valid_altitudes(icao):
    """Every clearance cut is a node_altitudes polygon with one finite
    altitude per ring vertex (incl. the closing repeat)."""
    layout = _build(icao)
    cuts = [s for s in layout.shapes if s.role in _CLEARANCE_ROLES
            and s.polygon is not None and not s.polygon.is_empty]
    if not cuts:
        pytest.skip(f"{icao}: no clearance cuts emitted")
    for c in cuts:
        assert c.node_altitudes, f"{icao}: clearance cut missing node_altitudes"
        ring = list(c.polygon.exterior.coords)
        assert len(c.node_altitudes) == len(ring), (
            f"{icao}: node_altitudes length {len(c.node_altitudes)} != "
            f"ring length {len(ring)}")
        assert all(math.isfinite(a) for a in c.node_altitudes), (
            f"{icao}: non-finite altitude in clearance cut")


# ──────────────────────────────────────────────────────────────────
# _merge_coincident_ring_vertices — pure helper (no X-Plane build)
# ──────────────────────────────────────────────────────────────────
def test_merge_coincident_ring_vertices_collapses_microcliff():
    """A 2 mm zero-length edge across an altitude step (the torn vertical
    micro-cliff _decimate preserves) collapses to one vertex at the mean
    altitude; real-length edges are untouched."""
    from auto_patch.clearance import _merge_coincident_ring_vertices
    # Square with a duplicate near-coincident vertex (index 2 ≈ index 1)
    # carrying a 1.8 m altitude step.
    coords = [(0.0, 0.0), (10.0, 0.0), (10.002, 0.0), (10.0, 10.0),
              (0.0, 10.0)]
    alts = [1122.8, 1124.6, 1122.8, 1122.8, 1122.8]
    out_xy, out_a = _merge_coincident_ring_vertices(coords, alts)
    # The coincident pair merged → one fewer vertex.
    assert len(out_xy) == 4
    assert len(out_a) == 4
    # Merged altitude is the mean of the collapsed pair (no vertical wall).
    assert 1123.7 in out_a
    # No two consecutive vertices remain within the merge tolerance.
    n = len(out_xy)
    for i in range(n):
        j = (i + 1) % n
        d = math.hypot(out_xy[i][0] - out_xy[j][0],
                       out_xy[i][1] - out_xy[j][1])
        assert d > 0.1


def test_merge_coincident_ring_vertices_noop_on_clean_ring():
    """A ring with only real-length edges is returned unchanged."""
    from auto_patch.clearance import _merge_coincident_ring_vertices
    coords = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    alts = [1120.0, 1121.0, 1122.0, 1121.0]
    out_xy, out_a = _merge_coincident_ring_vertices(coords, alts)
    assert out_xy == coords
    assert out_a == alts


# ──────────────────────────────────────────────────────────────────
# LEGACY CLEARANCE CHARTER (O4_CLEARANCE_CHARTER) — source-role scope
# ──────────────────────────────────────────────────────────────────
# Synthetic, X-Plane-independent: one square pavement shape sitting on a
# plateau that drops away outside its footprint (so the Pass-A3 ring-edge
# sweep sees terrain and cuts).  The charter gate scopes WHICH source
# roles feed that sweep: OFF = historical (apron/service included), ON =
# runway/taxiway-family only.  The B4 EXTENSION additionally drops the
# LARGE CHUNKY blob pieces (junction/RESA terminal sweeps, aspect < 2 and
# area >= _CHARTER_BLOB_MIN_AREA_M2) — "never large-area pieces" — while
# keeping the ELONGATED wingtip strips.
class _ChartHarness:
    _ALT = 700.0
    _L = 200.0          # square side, metres
    _RAISE = 15.0       # terrain lift OUTSIDE the footprint (obstruction)

    def _layout(self, role, poly=None):
        from auto_patch.layout import BuiltShape, PavementLayout
        L = self._L
        sq = poly if poly is not None else Polygon(
            [(0.0, 0.0), (L, 0.0), (L, L), (0.0, L)])
        layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
        layout.shapes.append(BuiltShape(
            polygon=sq, role=role, ref="X",
            node_altitudes=[self._ALT] * len(sq.exterior.coords)))
        return layout

    def _fake_dem(self, footprint=None):
        from auto_patch.layout import R_EARTH
        if footprint is None:
            minx = miny = 0.0
            maxx = maxy = self._L
        else:
            minx, miny, maxx, maxy = footprint.bounds

        def _s(dem, tile_lat, tile_lon, lat, lon):
            x = math.radians(lon) * R_EARTH
            y = math.radians(lat) * R_EARTH
            inside = (minx - 0.01 <= x <= maxx + 0.01
                      and miny - 0.01 <= y <= maxy + 0.01)
            return self._ALT if inside else self._ALT + self._RAISE
        return _s

    def _emit(self, monkeypatch, role, charter, poly=None):
        from auto_patch import clearance
        layout = self._layout(role, poly=poly)
        footprint = layout.shapes[0].polygon
        monkeypatch.setattr(clearance, "_sample_dem",
                            self._fake_dem(footprint))
        monkeypatch.setattr(clearance, "_CLEARANCE_CHARTER", charter)
        clearance.emit_surface_clearance_cuts(
            layout, dem=object(), tile_lat=0, tile_lon=0)
        return [s for s in layout.shapes
                if s.role in _CLEARANCE_ROLES
                and s.polygon is not None and not s.polygon.is_empty]


class TestClearanceCharter(_ChartHarness):
    def test_apron_only_emits_zero_under_charter(self, monkeypatch):
        """CHARTER ON: an apron sources NO clearance (apron is not a
        taxiway/runway)."""
        cuts = self._emit(monkeypatch, "apron", charter=True)
        assert cuts == [], (
            f"charter ON: apron sourced {len(cuts)} clearance shape(s)")

    def test_apron_emits_without_charter(self, monkeypatch):
        """Gate OFF restores the historical apron ring sweep — the same
        apron/DEM DOES cut (proves the ON result is the gate, not the
        geometry failing to trigger)."""
        cuts = self._emit(monkeypatch, "apron", charter=False)
        assert cuts, "charter OFF: apron should source the historical sweep"

    def test_service_road_emits_zero_under_charter(self, monkeypatch):
        """CHARTER ON: a service road sources NO clearance."""
        assert self._emit(monkeypatch, "service_road", charter=True) == []
        # gate off → historical sweep present
        assert self._emit(monkeypatch, "service_road", charter=False)

    def test_taxiway_family_keeps_wingtip_under_charter(self, monkeypatch):
        """CHARTER ON: an ELONGATED junction (taxiway-family) KEEPS its
        wingtip clearance — the charter scopes out aprons/service and the
        chunky terminal blobs, not the genuine wingtip strips."""
        from shapely.geometry import Polygon as _P
        # A long, thin junction (aspect ~13) — a real taxiway-family strip.
        strip = _P([(0.0, 0.0), (400.0, 0.0), (400.0, 30.0), (0.0, 30.0)])
        cuts = self._emit(monkeypatch, "junction", charter=True, poly=strip)
        assert cuts, "charter ON: elongated junction lost its wingtip clearance"
        assert all(s.role == "taxiway_clearance" for s in cuts)

    def test_junction_blob_dropped_under_charter(self, monkeypatch):
        """CHARTER EXTENSION (Slice B B4): a LARGE CHUNKY junction (a
        terminal/parking blob, aspect ~1) sources NO clearance under the
        charter — Noah's ruling "never large-area pieces".  Without the
        charter the historical sweep still cuts (proves the ON result is the
        gate, not the geometry failing to trigger)."""
        # The default harness square is 200x200 m (aspect 1.0, 40,000 m2) —
        # exactly a large-area chunky blob.
        assert self._emit(monkeypatch, "junction", charter=True) == [], (
            "charter ON: large chunky junction blob should source no clearance")
        assert self._emit(monkeypatch, "junction", charter=False), (
            "charter OFF: the same junction should source the historical sweep")

    def test_gate_default_follows_b4_flip(self):
        """The charter default follows the ONE B4 review switch
        (``config.B4_FLIP_DEFAULTS``, flipped ON 2026-07-15 with the KBNA
        performance round); an explicit O4_CLEARANCE_CHARTER env var always
        wins over the switch."""
        import os
        from auto_patch import clearance
        from auto_patch.config import B4_FLIP_DEFAULTS
        if "O4_CLEARANCE_CHARTER" in os.environ:
            expected = os.environ["O4_CLEARANCE_CHARTER"] == "1"
        else:
            expected = B4_FLIP_DEFAULTS
        assert clearance._CLEARANCE_CHARTER is expected

"""Adjacent-ground LATERAL grade-law VALIDATOR (slice 4).

``verification.check_adjacent_ground`` is the DEM-based reader in lockstep
with the ``adjacent_ground`` emitter (both consume the ONE law function
``grade_law.adjacent_ground_envelope``).  These synthetic-DEM cases pin
the corridor semantics the reader must honour:

  (a) terrain inside the corridor (within tolerance) → no findings;
  (b) a fill band OWED but not emitted (DEM below the finite zone-1/2
      floor, uncovered) → flagged ``should_fill``;
  (c) a zone-3 cliff beyond the graded band (floor = None) → NOT flagged
      (the boundary-bridge killer);
  (d) a column CLAMPED by the emitter's clip (abutting/covering shape) →
      NOT flagged (the validator never demands what the emitter cannot
      emit);
  (e) the gate-off contract: ``dem=None`` → empty, and ``verify_and_log``
      grows no ``adjacent_ground`` counter when the gate is off.

The harness mirrors the runway-end-skirt validator harness: a flat
code-3 runway rect at 100 m, a monkeypatched ``elevation._sample_dem``
keyed on the lateral distance from the runway centreline edge.
"""
import math

import pytest
from shapely.geometry import Polygon

from auto_patch import elevation, verification
from auto_patch.apt_dat_reader import Runway
from auto_patch.layout import BuiltShape, PavementLayout, R_EARTH

_RUNWAY_LEN = 1500.0          # ICAO code 3 → graded half-width 75 m
_RUNWAY_ALT = 100.0
_HALF_WIDTH = 22.5            # rect half-width (short edge)


def _make_layout():
    rect = Polygon([
        (0.0, -_HALF_WIDTH), (_RUNWAY_LEN, -_HALF_WIDTH),
        (_RUNWAY_LEN, _HALF_WIDTH), (0.0, _HALF_WIDTH)])
    layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
    layout.shapes.append(BuiltShape(
        polygon=rect, role="runway", ref="09-27", altitude=_RUNWAY_ALT))
    return layout


def _make_runway():
    lon_b = math.degrees(_RUNWAY_LEN / R_EARTH)
    return Runway(
        desig_a="09", desig_b="27",
        lat_a=0.0, lon_a=0.0, lat_b=0.0, lon_b=lon_b,
        width_m=45.0, surface_code=1,
        displaced_a_m=0.0, displaced_b_m=0.0,
        markings_a=0, approach_lights_a=0,
        markings_b=0, approach_lights_b=0)


def _lateral_distance_m(lat):
    """Metres from the runway SIDE edge for a fake-DEM query latitude."""
    y = math.radians(lat) * R_EARTH
    return abs(y) - _HALF_WIDTH


def _patch_dem(monkeypatch, scenario):
    """Install a fake ``_sample_dem`` returning ``scenario(d)`` where ``d``
    is the lateral distance (m) from the runway edge (<0 = on/under the
    pavement, never sampled by the outward march)."""
    def _fake(dem, tile_lat, tile_lon, lat, lon):
        return scenario(_lateral_distance_m(lat))
    monkeypatch.setattr(elevation, "_sample_dem", _fake)


def _validate(layout):
    return verification.check_adjacent_ground(
        layout, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=[_make_runway()])


# ── (a) compliant terrain → no findings ─────────────────────────────────
def test_terrain_within_corridor_passes(monkeypatch):
    """Terrain at the pavement-edge level sits inside the corridor to
    within the 1.5 m tolerance at every distance (the near-field corridor
    is shallower than the tolerance, and the zone-3 ceiling rises) — the
    validator must report nothing."""
    layout = _make_layout()
    _patch_dem(monkeypatch, lambda d: _RUNWAY_ALT)
    assert _validate(layout) == []


# ── (b) fill owed but not emitted → should_fill ─────────────────────────
def test_unfilled_drop_in_graded_zone_is_flagged(monkeypatch):
    """The DEM drops 10 m below the edge across the whole graded band and
    NO graded_strip covers it (gate-off / un-emitted) — a fill was owed,
    so the reader flags ``should_fill`` well beyond the floor."""
    layout = _make_layout()
    # Inside the graded band (d < 75 m): 10 m below the edge; beyond it,
    # flat (so only the graded-zone drop is on trial).
    _patch_dem(monkeypatch,
               lambda d: _RUNWAY_ALT - 10.0 if d < 60.0 else _RUNWAY_ALT)
    findings = _validate(layout)
    assert findings, "an un-filled below-floor drop must be reported"
    kinds = {f[0] for f in findings}
    assert kinds == {"should_fill"}
    assert all(f[2] > 1.5 for f in findings)      # magnitude > tolerance


# ── (c) zone-3 cliff → NOT flagged (boundary-bridge killer) ─────────────
def test_zone3_cliff_beyond_graded_band_is_lawful(monkeypatch):
    """A sheer 50 m drop starting BEYOND the graded half-width (75 m) is
    zone 3, whose floor is None — a cliff there is LAWFUL and must never
    be flagged (the boundary-bridge killer).  Inside the band the terrain
    is at edge level (compliant)."""
    layout = _make_layout()
    _patch_dem(monkeypatch,
               lambda d: _RUNWAY_ALT if d <= 76.0 else _RUNWAY_ALT - 50.0)
    assert _validate(layout) == []


# ── (d) clamped column → NOT flagged ────────────────────────────────────
def test_column_clamped_by_covering_shape_is_exempt(monkeypatch):
    """The SAME 10 m graded-band drop as (b), but a covering shape
    (groundside pavement) abuts the runway over it: the emitter could not
    grade the column (its static clip removes it), so the reader — sharing
    that clip — must not flag it."""
    layout = _make_layout()
    _patch_dem(monkeypatch,
               lambda d: _RUNWAY_ALT - 10.0 if d < 60.0 else _RUNWAY_ALT)
    # Cover BOTH sides from just outside the runway edge outward.
    for sign in (1.0, -1.0):
        y0 = sign * _HALF_WIDTH
        y1 = sign * 200.0
        cover = Polygon([
            (-50.0, min(y0, y1)), (_RUNWAY_LEN + 50.0, min(y0, y1)),
            (_RUNWAY_LEN + 50.0, max(y0, y1)), (-50.0, max(y0, y1))])
        layout.shapes.append(BuiltShape(
            polygon=cover, role="groundside_pavement", ref="lot",
            altitude=_RUNWAY_ALT - 10.0))
    assert _validate(layout) == []


# ── (e) gate-off contract ───────────────────────────────────────────────
def test_none_dem_returns_empty():
    layout = _make_layout()
    assert verification.check_adjacent_ground(
        layout, dem=None, tile_lat=0, tile_lon=0) == []


def test_verify_and_log_has_no_adjacent_counter_when_gate_off(monkeypatch):
    """Gate off (the default): ``verify_and_log`` must neither call the
    DEM reader nor add an ``adjacent_ground`` key — byte-identical counts
    to a pre-law build."""
    monkeypatch.setattr(
        verification, "check_adjacent_ground",
        lambda *a, **k: pytest.fail("reader must not run with gate off"))
    import auto_patch.config as cfg
    monkeypatch.setattr(cfg, "ADJACENT_GROUND_LAW_ENABLED", False)
    counts = verification.verify_and_log(_make_layout(), "ZZZZ")
    assert "adjacent_ground" not in counts


def test_verify_and_log_counts_adjacent_when_gate_on(monkeypatch):
    """Gate on: the counter appears and reflects the reader's findings
    (here 0 for flat terrain, but the KEY is present)."""
    import auto_patch.config as cfg
    monkeypatch.setattr(cfg, "ADJACENT_GROUND_LAW_ENABLED", True)
    _patch_dem(monkeypatch, lambda d: _RUNWAY_ALT)
    counts = verification.verify_and_log(
        _make_layout(), "ZZZZ", dem=object(), tile_lat=0, tile_lon=0,
        source_runways=[_make_runway()])
    assert "adjacent_ground" in counts
    assert counts["adjacent_ground"] == 0


# ── source_runways=None fallback (the production verify path) ──────────
def test_none_runways_fallback_reads_runways_without_crashing(monkeypatch):
    """The production ``verify_and_log`` path has no apt.dat runway rows
    (the driver threads only the DEM), so the runway family must key its
    code number from the runway SHAPE's own geometry (minimum-rotated-
    rectangle long side) — the round-1 fallback fed the long-edges LIST
    from ``_rect_long_short_edges`` to ``runway_code_number`` and crashed
    with TypeError on every 4-corner runway ring (swallowed to a false 0
    by the old broad except).  Same below-floor drop as the flagged case,
    no runways: must still flag, never crash."""
    layout = _make_layout()
    _patch_dem(monkeypatch,
               lambda d: _RUNWAY_ALT - 10.0 if d < 60.0 else _RUNWAY_ALT)
    findings = verification.check_adjacent_ground(
        layout, dem=object(), tile_lat=0, tile_lon=0, source_runways=None)
    assert findings, "the None-runways fallback must still read runways"
    assert {f[0] for f in findings} == {"should_fill"}


def test_verify_and_log_counter_is_live_without_runways(monkeypatch):
    """Gate on, NO explicit runways (the driver's production call shape):
    a synthetic below-floor drop must show up in the counter — a live
    count, not the swallowed 0 the old broad except produced."""
    import auto_patch.config as cfg
    monkeypatch.setattr(cfg, "ADJACENT_GROUND_LAW_ENABLED", True)
    _patch_dem(monkeypatch,
               lambda d: _RUNWAY_ALT - 10.0 if d < 60.0 else _RUNWAY_ALT)
    counts = verification.verify_and_log(
        _make_layout(), "ZZZZ", dem=object(), tile_lat=0, tile_lon=0)
    assert counts.get("adjacent_ground", 0) > 0


def test_verify_and_log_surfaces_programming_errors(monkeypatch):
    """A TypeError inside the check must PROPAGATE out of verify_and_log
    (the _GEOM_EXC rule: shapely-domain failures may be contained,
    built-in errors never — the old broad except read every crash as a
    false 0 count)."""
    import auto_patch.config as cfg
    monkeypatch.setattr(cfg, "ADJACENT_GROUND_LAW_ENABLED", True)

    def _boom(*args, **kwargs):
        raise TypeError("programming error must surface")
    monkeypatch.setattr(verification, "check_adjacent_ground", _boom)
    with pytest.raises(TypeError):
        verification.verify_and_log(
            _make_layout(), "ZZZZ", dem=object(), tile_lat=0, tile_lon=0)


# ── OSM-side tear sentinel (tools/check_grade) ──────────────────────────
def _make_graded_way(elevs):
    """A synthetic closed ``adjacent_ground`` way whose ring runs along the
    x axis at 5 m spacing (metres carried as lon), ``elevs`` per vertex."""
    import sys
    sys.path.insert(0, "tools")
    from check_grade import Way
    n = len(elevs)
    nids = [f"n{i}" for i in range(n)]
    nodes = {f"n{i}": (0.0, float(i) * 5.0) for i in range(n)}  # (lat, lon=x)
    return Way(wid="w", role="graded_strip", ref="adjacent_ground",
               aeroway="aerodrome", nids=nids, elevs=elevs, tags={}), nodes


def _ll_xy(lat, lon):
    return (lon, lat)   # lon carries x-metres, lat carries y-metres


def test_osm_tear_reader_flags_submetre_vertical_edge():
    """A sub-metre edge carrying a multi-metre jump (a clip/weld tear) is
    flagged; a gently graded band at 5 m spacing is not."""
    import sys
    sys.path.insert(0, "tools")
    import check_grade as CG
    # Clean band: 3 % over each 5 m step — no tear.
    clean, cnodes = _make_graded_way([100.0, 99.85, 99.70, 99.55, 100.0])
    assert CG._check_adjacent_ground_edges([clean], cnodes, _ll_xy) == []
    # Insert a tear: a vertex 0.5 m from its neighbour, 20 m lower.
    torn, tnodes = _make_graded_way([100.0, 99.85, 80.0, 99.55, 100.0])
    tnodes["n2"] = (0.0, 5.5)         # 0.5 m past n1 → sub-metre edge
    findings = CG._check_adjacent_ground_edges([torn], tnodes, _ll_xy)
    assert findings, "a sub-metre near-vertical edge must be flagged"
    assert findings[0].de_m > 1.0

"""Unit tests for the WIDE parallel-road station merge (part 30m OPEN item (a))
— the tangent-guarded seed coupling for two non-touching but near-parallel
service ways a few metres apart.

Shipped DEFAULT OFF (it over-couples genuine terrain-height-difference parallel
roads and regressed CYXY — see the module comment in ``anchors``); these tests
pin the machinery's contract so a future revisit can rely on it: parallel pairs
within the gap couple, crossing roads never do, the tangent guard holds, and the
env gate flips the behaviour (default off ⇒ byte-identical to the 2 m window).
"""
import importlib
import math

from auto_patch.elevation_per_surface.route_profile import anchors


# Two stations per line at s≈0 and s≈10; lines A (y=0) and B (y=3) run parallel
# 3 m apart — the synthetic analogue of two SEPARATE parallel service roads
# (the documented #576↔#584 shape; on live fixtures this fires at CYXY
# -10045↔-10195).
_PARALLEL_XY = {0: (0.0, 0.0), 1: (0.0, 3.0),
                2: (10.0, 0.0), 3: (10.0, 3.0)}
_LINE = {0: 0, 1: 1, 2: 0, 3: 1}
_GAP = anchors.PARALLEL_SERVICE_STATION_MERGE_MAX_GAP_M
_MINCOS = anchors.PARALLEL_SERVICE_STATION_MERGE_MIN_ABS_COS


def _pairs(tangents, gap=_GAP, mincos=_MINCOS):
    return {tuple(sorted(p)) for p in anchors._parallel_station_merge_pairs(
        _PARALLEL_XY, _LINE, tangents, gap, mincos)}


def test_parallel_lines_3m_apart_couple():
    """Two parallel service lines 3 m apart couple their facing stations."""
    east = {sid: (1.0, 0.0) for sid in _PARALLEL_XY}
    assert _pairs(east) == {(0, 1), (2, 3)}


def test_antiparallel_loop_return_couples():
    """A loop road's return leg runs antiparallel (|cos|≈1) — still coupled."""
    tangents = {0: (1.0, 0.0), 1: (-1.0, 0.0),
                2: (1.0, 0.0), 3: (-1.0, 0.0)}
    assert _pairs(tangents) == {(0, 1), (2, 3)}


def test_crossing_lines_do_not_couple():
    """Perpendicular (crossing) roads never couple, even within the gap."""
    crossing = {0: (1.0, 0.0), 1: (0.0, 1.0),
                2: (1.0, 0.0), 3: (0.0, 1.0)}
    assert _pairs(crossing) == set()


def test_oblique_lines_below_threshold_do_not_couple():
    """A ~45° divergence is below the near-parallel threshold → no couple."""
    diag = {0: (1.0, 0.0), 1: (math.cos(math.radians(45)),
                               math.sin(math.radians(45))),
            2: (1.0, 0.0), 3: (math.cos(math.radians(45)),
                               math.sin(math.radians(45)))}
    assert _pairs(diag) == set()


def test_gap_beyond_window_does_not_couple():
    """Stations farther apart than the window are not coupled."""
    far_xy = {0: (0.0, 0.0), 1: (0.0, _GAP + 2.0)}
    far_line = {0: 0, 1: 1}
    east = {0: (1.0, 0.0), 1: (1.0, 0.0)}
    assert anchors._parallel_station_merge_pairs(
        far_xy, far_line, east, _GAP, _MINCOS) == []


def test_same_line_stations_never_couple():
    """Two stations on the SAME line are longitudinal neighbours, not a
    cross-section pair — never coupled by this pass."""
    same = {0: (0.0, 0.0), 1: (3.0, 0.0)}
    same_line = {0: 0, 1: 0}
    east = {0: (1.0, 0.0), 1: (1.0, 0.0)}
    assert anchors._parallel_station_merge_pairs(
        same, same_line, east, _GAP, _MINCOS) == []


def test_line_unit_tangent_direction():
    """The tangent helper returns a unit vector along the line direction."""
    from shapely.geometry import LineString
    ln = LineString([(0.0, 0.0), (10.0, 0.0)])
    tx, ty = anchors._line_unit_tangent(ln, 5.0)
    assert abs(tx - 1.0) < 1e-9 and abs(ty) < 1e-9


def test_gate_defaults_off():
    """The widen-merge ships OFF (measured 2026-07-08: it over-couples genuine
    terrain-height-difference parallel roads and regressed CYXY).  Default build
    is byte-identical to the 2 m window."""
    assert anchors.PARALLEL_SERVICE_STATION_MERGE is False


def test_gate_env_enables_merge(monkeypatch):
    """O4_SVC_PARALLEL_STATION_MERGE=1 turns the experiment on."""
    monkeypatch.setenv("O4_SVC_PARALLEL_STATION_MERGE", "1")
    reloaded = importlib.reload(anchors)
    try:
        assert reloaded.PARALLEL_SERVICE_STATION_MERGE is True
    finally:
        monkeypatch.delenv("O4_SVC_PARALLEL_STATION_MERGE", raising=False)
        importlib.reload(anchors)


# ── end-to-end through _svc_spine_station_seeds (gate ON) ───────────────────
# The documented defect (#576↔#584) is two SEPARATE service ways — hence two
# separate service centerlines — a few metres apart.  These synthetic tests
# exercise the seed path on that shape with the experiment ON: two parallel
# centerlines 3 m apart, anchor-unreachable (seed = DEM), a DIFFERENT DEM per
# line.  Merge ON ⇒ ONE cross-section value (wall unseedable); crossing lines ⇒
# each keeps its own value; gate default (OFF) ⇒ split.
import types  # noqa: E402


def _ls(coords):
    from shapely.geometry import LineString
    return LineString(coords)


def _seed_targets_with_gate(gate_on, line_coords, node_pos, dem,
                            monkeypatch):
    if gate_on:
        monkeypatch.setenv("O4_SVC_PARALLEL_STATION_MERGE", "1")
    else:
        monkeypatch.setenv("O4_SVC_PARALLEL_STATION_MERGE", "0")
    reloaded = importlib.reload(anchors)
    try:
        layout = types.SimpleNamespace(apt_taxi_centerlines=[
            types.SimpleNamespace(is_service=True, line=_ls(c))
            for c in line_coords])
        empty = {}
        return reloaded._svc_spine_station_seeds(
            layout, set(node_pos), node_pos, {}, dem, 0.04,
            empty, empty, empty, empty, prox_pairs=())
    finally:
        monkeypatch.delenv("O4_SVC_PARALLEL_STATION_MERGE", raising=False)
        importlib.reload(anchors)


def test_seed_parallel_separate_lines_single_valued(monkeypatch):
    """Gate ON: two parallel centerlines 3 m apart, facing nodes at x=10 with
    different DEM — the widen-merge shares one DEM seed so both take the SAME
    target (wall unseedable), instead of 100 vs 106."""
    lines = [[(0.0, 0.0), (20.0, 0.0)], [(0.0, 3.0), (20.0, 3.0)]]
    node_pos = {0: (10.0, 0.0), 1: (10.0, 3.0)}
    tgt, _ = _seed_targets_with_gate(True, lines, node_pos,
                                     [100.0, 106.0], monkeypatch)
    assert 0 in tgt and 1 in tgt
    assert abs(tgt[0] - tgt[1]) < 1e-6          # coupled → single-valued
    assert abs(tgt[0] - 103.0) < 1e-6           # shared mean DEM


def test_seed_crossing_lines_not_coupled(monkeypatch):
    """Gate ON but crossing (perpendicular) road — NOT coupled; each node keeps
    its own DEM seed (the tangent guard admits genuine cross-road difference)."""
    lines = [[(0.0, 0.0), (20.0, 0.0)], [(10.0, -10.0), (10.0, 10.0)]]
    node_pos = {0: (10.0, 0.0), 1: (10.0, 3.0)}
    tgt, _ = _seed_targets_with_gate(True, lines, node_pos,
                                     [100.0, 106.0], monkeypatch)
    assert abs(tgt[0] - 100.0) < 1e-6
    assert abs(tgt[1] - 106.0) < 1e-6           # uncoupled → distinct


def test_seed_gate_off_leaves_separate_lines_split(monkeypatch):
    """Gate OFF (the shipped default): the two parallel separate lines are NOT
    widen-merged (the 2 m window can't reach a 3 m gap), so their seeds stay
    split — proving the gate is the sole cause of the coupling."""
    lines = [[(0.0, 0.0), (20.0, 0.0)], [(0.0, 3.0), (20.0, 3.0)]]
    node_pos = {0: (10.0, 0.0), 1: (10.0, 3.0)}
    tgt, _ = _seed_targets_with_gate(False, lines, node_pos,
                                     [100.0, 106.0], monkeypatch)
    assert abs(tgt[0] - 100.0) < 1e-6
    assert abs(tgt[1] - 106.0) < 1e-6

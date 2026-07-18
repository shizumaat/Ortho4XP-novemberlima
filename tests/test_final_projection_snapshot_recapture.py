"""Scoped-projection snapshot RECAPTURE at the end of
``final_grade_projection`` (2026-07-18).

The solve captures the scoped snapshot once at its writeback, so the LATE
pipeline-end projection run used to compare against SOLVE-time values, see
every mid-projected value as touched, and defer almost nothing.  The fix
recaptures the snapshot at the end of every successful projection run, so
the next run scopes against the PREVIOUS run's output.

Hermetic unit tests on a tiny hand-built layout (the ``_FakeShape`` /
``_FakeLayout`` pattern of ``test_one_solve_skirt_pins.py`` — no fixtures,
no DEM, no network).  Coordinates are non-integral so the identity
``m_to_ll`` never triggers the tile-seam terrain pins.
"""
import auto_patch.config  # noqa: F401  (config import side effects)
from auto_patch.canonical_points import CanonicalPointRegistry
from auto_patch.elevation_per_surface import solver_primitives as SP
from auto_patch.elevation_per_surface.route_profile import solve as RP
from auto_patch.layout import ROLE_APRON

from shapely.geometry import Polygon


class _FakeShape:
    def __init__(self, role, polygon, *, ref=None, altitude=None,
                 altitude_high=None, altitude_low=None, node_altitudes=None):
        self.role = role
        self.polygon = polygon
        self.ref = ref
        self.altitude = altitude
        self.altitude_high = altitude_high
        self.altitude_low = altitude_low
        self.node_altitudes = node_altitudes
        self.is_bridge = False


class _FakeLayout:
    def __init__(self, shapes, icao="TEST"):
        self.shapes = shapes
        self.icao = icao
        self.canonical_points = CanonicalPointRegistry()

    def m_to_ll(self, x, y):
        return (x, y)


def _flat_apron_shape():
    """A flat 10 m apron square at 100 m, away from integral coordinates
    (identity ``m_to_ll`` would tile-seam-pin integral lat/lon)."""
    polygon = Polygon([(0.3, 0.3), (10.3, 0.3), (10.3, 10.3), (0.3, 10.3)])
    return _FakeShape(ROLE_APRON, polygon, altitude=100.0)


def _apron_layout():
    return _FakeLayout([_flat_apron_shape()])


def _read_back_values(layout):
    """Per-canonical-key elevations exactly as the next projection run will
    re-read them (the snapshot's own readback path)."""
    nodes, bucket_to_idx = SP._build_node_list(layout)
    elev, _is_hard, _have = SP._seed_elevations(layout, nodes, bucket_to_idx)
    return {key: elev[index] for key, index in bucket_to_idx.items()}


def test_projection_recaptures_snapshot_with_post_projection_values(
        monkeypatch):
    monkeypatch.setenv("O4_SCOPED_FINAL_PROJECTION", "1")   # opt-in since the 2026-07-18 default flip
    monkeypatch.delenv("O4_FINAL_GRADE_PROJECTION", raising=False)
    layout = _apron_layout()
    assert getattr(layout, "_final_projection_snapshot", None) is None

    RP.final_grade_projection(layout, icao="TEST")

    snapshot = getattr(layout, "_final_projection_snapshot", None)
    assert snapshot is not None, "projection must recapture the snapshot"
    # The snapshot's values are the POST-projection writeback state, read
    # through the same path the next run seeds from — bitwise identical.
    assert snapshot["values"] == _read_back_values(layout)
    # The apron ring is snapshotted under its role.
    assert any(role == ROLE_APRON for (role, _ring) in snapshot["rings"])
    assert isinstance(snapshot["fairing_moved"], set)
    assert isinstance(snapshot["broken"], set)


def test_second_projection_run_defers_unchanged_shape(monkeypatch, capsys):
    monkeypatch.setenv("O4_SCOPED_FINAL_PROJECTION", "1")   # opt-in since the 2026-07-18 default flip
    monkeypatch.delenv("O4_FINAL_GRADE_PROJECTION", raising=False)
    layout = _apron_layout()
    apron = layout.shapes[0]

    RP.final_grade_projection(layout, icao="TEST")
    snapshot = layout._final_projection_snapshot

    # Zero value-contamination against the recaptured snapshot: the
    # untouched apron is provably deferrable on the next run.
    nodes, bucket_to_idx = SP._build_node_list(layout)
    elev, _is_hard, _have = SP._seed_elevations(layout, nodes, bucket_to_idx)
    defer_ids, pre_broken = RP._scoped_projection_defer_ids(
        layout, nodes, bucket_to_idx, elev, snapshot)
    assert id(apron) in defer_ids
    assert pre_broken == set()

    # And the second full projection run actually defers it (the scoped
    # debug line names the kept shape) — then re-recaptures an equally
    # clean snapshot for a hypothetical third run.
    monkeypatch.setenv("O4_STEP_DEBUG", "1")
    RP.final_grade_projection(layout, icao="TEST")
    output = capsys.readouterr().out
    assert "[scoped] deferred apron" in output
    assert layout._final_projection_snapshot["values"] == \
        _read_back_values(layout)


def test_value_churn_after_projection_blocks_deferral(monkeypatch):
    monkeypatch.setenv("O4_SCOPED_FINAL_PROJECTION", "1")   # opt-in since the 2026-07-18 default flip
    monkeypatch.delenv("O4_FINAL_GRADE_PROJECTION", raising=False)
    layout = _apron_layout()
    apron = layout.shapes[0]

    RP.final_grade_projection(layout, icao="TEST")
    snapshot = layout._final_projection_snapshot

    # Simulate post-projection churn (a weld adoption): one apron vertex
    # value moves.  The recaptured snapshot must catch it — the shape may
    # NOT defer.
    ring_length = len(apron.polygon.exterior.coords)
    apron.node_altitudes = [100.0] * ring_length
    apron.node_altitudes[1] = 101.0
    apron.altitude = None

    nodes, bucket_to_idx = SP._build_node_list(layout)
    elev, _is_hard, _have = SP._seed_elevations(layout, nodes, bucket_to_idx)
    defer_ids, _pre_broken = RP._scoped_projection_defer_ids(
        layout, nodes, bucket_to_idx, elev, snapshot)
    assert id(apron) not in defer_ids


def test_scoped_gate_off_skips_recapture(monkeypatch):
    monkeypatch.setenv("O4_SCOPED_FINAL_PROJECTION", "0")
    monkeypatch.delenv("O4_FINAL_GRADE_PROJECTION", raising=False)
    layout = _apron_layout()

    RP.final_grade_projection(layout, icao="TEST")

    assert getattr(layout, "_final_projection_snapshot", None) is None

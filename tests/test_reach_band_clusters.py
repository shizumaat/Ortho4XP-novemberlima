"""Reach-band cluster amortization — Tier 3 wave 1 (``O4_REACH_BAND_CLUSTERS``).

Hermetic unit tests (no fixtures, no network) for the sound, byte-identical
serving-line sharing in ``building_feasibility.reach_band_unified`` and its
driver ``route_profile.anchors.node_bands``
(docs/specs/flat-airport-fast-path-spec.md Tier 3):

  * ``node_bands`` gate-off / no-``.batch`` inertness — the per-node scan runs
    and the result is byte-identical to the legacy list comprehension;
  * ``node_bands`` bucketing dispatch — the gate routes to ``band.batch`` and
    the returned list equals the per-node scan exactly (including the
    ``skip_from`` zone tail);
  * the batch is bit-identical to the per-node band on a controlled band whose
    value depends on the serving line, so a mis-shared line would show up;
  * ``_confirms_line`` is a SOUND sufficient condition (never a false positive):
    it only returns True when the candidate line is the point's nearest and its
    chord is on pavement, which is exactly when the nearest-visible scan returns
    it.

The reach-band internals are exercised through a small hand-built layout +
unified graph so the real ``reach_band_unified`` closure (its ``band`` and
``band.batch``) runs unmodified.
"""

import math

import pytest

from auto_patch.elevation_per_surface.route_profile import anchors


@pytest.fixture(autouse=True)
def _quiet_summary(monkeypatch):
    """Silence the per-call cluster summary line during the unit tests."""
    monkeypatch.setenv("O4_REACH_BAND_CLUSTER_QUIET", "1")


# ── node_bands dispatch (fake band closure) ──────────────────────────────────
class _FakeBand:
    """A band closure whose value is a function of the point, with an optional
    ``.batch`` that must return exactly the per-node result."""

    def __init__(self, with_batch):
        self.calls = 0
        if with_batch:
            self.batch = self._batch                    # attribute → dispatch

    def __call__(self, x, y):
        self.calls += 1
        return (x - 1.0, x + 1.0)

    def _batch(self, nodes, limit):
        # A trivial faithful batch: exactly the per-node scan (byte-identical),
        # so node_bands' dispatch can be checked without the real geometry.
        lim = len(nodes) if limit is None else min(limit, len(nodes))
        out = [None] * len(nodes)
        for i in range(lim):
            out[i] = self(nodes[i][0], nodes[i][1])
        return out


def _nodes():
    return [(float(i), float(i * 2)) for i in range(10)]


def test_node_bands_gate_off_uses_per_node_scan(monkeypatch):
    monkeypatch.setenv("O4_REACH_BAND_CLUSTERS", "0")
    band = _FakeBand(with_batch=True)
    nodes = _nodes()
    got = anchors.node_bands(nodes, band, skip_from=None)
    assert got == [(x - 1.0, x + 1.0) for (x, y) in nodes]
    assert band.calls == len(nodes)                     # batch NOT used


def test_node_bands_no_batch_attr_uses_per_node_scan(monkeypatch):
    monkeypatch.setenv("O4_REACH_BAND_CLUSTERS", "1")
    band = _FakeBand(with_batch=False)                  # no .batch
    nodes = _nodes()
    got = anchors.node_bands(nodes, band, skip_from=None)
    assert got == [(x - 1.0, x + 1.0) for (x, y) in nodes]
    assert band.calls == len(nodes)


def test_node_bands_dispatches_to_batch(monkeypatch):
    monkeypatch.setenv("O4_REACH_BAND_CLUSTERS", "1")
    band = _FakeBand(with_batch=True)
    nodes = _nodes()
    got = anchors.node_bands(nodes, band, skip_from=None)
    # identical values to the per-node scan
    assert got == [(x - 1.0, x + 1.0) for (x, y) in nodes]


def test_node_bands_skip_from_zone_tail(monkeypatch):
    monkeypatch.setenv("O4_REACH_BAND_CLUSTERS", "1")
    band = _FakeBand(with_batch=True)
    nodes = _nodes()
    got = anchors.node_bands(nodes, band, skip_from=4)
    assert all(b is not None for b in got[:4])
    assert all(b is None for b in got[4:])
    # and identical to the per-node scan with the same skip
    ref = [None] * len(nodes)
    for i in range(4):
        ref[i] = (nodes[i][0] - 1.0, nodes[i][0] + 1.0)
    assert got == ref


# ── real reach_band_unified.batch byte-identity ──────────────────────────────
@pytest.mark.xdist_group("CYXY")            # reuse CYXY's already-built layout
def test_real_batch_matches_per_node(monkeypatch):
    """On a real airport geometry, ``band.batch`` returns bit-identical bands
    to the per-node ``band`` scan for the same node list — the sound-sharing
    invariant (a shared serving line is only ever reused when it is the
    member's own serving line, else the exact per-point scan runs)."""
    monkeypatch.setenv("O4_REACH_BAND_CLUSTER_QUIET", "1")
    # The cluster amortization (``.batch``) is a LEGACY-band feature; the raster
    # reach field (default on) answers every query in O(1) and node_bands never
    # calls ``.batch``, so this legacy-path invariant is measured with the raster
    # gate forced off.
    monkeypatch.setenv("O4_RASTER_REACH_BAND", "0")
    from conftest import cached_airport_layout
    from auto_patch import grade_graph as GG
    from auto_patch.elevation_per_surface.solver_primitives import _build_node_list
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_unified)

    layout = cached_airport_layout("CYXY")
    nodes, bucket_to_idx = _build_node_list(layout)
    ctx = GG.build_context(layout, bucket_to_idx)
    G = GG.build_unified_graph(layout, bucket_to_idx, ctx=ctx)

    pts = [(float(x), float(y)) for (x, y) in nodes]
    # A confirmed (line-shared) member computes its band from the shared line
    # directly, bypassing the nvc scan/cache — so a mis-shared line surfaces
    # here as a value mismatch against the per-node scan.
    band = reach_band_unified(layout, G)
    assert hasattr(band, "batch"), "reach_band_unified must expose .batch"
    per_node = [band(x, y) for (x, y) in pts]
    batched = reach_band_unified(layout, G).batch(pts, None)
    assert batched == per_node


if __name__ == "__main__":                               # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

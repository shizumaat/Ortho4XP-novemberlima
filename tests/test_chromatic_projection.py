"""Unit tests for the chromatic (graph-colored) Gauss-Seidel projection and its
closed-form chain pre-pass (Tier 3 wave 2c, routing-survey candidates 1, 2, 4;
docs/research/routing_optimization_survey.md).

Hermetic — no build, no fixtures.  Covers, per the acceptance discipline:
  (a) coloring correctness — no intra-color WRITE adjacency, determinism;
  (b) chain detection + chain-projection exactness vs an O(n²) brute force;
  (c) the colored sweep reaches a feasible fixpoint with counts NOT WORSE than
      the legacy scalar worklist on the same instance;
  (d) gate-off inertness — with ``O4_CHROMATIC_PROJECTION`` off the chromatic
      path is never entered and the result is the byte-identical legacy surface.
"""
import random

import pytest

import auto_patch.config as cfg
from auto_patch.elevation_per_surface.route_profile import one_solve as OS


@pytest.fixture(autouse=True)
def _zero_emit_margin(monkeypatch):
    monkeypatch.setattr(cfg, "EMIT_QUANTIZATION_MARGIN_M", 0.0)


# ── helpers ──────────────────────────────────────────────────────────────

def _iter_edges_from(edges, hard, n):
    """Reproduce ``feasibility_project``'s symmetric ``iter_edges`` build (kind
    0 both free, 1 i fixed, 2 j fixed; both-hard dropped) so the coloring /
    chain / sweep primitives can be exercised directly."""
    out = []
    for (i, j, b) in edges:
        hi = i in hard
        hj = j in hard
        if hi and hj:
            continue
        out.append((i, j, b, 1 if hi else (2 if hj else 0)))
    return out


def _worst_symmetric(elev, edges):
    worst = 0.0
    for (i, j, b) in edges:
        worst = max(worst, abs(elev[i] - elev[j]) - b)
    return worst


def _count_violations(elev, edges, tol=1e-3):
    return sum(1 for (i, j, b) in edges if abs(elev[i] - elev[j]) - b > tol)


# ── (a) coloring correctness ──────────────────────────────────────────────

def test_coloring_has_no_intra_color_write_collision():
    random.seed(1)
    n = 40
    edges = [(random.randrange(n), random.randrange(n)) for _ in range(120)]
    iter_edges = []
    for (i, j) in edges:
        if i == j:
            continue
        iter_edges.append((i, j, 1.0, random.choice((0, 1, 2))))
    colors = OS._color_edges_by_write(iter_edges)
    # every edge appears exactly once
    flat = [e for group in colors for e in group]
    assert sorted(flat) == list(range(len(iter_edges)))
    for group in colors:
        written = []
        for e in group:
            i, j, _b, kind = iter_edges[e]
            if kind == 0:
                written += [i, j]
            elif kind == 1:
                written += [j]
            else:
                written += [i]
        # no node written twice within one color
        assert len(written) == len(set(written)), "intra-color write collision"


def test_coloring_is_deterministic():
    iter_edges = [(0, 1, 1.0, 0), (1, 2, 1.0, 0), (2, 3, 1.0, 0),
                  (0, 2, 1.0, 0), (1, 3, 1.0, 0)]
    a = OS._color_edges_by_write(iter_edges)
    b = OS._color_edges_by_write(list(iter_edges))
    assert a == b


def test_coloring_hub_of_one_directional_edges_is_one_color():
    # a host node 0 (immovable, kind moves the OTHER endpoint) shared by many
    # zone edges: all write DISTINCT zone endpoints, so one color suffices.
    iter_edges = [(0, k, 1.0, 1) for k in range(1, 20)]   # i fixed, move j=k
    colors = OS._color_edges_by_write(iter_edges)
    assert len(colors) == 1


# ── (b) chain detection + closed-form exactness ───────────────────────────

def _brute_running_clamp(interior_seed, budgets, v_left, v_right):
    """Independent O(k) reference for :func:`OS._chain_envelope_clamp` — a
    plain-list forward-then-backward running clamp, written separately so an
    indexing / budget-offset bug in the production version is caught."""
    v = [v_left] + list(interior_seed) + [v_right]
    k = len(interior_seed)
    for t in range(1, k + 1):                       # forward
        lo, hi = v[t - 1] - budgets[t - 1], v[t - 1] + budgets[t - 1]
        v[t] = min(max(v[t], lo), hi)
    for t in range(k, 0, -1):                       # backward
        lo, hi = v[t + 1] - budgets[t], v[t + 1] + budgets[t]
        v[t] = min(max(v[t], lo), hi)
    return v[1:k + 1]


def test_chain_running_clamp_matches_brute_force():
    random.seed(3)
    for _ in range(200):
        k = random.randint(1, 8)
        budgets = [round(random.uniform(0.2, 2.0), 3) for _ in range(k + 1)]
        v_left = round(random.uniform(-5, 5), 3)
        v_right = round(random.uniform(-5, 5), 3)
        seed = [round(random.uniform(-10, 10), 3) for _ in range(k)]
        elev = [v_left] + list(seed) + [v_right]
        OS._chain_envelope_clamp(elev, list(range(1, k + 1)), budgets,
                                 v_left, v_right)
        got = elev[1:k + 1]
        exp = _brute_running_clamp(seed, budgets, v_left, v_right)
        assert got == pytest.approx(exp, abs=1e-12)


def test_chain_running_clamp_is_feasible_when_chain_is_feasible():
    # feasible ⟺ |v_left − v_right| ≤ Σb; the two passes must then satisfy
    # every consecutive edge exactly (no iteration).
    random.seed(7)
    for _ in range(300):
        k = random.randint(1, 10)
        budgets = [round(random.uniform(0.5, 2.0), 3) for _ in range(k + 1)]
        total = sum(budgets)
        v_left = round(random.uniform(-5, 5), 3)
        # choose v_right within the feasible span
        v_right = round(v_left + random.uniform(-total, total), 3)
        seed = [round(random.uniform(-30, 30), 3) for _ in range(k)]
        elev = [v_left] + list(seed) + [v_right]
        OS._chain_envelope_clamp(elev, list(range(1, k + 1)), budgets,
                                 v_left, v_right)
        for t in range(k + 1):
            assert abs(elev[t] - elev[t + 1]) <= budgets[t] + 1e-9
        assert elev[0] == v_left and elev[-1] == v_right


def test_chain_prepass_makes_a_feasible_chain_feasible():
    # a 6-node chain between two hard anchors; the closed form must satisfy
    # every consecutive edge in ONE pass (no iteration).
    n = 6
    b = 1.0
    edges = [(k, k + 1, b) for k in range(n - 1)]
    hard = {0, n - 1}
    elev = [0.0, 9.0, -4.0, 7.0, -2.0, 3.0]   # node 0 and 5 are anchors
    elev[0] = 0.0
    elev[5] = 3.0
    iter_edges = _iter_edges_from(edges, hard, n)
    OS._project_chain_prepass(elev, iter_edges, n, hard)
    assert _worst_symmetric(elev, edges) <= 1e-9
    # anchors untouched
    assert elev[0] == 0.0 and elev[5] == 3.0


def test_chain_prepass_terminates_on_a_pure_ring():
    # a pure degree-2 free RING (no immovable boundary) must not hang the walk
    # and must be left entirely to the colored sweep.
    n = 5
    edges = [(0, 1, 1.0), (1, 2, 1.0), (2, 3, 1.0), (3, 4, 1.0), (4, 0, 1.0)]
    hard: set = set()               # every node free, all degree 2 -> a ring
    elev = [0.0, 3.0, -2.0, 5.0, 1.0]
    before = list(elev)
    iter_edges = _iter_edges_from(edges, hard, n)
    n_chains = OS._project_chain_prepass(elev, iter_edges, n, hard)
    assert n_chains == 0            # no boundary -> no chain solved
    assert elev == before           # ring untouched


def test_chain_prepass_skips_branch_and_interval_nodes():
    # node 2 has degree 3 (a branch) → not interior; node 4 touches an interval
    # edge → excluded.  The pre-pass must leave those and their incident
    # non-chain structure to the sweep, moving nothing it should not.
    n = 6
    edges = [(0, 1, 1.0), (1, 2, 1.0), (2, 3, 1.0), (2, 5, 1.0)]
    hard = {0, 3, 5}
    elev = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    iter_edges = _iter_edges_from(edges, hard, n)
    # add an interval edge touching node 4 (isolated here) — must be ignored
    iter_edges.append((4, 3, None, 2))
    before = list(elev)
    OS._project_chain_prepass(elev, iter_edges, n, hard)
    # node 4 never moves (interval-touched); node 2 is a branch, never an
    # interior of a solved chain.
    assert elev[4] == before[4]


# ── (c) colored sweep: feasibility + counts-not-worse ─────────────────────

def _random_feasible_instance(seed):
    """Build a random graph with a KNOWN feasible surface (so the polytope is
    non-empty): pick true elevations, set each edge budget >= the true gap."""
    rng = random.Random(seed)
    n = rng.randint(8, 25)
    true = [rng.uniform(-20, 20) for _ in range(n)]
    edges = []
    seen = set()
    for _ in range(n * 3):
        i, j = rng.randrange(n), rng.randrange(n)
        if i == j or (i, j) in seen or (j, i) in seen:
            continue
        seen.add((i, j))
        gap = abs(true[i] - true[j])
        budget = gap + rng.uniform(0.0, 3.0)      # feasible by construction
        edges.append((i, j, round(budget, 3)))
    # a couple of hard anchors pinned at the true values
    hard = set(rng.sample(range(n), k=max(2, n // 5)))
    seed_elev = [true[i] if i in hard else true[i] + rng.uniform(-8, 8)
                 for i in range(n)]
    return n, edges, hard, seed_elev, true


def test_chromatic_reaches_feasible_fixpoint():
    for s in range(30):
        n, edges, hard, seed_elev, _true = _random_feasible_instance(s)
        iter_edges = _iter_edges_from(edges, hard, n)
        if not iter_edges:
            continue
        elev = list(seed_elev)
        stats: dict = {}
        OS._project_chromatic(elev, iter_edges, n, 4000, 1e-3, {}, stats=stats)
        # hard nodes never moved
        for h in hard:
            assert elev[h] == pytest.approx(seed_elev[h], abs=1e-9)
        # feasible: no symmetric edge with a free endpoint over cap
        for (i, j, b) in edges:
            if i in hard and j in hard:
                continue
            assert abs(elev[i] - elev[j]) - b <= 2e-3, f"seed {s}"


def test_chromatic_counts_not_worse_than_scalar_worklist(monkeypatch):
    # On the same instance, the colored sweep's residual violation count must be
    # <= the legacy scalar worklist's (the acceptance gate, different fixpoint).
    for s in range(40):
        n, edges, hard, seed_elev, _true = _random_feasible_instance(s)
        sc = [{"edges": list(edges)}]

        monkeypatch.setattr(cfg, "CHROMATIC_PROJECTION", False)
        scalar = list(seed_elev)
        rem_s, _ = OS.feasibility_project(scalar, sc, set(hard),
                                          force_scalar=True, max_iters=4000)

        monkeypatch.setattr(cfg, "CHROMATIC_PROJECTION", True)
        chroma = list(seed_elev)
        rem_c, _ = OS.feasibility_project(chroma, sc, set(hard),
                                          force_scalar=True, max_iters=4000)
        assert rem_c <= rem_s, f"seed {s}: chromatic {rem_c} > scalar {rem_s}"


def test_chromatic_reports_dual_stopping_certificate():
    # a trivially-feasible instance stops on PROOF (a clean sweep) well before
    # the cap and reports the avoided sweeps.
    n = 5
    edges = [(0, 1, 5.0), (1, 2, 5.0), (2, 3, 5.0), (3, 4, 5.0)]
    hard = {0, 4}
    iter_edges = _iter_edges_from(edges, hard, n)
    elev = [0.0, 0.0, 0.0, 0.0, 0.0]
    stats: dict = {}
    OS._project_chromatic(elev, iter_edges, n, 4000, 1e-3, {}, stats=stats)
    assert stats["certified"] is True
    assert stats["sweeps"] <= 2
    assert stats["sweeps_avoided"] >= 3998


# ── (d) gate-off inertness ────────────────────────────────────────────────

def test_gate_off_never_enters_chromatic(monkeypatch):
    called = {"n": 0}
    orig = OS._project_chromatic

    def _spy(*a, **k):
        called["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(OS, "_project_chromatic", _spy)
    monkeypatch.setattr(cfg, "CHROMATIC_PROJECTION", False)
    edges = [(0, 1, 1.0), (1, 2, 1.0)]
    elev = [0.0, 5.0, 0.0]
    OS.feasibility_project(elev, [{"edges": edges}], {0, 2},
                           force_scalar=True, max_iters=4000)
    assert called["n"] == 0


def test_gate_on_enters_chromatic(monkeypatch):
    called = {"n": 0}
    orig = OS._project_chromatic

    def _spy(*a, **k):
        called["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(OS, "_project_chromatic", _spy)
    monkeypatch.setattr(cfg, "CHROMATIC_PROJECTION", True)
    edges = [(0, 1, 1.0), (1, 2, 1.0)]
    elev = [0.0, 5.0, 0.0]
    OS.feasibility_project(elev, [{"edges": edges}], {0, 2},
                           force_scalar=True, max_iters=4000)
    assert called["n"] >= 1


def test_gate_off_is_deterministic_legacy(monkeypatch):
    # gate-off must be byte-identical run to run (the legacy scalar worklist).
    monkeypatch.setattr(cfg, "CHROMATIC_PROJECTION", False)
    edges = [(0, 1, 1.0), (1, 2, 1.0), (2, 3, 1.0), (0, 3, 1.0)]
    hard = {0, 3}
    a = [0.0, 6.0, -3.0, 2.0]
    b = [0.0, 6.0, -3.0, 2.0]
    OS.feasibility_project(a, [{"edges": edges}], set(hard), force_scalar=True)
    OS.feasibility_project(b, [{"edges": edges}], set(hard), force_scalar=True)
    assert a == b

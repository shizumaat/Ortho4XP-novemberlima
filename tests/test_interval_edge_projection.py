"""Synthetic unit tests for the Stage B0 SIGNED-INTERVAL edge primitive and the
terrain-role admission scaffolding (docs/slice_b_solver_absorption_design.md).

Hermetic — no build, no fixtures.  These are the fast iteration harness for the
interval-edge projection: every case runs in milliseconds so the primitive is
developed entirely against them, with the CYXY byte-identity build reserved as
the one-shot acceptance gate.

Edge-tuple contract exercised here:
  * SYMMETRIC 3-tuple  ``(i, j, budget)``            → ``|z_i − z_j| ≤ budget``
  * INTERVAL  4-tuple  ``(i, j, low, high)``         → ``low ≤ z_i − z_j ≤ high``
    with either bound ``None`` (that side unbounded).
"""
import math

import pytest

from auto_patch.elevation_per_surface.route_profile import one_solve as OS


@pytest.fixture(autouse=True)
def _zero_emit_margin(monkeypatch):
    """Default the emit-quantization margin to 0 so the pure-projection cases
    converge to their exact law bounds.  The margin-specific tests re-set it
    (their own monkeypatch runs after this fixture, so it wins)."""
    import auto_patch.config as cfg
    monkeypatch.setattr(cfg, "EMIT_QUANTIZATION_MARGIN_M", 0.0)


# ── helpers ──────────────────────────────────────────────────────────────
def _project(elev, edges, hard, *, force_scalar=True, max_iters=4000,
             tol=1e-3):
    """Run ``feasibility_project`` on one edge list.  ``elev`` mutated in
    place; returns ``(remaining_over_cap, both_hard)``."""
    return OS.feasibility_project(
        elev, [{"edges": list(edges)}], set(hard),
        force_scalar=force_scalar, max_iters=max_iters, tol=tol)


def _difference(elev, i, j):
    return elev[i] - elev[j]


# ── 1. floor-only interval edge ──────────────────────────────────────────
def test_floor_only_edge_lifts_free_node_to_the_floor():
    # z_1 − z_0 ≥ 2.0 (floor 2.0, ceiling open); node 0 hard at 0.0.
    elev = [0.0, -5.0]
    edges = [(1, 0, 2.0, None)]
    rem, bh = _project(elev, edges, hard={0})
    assert rem == 0 and bh == 0
    assert elev[0] == 0.0                              # hard never moves
    assert _difference(elev, 1, 0) >= 2.0 - 1e-6
    assert elev[1] == pytest.approx(2.0, abs=1e-3)     # minimal lift onto floor


def test_floor_only_edge_leaves_a_satisfied_node_untouched():
    # A free node already above the floor must not be pulled DOWN — a None
    # ceiling permits any rise.
    elev = [0.0, 9.0]
    edges = [(1, 0, 2.0, None)]
    rem, _bh = _project(elev, edges, hard={0})
    assert rem == 0
    assert elev[1] == pytest.approx(9.0, abs=1e-9)     # open ceiling: no change


# ── 2. ceiling-only interval edge ────────────────────────────────────────
def test_ceiling_only_edge_lowers_free_node_to_the_ceiling():
    # z_1 − z_0 ≤ 3.0 (ceiling 3.0, floor open); node 0 hard at 0.0.
    elev = [0.0, 11.0]
    edges = [(1, 0, None, 3.0)]
    rem, bh = _project(elev, edges, hard={0})
    assert rem == 0 and bh == 0
    assert elev[0] == 0.0
    assert _difference(elev, 1, 0) <= 3.0 + 1e-6
    assert elev[1] == pytest.approx(3.0, abs=1e-3)


def test_ceiling_only_edge_leaves_a_low_node_untouched():
    elev = [0.0, -8.0]
    edges = [(1, 0, None, 3.0)]
    rem, _bh = _project(elev, edges, hard={0})
    assert rem == 0
    assert elev[1] == pytest.approx(-8.0, abs=1e-9)    # open floor: no change


# ── 3. two-sided asymmetric interval edge ────────────────────────────────
def test_asymmetric_interval_clamps_both_directions():
    # −1.0 ≤ z_1 − z_0 ≤ 4.0, node 0 hard at 10.0.
    edges = [(1, 0, -1.0, 4.0)]

    high = [10.0, 20.0]                                # above the ceiling
    rem, _bh = _project(high, edges, hard={0})
    assert rem == 0
    assert _difference(high, 1, 0) == pytest.approx(4.0, abs=1e-3)

    low = [10.0, 2.0]                                  # below the floor
    rem, _bh = _project(low, edges, hard={0})
    assert rem == 0
    assert _difference(low, 1, 0) == pytest.approx(-1.0, abs=1e-3)

    inside = [10.0, 11.5]                              # already lawful
    rem, _bh = _project(inside, edges, hard={0})
    assert rem == 0
    assert inside[1] == pytest.approx(11.5, abs=1e-9)  # untouched


def test_orientation_independence_of_the_signed_interval():
    # The same physical constraint written j−i must behave identically:
    #   z_1 − z_0 ≤ 4  ⇔  z_0 − z_1 ≥ −4.
    a = [10.0, 20.0]
    _project(a, [(1, 0, None, 4.0)], hard={0})
    b = [10.0, 20.0]
    _project(b, [(0, 1, -4.0, None)], hard={0})
    assert b[1] == pytest.approx(a[1], abs=1e-6)
    assert a[1] == pytest.approx(14.0, abs=1e-3)


# ── 4. hard-endpoint weights (hard node never moves) ─────────────────────
def test_both_hard_interval_edge_is_reported_not_forced():
    # Two hard anchors 5 m apart with a ceiling of 1.0: genuinely infeasible,
    # so it must be REPORTED (rem, both-hard) and NEITHER node moved.
    elev = [0.0, 5.0]
    edges = [(1, 0, None, 1.0)]
    rem, bh = _project(elev, edges, hard={0, 1})
    assert elev == [0.0, 5.0]                          # neither hard node moved
    assert rem == 1 and bh == 1


def test_one_hard_endpoint_moves_only_the_free_node():
    # z_1 − z_2 within [−1, 1]; node 0 hard, node 1 free, node 2 free, and a
    # symmetric tie 0–1 keeps 1 near the anchor.  Only free nodes move.
    elev = [100.0, 100.0, 108.0]
    edges = [(0, 1, 0.5),               # symmetric tie: |z0 − z1| ≤ 0.5
             (1, 2, -1.0, 1.0)]         # interval: −1 ≤ z1 − z2 ≤ 1
    rem, _bh = _project(elev, edges, hard={0})
    assert elev[0] == 100.0                            # hard unmoved
    assert abs(elev[0] - elev[1]) <= 0.5 + 1e-3
    assert -1.0 - 1e-3 <= (elev[1] - elev[2]) <= 1.0 + 1e-3
    assert rem == 0


# ── 5. convergence on a mixed symmetric + interval graph ─────────────────
def test_mixed_symmetric_interval_chain_converges_feasible():
    # Chain 0-1-2-3-4 with hard ends (0 at 0.0, 4 at 6.0).  Alternating
    # symmetric caps and one-sided / two-sided interval edges; a feasible
    # surface exists, and every edge must end satisfied.
    elev = [0.0, 0.0, 0.0, 0.0, 6.0]
    edges = [
        (0, 1, 3.0),                    # |z0 − z1| ≤ 3
        (1, 2, -2.0, 2.5),              # −2 ≤ z1 − z2 ≤ 2.5
        (2, 3, 4.0),                    # |z2 − z3| ≤ 4
        (3, 4, -3.0, None),             # z3 − z4 ≥ −3  (z3 ≥ 3)
    ]
    rem, bh = _project(elev, edges, hard={0, 4}, max_iters=8000)
    assert rem == 0 and bh == 0
    assert elev[0] == 0.0 and elev[4] == 6.0
    assert abs(elev[0] - elev[1]) <= 3.0 + 1e-3
    assert -2.0 - 1e-3 <= (elev[1] - elev[2]) <= 2.5 + 1e-3
    assert abs(elev[2] - elev[3]) <= 4.0 + 1e-3
    assert (elev[3] - elev[4]) >= -3.0 - 1e-3


def test_symmetric_edge_embeds_as_interval_exactly():
    # A symmetric budget b must give the SAME result as the interval [−b, +b].
    sym = [0.0, 12.0]
    _project(sym, [(1, 0, 4.0)], hard={0})
    itv = [0.0, 12.0]
    _project(itv, [(1, 0, -4.0, 4.0)], hard={0})
    assert itv[1] == pytest.approx(sym[1], abs=1e-9)


# ── 6. scalar vs vectorised agreement on an interval graph ───────────────
def test_scalar_and_vectorised_agree_on_an_interval_graph(monkeypatch):
    # A FULLY-DETERMINED mixed graph (unique feasible point), so Gauss-Seidel
    # and the degree-normalised Jacobi — which generally converge to DIFFERENT
    # feasible points — must land on the SAME surface within solver tolerance.
    #   hard 0 = 0.0, hard 3 = 5.0
    #   node 1: interval ceiling z1 − z0 ≤ 2  AND interval floor z1 − z3 ≥ −3
    #           ⇒ z1 ≤ 2 and z1 ≥ 2 ⇒ z1 = 2 (both interval sides active)
    #   node 2: symmetric budget-0 tie to node 1 ⇒ z2 = z1 = 2 (mixed graph)
    edges = [
        (1, 0, None, 2.0),              # interval ceiling
        (1, 3, -3.0, None),             # interval floor
        (2, 1, 0.0),                    # symmetric equality tie
    ]
    seed = [0.0, 9.0, -7.0, 5.0]
    hard = {0, 3}

    scalar = list(seed)
    OS.feasibility_project(scalar, [{"edges": list(edges)}], set(hard),
                           force_scalar=True, max_iters=8000, tol=1e-3)

    # Force the vectorised Jacobi variant on for the comparison run.
    monkeypatch.setattr(OS, "_FP_VECTORIZE", True)
    vect = list(seed)
    OS.feasibility_project(vect, [{"edges": list(edges)}], set(hard),
                           force_scalar=False, max_iters=8000, tol=1e-3)

    for surface in (scalar, vect):
        assert surface[0] == 0.0 and surface[3] == 5.0     # hard nodes pinned
        assert surface[1] == pytest.approx(2.0, abs=5e-2)
        assert surface[2] == pytest.approx(2.0, abs=5e-2)
    for k in range(4):
        assert vect[k] == pytest.approx(scalar[k], abs=5e-2)


# ── 7. quantization-margin behaviour on interval edges ───────────────────
def test_interval_margin_shrinks_finite_sides_inward(monkeypatch):
    import auto_patch.config as cfg
    monkeypatch.setattr(cfg, "EMIT_QUANTIZATION_MARGIN_M", 0.1)
    # Ceiling 3.0 with a 0.1 margin is enforced at 2.9, so a high free node
    # settles at 2.9 — yet the RAW tally (against 3.0) reports no violation.
    elev = [0.0, 11.0]
    rem, _bh = _project(elev, [(1, 0, None, 3.0)], hard={0})
    assert rem == 0
    assert elev[1] == pytest.approx(2.9, abs=1e-3)


def test_interval_margin_leaves_open_side_alone(monkeypatch):
    import auto_patch.config as cfg
    monkeypatch.setattr(cfg, "EMIT_QUANTIZATION_MARGIN_M", 0.1)
    # Floor 2.0 (finite) shrinks inward to 2.1; the open ceiling is untouched.
    elev = [0.0, -5.0]
    _project(elev, [(1, 0, 2.0, None)], hard={0})
    assert elev[1] == pytest.approx(2.1, abs=1e-3)


def test_margined_interval_helper_matches_margined_budget_symmetric():
    # The interval margin must generalise _margined_budget: a symmetric slab
    # (−b, +b) shrinks to (−(b−m), +(b−m)) on both sides.
    for b in (0.2, 1.0, 5.0):
        m = 0.05
        lo, hi = OS._margined_interval(-b, b, m)
        assert hi == pytest.approx(OS._margined_budget(b, m))
        assert lo == pytest.approx(-OS._margined_budget(b, m))


def test_margined_interval_floor_semantics():
    # Below-floor finite sides are never driven past ±_QUANT_MARGIN_FLOOR_M,
    # and a tiny finite side at/under the floor is left unchanged (mirrors the
    # symmetric flat-cross budget staying enforceable).
    floor = OS._QUANT_MARGIN_FLOOR_M
    lo, hi = OS._margined_interval(-0.5, 0.5, 10.0)     # huge margin
    assert hi == pytest.approx(floor)
    assert lo == pytest.approx(-floor)
    lo2, hi2 = OS._margined_interval(-0.001, 0.001, 0.1)   # both inside floor
    assert (lo2, hi2) == (-0.001, 0.001)
    assert OS._margined_interval(-2.0, 2.0, 0.0) == (-2.0, 2.0)   # margin 0


# ── pure-interval graph (no symmetric edges) still solves ────────────────
def test_pure_interval_graph_without_symmetric_edges():
    # The early-return guard must not bail when only interval edges exist.
    elev = [0.0, 20.0, -20.0]
    edges = [(1, 0, None, 2.0),         # z1 ≤ z0 + 2
             (2, 0, -2.0, None)]        # z2 ≥ z0 − 2
    rem, bh = _project(elev, edges, hard={0})
    assert rem == 0 and bh == 0
    assert elev[0] == 0.0
    assert elev[1] == pytest.approx(2.0, abs=1e-3)
    assert elev[2] == pytest.approx(-2.0, abs=1e-3)


def test_pure_interval_graph_vectorised_path(monkeypatch):
    # REGRESSION (EGWN, tile +51-001): the vectorised Jacobi with an
    # ALL-interval edge set (zero symmetric edges) crashed with
    # ``Cannot cast ufunc 'add' output from dtype('float64') to
    # dtype('int64')`` — np.bincount's empty-input fast path returns int64
    # even with float weights, so the symmetric block's empty bincounts made
    # ``acc``/``cnt`` int64 and the interval block's ``+=`` raised.  The
    # accumulators must be born float64 when no symmetric edges exist.
    monkeypatch.setattr(OS, "_FP_VECTORIZE", True)
    elev = [0.0, 20.0, -20.0]
    edges = [(1, 0, None, 2.0),         # z1 ≤ z0 + 2
             (2, 0, -2.0, None)]        # z2 ≥ z0 − 2
    rem, bh = OS.feasibility_project(
        elev, [{"edges": list(edges)}], {0},
        force_scalar=False, max_iters=4000, tol=1e-3)
    assert rem == 0 and bh == 0
    assert elev[0] == 0.0
    assert elev[1] <= 2.0 + 1e-3
    assert elev[2] >= -2.0 - 1e-3


# ── 8. interval-aware reach envelope + anchor-contradiction break (B3) ────
# These exercise the DIRECTED reach-envelope propagation over signed slabs
# (the deferred Stage-B0 concern the Stage-B3 fix delivers): a signed interval
# ``low ≤ z_i − z_j ≤ high`` contributes ``ceil_i ≤ ceil_j + high`` /
# ``floor_i ≥ floor_j + low`` (and the transpose) to the one-shot envelope, so
# an interval-only free node gets bounds AND a contradiction between two parent
# slabs is caught by the ``floor > ceil`` break detection instead of ping-
# ponging the POCS sweep to the visit cap.


def test_interval_reach_ceiling_clamps_in_one_shot():
    # A free node reachable ONLY through a ceiling-only interval from a hard
    # anchor is clamped by the envelope in a single pass — with max_iters=1
    # (one worklist visit budget per edge) it still lands exactly on the
    # ceiling, proving the clamp happened pre-sweep, not by iteration.
    elev = [10.0, 500.0]
    edges = [(1, 0, None, 3.0)]                     # z1 − z0 ≤ 3
    rem, _bh = _project(elev, edges, hard={0}, max_iters=1)
    assert rem == 0
    assert elev[1] == pytest.approx(13.0, abs=1e-3)


def test_interval_reach_floor_clamps_in_one_shot():
    # Symmetric of the above for the FLOOR envelope over a floor-only interval.
    elev = [10.0, -500.0]
    edges = [(1, 0, 2.0, None)]                     # z1 − z0 ≥ 2
    rem, _bh = _project(elev, edges, hard={0}, max_iters=1)
    assert rem == 0
    assert elev[1] == pytest.approx(12.0, abs=1e-3)


def test_interval_two_parent_disjoint_slabs_break_not_livelock():
    # THE B3 livelock class in miniature: one free spine node with two HARD
    # parents whose narrow slabs cannot be jointly satisfied.  The envelope
    # sees floor = max(z0+0, z1+0) = 2.0 > ceil = min(z0+0.5, z1+0.5) = 0.5,
    # so the node is BROKEN (quarantined) and the violation REPORTED — and
    # crucially the call returns under a TIGHT iteration budget instead of
    # ping-ponging to the cap (pre-fix this oscillated 2^k times).
    elev = [0.0, 2.0, 100.0]
    edges = [(2, 0, 0.0, 0.5),          # 0.0 ≤ z2 − z0 ≤ 0.5  → z2 ∈ [0.0,0.5]
             (2, 1, 0.0, 0.5)]          # 0.0 ≤ z2 − z1 ≤ 0.5  → z2 ∈ [2.0,2.5]
    rem, bh = _project(elev, edges, hard={0, 1}, max_iters=5)
    assert elev[0] == 0.0 and elev[1] == 2.0        # hard parents unmoved
    assert rem >= 1                                  # contradiction reported
    assert bh == 0                                   # neither edge is both-hard
    # The quarantined node sits between the two disjoint slabs (the distance-
    # weighted break blend), NOT flung outside them.
    assert 0.5 - 1e-6 <= elev[2] <= 2.0 + 1e-6


def test_interval_reach_broken_node_is_quarantined_via_broken_out():
    # The broken set surfaced through ``broken_out`` must contain the
    # contradiction node (so a caller can honestly quarantine it downstream).
    elev = [0.0, 3.0, 50.0]
    edges = [(2, 0, 0.0, 0.4), (2, 1, 0.0, 0.4)]    # disjoint by 3 m ≫ 0.4
    broken: set = set()
    OS.feasibility_project(elev, [{"edges": list(edges)}], {0, 1},
                           force_scalar=True, max_iters=5, broken_out=broken)
    assert 2 in broken


def test_interval_reach_compatible_parents_do_not_false_break():
    # Two parent slabs whose intersection is NON-empty must NOT break the node
    # — the directed envelope has to be an intersection, not a spurious
    # tightening.  z2 ∈ [z0+0, z0+1] ∩ [z1−1, z1+0]; with z0=0, z1=0.5 that is
    # [0,1] ∩ [-0.5,0.5] = [0,0.5], non-empty.
    elev = [0.0, 0.5, 9.0]
    edges = [(2, 0, 0.0, 1.0),          # 0 ≤ z2 − z0 ≤ 1
             (2, 1, -1.0, 0.0)]         # −1 ≤ z2 − z1 ≤ 0  → z2 ≤ z1
    rem, bh = _project(elev, edges, hard={0, 1})
    assert rem == 0 and bh == 0
    assert 0.0 - 1e-3 <= elev[2] <= 0.5 + 1e-3


def test_interval_reach_directed_bounds_through_a_free_relay():
    # The directed propagation must chain THROUGH a free intermediate node:
    # hard 0 = 0; interval 1←0 gives z1 ≥ z0 + 1; interval 2←1 gives
    # z2 ≥ z1 + 1; so the FLOOR envelope must lift z2 to ≥ 2 even though node 2
    # touches no anchor directly.
    elev = [0.0, -100.0, -100.0]
    edges = [(1, 0, 1.0, None),         # z1 − z0 ≥ 1
             (2, 1, 1.0, None)]         # z2 − z1 ≥ 1
    rem, _bh = _project(elev, edges, hard={0}, max_iters=1)
    assert rem == 0
    assert elev[1] == pytest.approx(1.0, abs=1e-3)
    assert elev[2] == pytest.approx(2.0, abs=1e-3)


def test_reach_strict_pop_guard_survives_equal_keys():
    # DIAMOND with two EQUAL-budget symmetric paths to node 3: both reach it at
    # the same envelope value, exercising the strict ``if k in best`` pop guard
    # under equal keys (the memory-documented CYXY-2026-07-04 hang class — no
    # epsilon tolerance).  Must terminate and give the exact reachable value.
    elev = [0.0, 9.0, 9.0, 9.0]
    edges = [(0, 1, 1.0), (0, 2, 1.0),  # two length-1 hops from the anchor
             (1, 3, 1.0), (2, 3, 1.0)]  # rejoining at node 3 (two equal paths)
    rem, _bh = _project(elev, edges, hard={0}, max_iters=1)
    assert rem == 0
    # ceil_3 = 0 + 1 + 1 = 2 by either path; the clamp pulls the seed 9 → 2.
    assert elev[3] == pytest.approx(2.0, abs=1e-3)
    assert elev[1] == pytest.approx(1.0, abs=1e-3)

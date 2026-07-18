# Routing & numerical-optimization survey for the grade-projection kernels

Status: research report (2026-07-17). **No source was modified.** This is a
literature/algorithm map for the three solver kernels, adversarially checked
against the code as it stands today.

## 0. What the code already does (the baseline we must beat)

Read from `src/auto_patch/elevation_per_surface/route_profile/one_solve.py`
(`feasibility_project`, `_project_vectorized`, `_reach`) and
`solver_primitives.py`.

- **KERNEL B — feasibility projection.** Project a value vector onto
  `{box_i} ∩ {|v_i − v_j| ≤ b_ij over E}` (plus signed-interval slabs
  `low ≤ v_i − v_j ≤ high`). Today = a two-part scheme:
  1. **Reach-envelope warm start / box clamp.** `_reach(+1)` / `_reach(-1)`
     compute `ceil_i = min_a (z_a + capdist(a→i))` and
     `floor_i = max_a (z_a − capdist(a→i))` by multi-source Dijkstra, then
     clamp every free node into `[floor_i, ceil_i]`. This is already the
     shortest-path dual of the difference-constraint system, and `floor_i >
     ceil_i` is already used as a **Bellman-Ford-style infeasibility
     certificate** → the node is quarantined ("broken") with a
     distance-weighted blend. *This is the single most important thing to
     notice: the anchor-driven (long-range) part of the projection is already
     solved in closed form by two Dijkstra passes; iteration only cleans up
     the residual free↔free edges.*
  2. **Worklist Gauss-Seidel POCS.** FIFO queue of violated edges; pop an
     edge, split the excess onto its free endpoint(s), re-enqueue incident
     edges. Deterministic (edge-order init, FIFO requeue, visit cap =
     `max_iters·|E|`). There is also a numpy **degree-normalised Jacobi**
     variant (`_project_vectorized`, gate `O4_FP_VECTORIZE`) using
     `np.bincount` scatter — faster per sweep but *weaker convergence*
     (stalls with many marginally-over-cap edges, so the final projection
     is forced back to scalar) and *not byte-identical*.
- **KERNEL E — multi-source value fields.** `_reach` = 2-pass multi-source
  Dijkstra, sign baked into edge weights. The brief calls this "already
  good"; agreed. It is exactly the **McShane–Whitney maximal/minimal
  Lipschitz extension** of the anchor values under the cap metric.
- **KERNEL F — route queries at scale.** Repeated cap-distance / reach
  queries on the frozen per-airport graph. Today these are folded into
  multi-source Dijkstra sweeps rather than point-to-point queries.

Hard constraints restated: determinism (fixed order **or** provably
order-independent fixpoint); byte-identity ideal but a **"counts-not-worse"**
different-legal-fixpoint precedent exists; Python-orchestrated
(numpy/scipy/C-ext fine); a few GB RAM; graph frozen ⇒ preprocessing
amortizes over one solve+projection+validation cycle.

Dependency note (repo convention "new dep ⇒ installers + onboarding"):
`numpy 2.4.4` and `scipy 1.17.1` are **already** dependencies. `pyamg`,
`osqp`, `networkit`, `RoutingKit` are **not** — each is a new-dependency cost
that must be weighed against its win.

---

## Shortlist (ranked)

| # | Candidate | Kernel | Expected win @ our size | Impl cost | Determinism class |
|---|-----------|--------|------------------------|-----------|-------------------|
| 1 | **Chromatic (graph-colored) Gauss-Seidel POCS**, numpy-vectorized per color, active-set gated | B | **5–20× wall** on iteration-bound airports | Medium | Order-independent fixpoint → *counts-not-worse* |
| 2 | **Closed-form chain/tree projection** for spines & rect couples (Lipschitz-on-paths) | B (+E) | Removes a whole POCS class; **O(n) exact** on 1-D substructures | Low–Medium | Exact/deterministic; near-byte on chains |
| 3 | **Multilevel (constraint-graph coarsening) POCS**, + PyAMG for the *Laplacian smoothing* target only | B (smoother), separate smoothing solve | Long-chain sweep count O(L)→O(log L); plateau airports | High (novel) / Low (PyAMG drop-in) | Deterministic w/ sorted aggregation → *counts-not-worse* |
| 4 | **Dual-coordinate-ascent framing + optional Dykstra** (guarantees, stopping rule; OSQP as measured-against baseline) | B | Convergence *guarantee*, not raw speed; OSQP likely **loses** | Low (framing) / High (OSQP) | Dykstra → exact L2 (byte-stable); OSQP rejected |
| 5 | **Hub labeling (PLL) / Contraction Hierarchies** | F | µs point-to-point *iff* F is truly many-to-many p2p | High (bind C++) | Exact; deterministic |

**What I would build first: #1 (colored GS), then the #2 chain closed-form.**
Rationale below.

---

## 1. Chromatic (graph-colored) parallel Gauss-Seidel POCS  — SURVIVOR, build first

**What it is.** Gauss-Seidel is sequential *because* two edges sharing a node
must not be relaxed simultaneously (they'd both move the shared node from the
same stale value). Color the nodes of the constraint graph so no edge joins
two same-color nodes (equivalently, partition *edges* into rounds where no two
edges in a round share an endpoint — a matching/edge-coloring, or the standard
node-coloring trick). Within one color class the updates are **independent**,
so they can be done as a single vectorized numpy step (`bincount` scatter,
exactly the primitive already in `_project_vectorized`); across colors you use
the *latest* values (true Gauss-Seidel, not Jacobi). This is the textbook way
to parallelize/vectorize GS and SOR without losing GS convergence
([Kaman, graph-coloring GS](https://erkaman.github.io/posts/gauss_seidel_graph_coloring.html);
[Sandia, parallel coloring for GS/manycore](https://www.osti.gov/servlets/purl/1344720)).

**Precise mapping to KERNEL B.** Replace *both* current inner paths (the
scalar worklist and the degree-normalised Jacobi) with: pre-compute one greedy
coloring of the frozen constraint graph; each sweep = for each color, gather
that color's active over-cap edges, compute signed excess, scatter the split
correction to the (free) endpoints with `bincount`. The box clamp and the
signed-interval slabs fold in as a "color 0" per-node projection each sweep.
The worklist's key virtue — *only touch edges whose endpoint moved* — is kept
by intersecting each color with a live active-set (an edge re-enters when an
incident node moved, same trigger as today's `incident[]` re-enqueue).

**Why it fits us specifically.** Airport constraint graphs are **near-planar**
(pavement rings + visibility chords bounded by a local window; spines are
chains). Planar and near-planar graphs have small chromatic number (≤ 6 for
planar; empirically 4–12 here after greedy Welsh–Powell), so a sweep is
4–12 vectorized numpy ops over ~E/colors edges each — versus E Python-level
scalar iterations today. On an M-series core that is the 5–20× the vectorized
Jacobi already showed, **but without the Jacobi convergence penalty** (the
reason the final projection is currently forced back to scalar). It also keeps
the reach-envelope warm start unchanged — colored GS only accelerates the
residual free↔free cleanup, which is where the sweeps actually go.

**Expected win (honest).** Asymptotically same sweep *count* as sequential GS
(colored GS ≈ sequential GS in iteration count on these graphs), with per-sweep
cost moving from interpreted-Python to vectorized C. So the win is a **constant
factor set by (Python-scalar cost per edge) / (numpy cost per edge / colors)** —
realistically 5–20× on the airports where iteration dominates (OTHH-class
flat-but-huge, and dense-apron hard airports), and ~1× (no regression) on
airports the worklist already drains in <1 s. It does **not** reduce the
*number* of sweeps on pathological long chains — that is survivor #2/#3.

**Determinism / byte-identity.** Within a color the updates are independent, so
the result is *invariant to intra-color order* — an order-independent fixpoint,
which satisfies the determinism ruling by construction (no `sorted()` even
needed inside a color; use `sorted()` only to fix the color *labels*). It is
**not** byte-identical to today's FIFO worklist (different legal fixpoint) →
lands in the accepted **counts-not-worse** class. Validate with the existing
counts-gate on SPJC/SPLP/CYXY/HECA.

**Implementation cost.** Medium. Coloring: greedy, `O(E)`, once per airport
(frozen graph). No new dependency (pure numpy; `scipy.sparse.csgraph` can
supply the CSR adjacency). The scatter primitive already exists in
`_project_vectorized`. Main work is the active-set/color bookkeeping and
proving the counts-not-worse gate.

**Risks.** (a) High-degree hub nodes — a host shared by *k* adjacent-ground
zones, or a flat-group representative aliasing many chords — inflate the color
count or serialize their neighborhood. Mitigation: the `interval_yield_from`
host-authoritative kind fix already makes zone→host one-directional, so hubs
move one way; and those edges can be pulled into a small "residual scalar tail"
color. (b) Coloring quality: a bad greedy order → too many colors → less
speedup (never wrong, just slower). Use degree-descending (Welsh–Powell),
`sorted()` on `(−degree, node_id)` for determinism.

---

## 2. Closed-form chain/tree projection for 1-D substructures — SURVIVOR

**What it is.** The projection of a target vector onto Lipschitz (bounded
consecutive-difference) constraints **on a path or tree** has an *exact
linear-time* algorithm — no iteration. This is the "Lipschitz isotonic/unimodal
regression on paths and trees" line
([Agarwal–Phillips–Sadri, arXiv:0912.5182](https://arxiv.org/abs/0912.5182);
[Lipschitz-PAV](https://users.cs.utah.edu/~jeffp/papers/regression.pdf)) and,
in the interpolative view,
[From isotonic to Lipschitz regression (arXiv:2307.05732)](https://arxiv.org/abs/2307.05732).
The `min-over-anchors (anchor + cap·dist)` field is itself the McShane–Whitney
maximal Lipschitz extension; on a *chain* the tightest feasible profile nearest
a target is a forward+backward two-pass ("running clamp": `v_i ←
clamp(v_i, v_{i-1} − b, v_{i-1} + b)` forward, then backward), i.e. exactly the
envelope idea applied *within* the chain.

**Precise mapping to KERNEL B.** Our graph has explicit 1-D substructures the
docstrings call out: the **taxi spine** ("clamps ONLY to its
centerline-consecutive neighbours — a 1-D feasible chain") and **rect
axial/cross couples**. For any connected component of the *free* subgraph that
is a path or tree (spines, service-road chains, rect ladders), replace POCS
with the O(n) forward/backward sweep to the exact L∞-tightest feasible profile
consistent with its two anchored ends and box bounds. Only the genuinely
**cyclic** 2-D apron/junction interiors then need the iterative smoother
(survivor #1).

**Expected win.** Spines and rect couples are exactly the substructures where
POCS chains excess node-by-node (O(chain length) sweeps). Closed-form makes
them O(n) *once*, deterministic, and removes their contribution to the "1–2k
sweep plateau." On airports whose residual is spine-dominated (CYXY-class
service-road ravines are named in the code) this can be most of the remaining
iteration. It does **nothing** for dense apron interiors (those aren't trees).

**Determinism / byte-identity.** The two-pass chain clamp is exact and
order-fixed; for a pure chain it can even be made *byte-comparable* to a fully
converged scalar POCS on that chain (both reach the unique tightest profile).
Effectively the strongest determinism story of any survivor. Safe-classify as
counts-not-worse and check for byte-identity on chain fixtures.

**Implementation cost.** Low–Medium. Detect tree/path components of the free
subgraph (union-find + degree check, `O(E)`), run the sweep, hand the rest to
survivor #1. `scipy.optimize.isotonic_regression` (SciPy ≥1.12, present)
covers the monotone special case and is a useful reference/validation oracle,
but the *symmetric Lipschitz* clamp is a ~10-line two-pass you write directly.

**Risks.** Component classification must be exact (a chord silently making a
"chain" cyclic would make the closed form wrong) — gate on strict degree ≤2 /
acyclic, fall back to #1 otherwise. Interval (asymmetric) slabs on a chain
still work (clamp with `[low, high]` instead of `[−b, +b]`).

---

## 3. Multilevel / constraint-graph coarsening (+ PyAMG for the smoothing target)

**Two distinct things, don't conflate them.**

**3a. Multilevel POCS for the feasibility projection (novel, high-risk).**
POCS/GS is a *smoother*: it kills high-frequency (local) infeasibility fast but
low-frequency error — long cap-chains spanning the field — decays as
O(1/sweeps), which is exactly the "smears ±1 m across the whole region" /
"1–2k sweep plateau" failure the code documents. The multigrid remedy: build a
hierarchy by aggregating nodes (matching), where a **coarse edge budget = the
min cap-path between aggregates** (obtainable from the same Dijkstra
machinery), project on the coarse graph (few nodes, cheap), interpolate the
coarse solution as a warm start, then smooth on the fine graph (survivor #1).
A V-cycle. The reach envelope is already a *degenerate* two-level method (the
anchors are the coarsest level); this generalizes it to a real hierarchy so
low-frequency modes are resolved in O(log L) rather than O(L) sweeps.

- **Win:** on long-chain-dominated / plateau airports, potentially turns
  thousands of sweeps into tens. This is the only survivor that attacks the
  *sweep count* rather than the per-sweep cost.
- **Cost:** High. There is **no off-the-shelf library for *inequality*
  multigrid** — the aggregation, the cap-consistent coarse budgets, and the
  interpolation are hand-built and must be proven feasibility-preserving. This
  is a research spike, not a port.
- **Determinism:** `sorted()` aggregation + fixed cycle schedule →
  deterministic; counts-not-worse.
- **Verdict:** promising ceiling, but only after #1 and #2 land and profiling
  shows a residual *low-frequency* plateau that #2's chain handling didn't
  already remove.

**3b. PyAMG for the *smoothing / min-curvature* target (drop-in, low-risk).**
The apron "smoothest (min-curvature)" and route targets are **weighted graph
Laplacian least-squares** — genuine *linear* systems, not inequalities. Those
are precisely what algebraic multigrid solves optimally.
[PyAMG](https://github.com/pyamg/pyamg) (mature, Python + small C++) or the
[LAMG](https://arxiv.org/pdf/1108.1310) / [CMG](https://www.sciencedirect.com/science/article/abs/pii/S1077314211001627)
graph-Laplacian solvers apply *directly* to the target-shaping sub-solve, not
to the feasibility projection. If the min-curvature target is currently formed
by iteration, an AMG solve is a clean asymptotic win there.
- **Cost:** Low (PyAMG is a drop-in `scipy.sparse` solver) — but it is a **new
  dependency** (installers/onboarding/wheels per repo convention).
- **Determinism:** AMG is iterative-to-tolerance; deterministic given fixed
  setup + tolerance, but *not* byte-identical to a different iterative target
  former → counts-not-worse, and only where the target is a pure Laplacian.

---

## 4. Dual-coordinate-ascent framing, Dykstra, and the OSQP baseline

**The framing (free, do it regardless).** The scalar worklist is *literally*
block-coordinate ascent on the **dual** of the L2 projection: each
difference-constraint has one dual variable, and relaxing an edge = one dual
coordinate step
([Bertsekas, dual coordinate step for network flow](https://web.mit.edu/dimitrib/www/DualCoordinateStep.pdf);
[Tseng, dual ascent for strictly convex + linear constraints](https://epubs.siam.org/doi/10.1137/0328011)).
This matters because it **removes the "Jacobi/GS has no convergence guarantee"
worry** the code comments flag: a properly-ordered dual coordinate ascent on a
convex feasibility QP has monotone convergence, and it hands you a principled
**stopping rule** (dual residual / KKT gap) instead of the current visit cap.
Cost: zero new code to *think* this way; small code to add the stopping test.

**Dykstra vs POCS (optional exactness).** Plain POCS converges to *some*
feasible point; **Dykstra's algorithm** (POCS + per-set correction terms)
converges to the *unique L2-nearest* feasible point, with linear rate
([Wikipedia: Dykstra](https://en.wikipedia.org/wiki/Dykstra's_projection_algorithm);
[Bauschke et al., finite convergence/stalling, arXiv:2001.06747](https://arxiv.org/pdf/2001.06747)).
Relevance: if we ever want the *minimum-displacement* projection to be a
canonical, warm-start-invariant surface (byte-stable regardless of the initial
guess), Dykstra gives it. Since the project *accepts* counts-not-worse, Dykstra
is **optional** — but note it converges *slower* than POCS
(~1100 vs ~380 iters in the cited benchmark), so adopt it only for the
canonicality property, never for speed.

**OSQP as measured-against baseline — REJECT with reason.**
[OSQP](https://arxiv.org/abs/1711.08013) is ADMM for general QP with excellent
warm-start + factorization caching (6–8× on *repeated* solves). Adversarial
check against our structure:
- It needs a **KKT factorization** of an `(n+E)×(n+E)` quasi-definite system.
  At `E` up to 2.5×10⁶ that factorization's fill-in dominates memory and time,
  and it must be redone whenever the constraint *set* changes (our active set /
  lazy-expanded edges change within a solve).
- It targets *general* QP and thereby **discards the pure combinatorial
  difference-constraint structure** that Dijkstra + colored-GS exploit for
  near-linear cost. The warm-start advantage is for *parametric families of
  similar QPs*; our projection is essentially one-shot per solve.
- **Verdict:** OSQP loses here on both the factorization cost and the
  structure-discarding grounds. Keep as the documented baseline the survivors
  beat, not a survivor. (Same logic rejects interior-point QP.)

---

## 5. Hub labeling (PLL) / Contraction Hierarchies for KERNEL F — conditional

**What it is.** CH and Hub Labeling answer point-to-point shortest-path in
microseconds after heavy preprocessing on a frozen graph
([Abraham et al., hub labeling](https://www.microsoft.com/en-us/research/wp-content/uploads/2010/12/HL-TR.pdf);
[PLL / pruned landmark labeling, VLDB experimental study](http://www.vldb.org/pvldb/vol11/p445-li.pdf)).
Exactly the "continent-scale shortest path in µs" technology.

**Adversarial check against our F.** The reach *fields* are already
multi-source Dijkstra = **one sweep amortizes all sources at once**; CH/HL only
help when F is genuinely **many distinct point-to-point (s,t) queries** that
can't be batched into a few multi-source sweeps. Concretely:
- If profiling shows F is dominated by 10⁴–10⁵ *distinct* p2p cap-queries
  between pavement features (not expressible as a handful of multi-source
  fields), then **Hub Labeling (PLL)** is the pick over CH (simpler µs queries,
  better for many-to-many; label sizes on near-planar n=10⁵ graphs are modest).
- Otherwise (the likely case — the code batches queries into fields), CH/HL
  **do not pay off**: preprocessing at n=10⁵ costs seconds-to-minutes
  (competing with the whole solve budget), and Python per-query overhead erodes
  the µs advantage unless the query *loop* is also native.

**Library reality.** No mature *Python* CH/HL library. Options: bind
[RoutingKit](https://github.com/RoutingKit/RoutingKit) (C++, CH+HL) or a Go
[LdDl/ch](https://github.com/LdDl/ch); `networkit` has related contraction
tooling. All are **new native dependencies** — a heavy lift for a conditional
win.

**Verdict:** Do **not** build speculatively. Gate on a profiling finding that F
is a distinct, dominant, non-batchable point-to-point cost. If it is, PLL via
RoutingKit bindings; else keep batching into multi-source Dijkstra. Determinism
is fine (exact distances). Parallel/deterministic **Δ-stepping**
([Meyer–Sanders; VDS variant](https://ieeexplore.ieee.org/document/9915894/))
or a **Fast Iterative Method** sweep
([Jeong–Whitaker FIM](https://epubs.siam.org/doi/10.1137/060670298)) is the
lighter alternative if the *field* construction (E) ever becomes the
bottleneck — but the brief rates E "already good," so this is a back-pocket
item, not a survivor.

---

## 6. On "SIMD/vectorized constraint sweeping" (the brief's last bullet)

This is not a separate candidate — it is *the implementation mechanism* of
survivors #1 and #3a, and the project already has the core primitive
(`np.bincount` scatter in `_project_vectorized`). The lesson from OSQP internals
and graph-projection-splitting (POGS) is that at our sizes the winning shape is:
**edge arrays in CSR, deterministic color/round ordering, scatter-add per
round** — not a global matrix factorization. Survivor #1 is that lesson applied
correctly (per-color independence recovers Gauss-Seidel convergence, which the
naive all-edges-at-once Jacobi threw away).

---

## 7. What I would build first (concrete sequence)

1. **Colored GS POCS (survivor #1).** Greedy Welsh–Powell coloring of the
   frozen constraint graph (`sorted((−deg, id))`), per-color `bincount` scatter
   reusing the existing primitive, active-set gating from the existing
   `incident[]` trigger, box+interval clamp as color 0. Replaces both the
   scalar worklist and the stalling Jacobi path. Gate + counts-not-worse
   validation on SPJC/SPLP/CYXY/HECA. Highest certain-win-to-cost ratio, no new
   dependency, cleanest determinism story.
2. **Chain closed-form (survivor #2).** Detect acyclic free components
   (spines, rect couples, service chains), solve them with the two-pass
   Lipschitz clamp, hand cyclic interiors to #1. Removes the spine/ravine POCS
   class outright; strongest determinism (near byte-identity on chains).
3. **Dual stopping rule (survivor #4 framing).** Add a KKT/dual-residual stop
   to replace the raw visit cap — cheap, and it retires the "no convergence
   guarantee" caveat that currently forces the scalar fallback.
4. **Re-profile.** Only if a *low-frequency* plateau survives #1–#3 do we
   invest in **multilevel POCS (survivor #3a)** — the highest ceiling but the
   only research-grade build. PyAMG (#3b) is an independent, low-risk win *iff*
   the min-curvature target is currently iterated and can be posed as a pure
   Laplacian solve (weigh the new dependency).
5. **KERNEL F (survivor #5) stays gated** on a profiling finding that
   point-to-point queries dominate and can't be batched. Default answer: keep
   batching into multi-source Dijkstra.

Rejected and why: **OSQP / interior-point QP** (factorization cost at E≈10⁶ +
discards difference-constraint structure); **speculative CH/HL** (no Python lib,
preprocessing competes with solve budget, queries already batched);
**naive all-edge Jacobi** (already in-tree, stalls — superseded by #1).

---

## Sources

- Colored/parallel Gauss-Seidel: [Kaman — Parallelizing GS via graph coloring](https://erkaman.github.io/posts/gauss_seidel_graph_coloring.html); [Sandia — Parallel graph coloring for manycore](https://www.osti.gov/servlets/purl/1344720); [Koçak — Coloring for distributed-memory parallel GS](https://repository.bilkent.edu.tr/bitstream/handle/11693/52416/MScThesis_OnurKocak-Coloring_For_Distributed-Memory-Parallel_Gauss-Seidel_Algorithm.pdf?sequence=1).
- Lipschitz/isotonic regression on paths & trees: [Agarwal, Phillips, Sadri — arXiv:0912.5182](https://arxiv.org/abs/0912.5182); [Lipschitz-PAV](https://users.cs.utah.edu/~jeffp/papers/regression.pdf); [From isotonic to Lipschitz regression — arXiv:2307.05732](https://arxiv.org/abs/2307.05732).
- Dykstra vs POCS: [Dykstra's projection algorithm (Wikipedia)](https://en.wikipedia.org/wiki/Dykstra's_projection_algorithm); [POCS (Wikipedia)](https://en.wikipedia.org/wiki/Projections_onto_convex_sets); [Bauschke et al. — finite convergence/stalling, arXiv:2001.06747](https://arxiv.org/pdf/2001.06747).
- Dual coordinate / network-flow duality: [Bertsekas — Dual coordinate step methods for network flow](https://web.mit.edu/dimitrib/www/DualCoordinateStep.pdf); [Tseng — Dual ascent for strictly convex costs, SIAM](https://epubs.siam.org/doi/10.1137/0328011).
- OSQP: [Stellato et al. — arXiv:1711.08013](https://arxiv.org/abs/1711.08013); [Springer MPC version](https://link.springer.com/article/10.1007/s12532-020-00179-2).
- AMG / graph Laplacian: [PyAMG](https://github.com/pyamg/pyamg) and [PyAMG paper](http://lukeo.cs.illinois.edu/files/2023_PyAMG.pdf); [LAMG — arXiv:1108.1310](https://arxiv.org/pdf/1108.1310); [CMG — combinatorial multigrid](https://www.sciencedirect.com/science/article/abs/pii/S1077314211001627).
- Hub labeling / CH: [Abraham et al. — hub-based labeling](https://www.microsoft.com/en-us/research/wp-content/uploads/2010/12/HL-TR.pdf); [Li et al. — hub-labeling experimental study, VLDB](http://www.vldb.org/pvldb/vol11/p445-li.pdf); [RoutingKit](https://github.com/RoutingKit/RoutingKit).
- Parallel SSSP / eikonal: [Δ-stepping VDS variant, IEEE](https://ieeexplore.ieee.org/document/9915894/); [Jeong–Whitaker — Fast Iterative Method, SIAM](https://epubs.siam.org/doi/10.1137/060670298).

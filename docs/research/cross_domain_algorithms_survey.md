# Cross-domain algorithm survey — fast reach fields, constrained NN, and pairwise-constraint projection

Status: research report (read-only). 2026-07-17. Author: cross-domain scout agent.
Audience: the flat-airport fast-path work (docs/specs/flat-airport-fast-path-spec.md)
and the elevation solver owners.

This report hunts *other fields* for algorithmic machinery that maps onto three
kernels the airport terrain solver runs over 10^4–10^5 pavement nodes. It states
each mapping explicitly, then adversarially tests it against our hard constraints
(determinism; provably one-sided approximation only; Python / numpy / scipy /
shapely; Apple M-series, no GPU assumed). Most cross-domain analogies die on
inspection; the ones that survive carry a concrete transfer plan.

---

## 0. The three kernels, stated precisely (so the mappings are checkable)

Grounding reads: `elevation_per_surface/building_feasibility.py`
(`reach_band_unified`, `_build_skeleton_band`, `_nearest_visible_centerline`),
`route_profile/one_solve.py` (`feasibility_project`, `_reach`),
`solver_primitives._certify_flat_shape`, and the spec above.

**Kernel A — reach VALUE BANDS (a cap-Lipschitz field).**
Over the unified grade graph `G` we compute, from a set of hard anchors `a`
(runway-edge joins, seams) at fixed elevations `z_a`:

```
ceil_i  = min_a ( z_a + cap · geodist_G(a, i) )      # steepest-compliant reach down onto i
floor_i = max_a ( z_a − cap · geodist_G(a, i) )
```

`geodist_G` is the **on-pavement graph geodesic** (cap-weighted edge budgets),
*not* free-space Euclidean. Today this is a multi-source Dijkstra per field
(`_runway_value_field`, `_anchor_value_field`) — already O(E log V) and correct.
For **off-graph** query points (apron body samples, footprint samples) the band
is `(floor_nn − cap·off, ceil_nn + cap·off)` where `nn` = nearest graph node and
`off` = Euclidean offset to it (`_build_skeleton_band.band`, and the perp-climb
widening in `_band_via`). This is a min-plus / tropical field: `⊕ = min`,
`⊗ = +`.

**Kernel B — nearest-visible-feature queries constrained to pavement.**
`_nearest_visible_centerline(c, …)`: the nearest taxi centerline to `c` whose
straight chord to `c` stays ≥ 97 % on the pavement union. ~10^4 queries × ~500
lines; STRtree-ordered, batched-shapely accept test, endpoint-margin pruning.
Already heavily optimized; ~77 % of a KBNA build before the current vectorization.

**Kernel C — projection onto pairwise difference constraints.**
`feasibility_project`: drive every edge to `|z_i − z_j| ≤ b_ij` (b = cap·length),
holding `hard` nodes fixed, landing close to the DEM seed. Two pieces:
(C1) the exact **reach envelope** `_reach` (the min/max above, one-shot clamp),
then (C2) an iterative **Gauss-Seidel POCS** (or Jacobi) on the residual
free↔free edges. The measured pain (spec §1) is *building* the O(n²) within-shape
pair set (2.5 M pairs / ~108 s at KDFW) and iterating C2.

The existing **flatness certificate** (`_certify_flat_shape`) is the key prior
art every survivor builds on: if a whole soft shape's DEM gradient is
≤ `0.6·APRON_MAX_GRADE` (`FLATNESS_CERTIFICATE_RATE_FACTOR = 0.6`,
`APRON_MAX_GRADE = 0.01`) everywhere — estimated from ring vertices + a ~25 m
grid — then every body pair holds at the DEM seed and the O(n²) pairs need not
exist until a node drifts off seed. It is **all-or-nothing per shape** and its
sample spacing (25 m) is a hand-tuned constant.

---

## 1. SURVIVORS (ranked; see §3 for the build order)

### S1 — Well-Separated Pair Decomposition / dual-tree pruning  ▷ Kernel C construction (+ region certificate)

**Source field.** N-body astrophysics and statistical machine learning.
Callahan & Kosaraju's **Well-Separated Pair Decomposition** (WSPD, 1995) and the
**dual-tree** family (Gray & Moore; Curtin et al. "Tree-Independent Dual-Tree
Algorithms") that generalize Barnes–Hut/FMM far-field summarization to *all-pairs*
problems: recurse over **pairs of tree nodes** `(A, B)`; when a whole block can be
bounded, prune it — never enumerate the |A|·|B| leaf pairs.

**Explicit mapping.** Our within-shape constraint set is exactly an all-pairs
interaction: every pair `(i, j)` in a shape carries `|z_i − z_j| ≤ cap·d_ij`.
Build a kd-tree / ball-tree over each shape's nodes (scipy `cKDTree`). For a
tree-node pair `(A, B)` with DEM-seed ranges `[minA, maxA]`, `[minB, maxB]` and a
lower bound `D_lo` on the inter-cluster distance:

```
if  (maxA − minB)  ≤  cap · D_lo   and   (maxB − minA)  ≤  cap · D_lo   →  CERTIFY block (emit no edges)
```

Every pair inside a certified block provably satisfies its cap at the seed — this
is `_certify_flat_shape`'s Lipschitz test, but **per cluster-pair instead of per
whole shape**. Only blocks that fail get split; genuinely-tight pairs at the leaf
level are the only ones enumerated. This is the missing "Tier 2.5": it certifies
**partially-flat** shapes (most of a big apron) that the all-or-nothing shape
certificate refuses, and it is precisely the data structure Tier 3 (mixed-airport
narrowing) wants.

**Expected win.** Direct hit on the largest measured cost — within-shape pair
construction (108 s / 2.5 M pairs, spec §1). On flat regions it collapses O(n²) to
O(n) cluster-pairs; on rough regions the work scales with the count of
genuinely-near-cap pairs, not graph size. This is the single highest-leverage item.

**Implementation cost.** Medium (~1–2 wks). `cKDTree` build is in scipy;
the dual-traversal is a ~60-line recursion. Emit a certified block as a **lazy
super-entry** (one `lazy_expand` thunk + a movement watch on `min/max` of each
cluster) so it plugs straight into the existing lazy machinery in
`feasibility_project` (the block expands to real edges the moment a member drifts
off seed — same soundness invariant as today's per-shape lazy entries).

**Soundness.** One-sided sound: widen conservatively — inflate each cluster's DEM
range by its covering radius and use a *lower* bound `D_lo` on separation (kd-tree
node distance is exactly such a bound). If the widened test passes, every leaf
pair passes. A block never wrongly suppresses a real constraint; at worst it fails
to certify and enumerates (falls back to today's behavior). Determinism: split in
sorted index / axis order; `cKDTree` build is deterministic.

**Risks.** (i) The DEM-range-over-cluster bound must be a true bound on the
*seed* values the solver uses — reuse `elevation._sample_dem` through the same
frame (spec §2.2), and drive sample density from S5. (ii) Integration point is
the constraint *builder*, not `feasibility_project`'s projection loop, so it does
**not** touch the drive-to-zero files (spec §3.4 constraint respected).

Citations: Callahan & Kosaraju (JACM 1995); Gray & Moore, "N-body problems in
statistical learning" (NeurIPS 2000) and "Nonparametric density estimation:
toward computational tractability" (2003); Curtin et al., "Tree-Independent
Dual-Tree Algorithms" (ICML 2013, arXiv:1304.4327); Ram et al.,
"Linear-time algorithms for pairwise statistical problems" (NeurIPS 2009).
Implementations: `scipy.spatial.cKDTree`; mlpack dual-tree framework (reference
only, not a dependency).

---

### S2 — Safety certificates / lazy collision checking  ▷ Kernel C invalidation (refines Tier 0/1)

**Source field.** Sampling-based motion planning. Bialkowski, Otte, Karaman &
Frazzoli, "Efficient collision checking in sampling-based motion planning via
safety certificates" (IJRR 2016): when a configuration is collision-checked, store
a **lower bound on its distance to the nearest obstacle** — a certified free ball.
A new sample landing inside an existing ball skips the expensive check; the
membership test rides the nearest-neighbor search already present. Related: Lazy
PRM (Bohlin & Kavraki), LazySP, GLS — defer expensive edge evaluation until forced.

**Explicit mapping.** Our `lazy_move_tolerance` *is* a safety certificate: a
certified shape holds while its nodes stay near seed. The motion-planning version
sharpens it in two ways we don't yet exploit:
1. **Maximal per-node radius.** Store each certified node's *own* slack ball
   `r_i = (budget_i − relief_i)/cap` (how far it may move before *its* tightest
   pair reaches cap), rather than one shape-wide scalar tolerance. Nodes near a
   tight route get small balls (expand early, correctly); flat-interior nodes get
   large balls (rarely expand). Strictly better invalidation than the current
   single `lazy_move_tolerance` per entry.
2. **Certificate set as a queryable structure.** Answer "did this move invalidate
   anything?" by NN into the certificate set — which S1 already builds.

**Expected win.** Fewer spurious mid-solve expansions (each expansion regenerates
a full pair set — expensive). Compounding rather than headline, and it *stabilizes*
S1 (keeps certified blocks certified longer).

**Implementation cost.** Low (days). It is a refinement of the existing
`lazy_move_tolerance` in `_certify_flat_shape` / `feasibility_project` from a
per-entry scalar to a per-node slack radius.

**Soundness.** Exact / one-sided: a ball certified in-grade stays in-grade until a
node exits it; exiting triggers expansion. No determinism issue.

**Risks.** Low. Mostly validates the current lazy design (the owner asked for the
comparison — it is favorable) and yields one concrete tightening.

Citations: Bialkowski, Otte, Karaman, Frazzoli, IJRR 35(7) 2016 (SAGE); Bohlin &
Kavraki, "Path planning using Lazy PRM" (ICRA 2000); Mandalika et al.,
"Generalized Lazy Search" (ICAPS 2019, arXiv:1904.02795).

---

### S3 — Generalized distance transform / fast marching  ▷ Kernel A off-graph field (+ a correctness fix)

**Source field.** Computer vision + seismology. Felzenszwalb & Huttenlocher,
"Distance Transforms of Sampled Functions" (Theory of Computing 2012): compute
`D(p) = min_q ( f(q) + ρ(p, q) )` for a seeded cost `f` over a grid in **O(n)** via
two separable **lower-envelope** passes (parabolas for squared-Euclidean; a
linear-cone variant for L1/L∞). This is a min-plus convolution — exactly Kernel A's
algebra. Sibling: the **Fast Marching Method** (Sethian) / Fast Sweeping, which
solve the Eikonal equation `|∇u| = cap` — whose viscosity solution *is* the
cap-Lipschitz geodesic field — on a grid in O(N log N), and handle masked domains
(obstacles / off-pavement) natively.

**Explicit mapping.** The off-graph widening step evaluates, at every one of ~10^4–
10^5 sample points, a min-plus lower envelope of cones seeded at graph nodes:
`ceil(p) = min_i ( ceil_i + cap·|p − p_i| )`. A single FH generalized distance
transform over a raster at the widening resolution computes this for **all** grid
cells in two linear passes, replacing per-point `STRtree.nearest` + widen.

**Bonus — a latent correctness fix.** The current code widens from the **nearest
node only** (`_build_skeleton_band.band`, `_band_via` uses the nearest centerline's
foot). For a *ceiling* (a `min`), nearest-node returns a value ≥ the true
min-over-all-nodes envelope → it can report a ceiling that is too **high** (less
conservative). FH-GDT computes the exact min-over-all envelope at the same or lower
cost — a strict tightening of the band toward correctness. (Symmetrically the floor
becomes exact.) This must be gated (not byte-identical) but it removes an unproven
approximation.

**Expected win.** Replaces ~10^4–10^5 nearest+widen calls with a couple of linear
grid passes, and upgrades the nearest-node approximation to the exact envelope.
Medium speed win on band sampling + a correctness improvement.

**Implementation cost.** Medium (~1 wk). FH lower-envelope is ~40 well-documented
lines (public reference: cs.brown.edu/people/pfelzens/dt). `scipy.ndimage`
provides only the *unseeded* EDT, so the seeded/generalized pass is hand-written.
Need a rasterization at a resolution tied to `cap` and the tile frame.

**Soundness.** FH-GDT is **exact** for the Euclidean-widening envelope; one-sided
if grid quantization is charged conservatively (snap a query to its cell's
worst-corner offset, add `½·cell·cap` to the widening). Deterministic grid.
**Fast Marching on a rasterized pavement mask is NOT recommended for the on-graph
geodesic** — it approximates the geodesic with grid error that is not cleanly
one-sided, and the existing multi-source Dijkstra already computes that field
exactly and fast. Use FH-GDT for the *Euclidean off-graph widening* only; leave
the on-pavement geodesic to Dijkstra.

**Risks.** Grid resolution vs cap must be bounded one-sided (above). If the
widening resolution is coarse relative to footprint detail the win shrinks;
profile before committing.

Citations: Felzenszwalb & Huttenlocher, Theory of Computing 8 (2012) 415–428;
Sethian, "Fast Marching Methods" (SIAM Review 1999); Tsitsiklis (1995) for the
Eikonal/Dijkstra correspondence. Reference code: taiya/dtform, giorgiomarcias/
distance_transform (read-only, for the lower-envelope pass).

---

### S4 — VLSI constraint-graph compaction / L∞ isotonic regression  ▷ Kernel C projection on 1-D & tree substructures

**Source field.** EDA layout compaction and shape-constrained statistics. Layout
compaction legalizes a design under difference constraints `x_j − x_i ≥ d_ij` by
**longest/shortest-path** passes on the constraint graph (Liao–Wong, Bellman–Ford),
not iteration — a legal input solves in Θ(m + n log n). The projection analogue is
**L∞ isotonic regression** (Stout 2015, arXiv:1507.02226): the closest feasible
assignment under a *linear or tree order* in the L∞ metric in **Θ(n)** via a
"rendezvous graph with bounded error envelopes at each vertex" — strikingly close
to the reach-envelope-then-clamp we already run.

**Explicit mapping.** Kernel C already uses the right machinery for the *extreme*
surfaces: `_reach` (min/max over anchors) is exactly the shortest-path solution of
a difference-constraint system (CLRS §24.4). The gap is the *closest-to-DEM*
projection (C2), currently thousands of Gauss-Seidel sweeps. Our **spine chains**
and **rect axial couples** are *linear/tree* orders — on those substructures, L∞
isotonic regression gives the exact closest-feasible assignment in **one linear
pass**, no iteration. Use it (a) to solve spine/rect profiles exactly, and (b) as a
warm start for the residual 2-D POCS, cutting sweep counts.

**Expected win.** Exact one-pass projection on the 1-D/tree parts + a better warm
start for the 2-D remainder → fewer POCS sweeps. Medium; upside is bounded because
the general 2-D graph is not a tree (POCS stays for the genuinely 2-D residual).

**Implementation cost.** Medium (~1 wk). Implement the linear-order L∞ projection
(a monotone forward/backward envelope sweep); wire it as a warm-start feeding
`feasibility_project`. It sits *before* the projection, so — like S1 — it need not
edit the drive-to-zero projection internals.

**Soundness.** Exact projection (feasible and L∞-optimal on the substructure); not
an approximation. Deterministic (envelope sweep in index order).

**Risks.** Benefits only the tree/linear substructure; the 2-D core cost may
dominate. Measure the spine/rect share of C2 first — if small, deprioritize.

Citations: Stout, "L∞ isotonic regression for linear, multidimensional, and tree
orders" (arXiv:1507.02226); Liao & Wong (1983) constraint-graph compaction; CLRS
§24.4 "systems of difference constraints"; Y.-W. Chang EDA lecture 4 (layout
compaction).

---

### S5 — Minimizers / covering-radius sampling  ▷ makes S1 and the seat/shape certificates provably one-sided

**Source field.** Genomic sketching. Minimizers and syncmers deterministically
subsample k-mers with a **window guarantee**: no gap longer than `w` lacks a
sample — the 1-D form of a covering-radius guarantee.

**Explicit mapping.** The current certificates sample by *convention*:
`_certify_flat_shape` uses ring vertices + a ~25 m grid; the seat certificate
(`_footprint_dem_relief`) uses ring vertices + centroid — with **no guarantee about
the interior between samples** (a spike between two ring vertices is invisible). The
covering-radius framing makes the density *derivable*: to certify a region flat
one-sided, samples must have covering radius `r` with `cap·r ≤ remaining_slack`, so
any unsampled point differs from a sample by ≤ `cap·r ≤ slack` — provably in
tolerance. Required spacing = `min(DEM_cell_size, slack/cap)`. This replaces the
magic 25 m and centroid-only footprint sampling with a proven bound, and it is the
sampling primitive S1's cluster-range bounds depend on.

**Expected win.** Not a speed win — a **soundness enabler**. It closes a latent gap
(interior spikes) in the existing certificates and lets S1 certify larger blocks
safely. Slightly more DEM samples, cheap against the pair construction it protects.

**Implementation cost.** Low (days). Deterministic grid / Poisson-disk at spacing
`2r`; reuse `elevation._sample_dem`.

**Soundness.** Turns a heuristic into a provable one-sided certificate. Deterministic.

**Risks.** Low. If `slack/cap` is smaller than the DEM cell, the DEM's own
resolution bounds relief and the sample count stays modest.

Citations: Roberts et al. (2004) minimizers; Edgar (2021) syncmers; Ndiaye et al.,
"When less is more: sketching with minimizers in genomics" (2024, PMID 39402664);
the window-guarantee analysis in "Creating and Using Minimizer Sketches in
Computational Genomics" (J. Comput. Biol. 2023).

---

### S6 — Banded alignment / X-drop  ▷ Kernel C, Tier-3 corridor narrowing (deferred, when Tier 3 lands)

**Source field.** Long-read genomics. Banded Smith–Waterman computes only a
diagonal band of the DP matrix; **X-drop** stops extending a band when the score
falls a threshold below the best.

**Explicit mapping.** Tier 3 (spec §3.4) narrows expensive stages to the
uncertified remainder at mixed airports. Banded DP gives the discipline: build /
solve constraints only within a **corridor** around uncertified regions; grow the
band adaptively while over-cap residual exceeds threshold; stop (X-drop) when the
field is in-grade. S1's tree already *identifies* the uncertified blocks; banding is
how you bound the work of repairing them without touching the certified interior.

**Expected win.** Bounds Tier-3 work to terrain-difficulty rather than airport size.
**Implementation cost / status.** Medium, but Tier 3 is explicitly deferred and
coordinated with the drive-to-zero session — this is a "when Tier 3 opens" note, not
a WP1/WP2 item.
**Soundness.** One-sided if the band is grown until no over-cap edge crosses its
boundary (a certified boundary is a proof the interior is isolated). Deterministic.
Citations: Chao, Pearson, Miller (1992) banded alignment; Zhang et al. (2000)
X-drop; minimap2 (Li 2018) for the modern practical form.

---

## 2. SEDUCTIVE ANALOGIES THAT DIED (negative knowledge)

- **Fast Multipole Method — the multipole *expansion* math.** FMM summarizes a far
  cluster by a truncated analytic series with a provable truncation error. That
  requires an **additive, analytically separable** kernel (Σ q/|x−y|). Our kernel is
  **min-plus** (`min_a z_a + cap·d`) — there is no additive series for a `min`, and a
  targeted search found **no literature on a tropical/min-plus FMM**. What *does*
  transfer is far-field **cluster summarization** — but that machinery is WSPD /
  dual-tree (S1), not the expansion. Keep the distinction sharp: we get Barnes–Hut's
  *pruning*, never its *multipole moments*.

- **Friends-of-Friends halo finding.** Union-find at a fixed linking length =
  connected components by proximity. Our connectivity is already the pavement graph;
  we do not need to *discover* clusters by distance. Adds nothing.

- **FM-index / suffix automata (heavy preprocess, light query).** Amortize a costly
  index over *many* queries against a *fixed* text. We rebuild geometry per airport
  per build and query it a bounded number of times — no amortization horizon. (The
  STRtree already is the right light-preprocess structure and is used.)

- **Pangenome graph routing (vg toolkit).** Aligns *sequences to* a graph
  (graph-alignment), not shortest-path fields or difference-constraint projection.
  Wrong problem shape.

- **FPGA PathFinder negotiated congestion.** Models **resource contention** — many
  nets competing for scarce wires — via iteratively rising congestion prices. Our
  nodes do not compete for a shared capacity resource; there is no congestion to
  negotiate. The mechanism has nothing to bite on.

- **Rectilinear Steiner minimal trees.** Constructs interconnect trees; not a field
  or a projection. Not our problem.

- **HPA\* hierarchical pathfinding / PRM roadmap reuse.** Both amortize *many*
  point-to-point queries via a precomputed abstraction/roadmap. We run **one**
  multi-source pass — there is no query stream to amortize. PRM is also randomized
  (violates determinism). The clustering idea survives, but only inside S1.

- **Level-set / fluid front methods.** Grid PDE evolution — approximate and slower
  than the exact discrete multi-source Dijkstra we already run, with the same grid
  one-sidedness worry as fast marching and none of S3's upside.

- **minimap2 concave-gap chaining (SMAWK / Larmore–Schieber).** A near-linear
  speedup for a 1-D DP with concave gap costs. Our spine 1-D profile is already
  cheap; this optimizes a DP that is not the bottleneck. Filed for later *only if*
  the spine profile ever dominates a profile.

---

## 3. Ranked shortlist and what I would build first

| Rank | Item | Kernel | Win | Cost | Soundness |
|---|---|---|---|---|---|
| 1 | **S1 WSPD / dual-tree pruning** | C construction | Attacks the 108 s / 2.5 M-pair cost; generalizes the shape certificate to partial regions | Med (1–2 wk) | One-sided sound |
| 2 | **S5 covering-radius sampling** | certificate soundness | Makes S1 + seat/shape certs provably one-sided; kills the 25 m magic number | Low (days) | Enabler; provable |
| 3 | **S2 safety-certificate balls** | C invalidation | Fewer spurious re-expansions; stabilizes S1 | Low (days) | Exact/one-sided |
| 4 | **S3 FH generalized distance transform** | A off-graph field | Linear-pass band sampling + fixes nearest-node ceiling approximation | Med (1 wk) | Exact, gated |
| 5 | **S4 L∞ isotonic / compaction warm start** | C projection | Exact 1-pass spine/rect projection + fewer POCS sweeps | Med (1 wk) | Exact |
| 6 | **S6 banded / X-drop** | C Tier-3 | Bounds mixed-airport repair to terrain difficulty | Med (deferred) | One-sided |

**What I would build first: the S1 + S5 + S2 trio.** They are one coherent
package and they land inside spec Tiers 1–2 **without touching the drive-to-zero
projection files** (§3.4):

1. **S5 first** (days): a covering-radius sampler over `elevation._sample_dem` that,
   given a region and its slack, returns a sample set whose covering radius proves
   `cap·r ≤ slack`. This retires the 25 m constant and the centroid-only footprint
   sampling with a bound, and is the primitive S1 needs.
2. **S1 next** (1–2 wk): a `cKDTree` per soft shape + a dual-traversal that certifies
   well-separated cluster-pairs (Lipschitz block test on S5-sampled seed ranges) and
   emits certified blocks as lazy super-entries into `feasibility_project`'s existing
   lazy machinery. This is the general form of `_certify_flat_shape` — it certifies
   the *flat parts of rough shapes*, which is exactly the mixed-airport coverage the
   whole-shape certificate cannot reach, and it directly cuts the dominant
   construction cost.
3. **S2 alongside** (days): give each certified node its own slack ball so mid-solve
   moves invalidate the *minimum* set of blocks, keeping S1's certified blocks
   certified as long as they provably hold.

S3 and S4 are strong second-wave items (a correctness-improving band field and an
exact projection warm start) but each changes numeric output and so wants its own
A/B gate; sequence them after the S1/S5/S2 trio proves out on the OTHH / KDFW
profiles the spec calls for.

The deepest cross-domain insight, stated plainly: **our reach field is a min-plus
(tropical) Lipschitz field, and our constraint set is an all-pairs interaction.**
Astrophysics' additive far-field *expansions* do not transfer (no tropical
multipole exists), but its far-field *pruning* — WSPD / dual-tree — transfers
cleanly and is the same idea the flatness certificate already gropes toward, just
hierarchical and provably sound. That is the machine worth borrowing.

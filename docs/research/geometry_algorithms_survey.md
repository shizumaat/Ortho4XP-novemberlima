# Geometry / GIS algorithm survey — accelerating the reach-band kernels

Status: research report, 2026-07-17. No code changed. Author: research agent.
Scope: literature + library survey for KERNEL A (reach bands) and KERNEL D
(nearest-visible-feature), grounded in
`src/auto_patch/elevation_per_surface/building_feasibility.py` and
`.../route_profile/anchors.py`.

---

## 0. What the two kernels actually compute (restated precisely)

Read the code before the survey: the mathematical object matters more than the
paper titles.

**KERNEL A — reach band.** For a query point `x` the band is

```
ceiling(x) = min over anchors a ( value_a + cap · d_geo(x, a) )
floor(x)   = max over anchors a ( value_a − cap · d_geo(x, a) )
```

where `d_geo` is distance **inside the pavement polygon-with-holes** and `cap`
is the per-route Lipschitz slope (per-letter taxi cap / apron 1 %). This is the
**lower/upper envelope of cones** of slope `cap` seeded at each anchor — an
*additively-weighted geodesic distance field*. The result field is `cap`-
Lipschitz by construction (`|∇u| ≤ cap`).

The current implementation does **not** compute a true polygon geodesic. It
approximates `d_geo` by:
- a multi-source Dijkstra over the centerline **graph** `G.spine_adj`
  (`_runway_value_field`) — this already gives `min_a(value_a + cap·d_graph)`
  and `max_a(...)` as two settled fields, exactly the cone envelope on the
  graph;
- plus a per-query *straight* perpendicular foot + along-centerline arc
  (`_band_via`), gated by a visibility test so the foot chord must stay on
  pavement.

So the expensive part is **not** the field solve (already a 2-pass multi-source
Dijkstra) — it is the **per-query geometry**: for each of 10⁴–10⁵ points, find
the serving centerline whose connecting chord stays on pavement
(`_nearest_visible_centerline`, ~77 % of a KBNA build per the docstring), and
for off-net "zone" points the skeleton `_fallback` (~74 ms/point × 45 k points).

**KERNEL D — nearest visible feature.** `_nearest_visible_centerline` +
`_chord_on_pavement`: for each point, the nearest taxi-centerline reachable
**without leaving the pavement polygon**. This is a *geodesic* (not Euclidean)
nearest-site query with a visibility gate, answered today point-by-point with an
exact-point-only cache.

**The soundness contract.** A replacement is admissible iff it is byte-identical
to the current computation, **or** conservatively one-sided: `floor` may only
move **down**, `ceiling` only **up** (a looser band never introduces a new false
grade-violation flag). Anything that tightens the band must be exact.

**The soundness lever that unlocks everything.** Because
`ceiling = value + cap·d` and `floor = value − cap·d`, **overestimating the
geodesic distance `d` moves ceiling up and floor down simultaneously — the exact
conservative direction.** Therefore any distance surrogate that *provably never
underestimates* the true in-pavement geodesic distance yields a conservative
band. This is why the current graph-Dijkstra (a path length, hence ≥ true
geodesic) is already sound, and it is the crack through which a raster method
walks in.

---

## 1. Survivors (ranked) — detail

### S1 — Rasterized masked multi-source weighted distance field (chamfer / eikonal), one field, O(1) queries. **[recommended]**

**What it is.** Rasterize the pavement-with-holes at 1–2 m into a boolean mask.
Run **one** multi-source shortest-path/eikonal pass over the masked grid to
produce three cell fields: `ceiling`, `floor`, and `nearest_centerline_label`.
Then every query (KERNEL A and D) is an O(1) grid lookup (+ bilinear /
nearest-cell read). The field solve is O(N_cells · log N_cells) once; at 1 m a
large airport pavement is on the order of a few ×10⁶–10⁷ cells → seconds, not
minutes, and it is paid **once** instead of 10⁵ times.

**Mapping onto the kernels.**
- *KERNEL A.* Seed a grid Dijkstra with **nonzero initial cost per anchor cell**:
  seed cell of anchor `a` at `+value_a`, edge weight `cap · (cell Euclidean step
  length)`; the settled cost field is exactly `min_a(value_a + cap·d_grid)` =
  the ceiling. For the floor, seed at `−value_a`, settle the same min, negate:
  `floor = −min_a(−value_a + cap·d_grid) = max_a(value_a − cap·d_grid)`. Two
  passes total. The visibility gate is **free**: distance only propagates
  through paved cells, so a point only "sees" anchors reachable on pavement — the
  chord-on-pavement test disappears into the mask.
- *KERNEL D.* A third multi-source pass seeded from every centerline cell,
  carrying the source label; the settled label = nearest centerline **reachable
  on pavement** = exactly `_nearest_visible_centerline`. `MCP_Geometric.traceback`
  or a label-propagating Dijkstra gives it directly.
- *The off-net "zone" tail and the skeleton `_fallback` both vanish*: a
  grass/zone vertex just reads the same grid (or reads `None` where the pavement
  component doesn't reach it), at the same O(1) cost as an on-net point. This
  kills the 74 ms/point × 45 k-point wall outright.

**Soundness classification: one-sided-conservative, with a provable and
tunable bound.** A grid path's weighted length with standard geometric weights
(axial = cell size, diagonal = √2·cell size, i.e. `MCP_Geometric`'s
"cost of a unit distance of travel") is **≥** the true continuous
in-mask geodesic distance (any staircase path is a valid path; its Euclidean
length ≥ the geodesic; the geometric chamfer weight equals that staircase's
Euclidean length). Hence `d_grid ≥ d_geo` ⇒ ceiling only rises, floor only
falls ⇒ **conservative**. Two digitization caveats, both handled conservatively:
  1. *Metric over-estimation is bounded* (8-connected staircase over-estimates a
     straight Euclidean segment by ≤ ~8 % at 22.5°; a fuller 16-neighbour
     Knight's-move chamfer mask cuts this to <1 %). Over-estimation is the
     *safe* direction here, so it needs no correction — only a note that the
     band is slightly loose, tightenable by richer neighbourhoods if the
     looseness ever matters.
  2. *Boundary rasterization.* Rasterize the pavement **conservatively** — mark a
     cell paved only if it is (nearly) fully inside the union (an erosion by ~½
     cell), so the discrete domain ⊆ the true domain and discrete geodesics ≥
     true geodesics. Thin corridors (< ~1 cell) must not be eroded shut; guard by
     rasterizing at a resolution below the narrowest real corridor (taxiway ≥ 15 m
     ≫ 1–2 m cell) and by snapping each anchor to the nearest paved cell.
  The one place exactness is *not* one-sided is `skfmm` (fast marching, below):
  it targets the true continuous solution and its truncation error is two-sided,
  so it would need an explicit `+ε` inflation to be conservative — prefer the
  chamfer/Dijkstra formulation, which is one-sided by construction.

**Library availability (Apple M-series, pure-Python-orchestrated, no GPU).**
  - **`scipy.sparse.csgraph.dijkstra`** — *already a dependency* (scipy 1.17.1 in
    `requirements.txt`). Build the grid as a sparse adjacency (8- or 16-neighbour,
    masked), add a **virtual super-source** node connected to anchor cell `a`
    with edge weight `value_a` (ceiling) / `−value_a` handled by offset; one
    `dijkstra` call gives the whole additively-weighted field, and
    `return_predecessors=True` gives the nearest-source label for KERNEL D. This
    is the exact additively-weighted cone envelope on the grid graph — **no new
    dependency**.
  - **`scikit-fmm`** — *already a dependency* (`scikit-fmm==2025.6.23`).
    `skfmm.distance`/`skfmm.travel_time` accept **masked arrays** (obstacles) and
    multi-source zero-contours; Cartesian-grid only (fine — the pavement is a 2D
    field). Accurate but **two-sided error** → only use with an explicit
    conservative inflation, and it does not natively carry per-source initial
    values (KERNEL A's additive seeding) — the csgraph super-source route is
    cleaner for A. Good fallback / cross-check.
  - **`scikit-image` `graph.MCP_Geometric`** — the most ergonomic single call:
    `find_costs(starts)` with **multiple starts** returns the geometric (chamfer)
    cumulative-cost field, and `traceback()` reconstructs the path **to the
    nearest start** (KERNEL D's label for free). Uses proper diagonal geometry.
    *Caveat:* not currently a dependency — adding scikit-image pulls the
    installer/onboarding/wheels chore (see MEMORY: "new dep ⇒ requirements +
    installers + ONBOARDING + wheels, same change"). It does **not** expose
    per-start initial costs, so KERNEL A's additive seeding still wants the
    csgraph super-source. Net: attractive but the csgraph path avoids a new dep.

**Expected win at our sizes.** Field build: one Dijkstra over ~10⁶–10⁷ masked
cells ≈ single-digit seconds (Cython/C inner loop in scipy/skimage). Queries:
10⁵ × O(1) grid reads ≈ sub-second. Versus ~700 s inclusive today ⇒ **plausibly
100×+ on the band kernels**, and it deletes the skeleton-fallback and
zone-tail costs entirely. Memory: three float/int fields at 1 m over a ~5 km ×
5 km airport ≈ 25 M cells × ~12 B ≈ 300 MB — inside the few-GB budget; a
2 m grid quarters it.

**Implementation cost: ~3–5 days.** Rasterizer (shapely → mask via
`rasterio.features.rasterize` or a numpy polygon fill; note rasterio may be a new
dep — a pure-numpy/`matplotlib.path`-free fill or `shapely.contains_xy` on the
grid centres avoids it), grid-graph builder + super-source seeding, the two
value fields + one label field, a `band(x,y)` shim that reads the grid and
reproduces the exact float expression at the sub-cell level (bilinear on the
value fields is itself Lipschitz-safe). The hard part is the **conservative
rasterization guard** and an A/B harness proving one-sidedness on SPJC / CYXY /
HECA fixtures.

**Risks.** (a) Boundary digitization on thin/curved corridors — mitigate with
resolution ≤ corridor width and anchor-snap; (b) determinism — grid Dijkstra
tie-breaks must be index-ordered (`sorted` seeds; scipy csgraph is
deterministic given a fixed sparse structure), no hash/parallel nondeterminism;
(c) the band today also credits a *per-segment along-centerline cap that varies
by ICAO letter* — the raster metric must vary `cap` per cell (paint the local
cap into the edge-weight field from the owning centerline's letter) to stay
faithful, else it is only conservative if it uses the **largest** applicable cap
(safe but loose). (d) exact float reproduction of the current band is *not*
possible (different metric), so this lands as the **conservative** variant, not
byte-identical — acceptance is the one-sided proof + not-worse `check_grade`
counts, per the flat-airport spec's acceptance model.

Links: scipy csgraph
<https://docs.scipy.org/doc/scipy/reference/sparse.csgraph.html>;
scikit-fmm <https://scikit-fmm.readthedocs.io/>,
<https://github.com/scikit-fmm/scikit-fmm>;
scikit-image MCP
<https://scikit-image.org/docs/stable/api/skimage.graph.html>;
Felzenszwalb–Huttenlocher (exact separable DT, background)
<https://cs.brown.edu/people/pfelzens/papers/dt-final.pdf>.

---

### S2 — Exact any-angle path queries on a navigation mesh (Polyanya) for KERNEL D and on-demand A. **[strong #2 / exactness path]**

**What it is.** Triangulate/merge the pavement-with-holes into a **navigation
mesh** of convex polygons; Polyanya (Cui, Harabor, Grastien, IJCAI 2017) answers
**exact Euclidean shortest-path** queries between two points online, optimal and
fast, via interval expansion across mesh edges. In our domain the pavement
shortest path *is* the geodesic, so Polyanya's path length is the **exact**
`d_geo` — no digitization error.

**Mapping.**
- *KERNEL D.* Nearest visible centerline = nearest site under the geodesic
  metric; run Polyanya from the query to candidate centerline points (STRtree-
  pruned) and take the min — exact, and "visible on pavement" is automatic
  because the mesh **is** the pavement (a path exists iff it stays on pavement).
- *KERNEL A.* Per query, the exact geodesic to each anchor gives the exact cone
  envelope — replacing both the graph-Dijkstra field *and* the straight-perp
  approximation with one exact per-query solve. This is the route to a
  **byte-tightenable (exact)** band rather than a conservative one.

**Soundness: exact** (continuous Euclidean shortest path on the polygonal
domain). It can therefore legitimately *tighten* the band.

**Library availability.** Reference C++ (`https://bitbucket.org/dharabor/pathfinding`
lineage) and a maintained **Rust** crate `polyanya` (`https://docs.rs/polyanya`).
**No maintained, pip-installable Python binding** — this is the gating weakness.
Options: PyO3/pybind wrapper (days), or reimplement the interval search in
Cython/numba (weeks). The mesh build wants a constrained Delaunay triangulation
(`triangle` / `PythonCDT`, both pip-installable) plus convex merging.

**Expected win.** Per-query O(mesh path complexity) — for a compact airport mesh
(10³–10⁴ polys) typically microseconds–low-milliseconds, but it is **still
per-query**, so at 10⁵ queries this is competitive with, not obviously better
than, S1's O(1) grid reads. Its real value is **exactness** (tightening) and
handling curved/complex boundaries without digitization. Preprocessing: mesh
build seconds; no per-source field, so unlike S1 it does not amortize the field
across all queries in one pass.

**Implementation cost: ~7–12 days** (binding/port + CDT mesh build + merge +
STRtree candidate pruning + determinism audit). Higher than S1.

**Risks.** No blessed Python binding (build-and-maintain burden, a real cost
given the fork-minimization ruling); per-query cost doesn't collapse the way a
one-pass raster field does; mesh robustness on degenerate weld-seam slivers
(the same GEOS non-noded-intersection failure the code already patches with
`buffer(0)`).

Links: Polyanya paper <https://www.ijcai.org/proceedings/2017/0070.pdf>;
Rust crate <https://docs.rs/polyanya>;
`triangle` <https://rufat.be/triangle/>; PythonCDT
<https://github.com/artem-ogre/PythonCDT>.

---

### S3 — Heat method on a 2-D constrained-Delaunay triangulation (potpourri3d / geometry-central) for the field + nearest-label. **[#3, conditional]**

**What it is.** Triangulate the pavement-with-holes (a flat 2-D mesh — holes are
just interior boundary loops; the heat method is indifferent to embedding).
`potpourri3d.MeshHeatMethodDistanceSolver.compute_distance_multisource(sources)`
solves two sparse linear systems (heat diffusion, then a Poisson solve on the
normalized gradient) to get **distance to the nearest source** across the whole
mesh in near-linear time; after one Cholesky factorization, **repeated solves
are cheap** (Crane–Weischedel–Wardetzky 2013). `MeshVectorHeatSolver.extend_scalar`
propagates a **per-source scalar** to its nearest-geodesic-neighbour region —
usable to carry an anchor's value or a centerline label.

**Mapping.**
- *KERNEL A.* `compute_distance_multisource` gives `d_geo(x, nearest anchor)` as
  a vertex field; but the band needs `min_a(value_a + cap·d_a)` (additively
  weighted, *per-anchor*, not just nearest) — the vector-heat `extend_scalar`
  gives the nearest source's value, which combined with the distance field
  reconstructs the *nearest-anchor* cone but **not** the full lower envelope when
  a farther-but-lower anchor wins. Faithful reproduction needs either per-anchor
  solves (too many) or accepting the nearest-anchor approximation (unsound unless
  bounded). This is a genuine mismatch with the additive structure.
- *KERNEL D.* `extend_scalar` with centerline-id sources → nearest-centerline
  label field; a good fit for D specifically.

**Soundness: approximate, two-sided** (heat method converges to exact only as
mesh → 0 and t → 0; smoothed distances are deliberately regularized). It does
**not** give a one-sided guarantee, so under our contract it is
**unsuitable for the value fields without an added, hard-to-certify error
bound**. Its natural home is KERNEL D's *label* (a discrete nearest-site
assignment, where small distance error rarely flips the label) rather than the
metric that feeds the band.

**Library availability.** `potpourri3d` (pip, actively maintained by
N. Sharp, C++ geometry-central bindings). 2-D flat meshes work (pass z=0);
`compute_distance_multisource` and `extend_scalar` are exactly the entry points.
Would be a **new dependency** (with its build/wheel chore).

**Expected win.** Field solve near-linear + cached factorization → fast repeated
solves; but the additive-envelope mismatch means it doesn't cleanly answer
KERNEL A, and for KERNEL D it competes with S1's label field without S1's
one-sidedness or its zero new deps.

**Implementation cost: ~4–6 days** (CDT mesh + solver wiring + label plumbing),
but **blocked on the soundness gap** for the value fields.

**Risks.** Not one-sided (fails the contract for A); new dependency; mesh
quality sensitivity; the additive-weight structure is not what the heat method
natively computes.

Links: paper <https://www.cs.cmu.edu/~kmcrane/Projects/HeatMethod/paper.pdf>;
potpourri3d <https://github.com/nmwsharp/potpourri3d>;
geometry-central heat solver
<https://geometry-central.net/surface/algorithms/geodesic_distance/>.

---

### S4 — Exact polyhedral geodesic (MMP / "continuous Dijkstra") from anchors, single-source-all-destinations. **[#4, exact seeding oracle]**

**What it is.** MMP (Mitchell–Mount–Papadimitriou) / CH exact geodesic on a
triangulated pavement: propagates distance **windows** along edges to give the
exact geodesic distance from a source to *all* mesh points, O(n² log n) worst
case but near-linear in practice. CGAL `Surface_mesh_shortest_path` and
geometry-central (Kirsanov MMP) implement it; `gdist`/`pygeodesic` wrap MMP for
Python.

**Mapping.** An **exact** distance field from each anchor — the exact version of
S1's per-anchor cone. Useful as a *reference oracle* to certify the conservative
raster band, or to seed a small number of anchors exactly. For 10²–10³ anchors
it is one MMP per anchor (expensive) unless the additive envelope is folded into
a single multi-source window propagation (a modest extension of the algorithm,
not in off-the-shelf libs).

**Soundness: exact.** Can tighten.

**Library availability.** `pygeodesic` / `gdist` (pip, MMP, single-source),
CGAL (C++, needs binding). No off-the-shelf *additively-weighted multi-source*
MMP.

**Expected win.** Not a throughput win at 10⁵ queries (it is a field-per-source
oracle); its value is **exactness for validation** and for the handful of
hardest anchors. Best used to *audit* S1, not to replace it.

**Implementation cost: ~2–3 days** as a validation oracle (`pygeodesic` wrap on
a CDT mesh); a production multi-source additive MMP is weeks (research-grade).

**Risks.** Cost scales with #anchors; window propagation robustness; overkill for
the throughput goal.

Links: Surazhsky et al. "Fast exact and approximate geodesics on meshes"
<https://www.cs.harvard.edu/~sjg/papers/geod.pdf>; CGAL
<https://doc.cgal.org/latest/Surface_mesh_shortest_path/index.html>;
pygeodesic <https://github.com/mhogg/pygeodesic>.

---

### S5 — Batched/vectorized shapely for the chord-gate and candidate scan. **[cheap immediate win, complementary]**

**What it is.** Not an algorithmic change — the shapely 2.x vectorized surface
already exploited in the current code (`contains_xy`, `shortest_line`,
`get_coordinates`, `STRtree.query` with `predicate=`). The remaining wins:
(a) hoist the per-query `_nearest_visible_centerline` into a **single batched
pass over all query points** (concatenate every query's candidate chords into
one `contains_xy` / `STRtree.query` call rather than per-point growing chunks);
(b) use `STRtree.query(points, predicate="dwithin")` (shapely ≥ 2.0.x) to get
all (point, centerline) candidate pairs in one C call; (c) prepared-geometry the
pavement union once (already done via `_pavement_visibility` cache).

**Mapping.** Directly shrinks KERNEL D's constant factor and the chord gate in
KERNEL A without changing results.

**Soundness: byte-identical** (same predicates, same sample points) — this is
the only survivor that can be *exactly* inert.

**Library availability.** shapely 2.1.2 (**already a dependency**), GEOS 3.13.
`STRtree` bulk/predicate query, `contains_xy`, `intersects_xy`, `shortest_line`,
`length`, `get_coordinates` all vectorized.

**Expected win.** Constant-factor: perhaps 2–5× on the chord/candidate work by
paying numpy/GEOS call overhead once per *batch of points* instead of once per
point — real but bounded (it does not change the O(points × candidates)
structure the way S1 does). Best as a **stopgap** and as the reference
implementation the S1 raster field is A/B-tested against.

**Implementation cost: ~1–2 days.** Lowest risk, lowest ceiling.

**Risks.** Diminishing — the code already batches heavily; the algorithmic wall
(per-point serving-centerline scan, off-net fallback) remains.

Links: shapely 2.x notes
<https://shapely.readthedocs.io/en/latest/release/2.x.html>.

---

## 2. Killed / parked candidates (and why)

- **Geodesic Voronoi diagram / dynamic geodesic nearest-neighbour data
  structures** (de Berg, Oh, Ahn; SoCG 2018; simple-polygon O(log n) queries).
  Theoretically the *ideal* answer to KERNEL D (O(log n) per query after
  preprocessing). **Killed for practice:** the strong results are for *simple*
  polygons; polygon-**with-holes** raises complexity and, decisively, there is
  **no maintained, robust, pip-installable implementation** — these are
  proofs-of-concept in papers. Reimplementing is a multi-week research task with
  serious robustness exposure. Park as "if S1 ever isn't enough."
  <https://arxiv.org/pdf/1803.05765>

- **Jump Flooding Algorithm (JFA) on GPU.** Constant-round Voronoi/DT on a grid.
  **Parked, GPU-conditional:** (a) it computes *Euclidean* Voronoi/DT, not
  geodesic-through-holes (a hole is not respected by JFA's flooding) without
  extra masking work; (b) it is **approximate and two-sided** (JFA+1 reduces but
  doesn't eliminate error) — fails the one-sided contract without inflation;
  (c) needs Metal/GPU, which the brief excludes from the baseline. Note
  separately: *if* an Apple-Metal path is ever pursued, JFA is the fast way to a
  masked distance field, but S1's CPU chamfer field already meets the target.
  <https://www.comp.nus.edu.sg/~tants/jfa/i3d06.pdf>

- **Fast Sweeping / Fast Iterative Method (FIM) eikonal solvers.** Alternatives
  to FMM for the field solve. **Parked:** same two-sided-error soundness issue as
  skfmm, and FIM's advantage is GPU parallelism we're not assuming; on CPU the
  scipy-csgraph / skimage-MCP chamfer Dijkstra of S1 is simpler, already
  available, and one-sided. Keep FIM in mind only if a GPU port happens.
  <https://arxiv.org/abs/2106.15869>

- **Anisotropic FMM (Mirebeau, lattice-basis-reduction).** Relevant *if* the
  per-letter directional cap were made a full anisotropic (Finsler) metric.
  **Parked as premature:** our cap is isotropic per region; anisotropy would be a
  refinement of S1's cap-per-cell field, not a separate track.
  <https://arxiv.org/abs/1201.1546>

---

## 3. Ranked shortlist

1. **S1 — Rasterized masked multi-source weighted-Dijkstra field**
   (`scipy.sparse.csgraph.dijkstra` super-source, or `scikit-fmm`, or
   `skimage.MCP_Geometric`). One field, O(1) queries, **conservative one-sided by
   construction**, uses **already-present deps** (scipy + scikit-fmm), collapses
   the 74 ms off-net fallback and the 45 k zone-tail. Best win/risk ratio by far.
2. **S2 — Polyanya exact navmesh path queries.** The *exact* route (can tighten
   the band, not just loosen it); gated by the lack of a maintained Python
   binding and its per-query (non-amortized) cost.
3. **S3 — Heat method on a 2-D CDT (potpourri3d).** Clean for KERNEL D's *label*;
   **unsound (two-sided) for KERNEL A's value fields** and adds a dependency.
4. **S4 — Exact MMP geodesic (pygeodesic/CGAL).** Best as an **exactness oracle**
   to certify S1's conservative band; not a throughput answer.
5. **S5 — Batched vectorized shapely.** Byte-identical stopgap and the A/B
   reference for S1; low ceiling, lowest risk.

---

## 4. What I would build first

**Build S1 as a conservative reach-field, validated against S5 as the byte-exact
reference and S4 as an exact spot-check oracle.** Rasterize the frozen pavement-
with-holes union at 1–2 m (resolution well under the narrowest real corridor),
conservatively (erode by ½ cell so discrete geodesics never *under*-estimate the
truth), snapping every runway/spine anchor to its nearest paved cell. Paint the
per-cell longitudinal `cap` from the owning centerline's ICAO letter into the
edge-weight field. Run **one** `scipy.sparse.csgraph.dijkstra` with a virtual
super-source seeded at `+value_a` for the ceiling field and a second at
`−value_a` (negated) for the floor field, plus one label-carrying multi-source
pass for KERNEL D — all deterministic (index-ordered seeds, fixed sparse
structure, no hash/parallel order). Replace `band(x,y)`,
`_nearest_visible_centerline`, and the skeleton `_fallback` with O(1) grid reads;
the 45 k off-net "zone" nodes and the 74 ms fallback disappear into the same
lookup. Because scipy and scikit-fmm are **already dependencies**, this needs no
new install-chore. Gate it (`O4_RASTER_REACH_FIELD`) behind an A/B harness whose
acceptance is exactly the spec's model: **provable one-sidedness** (floor only
down, ceiling only up versus the current band on SPJC / SPLP / CYXY / HECA) and
**not-worse `check_grade` violation counts**, with the flat-airport fast path
untouched. That single field turns ~700 s of per-query pavement geodesics into a
few seconds of one-time propagation plus microsecond lookups, conservatively and
deterministically, without adding a dependency or a GPU.

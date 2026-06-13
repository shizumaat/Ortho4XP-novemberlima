# HECA → 0 within-shape violations — census + plan

Build: dev @69ffc54 + concurrent geometry WIP (uncommitted boundary/
bridges/finalize/groundside/layout/pipeline).  Census via
`/tmp/probes/s81_*.py` (census → classify → actionable → dem).

## 1. Census

```
within = 72   cross = 0   steps = 0
```

Cross-shape and edge-step are already clean.  All 72 are **within-shape**
(a pair of vertices in ONE emitted polygon steeper than the role cap —
apron/taxi/junction 1.5 %).

### Severity
| band | count |
|---|---|
| severe ≥5 % | 8 |
| moderate 2.5–5 % | 12 |
| minor 2–2.5 % | 15 |
| marginal 1.5–2 % | 37 |

Half the count is marginal (≤2 %); the **8 severe + 12 moderate = 20**
walls are what an eye sees in-sim.

### By role
`apron 36, cross_connector 12, stub 10, building 9, primary_parallel 4,
junction 1`.  Aprons dominate, and every severe (>5 %) wall is on an
apron.

## 2. Actionable families (root cause from field band + DEM)

Each violation tagged by the field state at its location: distance to the
nearest taxi-centerline field vertex (`fgap`), route-band width (`bw`),
and DEM vs surface.

### Family 1 — apron-interior, unconstrained — 19 viols, worst 16.2 %
Shapes: **#233 (1.1 M m²), #289 (471 k), #242, #234, #244**.
`fgap` 117–463 m — these vertices are **60–460 m from any taxi lane**, so
the field has no constraint there and the surface follows relief/DEM.
The worst (#242, 16.2 %, surface 82→86) sits where DEM ≈ 90: the surface
is *below* terrain and walls at a transition.  Bands are wide (bw 21–23),
so **not** band-pinned — purely under-served interior.
→ **Root: the giant aprons have interior regions outside the 200 m
corridor-smoothing zone and not connected by the in-polygon visibility
graph; nothing enforces ≤1.5 % across them.**

### Family 2 — apron transition cliff — 6 viols, worst 16.2 %
Shapes: **#236** (16.2 %, band TIGHT [71,75], surface 82–86) and **#284**
(10 %, the terminal1/11 pad-face, DEM ≈ 103, band wide).
Two distinct mechanisms share this bucket:
- **#236 = route-vs-free seam**: #236's edge is route-pinned LOW (band
  [71,75] — it routes to a low runway) but its neighbour apron #242 sits
  at ~86 (DEM, unconstrained).  The 16.2 % wall is the seam between a
  route-pinned-low apron and a DEM-high apron — the *same physical edge*
  as Family 1's #242 wall, seen from the constrained side.
- **#284 = pad-lift return**: the chord-window floor (s80p2/p3) lifted
  the pad-face lanes to ~103.6, but the lift STOPS at 2× chord reach and
  the apron drops back to terrain (~100) at 10 % over 37 m.

### Family 3 — taxi route-band squeeze — 20 viols, worst 2.6 %
Shapes: **T, G, J, F1, T4, T2** (taxiways/stubs).  `bw < 2.5 m` — the
runway-route band is tight; conflicting runway routes pin the corridor to
a narrow window the geometry can't fit at ≤1.5 %.  All ≤2.6 % (minor).

### Family 4 — long-range route spread — 12 viols, worst 1.6 %
Shapes: **terminal23, terminal26, taxiway G**.  Distance 300–1066 m,
≤1.6 %.  The documented **M5 spread**: a route-justified deficit to a low
runway shows as ~1.6 % over a long stretch instead of a wall.  Legal by
the network-profile model — but the validator still counts it.

### Family 5 — terminal slope — 1 viol, 1.9 %
**terminal7** (DEM 94, surface 88–89): a squeezed pad sloping toward its
serving lanes.  Legal-by-design under the apron-follows model.

### Family 6 — other corridor residual — 14 viols, worst 3.7 %
Mixed: #236 apron 3.7 %, taxiways **T 2.8 %, T2** — corridor-write seams
and small apron pieces, 2–3.7 %.

## 3. Plan to 0 — by family, in priority order

The 72 split into **mechanically fixable** (Families 1, 2, 6, parts of 3)
and **requires-a-ruling** (Families 4, 5, the irreducible part of 3).
Order is by visible severity, since the severe walls are all in 1+2.

### Step 1 — Whole-apron ≤1.5 % enforcement (Families 1 + the #242/#236 seam; ~25 viols incl. ALL 5 severe apron walls)
Today the apron grade law is enforced only on the shape's *visibility
edges* and smoothed only within the 200 m corridor zone.  A 1.1 M m²
apron's far interior is neither visible to its constrained edges nor
inside any corridor zone, so transitions wall.
**Build:**
1. **Decompose the mega-aprons at internal necks** so the visibility
   graph connects interiors (a 1.1 M m² ring is non-convex; its far
   corners aren't mutually visible, so no edge constrains them).  The
   neck-split machinery exists (`ENABLE_APRON_NECK_SPLIT`) — widen its
   trigger to fire on grade-disconnection, not just geometric necks.
2. **Apron-wide geodesic ≤1.5 % pass**: multi-source from the apron's
   *constrained* vertices (band-pinned edges + corridor-zone verts) over
   the full in-polygon visibility graph, propagating a ≤1.5 % ceiling/
   floor to every interior vertex (the `_lipschitz_tighten_bands`
   mechanic, scoped to one apron's interior).  This makes the interior
   follow its constrained rim at ≤1.5 % instead of raw DEM.
3. **Seam coupling** for the #236/#242 case: where two aprons share an
   edge and one is route-pinned low, the free neighbour's edge must
   track it (extend the cross-shape weld to in-emit shared apron edges).
**Risk:** pulling a huge apron's interior toward a low route-pinned rim
can over-flatten legitimately-high regions; gate it and measure SPJC
(its sanctioned balloon must not collapse).  **Expected: −18 to −22.**

### Step 2 — Pad-lift graded return (Family 2 #284; ~6 viols, the 10 % padface)
The chord-window floor lifts pad-face lanes then cuts off hard at 2×
reach.  **Build:** make the floor DECAY at ≤1.5 % back to terrain over
distance (a graded ramp, not a step), so the apron-corridor smoothing
carries the transition.  Equivalent to widening the lift's apply radius
with a linear roll-off.  **Risk:** low — it only lowers the cutoff
gradient.  **Expected: −6.**

### Step 3 — Taxi corridor squeezes via runway flex (Family 3; 20 viols, ≤2.6 %)
The tight bands (bw <2.5 m) are conflicting-runway squeezes.  Two levers:
1. **Deeper runway flex** — let the binding runway dip/rise another step
   to open the band (the corridor→runway flex feedback exists; raise its
   round cap for these contacts).  Closes the ones with headroom.
2. The residue with no flex headroom is **genuinely irreducible** → goes
   to Step 5.  **Expected: −10 to −15.**

### Step 4 — Corridor-write seams (Family 6; ~8 viols, 2–3.7 %)
Taxiway T/T2 2.8 % and the small apron pieces are corridor-write-vs-
neighbour seams (the documented write-arbitration family).  **Build:**
fold into the strict pair-law closure (already runs at ±2.5 m) — raise
its budget for these specific seams or add them to the tie set.
**Expected: −6 to −8.**

### Step 5 — RULING: route-justified spread (Families 4, 5, + Step 3 residue; ~15 viols, ≤1.9 %)
These are **not solver bugs** — they are route-justified: a pad/taxiway
800 m from a low runway *must* lose ~1.6 % over that distance, and a
squeezed pad *must* slope.  The network-profile model already treats
these as legal.  **The validator does not.**  To reach literal 0, choose:
- **(a) Accept** — the validator counts a within-shape pair as legal when
  it is ≤ the route-justified rate over the taxi-route distance to its
  binding runway anchor (the model's own metric).  Clean, principled,
  matches the design.  **Recommended.**
- **(b) Grade away** — escalate runway flex until even these close.  Risks
  over-dipping runways the user has blessed (05C 108.70) for cosmetic
  validator-zero.  Not recommended.

## 4. Projected trajectory

| after | within | notes |
|---|---|---|
| now | 72 | 8 severe |
| Step 1 | ~52 | severe → ~1 (the #284 padface) |
| Step 2 | ~46 | 0 severe |
| Step 3 | ~33 | 0 moderate from taxi |
| Step 4 | ~25 | only route-spread left |
| Step 5(a) | **0** | validator accepts route-justified spread |

Steps 1–4 are mechanical and measurable against the gate-off baseline
(63) + SPJC/CYXY green guards.  Step 5 is the one user ruling — the same
"irreducible squeeze" question deferred since s78, now with HECA's exact
list.  **Without Step 5 the floor is ~15–25** (the route-justified
residue); 0 is only reachable by either accepting that residue as legal
(5a) or spending runway-flex depth the invariants forbid (5b).

## 5. Validation protocol (every step)
Per-axis audit only (`tests/test_pavement_grade.py`); invariants register
05C 108.70 / 05L 57.9–62.8 / A4 / A5; SPJC + CYXY gates GREEN (hard);
gate-off byte-identical; deterministic (PYTHONHASHSEED 1==2); suite
baseline.  ⚠ the concurrent geometry WIP is uncommitted — re-census on a
clean tree once it lands (shape numbering shifts: the pad-face family was
#257 → now #284).

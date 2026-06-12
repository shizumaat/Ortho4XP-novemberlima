# Interior-path entries — killing the across-grass grade-check class

★★ USER RULING (2026-06-11): **no shape may ever check grade ACROSS
GRASS.**  Every place the long-range law or the network-profile field
couples an off-graph point to the centerline graph through a STRAIGHT
gap charged at cap is a potential across-grass coupling.  This doc is
the implementation plan; build it BEFORE flipping `SERVICE_ROAD_CARVE`
on (the road work's gate-on residual at CYXY is exactly this class).

## 1. Evidence (s79 item-2 investigation, all measured)

* Pavement-gating the field's straight-gap entries at CYXY: 0/0/0 →
  50 within.  Attribution: the 4 anchor straight-gap entries gate for
  FREE (0 viol); **ONE law-entry gap edge carries 49/50** —
  `(-221,-429)→(-132,-430)`, 89 m straight, 42 % over grass
  (stub#11/junction#39 ↔ junction#39/#42, the n31/n42 class).
* **ALL 13 killed couplings join nodes in the SAME airside component**
  — an interior pavement path exists in every case.  Load-bearing
  pair: straight 89 m vs **interior geodesic 103 m** (legal spread
  1.34 vs 1.54 m).  Uncoupled, the field drifts 7.8 m and the
  wrap-around pavement (apron #67/#69 — legitimately within-shape
  checked) absorbs it as 49 smeared sub-metre pairs AT the notch.
* Conclusion: the network needs the COUPLINGS, not the across-grass
  shortcuts — the straight gap is an under-measure of a real
  through-pavement path.  Where no interior path exists (true
  islands), the coupling should not exist at all.
* Partial application is THE failure mode — measured twice (the s78p5
  revert; the s79 field-only experiment = the 50 viol).  Solver and
  law must move together.
* Reference implementation of the geodesic:
  `/tmp/probes/s79_interior_path.py` (local-window visibility-graph
  Dijkstra over the airside union; 1,464 verts, instant).
  Experiment harness: `/tmp/probes/s79_gate_gap_experiment.patch`
  (`O4_NPF_GATE_GAP=1|edges|anchors`, `O4_NPF_GATE_ONLY=i,…`).

## 2. The shared measure

New `auto_patch/interior_path.py`:

```
build_interior_measure(airside_union) -> InteriorPathMeasure
InteriorPathMeasure.distance(pa, pb) -> float | None
```

* FAST PATH: chord pa→pb covered by the union (buffer 0.5, prepared)
  → straight length.  This must answer ~all queries (entry gaps are
  usually tiny and on-pavement).
* Else: local-window visibility Dijkstra (window = max(300 m,
  2×straight) around the pair; verts = pa, pb + union boundary verts
  in window; edges where the chord stays inside).  No path → widen
  the window ONCE iff both points share an airside component (cheap
  point-in-part test) → still nothing ⇒ **None = no coupling**.
* Deterministic: sorted vertices, no set iteration into values.
* ONE instance per solve, built where `bridge_test` is built today
  (unified_jacobi ~L4160, same `PAVEMENT_ROLES` union) and THREADED to
  every consumer — never rebuilt per call site (parity by sharing).

## 3. The sites (must change TOGETHER — §1 partial-application trap)

1. **network_profile law-entry gap edges** (~L665): weight =
   `measure.distance(node, apt_node)`; None → no edge.  Nearest-node
   selection may stay Euclidean (the measured pair re-weights).
2. **network_profile anchor band entries** (~L902 extra_band_anchors →
   point_seeds): gap = interior distance; None → skip (measured FREE
   at CYXY — but see the tile-seam trap, §6).
3. **route_field anchor/vertex entries** (~L239 `grid.nearest` →
   (key, gap) on BOTH ends): charge the interior distance.  Docstring
   L11/L22 ("a far vertex gets a WEAK band … CORRECT behaviour") is
   SUPERSEDED — rewrite it to the ruling.
4. **`_runway_reach_bands`** (unified_jacobi ~L1247): the `_near`
   cache, anchor seeds, `extra_points` field-vertex entries — same
   measure.  Field vertices sit ON the graph (gap≈0 → fast path).
5. **Validator simultaneity**: `route_field` is shared, but the
   standalone validator (`tools/check_grade.py` via
   `verification.route_ctx_from_layout`) only ships centerlines —
   extend `route_ctx` with the airside polygons (lat/lon → rebuilt
   union) so check_grade builds the SAME measure.  Without this the
   suite asserts a law the build didn't apply.

Gate: `INTERIOR_PATH_ENTRIES` (default ON at ship; OFF = byte-identical
straight-gap behaviour, verified at CYXY).

## 4. Optional unifications (separate, measured, AFTER green)

* Prox-coupling notch inflation (straight + 4×outside, ≤35 % outside)
  is a heuristic interior-path estimate — replace with the true
  measure.  Measured A/B; the 60 m radius stays.
* `ROUTE_NOISE_FRAC` (4 %) partly compensates entry under-measurement
  — re-derive only after the model is green everywhere.

## 5. Work order & validation

1. Re-baseline on landed dev (concurrent sessions active): CYXY/SPJC
   0/0/0, HECA within, SPLP, suite 320p/2f.
2. `interior_path.py` + unit tests (synthetic: notch pair ≈ the
   long way around; inside pair = straight; island = None;
   determinism).
3. Sites 1–4 in ONE change behind the gate; CYXY first (its n72/n88
   notch is THE fixture): expect 0/0/0 gate-on.
4. Site 5 (validator route_ctx) + suite.
5. Battery: CYXY+SPJC 0/0/0 per-axis (s75_axis_audit — never trust
   the standalone count), HECA within ≤ baseline + invariants
   register (05C 108.70, 05L 57.9–62.8 smooth, A4/A5, terminals,
   #256 spread, #198 w/ retreat ON), SPLP both tiles (THE seam
   fixture — §6 trap), HEAZ/MMOX compare targets, PYTHONHASHSEED
   determinism, O4_PERF build-time delta (reach-bands vertex entries
   are the hot spot — add a per-cell cache only if measured needed).
6. **Acceptance test that closes the loop**: flip
   `O4_SERVICE_ROAD_CARVE=1` and re-measure — CYXY gate-on 18 → ~0
   (the road decouples from apron #67/#69 across the grass), HECA
   gate-on ≤ 52 with the #198 cliff intact.  Then the road gate can
   ship ON.

## 5b. BUILD RESULTS (s79, all measured at HEAD)

Gate `INTERIOR_PATH_ENTRIES` default ON (`O4_INTERIOR_PATH=0` = legacy
straight gaps).  Measure in `auto_patch/interior_path.py`; consumers:
network_profile (gap edges via ``entry_dist``, anchor band entries),
`_runway_reach_bands` (``_near`` + extra_points), route_field
(``_entry`` on anchors, field_pts AND check vertices), check_grade
(builds the same measure from the emitted airside ways —
`AIRSIDE_MEASURE_ROLES` is the ONE canonical role set).

* CYXY 0/0/0 gate-ON and gate-OFF; SPJC 0/0/0; **HECA 49/0/0 (was
  68)** with every invariant held (05C 108.70, 05L 57.9–62.8 smooth,
  A4/A5, terminals, #256 0.7); **SPLP 71 (was 82)** + the known
  single step — the seam trap did NOT bite at the untiled fixture;
  suite 325p/2f (baseline failures only); PYTHONHASHSEED
  byte-identical.
* PERF (hard-won): naive per-query geometry ops = **245 s** HECA
  enforce.  Three fixes → **10.3 s** (gate-off 1.2): (1) trivial-gap
  fast return ≤5 m (band effect ≤7.5 cm); (2) GRAZE shortcut —
  chord outside-length ≤2 m charges straight+2×graze, no Dijkstra;
  (3) ★ 400 m lazy TILE CACHE of the union (difference/intersection
  against HECA's 83 km-perimeter slab was the dominant cost) +
  K-NEAREST (24) visibility sparsification (full O(V²) adjacency was
  90 ms/query × 371 queries; overestimates are CONSERVATIVE — longer
  path = looser band).  Query mix at HECA: 1566 covered / 371
  Dijkstra / 39 none / 6 graze.
* ★ visibility-graph corner tangency: edge tests run against
  geom.buffer(0.3) or chords ENDING at concave ring corners fail
  ``covers`` by centimetres.
* Acceptance (roads + interior): CYXY road-gate-on within 18 → 9 ✓;
  the road/apron flanks now genuinely decouple, EXPOSING the road
  feature's deferred flank work as 16 cross + 151 steps at road seams
  (welded airside boundaries need the unweld/clearance-face rendering
  — service_road_carve.md Step D, the next road milestone).

## 6. Trap register

* **Tile-seam pins**: the tile cut opens a deliberate 10 m pavement
  gap at integer boundaries; a seam pin's entry chord may cross it —
  that gap is an ARTIFACT, not grass.  Treat the cut band as pavement
  for the measure (buffer the tile boundary line into the union) or
  exempt base_hard seam entries; decide at SPLP (its 17 m/660 m seam
  story is the sensitive case).
* **False None from a too-small window** fragments the field → the
  anchor-less component current-surface seed handles TRUE islands, but a
  falsely-orphaned component writes drift (the 25 % wall class) —
  hence the same-component escalation in §2.
* **Flex demands ride the band Dijkstras** — 05C/05L demand depths may
  shift slightly with corrected gaps; the invariants register is the
  guard (05C 108.70 user-blessed).
* The validator must never get the measure while the solver doesn't
  (or vice versa) — single shared builder, both sides read the gate.
* Entry-gap caching keys must include the plain_only flag (the
  existing `near{}` semantics).

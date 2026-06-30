# Route-Field Model (#3) — implementation design

> ⚠ **SUPERSEDED (2026-06-30 audit).** Intermediate solver model. Lineage:
> route-field → network_profile → route_profile → **`anisotropic_edge_handling_plan.md`**
> (the live model). The code targets named here (`unified_jacobi.py`) are deleted.
> Its one still-open follow-up (route-band check on the shipped OSM patch) is tracked
> in **`OPEN_ITEMS.md`**.

**Status: DESIGN, user-approved direction (s73-p3, re-confirmed s73-p10g).
Not built.  This document is the handover for the implementing session.**

Written 2026-06-10 at the close of s73-p10g (`corridor-curves` @153ad57).
Read together with: repo `STATUS.md` (s73 parts 3, 10d-10g), memory file
`runway_flat_profile_route_band_flex.md`, and `docs/elevation_solver.md`
(the current model this supersedes in part).

---

## 1. What changes, in one paragraph

The within-shape grade law today says: any two vertices of one pavement
shape constrain each other at `cap` (1.5 %) over the **visibility-geodesic
chord** between them — kilometre-scale chords included.  The route-field
model replaces that long-range law: a vertex's feasible elevation band is
`∩ over anchors a of [E_a ± cap · route_d(a, v)]` where `route_d` is the
**taxi-route distance** over the centerline graph, and visibility chords
survive **only as a local smoothness cap** (pairs ≤ ~60–100 m, window to
be measured).  The validator (`tools/check_grade.py`) changes
**identically and simultaneously** — the definition of a violation
changes, which is why this is a model change and cannot ship solver-only
or validator-only.

## 2. Why (the measured evidence — do not re-litigate)

Grade rules (ICAO Annex 14 §3.9, EASA CS-ADR-DSN.D.265/.280) regulate
longitudinal-along-route and transverse slope.  Nothing regulates the
straight line between two points kilometres apart.  Chords systematically
UNDER-measure the route (corner cuts, detours) → manufactured
infeasibility.  Receipts, newest first:

- **s73-p10g (the decisive one)**: provenance Dijkstra over the live
  constraint graph at HECA: the HARD 05C contact (108.74, node n656 at
  30.113613, 31.416040) reaches the taxiway-A apron mouth through
  ~2,500 m of CHAINED visibility chords (chains cross shape boundaries
  via shared nodes) where the real taxi route is ~3,080 m.  Edge-graph
  band at the mouth: `[71.2, 62.7]` — **8.5 m infeasible** — smeared as
  ~70 sub-metre "violations" across aprons #190/#194.  The user's own
  route arithmetic (62.5 at the mouth) was exactly right; route bands
  agree (ceiling 62.4); the chord graph alone disagrees.
- **s73-p10d**: terminal7 is 749 m BY ROUTE through apron #190's lanes
  from the A-mouth (floor 58.8) — the apron pull-down the user predicted
  is route-legal; chord chains forbade it.
- **s68**: the terminal7→05C "104.2 ceiling" audit chain rode an 839 m
  cross-apron geodesic hop (s66 artifact class); the route answer
  (~108–110) was ruled authoritative and later confirmed in-sim (107.9
  user-predicted ≈108).
- **s66**: the "infeasible mega-apron" verdict was a chord-measurement
  bug; the ruling "never say split the apron" dates from this.
- Per-axis junctions (s73-p10) are the SAME principle applied to
  junction interiors: cap along the centerline arc, diagonals
  unregulated.  #3 promotes it airport-wide.

HECA's remaining **per-axis within = 216** (audit
`/tmp/probes/s75_axis_audit.py HECA <osm>`) is dominated by this class:
apron chains (#190 ~70, #194), terminal4's lump family (~10 entries,
5–10 % — terminal-seed-vs-route tension), stubs J3/U.  CYXY and SPJC are
already 0/0/0 per-axis, and neither has km-scale chords binding — expect
no movement there (good regression canaries).

## 3. The model, precisely (authoritative statements)

1. **Long-range law**: for every pavement vertex v and every ANCHOR a
   (definition of anchor set in §5.3), the band contribution is
   `[E_a − capL·route_d(a,v), E_a + capL·route_d(a,v)]` with `route_d`
   = shortest path over the taxi-route graph (see §5.1) + endpoint gaps
   (`TaxiRouteGraph.nearest_key` gap on both ends).  Per-role caps
   integrated along the path are a future refinement — today every
   pavement role caps at 1.5 % (config `ROLE_GRADE_LIMITS`), so a single
   `capL` is correct and simpler (s73-p3: "only worth building when caps
   diverge").
2. **Local law**: visibility-chord pairs with chord length ≤ `W` (the
   LOCAL WINDOW, start at 80 m, measure 60–100) keep the current
   semantics: `|E_i − E_j| ≤ cap · chord_d`.  Ring-adjacent pairs always
   survive regardless of length (the physical edge).  Junction per-axis
   logic is UNCHANGED (it already replaces chords with arcs and drops
   diagonals; it operates within W anyway).
3. **Validator**: `check_grade.py` within-shape check enumerates only
   pairs ≤ W (+ ring-adjacent + the existing per-axis junction logic via
   `taxi_axes_ll`), and ADDS a route-band compliance check (each vertex
   within its anchor route bands, with the noise margin of §5.2).  The
   build WARN (`elevation._report_within_shape_violations`) mirrors it —
   those two have drifted before (s64); keep them on one engine.
4. **Supersession of an older ruling — make this explicit to the user if
   any doubt arises**: s65/s68 recorded "within-apron grade stays
   geodesic".  s73-p3's user-approved #3 explicitly demotes the geodesic
   to the LOCAL cap; p10g's evidence is why.  The rulings register in
   §8 lists what stays binding.

## 4. Why it cannot be done as another patch

Chord semantics are load-bearing in every layer, and the layers now run
on TWO currencies (corridor/flex = route; bands/enforce/validator =
chord).  Today's apron residue is literally the two currencies fighting
over the same vertices.  Patches so far (route bands for runway
reachability, route-noise deadband, per-axis junctions, curve-aware
corridor distances, band exemptions near corridor writes — see STATUS
s66→s73) are per-subsystem carve-outs; each fixed its subsystem and then
collided with the chords still live in the next.  Flip the solver alone
→ the gate flags legal surfaces; flip the gate alone → the solver keeps
manufacturing tension the gate no longer sees.  One piece, both sides.

## 5. Implementation map

### 5.1 The route graph (`auto_patch/taxi_routing.py`)

`build_taxi_route_graph(layout)` over `layout.apt_taxi_centerlines`
(includes apron lanes — verified p10d: terminal7→A-mouth routes through
#190's lanes).  KNOWN GAPS the implementation must handle:

- **Threshold ring corners are NOT reached** by the graph (s68) — the
  runway-centerline augmentation in `_flex_route_bands` exists for this;
  reuse/centralize it.
- **Ingest drops runway-crossing + junction-buried centerlines** (HECA:
  12 + 28 of 172).  The A4 curved exit line is missing even from the
  full set (p10).  The corridor machinery's THROAT-geodesic fallback
  (p10) covers exits; for the route graph, expect local holes near
  exits — endpoint-gap handling (`nearest_key` gap, added at cap as
  straight-line) is usually sufficient because gaps are short.
- **Coverage holes** (service lanes, apron corners far from any lane):
  a vertex whose `nearest_key` gap is large gets a weak band
  (cap·(route+gap) with a big gap) — that is CORRECT behavior (less
  long-range constraint, local law still applies).  Do NOT invent a
  chord fallback for them; that reintroduces the bug.
- Graph is built per call — cache one instance per solve (the enforce,
  the corridor pass and the flex path can share it; today they each
  rebuild).

### 5.2 Route noise margin

The route graph under-counts real routes by ~4 % (s73-p3 measured:
straight endpoint stubs, uncurved row joins; `_ROUTE_NOISE_FRAC = 0.04`
exists in the flex demand path).  Bands used as HARD constraints must
carry the margin: use `cap · route_d · (1 + _ROUTE_NOISE_FRAC)` (or
equivalently relax each bound by `0.04·cap·route_d`).  The VALIDATOR
must use the SAME margin or it will flag the solver's own legal output.
(The p10g A-mouth numbers: chord-chain 2,500 m vs route 3,080 m — the
route number itself may still be a few % short of physical truth; the
margin is what keeps that honest.)

### 5.3 Anchor set for the long-range law

Same sources the enforce/bands already treat as immovable:
- runway nodes at solved values + seam-pinned nodes
  (`_runway_node_set`, `_seam_pinned_runway_nodes`),
- `base_hard` nodes (thresholds, seam, boundary pins),
- corridor-held writes (`held_extra`) — they threaded their own route
  bands, so anchoring on them is consistent (this REPLACES the p7/p10g
  `band_exempt` machinery rather than extending it),
- terminal pads: see §6 (transitional).

### 5.4 Solver touch points (all in
`elevation_per_surface/unified_jacobi.py` unless noted)

| Site | Today | Change |
|---|---|---|
| `_visible_grade_edges` (L~1293) | all-pair visible chords | add `max_len=W` parameter; keep ring-adjacent unconditionally; junction per-axis block unchanged |
| `_build_shape_constraints` (L~1341) | consumes the above | pass W for apron/terminal/junction roles (rects are small; unchanged) |
| `_grade_bands` | Dijkstra over chord edges from hard anchors | still used (edges now local — bands become weak/local); fine |
| `_runway_reach_bands` (L~735) | route-graph bands, RUNWAY+seam anchors only | extend anchor set per §5.3; add the §5.2 margin; this becomes THE long-range law for the enforce |
| `_enforce_within_shape_grade` (L~870) | route bands for runway reachability + chord edges + `band_exempt` | bands from the extended `_runway_reach_bands`; `band_exempt` machinery and the P2 apron exemption become REMOVABLE (measure before deleting; the corridor-junction exemption (p7) existed because route bands fought corridor writes — with corridor writes IN the anchor set the fight disappears by construction) |
| `_project_within_bands` | consumes edges + bands | unchanged (inputs change) |
| `_directional_relief` | consumes `shape_constraints` edges | unchanged (edges now local) |
| corridor `_taxi_corridor_profiles` | already route-currency (station route bands, curve-aware distances, ties at in-junction geodesics ≤ junction scale = local) | unchanged in spirit; its `st_lo/st_hi` should come from the SAME shared band computation; the freeze-skip → runway-flex demand path unchanged |
| flex demand synthesis (`_flex_route_bands` etc.) | own route graph + augmentation | unify on the shared graph/augmentation; deadband doctrine unchanged |
| `_report_within_shape_violations` (elevation.py) | mirrors check_grade | change with the validator, same engine |

### 5.5 Validator touch points (`tools/check_grade.py`)

- Within-shape pair enumeration: window at W + ring-adjacent (the
  per-axis junction path via `taxi_axes_ll` already exists and is
  used by the gate test — `tests/test_pavement_grade.py` constructs
  axes; keep that contract).
- NEW route-band check: per vertex, against §5.3 anchors with §5.2
  margin.  This requires the layout (route graph) at audit time — the
  gate test already builds the layout; give `run_checks` an optional
  `route_ctx` the test passes (standalone CLI runs skip it, printing a
  notice — same pattern as `taxi_axes_ll`).
- `WITHIN_SHAPE_CAP = 0` and `MID_EDGE_CAP = 0` stay the bar.

### 5.6 What becomes dead after (delete only with measurements)

`band_exempt` + corridor-junction exemption (p7), the P2 apron
exemption (p10f, already measured no-op), terminal taxi-route seeds
(§6), possibly the route-noise deadband's special-casing in flex (it
generalizes into §5.2).  History says: gate first, measure, then
delete (`O4_*` env levers; the user's standing instruction is to not
revert experiments before review).

## 6. Terminals under the route field

s73-p3 derivation: with route-distance law, pads EMERGE at their
route-feasible positions — seeds become redundant.  BUT two standing
rulings constrain the transition: **terminals must not rise** (s68) and
terminal flatness < grade priority (s67).  Recommended sequence: keep
`_seed_terminals_from_taxi_routes` + held seeds in step 1 (the route
law alone should already drain the terminal4 lump family, because its
lumps are seed-vs-chord tension); then trial seed removal as a separate
measured step.  Do not bundle the two changes — attribution dies.

## 7. Validation protocol (the gate for this change)

1. **Invariants that must not move** (user-verified in-sim values):
   HECA 05C/23C min 108.7 (max 116.5), 05L/23R exactly 57.9–60.7,
   A4 carries the climb (~1–1.5 %), A5 flat 59.9–60.5, T monotone
   through -10292, thresholds intact (116.5).  Probe:
   `/tmp/probes/s69_runway_mins.py`.
2. **Per-axis audit** (`/tmp/probes/s75_axis_audit.py ICAO file.osm`) —
   NEVER bare `check_grade` (it over-reports per-axis-exempt diagonals;
   this cost p10e/p10f real time).  Expected: CYXY 0/0/0 and SPJC 0/0/0
   unchanged; HECA within 216 → the residual should collapse to the
   non-route classes (T4-wall arbitration family around #242, SPLP-class
   lumps) — if it does NOT collapse, the route graph has coverage holes;
   debug with the §5.1 list before touching the model.
3. **Suite** (worktree, `venv/bin/python -m pytest tests/ -q`): baseline
   at @153ad57 is **307p/2f** (SPLP deferred lump + HECA within).  No
   green→red anywhere; HECA's gate should flip when the residual tail is
   small enough.
4. **Determinism**: build CYXY twice under different `PYTHONHASHSEED`,
   hashes must match (p10f protocol).
5. Diagnosis tooling that already exists and works: `O4_TRACE_LL=
   "lat,lon;…"` (corridor write trace + enforce band trace + provenance
   Dijkstra naming the binding anchor), `O4_CORRIDOR_DEBUG=1`,
   `O4_STEP_DEBUG=1`, `O4_ENFORCE_DEBUG=1`.

## 8. Rulings register (binding constraints, verbatim intent)

- NO airport is ever legitimately infeasible — every violation is a
  solver gap (s62; the zero-cap gates encode this).
- Runway flex is SYMMETRIC (dip or rise) the MINIMUM; route bands are
  the authoritative runway-reachability metric (s67/s68).
- Terminals must NOT rise; terminal7 ≈ 70; squeezed clusters slope
  (s68).
- 1.5 % applies along the CENTERLINE; cross-axis junction diagonals are
  unregulated; inside-curve edges may exceed; exit junctions CARRY the
  climb runway→rect (s73-p10).
- Taxiway A near 23R stays flat (~60) until the APRON (s73-p10c).
- Feasibility headroom never lifts a projected surface — the corridor
  must WRITE it (s73-p10).
- DEM is a starting point; correct grade is king (s73-p5).
- "Within-apron grade stays geodesic" (s65/s68) is SUPERSEDED by #3's
  local-window demotion (s73-p3 approval, p10g evidence) — at the LOCAL
  scale it still holds, which is the window's purpose.

## 9. Practical/ops notes for the implementing session

- Work in the worktree
  `/Users/noah/Ortho4XP-novemberlima/.claude/worktrees/joint-corridor-solve`
  (branch `corridor-curves`).  ⚠ The harness RESETS the cwd to the main
  repo at EVERY turn boundary — re-`cd` or use absolute paths in every
  command, and re-verify with `pwd`/`git branch --show-current` before
  any relative-path measurement (a dev-contamination incident cost
  p10e its numbers).
- Build: `JCS_GATE=1 venv/bin/python /tmp/probes/build_jcs.py ICAO
  /tmp/out.osm` (~3–5 min HECA).  `venv/bin/python` only (no system
  python); probes need CWD=repo root for OSM_data.
- `TAXI_CORRIDOR_PROFILE` defaults ON (config.py).  dev is the user's
  in-sim state at s73-p9 — do NOT merge `corridor-curves` without the
  gate checks (merge is currently gated on HECA/SPLP only).
- The standalone test path uses the SMOOTHED DEM (apt_smoothing_pix=8)
  via `_load_airport_dem`; probe builds via build_jcs use the same.
  Numbers differ slightly from raw-DEM builds — compare like with like.
- shapeIDs in the emitted OSM = `shapeID` way tag; the user inspects in
  JOSM by `shapeID=N` search.  Standalone-build numbering ≠ production
  patch numbering (+2 offset historically).

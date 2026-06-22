# STATUS — handover (2026-06-21)

Branch `dev`. **UNCOMMITTED.** This session worked on **taxi-centerline grading
for variable-width & hilly airports** (CYXY taxiway G climbing to rim buildings).

## ★ START HERE
- **`docs/taxi_centerline_grading_plan.md`** — the authoritative plan + priority
  model. **§9 "BRINGING IT TOGETHER" is the definitive plan for the final piece**
  (written 2026-06-22 after a full pipeline + history analysis; it SUPERSEDES the
  P4–P7 sketch in §5). READ §1 (model) then §9 FIRST.
  - TL;DR of §9: the field `F` already computes the right answer (closest-to-DEM,
    per-letter-banded, network-consistent — CYXY G 708.9 ≈ building3 709). The
    bowl is a TRANSFER failure: emitted apron/junction/building verts seed DEM,
    relief flattens them low, and the enforce uses `F` only as band BOUNDS, never
    as a TARGET (movement-min POCS, no attractor). FIX = adopt `F` as the enforce's
    per-node TARGET (lift-only, in-band) so neighbours rise WITH the held corridor
    (P4a), + make the yield-release directional (P4b). Pairs with P3.
- Memory note `apron_spine_dem_seed_climb.md` — raw session findings behind the
  plan (the dead-ends and why).
- ⚠ **Pin `PYTHONHASHSEED=0`** for ANY A/B build comparison — the apron/junction
  partition is hashseed-nondeterministic (CYXY within-shape count flakes 5↔18 on
  the SAME config, independent of any change).

## The goal & model (1-paragraph)
The shipped solver is good at most airports but couldn't (a) use the steeper
grade narrow code-A/B taxiways are allowed (3% vs 1.5%), (b) climb truly hilly
terrain (CYXY's network sat ~10 m below terrain in a "bowl"), or (c) keep some
junctions smooth. Model: a **feasibility route band** `[floor, ceiling]` says
what elevations are POSSIBLE at each point; grading then conforms to FAA/EASA
grade+curvature within it under a priority hierarchy — **runway thresholds +
tile seams = the only hard anchors**; grade/curvature compliance is sacred and
overrides DEM where needed; buildings flat (rare HECA-class small-slope
exception), aprons ≤1%, taxiways/junctions variable per-letter grade and carry
the elevation change. **Minimal-deviation principle:** everything sits as close
to DEM as grade allows — move only as much as REQUIRED, never the runway-pulled
floor (that over-pull was the bowl). Full detail: the plan §1.

## Done & banked — KEEPERS (default ON, the variable-grade primitives)
1. **`TAXI_REACH_BAND_BY_WIDTH`** — `_runway_reach_bands` uses per-edge caps
   (`TaxiRouteGraph.edge_cap` from `apt_taxi_letters`), so the feasibility band
   reflects the real per-letter climb rate (CYXY G ceiling 720→727).
2. **`JUNCTION_NARROW_GRADE`** (per-axis) — a junction edge ALONG a code-A/B
   centerline earns 3%; ring/transverse stay 1.5% (matches the validator's
   per-axis cL/cT). `_collect_junction_axes`→caps + `_edge_narrow_cap`.
   PER-AXIS only — isotropic 3% destabilises (CYXY within 18→41).
3. **`SPINE_PIECE_ROLE_REEVAL`** — narrow corridor pieces sliced from an apron
   parent are promoted apron→junction (so a taxiway-through-apron isn't capped at
   1%). Promotion-only (junction parents byte-identical). `junction_spine.py`.
4. **`CORRIDOR_SPINE_CHAINS`** (plan P2, NEW 2026-06-22) — `_taxi_corridor_profiles`
   now adds a station-only `chain_data` entry over the spine nodes of every
   centerline a rect chain doesn't fully cover (the promoted-apron stretches —
   CYXY taxiway G's 35 pieces), so the NETWORK PROFILE field value is written and
   HELD along the WHOLE route (G held nodes 82→385). Built only where ≥1 node is
   uncovered → airports without such stretches are byte-identical (gate-off ≡ on).
   **Flipped `test_pavement_grade[CYXY]` RED→GREEN** (suite 6→5 failed, no new
   reds; gate-off = the 6-failed baseline exactly). G still tops ~708 vs the
   ~718 m rim (the field "bowl" — that's P3, not P2 coverage).

These shift solved values slightly at junctions, so SPJC/SPLP compare-target +
CYXY grade move (see suite below); they introduce NO new grade violations on the
good airports.

## Scaffolding — DEFAULT OFF, to RETIRE (proved mechanisms, NOT the architecture)
- **`APRON_FEASIBLE_LIFT`** (`_anchor_aprons_at_feasible_high`) — hard-anchors
  aprons flat at their route-band ceiling. Lifts the complex but flat hard-anchor
  is the WRONG driver (pins the apron body high, troughs the centerline).
- **`O4_TAXI_SPINE`** — per-centerline smooth profile (inside the same fn). Proved
  G CAN be made smooth (705→718) but per-segment, not network-consistent.
- **`O4_APRON_NOANCHOR`** — with spine on, skip the flat anchor so aprons conform.
- **`BUILDING_DEM_ANCHOR`** (`_anchor_buildings_at_feasible_dem` +
  `_relax_buildings_and_resolve` flex) — hard building pins; too rigid (CYXY
  within 18→410). RETIRE; buildings are flat-but-conforming, not hard.
- **`O4_DEM_ATTR`/`O4_DEM_FLOOR_ATTR`** — made the existing DEM-attraction
  strengths env-tunable (defaults unchanged); keep as knobs.
- **`FIELD_ROUTE_BAND_BY_WIDTH`** (P3, NEW 2026-06-22, default OFF) — the
  CORRECT per-letter field route-band fix (`_runway_route_band` uses
  `edge_cap`); banked gated-off because it regresses without P4 (held centerline
  climbs, neighbours don't). FLIP ON with P4. This is the real P3 — not
  scaffolding to retire, just dormant until P4 lands.

**Reproduce the in-sim evaluation build** (lifted aprons + smooth-ish G, rough
junctions) that the user reviewed: `O4_APRON_FEASIBLE_LIFT=1 O4_TAXI_SPINE=1`.
Default build = the clean keepers-only baseline (no lift; the bowl).

## Where the plan goes next (P4–P7, see doc; P2 DONE, P3 BANKED 2026-06-22)
Make the **corridor profile (`_taxi_corridor_profiles`) the single
smooth-centerline driver**: ✅ P2 DONE — chains now cover the promoted-apron
junction stretches (gate `CORRIDOR_SPINE_CHAINS`, keeper #4 above); every
centerline is one network-consistent held profile. ◐ P3 band fix BANKED (gate
`FIELD_ROUTE_BAND_BY_WIDTH` default OFF — `_runway_route_band` now uses the
per-letter `edge_cap` so a narrow route's field ceiling is its real 3% reach,
not 1.5%; CYXY G 712→714). It REGRESSES standalone (held centerline climbs but
its neighbours don't → within-shape 0→10) so it is gated OFF pending P4. **NEXT
= P4** (it enables P3): aprons/buildings CONFORM up/down to the held centerlines
(no hard anchors, minimal deviation).
  ⮑ P4 INVESTIGATED 2026-06-22 (no code shipped — two dead-ends ruled out, tree
  clean). The P3 climb is written+held on G, then LOST in the final enforce's
  *"CORRIDORS YIELD TO FLAT TERMINALS"* release (`held_all - corridor_held_set`,
  unified_jacobi ~L3318/L3455): it releases the corridor to sink toward a lower
  neighbour even though the adjacent `building3` is flat at 709 ≈ G's field 708.9.
  Dead-end A: holding the corridor through the release → within 10→13 (WORSE) —
  the neighbours don't conform UP, they're pinned low. Dead-end B: P3 band alone
  → 0→10. So P4 is the ACTIVE upward-conformance rework: DRIVE apron/junction/
  building neighbours UP to the held climbed centerline (remove the route-band
  floor / DEM-relief seed / flat-pad pin that holds them low), and make the
  yield-release BIDIRECTIONAL (yield DOWN to a low pad = SPJC; hold + lift
  neighbours UP to a high pad = CYXY). Coupled with P5. NOT a toggle — needs the
  real conformance pass. ALSO: kill the junction trough (P5); explicit
corridor↔wide-apron transition only where genuinely needed (P6); retire the
scaffolding + add a centerline-smoothness check/test (P7).

User's latest in-sim review of the eval build (the concrete targets for P2–P5):
G too shallow the first ~100 m (should climb at 3% from the start); a too-steep
jump then plateau (segment-boundary steps, not one uniform grade); junctions
(shapes 85/86) trough — centerline BELOW the edges — and don't match the cap/rect
they connect; connectors (to apron 105) sit flat when they could climb 3% to meet
G; junctions across the airport rougher than before. **Invariant to hit:** every
taxiway centerline route smooth and within grade at all times; junctions
invisible (seamless rect→cap→junction). Aprons & buildings looked good — keep
them close to DEM.

## Test suite
`PYTHONHASHSEED=0 venv/bin/python -m pytest tests/ -q` → **5 failed / 359 passed**
(seed 0; 329 s) with the P2 keeper ON (default). The committed baseline before
this session was 2 failed. The 5:
- `rests_on_source[CYXY]`, `grade[HECA]` — **pre-existing** standing reds (the 2).
- `compare_target_splp[-13--77]`, `compare_target_splp[-13--78]`,
  `compare_target_spjc` — **EXPECTED**: the keeper changes shift junction solved
  values, so these fixtures no longer match. User will re-cut SPJC/SPLP once CYXY
  is right — do NOT chase them.
- `grade[CYXY]` — **NOW GREEN** (P2 resolved it, as predicted). It was the 6th
  failure pre-P2.

A/B reference (seed 0): `O4_CORRIDOR_SPINE_CHAINS=0` → **6 failed / 358 passed**
(grade[CYXY] red) = the exact pre-P2 baseline. So P2 gate-off ≡ baseline, gate-on
flips grade[CYXY] green with no new reds.

⚠ hashseed-flaky — always pin seed 0 for A/B.

## Probes (in /tmp, not tracked)
- `/tmp/probe_spine.py` — **node-accurate** G centerline profile (dist-from-E,
  alt). USE THIS, not the shape-level `/tmp/probe_gdem.py` (it misreads — samples
  the flat apron body, not the centerline).
- `/tmp/probe_clean.py` — within/cross/steps via `check_grade.run_checks` with
  per-axis axes + route_ctx (mirrors `test_pavement_grade`).
- `O4_SPINE_DEBUG=1` per-centerline (proj,ceiling,profile); `O4_BAND_KML=/p.kml`
  per-node band+provenance.

## Files touched
- `config.py` — 6 gates (above; +`CORRIDOR_SPINE_CHAINS` P2 committed,
  +`FIELD_ROUTE_BAND_BY_WIDTH` P3 default-OFF).
- `network_profile.py` — P3: `_runway_route_band` per-edge cap (gated).
- `taxi_routing.py` — `TaxiRouteGraph.edge_cap` + `_ekey`; per-letter caps in
  `build_taxi_route_graph`.
- `elevation_per_surface/unified_jacobi.py` — per-edge caps in
  `_runway_reach_bands`; `_collect_junction_axes`→caps + `_edge_narrow_cap`;
  **P2 spine-station chains** in `_taxi_corridor_profiles` (between the
  ≥2-station filter and STAGE B; gate `CORRIDOR_SPINE_CHAINS`);
  `_anchor_aprons_at_feasible_high` (spine+flat, to retire/merge);
  `_anchor_buildings_at_feasible_dem` + `_relax_buildings_and_resolve` (to
  retire); env-tunable DEM attraction.
- `elevation_per_surface/junction_spine.py` — `SPINE_PIECE_ROLE_REEVAL`.
- `layout.py` — reverted to original (`taxi_shape_code_letter` unchanged).
- `docs/taxi_centerline_grading_plan.md` — NEW, the plan.

# Taxi-network slack for flat terminals (multi-session feature)

**Branch:** `taxi-slack-terminals` (worktree `/Users/noah/Ortho4XP-taxi-slack`,
off `dev@de15311`). **Gate:** `TAXI_SLACK_TERMINALS` (config, default OFF →
byte-identical until shipped). Build/test with the main repo venv:
`/Users/noah/Ortho4XP-novemberlima/venv/bin/python3`.

## User ruling (2026-06-16) — the grade hierarchy this implements
1. **Buildings FLAT.** Raise/lower the flat pad to whatever level lets the
   aprons hold grade — never leave a terminal in a DEM canyon.
2. **Aprons 1% WHENEVER POSSIBLE** on the building↔taxi-corridor visible/
   geodesic chords (1% is the default target, not just "preferred").
3. **Aprons 1.5% ONLY when 1% is infeasible** even after spending slack.
4. **Taxi corridors take the steepness** — flex them STEEPER within their
   runway-anchored route bands so the apron stays gentle.
5. **Slope the building ONLY when no feasible taxi route band allows even a
   1.5% apron** (band-widened window inverts → genuine squeeze).

⛔ Supersedes the 4% back-edge-ramp approach: the user does NOT want 4% aprons;
the slack comes from the taxi network, not steep apron edges.

## Why today's solver fails this
Priority cascade: runway (hard) → taxi corridors → aprons → buildings (leaf).
Geometry only ever flows DOWN the cascade (aprons follow corridors, buildings
follow aprons); nothing pushes up. The corridor network-profile field
(`network_profile.py`) computes per-node feasibility **bands**
`[band_lo, band_hi]` = how far each corridor node may flex and stay
runway-grade-legal. **That band is the slack — computed but never spent for
terminals.**

The building-flat test `_terminal_chord_windows` (`unified_jacobi.py`)
intersects `[corridor_VALUE ± g·d]` over serving corridors at 1%/1.5%, using
each corridor's single SOLVED value. For a terminal straddling terrain
(corridors high one side, low the other) the window INVERTS → "genuine squeeze"
→ building slopes / sinks following the low side.

Measured slack (probe `tools/`-style `/tmp/probe_slack.py`, uses
`field.sample_band`):
- SPJC building19 (64k m²): fixed-win@1.5% **inverted 4.6 m** → band-win@1%
  **[23.4, 30.3] FEASIBLE** (~10 m corridor slack).
- OMAA building2 (450k m² main terminal, currently SLOPED −5.3→+16.4 m = the
  canyon): fixed @1.5% **inverted 9.7 m** → band-win@1% **[17.4, 25.7]
  FEASIBLE** (~15–20 m slack). Every OMAA terminal's band window is feasible.

## Phases (check off as completed)

### Phase 0 — Instrumentation & safety  ✅ (in progress)
- [x] `NetworkProfileField.sample_band(x,y) -> (lo,hi,gap)` (network_profile.py).
- [x] Probe `/tmp/probe_slack.py`: per terminal, fixed vs band-widened window
      @1%/1.5%, current building level. Baselines: SPJC, OMAA, HECA, CYXY.
- [ ] Config gate `TAXI_SLACK_TERMINALS` (default OFF, env `O4_TAXI_SLACK`).
- [ ] Confirm gate-off byte-identical vs `dev@de15311` (SPJC + HECA, seed 0).

### Phase 1 — Band-aware feasibility window  ✅ DONE (commit on branch)
- [x] In `_terminal_chord_windows`: per serving corridor add band-widened
      bounds `[band_lo − g·d, band_hi + g·d]` at g=1% and g=1.5% (sample the
      field band at the corridor foot). Window tuple grew 5→9; all 4 consumers
      (combine, `_chord_window_midpoint`/`_target`, validator) updated.
- [x] `_chord_window_slack_target(win9, cur9)`: 1% fixed > 1% band > 1.5% band
      > slope; clamp natural level into the chosen window (raises out of a
      canyon, lowers from a peak, else keeps — least movement + anti-canyon).
- [x] Gate-off BYTE-IDENTICAL vs de15311 (SPJC seed 0, proven).
- [x] Gate-on: SPJC building19 FLAT @30.3 (was sloped); OMAA building2 FLAT
      @17.4 (was sloped −5.3→+16.4 = the CANYON — now raised out of it).
- ⚠ As expected, aprons still steep here — corridors haven't moved yet, and the
      4% back-edge ramps (still on) mask it at the acceptance step. Phase 2+3
      make the corridors flex and the aprons 1%.

### Phase 2 — Corridor flex toward the target  ◐ FIRST CUT (WIP, committed)
- [x] Found the existing terminal pass in `build_and_solve` (~L1462): it is
      LIFT-ONLY on apron LANES toward a low pad floor — does NOT move corridors.
- [x] Added a `taxi_slack` param + a CORRIDOR-FLEX pass after the apron-lane
      pass: per terminal, band-widened 1% window → plane P = median serving
      corridor value clamped into it; flex each serving corridor NODE minimally
      toward `[P−g·d, P+g·d]` within its band; re-converge cap/rate.
- [x] Gate-off byte-identical (SPJC). Gate-on: SPJC building19 — flex fires 0
      nodes and aprons are already ≤1.5% (within 13→3): **Phase 1's band-window
      flatten ALONE solves the common case** (close corridors already ≈ the flat
      level, far ones have grading room).
- ⚠ **OMAA building2 (450k m² main terminal) is the STRESS CASE and NOT solved:**
      flattens to 17.4 (out of canyon ✓) and 7 corridor nodes flex, but the
      huge terminal's aprons break into 53% walls (apron #271/#332 at 21–25 m
      vs the 17.4 pad). Two root causes to fix next session:
      1. **Level choice too low.** Clamping the natural median (17.29) raises it
         only to 17.4 (band floor) — the HIGH-terrain aprons (21–25) then can't
         meet it. For a wide-straddle terminal the band-window MIDPOINT (~21)
         or a min-max-apron level is better. (HECA earlier showed midpoint can
         over-raise — needs a straddle-aware rule, not a blanket midpoint.)
      2. **Flex too weak + apron doesn't follow.** Only 7 nodes moved; the
         apron-internal solve still walls. Need: flex MORE serving corridors
         (and the apron lanes) and re-grade the apron from the flexed field
         (Phase 3), not just the corridor nodes. The 53% walls are
         apron-vert↔apron-vert, i.e. the apron field itself isn't reconciling
         to the flat pad.
- [x] **BALANCED level (user 2026-06-16):** `_chord_window_slack_target` now
      returns the MIDPOINT of the 1% fixed window (= the minimax level that
      equalises the worst up/down apron demand — "balance the elevation load"),
      clamped into the band window. DROPS the DEM-natural bias that sank OMAA
      b2. Field flex plane P matches it. SPJC b19 → 28.9 (was 30.3), within
      still 3. Gate-off byte-identical. (Both b19 & b2 have NO close corridors
      <60 m — all serving taxiways are >150 m, so balance over the whole spread
      is right.)
- [ ] **Phase 3 apron-follow — THE remaining gap.** OMAA check_grade: gate-off
      99 within (b2 sloped −5.3→16.2 = canyon) → gate-on 98 (b2 FLAT@17.4, out
      of canyon ✓) but the COUNT barely moved. The flex moves corridors (16
      nodes) yet the APRON SHAPES don't all re-grade to the flat pad: worst
      walls apron #271/#332 (21.5↔29.2, 110%) are present gate-OFF too
      (pre-existing, NOT terminal-caused) and the flex doesn't reach them. NEXT:
      after the corridor flex, the apron field/geodesic-corridor pass must
      re-grade the apron shapes from the flexed corridors + flat pad (the
      existing apron-lane LIFT pass is lift-only and pad-floored low). Also:
      flex MORE corridors (only 16 of OMAA's hundreds moved), and the
      corridor-plane attractor / two-rate band must target 1% off the flexed
      field. Investigate whether #271-class walls are decompose/narrow-strip
      aprons that never follow any plane.
- [ ] Junction / multi-terminal consistency; outer iteration. NOT done.

### Phase 3 — Aprons follow the flat pad  ◐ MOSTLY DONE
- [x] **Back-band OFF under TAXI_SLACK** (`_apron_back_band_nodes` returns ∅):
      no 4% relaxed strip — every apron vert is plane-attracted to the FLEXED
      corridor plane and capped 1.5%/1%. (OMAA 98→91.)
- [x] **CONDITIONAL network clustering** (user 2026-06-16: do NOT force all near
      buildings to one elevation — a string of buildings along a long corridor
      should step gently, the apron sloping ≤1.5% between them). Two terminals
      co-level ONLY when within `_TERMINAL_CLUSTER_REACH_M` (250 m) AND the apron
      can't bridge their INDEPENDENT balanced levels at the apron grade
      (|ΔL| > apron_grade·gap). Computes per-complex independent levels first,
      then conditional union. Replaces the pairwise co-level that OSCILLATED.
      The old pairwise device is SKIPPED under the gate (it over-raised
      building28 21→26.8 into a neighbour). `_chord_window_slack_target` now
      returns a flat COMPROMISE (inverted-band midpoint) instead of sloping —
      buildings stay flat. VERIFIED on OMAA: building24/25 → 16.2/17.2 (apron
      1.68% between), building17/18/28/31 step 21.9→23.6 (0.25–0.51%), while
      genuinely-tight groups share a level (building21/22/33 @13.9, b19/35
      @22.1). NOT "all near buildings same elevation".
- [x] Measured (gate-on vs baseline, gate-off byte-identical): **SPJC 2** ·
      **OMAA 99→57** (110% walls gone; 20 flat / 8 sloped buildings) ·
      **HECA 85→78 + mid-edge steps 2→0** — all improve.
- [ ] **Remaining (~57 OMAA):** junction `-10225` inside a terminal cluster
      that did NOT co-level (1.7 m step, ~8 viols); 8 buildings still SLOPED
      (acceptance reverts the flatten — apron can't follow even flexed); minor
      aprons (#235/#238 3–8%). NEXT: cluster-embedded JUNCTIONS follow the
      cluster level; decide whether to force the 8 reverted buildings flat
      (accept apron strain) per "all buildings flat"; flex more corridors.

### Phase 4 — Validation & tuning
- [ ] SPJC b19 flat + aprons ≤1%; OMAA b2 raised ~21 m out of canyon; HECA /
      CYXY / SPLP no regression. Full suite ≤ baseline failures (currently
      5f/344p @ de15311). Runway invariants exact. Deterministic across
      hashseeds 0/1/2. In-sim check by user. Flip gate ON.

## Risks / open questions
- **Corridors ARE the route-band reference** — flexing them changes the bands
  they were measured against. Staying inside each node's PRE-computed band keeps
  it runway-legal, but the flexed values must stay smooth along the taxiway
  (re-solve, not independent clamps). Deepest risk; most of Phase 2.
- **Multi-building / junction corridors** — compromise needed.
- **Co-level question (asked, awaiting answer):** buildings sharing a corridor —
  co-level, or each own level with the corridor compromising? Default assumption:
  each own level, corridor compromises; revisit if it looks wrong.
- **Determinism** (SPJC ~0.1 m hashseed noise); **concurrent session on `dev`**
  → this feature lives in its own worktree/branch.

## Handover state
Last updated: 2026-06-16. Phase 0 underway. Earlier exploratory 4% back-edge
change (`/tmp/uj_backedge.py`) is SUPERSEDED — kept only as a reference for the
7-tuple chord-window plumbing. `sample_band` saved in `/tmp/np_sampleband.py`.

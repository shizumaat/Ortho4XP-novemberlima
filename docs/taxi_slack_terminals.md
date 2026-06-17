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

### Phase 1 — Band-aware feasibility window
- [ ] In `_terminal_chord_windows`: per serving corridor add band-widened
      bounds `[band_lo − g·d, band_hi + g·d]` at g=1% and g=1.5% (sample the
      field band at the corridor foot). Return them in the window tuple.
- [ ] `_chord_window_*`: choose flat L = natural level clamped into the 1% band
      window (fall to 1.5% band window only if 1% inverts; slope only if 1.5%
      band inverts). Bias L to (a) least corridor movement, (b) prefer RAISING
      over sinking (anti-canyon).
- [ ] Verify buildings pick good flat targets (SPJC b19 ~29, OMAA b2 ~21).
      NOTE: aprons still steep here — corridors haven't moved. Not shippable
      alone; this only sets the target.

### Phase 2 — Corridor flex toward the target  (the core)
- [ ] Inspect existing `term_polys` / `chord_grade` handling inside
      `network_profile.build_and_solve` (already passed in) — extend vs add.
- [ ] Per serving corridor derive a target value = L clamped into
      `[L−g·d, L+g·d] ∩ [band_lo, band_hi]`; inject as a SOFT demand on those
      corridor nodes (reuse the runway-flex `demands` / `extra_band_anchors`
      plumbing). Re-solve so corridors flex toward the building while:
      (a) staying inside their bands (runway-legal), (b) smooth along each
      taxiway ≤1.5% (no new kinks — must be a re-solve, not per-node clamps),
      (c) consistent at junctions, (d) a corridor serving TWO terminals
      compromises.
- [ ] Outer iteration (1–2): solve corridors → choose L → demand → re-solve.
      Bounded; converge on band midpoints.

### Phase 3 — Aprons at 1% from the flexed field
- [ ] Retarget the corridor-plane attractor (apron-follows §2b) to 1% default
      (1.5% only where the band forced it).
- [ ] Retire the five 4% back-edge-ramp touch-points + the back-band machinery
      (or keep gated under the old flag for fallback).
- [ ] Flatten-acceptance metric: accept when apron ≤1% (≤1.5% where forced).

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

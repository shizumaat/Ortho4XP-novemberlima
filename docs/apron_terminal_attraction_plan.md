# Plan — settle terminals in the feasible middle + attract aprons to them

> ⚠ **SUPERSEDED (2026-06-30 audit) — never built.** Its model rested on
> `_terminal_chord_windows` in the deleted `unified_jacobi.py`. The terminal-leveling
> problem was instead solved by `TAXI_SLACK_TERMINALS` (see `taxi_slack_terminals.md`).

**Status:** PLAN (not built). Supersedes the level-picking parts of
[`apron_back_edge_ramps.md`](apron_back_edge_ramps.md); the back-edge corridor
relaxation + the KML band tool from that work are inputs here.

**User direction (2026-06-14):** "If the bands all look fine, providing a wide
range of feasible elevations, why are we not solving somewhere in the middle?
Seems like we should be able to have aprons within grade with both terminal9 and
terminal11 anywhere from 90 to 100." → Document the plan for making aprons
**attracted to terminals** once the terminal flat-feasible level is settled in
the **middle of the band**.

---

## 1. What the investigation established (the KML confirmed it)

For HECA terminal9, generate the band KML (`O4_BAND_KML=out.kml` on a build) and
the apron vertices read:

```
runway-route band: [82.6, 100.3]   FEASIBLE   (ceiling from rwy node 905 / floor from 837)
effective (field) band: [88.5, 96.7]
```

* **The routes are correct** — the runway-route band is wide and *includes ~98*.
  terminal9 flat at 98 IS feasible.
* **The apron settles at the LOW end (~96), not the middle.** Two causes:
  1. **One-way cascade.** The solve order is network-profile field → apron →
     terminal. The apron is governed by the tight **field band** (= the taxi-
     corridor level ~96), not the wide runway band, and it is *attracted to the
     taxi field*. Nothing pulls it up toward a terminal. So it sinks to ~96 and
     the terminal inherits ~96.
  2. **Widening ≠ rising.** Widening the back band to the runway range (built,
     `bb9` in the enforce) makes ~98 *allowed* but the solver still places the
     apron at the field/DEM level within the band — permission without a pull.
* **Band coverage misses the hill-cut side.** The inter-terminal corridor band
  only covers apron *between* building pairs. terminal9's residual excess is on
  its **south side** (the hill it should cut into), which is not between two
  buildings, so it stays field-pinned.

So the feasible middle exists; the architecture never explores it.

## 2. The model

Three coupled changes, all under `APRON_BACK_EDGE_RAMPS`:

### A. Settle each terminal flat at the level its TAXI CORRIDORS dictate

★ User refinement 2026-06-14: **the terminal's DEM is irrelevant.** The flat
level is the elevation that simultaneously allows:
1. the terminal to be **flat** (one level), AND
2. the apron to meet it at **≤1 %** on **every taxi-corridor-facing edge**, AND
3. the **taxi corridors stay within grade**.

So the level is a function of the (in-grade) **taxi corridors**, not terrain.
This is the perpendicular-chord window (`_terminal_chord_windows`, already in
the code) **restricted to corridor-facing chords, at the 1 % rate**:

* For each taxi centerline chord that crosses the pad on a **taxi-corridor-
  facing** edge, sample the corridor / network-field value at the foot and form
  `[v − 0.01·d, v + 0.01·d]`. Intersect over all such chords → the pad's
  **1 %-corridor window**. The corridor values come from the network-profile
  field, which is already solved in grade (requirement 3 holds by construction;
  if a corridor itself is out of grade that is a separate network-field bug).
* **Exclude the back / hill-cut / between-building edges** from this window —
  those are NOT corridor-facing and are free to ramp at 4 % (the back-edge
  relaxation, §B/§C). Only the taxi-facing edges pin the level.
* **Pick a value inside the window** (the middle of the feasible 1 % window).
  This is the elevation where the corridor-facing apron grades at exactly ≤1 %
  to every serving taxiway — the terminal sits where its taxiways put it.
* **Co-level adjacent terminals** — close pads (`_INTER_TERMINAL_ADJ_M`) share
  the larger pad's level (built: co-level-big).
* **Infeasible window** (serving corridors at conflicting levels, 1 % window
  inverts even after the corridors use their in-grade slack): the terminal
  **slopes** — last resort.

Key point: the level is computed from the **taxi corridors at 1 %**, NOT from
DEM and NOT from where the apron happened to settle. (This is why DEM-median was
wrong: terminal9's DEM hill is ~100 but its corridors put it at ~98.) The wide
runway-route band (§B) is the *outer* legality bound for the back apron; the
*corridor-facing 1 % window* is what sets the terminal level.

### B. Back band = ALL apron around a building, on the runway band

Redefine `_apron_back_band_nodes`: an apron vertex is back-band if it is within
`APRON_BACK_BAND_DEPTH_M` of ANY building frontage — frontage + between-buildings
+ **the hill-cut side** — minus the taxi front guard. (The inter-terminal
convex-hull definition was too narrow; the hill-cut side must be included.) Back
nodes are governed by the **runway-route band** (not the field band) and may
grade to `APRON_BACK_EDGE_GRADE` (4 %). Taxi-facing apron keeps the field band
and 1 %.

### C. Two-attractor apron (the new pull)

Today the corridor pass attracts every in-zone apron vertex to the **taxi-field
plane**. Add a second attractor: **back-band apron vertices are attracted toward
the nearest flat TERMINAL level** instead of the taxi field.

Per apron vertex, pick the attractor by physical proximity / membership:
* **taxi-facing** (front guard / near a corridor): attract to the taxi field at
  1 % (unchanged).
* **back-band** (near a building, not the front): attract to the adjacent flat
  terminal's level, clamped into the runway band, at up to 4 %.

The result is the **twist**: the apron grades from the taxi corridor (~96, 1 %)
up to the flat terminal (~98, ≤4 % on the back), meeting it on every side
including the hill cut. The attractor is a *target* in the relief slot; the band
clamp + the `_project_within_bands` pass enforce legality after, exactly like the
existing corridor-plane attractor.

### D. Arbitration / acceptance

With the apron actively pulled to the terminal, the FLAT-vs-SLOPE acceptance
should rarely fire — but keep it (with `_BACK_ACCEPT_BUDGET_M`) as the last-
resort guard: if even the two-attractor apron can't reach the terminal within
4 % (genuine squeeze), the terminal slopes.

## 3. Implementation map

| Step | Site | Change |
|---|---|---|
| A | `_terminal_chord_windows` / INHERIT level pick (`unified_jacobi` ~L2230) | Pad target = **middle of the 1 %-corridor window** (corridor-facing chords only, sampling the in-grade network field), NOT DEM and NOT the settled-apron median. `_terminal_chord_windows` already builds the chord window — restrict it to corridor-facing edges, take the window midpoint, drop the chord-clamp-of-current. Keep co-level-big + slope-on-infeasible. |
| B | `_apron_back_band_nodes` | Back band = apron within `APRON_BACK_BAND_DEPTH_M` of any building frontage (all sides), minus front guard. Drop the convex-hull-pair restriction. |
| B | enforce band step (`bb9`, built) | Back nodes use runway band — already wired; keep. |
| C | corridor-plane attractor (`unified_jacobi` ~L2085) | Add the terminal attractor: back-band vertices target the nearest flat-terminal level (clamped to band) instead of the taxi-field plane. |
| D | acceptance (`_inherit_flat` loop) | Keep budget guard; expect far fewer reverts. |
| — | validator `check_grade` | Back-band exemption already corridor-hull-based; update to the frontage-depth band to match B. |

## 4. Validation

* HECA: terminal9 + terminal11 **flat at ~98** (your target), apron grading 96→98
  smoothly on all sides incl. the south hill cut; within-shape near baseline
  (the +39 walls from the low-flatten should largely resolve once the apron
  *rises* to meet the terminal instead of walling).
* Regenerate the band KML; back-band apron effective bands should now reach the
  runway ceiling (not pinned at the field 96.7).
* CYXY / SPJC / KPHL + full suite; gate-off byte-identical.

## 5. Rejected / why not simpler

* **Full seed model** (`O4_TERMINAL_NATURAL=0`) — pins terminals flat but with
  the *one-way* apron-follows-DEM, so it shoved grade into the apron as walls
  (HECA 300 within-shape, 27 % walls). The difference here is the **two-attractor
  apron** that rises to meet the terminal, so no walls.
* **Band widening alone** (built) — necessary (permission) but not sufficient
  (no pull). Keep it as the band layer; the attractor is the missing pull.
* **Co-level / acceptance-budget alone** (built) — flatten terminal9 at the
  *low* end (95.4), which the user rejected as too low. The middle-band target
  (step A) is the fix.

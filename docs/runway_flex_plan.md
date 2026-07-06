# Runway flex — design plan (user-approved 2026-07-06)

## The ruling

> All we know for certain is the CIFP threshold anchors and the tile
> seam anchors.  Enhance the algorithm to solve feasibility of the
> intermediate runway anchors themselves.  If there's only one runway,
> then it's as it is currently, but when there's more than one runway,
> they connect through the taxi route graph, and all connections must
> be feasible within grade — flex the runway (only within the grade
> law) to facilitate the taxi route staying feasible.  Particularly
> noticeable at HECA.

Related ruling, same session: **seam values sample the SMOOTHED DEM**
(`elevation._sample_dem` → `dem.alt`), never raw HGT / `alt_strict`
(landed in `95347fb`).

## Current model (what changes)

The runway profile is solved FIRST and then frozen: CIFP thresholds
(+displaced positions), runway↔runway crossing reconciliation values,
and tile-seam samples are all immutable anchors; `faa_joint_solve`
smooths the DEM-seeded free samples between them (1.5 % grade cap,
0.8 % end zones, K-factor curvature).  The elevation field then grades
the whole airport TO the frozen runway values.  Where the field cannot
reach two frozen runway contacts within taxi-law grade, the pocket is
declared broken and blended (HECA: ~11k break-region pairs across 14
pockets on ~50 m of DEM relief).

## Target model

Anchor taxonomy:
- **Certain (hard)**: CIFP threshold altitudes (at their displaced
  positions) and tile-seam samples (smoothed DEM).  These stay
  immutable, with the existing `regrade_runway` threshold-shift as the
  only escape valve (CIFP as soft preference clipped to feasibility) —
  and that shift must learn to move DISPLACED thresholds, not just the
  physical-end samples (the SPLP 1.58 % gap).
- **Solved (flexible)**: every other profile value — interior samples
  (DEM = preference, not pin) AND runway↔runway crossing values (the
  crossing remains an EQUALITY constraint between the two profiles; its
  VALUE joins the solve).

Coupling: runway↔taxi contacts (the edge-crossing anchor nodes) tie
the taxi route graph to the profiles.  Feasibility demand flows BOTH
ways: the field still grades to the runway, but where the route graph
between two contacts is infeasible at the current profiles, the
profiles flex — strictly within the runway law (1.5 % / end zones /
K-factor) and never past the certain anchors.

**FLEX-LAST RULE (user 2026-07-06)**: the runway is the STIFFEST
member and flexes ONLY when the connecting taxiways are already at
their max caps.  Operationally: a contact's demand interval is the
reach envelope computed with every route edge at its FULL legal
budget — the envelope IS "taxiways at max cap" — and the profile
flexes only to the NEAREST edge of that interval (zero when the
current value is inside it).  The runway never moves to make taxi
grades gentler, only to make an otherwise-infeasible connection
feasible, by the minimum amount.

Single-runway airports with no infeasible contacts: byte-identical
behaviour (the flex solve is a no-op when all demands are satisfiable
at the seeded profile).

## Staged implementation (gate `O4_RUNWAY_FLEX`)

**Stage A — instrumentation (no behaviour change).**  Per runway
contact, compute the DEMAND INTERVAL: the [floor, ceil] the taxi route
graph imposes at that contact from all NON-runway anchors (reach
envelope over the route graph with runway anchors removed).  Report
per-contact deficit (current profile value vs interval).  Measured at
HECA this maps which pockets a flex can drain and by how much; it also
quantifies the SPLP displaced-threshold case.

**Stage B v1 MEASURED (2026-07-06, gate `O4_RUNWAY_FLEX` default
OFF)**: contact-pair flexing implemented (hook in
`solve_route_profile` + `runway_redistribute.apply_runway_flex` /
`flex_slack_at`).  At HECA it drains the full 8.50 m contact deficit
(2 pairs, 4 contacts, 05C/23C + 05L/23R) — but the quarantine only
moves 11,265 → 10,838 and 7 small new actionable residuals appear.
FINDING: the pocket contradictions press against the WHOLE profile
(every runway node is a hard envelope anchor), not just the taxi-join
contacts.  **Stage B2**: demands must be ENVELOPE-LEVEL along the full
profile — for each runway profile sample, the [floor, ceil] the rest
of the field's certain anchors impose through the max-cap graph; the
profile then re-solves against those interval targets (still through
`faa_joint_solve`, certain anchors hard).  Equivalently: runway
interior nodes become interval-constrained members of the field solve
with runway-law edges along the axis.

**Stage B (original sketch) — two-pass profile flex.**
1. Field pre-solve (current pipeline) → contact demand intervals.
2. Per runway (or crossing-coupled runway GROUP): re-run
   `faa_joint_solve` with contact demands folded in as SOFT targets
   (interval-clipped, weighted below the DEM preference so the runway
   only moves where a connection is otherwise infeasible), certain
   anchors hard, crossing equality maintained within the group.
3. Re-freeze the flexed profiles; final field solve as today.
   Cross-tile parity: both tile builds run the same whole-airport
   passes on the same inputs → identical flexed profiles.

**Stage C — displaced-threshold shift + crossing groups.**
`_shift_thresholds_for_seams` (and the flex pass) treat the DISPLACED
CIFP threshold sample as the movable threshold; crossing-coupled
runways solve as one system (HECA's 05/23 family).

**Gates per stage**: HECA break-region count (expect large drop in
Stage B), HECA/SPLP actionable, longitudinal grade test (profile must
stay law-true — the test is the flex law's own gate), CYXY/SPJC
unchanged (single-runway feasible airports), full suite vs base10.

## Notes

- The K-factor lives in `faa_joint_solve`, so flexing through it (not
  through the field's pairwise projection) keeps curvature enforcement
  without new constraint machinery.
- The persisted `_runway_redistributed_profiles` (clamp floor,
  tile_cut rewrites) must be refreshed AFTER the flex pass.
- The emitted profile remains flat across the runway width; only the
  longitudinal shape flexes.

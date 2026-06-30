# Elevation solver — auto_patch's core component

> **⚠ CURRENT (session 49): the RELIEF changed.** Phase 2 is now a **directional
> shape-cascade** (`_directional_relief`), NOT the stiffness-weighted cap projection
> described below — that symmetric relief over-dropped compliant nodes to the lowest
> feasible surface and is **superseded**. The new relief propagates compliance OUTWARD
> from the runway/seam anchors, building on phase 1 (no reseed), solving each shape as a
> unit (terminals rigid, aprons/junctions flex as compliant all-pair surfaces). It SOLVES
> SPLP but CYXY's non-convex excavated-terrace apron cluster still shears — next steps are
> cutting non-convex transition aprons + a force-hierarchy leaf network. **See `STATUS.md`
> for the live state + plan.** The cascade (phase 1) + model + rejected-approaches below
> are still accurate; only the phase-2 "Relief bounce" mechanism is replaced.

This is the authoritative reference for how auto_patch grades pavement elevations.
It supersedes and merges the two earlier design docs (`ELEVATION_FIELD_PLAN.md`,
`docs/elevation_per_surface_redesign.md`), both of which described designs that were
implemented, measured, and then superseded — see **History** at the bottom.

**Active solver:** `src/auto_patch/elevation_per_surface/route_profile/` →
`solve.solve_route_profile` (one elevation profile solved on the single unified
grade graph; see `one_solve.py`, `building_feasibility.py`, and the
elevation-neutral primitives in `elevation_per_surface/solver_primitives.py`).
The retired `unified_jacobi.py::solve()` is no longer the active path. Rule
*values* live in `config.py` (`ROLE_GRADE_LIMITS`, runway caps, the
`O4_ANISO_EDGES` anisotropic-edge gate); the within-shape law is
`grade_law.py`/`grade_graph.py` and the validator is `tools/check_grade.py` — all
read the same constants and the same `grade_graph.shape_constraints` (solver ↔
validator stay in lockstep, incl. the baked anisotropic allowance).

---

## The model (authoritative — match this)

The **DEM is the natural terrain, NOT the graded airport.** auto_patch *grades*: it sets
elevations to whatever the FAA/EASA slope rules require, **overwriting the DEM wherever
necessary, attracted to the DEM where possible.** Authority hierarchy (who is truth vs.
who yields):

1. **Tile-seam boundary** elevations — HARD (immutable; shared across adjacent tiles).
2. **Runway thresholds** — HARD (CIFP / surveyed; published truth). Runways get an FAA
   vertical profile (CIFP threshold alts → `runway_regrade` → `runway_redistribute`).
3. **Terminals** — a **STIFF soft anchor** seeded at the footprint's centroid DEM. It
   holds that level and yields **only the minimum, and only when the solver cannot make a
   grade-compliant path back from the runway.** (Not HARD — it can adjust; not freely
   soft — it must not be dragged down by a low connection.)
4. **Aprons / taxi** — flexible; they grade *away* from the anchors, following the DEM but
   **capped** wherever a DEM elevation would break the slope limit. The cap **overwrites**
   the DEM (an apron cannot descend faster than the cap from the stiff terminal/runway, so
   it stays near terrain instead of sinking to a false-low DEM reading).
5. **Continuity** — only at **shared vertices** (one elevation via the canonical-point
   registry). No Euclidean/graph-distance propagation through unrelated surfaces.

## Grade rules per role (`config.py` `ROLE_GRADE_LIMITS`)

- **Runway:** FAA vertical profile; `MAX_RUNWAY_GRADE` 1.5%, `RUNWAY_END_GRADE` 0.8%.
- **Taxi rects** (`primary_parallel` / `secondary_parallel` / `stub` / `cross_connector`):
  1.5% **along the source_axis**, flat cross-section (the `[H,L,L,H]` short-end convention).
- **Junction:** 1.5%, multi-directional (all-pair within the polygon — a junction may
  slope along several converging routes).
- **Apron / stand:** 1.5% all-pair (the apron-grade-standards research argues stands
  should be 1.0% and taxilanes 1.5% along-axis — see `docs/STANDARDS.md`; currently a
  single 1.5% all-pair cap is used).
- **Terminal:** flat.
- Tunnel/groundside 4%; boundary/wall/clearance = no cap.

A plane only ever travels along one surface's axis at a time; a constraint that doesn't
model a real travel path is wrong. **all-pair-Euclidean ≡ "≤grade everywhere" for a
CONVEX polygon** (straight chords stay inside); it over-restricts non-convex/winding
shapes, but the stiffness relief (below) handles the practical cases without splitting.

---

## Architecture: cascade + relief

### Warm-start cascade (`_node_tiers`, `_run_phase`)
An **inverted** priority cascade, each tier frozen as the next grades against it via shared
HARD nodes: seam/runway HARD → **TERMINAL** (flat plane at DEM-mean) → **APRON** (grades
outward from the frozen terminal, follows DEM up to cap) → **TAXI** (grades from the frozen
aprons to the runway/seam anchors; rect cross-sections stay flat). Tiers:
`_TIER_TERMINAL(3) > _TIER_APRON(2) > _TIER_TAXI(1)`; runway/seam = HARD (tier 0).

### Relief "bounce" — stiffness-weighted cap projection (THE key mechanism)
The cascade freezes each tier, so a taxiway bridging a low runway and a high apron can be
forced over grade with no recourse. The relief comes back the other way:

- **Every pavement node is SOFT** (only runway/seam stay HARD), re-solved by
  `_compliant_spread_fit(..., stiffness=…)`.
- **Stiffness-weighted split:** at an over-grade edge the correction is distributed so the
  *stiffer* node moves *less* — `fu = kv/(ku+kv)` is u's share (large when v is stiffer).
- **Terminal nodes are STIFF** (`_RELIEF_TERMINAL_STIFFNESS = 20.0`); aprons/taxi = 1.0.
  The terminal therefore holds its DEM-centroid; the flexible apron/taxi side absorbs the
  grade, and the cap **overwrites the false-low DEM**. The terminal yields only where a
  compliant path genuinely can't be made — and then by the minimum.
- A stiff anchor yields ~1/stiffness per sweep, so the relief gets a larger iteration
  budget: `_RELIEF_MAX_ITERS = 12000` (vs `_L2_MAX_ITERS = 2000` for cascade tiers).
- `reseed=True`; terminal flat-groups + rect cross-section flat-groups are eq-pairs.

**Verified (CYXY):** terminal **697.0** (the highest compliant value for the ~250 m
pavement path to the 693 m runway: 693 + 250·1.5% ≈ 697), **within-shape grade = 0**. With
the terminal wrongly soft it sank to 692; forced HARD at 700 it left ~26 break violations.
697 is "the minimum yield for a compliant path" — exactly the model.

---

## Key understandings (non-obvious — read before changing the relief)

- **False-low DEM is real and common.** CYXY's runways sit ~693–706 m built up on a
  plateau while the raw DEM there reads the ~667–688 m valley. The stiff anchors + grade
  cap **overwrite** that false low. A solver that *follows* or *sinks to* the DEM parks
  pavement in the valley and is wrong.
- **The terminal's correct value is its highest-compliant level, not its DEM** (697, not
  700 and not a dragged 692).
- **Terminal-free aprons sit at their highest-compliant level, which can be well below DEM
  and that is CORRECT, not an over-drop.** CYXY south aprons ~711 (DEM 722) — 722 is
  genuinely infeasible, 17 m above the SE runway ends they connect to over too short a path.
- **Ortho4XP bakes the patch by INTERPOLATING interior elevation from the boundary nodes**
  (`INTERP_ALT`: `O4_Vector_Map.py` insert_way + `O4_Mesh_Utils.py:301`). Triangle4XP adds
  interior Steiner points and linearly interpolates the boundary altitudes — interior
  points never sample DEM. So we control the surface entirely through boundary node
  altitudes; a large apron is a smooth boundary-interpolated surface (good for grading).

## The one tension
On terrain the airport was graded against, **correct-looking elevations (follow DEM) and
strict ≤grade compliance cannot both hold for a continuous draped surface** — real airports
cut/fill, so the compliant surface deviates from the natural DEM by design. The stiffness
relief resolves it the way intended: anchor the truth (runway HARD, terminal stiff at
DEM-centroid), overwrite the DEM via the cap, let flexible pavement absorb the grade,
yielding the stiff anchors only the minimum. Do **not** reintroduce a "just follow the DEM"
mode — it produces hundreds of within-shape violations wherever the DEM is steeper than the
cap.

## Approaches tried and REJECTED (do not repeat without reading why)

- **Soft terminal (no stiffness):** dragged DOWN to the false-low apron/valley chain (CYXY
  terminal 692, below the 694 runway). Root bug — a plain global re-solve sinks the terminal.
- **Pure cap projection (no DEM/stiffness bias):** compliant but picks the LOWEST feasible
  surface → over-drops aprons ~11 m below DEM.
- **DEM attraction, non-decaying (`_run_jacobi` use_attraction):** correct elevations
  (terminal 700) but ~296 grade violations — it follows a DEM steeper than 1.5% in many
  places. Decaying attraction: lift fades, cap re-collapses it.
- **Ceiling-spread** (`min(DEM, min_j(elev_j+cap))`, Bellman-Ford down from DEM): the
  highest compliant ≤DEM surface, but propagates the false-low into the terminal → 692.
- **Floored ceiling-spread** (don't drop > floor below DEM): ~657 violations wherever the
  natural DEM exceeds the cap.
- **Terminal forced HARD at 700:** ~26 unavoidable break violations at the short
  terminal→runway transitions.
- **Apron subdivision into ≤150 m runway-aligned grid pieces:** did NOT fix the over-drop
  (pieces share nodes ⇒ still one connected network) and added sliver within-violations.
  (If ever revived for genuine multi-level terraces, pieces must be DISCONNECTED at grade
  breaks — separate nodes + a wall/ramp — not shared-node grid cells.)

## Config knobs (legacy `unified_jacobi.py` — retired; see `route_profile/` for the active solver)
- `_RELIEF_TERMINAL_STIFFNESS = 20.0` — higher = terminal holds DEM harder / yields less.
- `_RELIEF_MAX_ITERS = 12000` — relief iteration budget (stiff anchor converges slowly).
- `_compliant_spread_fit(..., stiffness=<list>)` — the weighted cap projection.
- Cascade tiers in `_node_tiers`; per-role caps in `config.py` `ROLE_GRADE_LIMITS`.

## Related: SPJC degenerate sloping-rect guard
`_writeback` encodes a 4-corner shape as `altitude_high/low` only when
`_rect_short_ends_perpendicular()` confirms its two axis-end edges are perpendicular to the
`source_axis`. A tapering-wedge sub-rect (opposite sides not parallel, e.g. from
junction-splitting) falls back to `node_altitudes` instead of a bogus hi/lo that tilts
across a perpendicular edge.

---

## History (superseded designs — kept for context, do not implement)

- **Per-polygon elevation field, Mode A/B grid smoothing** (was `ELEVATION_FIELD_PLAN.md`,
  2026-04-26, in the pre-auto_patch `O4_Airport_Pavement_Builder.py`). Mode B
  (2D-grid smoothing within each polygon) was implemented behind
  `USE_PER_POLYGON_ELEVATION_FIELD` and **disabled** — it did not deliver the promised
  reduction. Durable lesson: many within-shape violations come from **HARD-anchor
  disagreements** — two rect corners a few metres apart in 2D differing by >1.5%/m because
  the centerline graph that seeded them is compliant over *network* distance, not 2D. That
  whole module is gone; the current cascade/relief replaced it.
- **BFS-from-runway phased per-surface solver** (was `docs/elevation_per_surface_redesign.md`,
  2026-05-02). This was the original intent for the `elevation_per_surface/` package
  (phase files `dem_targets.py` / `axial_profile.py` / `bfs_propagate.py` /
  `junction_field.py` / `continuity.py`). The implementation **diverged** into the inverted
  cascade + bounce in `unified_jacobi.py`; those phase files were never built. Durable
  carry-overs: the per-axis problem statement and the per-role rule table (above), and the
  "min over (DEM target, anchored-end + grade·dist)" intuition (now realized by the cap
  projection).

# Junction centerline spine — maintain ≤1.5% grade along taxi corridors through junctions

**Status:** P0 committed (`13a58a3`); **P1+P2 committed (`f453c52`) and VALIDATED**;
P3 (conformance) IN PROGRESS; P4 pending. Branch `junction-centerline-spine`.
**Gate:** `JUNCTION_CENTERLINE_SPINE` (config, env `O4_JCT_SPINE`, default OFF
→ gate-off byte-identical, proven CYXY MD5-match vs HEAD).

## Progress / measured state (2026-06-17, PYTHONHASHSEED=0)
**P2 SUCCESS — the decisive metric is fixed.** OMAA taxiway H @ junction `-10225`:
emitted surface grade was `1.27 → 2.12 (bulge) → 0.13 (flat) → 3.58%` (the user's
spike); at gate-ON it tracks the field's flat **1.50%** (max 1.68%).

**P3 DONE — the triangle fan was REPLACED by the user's rib-quad model** (`b9ea6cc`),
which keeps the grade fix AND restores conformance to ~the ring baseline. The fan poked
unconstrained-Delaunay chord edges across concave junction boundaries into neighbours
(28 crossings) — abandoned. `src/auto_patch/junction_spine.py` now:
- Densifies spine nodes along each crossing centerline at `SPINE_STEP_M`, **strictly
  INBOARD** (first ~one interval in, NONE on the boundary). This is the key conformance
  move — the crossing point is the interior of the feeding rect's flat edge where a node
  is illegal (`test_no_vertex_on_sloping_rect_flat_edge`); inboard placement leaves the
  entry as a gap polygon bounded by the rect's existing corners. (191→3 T-junctions.)
- Casts a perpendicular **rib** from each spine node to the junction edge. A rib hitting
  boundary shared with a sloping rect/runway (`near_hard`) reuses an existing corner; on
  apron/free boundary it may land a new node.
- Polygonizes `{spine polylines ∪ ribs}` into corridor quad strips + gap polygons.
- **Conformance-heal:** `enforce_conformance(owner_roles={junction,apron})` welds the new
  apron/junction-shared boundary nodes into the neighbour (collinear, altitude-neutral);
  rects are excluded (sloping invariant) and stay conformant via the corner-reuse rule.

**Measured (gate-ON):** OMAA conformance **191/51 (naive rib) → 3 T-junc / 5 crossings**
(ring baseline 2/2); CYXY **conformance-clean (0/0)**; H grade tracks 1.50%. OMAA 45
junctions → 1216 pieces (was 2285 fan triangles). **Gate-OFF byte-identical** (CYXY MD5
match vs HEAD).

**P4 OPEN:**
- Residual 1 T-junc + 3 crossings over baseline at OMAA — chase to zero.
- within-shape 29→55, route-band 1→36: mostly per-piece counting inflation (one junction
  → ~27 pieces each counted) + the lateral spine-vs-boundary seam; confirm how much is real.
- Multi-airport (SPJC/HECA/SPLP) conformance + grade; mesh-tri count
  (`tools/mesh_region_tris.py`); determinism; add a gated-on test. Then gate ON.

## Problem (diagnosed)
The corridor solver (`network_profile.py`) already solves every taxi centerline's
elevation profile **correctly** — a clean ≤1.5% ramp. But junctions are emitted as
**ring polygons** (per-vertex `node_altitudes` on the boundary only; see
`triangulation.py`), and X-Plane/Triangle4XP triangulates the interior by
interpolating between those boundary vertices. A centerline passes through the
junction *interior*, where there are **no vertices on it**, so the rendered surface
waves instead of tracking the corridor profile.

**Evidence (OMAA, taxiway H through junction `-10225`):** the field is a constant
**1.5%** from 22.87→24.19 to meet the runway, but the emitted junction surface
along H goes 2.1% → 0.2% (bulge) → **3.6%** (the user's reported steep spot at
~24.4370, 54.6466) → 1.1%. Diagnostic: sample `field.sample` vs the emitted
triangulated surface along ONE centerline (reliable; the global `/tmp/cl_grade.py`
sweep is too noisy — barycentric on an approximate triangulation).

## Goal
≤1.5% (1% preferred) along **every taxi corridor of the FULL route graph**
(apt centerlines + the 44 discovered/unreferenced ones), *through* junctions, in
the **emitted/rendered** surface — not just in the solver field.

## Approach — self-triangulated junctions with a centerline spine
X-Plane renders triangles as-is, so the clean way to put regularly-spaced nodes on
the interior centerline is to **emit each junction as our own constrained triangle
fan** instead of one ring:
1. Collect every centerline (full route graph) crossing the junction polygon.
2. **Spine nodes:** densify each crossing centerline every ~12 m inside the
   junction; assign each spine node the **network-profile field value** at that
   point (the smooth ≤1.5% profile the solver already produced —
   `layout._network_profile_field.sample`).
3. **Constrained triangulation** of the junction polygon with the spine nodes +
   boundary vertices as constraints (shapely/`triangle` constrained Delaunay,
   respecting centerline edges so triangles don't cross between two centerlines at
   different levels). Boundary vertices keep their current solver altitudes.
4. **Emit the triangles** (3-vertex polygons) in place of the ring. The surface now
   follows the spine → the centerline grades ≤1.5% through the junction.

## Phases
- **P0 — instrument + gate.** `JUNCTION_CENTERLINE_SPINE` gate (default OFF, byte-id).
  Turn the field-vs-emitted-along-one-centerline check into a reusable probe/tool.
  Baseline: OMAA H@`-10225`, plus a runway-crossing and a simple 4-way junction.
- **P1 — spine geometry.** Per junction, collect crossing centerlines (full graph),
  densify, sample field → spine node list with elevations. No emit change yet;
  dump/verify the spine elevations are the smooth ≤1.5% profile.
- **P2 — constrained triangulation + emit.** Triangulate junction with spine; emit
  triangle fan. Verify the emitted surface along H is now ≤1.5% end-to-end.
- **P3 — conformance.** Make the triangle fan pass the conformance/T-junction
  invariants (shared edges with neighbouring rects/aprons; no T-junctions; the
  spine endpoints weld to the entering rect centerlines). This is the hard part —
  `conformance.py`, `junction_repair.py`, the "conformance invariant NOT met"
  warnings. Watch mesh triangle count (`tools/mesh_region_tris.py`).
- **P4 — validate.** SPJC/OMAA/HECA/CYXY/SPLP: centerlines ≤1.5% through junctions;
  no new check_grade within/cross/step regressions; runway invariants exact;
  deterministic; gate-off byte-identical; full suite ≤ baseline. Then gate ON.

## Key files
- `src/auto_patch/triangulation.py` — junction per-vertex altitude (today's ring
  model); the spine triangulation lives here or a sibling.
- `src/auto_patch/junction_emit.py`, `junction_rules.py`, `junction_repair.py` —
  junction build/emit/repair.
- `src/auto_patch/elevation_per_surface/unified_jacobi.py` — `_collect_junction_axes`
  (~L9248, full-graph axis source; NOTE `layout.apt_taxi_centerlines` is snapshotted
  in `pipeline.py` L1462 BEFORE the discovered centerlines at L1651 — include
  `_discovered_centerlines` for the full graph), per-axis junction grading.
- `src/auto_patch/network_profile.py` — `NetworkProfileField.sample` (spine values).
- `src/auto_patch/layout.py` — `to_osm` (triangle-fan emit), conformance.
- `src/auto_patch/conformance.py` — shared-edge / T-junction conformance.

## Build & test
- Worktree + main-repo venv: `/Users/noah/Ortho4XP-novemberlima/venv/bin/python3`.
  Run the WORKTREE's `tools/build_target_osm.py` (puts its own `src` on the path).
- `O4_JCT_SPINE=1 ... tools/build_target_osm.py OMAA --out /tmp/x.osm`; validate with
  `tools/check_grade.py`. The decisive metric is **field-vs-emitted grade along a
  centerline through a junction** (not the global within-shape count, which is
  dominated by harmless hole/pavement-edge pinch pairs — user confirmed those are
  fine).

## Risks
- Conformance/T-junctions with a triangle fan (P3, the hard part).
- Triangle count / mesh load (densify ~12 m × many junctions).
- A centerline through a junction at the SAME crossing point as another must share
  the crossing node (the field already co-levels crossings).
- Determinism (pin PYTHONHASHSEED=0 for A/B).

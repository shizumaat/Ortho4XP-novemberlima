# STATUS — handover (2026-07-01) — HECA F→05R FIXED; **T5→05C spine FIXED**

> Everything below is **committed on `dev`** and the tree is clean. Both runway-contact
> spine complaints are now fixed: **F→05R** (edge-contact anchor `5ca42e7`) and
> **T5→05C** (junction interior-stitch — see SHIPPED, newest first).
> ⚠ Ortho4XP caches `auto_patch.*` — after any commit you must **restart Ortho4XP**,
> not just rebuild the tile, or it runs stale modules (this masked the F fix for a while).

Build/verify (no system python): `venv/bin/python`. Single build
`build_airport_pavement("HECA", xplane_root(), compute_elevations=True, tile_dem=<dem>,
current_tile_lat=30, current_tile_lon=31)` (~2 min). Suite
`venv/bin/python -m pytest tests/ -q` (~4–7 min). Fixtures: CYXY, SPJC, SPLP, HECA.
⚠ shapeIDs are build-specific — identify shapes by **ref/coord**, not index.

**Fast iteration (learned the hard way this session):**
- DEM load+smooth is only ~1 s — NOT the bottleneck. A build is ~2 min (solver+geometry).
  Cache the smoothed DEM once (`elevation._load_airport_dem(30.5,31.5)` → pickle) and pass
  `tile_dem=dem` to skip the reload.
- `compute_elevations=False` (~44 s) SKIPS Phase 2 — so it also **skips the junction-spine
  slice**; use it only for pre-slice geometry (centerline/node positions), never spine work.
- Run gate A/B builds as TWO parallel background commands (~2 min wall, not 4).
- ⚠ Geometric probes were repeatedly MISLEADING here (measured adjacent shapes / wrong
  junctions → a long wrong diagnosis). Cross-check the emitted ride profile and the sim.

---
## ✅ RESOLVED — T5→05C taxiway spine now reaches the runway (HECA)

**Root cause (confirmed via a T4-works-vs-T5-broken differential):** T5 and T4 are
near-identical diagonal taxiways curving into 05C, but their runway-contact *neighbourhood*
differs. T4 joins through LARGE multi-taxiway junctions (5–6 crossing centerlines) that
slice into a spine regardless. T5 joins through ONE small junction (5794 m², only the 2
bend-split pieces of T5 crossing it), and its only qualifying piece **dead-ends 1.46 m
INSIDE** the polygon: the apt.dat route bends ~6 m from the runway edge, so the 77 m piece
stops at that interior bend and the 4.8 m runway-reaching remainder is dropped by the 6 m
min-cut-length gate. A cut that dead-ends inside a polygon can't split it → `single_face`
→ no spine node planted at the contact → T5 grade drapes to DEM.

⚠ The earlier "geometric probes show T5 forms spine boundaries" note was a CONFOUND — those
probes re-sliced the FINAL `layout.shapes` (already spine pieces → trivially `single_face`).
Instrument the REAL slice by monkeypatching `_partition_junction` during the build.

**Fix (`junction_spine._partition_junction`, gate `O4_JCT_SPINE_INTERIOR_STITCH`, default-on):**
try the plain per-piece slice FIRST (byte-identical for junctions that already split); only
when it fails to split, retry with the crossing pieces stitched at their shared INTERIOR
bends (`_stitch_interior_joints`, degree-2 interior joints only — never on the boundary, so
no shared neighbour corner is absorbed into a cut interior). Verified: T5 contact junction
`single_face`→`ok_stitched` (2 pieces); T5 emitted profile grades smooth through the junction
(114.44→114.80 m @ ~1.2%) and MEETS 05C at 0.05% — no step. Full suite: **0 new failures**
(failed-set identical to HEAD baseline). An always-on stitch (no plain-first gate) instead
broke `neighbour_corners_shared[SPJC]`; the adaptive plain-first fallback is what avoids it.

Files: `junction_spine.py` (`_partition_junction`, `_stitch_interior_joints`, `_slice`),
`config.py` (`JUNCTION_SPINE_INTERIOR_STITCH`). Memory: `curved_runway_crossing_spine.md`,
`runway_join_edge_contact.md`.

---
## 🟡 DEFERRED quality lever (real, not tied to T5)

Slicing junctions along the **chained route** (`junction_spine._full_centerlines` →
continuous `route_line` / `linemerge(pieces)` instead of bend-split pieces) cut HECA
within-shape violations **4144 → 3479 (−16%)** — curved crossings grade sub-optimally in
many junctions network-wide. BUT it breaks per-junction invariants
(`test_junction_vertices_have_source`, `test_junction_neighbour_corners_shared` on
CYXY/SPLP/SPJC): a cut spanning junction A→B conflicts with the per-junction slice model.
Needs a per-junction-LOCAL formulation (or fix the A/B-boundary corner sharing) to capture
the win safely. Gate idea `O4_SLICE_CHAINED_ROUTE`.

---
## ✅ SHIPPED THIS SESSION (committed on `dev`, newest first)

- **junction interior-stitch — FIXES T5→05C** (default-on, `O4_JCT_SPINE_INTERIOR_STITCH`).
  A taxi route bend-split INSIDE a small runway-contact junction left every crossing piece
  dead-ending in the interior → the slice never split → no spine (HECA T5→05C).
  `_partition_junction` now tries the plain per-piece slice first and, only on failure,
  retries with pieces stitched at their shared INTERIOR bends (`_stitch_interior_joints`).
  Junctions that already slice stay byte-identical; T5 now grades smooth to 05C (0.05% at
  contact). 0 new suite failures. See RESOLVED section above for the full differential.
- `5ca42e7` **grade: anchor taxi↔runway joins at the runway EDGE** (default-on,
  `O4_RUNWAY_EDGE_CONTACT`). A taxi route joins at the runway CENTERLINE, so on a wide
  (shoulder-widened 86 m) runway its endpoint is ~43 m inside — beyond the 18/30 m anchor
  radius, so the join was never anchored/checked → junction grades to DEM. Shared law
  `grade_law.runway_join_contact` resolves the contact to the runway EDGE crossing; solver
  anchor + validator both use it (lockstep). HECA within 4351→4152; **FIXED F→05R.**
- `44b5688` + `e337000` **junction within-shape grade = spine + triangle-mesh edges**
  (default-on, `O4_JUNCTION_MESH_CONSTRAINTS`). Body CHORDS (81–97% of the count, phantom)
  retired from solver+validator; APRON visibility-geodesic kept. HECA within 6950→4351,
  junction −64%, solve −37%. Lockstep via shared `shape_constraints`.
- `527c4be` **rects: short taxi rects (<100 m) stay junction** (default-on,
  `O4_MIN_RECT_LENGTH_M=100`) so the spine curves through tight climbing turns smoothly.
  Within-pair count regresses (junction fill = many facets); that metric scores facets, not
  the ride — visual in-sim is the judge.
- `3b93fef` **painted centerlines return `TaxiCenterline`, not legacy `(line,ref)` tuples.**
  Root cause of a tile emitting only ONE airport's patch: `painted_taxi_centerlines`
  (airports with no apt.dat taxi network, e.g. HEAZ) still returned tuples post-connectivity-
  refactor → `AttributeError` in `densify_junction_edges` → HEAZ build crashed.
- `f6eec8c` **driver: contain per-airport build failures.** Now catches broad `Exception`
  per-airport with traceback to the tile's `auto_patch_verify_debug.log`; one bad airport
  never aborts the tile or vanishes as a parallel "worker died hard".

(Earlier this session, also committed: parallel per-airport builds default-on
`2729650`/`9861f6a`, worker progress→main window `f314ea7`, solver byte-ident −26% `667a514`,
solver/validator canonical identity `20265a7`, grade-audit gating + small-apron anchor
`6aab784`, xdist grouping fixes `281ef9e`/`de9a911`.)

## Pre-existing suite reds (baseline, NOT from this session)
`test_pavement_grade` (universal-zero, RED by design until the solver drives misses to 0),
`test_junction_boundary_near_centerline[CYXY]`, `test_apron_..._one_percent_body` (stale
flat-1% assert vs blend 0.0133 — chip spawned), `route_band` XFAILs. Confirm any "new" red
is truly new by stashing the change and re-running on HEAD.

# STATUS — handover (2026-07-01) — HECA F→05R FIXED; **T5→05C spine still OPEN**

> Everything below is **committed on `dev`** and the tree is clean. The one open
> engineering item is **T5→05C: the taxiway-T5 spine does not reach runway 05C in
> the sim** (user-confirmed in X-Plane). F→05R is FIXED (edge-contact anchor).
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
## 🔴 OPEN — T5→05C taxiway spine does not reach the runway (HECA)

**Symptom (user, in X-Plane):** T5 (a diagonal taxiway curving into runway 05C) — its
spine/grade does not properly reach the runway. F→05R had the same complaint and is now
FIXED by `5ca42e7`; **T5 is not.** Both curve at the runway.

**What geometric probes show on HEAD (take with salt — see warning above):** T5 DOES appear
to form spine boundaries — 5 emitted junctions have T5 vertices ON their ring (two corridor
pieces with 14/15 verts), T5's runway-edge crossing at local `(-1260, 991)` has an airside
node 4.6 m away (within the 18 m anchor radius). So it is NOT the "no node on the runway
edge" story. **Prime suspect:** an adjacent junction near T5 has a **5.6 m elevation span**
(vs ~1.5–2 m on the clean F/T5 corridors) — likely the visible cliff.

**Concrete next step:** build HECA (cached DEM), get the T5 union
(`[c.line for c in layout.apt_taxi_centerlines if c.name=="T5" and not c.is_service]`),
dump every junction within ~5 m of it — `node_altitudes` span, which routes bound each,
and the emitted elevation profile ALONG T5 from the runway edge inward (mirror what was
done for F: it graded smooth 136.6→138.7 @ 1.1–1.6%). Find where T5's profile jumps.

**Ruled out this session (do NOT re-chase):**
- NOT the runway anchor missing (contacts resolve; F's fix `5ca42e7` = edge-crossing contact).
- NOT clutter / `near_hard` (user confirmed area not cluttered; trace: both cut ends SOFT).
- NOT "no node on the runway edge" (already 0.01 m at F's crossing, 4.6 m at T5's).
- NOT the runway-crossing rect drop; NOT splitting the route at the runway edge
  (implemented → **−5 within, a no-op**; the slice already reaches the edge via `_on_pav`).
- The junction-spine slice DOES split along curved crossings (cut-line trace: the interior
  bend-piece connects the runway-edge cut to the opposite-boundary cut → 6 faces). The long
  "dangling cut → no split" theory was WRONG.

Files: `junction_spine.py` (slice / `_partition_junction`), `grade_graph.py`
(`_spine_membership`, `_runway_anchors`), `grade_law.runway_join_contact` (the F fix).
Memory: `curved_runway_crossing_spine.md`, `runway_join_edge_contact.md`.

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

# STATUS — handover (2026-07-02) — **V13 ROUTE-ARC SPINE wired (gate OFF); next: default ON + solver audit**

> Everything is **committed on `dev`** (HEAD `5ba1970`), tree clean. This session replaced
> the spine-synthesis experiments (v8–v12) with the **route-arc spine**: the apt.dat
> 1201/1202 route graph **verbatim** (metric-true taxi distances — the feasibility/anchor
> math depends on them) plus standard-radius fillet arcs at every junction turn, bend and
> runway contact. User verdict on the geometry: *"this is the solution"* (4 JOSM review
> rounds, all items closed).
> ⚠ Ortho4XP caches `auto_patch.*` — restart Ortho4XP after any commit.

Build/verify: `venv/bin/python` (no system python; `venv/bin/pip` broken → `python -m pip`).
Suite `venv/bin/python -m pytest tests/ -q` (~3.5 min). Full single build script used this
session: `venv/bin/python <scratchpad>/full_build.py` pattern —
`build_airport_pavement("SPJC", xplane_root(), compute_elevations=True)` → `to_osm`.
Grade check: `venv/bin/python tools/check_grade.py <patch.osm>`.

---
## WHERE THINGS ARE

- **Model + production wiring**: `src/auto_patch/pavement/route_arcs.py`
  - `_build_route_arc_graph(routes, pav_all, runway_union)` — the core (shared).
  - `synthesize_spine_v13(...)` — standalone tool path.
  - `apply_route_arc_spine(layout, icao)` — **production entry**, called from
    `pipeline.py` at the pre-slice hook (next to `taxi_route_fillets`; skipped when
    `O4_RECOGNIZED_CENTERLINES=1`). Replaces non-service `apt_taxi_centerlines` with
    `TaxiCenterline` entries named `route` / `route_arc`; service routes untouched.
  - **Gate `O4_ROUTE_ARC_SPINE` — DEFAULT OFF.** Flip: `O4_ROUTE_ARC_SPINE=1`.
- **Iteration tool**: `tools/pav_skeleton_osm.py SPJC --cache` (route-arc spine is the
  default; `--v7` old heuristic, `--medial-only` raw skeleton). Writes
  `/tmp/<ICAO>_skel_skeleton.osm` + `_pavement.osm` for JOSM. Cache v4 carries
  `rwy_full` (shoulder-inclusive runway rects) + `ramps`.
- **Deleted experiments** (in git history only): `edge_trace.py` (v8/v9 paint-primary,
  parked at `28d706d`), `outline_trace.py` (v10), `medial_reshape.py` (v11),
  `pure_trace.py` (v12 wall-trace — best pavement-only fallback, `294a81d`/`f98e3e0`).
- **Deep handover / all user rulings**: session memory
  `~/.claude/projects/-Users-noah-Ortho4XP-novemberlima/memory/pav_skeleton_medial_axis_spine.md`
  (every model, ruling, dead end and gotcha from v2→v13).

## MODEL INVARIANTS (user-ruled, do not regress)

1. **No route edge is ever deleted; every smoothing is in place** (same endpoints/nodes) —
   distance calculations must never lose a link.
2. In-place turn smoothing is **strictly interior** (first/last segments = junction
   tangents, never bent) with a **bidirectional 4 m follow-the-route cap**.
3. Junction arcs: standard R90 radii, mirrored per junction, **walk-mode placement**
   (`r_start_for=lambda P,r: r` → `_walk_locate`; without it, a branch hosting two arcs
   loses its second quadrant), **`gamma_max=120°`** (sharper pairs bulge across the
   junction; that's runway-turn territory).
4. Deg-2 route nodes merge first → corners become tight interior fillets
   ("arc at the corner, straights to the runway").
5. Duplicate ADDED arcs die (`_drop_duplicate_arcs`) — the route is authoritative.

## VERIFIED STATE (SPJC)

- Tool: route-graph coverage **100.0%**, 121 arcs, floating 19 = stand ends (route
  endpoints at stands are the building-side anchors, NOT defects — gate semantics still
  to update).
- Full production build with gate ON: **481 route-arc centerlines (132 arcs) flow through
  slice + solver + to_osm** cleanly.
- **check_grade A/B (SPJC, full patch): baseline 1242 within-shape violations → 2533 with
  route-arc spine ON.** This is why the gate defaults OFF.
- Suite: **19 failures with AND without all of this** (stash-atomic A/B; this work added
  0). List: `/tmp/suite_failures_20260702.txt`. NOTE: 19 vs the documented "7" baseline
  (20260701) — growth accrued earlier, unattributed; `test_pavement_grade` is *red by
  design* (universal-zero policy), so part of the delta may be threshold/policy shifts,
  not breakage. Bisect (`test_pavement_grade[CYXY]` is cheap) before chasing.

## NEXT SESSION (user-stated intent): default ON + SOLVER AUDIT

The user believed anisotropic grading was already implemented — **it is**:
aniso-edges phase 3 is default-on since `e463b02` (`cL·Δs∥ + cT·Δs⊥` per-edge in
`shape_constraints`; memory `aniso_edges_phase3.md`). So the audit question is:
**why does the existing aniso solver report 2× violations on the route-arc spine?**
Suspects, in order:
1. **Slice interaction**: route-arc centerlines are LONG continuous ways spanning many
   junctions. The known DEFERRED item (2026-07-01) found chained-route slicing cuts HECA
   violations −16% but breaks per-junction invariants — the route-arc spine hits the same
   per-junction slice model. Check how `junction_spine._partition_junction` digests the
   new ways vs the old bend-split pieces.
2. **Aniso coefficients on arcs**: per-edge ∥/⊥ decomposition assumes a local axis — check
   what axis arc-sliced pieces get (`shape_constraints`), whether `route_arc` pieces are
   classified like taxi centerlines, and whether `seg_sizes`/names feed any table.
3. **Denominator effect**: more spine → more slicing → more constrained vertex pairs;
   2533 may partly be MORE pairs, not worse grading. Compare violation *rate* and the
   emitted ride profiles (the T5 lesson: probes mislead, check profiles/sim).
4. **Rect interplay**: the hook is post-rects — rect axes were built from ORIGINAL
   centerlines; spine now differs slightly (smoothed turns). Probably benign; verify.

Suggested kickoff prompt for the new session:
> "Continue the route-arc spine work (STATUS.md + memory
> pav_skeleton_medial_axis_spine.md). Set O4_ROUTE_ARC_SPINE=1 as default and audit the
> solver: aniso-edges is already default-on (e463b02), yet the route-arc spine doubles
> check_grade within-shape violations on SPJC (1242→2533). Find why — start with the
> junction-slice interaction (see the deferred chained-route-slicing note) and the aniso
> axis classification of arc pieces — and drive violations BELOW the 1242 baseline."

## Pre-existing suite reds (baseline, NOT from this session)
19 at `dev@5ba1970` — list in `/tmp/suite_failures_20260702.txt`. Includes the by-design
`test_pavement_grade` universal-zero reds, 2 stale `TaxiCenterline`-tuple tests, dsf
cluster-bridge, SPJC/SPLP compare-targets, junction invariants (CYXY/SPLP/SPJC). Confirm
any "new" red by stash + rerun on HEAD before attributing.

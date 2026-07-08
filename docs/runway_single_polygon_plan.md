# Runway de-segmentation — single-polygon runways (Fable design, 2026-07-07)

USER MANDATE: runway segment cross-edges cut straight across the crowned
surface — a visible center dip at every segment line on every runway
(airports unusable). Fix properly before Monday 2026-07-13. Segments are
a hi/lo-era vestige (each was a 4-corner PLANE, so the curved FAA
profile required cutting); per-node emission (part 25) removed the
constraint. Goal: one polygon per runway; profile carried by edge
nodes at the former sample stations; no interior cross-edges at all.

## Phase 0 — HOTFIX (Opus, immediately; independent of de-seg)
Crown the interior cross-edges instead of removing them yet: in the
crown drop field (crown.py), every runway ring node's drop is already
rate×min(lateral,hw) in dome zones and uniform elsewhere — the defect
is that interior cross-edges have ONLY corner nodes (full drop), so
the edge constrains flat-across. Insert a CENTERLINE node (drop 0,
z = profile) on every interior cross-edge, plus optional quarter-width
nodes (drop = rate×w/4) — each cross-section becomes a tent matching
the crown. Placement: with the crown field pass (pre-solve node list?
No — cross-edge nodes are ring vertices of BOTH abutting sub-rects;
insert into both rings at identical XY/alt, or at the same point the
per-ref uniform drop is computed). Verification: cross-section probe
at 3+ segment lines per runway (center == profile ±1 cm, corner ==
profile − rate×hw); O4_PROBE_NODES in-sim check; standard gates.
NOTE: cross-edge center nodes must weld across the two abutting
sub-rects (same canonical point) or the fix mints its own tear.

## Phase 1 — consumer inventory (Opus scout, read-only)
Grep/catalog every consumer of runway sub-rect structure:
runway_segments.py emit (sub-rect construction, pav_intersection
breakpoints), seam pipeline (split_pavement_at_seams, per-piece pins,
_pin_runway_piece_to_profile), redistribute writeback (per-piece
node_altitudes), flex hook (per-sub-rect re-stamp + shared-bucket
propagation), runway_join anchors, skirts/RESA end reads
(_nearest_pav_alt on end pieces), crossings (_resolve_runway_crossings
merges segment pieces), junction stitching (welds to sub-rect corners
at pav_intersection stations), tile_cut (_SLOPING_RECT_ROLES,
runway piece clipping), check_grade + compare-target fixtures
(runway way counts WILL change → deliberate re-cut), crown field
(per-ref uniform + dome), to_osm (rect-planar collapse path).
Deliverable: table consumer → what it assumes → single-polygon change.

## Phase 2 — emitter (Fable-reviewed design, Opus execution)
One ring per runway ref: two long edges with nodes at every profile
sample station (100 m grid + breakpoints + pav_intersections + seam
crossings — the SAME stations segments used), end edges, per-node
altitudes = profile(station) − crown drop. Junction welds land on
long-edge nodes (stations preserved ⇒ weld points preserved). Seam
pins attach to ring nodes at seam stations. Flex re-stamps ring nodes
from the flexed profile by station. Crossings: crossing polygon still
carved as today, but member "pieces" become ONE ring with the crossing
node loop welded in (needs care — dome field already per-node).
Crown breakline: one continuous centerline way per ref (exists,
30c). tile_cut: a runway crossing the seam is still SPLIT at the
seam band (cross-tile invariant unchanged) — de-seg does not remove
SEAM cuts, only profile-sampling cuts (a seam-split runway = 2 rings).

## Phase 3 — consumers + fixtures + verification (Opus slices)
Order: emitter behind O4_RUNWAY_SINGLE_POLY gate → seam/flex/redistrib
→ joins/skirts/RESA → validators → fixture re-cut (deliberate, with
Noah sign-off) → full-tile bake A/B (dip probe at former segment
stations, wedge audit, triangle counts, in-sim).

## Risks
- Flex + seam interplay is the highest-risk consumer (part-29 scarred).
- compare-target floors change by construction — re-cut is deliberate.
- Fixture airports all have seam-crossing runways (SPLP) — good.
- The hotfix (Phase 0) de-risks the deadline: dips die this week even
  if de-seg slips.

# HANDOVER ADDENDUM — session kickoff state (2026-07-07 night)

You are a fresh Fable session executing this plan. Base: dev @ 1655550.
Work on branch `runway-deseg` off dev — a parallel session orchestrates
Opus agents committing to dev (verify-log fixes, spine-first service
roads); merge coordination happens in the morning with Noah. Your
primary file surface: runway_segments.py, runway_geometry.py,
runway_redistribute.py, runway_regrade.py, the runway paths of
tile_cut.py/pipeline.py, crossings, crown.py runway parts, validators.
AVOID clearance.py / boundary.py / verification.py beyond read (the
dev-side agents own them tonight).

## What changed since the phases above were written
- **Phase 0 is DONE** (7424cb8): interior cross-edges carry a welded
  centerline node at profile level (O4_RUNWAY_XEDGE_CROWN, default on).
  Dips are dead; de-seg now removes the cross-edges entirely (the
  hotfix machinery becomes deletable with them).
- to_osm exports a `crown_centerline` sidecar; check_grade/verification
  exempt runway within-shape all-pair/plane pairs touching a centerline
  node (7424cb8) — your one-ring runway inherits this convention.
- `verification.check_runway_profile` now reconstructs cross-ends from
  the runway axis (no longer len==4-gated) — it had been silently
  skipping crowned rects and masking a real SPLP profile violation.
- Epsilon-wedge tripwire is always-on in the verify pass (eafbd5d);
  tools/wedge_audit.py is committed. Junction-family wedges at segment
  seams (KJQF 5, KCLT 12, HECA 3-5) should DROP with de-seg — a
  success metric.
- Cautionary finding (svc-road tear, task #14 report, STATUS 30h note):
  per-vertex anchors from different regimes pinning one shape's two
  edges = unresolvable both-hard contradictions. Your one-ring runway
  concentrates MORE vertices in one shape — preserve the invariant
  that runway values come from ONE profile authority per ref
  (redistribute → flex → per-node stamp), never per-vertex anchors
  from mixed regimes. Seam pins are the one sanctioned exception.

## Non-negotiable invariants (each is scar tissue — see STATUS 29-30j)
1. SEAM CUTS STAY: de-seg removes profile-sampling cuts only; a
   seam-crossing runway remains two rings; seam vertices DEM-pinned;
   cross-tile determinism (covering-raster thresholds, f1a0bb3) intact.
2. Junction welds land on long-edge nodes at the SAME stations as
   today (100 m grid + breakpoints + pav_intersections + seam
   crossings) — stitching partners must find their vertices.
3. Profile authority: CIFP thresholds + redistribute + flex, evaluated
   per station; crown drop field applies per-node (uniform + dome
   zones at crossings unchanged).
4. Skirts/RESA read end elevations via _nearest_pav_alt — end edges
   of the single ring must present identically.
5. Solver runs in z' = z + c (crown) space; never mutate solved
   values post-solve; the emit consensus welds by canonical point.

## Gates (run per slice; ship in slices behind O4_RUNWAY_SINGLE_POLY)
- fast_suite: exactly these 8 pre-existing failures: compare_target_splp
  x2, cyxy_taxi_e_south_apron_follows_terrain, pavement_grade[CYXY],
  pavement_grade[SPLP], runway_longitudinal_grade[SPLP],
  cyxy_route_reach_zero, solver_validator_same_edge_budgets.
  Full suite: those + compare_target_spjc, no_self_overlap[SPJC],
  pavement_grade[SPJC], route_band_zero[SPJC], pavement_grade[HECA].
  EXCEPTION: compare-target floors count runway ways — they WILL change
  by construction; re-cut fixtures DELIBERATELY with Noah's sign-off
  (morning checkpoint), never silently.
- check_grade: SPLP within 16 / CYXY 1 / HECA 0; skirt+plane+cross 0.
- Dip probe (P0's method): centers == profile, corners == profile −
  rate×hw at former segment stations; NO interior flat cross-edges.
- tools/wedge_audit.py: junction-family wedge counts must not grow
  (target: drop at KJQF/KCLT).
- Isolated triangle harness (STATUS 30g method): runway-class triangle
  counts DOWN vs 1655550 at KCLT/HECA/SPLP.
- Verify-log (auto_patch_verify_debug.log) clean of new classes.
- Flex verification: tools/flex_audit.py unchanged results at HECA.

## Suggested slice order (from Phase 2-3 above)
1. Read-only consumer inventory (Phase 1) → table in this doc.
2. Emitter behind the gate: one ring per (ref × seam-piece), stations
   preserved; crown breakline unchanged.
3. Seam pipeline + redistribute writeback on the ring.
4. Flex re-stamp + runway_join + skirts/RESA reads.
5. Crossings as welded node-loops in the ring.
6. Validators + fixture re-cut (SIGN-OFF) + full-airport A/Bs.

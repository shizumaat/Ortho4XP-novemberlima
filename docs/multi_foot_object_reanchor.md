# Multi-ground-cluster object re-anchor (foot re-anchor)

Status: BUILT, gated ON by default (`O4_DSF_OBJECT_FOOT_ANCHOR=0`
disables everything and restores pre-feature behaviour byte-for-byte).
Depends on the airport elevation insets feature — correct terrain under
the feet is necessary but not sufficient
(`docs/airport_elevation_insets_spec.md` section 2 non-goals).
Diagnosis: project memory `kbna-gantry-pond-multi-foot-objects`.

## 1. Problem

`Objects/KBNA Water Treatment/BNA_Water Treatment_Stair_45m.obj` (a
45 m walkway spanning a wastewater pond, anchor 36.1376421,
-86.6759065) and its 42 m twin defeated every seating path:

1. **Author-baked vertical offset.** The lowest solid vertex sits at
   local y = +6.50 m (42 m twin: +2.78 m): the author baked the
   terrain height of THEIR mesh into the geometry.  The absolute
   ground-touching test (`base_y <= DSF_OBJECT_ELEVATED_BASE_M`)
   classified the structure as rooftop clutter, so it inherited a
   neighbour's offset, and the seating audit
   (`tools/object_seating_report.py`) had no row for it at all.
2. **Two disjoint ground-contact feet** whose relative heights are
   fixed by the geometry (authored bases 6.496 and 7.671 — 1.17 m
   apart, compensating the pond-rim height difference on the author's
   mesh).  The single centroid/median ground sample can never seat
   both.
3. **Under the discovery reach floor.**  `solid_reach_metres()` is
   24.3 m / 20.6 m — below `DSF_OBJECT_MIN_REACH_M` (25 m), so Phase 2
   discovery dropped the objects before any of the above even ran.
   The floor's premise ("compact + correctly anchored = X-Plane's
   business") is broken by a baked offset: X-Plane puts the object's
   y = 0 plane at the terrain under the anchor, so the baked base
   floats or sinks by the author-mesh/our-mesh difference no matter
   how compact the object is.

On the densified inset mesh the stair floated +4.4 to +8.1 m.

## 2. What was built

All in Phase 2 (`object_anchor.structure_deltas` and the discovery in
`post_mesh.discover_and_rebake_airport`); Phase 1 building pads are
untouched (a foot-anchored walkway must NOT become a flat building
pad).

### 2.1 Foot detection (`object_anchor.detect_foot_clusters`)

Feet are detected relative to the structure's OWN lowest band, in the
pool frame, in three stages (constants and measured rationale in
`config.py`):

1. **Contact band** — a vertex qualifies when within
   `DSF_OBJECT_FOOT_BAND_M` (0.5 m) of the lowest vertex in its own
   horizontal neighbourhood (radius `DSF_OBJECT_FOOT_CLUSTER_GAP_M`,
   5 m).  A LOCAL band: a global one either misses the second foot
   (1.17 m up) or floods with deck vertices.
2. **Clustering** — band vertices chain into one foot when within 5 m
   horizontally AND 0.5 m vertically per link; the vertical constraint
   stops a foot chaining up a stair stringer onto the deck underside.
3. **Foot gate** — a cluster is a foot only when its base is within
   `DSF_OBJECT_FOOT_MAX_BASE_SPREAD_M` (1.65 m) of the structure's
   overall minimum.  Measured on the KBNA stairs: genuine second feet
   at y_min + 1.17 / + 1.44; the lowest mid-span deck-underside
   clusters (their own local minima — stage 1 cannot see the feet from
   mid-span) start at y_min + 1.88.

### 2.2 Which structures foot-anchor

A structure that is NOT ground-touching by the absolute test, but
whose minimum base for some resource sits within
`DSF_OBJECT_ELEVATED_BASE_M` of that OBJECT's own lowest solid vertex,
is a baked-offset candidate.  (Within-object rooftop clutter — a
structure 3 m above its own object's ground floor — is excluded by
this and keeps invariant-I-8 inheritance.)

Candidates whose every foot lies over one ground-touching supporter's
bounding box are genuine baked rooftop clutter: they keep inheritance
(the supporter's delta carries them).  Candidates with feet over open
terrain are FOOT-ANCHORED.

### 2.3 The rigid fit

Per foot, the SEAT TARGET is `ground_under(foot) − base_y(foot)` — the
y = 0-plane elevation landing that foot exactly.  The body rests on
its highest contact: feet whose target falls more than
`DSF_OBJECT_FOOT_CONTACT_TOLERANCE_M` (1.5 m) below the topmost target
are excluded from the fit (they would drag the true feet down — and a
mis-detected cluster hanging over a pond is exactly such a target).
The seating elevation is the MIDPOINT of the kept targets — the rigid
offset minimising the worst kept-foot residual.  Everything downstream
(per-(structure, object) deltas, invariant I-3, the amendment-A3
worse-than-uncorrected guard) is the existing machinery consuming the
foot records in place of part records.

### 2.4 Foot pad requests (terrain, not object)

After the fit, a foot still off the mesh by more than
`DSF_OBJECT_FOOT_PAD_RESIDUAL_M` (0.75 m) raises a `FootPadRequest`
(per-foot ring from `object_footprints.foot_pad_ring` — convex hull of
the contact points dilated by `DSF_OBJECT_FOOT_PAD_MARGIN_M` — plus
the target ground elevation that would zero the residual).
`post_mesh.rebake_dsf_objects` writes them to the per-tile sidecar
`Patches/<tile>/o4_object_foot_pads.json` (refreshed every run,
removed when none remain) and reports the count.  A future
terrain-shaping stream consumes the sidecar; until then it is the
durable audit trail for feet a rigid body cannot seat.

### 2.5 Discovery reach floor

Baked-offset geometry (lowest solid vertex above the elevated
threshold) is admitted at the reduced
`DSF_OBJECT_FOOT_MIN_REACH_M` (15 m) instead of
`DSF_OBJECT_MIN_REACH_M` (25 m).  Base-0 geometry keeps the standard
floor.

### 2.6 Audit un-blinded

`tools/object_seating_report.py` prints foot-anchored structures as
`feet:N` rows using the decision's per-foot residuals (the per-part
sweep is blind to them — every part sits above the absolute
threshold).  `tools/object_foot_anchor_probe.py` is the fast harness:
the same chain for named resources only, seconds per run.

## 3. Acceptance (measured 2026-07-11)

Against the densified inset mesh
(`Data+36-087.mesh`, agent-a636218d27f0d0ad6 artifacts):

- 45 m stair: two feet detected (bases +6.496 / +7.662), residuals
  **±0.390 m** (was +4.4..+8.1 m floating).
- 42 m twin: two feet (bases +2.777 / +4.219), residuals **±0.343 m**.
- Full-airport audit: 427 structures evaluated (399 before — the
  baked-offset population is now visible), 13 foot-anchored; the
  pre-existing offender list is unchanged.
- `tests/test_object_anchor.py`, `test_object_rebake.py`,
  `test_post_mesh.py`, `test_object_terrain_features.py`,
  `test_dsf_object_buildings.py`, `test_object_elevation_ordering.py`,
  `test_object_bridge_terrain.py`: 241 passed.

## 4. Known limitations

- The supporter check sees `.obj` structures only.  A baked rooftop
  object over a FACADE (`.fac`) building has no visible supporter and
  would foot-anchor to the terrain at the building's base.  No such
  case is known in the gate packs; the kill switch is
  `O4_DSF_OBJECT_FOOT_ANCHOR=0`, and extending the supporter test to
  facade footprints is the designated fix if one appears.
- Foot pad requests are recorded, not yet consumed: the sidecar is the
  contract for the future terrain-shaping stream.

# Runway-end down-slope skirt ("inverse RESA") — plan

**Status (2026-07-05, branch `runway-end-skirt`): M0–M3 IMPLEMENTED,
gate `O4_RUNWAY_END_SKIRT` default OFF.  M4 (calibration + default-on)
and M5 (pre-threshold smoothness) remain.**

## AMENDMENT 2026-07-09 — governed footprint anchored at the RUNWAY END

User report: skirts ran "about 70 m too long past the end of the
runway" at multiple airports.  Root cause: the emitter applied the full
governed length from the PAVEMENT EXIT (end of the blast pad /
stopway), but FAA AC 150/5300-13B §3.16 measures the safety area from
the RUNWAY END with the stopway INSIDE it — so every skirt ran long by
its overrun-pavement length (KCLT 18R: 124 m pad → fill to 429 m past
the end vs the lawful 305; HECA pads 59–71 m = the observed ~70 m).
Fix (lockstep emitter `clearance._emit_one_end` + validator
`verification.check_runway_end_skirt`): overrun pavement consumes
governed length (`runway_end_governed_length_beyond_pavement_m`) and
the floor profile arrives at the exit already `pavement_beyond_end`
into its descent (`runway_end_skirt_floor_profile_beyond_pavement` —
the fill still starts FLUSH at the exit-edge elevation, but falls at
the advanced profile's grade instead of restarting the 0→−3 % easing).
Also 2026-07-09: the skirt WELDS to the pavement (inner row at d = 0
with per-station pavement-edge values, exact static clip) — see the
adjacent-ground plan's weld amendment; the 1 m standoff groove rendered
as the worst CYXY cliff (11.9 m).

Implementation deltas vs the original design (§3):

* **Banded emission.**  The law floor is piecewise QUADRATIC, and a
  two-row `node_altitudes` ring renders as a ruled chord that sags up
  to ~3.5 m below the curved floor mid-span (found by the lockstep
  test).  The skirt therefore emits as abutting BANDS split at the
  law's own grade breakpoints (`runway_end_skirt_profile_breakpoints`),
  bounding the chord sagitta at `rate·L²/8` ≈ 0.31 m.
* **Own finalize.**  Bands bypass the cuts' union `_finalize` (union
  dissolves the interior band rows) — `_finalize_skirts` clips each
  band against pavement/static geometry, the cut strips (cut wins) and
  previously emitted skirt pieces (crossing runways; first wins), then
  recomputes per-vertex altitudes ANALYTICALLY from the outward
  projection, so clipping can introduce vertices freely.  Skirt shapes
  carry `ref="runway_end_skirt"`.
* **Validator (`verification.check_runway_end_skirt`)** marches each
  end's extended centerline with the emitter's own anchor geometry /
  entry-grade window / law functions; samples the rendered surface by
  ruled RAY interpolation across covering patches (edge-projection
  sampling would read the nearest row, not the surface) and the DEM
  elsewhere; checks stations strictly INSIDE the governed length (the
  endpoint is the crest of the lawful beyond-zone face on
  cap-truncated skirts).  Tolerance 1.5 m = fill trigger 1 m + emit
  rounding + interpolation.
* **check_grade OSM-only profile check: deferred.**  The verification
  reader validates law conformance directly against the DEM and covers
  both un-governed drops and unlawful emitted profiles; an OSM-only
  check without DEM adds little — revisit at M4 if the CI shape needs
  it.
* **Gate-off fixture baselines (2026-07-05)**: CYXY 4 ends flagged,
  worst −25.5 m below the law floor (the plateau cliffs — the
  motivating defect); SPLP 2 ends at −1.5/−1.6 m (marginal); SPJC 0.

M4 calibration at KCLT (2026-07-05, user-requested):

* **Gate-off baseline: 5 of 6 ends below the law floor** (18R −8.2 m,
  36R −3.9, 36C −3.2, 18C −2.7, 18L −2.5); all three runways classify
  precision/precision from real apt.dat metadata (markings 3 +
  ALSF-II/MALSR/Calvert lights) → 305 m governed footprints.
  **Gate-on: 0 findings**, 28 skirt shapes (136k m² fill),
  self-overlaps unchanged from baseline (12).
* Two fixes shaken out by the 18L residual (blast-pad end, 140 m pad):
  1. **Skirts emit LAST in the pipeline** —
     `clearance.emit_runway_end_skirts` extracted from Pass D and
     called after `final_grade_projection` AND `decimate_emit_nodes`:
     the skirt bakes its floor from edge-interpolated pavement reads,
     and earlier placements read rings that later passes rewrite.
     Emitting last, the emitter reads exactly what renders — the same
     reads the validator makes.  Skirt pieces clip to the airport
     boundary and slice at tile lines like other post-solve features.
  2. **Containment-free end reads** (`clearance._nearest_pav_alt`,
     shared verbatim by emitter and validator): the containment-based
     `_pav_alt` returned None on a hairline gap at the 31 m
     entry-grade sample, silently flattening the validator's entry
     grade → its floor diverged from the emitter's (phantom −1.6 m).
  Plus **narrow-seam bridging** in the validator: a station bracketed
  by two constraint surfaces (blast-pad end vs skirt inner edge) reads
  the lower surface, not the DEM dip inside a notch the mesh never
  renders.

Road awareness + blast-pad flank wrap (2026-07-05, user items 1–2):

* **Road awareness**: `clearance._surface_road_corridors` — union of
  SURFACE road/railway corridors from the big+small OSM road caches
  (per-class carriageway widths + 1 m shoulder; tunnel-tagged ways
  excluded on purpose — filling over a tunnel is lawful, and the
  tunnel emitter's own shapes are already respected via the static
  clip).  Subtracted from every skirt band; the validator exempts the
  SAME corridors (shared helper).  NOTE: inert in lab builds without
  the tile's OSM road caches (KCLT lab worktree has none); engages in
  real tile builds.  Emitted infrastructure (service roads, groundside
  lots, tunnel ramps, buildings) is protected by the static clip and
  exempted in the validator by the JURISDICTION rule: a station whose
  rendered surface belongs to any non-clearance emitted shape is the
  solver's business, not this law's (KCLT 18L's flank apron sits 4 m
  below the pad, correctly).
* **Flank wrap**: fill strips along the overrun pavement's SIDE edges
  between the runway end point and the pavement exit, out to the
  end-zone corridor (± max(width, strip half-width)), flat-entry law
  floor, banded like the end strips; per-strip analytic altitude
  closures (edge altitude interpolated along the flank) so clipping
  can introduce vertices.  Validator marches the flanks the same way
  (``end_drop_flank`` findings).
* **Lab debug**: ``O4_SKIRT_DEBUG=1`` prints per-end anchor/entry
  numbers, per-flank ring extents in end-local coordinates, and
  per-strip clip-stage area accounting in the finalize.

OPEN (KCLT flank slivers): 4 ``end_drop_flank`` findings remain at
KCLT (0.3–3.4 m outside the emitted pieces, 2.8–4.3 m below floor).
Everything measurable has been exonerated: the law wants fill there
(trigger fires), the raw band rings COVER the finding points, no
finalize stage (static / road / prior-fill / boundary) removes >2 % of
the affected strips, and no other shape lies within 6 m of three of
the four points.  The discrepancy is a sub-metre mismatch between one
emitted band piece and its raw ring that out-of-process probes cannot
reproduce (in-pipeline vs post-hoc ``_pavement_exit_along`` start
values also differ systematically by one 5 m step — worth
understanding while debugging this).  Needs a LOCAL overlay of the
emitted piece ring vs the raw ring (dump both under O4_SKIRT_DEBUG) —
do this before flipping the gate on.

Remaining for M4 default-on: resolve the KCLT flank slivers, recut
fixture scoreboards with the gate on, KDFW tunnel-clip regression
pass, SPLP seam-crossing skirt check, then flip `O4_RUNWAY_END_SKIRT`
to "1".  Separate follow-up (task chip): FAA NASR arresting-system
reader — EMAS ends (KCLT 18L is one, per user ground truth) should
get the shorter EMAS-equivalent governed footprint instead of full
fill.

Today the runway-end safety area (Pass C in `clearance.py`) is *cut-only*: terrain
that rises above the 5 % up-ramp is cut down to it, but terrain that **drops away**
beyond a runway end is left untouched — a runway ending at a hillside brow gets a
cliff at the pavement edge. Both FAA and ICAO/EASA govern the *downward* direction
too, with the governed footprint scaled by approach sophistication. This plan adds
that inverse law to the grade rulebook, an emitter pass that builds the down-slope
skirt, and the validator/test wiring.

---

## 1. Regulatory basis (verified against primary texts, 2026-07-05)

### FAA — AC 150/5300-13B Chg 1, §3.16.5 "RSA Grades" + Figure 3-35

* First **200 ft (61 m)** beyond the runway end: longitudinal grade between
  **0 and −3.0 %**, "with any slope being downward from the ends" (downward only,
  and gently).
* Beyond 61 m: **maximum negative grade −5.0 %**; maximum positive grade bounded by
  the approach surface (that upward half is what Pass C already enforces).
* **Grade changes limited to ±2.0 % per 100 ft (30.5 m)** — a curvature law.
* Footprint scales with approach category / visibility minimums (Appendix G):
  A/B-I small visual **240 ft × 120 ft** → lower-than-¾-mile minimums
  **600 ft × 300 ft** → C/D/E **1,000 ft beyond departure end, 600 ft prior to
  threshold, 500 ft wide**.

### ICAO Annex 14 / EASA CS-ADR-DSN (CS ADR-DSN.C.215/.220)

* RESA longitudinal slopes: **downward ≤ 5 %**; transverse up or down ≤ 5 %;
  "abrupt changes or sudden reversals of slopes avoided"; no penetration of the
  approach / take-off climb surface.
* RESA length: **90 m mandatory** beyond the strip end (strip itself extends 60 m
  past the runway end) for code 3/4 and instrument code 1/2; **240 m recommended**
  for code 3/4. Width = graded-strip width.
* Beyond the graded strip only *upward* transverse slope is limited (≤ 5 %);
  downward is **unregulated outside the governed footprint** — cliffs beyond the
  skirt are lawful (Madeira, Saba). The governed length is therefore also the
  hard cap on how much earth we mint.
* **Radio altimeter operating area** (precision runways only, Annex 14 §3 +
  Attachment A §4.3): **300 m before threshold × 60 m each side**, slope changes
  ≤ **2 % per 30 m**. This is the purest "better approach ⇒ longer smoothed
  area" rule and maps to an optional later milestone (M5).

Design constants adopted (FAA numbers, they are the stricter superset):

| Constant | Value | Source |
|---|---|---|
| Near-zone length | 61 m | 13B §3.16.5(2) |
| Near-zone down-grade | ≤ 3 % (and never upward-forced: fill only) | 13B §3.16.5(2) |
| Far-zone down-grade | ≤ 5 % | 13B §3.16.5(4), Annex 14 §4.7.1 |
| Grade-change rate | ≤ 2 % per 30.5 m | 13B §3.16.5(5) |
| Governed length | by code × approach class (see §4) | 13B App G / Annex 14 §4.2–4.3 |

---

## 2. Current state (verified on `dev`)

* **Pass C RESA is cut-only.** `_build_graded_strips`
  (`src/auto_patch/clearance.py:510–517`) triggers on `dd > ceil + trigger`; its
  docstring states "flat or falling terrain is left untouched." `_emit_resa`
  (`clearance.py:1262–1336`) builds the up-ramp at
  `RUNWAY_END_RESA_MAX_SLOPE = 0.05` from the outer pavement edge (blast pad /
  stopway end, found by `_pavement_exit_along`, probe max 300 m), half-width
  `max(runway_width, runway_strip_half_width_m(full_len))`, anchored on apt.dat
  row-100 endpoints when `source_runways` is present (authoritative path), else
  on emitted runway rects (fallback).
* **Reach cap:** Pass C uses `CLEARANCE_MAX_REACH_M["runway"] = 300`
  (`config.py:1565`). `RUNWAY_END_CLEARANCE_LENGTH_BY_CODE = {1:60, 2:90, 3:150,
  4:240}` and `runway_end_clearance_length_m()` (`config.py:1579,1638`) are
  exported but **currently unused** — the skirt becomes their first real consumer.
* **Approach metadata is parsed then discarded.** `_parse_runway`
  (`src/auto_patch/apt_dat_reader.py:929–975`) reads only tokens 0–4 of each
  9-token end block (`desig lat lon displaced blastpad markings approach_lights
  tdz_lights reil`); `markings` (idx 5) and `approach_lights` (idx 6) never reach
  the `Runway` dataclass (`apt_dat_reader.py:133–152`).
* **Law plumbing:** `grade_law.py` is the single-source rulebook (constants +
  pure functions; `classify_pair()` consumed by both solver
  `grade_graph.shape_constraints` and validator
  `tools/check_grade.py:iter_shape_grade_constraints`). Curvature laws live as
  profile passes (`_fair_spine_chains`, `TAXIWAY_MAX_GRADE_CHANGE_PER_M` at
  `config.py:517`) with a reporter-only check in
  `check_grade.py:_check_spine_curvature`. Runway-profile verification pattern:
  `verification.py:check_runway_profile` (~line 339).
* **Emission format:** clearance shapes are `BuiltShape(role=ROLE_RUNWAY_CLEARANCE,
  node_altitudes=[...])` polygons; `layout.to_osm()` writes per-node `alt_abs`.
  No DEM editing anywhere — the skirt is just another elevation-carrying patch.

---

## 3. Design

### 3.1 The law (grade_law.py — single source, both writers and readers import it)

New section "runway end skirt law" with constants and two pure functions:

```python
RUNWAY_END_SKIRT_NEAR_ZONE_M = 61.0          # FAA first 200 ft
RUNWAY_END_SKIRT_NEAR_MAX_DOWN_GRADE = 0.03  # 0 to −3 % in the near zone
RUNWAY_END_SKIRT_MAX_DOWN_GRADE = 0.05       # −5 % beyond
RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M = 0.02 / 30.5   # ±2 % per 100 ft

def runway_end_skirt_floor_profile(distances_m, start_grade=0.0):
    """Lowest lawful surface beyond a runway end, as depths below the
    runway-end elevation at each distance.  Starts at the runway's own
    end grade, steepens to the near-zone cap then the far cap, with the
    grade-change rate limited to RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M
    (a piecewise-quadratic vertical curve, same shape family as the
    runway K-factor envelope in runway_segments)."""

def runway_end_governed_length_m(runway_length_m, approach_class):
    """Distance beyond the pavement end within which the floor applies.
    Base = RUNWAY_END_CLEARANCE_LENGTH_BY_CODE[code]; 'visual' clamps to
    ≤ 90 m, 'non_precision' uses the base, 'precision' extends to
    ≥ 240 m (code 3/4: 305 m, the FAA 1,000 ft figure)."""
```

Notes:
* This is a **terrain-clearance law**, not a vertex-pair law — it does *not* go
  through `classify_pair()`/`PairContext`. It sits beside `runway_join_contact()`
  as a law-function that the emitter (writer) and `check_grade` (reader) both
  import, per the lockstep doctrine.
* The floor profile starting at the runway's actual end grade (not 0) is what
  makes the curvature law meaningful: a runway ending at +1.5 % up cannot snap to
  −5 % at the pavement edge; the vertical curve eases over ~100 m.

### 3.2 Approach classification (apt_dat_reader.py + config.py)

* Extend `Runway` with four fields: `markings_a`, `markings_b`,
  `approach_lights_a`, `approach_lights_b` (ints, default 0), populated in
  `_parse_runway` from end-block indices 5 and 6. Defaults keep every existing
  constructor call site valid.
* New classifier (config.py, beside `runway_code_number`):

```python
def runway_end_approach_class(markings_code, approach_lights_code) -> str:
    # markings: 0 none, 1 visual, 2 non-precision, 3 precision,
    #           4 UK non-precision, 5 UK precision (apt.dat 1000 spec)
    # approach lights: 1 ALSF-I, 2 ALSF-II, 3/4 Calvert, 5 SSALR, 8 MALSR
    #           imply a precision or near-precision approach
    # Fallback ladder: markings → approach lights → 'non_precision'
    #           (gateway apt.dat data is frequently 0/stale; never let a
    #           blank field pick the *short* skirt on a big runway).
```

Per-END classification (a runway can be ILS one way, visual the other).

### 3.3 The emitter — Pass D in clearance.py

New `_emit_resa_skirt(...)` mirroring `_emit_resa`, plus a fill-direction twin of
`_build_graded_strips` (call it `_build_filled_skirts`; do **not** overload the
cut function with sign flags — the daylighting logic inverts too):

* Same anchor as Pass C: apt.dat row-100 end + `_pavement_exit_along` outer
  pavement edge; same half-width; same station spacing
  (`CLEARANCE_STATION_STEP_M`).
* Floor at station distance `d`: `ref − runway_end_skirt_floor_profile(d)`
  where `ref` is the pavement-end elevation and the start grade is sampled from
  the last ~60 m of the emitted runway profile.
* Trigger: station contributes where `dd < floor − trigger` (fill-only; terrain
  at or above the floor is left untouched — the two passes are exact mirrors and
  can BOTH fire on one end across rolling terrain).
* Daylight: the skirt extends to where the descending floor re-meets the DEM or
  to `runway_end_governed_length_m(...)`, whichever first. Outer-edge vertices
  take the DEM altitude at the daylight line so the skirt itself never ends in a
  step. **If the governed length ends while still airborne above the DEM** (true
  cliff, Madeira-style), close the skirt with a shortest lawful ramp down at the
  far cap? No — that re-creates the wall further out. Close it by walking the
  outer edge down the DEM's own slope (skirt edge follows terrain), and report
  the residual as an `end_of_governed_zone` info line, not a violation: beyond
  the footprint the drop is lawful.
* Clipping, in this order: pavement union (never over pavement), existing Pass
  A/B/C clearance geometry (cut wins where both claim a cell — a cut means
  terrain is HIGH there, mutually exclusive with fill anyway), tunnel/underpass
  corridors and road bands (`bridges.py` shapes — a skirt must not bury a KDFW
  portal), boundary-ribbon shapes.
* Role: `ROLE_RUNWAY_CLEARANCE`, `node_altitudes` per vertex, same `_collect` /
  `_finalize` path — zero new emission machinery.

### 3.4 Validator + scoreboard

* `tools/check_grade.py`: new `_check_runway_end_skirt(...)`, called from
  `run_checks()`, returning its own violation list (new tuple slot, mirroring
  within/cross/steps). For each runway end: march the extended centerline over
  the governed length sampling the **emitted result** (clearance patches where
  present, DEM elsewhere) and flag
  (a) surface below the law floor (un-governed drop),
  (b) skirt profile steeper than the down-grade caps,
  (c) grade-change rate above `RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M`.
  All caps imported from `grade_law` — no local numbers.
* `src/auto_patch/verification.py`: `check_runway_end_skirt(layout, dem)`
  reporter following the `check_runway_profile` pattern (pure reporter at
  runtime, per the verification architecture ruling; gating only in CI/tests).

### 3.5 Config gates

```python
RUNWAY_END_SKIRT_ENABLED = _os.environ.get("O4_RUNWAY_END_SKIRT", "0") == "1"   # M2–M3
# flipped to default "1" in M4 after calibration
RUNWAY_END_SKIRT_APPROACH_SCALING = _os.environ.get(
    "O4_RUNWAY_END_SKIRT_APPROACH_SCALING", "1") == "1"
```

(+ `__all__` entries.) Grade caps are **not** env-tunable — they are law.

---

## 4. Milestones

**M0 — approach metadata capture.** `Runway` fields + parser change + 
`runway_end_approach_class()` + parser unit tests (existing apt.dat fixtures;
include a row with the fields absent → defaults). No behavior change anywhere
else; suite must be green untouched.

**M1 — law functions.** Constants + `runway_end_skirt_floor_profile` +
`runway_end_governed_length_m` in `grade_law.py`, pure-function unit tests
(monotonic non-increasing floor, near-zone ≤ 3 %, far ≤ 5 %, second-difference ≤
rate cap, start-grade continuity, governed lengths per class × code).

**M2 — emitter Pass D** (gated off by default). `_build_filled_skirts` +
`_emit_resa_skirt` + clipping. Verification: gate-off build **byte-identical**
(same-path stash A/B, foreground-atomic); gate-on runs at the fixture airports +
targeted looks at ends with known drops (SPLP had the cross-tile seam cliff;
CYXY hillside ends; KDFW for the tunnel-clip regression). Unit test in
`tests/test_clearance.py` with a synthetic plateau-to-cliff DEM asserting skirt
presence, floor conformance, and daylight closure.

**M3 — validator + scoreboard.** `_check_runway_end_skirt` in check_grade +
`verification.py` reporter + new parametrized test in
`tests/test_pavement_grade.py` over `_GRADE_TEST_AIRPORTS`. First run captures
per-airport baselines gate-off (expect nonzero at cliff ends = the motivating
defect) and asserts ~0 gate-on.

**M4 — default on.** Flip `O4_RUNWAY_END_SKIRT` to "1", approach scaling on,
re-baseline suite scoreboards, note in STATUS.

**M5 (optional, separate gate) — pre-threshold smoothness patch.** Precision
ends only: 300 m × ±60 m radio-altimeter area before the threshold, slope-change
≤ 2 % per 30 m as a *smoothing* pass (POCS second-difference, the
`_fair_spine_chains` machinery) rather than a floor. Only worth doing if visual
QA shows lumpy short finals at ILS runways.

---

## 5. Risks / open questions

* **Tile seams.** A skirt crossing a tile boundary goes through `tile_cut` and
  seam-pin anchoring like any clearance shape; seam pins are graded-TO hard
  anchors sampled from the DEM, which may sit *below* the skirt floor at the
  seam. The skirt's seam vertices must contribute their profile altitude to the
  seam sidecar (writer side), not inherit raw DEM. Check ordering of
  `emit_surface_clearance_cuts` vs `apply_seam_dem_anchors` in the pipeline
  before M2 lands; SPLP is the regression airport.
* **apt.dat data quality.** Gateway airports frequently carry `markings=0` /
  `approach_lights=0` on runways that plainly have ILS. The fallback ladder
  (markings → lights → default `non_precision`) deliberately errs long; visual
  class is only chosen on an explicit visual/none marking with no approach
  lights.
* **Fill vs real infrastructure.** Roads, railways, or mapped tunnels under a
  runway-end drop (KDFW-class underpasses) must not be buried; the clip list in
  §3.3 handles the mapped ones, but an unmapped service road in the skirt
  footprint will be filled over. Acceptable — that is what real RESA earthworks
  do — but note it for troubleshoot triage.
* **Interaction with the boundary ribbon and groundside surfaces** off runway
  ends: the skirt is emitted before/independent of boundary bridges; ensure
  `boundary.py` treats skirt patches like other clearance geometry (its DEM →
  boundary bridge should seed from the skirt surface where one exists).
* **Displaced thresholds / blast pads.** Anchoring at the *outer pavement edge*
  (Pass C behavior, reused) already accounts for blast pads; displaced
  thresholds are irrelevant for the overrun end geometry.
* **No monster embankments.** The governed length is a hard cap by design
  (regulatory: beyond the RESA the drop is lawful), so a runway on a true mesa
  gets ≤ 240–305 m of skirt, not a mountain of fill.

## 6. Naming

"RESA" stays the name of the Pass C up-cut. The new feature is the **runway end
skirt** (`_emit_resa_skirt`, `O4_RUNWAY_END_SKIRT`, `runway_end_skirt_*` law
functions) — full words, no abbreviations, per project naming rules.

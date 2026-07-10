# Aerodrome design standards implemented by `auto_patch`

This is the human-readable index of the FAA / EASA / ICAO rules that `auto_patch`
enforces, with citations and a pointer to the **code constant that implements each rule**.

> **Source of truth = the code constant, not this document.**
> `src/auto_patch/config.py` holds the machine-readable rule *values*. This file explains
> *why* each value is what it is and where the standard comes from. If a number here
> disagrees with the constant in code, the code wins — and the doc should be fixed. Never
> hard-code a rule value at a call site; import it from `config.py`.

Algorithmic rules (vertical-curve relaxation, the elevation-solver priority cascade) are
not single constants — they live in the modules noted below, and this index points at them.

## Grade limits

| Rule | Value | Standard | Implemented in |
|------|-------|----------|----------------|
| Within-shape grade cap, taxiway-family (taxiway, parallels, stub, cross-connector) | 1.5% | FAA AC 150/5300-13 | `config.py` `ROLE_GRADE_LIMITS` |
| Taxiway **longitudinal** grade cap `cL` (along the route, size-dependent) | C–F 1.5%, A/B 3.0% | ICAO Annex 14 Vol I §3.9.3 / Table 3-2; EASA CS-ADR-DSN.D.265 | `config.py` `taxi_grade_cap_for_letter` (`TAXI_MAX_GRADE` / `TAXI_MAX_GRADE_NARROW`), gate `TAXI_GRADE_BY_WIDTH` |
| Taxiway **transverse** grade cap `cT` (across the route) | C–F 1.5%, A/B 2.0% | ICAO Annex 14 Vol I Table 3-2 (taxiway transverse slope); EASA CS-ADR-DSN.D.280 | `config.py` `taxi_transverse_cap_for_letter` (`TAXI_MAX_TRANSVERSE_NARROW`), same gate |
| Within-shape grade cap, apron / junction body (all directions) | 1.5% | FAA AC 150/5300-13 (per-user 2026-05-07) | `config.py` `ROLE_GRADE_LIMITS` |
| Within-shape grade cap, aircraft STAND (all directions) | 1.0% | FAA AC 150/5300-13B §5.9 / EASA CS ADR-DSN.E.360 / ICAO Annex 14 §3.13 | `config.py` `STAND_MAX_GRADE` + `ROLE_GRADE_LIMITS["stand"]`. Aprons are split into `ROLE_STAND` pads (at ramp-start 1300/1301 zones), lane corridors, and body by `pavement/apron_split.decompose_aprons` so the cap binds to real geometry |
| Within-shape grade cap, terminal | 1.5% | design | `config.py` `ROLE_GRADE_LIMITS` |
| Ground-vehicle service road + its junctions (apt.dat 1206 truck route + OSM small road, dedicated strip off aircraft pavement) | 5.0% | design (cars handle steeper terrain than aircraft; user 2026-07-04) | `config.py` `SERVICE_ROAD_MAX_GRADE` + `ROLE_GRADE_LIMITS["service_road"]` / `["service_junction"]`; rect+junction network built by `pavement/service_roads.build_service_road_network` |
| Tunnel ramp navigable grade | 4.0% | per-user 2026-05-08 | `config.py` `ROLE_GRADE_LIMITS` |
| Groundside pavement (curbside / parking) ramp grade | 4.0% | per-user 2026-05-22 | `config.py` `ROLE_GRADE_LIMITS`; `groundside.py` `GROUNDSIDE_MAX_GRADE` |
| Grade rule SKIPPED (footprint outlines / vertical walls / clearance shadows) | — | n/a (trace terrain or vertical by design) | `config.py` `ROLE_GRADE_LIMITS` = `None` for `boundary`, `retaining_wall`, `taxiway_clearance`, `runway_clearance` |

The within-shape grade *validator* (`tools/check_grade.py`) reads `ROLE_GRADE_LIMITS`
directly, so it always matches whatever the table says.

The taxiway caps are **anisotropic** in the local route frame: a pair's grade
budget is `cL·Δs∥ + cT·Δs⊥`, where `Δs∥` is the along-route spine ARC and `Δs⊥`
the transverse offset (`grade_law.Allowance`, `grade_graph.ds_decompose`). For C–F
`cT == cL` so the allowance is isotropic (the legacy `cap·dist`); only A/B and
curved spines are genuinely anisotropic, which is what lets a climbing taxiway
CURVE keep its full longitudinal budget through a junction (the spine arc, not its
shorter chord). See `docs/anisotropic_edge_handling_plan.md`.

## Runway longitudinal profile

| Rule | Value | Standard | Implemented in |
|------|-------|----------|----------------|
| Max longitudinal grade | 1.5% | FAA AC 150/5300-13B (ARC C–E) | `pavement/runway_segments.py` `MAX_RUNWAY_GRADE` |
| Max grade in first / last quarter (code 3/4) | 0.8% | EASA CS-ADR-DSN / ICAO Annex 14 | `pavement/runway_segments.py` `RUNWAY_END_GRADE` |
| Vertical-curve length | L = K·\|Δg\|, K = 305 m (ARC C/D) | FAA vertical-curve rule (L ≥ 1000 ft × \|ΔG\| for Design Group III+) | `runway_regrade.py` `DEFAULT_ARC_K_M`; `pavement/runway_segments.py` `MAX_RUNWAY_GRADE_CHANGE_PER_M` (= 1/30000 per m) |

The runway profile is built and re-checked by:
- `pavement/runway_segments.py` `faa_joint_solve` — emit-time FAA gate (envelope clamp +
  grade-limited smoothing + vertical-curve relaxation).
- `runway_regrade.py` `regrade_runway` — re-optimises threshold altitudes against tile-seam
  HARD anchors while honoring the grade cap and K-factor (relaxation order: K-factor first,
  then grade cap; seam altitudes are never relaxed).
- `runway_redistribute.py` — re-runs the FAA gates after seams / tile cuts so the combined
  multi-segment profile stays compliant.

## Aerodrome reference code

| Rule | Value | Standard | Implemented in |
|------|-------|----------|----------------|
| Code NUMBER from runway length | 1 (<800 m), 2 (800–1199), 3 (1200–1799), 4 (≥1800 m) | ICAO Annex 14 | `config.py` `runway_code_number()` |
| Code LETTER from taxiway width | A (≥7.5), B (≥10.5), C (≥15), D (≥18), E (≥23), F (≥25 m) | ICAO Annex 14 | `config.py` `taxiway_code_letter()` |
| Max wingspan per code letter | A 15 … F 80 m | ICAO Annex 14 | `config.py` `WINGSPAN_BY_CODE_LETTER` |

## Runway-end safety area (RESA) and runway strip

| Rule | Value | Standard | Implemented in |
|------|-------|----------|----------------|
| RESA / runway-end graded length, by code | 1:60, 2:90, 3:150, 4:240 m | ICAO Annex 14 (90 m min, 240 m recommended) | `config.py` `RUNWAY_END_CLEARANCE_LENGTH_BY_CODE` |
| Runway-end governed footprint ANCHOR | measured from the RUNWAY END (apt.dat row-100 endpoint); blast pad / stopway pavement past the end sits INSIDE the footprint and consumes governed length | FAA AC 150/5300-13B §3.16 (the safety area is measured from the runway end and encompasses the stopway).  User ruling 2026-07-09 — the pre-fix pavement-exit anchor extended every skirt by its blast-pad length (KCLT 18R: 429 m vs the lawful 305 m; HECA pads 59–71 m = the "~70 m too long" report) | `grade_law.runway_end_governed_length_beyond_pavement_m`, `runway_end_skirt_floor_profile_beyond_pavement`, `runway_end_skirt_profile_breakpoints_beyond_pavement`; consumers `clearance._emit_one_end` + `verification.check_runway_end_skirt` (lockstep) |
| RESA longitudinal slope cap | 5% | ICAO Annex 14 | `config.py` `RUNWAY_END_RESA_MAX_SLOPE` |
| Graded runway-strip half-width, by code | 1:30, 2:40, 3:75, 4:75 m | ICAO Annex 14 (graded portion) | `config.py` `RUNWAY_STRIP_HALF_WIDTH_BY_CODE` |
| Runway shoulder max width per side (extent-based widening cap; measured strips wider than this adjoining a runway are taxiway/apron, never shoulder) | 15 m | FAA AC 150/5300-13B / EASA CS-ADR-DSN.B.080 (runway + shoulders ≤ 75 m at code F) | `config.py` `RUNWAY_SHOULDER_EXTENT_MAX_M`; detector `pavement/runways._detect_runway_shoulder_extent`, wired in `pipeline.py` after the row-100 spec widening |

## Lateral (wingtip) clearance

| Rule | Value | Standard | Implemented in |
|------|-------|----------|----------------|
| Taxiway clearance half-width | ½ wingspan + 3 m margin | FAA AC 150/5300-13 TOFA; ICAO wingspan table | `config.py` `taxiway_clearance_half_width_m()`, `TAXIWAY_WINGTIP_MARGIN_M` |
| Obstruction threshold (terrain rise above surface edge that triggers a cut) | 1.0 m (taxiway, runway & service road) | design | `config.py` `CLEARANCE_OBSTRUCTION_THRESHOLD_M` |
| Lateral strip slope | 0 (flat shadow, cut-only) | design (a non-zero slope carves canyons where pavement sits below its surroundings) | `config.py` `CLEARANCE_LATERAL_MAX_SLOPE` |
| Max outward reach (earthwork bound) | taxiway 100 m, runway 300 m | design (must exceed code-4 RESA 240 m) | `config.py` `CLEARANCE_MAX_REACH_M` |
| Service-road roadside clearance band | 15 m beyond the road edge, cut-only | **design choice, NOT an AASHTO mandate** — FAA sets no service-road grade/clear-zone numbers (width/marking only); the AASHTO Roadside Design Guide low-speed clear zone is only 2–3 m. Our 15 m band is a conservative design value (a ground-vehicle road has no wingtip envelope, so the band is the reach). Do not cite it to AASHTO as a requirement (2026-07-08 audit) | `config.py` `CLEARANCE_MAX_REACH_M["service"]`, `CLEARANCE_OBSTRUCTION_THRESHOLD_M["service"]`; walker: `clearance.emit_surface_clearance_cuts` Pass A3 |

The clearance pass is `clearance.emit_surface_clearance_cuts`: it samples the DEM inside the
protected band and cuts terrain that rises above the adjacent surface-edge altitude down to a
ramped ceiling. Terrain at or below the surface is left untouched (cut-only — we never fill).

## Single source of truth
Every grade / vertical-curve rule value is defined once in `config.py` (the named caps
above, which `ROLE_GRADE_LIMITS` also references). The solver modules import those values
and re-export them under their existing local names — there is no second copy:
- `elevation.py` `TAXI_MAX_GRADE` / `APRON_MAX_GRADE` ← `config.py`.
- `pavement/runway_segments.py` `MAX_RUNWAY_GRADE` / `RUNWAY_END_GRADE` /
  `RUNWAY_END_FRACTION` / `MAX_RUNWAY_GRADE_CHANGE_PER_M` ← `config.py`.
- `runway_regrade.py` `DEFAULT_GRADE_CAP` / `DEFAULT_ARC_K_M` ← `config.py`.
- `groundside.py` `GROUNDSIDE_MAX_GRADE` ← `config.py`.

To change a rule value, edit the constant in `config.py` only.

## Transverse (lateral / crown) grades — RESEARCHED 2026-07-07, IMPLEMENTED (part 30)

User ruling 2026-07-07: everything with a spine (runway, taxiway, service road) crowns
for drainage — spine slightly higher than the edges, per-role values. Verified from the
primary documents (FAA AC 150/5300-13B Chg 1; EASA CS-ADR-DSN Issue 7 — identical in
Issue 4; ICAO Annex 14 Vol I 7th ed.; AASHTO Green Book).  Implemented constants
(`config.py`): crown RATES `RUNWAY_CROWN_TRANSVERSE` / `TAXI_CROWN_TRANSVERSE` (1%,
gentlest-legal) and `SERVICE_ROAD_CROWN_TRANSVERSE` (1.5%); transverse LAW caps
`TAXI_MAX_TRANSVERSE_NARROW` (2%) and `SERVICE_ROAD_MAX_TRANSVERSE` (2%); gate
`ENABLE_SPINE_CROWN`.  Mechanism: `src/auto_patch/crown.py` (crown drop field + law
offsets, solver+validator lockstep) — the planned-constant names below are historical.

| Feature | Min | Max | Crowned? | Standard | Planned constant |
|------|-----|-----|----------|----------|------------------|
| Runway, AAC A–B / code A–B | 1.0% | 2.0% | Yes (center crown standard) | FAA ¶3.16.2 + Table 3-6 (S-1); CS ADR-DSN.B.080(b)(2),(c); ICAO §3.1.19 | `RUNWAY_TRANSVERSE_{MIN,MAX}_NARROW` |
| Runway, AAC C–E / code C–F | 1.0% | 1.5% | Yes | FAA Table 3-6; CS ADR-DSN.B.080(b)(1) | `RUNWAY_TRANSVERSE_{MIN,MAX}` |
| Taxiway, FAA all ADGs | 1.0% | 1.5% (1–2% if only <30,000 lb) | Yes ("ideal configuration is a center crown") | FAA ¶4.14.2(1)(a)–(c) | `TAXI_TRANSVERSE_{MIN,MAX}` |
| Taxiway, ICAO/EASA code A–B | drainage-sufficient | 2.0% | not mandated | CS ADR-DSN.D.280(b)(2); ICAO §3.9.11 | (covered by existing `TAXI_MAX_TRANSVERSE_NARROW`) |
| Taxiway, ICAO/EASA code C–F | — | 1.5% | not mandated | CS ADR-DSN.D.280(b)(1) | — |
| Apron / stand | FAA 0.5% min | ICAO/EASA 1% any direction; FAA rec. 1% stands | No (drain to inlets/edge) | FAA ¶5.9.1–5.9.2; CS ADR-DSN.E.360(b); ICAO §3.13.4–5 | (existing `APRON_MAX_GRADE`) |
| Paved service/perimeter road | 1.5% | 2.0% (2.5% intense rainfall) | Yes (normal crown) | AASHTO Green Book Ch.4 "Cross Slope" / Exhibit 4-4 (high-type 1.5–2%); TxDOT RDM §4-10-4 cross-check | `SERVICE_ROAD_TRANSVERSE_{MIN,MAX}` |
| Unpaved (low-type) road | 2% | 6% (3% desirable) | Yes | AASHTO Exhibit 4-4 | — |

Adjacent-surface values (same research, for clearance/shoulder work): runway paved
shoulder 1.5–5% (Table 3-6 S-2); RSA side slope 1.5–5% (A–B) / 1.5–3% (C–E, S-3);
taxiway shoulder + TSA 1.5–5%; unpaved strip adjacent to any paved edge 5%±0.5% for the
first 10 ft, edge drop-off 1.5in±0.5in (FAA Fig. 3-33 Detail A: 3–5% negative for 10 ft).
FAA "Table 3-7 Transverse Grades Based on ADG" is the runway OFA (S-4 ≤0%, back slopes
8:1/10:1/16:1 by ADG) — NOT taxiway pavement; FAA taxiway transverse is ADG-independent.

Notable FAA-vs-EASA deltas: FAA keys runways to AAC, EASA/ICAO to code letter (numbers
agree); FAA mandates a 1% taxiway minimum + centerline crown, EASA/ICAO have no taxiway
minimum and don't mandate crown; ICAO/EASA allow single-crossfall runways where rain-wind
justifies.

## Adjacent-ground grade law — lateral corridor off a pavement edge — RESEARCHED 2026-07-08

The LATERAL generalization of the runway-end skirt (which is the LONGITUDINAL instance of
the same idea): ground beside a paved surface is governed as a two-zone-plus-ungraded
**corridor** off the pavement edge — a signed `(floor_offset, ceiling_offset)` envelope
relative to the edge elevation, as a function of lateral distance `d`. Design, primary-
verified regulatory model and the four Noah rulings: `docs/adjacent_ground_grade_law_plan.md`.
Law function: `grade_law.adjacent_ground_envelope(role, code_number, code_letter, d)`
(pure; the bounds ACCUMULATE across zones so they are continuous in `d`). Verified from
FAA AC 150/5300-13B w/ Chg 1, ICAO Annex 14 Vol I 8th ed., EASA CS-ADR-DSN Issue 7
(ICAO/EASA strip text is word-identical).

**Ruling 1 (ENFORCE FULLY):** each graded zone is a mandatory-DOWN band `[min, max]` with
direction, exactly as the FAA writes it — a FLAT surround (offset 0) is OUTSIDE the corridor
in a down-mandatory band, so flat surrounds beside pavement are regraded to at least the
minimum fall. Where FAA mandates DOWN and ICAO merely permits UP, FAA wins (maximal
conformance; one blended global ruleset).

| Rule | Value | Standard | Implemented in |
|------|-------|----------|----------------|
| Drainage lip width (zone 1, runway & taxiway strips) | 3 m (10 ft) | FAA AC 150/5300-13B Fig 3-33 Detail A; §4.14.2 (first 10 ft) | `config.py` `ADJACENT_GROUND_LIP_WIDTH_M` |
| Drainage lip transverse slope (mandatory DOWN) | 3–5% down | FAA Fig 3-33 Detail A (3–5% negative for 10 ft); FAA TSA 5%±0.5% (§4.14.2); ICAO Annex 14 §3.4.15 (negative ≤5%) | `config.py` `ADJACENT_GROUND_LIP_{MIN,MAX}_DOWN_SLOPE` |
| Runway graded strip — transverse fall (zone 2) | min 1.5%; max 3% (code 3/4 ≈ AAC C–E) / 5% (code 1/2 ≈ AAC A/B) | FAA AC 150/5300-13B Table 3-6 S-3 (RSA side slope 1.5–5% A/B, 1.5–3% C–E; FAA has a 1.5% MINIMUM, ICAO none); ICAO Annex 14 §3.4.15 | `config.py` `RUNWAY_STRIP_BAND_MIN_DOWN_SLOPE`, `RUNWAY_STRIP_BAND_MAX_DOWN_SLOPE_BY_CODE` |
| Runway graded strip — width (zone-2 outer bound) | 30 / 40 / 75 / 75 m by ICAO code number | ICAO Annex 14 §3.4.8–9 (graded portion) — **reuses** the strip half-width, no duplicate | `config.py` `RUNWAY_STRIP_HALF_WIDTH_BY_CODE` (existing) |
| Taxiway graded strip — transverse fall (zone 2) | 1.5–5% down (UP ≤2.5% C–F / 3% A/B) | ICAO Annex 14 §3.11.5 / EASA CS-ADR-DSN.D.280; FAA TSA 1.5–5% (§4.5.3, §4.14.2) | `config.py` `TAXIWAY_STRIP_BAND_{MIN,MAX}_DOWN_SLOPE` |
| Taxiway graded strip — width (OMGWS-derived, by code letter) | A 10.25 / B 11 / C 12.5 / D 18.5 / E 19 / F 22 m | ICAO Annex 14 §3.11.4 / EASA CS-ADR-DSN.D.325(b) (OMGWS <4.5 / 4.5–6 / 6–9 m → 10.25/11/12.5; D/E/F → 18.5/19/22) | `config.py` `TAXIWAY_STRIP_GRADED_HALF_WIDTH_BY_LETTER`, `taxiway_strip_graded_half_width_for_letter()` |
| Ungraded strip (zone 3) — rising-ground cap; NO downward mandate | ceiling ≤5% UP; floor unbounded (cliffs lawful) | ICAO Annex 14 §3.4.16 (runway strip) / §3.11.6 (taxiway strip) — open channels / drops permitted only in the non-graded strip | `config.py` `ADJACENT_GROUND_UNGRADED_STRIP_MAX_UP_SLOPE`; floor `None` in `grade_law.adjacent_ground_envelope` |
| Adjacent-ground outward reach (earthwork bound) | runway 300 m / taxiway 100 m | design (must exceed code-4 RESA 240 m) — **reuses** the clearance reach | `config.py` `CLEARANCE_MAX_REACH_M` (existing) |
| Apron shoulder (the only governed band beyond an apron edge) | 3 m at 1–3% down | FAA AC 150/5300-13B §5.9.2 (10 ft shoulder at 1–3%) — a RECOMMENDATION, not a requirement; no code mandates grading beyond an apron edge | `config.py` `APRON_SHOULDER_WIDTH_M`, `APRON_SHOULDER_{MIN,MAX}_DOWN_SLOPE` |
| Apron beyond-shoulder fill render target (zone-3 free-floor region) | 3–5% down | FAA AC 150/5300-13B §5.9.2 ("then 3–5% beyond") — a render target, not a corridor | `config.py` `APRON_BEYOND_SHOULDER_{MIN,MAX}_DOWN_SLOPE` |
| Apron edge retaining-wall threshold | 1.5 m drop below the shoulder edge | design (ruling 3; grade-to-edge + vertical drop/retaining wall is lawful where no RSA/OFA/TOFA overlaps — reuse the tunnel `retaining_wall` emitter) | `config.py` `APRON_EDGE_WALL_MIN_DROP_M` |
| Edge drop-off tolerance, pavement↔unpaved | 1.5 in ± 0.5 in | FAA (all pavement types); ICAO "flush" (§3.4.10) | (documented; enforced via the flush edge = lip start) |
| Adjacent-ground daylight slope-limit (along-frontage benching) | governed depth grows ≤2.0 × the along-frontage station spacing | engineering judgment — NO external citation (grading benches into a hillside; the daylight line cannot jump discontinuously along the frontage); user ruling 2026-07-09 (CYXY shapeID 417 knife-slot report) | `config.py` `ADJACENT_GROUND_DAYLIGHT_SLOPE_LIMIT`; law `grade_law.adjacent_ground_supported_depths` (emitter + validator lockstep) |

Runway ENDS are OUT OF SCOPE of this lateral law — the longitudinal runway-end skirt law
(above / `grade_law.runway_end_skirt_*`) owns terrain beyond a runway end. Service roads
keep the unchanged 15 m cut-only flat shadow (see the corrected note in "Lateral (wingtip)
clearance" — a design choice, not an AASHTO mandate).

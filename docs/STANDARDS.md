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
| Ground-vehicle service road + its junctions (apt.dat 1206 truck route + OSM small road, dedicated strip off aircraft pavement) | 4.0% | design (cars handle steeper terrain than aircraft) | `config.py` `SERVICE_ROAD_MAX_GRADE` + `ROLE_GRADE_LIMITS["service_road"]` / `["service_junction"]`; rect+junction network built by `pavement/service_roads.build_service_road_network` |
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
| RESA longitudinal slope cap | 5% | ICAO Annex 14 | `config.py` `RUNWAY_END_RESA_MAX_SLOPE` |
| Graded runway-strip half-width, by code | 1:30, 2:40, 3:75, 4:75 m | ICAO Annex 14 (graded portion) | `config.py` `RUNWAY_STRIP_HALF_WIDTH_BY_CODE` |
| Runway shoulder max width per side (extent-based widening cap; measured strips wider than this adjoining a runway are taxiway/apron, never shoulder) | 15 m | FAA AC 150/5300-13B / EASA CS-ADR-DSN.B.080 (runway + shoulders ≤ 75 m at code F) | `config.py` `RUNWAY_SHOULDER_EXTENT_MAX_M`; detector `pavement/runways._detect_runway_shoulder_extent`, wired in `pipeline.py` after the row-100 spec widening |

## Lateral (wingtip) clearance

| Rule | Value | Standard | Implemented in |
|------|-------|----------|----------------|
| Taxiway clearance half-width | ½ wingspan + 3 m margin | FAA AC 150/5300-13 TOFA; ICAO wingspan table | `config.py` `taxiway_clearance_half_width_m()`, `TAXIWAY_WINGTIP_MARGIN_M` |
| Obstruction threshold (terrain rise above surface edge that triggers a cut) | 1.0 m (taxiway & runway) | design | `config.py` `CLEARANCE_OBSTRUCTION_THRESHOLD_M` |
| Lateral strip slope | 0 (flat shadow, cut-only) | design (a non-zero slope carves canyons where pavement sits below its surroundings) | `config.py` `CLEARANCE_LATERAL_MAX_SLOPE` |
| Max outward reach (earthwork bound) | taxiway 100 m, runway 300 m | design (must exceed code-4 RESA 240 m) | `config.py` `CLEARANCE_MAX_REACH_M` |

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

# Adjacent-ground grade law — boundary-bridge retirement (Fable design, 2026-07-08)

USER MANDATE (Noah, 2026-07-08): boundary bridges were built to solve
CYXY-class DEM under-modeling (plateau cliff edge) and force-fill
terrain at airports where the ground legitimately falls away.  Replace
them with a lawful, per-role model for ground adjacent to pavement —
the lateral generalization of the runway-end skirt law.

## The regulatory model (researched 2026-07-08, primary-verified:
## FAA AC 150/5300-13B w/ Chg 1; ICAO Annex 14 Vol I 8th ed;
## EASA CS-ADR-DSN Issue 7 — ICAO/EASA strip text is word-identical)

Every code builds adjacent ground as the SAME two-zone profile off the
pavement edge, and the asymmetry is explicit everywhere:

- ZONE 1 — drainage lip, first 3 m (FAA 10 ft): ground falls AWAY
  from the pavement.  ICAO §3.4.15 (strip: negative, up to 5%);
  FAA §3.16.4 / Fig 3-33 Detail A (3–5% negative); FAA TSA first
  10 ft 5%±0.5% (§4.14.2); strip abuts FLUSH (ICAO §3.4.10).
- ZONE 2 — graded portion, to a per-role width: bounded slopes both
  directions.
  * Runway strip: half-width 75 m (code 3/4) / 40 m (2, and 1
    instrument) / 30 m (1 non-instr) — ICAO §3.4.8-9 (already
    config.RUNWAY_STRIP_HALF_WIDTH_BY_CODE); transverse ≤2.5%
    (3/4) / ≤3% (1/2) — §3.4.15; longitudinal ≤1.5/1.75/2% by code
    — §3.4.13.  FAA RSA transverse (Table 3-6 S-3): 1.5–5% (AAC
    A/B), 1.5–3% (C–E) — note FAA has a 1.5% MINIMUM, ICAO none.
  * Taxiway strip: graded half-width keyed to OMGWS in the current
    editions (ICAO §3.11.4 / EASA D.325(b)): 10.25 / 11 / 12.5 m
    (OMGWS <4.5 / 4.5–6 / 6–9) and 18.5 / 19 / 22 m (letters D/E/F);
    transverse UP ≤2.5% (C–F) / ≤3% (A/B), DOWN ≤5% — §3.11.5.
    FAA TSA: width = ADG max wingspan, grades 1.5–5% (§4.5.3, 4.14.2).
- ZONE 3 — beyond the graded portion (inside the strip): NO grading
  mandate.  Only transverse ≤5% UPWARD toward rising ground (ICAO
  §3.4.16 strip / §3.11.6 taxi); **no downward cap — a cliff beyond
  the graded portion is LAWFUL** (this is the boundary-bridge
  killer).
- APRON EDGES: **nothing is mandated beyond the apron edge in any
  code** (positive research finding).  Rules end at apron surface
  slopes (stand ≤1%, ICAO §3.13.5 / FAA §5.9) + stand CLEARANCE
  distances (3/3/4.5/7.5/7.5/7.5 m by letter, ICAO §3.13.6) that
  bound only RISING obstacles.  FAA §5.9.2 RECOMMENDS (not requires)
  a 10 ft shoulder at 1–3% then 3–5% beyond.  Grade-to-edge with a
  vertical drop / retaining wall beyond is lawful where no
  RSA/OFA/TOFA overlaps.
- Edge drop-off tolerance pavement↔unpaved: 1.5 in ± 0.5 in
  (FAA, all pavement types); ICAO "flush".
- Service roads: FAA sets NO grade/clear-zone numbers (width/marking
  only); AASHTO low-speed clear zone is 2–3 m.  Our 15 m cut band is
  a conservative design choice and should be documented as such, not
  cited to AASHTO.
- RESA (ties to skirt): longitudinal ≤5% down (ICAO §3.5.10),
  transverse ≤5% either way (§3.5.11) — the existing skirt law is
  the longitudinal instance of this law.

## THE LAW (single source, grade_law.py)

`adjacent_ground_envelope(role, code_number, code_letter, d)` →
`(floor_offset, ceiling_offset)` relative to the pavement EDGE value,
`d` = lateral distance from the edge:

- Zone 1 (0..3 m): ceiling = 0 (flush; terrain must not rise above
  the edge), floor = −0.05·d (may fall up to 5%).  Render target
  when filling: −3%·d (mid-band).
- Zone 2 (3..W(role)): ceiling = up_cap·(d−3), floor grows at
  −down_cap·(d−3) − 0.15.  Caps: runway 2.5%/3% up (by code) with
  down = 3% (adopt FAA C–E as the render bound; ICAO permits more),
  taxiway up 2.5%/3%, down 5%.
- Zone 3 (W..reach cap): ceiling continues at +5%; **floor = −∞**
  (DEM wins below — cliffs lawful).
- Apron: W = 3 m FAA-recommended shoulder (1–3% down) as the only
  governed band, then Zone 3 semantics immediately (ceiling from
  stand clearance / wingtip envelope, floor free).
- Ends: delegate to the existing runway_end_skirt law (unchanged).

DECISIONS (recommend, Noah to confirm):
1. Do NOT enforce FAA's 1.5% transverse MINIMUM (drainage floor):
   enforcing minimum slopes on flat surrounds carves artificial
   relief; ICAO has no minimum.  Encode max caps only.
2. Key runway strip by ICAO code number (existing constants);
   taxiway graded width by the OMGWS table, derived from our code
   letter (mapping in config with the table above); skip FAA
   TSA-wingspan width (it's the same order at letters D–F and our
   wingspan tables already drive wingtip clearance separately).
3. Apron wall: where DEM sits far below the apron edge beyond the
   3 m shoulder, emit the existing retaining_wall feature along the
   edge (visual face), gated separately — Noah question outstanding.

## EMISSION + VALIDATION

- Emitter: generalize the SKIRT's banded emission (not clearance —
  clearance stays cut-only): where DEM < floor inside zones 1–2,
  emit graded fill bands; where DEM > ceiling inside the reach, the
  existing clearance cut machinery applies with the new sloped
  ceiling replacing today's flat shadow (CLEARANCE_LATERAL_MAX_SLOPE
  = 0 becomes the law's up_cap per role).  Inherit the skirt's
  constraint inference (roads/water clamp the governed band) and
  seam-pin behavior at tile edges.
- Validator: `check_adjacent_ground` generalizing
  check_runway_end_skirt's DEM-free edge reader to lateral sections;
  law + reader from the same grade_law function (lockstep).
- Gate: O4_ADJACENT_GROUND_LAW, default off.  Boundary bridges
  (boundary_dem_bridge) gate OFF when the law is ON; the at-DEM
  boundary ribbon path is untouched initially.

## WHY CYXY STILL WORKS (the origin case)

At the plateau, DEM inside the runway/taxi graded band sits below the
zone-1/2 floor → the law emits the graded shelf at the lawful down
slope to the band edge; beyond the band (zone 3) the terrain falls as
the DEM says.  The pavement never floats, no ribbon is forced at the
boundary alignment, and airports whose ground falls steeply outside
the graded band are left alone entirely (the law emits NOTHING where
DEM is inside the envelope).

## SLICES

1. Constants + STANDARDS.md rows (the researched table, citations
   included; correct the AASHTO 15 m note).  No behavior.
2. grade_law.adjacent_ground_envelope + unit tests (pure function).
3. Emitter behind the gate: fill bands (skirt machinery) + sloped
   clearance ceiling; per-airport A/B at CYXY (origin), SPLP
   (Andes steep), KSVH/KEXX (today's bridge-overlap offenders),
   HECA/KDFW (flat controls — law must be near-no-op).
4. Validator + verify-pass wiring; wedge/triangle audits (bands are
   new constrained edges — watch the epsilon-wedge tripwire).
5. Boundary-bridge retirement: gate bridges off under the law,
   in-sim QA at the A/B set, then DELETE bridge machinery per the
   dead-code rule (byte-identical with law-off only).

## RISKS

- Fill direction is new earthwork: bound by W(role) (max 75 m at
  code 3/4 runways) + the skirt's constraint inference; never fill
  beyond zone 2.
- Corner arbitration where lateral bands meet end skirts (the skirt
  already has flank machinery — reuse, don't duplicate).
- Cross-tile bands need the covering-raster determinism rules
  (f1a0bb3) the skirt already obeys.
- KEXX/KSVH bridge-overlap classes should DIE with retirement — a
  success metric; verify-log watches.

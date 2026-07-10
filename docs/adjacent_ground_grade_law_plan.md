# Adjacent-ground grade law — boundary-bridge retirement (Fable design, 2026-07-08)

## AMENDMENT — WELD RULING (Noah, 2026-07-09)

The bands FULLY WELD to the pavement they grade next to — no standoff.
In-sim CYXY showed knife-edge walls/trenches (2.5–12 m) along pavement:
the 1 m clip standoff (`_PAVEMENT_GAP_M`) between bands and every other
constrained surface left grooves of RAW DEM that the mesh rendered as
blades (measured against the baked +60-136 mesh: 225 cross-shape-groove
+ 119 pavement↔strip-groove near-vertical edges of 620 total).
Implemented 2026-07-09:

- Band inner boundary at d = 0 ON the parent ring; weld-row vertices
  carry the pavement edge value VERBATIM (unrounded — emit consensus is
  a no-op; value authorities never move, the band adopts).
- ALL band clips are EXACT (static union, cross-shape, walls): shared
  boundaries share coordinates; guarded adoption welds agreeing values,
  genuine disagreement emits the deliberate node-split wall — never a
  groove.  (The round-2 groove clip is SUPERSEDED: its wedge risk is
  handled by adoption + the preloaded pavement-vertex value registry.)
- The runway-end skirt welds identically (its pavement↔skirt groove was
  the single worst CYXY cliff, 11.9 m).
- Mesh safety: `O4_Vector_Utils.insert_edge` splits constrained edges at
  encroaching nodes (parallel encroachment) with z interpolated along
  the old edge, so mid-edge weld vertices are safe by construction.

Still OPEN after the weld: the legacy `surface_clearance` strips keep
their 1 m standoff (their grooves remain where no band covers them —
the legacy chain is slated for deletion in slice 5 anyway), and the
zone-3 band OUTER edge still ends in a lawful vertical face where the
DEM is far below (193 mesh edges, up to 10 m — raise with Noah whether
the fill face should daylight at a render slope instead).

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

DECISIONS — NOAH RULED 2026-07-08 (all four):
1. DRAINAGE MINIMUMS: **ENFORCE FULLY** (overrides the earlier skip
   recommendation).  The envelope is a CORRIDOR, not just caps:
   per-zone [min_slope, max_slope] with direction, exactly as FAA
   writes them — zone-1 lip 3-5% DOWN (not 0-5%); runway RSA band
   transverse 1.5-3% DOWN (C-E; 1.5-5% A/B); taxiway TSA band
   1.5-5%; apron shoulder 1-3% DOWN then 3-5%.  Flat surrounds
   beside pavement get regraded to at least the minimum (a code-4
   runway's band falls ≥1.1 m over 75 m).  Where FAA mandates DOWN
   and ICAO merely permits UP, FAA wins (the ruling's spirit =
   maximal conformance; the repo applies one blended ruleset
   globally, as it already does for profiles).  Emission therefore
   covers most of every graded band (DEM is rarely inside a sloped
   corridor) — the emitter must be efficient and the triangle
   budget watched (wedge/triangle audits per slice).
2. Keying: ICAO code number for runway strip (existing constants);
   taxiway graded width by the OMGWS table derived from code letter;
   skip FAA TSA-wingspan width (wingtip clearance governs that
   envelope separately).  (Implementation-owned.)
3. APRON EDGES: 3 m FAA shoulder (1-3% down) + **retaining-wall
   face** where DEM sits >~1.5 m below the shoulder edge (reuse the
   tunnel retaining_wall emitter; threshold constant, tune at
   KSVH/KEXX).
4. SEQUENCING: law arc starts NOW; the named solver items interleave
   in parallel agents (file-ownership discipline).  OLS (gap-audit
   GAP 1) is the FOLLOW-ON arc reusing this envelope machinery —
   zones 1-3 here, the transitional/approach/conical surfaces next.

## RISING vs FALLING (the two directions, explicitly)

RISING terrain (DEM above ceiling) → CUT at every distance: zone 1
cuts to flush-then-falling; zone 2 cuts to the lawful up-slope
(replacing today's FLAT clearance shadow with the sloped ceiling —
a gentle bank lawfully survives); zone 3 cuts at the ≤5%
ungraded-strip cap out to the reach limit; beyond that the OLS
transitional surface (docs/grade_law_gap_audit.md GAP 1) is the
full-scale rising model.  Wingtip clearance still applies where
stricter.

FALLING terrain (DEM below floor) → FILL only inside zones 1-2
(the graded shelf at the lawful down rate; bounded by W(role) —
≤75 m at code 3/4 runways); zone 3 floor = −∞ — cliffs/ravines
beyond the graded band render as DEM, ALWAYS.  This asymmetry is
the boundary-bridge killer and is straight from the regs (no
downward mandate exists past the graded portion).

## DRAINAGE (why the envelope has its shape)

1. The zone-1 lip IS a drainage rule: mandatory fall-away in the
   first 3 m (ICAO §3.4.15 negative ≤5%; FAA Fig 3-33 3-5%) so water
   sheds off the pavement edge.
2. Fill render target = the drainage slope (~−3% mid-band, never
   flat), matching FAA Fig 3-33 Detail A.
3. Ditches fall out for free: ICAO §3.4.16 permits open storm
   channels only in the NON-graded strip, far from the runway — a
   DEM ditch inside the graded band dips below the floor → FILLED
   (correct: unlawful there); beyond the band the −∞ floor
   preserves it (correct: lawful there).  No special case needed.
4. Deliberately NOT enforced: FAA drainage MINIMUM slopes (1.5% RSA
   transverse floor, 0.5% apron minimum).  X-Plane does not simulate
   ponding; ICAO has no minimum; enforcing one would mutate flat
   surrounds into artificial relief for zero visual benefit
   (decision 1 above).  Revisit only on a user ruling.
5. Pavement-surface drainage = the crown law (part 30, shipped).

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

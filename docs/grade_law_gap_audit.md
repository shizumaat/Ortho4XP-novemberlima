# Terrain grade-law GAP AUDIT (2026-07-08, primary-verified)

Sweep of FAA AC 150/5300-13B Chg 1, AC 150/5220-22B (EMAS), AC
150/5320-5D (drainage), AC 150/5200-33C (wildlife/water), AC
150/5390-2D (heliports), AC 150/5340-30J (PAPI), FAA Order 6750.16E
(ILS glide slope), 14 CFR §77.19, ICAO Annex 14 Vol I 8th ed + Vol II,
ICAO Annex 10 Att. C — for TERRAIN/GROUND/STRUCTURE rules the grade
law does not yet handle.  Pavement-surface grades, crown, RESA skirt,
wingtip clearance, and the adjacent-ground strip law
(docs/adjacent_ground_grade_law_plan.md) are "have" and out of scope.

## THE STRUCTURAL INSIGHT

Existing lateral machinery (flat 0-slope clearance shadow ≤300 m +
the planned two-zone strip law to ~75 m) governs the STRIP only.
Nothing models the OBSTACLE LIMITATION SURFACES — the rising
transitional/approach/inner-horizontal/conical envelope that starts
at the strip edge and climbs to 45 m+ over kilometres.  A hill 200 m
off a runway penetrating the 1:7 transitional surface is invisible to
every current pass.  Largest terrain gap; highest visual value.

## TIER 1

GAP 1 — OLS terrain-penetration cut.  ICAO Annex 14 Table 4-1
(primary): transitional 1:7 (code 3/4) / 1:5 (1/2); inner-horizontal
45 m plane, radius 2000-4000 m by code; conical 1:20 rising 35-100 m;
approach first section 2% (precision) / 3.33% (NPA 3/4) / 5%
(non-instr 1), inner edge 60-280 m.  (Part 77 §77.19 is the
notification surface — larger; use Table 4-1 for a CUT law.)
REPO HAS: nothing.  CANDIDATE: generalize clearance.py's cut-only
walker to a per-runway OLS solid keyed by runway_code_number() +
CIFP approach type; the transitional surface REPLACES the flat
lateral shadow as the lawful rising ceiling; cuts start at the strip
edge (Annex 14 §4.2.12 Note — never grade the strip to the approach
inner edge), composing with the strip law.

## TIER 2

GAP 2 — ILS glide-slope reflection-plane FLATNESS (FAA Order
6750.16E §3.3-3.4, Fig 3-7, primary): graded image plane ahead of
the GS antenna, Fresnel zone up to ~3000 ft × ~130 ft; roughness
≈ ±1.2 ft/1000 ft (~0.12%) at 3° path; first 2000 ft transverse
most critical.  ICAO numeric equivalent lives in Doc 8071 (not
fetched — flagged).  REPO HAS: nothing (no NAVAID awareness).
CANDIDATE: skirt-style flatten band ~900 m × 40 m ahead of the TDZ
for ILS runways (CIFP knows), cut-AND-fill toward a fitted plane.

GAP 3 — Runway PVI SPACING + AAC-keyed K.  FAA §3.16.1/§4.14.1
(primary): min distance between successive PVIs = 1000 ft × Σ|Δg%|
(AAC C/D/E), 250 ft × Σ|Δg| (A/B), taxiway 100 ft × Σ|Δg|.  ICAO
§3.1.18: max(30000/15000/5000 m × Σ|slope changes|, 45 m) by code
(the common "30× sum" phrasing is wrong).  REPO HAS: per-PVI curve
LENGTH K only (runway_regrade.py); K is a single 305 m — FAA keys
A/B at 91 m (currently over-constrained, conservative).  CANDIDATE:
PVI-spacing check + AAC-keyed K in the runway solver.  (Queue after
the B1 redistribute fix lands — same files.)

GAP 4 — RSA longitudinal FINE STRUCTURE (FAA §3.16.5, Fig 3-35,
primary): first 200 ft beyond the end 0-3% DOWN; beyond bounded by
the approach plane; max negative 5%; grade change ≤2%/100 ft.
REPO HAS: the 5% skirt + RESA lengths; not the 0-3% first band or
the change limit.  CANDIDATE: two-band refinement of the shipped
skirt profile.

GAP 5 — EMAS/blast-pad/stopway grade INHERITANCE.  Blast pad =
"same grades as the safety area," flush, 1.5 in drop-off (FAA
§3.7.4); EMAS bed inherits RSA grades (AC 150/5220-22B — no "EB-109"
exists); ICAO stopway = runway grades minus the 0.8% quarter rule,
change ≤0.3%/30 m code 3/4 (§3.7.2).  REPO HAS: EMAS/blast pad as
skirt constraint/anchor only.  CANDIDATE: those polygons inherit
RSA-grade law in the emitter.

## TIER 3

GAP 6 — Runway LINE-OF-SIGHT: ICAO §3.1.17 two points at h (3 m
C-F / 2 m B / 1.5 m A) mutually visible ≥ half runway length; FAA
§3.8.1 5 ft points, full length (no parallel taxiway) or half.
REPO HAS: nothing.  CANDIDATE: LOS crest constraint in
runway_regrade.  (Intersecting-runway RVZ §3.8.2: planimetric,
defer.)

GAP 7 — HELIPADS (apt.dat row 102 carries them): FAA TLOF 0.5-2%,
FATO 0.5-5% (≤2% landing), safety ≤2:1 down (AC 150/5390-2D);
ICAO Vol II FATO/TLOF ≤2%, safety ≤4% up.  REPO HAS: nothing
helipad-specific.  CANDIDATE: helipad role + caps; FATO envelope
inferred.

GAP 8 — WATER in strip/RSA: open conveyance only in the NON-graded
strip, far from the runway (Annex 14 §3.4.16 Notes); standing water
= depth >3 mm.  REPO: water already a skirt constraint — mostly
verification-only.  Low.

GAP 9 — PAPI OCS (300 ft ahead, aiming angle −1°, ±10° to 4 NM) +
localizer critical area (75 m semicircle + 300×120 m).  Exclusion
surfaces, weak grading fit — fold PAPI OCS into GAP 1 if pursued.

GAP 10 — deicing/holding-bay/compass pads: inherit apron/taxiway
grades = what the repo already does; apt.dat cannot tag them.
NO GAP — do not implement.

## TOP-5 RECOMMENDATION (agent's, concurred)
1. GAP 1 OLS cut (composes with the adjacent-ground law → the
   complete lateral terrain model).
2. GAP 2 GS reflection-plane flatten band.
3. GAP 3 PVI spacing + AAC-keyed K (cheapest correctness win).
4. GAP 4+5 skirt fine structure + EMAS/blast inheritance.
5. GAP 7 helipad role.

Flagged as not-primary-verified: EASA CS-ADR-DSN.H OLS table
(assumed = ICAO), ICAO GS flatness number (Doc 8071), FAA RSA
"no standing water" exact line, compass-pad declination tolerance.

# Auto-Patch Status — session 78 = NETWORK PROFILE MODEL (#4) BUILT + HECA in-sim rulings; NEXT SESSION STARTS BELOW

## REFACTOR (user-approved, any future session): rename `unified_jacobi.py` → `airside_grading.py`
Both name words are fossils ("unified" = the long-gone per-surface
solver merge; "Jacobi" = the original iteration scheme — today the
relaxation core is ~10 % of the file and Gauss-Seidel-flavoured).
The file IS the airside grading engine (8.6 k lines, 60 defs):
orchestration cascade, solver graph, the long-range law
(reach bands / interior-path entries), within-shape enforcement,
runway-flex interaction, the corridor/network-profile wiring
(~2.7 k lines), apron+terminal features, the write layer.
PLAN: (1) single NO-OP commit — `git mv` + the 14 mechanical
references (8 code files incl. `elevation_per_surface/solver.py`,
`verification.py`, `interior_path.py`, `network_profile.py`,
`tools/check_grade.py`, 2 tests, `__init__.py`) + CLAUDE.md/docs
mentions; nothing else rides in the commit so the history seam is
trivially auditable (`git log --follow` covers it; STATUS/memory/docs
cite unified_jacobi line numbers — archaeology grep gets one seam).
(2) DO NOT split the file yet: a large slice (tie/consensus/freeze
corridor machinery) is slated for measured DELETION once the
network-profile-model verdict closes (§6 of that plan) — split after
those deletes, not before.  Natural split lines when the time comes:
law/bands, corridor profiles, write layer, apron/terminal features.

## ★★ NEXT SESSION (user directives, 2026-06-11 session close) ★★
1. **The #198 road strip should have been DECOMPOSED, not patched.**
   The narrow, long switchback strip beside apron #198 (emit-#197,
   ~30.110943,31.402940, climbing 103.8→108.7) is a ROAD: it should
   have been SPLIT AT ITS MOUTH and reclassified as a road with 4 %
   grading — a JUNCTION at the far south-west and SLOPING RECTS for
   the two main legs.  INVESTIGATE why phase-1 geometry didn't do
   that: why did the centerline/rect decomposition leave it inside an
   apron blob?  (Is there a 1206 service-road row / OSM road there
   that was dropped?  Did the narrowness test miss it?  Is it part of
   apt.dat's apron polygon with no centerline at all — and should the
   discovered-taxiway medial-axis machinery or a road-discovery
   equivalent have caught it?)  The s78p5 edge-retreat treats the
   SYMPTOM (the cliff edge); the decomposition is the real fix and
   likely replaces the retreat at this site.
2. **★★ RULING: NO shape may ever check grade ACROSS GRASS.**
   s78p5 claimed CYXY needs across-grass law-parity couplings (the
   field's LAW-ENTRY GAP EDGES + anchor straight-gap entries; removing
   them regressed CYXY to 34 violations) — INVESTIGATE that claim and
   kill the class.  The route-band law's straight endpoint-gap entry
   (route_field.py docstring says a far vertex "gets a WEAK band ...
   that is CORRECT behaviour") and `_runway_reach_bands`' gap charging
   couple surfaces across non-pavement; the field copied that topology
   for parity.  Per the ruling the LAW itself is wrong wherever the
   connector crosses grass: entry gaps must be pavement-gated (or
   interior-path-measured) in route_field + _runway_reach_bands + the
   field TOGETHER (validator simultaneity), and CYXY's 34-violation
   regression must be root-caused properly — what did those couplings
   paper over (probably the discovered-fragment fields disagreeing
   with held writes — the n31/n42/n98 class) and what is the
   pavement-respecting fix (interior-path entries? prox-style
   notch-inflated gating? component-local anchors)?
   Start: revert-test /tmp-style at CYXY with the s78p5 gatings
   re-applied (they exist in git history @c49b29e^ diffs), classify
   the 34, fix the root, then apply the ruling everywhere.


## ★★ SESSION 78 PART 5 (2026-06-11) — dev @c49b29e: HECA IN-SIM RULINGS (#198 cliff, T1/T2 bowl) ★★
User in-sim items: (1) #198 sharp ridge near 30.1070553,31.4004721;
(2) #198 flatter + 3-5 m LOWER along its 05C edge (a switchback road
climbs between it and taxiway S — a real cliff belongs there);
(3) terminals 1/2 too low (apron bowls down to them; groundside should
absorb, apron stays a smooth ~1 % plane).
1. **APRON TERRAIN-BREAK EDGE RETREAT** (post-writeback, gated): rim
   runs route-pinned ≥2.5 m above the LOCAL DEM, with the apron's
   interior-lane field median agreeing with its DEM median, get lowered
   to terrain (DEM-floored) and UNWELDED (10 m local-normal retreat,
   containment-tested) — the strip becomes non-airside and the
   clearance machinery renders the cliff face.  #198 rim 107.9-108.2 →
   **104.3-105.1** vs junction 108.2 across the road gap ✓ the ruling.
   Hard-won guards (each measured): RUNWAY-anchor law floor via
   `F.band_lo` (CYXY's route-law-LIFTED aprons = legal lift, 34 viol
   when missed); runs ≥3 with apply-feasibility BEFORE the run filter
   (partial application = new ring steps); local-NORMAL inward moves
   (centroid rays exit concave rings — SPJC/SPLP overlap reds);
   welded-unmovable verts kill their whole run; ≥0.8 m min drop.
   ⛔ field-graph pavement gatings (gap edges/raw segments) tried +
   MEASURED-REVERTED — they remove the across-grass law-parity
   couplings CYXY needs.
2. **PAD 1 %-PLANE LOWER BOUND** in the leaf re-level: bound the
   adjacent-apron median from below by the corridor 1 %-plane (c_lo,
   two-rate beyond the zone, ≥8 samples so terminal7 ≈ 70 stands).
   terminal1 99.7 → 100.2 (+0.5; in-sim verdict whether enough).
**HECA within 68 / cross 0 / steps 0** (s77 ship: 140); 05C 108.70 ✓,
05L 57.9-62.8 smooth ✓; SUITE 316p/2f (HECA within + SPLP's one 0.58 m
step); CYXY/SPJC + overlap gates green; deterministic; gate-off
byte-identical (modulo the concurrent o4_apt_dat header @8cf7480).
**OPEN → user:** (a) emit-#197 — the SWITCHBACK ROAD strip between
#198 and S is mapped as an APRON and carries ~12 violations climbing
103.8→108.7 at 30.110943,31.402940; physically a road → re-role to
groundside (4 % law, DEM-follow)?  (b) is T1/T2 +0.5-1.0 enough or
push the pads higher?  (c) in-sim re-verdict on the #198 cliff edge.


## ★★ SESSION 78 (2026-06-11) — NETWORK PROFILE MODEL (#4) BUILT END-TO-END (`auto_patch/network_profile.py`), DEFAULT ON ★★
Implements `docs/network_profile_model.md` (user-approved s77p4).  ONE
elevation field solved per CENTERLINE-GRAPH VERTEX over the complete
taxi network; the corridor write layer SAMPLES it; the tie /consensus/
freeze layer is BYPASSED under the gate (kept intact for gate-off).
USER RULINGS this session: (1) runway flex = DIP OR RISE symmetric
(supersedes the s73-p9 DIP-only rule — on the field the demand basis is
hard-anchored contact-vs-contact, so the old false-rise class cannot
fire); (2) zero per-axis violations expected — confirmed BY CONSTRUCTION
along the network; residuals were off-network machinery seams, driven to
0 at CYXY/SPJC.
**THE FIELD (`network_profile.build_and_solve`):** graph = FULL
`apt_taxi_centerlines` (pre-drop set incl. the 12+28 dropped lines) +
discovered-rect source axes, split at intersections AND at NEAR-PASS
closest-approach points (≤60 m); runway CONTACTS = lane×runway-edge
crossings (ring-lerped values) + runway-INTERIOR anchors (piece-axis
lerp) + polyline-END bridges (≤40 m) + EXIT-ARC overrides (the s73-p10
curve-aware arcs, now ref-tagged); runway MIDLINE chains as aug nodes
(law-graph parity — without them the field is legal on a longer graph
and the enforce rejects its writes: CYXY #68 held 713.1 vs a 702.5
ceiling THROUGH the runway); ★ PROXIMITY COUPLING edges (nodes ≤60 m
whose connector is airside; small grass notches inflate the weight
4×outside, >35 % outside = no edge) — near-passing lanes are ONE
surface (CYXY #95: 6.9 m cliff between uncoupled lanes 55 m apart);
★ LAW-ENTRY GAP EDGES (every non-apt node → nearest apt/midline node,
≤300 m) mirror the enforce's anchor-entry mechanic (CYXY n31/n42: law
route 99 m, field route ∞ → enforce self-pinch); base_hard pins enter
the BAND Dijkstras (a field ignorant of a seam pin writes what the
enforce then rejects).  Solve = DEM seed (anchored comps) /
CURRENT-SURFACE seed (anchor-less comps — DEM-seeding fragments wrote
raw terrain into graded junctions, 25 % walls), Gauss-Seidel cap
projection + Δg rate pass (phase 1), then ★ CAP-ONLY to convergence
(phase 2 — cap is LAW, Δg a preference; alternating them oscillates
~2 % ramps).  MINIMAX cap relax per component (M5) — ★ SAME-RUNWAY
contact pairs EXCLUDED (SPLP: its runway legitimately drops 17 m/660 m
at the tile seam; smearing that 1.67× relax network-wide degraded every
taxiway to 2.5 % = 197 viol; cross-runway squeezes keep the spread:
HECA relax 1.09 → flex → 1.03).  DIP-or-RISE demands per contact vs
OTHER refs' contacts at capm (with ROUTE_NOISE_FRAC margin — without it
05C over-dipped 106.9 vs the blessed ~108.5), 0.5 m deadband.
**WIRING (unified_jacobi):** singleton `_touches_runway` gate LIFTED +
station floor ≥2 (taxiway-B class profiles from the field); stations
sample the field (gap ≤50 m — at 30 a mouth at 31 m fell back to relief
0.7 m off the field = a written wall, SPJC #92), all sampled stations
anchored, st-bands None, per-chain faa solve = no-op; crossing-insert
stations kept (field samples at the SAME point agree by construction);
write layer/twist/W2/jhard unchanged; flex loop unchanged (demands now
from the field).  ENFORCE: long-range bands measured ON THE FIELD GRAPH
(`route_graph_view()`) with every plain field vertex as a dense anchor
(band-entry noise dies; discovered-lane areas had no consistent entry
into the apt-only graph) + Lipschitz tighten as before; short (≤20 m)
same-shape pairs are unconditional edges (solve-time vs emit-time
visibility flutter); ★ FINAL STRICT PAIR-LAW CLOSURE: bounded (±1 m,
per-move clamped, coupling-aware) cap-only projection with CORRIDOR
WRITES MOVABLE and pads held — hard anchors + the local pair cap
outrank band placements and field writes at seams ("zero violations"
> the written profile; unbounded variant = SOR divergence, SPLP 181).
APRON geodesic seeds = exact field samples at the nearest corridor
point (M6).  VALIDATOR: `route_field.route_band_violations` accepts
`field_pts` anchors; ⛔ exporting them MEASURED-REJECTED (292 false
flags at CYXY — the model deliberately lets pavement deviate where the
preference yields; straight-gap entry over-binds across grass) — the
validator law under the model = strict pair law + runway route bands,
both asserted; engine param kept for future measured steps.
**MEASURED (all at HEAD, gate ON):** CYXY grade gate GREEN 0/0/0 (from
100 viol + 12 m walls in the first build); SPJC GREEN 0/0/0; HECA
per-axis within ~116 (s77 baseline 140) / cross 0 / steps 0; HECA
invariants: 05C/23C min **108.70** ✓ (4 dip demands 108.7-110.3, relax
1.0898→1.0286 after flex), 05L/23R **57.9→62.8** ⚠ (8 RISE demands
61.8-62.8 at the 23R/A5 end — the user's dip-or-rise ruling firing on a
hard cross-runway floor; IN-SIM VERDICT NEEDED), A4 60.3-61.0, A5
61.6-62.4 (⚠ in-sim), terminals 99.2/99.7×3/95.0/97.3/79.8 stable,
#256 spread **0.6 m** (s77p3: 1.6), #198 graded 100.4-107.9 (the
through-apron item, was raw relief); SPLP within 119 vs 134 GATE-OFF on
its main tile (its red = the pre-existing deferred class, model
improves it); DETERMINISTIC (PYTHONHASHSEED 1==2 byte-identical);
GATE-OFF BYTE-IDENTICAL to s77 @407f833 (CYXY verified).  ⚠ SUITE
note: `test_compare_target_splp` (both tiles) fails on PURE s77 HEAD
too — environment/fixture drift (SPLP Test scenery), NOT this session;
true pre-session baseline = 305p/4f.
**s78 PART 2 (user follow-ups):**
1. ★★ **SPLP ROOT CAUSE = CIFP DATA, NOT THE MODEL** — the user's
   navdata update (morning 2026-06-11, Custom Data touched 09:16) ships
   `CIFP/SPLP.dat` with BOTH thresholds at 00253 ft (77.1 m); the real
   RW20 end is ~192 ft.  With RW20=00192 the runway segments back into
   the fixture's 8 pieces (verified by diagnostic edit, then restored).
   The flat-CIFP record collapses the runway to ONE degenerate piece →
   the 4.5 % runway pairs + ~190 downstream violations + the
   compare_target mismatch (runway matched 1 < floor 8).  User is
   correcting the navdata; RE-RUN the SPLP gates + compare_target after.
   ★ DSF packs / DEM / apt.dat were ruled out by isolation builds.
2. **HECA within 111 → 72** — final-closure budget ±1 m → ±2.5 m (the
   ±1 cut left taxiway-G rect planes 0.8 m over-cap: W2 clamped the
   ends to the legal 6.41 m, then the closure dragged the low mouth
   toward lower neighbours and hit its budget mid-redistribution).
   Residual 72 = dominated by **1.5–1.6 % long stretches (380–580 m)**
   on the documented squeeze corridors (taxiway G #18/#21/#23, aprons
   #185/#189/#209) = the design's M5 spreading policy IN ACTION — "an
   irreducible squeeze shows as ~1.6–1.9 % over a long stretch instead
   of a wall" — **the §10 'To confirm' ruling now has its measured
   numbers; needs the user verdict** (alternatives: more runway flex
   depth, or accept the spread).  Invariants re-verified after the
   budget change: 05C 108.70 ✓, 05L 57.9–62.8 (rise, flagged), A4/A5
   unchanged; CYXY+SPJC still 0/0/0; gate-off still byte-identical;
   still deterministic.
**s78 PART 3 (user in-sim: "HECA is the best one yet, much smoother"):**
1. ★ User fixed the SPLP CIFP (RW20 → 00152): compare_target SPLP GREEN
   both tiles, SPLP grade ~190 → **1** (a single 0.58 m vertex-to-edge
   step on apron -10015 at −12.151288, −76.999317, vertex 73.22 vs
   projected edge 73.8 at d=0.77 m — OPEN, ⚠ the EdgeStep lat/lon is
   way-centroid-adjacent; O4_TRACE_LL at it armed 0 nodes).
2. ★★ **05L SHARP STEPS (user in-sim) FIXED — runway-flex writeback
   hole**: `_writeback` SKIPS clean 4-corner runway pieces without
   node_altitudes (their CIFP plane was authoritative) — but the flex
   re-smooth mutates `elev` at runway nodes, so seam-regraded pieces
   (node_altitudes) took the 05L rise while plane pieces kept the stale
   pre-flex plane: piece@60.1/60.4 sharing corners with a risen 62.8
   ring = a 2.4 m emitted cliff ON the runway.  05C never hit it (all
   its pieces carry node_altitudes).  Fix (gated): refresh the plane
   from solved corners via `_canonicalise_rect` when any corner moved
   >0.05 m.  05L now CONTINUOUS 57.9 → 62.8 (peak at the demanded
   rise) → 60.7 threshold, shared boundary values exact; a runway
   VERTICAL-CURVE test flipped to XPASS (the flexed profile is fully
   curve-compliant).  ⚠ LATENT at gate-off: the same hole exists for
   s73-p9 dips if a dip ever lands on a plane-only piece (never
   observed — note for the tie-layer delete session).
   **SUITE 307p/2f** (SPLP 1-step + HECA within = the M5 spread);
   invariants re-held (05C 108.70, 05L 57.9-62.8 smooth, A4/A5);
   deterministic; gate-off byte-identical.
**s78 PART 4 — PERF (user: "auto-patch creation ~10× slower") @ed8e555:**
Measured at HEAD (no profiler overhead): CYXY 12 s, HEAZ 17 s, HECA
36.9 s gate-on vs 34.5 s gate-off (**network model = +2.4 s, ~7 %**),
KPHX 158 s (dominated by the PRE-EXISTING `hole_router._visible` pass,
~90 s — an old cost, not s78).  ⚠ cProfile inflates ~2× (the earlier
"70 s HECA" was overhead).  **No 10× reproduces inside auto_patch** —
prime suspect = the morning's FULL-CYCLE CIFP install (17,065
airports): every tile now builds EVERY ICAO airport it contains
(Phoenix +33-113: 6 incl. new KBXK/KGEU; +33-112: 5 incl. new KIWA;
Cairo +HECP, no apt.dat → cheap fallback) and NEW airports trigger
first-time OSM overpass downloads (minutes each, network-bound).  The
17 k CIFP scan itself is 0.4 s — fine.  INSTRUMENTATION SHIPPED:
production tile builds now print per-airport `took Xs (verify Ys)`;
`O4_PERF=1` adds the solver per-phase breakdown + network-field
sub-phases.  NEXT = user captures one slow tile build log (with
O4_PERF=1 in the Ortho4XP env) → read the per-airport lines.
**OPEN:** (1) in-sim re-verdict on 05L (smooth rise now) + the user's
"remaining cliffs and steep slopes" — collect locations, likely the
HECA-72 M5-spread + #185/#189/#209 small-pair families; (2) ★ M5
spreading ruling on HECA's 72 (above); (3) SPLP's last 0.58 m step;
(4) measured deletes of the tie layer (§6 — after the verdict);
(5) the slow-tile perf log (above); KPHX hole-router 90 s = the
biggest standing perf item overall (pre-dates s78).  Probes: /tmp/probes/s78_*.py
(field-vs-routegraph, pairpath, residuals, classify, invariants,
splp_relax/tile/pieces, 05l_profile/boundary); env `O4_NPF_DEBUG=1`
(field audit + demands), `O4_BAND_NODES="i,j"` (enforce band stages +
pair-edge presence).

## ★★ SESSION 77 PART 4 (2026-06-11) — `docs/network_profile_model.md`: NETWORK PROFILE MODEL (#4) DESIGN, USER-APPROVED ★★
USER (after the architecture explanation): "I think this is the right
direction — solve the full centerline taxi network, which includes
curves, solve every intersection, similar to crossing runways, so they
always agree, then map that to the geometry because we'll have a clear
profile running through almost everything."
**→ NEXT SESSION = BUILD #4, START AT `docs/network_profile_model.md`**
— the full design: ONE elevation profile solved over the complete
centerline graph (curves, apron lanes, exits), intersections = shared
vertices (agree by construction — no tie machinery), runway contacts =
hard anchors with direct flex demands, geometry graded FROM the field
(rect planes sample it, junctions twist, aprons bind via the s77
geodesic bands with EXACT field seeds, terminals leaf), validator reads
the SAME field.  Subsumes: #198/#208 through-apron grading, Exit-3 fan
holes, #256 residual, D #285, squeeze placement.  Prereqs in §4 (ingest
the 12+28 dropped centerlines as graph edges, intersection splitting,
connectivity audit probe FIRST).  Gate `NETWORK_PROFILE_MODEL`, ship
COMPLETE not incremental (the s77p3 partial-coupling lesson: walls
21→46), full validation battery + invariants register in §§8-9.

## ★★ SESSION 77 PART 3 (2026-06-11) — dev @6555527: APRON LONG-RANGE LAW + PINNED PLACEMENT; SINGLETON EXPERIMENT MEASURED-REVERTED ★★
User JOSM/X-Plane eval: extreme apron dips when distant from taxiways
(valley @30.1131103,31.4074412 = HECA #193), apron #198 bad where it
joins B and C ("corridors through this apron not graded"), #208 should
climb.
1. **TWO-RATE LONG-RANGE APRON LAW** — geodesic corridor-value bands now
   cover the FULL interior-path field: 1 % inside the 200 m zone, legal
   1.5 % rate beyond (extra slack on the geodesic-shortest path).  With
   80 m-windowed chords + holed route bands, distant apron interiors had
   NO long-range constraint (#193: ring 89.6..110.1 followed raw DEM).
2. **PINNED-NODE LEAST-VIOLATION PLACEMENT** — band-pinned verts (lo>hi,
   squeeze families) were held at RELIEF values; the valley vert sat at
   99.46 with band [104.14, 102.38] = 2.9 m below even the CEILING.
   Clamp into the inverted interval (nearest edge): valley 99.80→102.40;
   the legal A↔05C descent across #193 stays.
3. ⛔ **SINGLETON PROFILING MEASURED-REVERTED** (the #198 B/C item): the
   `_touches_runway` gate drops every non-runway singleton (taxiway B =
   5 stubs) — lifting it + ≥2-station chains + singleton bridge ties
   closed #256 FULLY (1.4 m), restored A5 60.1, B/C graded 102.3-106.3
   coherent — BUT the tie network spread the C/B-vs-S route squeeze onto
   MORE surfaces: standalone >5 % walls 21→46 (writes) / 29 (tie-only;
   D #285 corners first-written 107.9/104.7 = 29 %/11.8 m).  Reverted
   byte-identical.  **OPEN DESIGN ITEM: grade through-apron connectors
   (#198 B/C, #208 climbing) — needs per-tie handling where mouth
   networks are route-pinned APART (same family as #256 residual /
   irreducible-squeeze ruling).**
**SHIPPED STATE: HECA valley filled, #256 1.6 m (14 %), 05C 108.5, 05L
exact, A4, A5 60.9-61.1 (flagged), >5 % walls 21 (was ~32), per-axis
within 140 / 0 / 0; CYXY 0/0/0 spread 44.4→37.4; SPJC 0/0/0; gate-off
byte-identical; deterministic; suite 307p/2f.**

## ★★ SESSION 77 PART 2 (2026-06-10) — dev @bdeae13: WRITE-LAYER ARBITRATION + TERMINAL LEAF LEVELS (user-approved rulings) ★★
USER RULINGS this session: (1) taxi routes graded properly ⇒ aprons grade
locally vs nearest interior route (model CONFIRMED); (2) ★★ TERMINALS =
NATURAL LEAF NODES — rigid-flat, level follows the apron(s) they connect
to UP OR DOWN, never pre-calculated/locked (SUPERSEDES "terminals must
not rise"); (3) build the write-layer arbitration.
**A. WRITE_ARBITRATION (@39a987c, default ON):** (1) PARTIAL TIES —
freeze-skips clamp the consensus into the member chain's anchor-feasible
interval instead of dropping (HECA skips 14→0; flex demands still fire
from the ORIGINAL value); (2) NODE-LEVEL TIE CAPS — same-junction mouth
ties bind at the nearest NODE pair geodesic, not mid-mid (#256: mids
74-82 m, corners 11.5/18 m — W2 skew lesson at the tie layer);
(3) CAP-TIE RESIDUAL ARBITRATION post-consensus without the st-band
clamp (cross-chain bands disagree by metres at junction scale);
(4) SOFT-TERMINUS GROUPS — non-hard terminus anchors don't FIX their
group (p10d generalized); projected to the settled value bounded by hard
anchors + junction hard band + 2.5 m; (5) POST-FREEZE TIE RECONCILE —
frozen pairs re-project into tie windows, headroom decides who yields.
★ Singleton-virtual exit stubs (A5 class) exempt from ALL new move paths.
**Measured: #256 wall 3.3→2.3 m (28.7→20 %), standalone walls 202→157.**
**A2. TRANSITIVE RUNWAY-FLEX DEMANDS (@0d645b6, user: "if the taxiway
requires it, why isn't the runway already dipping? We shouldn't be
generating a violation"):** the demand synthesis only saw blockers
DIRECTLY on a runway contact; #256's blocker = a frozen TIE station
(crossing insert, no nodes) pinned by 05C THROUGH the crossing chain —
the demand never reached the runway.  Fix: walk the blocking tie group's
member chains to their runway-contact anchors, demand over the
ACCUMULATED route distance; flex feedback now TWO rounds under the gate
(transitive demands only measurable after the first flex re-ties the
network; basis is HARD-anchored → converges, not the s68 chase; round-3
demands still discarded).  **05C dips 109.1→108.50 (user-predicted ~108);
#256 1.6 m / 14 % (orig 3.3 m / 28.7 %); junction reads 100.9-102.6.
HECA within 135 = EXACT baseline / 0 / 0; terminal levels unchanged.**
**B. TERMINAL_LEAF_LEVELS (@bdeae13, default ON):** pads enter at their
own MEDIAN (coherence, NO seed ceiling), stay HELD through projections
(★ free-following MEASURED-REJECTED ×2 — cap edges bind at the worst
single neighbour: terminal1 99.7→106.1), then LEAF RE-LEVEL = median of
adjacent apron surface (≤40 m, robust to pinned outliers, ±3 m bound) +
final legal re-projection conforms aprons around the moved pads.  Sloped
(squeezed) pads keep symmetric internal kink smoothing.
**Measured: terminal1/2/9 = 99.80 ✓ (user ~99.7-101), terminal4 95.1 /
terminal5 64.3 / terminal8 79.8 = FLUSH with apron medians (seed values
sat 0.3-2 m below the surrounding surface); terminal7 71.5-73.0 = its
apron edges (absolute level awaits #186-squeeze ruling).**
**VALIDATION: all-three-s77-gates-off = s76 BYTE-IDENTICAL; CYXY+SPJC
per-axis 0/0/0; CYXY spread 42.6 kept; HECA within 136 (≈135 baseline) /
cross 0 / steps 0; invariants 05C 109.1 (was 109.5 — tie network
transmits demands slightly differently, in-sim verdict), 05L 57.9-60.7
exact, A4 exact, ⚠ A5 60.9-61.2 (was 60.4: still flat, risen toward the
apron-edge consensus 61.8 — model-consistent, FLAGGED for in-sim);
deterministic; suite 307p/2f = baseline.**
PREFERENCE HIERARCHY (user-confirmed model, where implemented):
terminals FLAT (cap 0) unless seed-band-infeasible (squeezed → slope
≤1.5 %); aprons 1 % preference (APRON_CORRIDOR_SMOOTH_GRADE zone pairs +
geodesic corridor-value bands) stretching to the 1.5 % law
(ROLE_GRADE_LIMITS) wherever route demands bind.
NEXT: user in-sim verdict (RESTART Ortho4XP) — 05C 109.1, A5 61.0,
terminal levels, #256 @ 2.3 m; the irreducible-squeeze ruling (above);
#186/terminal7 squeeze arbitration; Exit-3 exit-fan graph holes.
Probes: s77_apron_geodesic/invariants/spread/altdiff/alloff.py;
O4_CORR_JDUMP=<node,ids> junction tie dump, O4_CORR_RECDBG=1 reconcile
trace, O4_APZ_DEBUG=1 zone stats, O4_TERM_DEBUG=1 leaf moves.

# (s76 and earlier below)

## ★★ SESSION 77 (2026-06-10) — dev @3a72f9b: APRON INTERIOR-GEODESIC GRADE vs SERVING CENTERLINE — INVESTIGATED + BUILT ★★
User question: keep aprons from steep areas by measuring, per apron node,
the shortest INTERIOR path to a taxi-route centerline and holding grade
along it.  Probe `/tmp/probes/s77_apron_geodesic.py` (MAIN repo; fresh
HEAD builds /tmp/CYXY_s77.osm /tmp/HECA_s77.osm): graph = airside ring
edges + ≤80 m in-polygon visibility chords; multi-source Dijkstra from
corridor seeds (corridors = apt.dat centerlines + taxi-rect source axes,
the `_apron_corridor_zone_edges` set); two variants — mid-corridor IDW
attach (alt-estimation error, over-reports) and `VSEED=1` exact
vertex-alt attach (authoritative).
**MEASURED (VSEED):**
1. **At 1.5 % the metric ALREADY holds everywhere** — CYXY max 1.52 %
   (1 vert over), HECA max 1.49 % (0 over).  Expected mathematically:
   the surface is assembled from cap-bounded edges and the interior
   geodesic runs through them.  As a 1.5 % validator law it is ~vacuous;
   the in-sim "too steep" complaint lives AT the legal cap.
2. **At 1 % (the user's apron ruling) it is the real lever**: CYXY 77/397
   verts (19.4 %) over, move budget sum 22.3 m / max 1.08 m / median
   0.20 m = CHEAP, best-effort achievable.  HECA 332/864 (38.4 %) over,
   budget 785 m / max 7.49 m / median 1.95 m — DOMINATED by
   route-law-PINNED verts (graded 1.45-1.49 % against anchors 0.7-1.6 km
   away by interior path: #209 vs the 116.4 threshold, #185/#195 vs
   taxiway-A 60.7 — the #186-squeeze/T-wall ARBITRATION family).  A hard
   1 % geodesic law is infeasible there; must stay preference/yield.
3. **The current Euclid zone misattributes**: verts with d_eu 13-65 m but
   d_geo 0.7-1.6 km (corridor across grass / not actually the serving
   one).  Geodesic binding fixes attribution by construction.
**→ BUILT SAME SESSION (user: "proceed with both improvements") — dev
@3a72f9b, gate `APRON_CORRIDOR_GEODESIC` (default ON; OFF = s76 Euclid
zone byte-identical, verified):**
1. **Geodesic attribution** — zone by interior-path Dijkstra over the
   solver edge graph; pair smoothing keeps the s76 Euclid coverage as a
   UNION (strictly additive — pair caps reference no corridor value, so
   attribution cannot mislead them; geodesic-only zone MEASURED-REGRESSED
   CYXY 77→111 >1 % by dropping near-miss coverage).
2. **Corridor-value bands** — in-zone apron verts clamp best-effort into
   [corridor_alt ± g·interior_d]; ★ bands CLAMPED INTO the legal route
   bands, NOT intersect-or-drop (~95 % of wanting nodes have EMPTY
   intersections — route floors sit above corridor+1 %; collapse to the
   nearest legal edge moves the apron as close as the law allows).
3. **Seeds** — corridor-adjacent verts at own values ±g·d0: ≤15 m
   unconditional; 15-60 m require the connector INSIDE the airside union
   (apron lanes run through apron INTERIORS — ring verts sit 15-60 m off
   laterally; across grass = the misattribution).  Plus every aircraft
   taxi-rect vertex.  HECA seeds 426→961, in-zone 446→569.
**MEASURED: CYXY spread 44.4→42.6 m (#97 wall 2.7→1.2 m); CYXY+SPJC
per-axis 0/0/0; HECA within 135 = baseline, cross 0, steps 0, invariants
exact (05C 109.5 new-apt.dat, 05L 57.9-60.7, A4, A5 flat, 116.5);
deterministic (PYTHONHASHSEED 1==2); suite 307p/2f = baseline.
O4_APZ_DEBUG=1 prints seed/zone/pull stats.  ★ The big >1 % residual is
ROUTE-LAW-PINNED (verts at legal floors 1.3-1.5 % vs corridor — HECA
budget 785 m, max 7.5 m): by design the preference YIELDS there — that
class belongs to the write-layer arbitration session (user rulings),
which remains the top item.**  Probes: s77_apron_geodesic.py (VSEED=1
authoritative), s77_invariants.py, s77_spread.py, s77_altdiff.py,
s77_gateoff.py.

## ★★ SESSION 76 PART 3 (2026-06-10) — dev @cf87793: PAD COHERENCE + PULLED-VERTEX ALTITUDES (user verify report) ★★
User verify report: within-terminal spikes (32.5 %/5.8 m), terminal1↔#211
cross steps, junction (user #203) "surprisingly steep and uneven".
1. **PAD COHERENCE re-level** (yield-down clamp): the part-2 freeze
   preserved relief incoherence INSIDE pads; flat groups with INFEASIBLE
   band intersections (terminal1's 88-node group — the squeeze) were
   skipped entirely.  Now every pad group re-levels to min(median,
   band-ceiling-if-feasible, taxi-route-seed).  Coherence ≠ rise.
   Pre-apt.dat-edit HECA: ALL flat pads ONE level (terminal1/2/9 = 99.7),
   terminal7 70.1-71.3 (~70 ruling RESTORED — its earlier 71.5-73 was the
   per-node squeeze midpoints), **cross 3→0, per-axis within 119→89**.
2. Ring-Lipschitz seed-band smoothing + §5.2 margin on seed bands +
   down-only internal smoothing of sloped pads.
3. **`_split_sloped_rects_at_violations` pulled-vertex altitudes**
   (junction_repair): a junction vertex pulled metres along a rect edge
   now carries the rect PLANE altitude at its new corner (node_altitudes
   re-synced when the ring stays 1:1).  The stale solved altitude at the
   new position printed 3-8 % zig-zag ring edges.  Control-attributed
   (within-neutral; the visible zig-zag class).
**⚠ apt.dat RE-BASELINE: user edited HECA's apt.dat mid-session (fixed a
source error) — geometry reorganized (junction counts, splits); per-axis
within now ~134 (control without the part-3 junction fix: 135).  The
remaining VISIBLE walls (#256 27.8 %/12 m ring edge etc.) are the
CORRIDOR-vs-CORRIDOR write disagreement: #256 holds T@102.7-104.1 on two
sides vs G@100.7 between = the documented #261/T-wall family — per-axis
EXEMPT (diagonals) but 3 m walls to the eye → THE write-layer
arbitration design item (p10f NEXT #1) is now the top visible-quality
blocker; needs user rulings (T4-wall, #186 squeeze, 05C 109.3,
terminal7 ~70-vs-midpoint all same family).**  Suite 307p/2f; CYXY+SPJC
0/0/0; deterministic.  NEXT: write-layer arbitration session (user
rulings); terminal1↔#211-class pre-solve weld if it recurs on the new
apt.dat; in-sim re-verdict.

## ★★ SESSION 76 PART 2 (2026-06-10) — dev @3b35fef: IN-SIM TUNING — flat pads, fairing, Lipschitz bands, apron 1 % corridor smoothing ★★
User in-sim verdicts on part 1: structural issues fixed; NEW problems =
(a) sloping pads float buildings (terminal1 "all around 101" carried a
105 corner), (b) small ripples at almost every junction, (c) CYXY aprons
much too steep — RULING: smooth aprons toward ~1 % within ~200 m of the
taxi corridors that serve them.
1. **`TERMINAL_PADS_SLOPE = False`** (the s73-p2 experiment's verdict is
   in): pads rigid-flat, squeezed pads still slope via the seed marking.
   Pads HELD through every enforce projection, may only YIELD DOWN
   (group clamp to band-ceiling ∩ taxi-route SEED — the relief lifted
   squeezed pads 1.5-3 m above seeds; ruling: terminals must NOT rise).
   Terminal seed bands now carry the §5.2 route-noise margin.
   terminal1 BACK at 99.2-100.8 (user's expected ~101; its part-1
   100-105 slope = route-law floor from the 116.39 corridor-held write
   near 23C, ~670 m by route — measured via O4_TRACE_LL).
2. **SURFACE_FAIRING** (config): final weighted-Laplacian smoothing of
   soft uncoupled vertices — bands-clamped, ±0.5 m budget
   (SURFACE_FAIRING_MAX_MOVE_M), legal-cap re-projection after.
   Junction+apron ring bumps >0.15 m: CYXY 90→62, SPJC 65→35.
3. **`_lipschitz_tighten_bands`** (ripple ROOT): route bands live on the
   CENTERLINE graph → ring-adjacent vertices carry graph-entry
   discontinuities the band clamp printed into the surface.  Propagate
   every node's bound through the cap-weighted edge graph (pure
   tightening, the implied constraints) before clamping.
4. **APRON CORRIDOR SMOOTHING** (`APRON_CORRIDOR_SMOOTH_RADIUS_M=200`,
   `_GRADE=0.01`): best-effort projection of apron pairs (both endpoints
   in-radius of apt.dat centerlines + taxi-rect source axes — discovered
   taxiways have no apt.dat row) toward 1 % — solver PREFERENCE, the
   validator law stays ROLE_GRADE_LIMITS ("ideally 1 %").  ★ Pads held
   or the lift cascades (terminal7 rose 70→72.9 in the first build).
   CYXY apron spread sum 56.2→44.4 m (#63 9.6→6.7, #93 2.9→1.2).
**MEASURED: CYXY + SPJC 0/0/0 per-axis; HECA 05C 109.3 / 05L exact /
A5 flat / 116.5 ✓; suite 307p/2f = baseline; deterministic.  ⚠ FLAGGED
FOR USER: terminal7 now sits at its squeeze MIDPOINT 71.5-73.0 (its
per-node seed bands are INFEASIBLE — 05C floor crosses 05L ceiling;
the historical "~70" was the free pad dragged below its band while the
violations parked on the apron edges) — T4-wall arbitration family,
needs a ruling.  HECA within 118 / cross 3: the 3 cross = terminal1 ↔
apron #211, 0.3 m at d≈0.6 m (sub-weld-tolerance seam; post-solve
snap variant MEASURED-REJECTED — within 97→101, cross 3→4; fix =
PRE-SOLVE conformance insertion, vertices.py family).  NEXT: in-sim
verdict; then terminal1↔#211 pre-solve weld, terminal7 ruling, #186
squeeze arbitration, Exit-3 exit-fan graph holes.**

## ★★ SESSION 76 PART 1 (2026-06-10) — dev @619843e: ROUTE-FIELD MODEL (#3) BUILT — HECA within 216→111, CYXY+SPJC 0/0/0, suite 307p/2f ★★
Implemented `docs/route_field_model.md` end-to-end (solver + validator +
runtime WARN change TOGETHER, per the design's one-piece rule).  Gate =
`config.ROUTE_FIELD_MODEL` (default ON; OFF restores the s73-p10h
baseline byte-identically — verified at HECA, 05C back to exactly 108.7).
1. **LOCAL WINDOW** — `_visible_grade_edges(max_len=W)` with
   `ROUTE_FIELD_LOCAL_WINDOW_M = 80`: apron/terminal/junction visibility
   chords beyond W are dropped (ring-adjacent pairs ALWAYS survive — the
   physical edge); per-axis junction logic unchanged.  Same window in
   `check_grade._check_within_shape` + the elevation.py WARN.
2. **LONG-RANGE LAW** — `_runway_reach_bands` extended: anchors = runway
   nodes + base_hard pins + corridor-held writes (held_extra) at current
   values; weights carry the 4 % `ROUTE_NOISE_FRAC` margin (now in
   config, shared with the validator); graph = shared cached
   runway-augmented route graph (`taxi_routing.shared_taxi_route_graph` /
   `runway_augmented_route_graph`; the flex path's inline augmentation
   was centralized onto it, semantics identical).  `band_exempt` is NOT
   applied under the model (corridor writes are anchors — the route-band
   vs corridor fight disappears by construction; machinery kept for
   gate-off).  Corridor's own st-band call left untouched this step
   (attribution doctrine).
3. **VALIDATOR route-band check** — NEW shared engine
   `auto_patch/route_field.py` (one engine for check_grade + the WARN,
   the s64 drift lesson): per-vertex band vs every runway anchor over
   the centerline graph + runway-midline augmentation, margin + rounding
   noise.  `run_checks(route_ctx=...)`; gate test +
   `verification.run_grade_checks` pass it (`route_ctx_from_layout`);
   standalone CLI prints a skip notice.
4. **★ KEY FIX (measured, CYXY)** — augmented runway-midline nodes are
   ANCHOR-ENTRY/route segments only: pavement-VERTEX nearest-node queries
   are PLAIN-node restricted (`nearest_key(plain_only=True)`, engine
   mirror).  Otherwise a vertex in a route-graph COVERAGE HOLE near a
   runway (CYXY TX1: nearest taxi row 345 m, midline ~60 m) gets a TIGHT
   fictitious band through a straight hop across grass instead of the
   hole's weak band — 11 false violations on a corridor profile the
   curve-aware model had legally written.
**MEASURED (s76 probes /tmp/probes/build_rf.py + s76_axis_audit.py —
point at the MAIN repo; the old s75 probes point at the worktree):**
- CYXY 0/0/0 and SPJC 0/0/0 per-axis INCLUDING the route-band check.
- HECA per-axis within 216 → **111** (cross 0, steps 0): the ~70 small
  #190/#194 apron violations (the p10g km-chord under-measurement) are
  GONE.  Residual = terminal4 lump family (5-12 %, ≤50 m local pairs),
  J3/U stubs, + 18 route-band: 6 × Exit-3/#281/#171 near 05R (exit-fan
  route-graph holes — the §5.1 known-gap class, the curved exit lines
  are ingest-dropped) and 12 × apron #186 vs 05L over 0.9-1.7 km
  (excess ≤0.26 % — the 05C↔05L squeeze, T4-wall ARBITRATION family).
- Invariants: 05L/23R exactly 57.9-60.7 ✓, thresholds 116.5 ✓, A5 flat
  60.4-60.5 ✓, A4 = gate-off values ✓.  **⚠ 05C/23C min 108.7 → 109.3**:
  same demand machinery (per-tie debug compared), but the T4+U network
  settles ~0.5-0.7 m HIGHER without km-chord drag → demands a shallower
  dip (109.32 vs 108.74 contact ceiling); joint-solve freeze-skips after
  feedback drop 16 → 2 (network MORE consistent).  Model-consistent —
  **needs the user's in-sim verdict** (prior ~108 was a chord-law
  number).
- Suite **307p/2f** identical to baseline (SPLP deferred lump + HECA
  within; HECA+SPJC vertical-curve XPASS kept).  CYXY byte-identical
  across PYTHONHASHSEED 1/2.
**NEXT:** (1) user in-sim eval (RESTART Ortho4XP — module cache) incl.
the 109.3 dip; (2) HECA residual: terminal4 seed-vs-route transition
(§6 step 2 — trial seed removal, separate measured step), Exit-3
exit-fan graph holes, #186 squeeze arbitration (the p10e P5 user
ruling); (3) measured deletes per §5.6 (band_exempt machinery, P2
exemption) once the user ratifies; (4) corridor st-bands onto the
shared band computation (deferred this step to protect invariants).

## ★★ SESSION 73 PART 10h (2026-06-10) — `corridor-curves` MERGED → dev @7e12384 ★★
User call (no other sessions active): merge everything.  Clean ort
merge (branch = src code only; dev since base = STATUS/docs only).
**Merged-dev suite VERIFIED: 307p/2f — identical to the branch
(CYXY + SPJC grade gates GREEN; SPLP deferred lump + HECA within=216
remain; HECA+SPJC vertical-curve XPASS kept).**  dev is now the
implementation base for the route-field model — START AT
`docs/route_field_model.md`.  ⚠ dev's in-sim state moved from p9 to
the full s73-p10 line (curve-aware junctions, runway-exit extension,
apron-mouth relax, P1-P4, W1-W3) — **RESTART Ortho4XP before any
in-sim build** (module cache).  The joint-corridor-solve worktree is
now redundant with dev (kept; prune when convenient).

## ★★ SESSION 73 PART 10g (2026-06-10) — `corridor-curves` @153ad57: CYXY + SPJC PER-AXIS 0/0/0; HECA steps 0; ROOT OF THE APRON RESIDUE FOUND ★★
W1-W3 from the 10f NEXT list (all general):
1. **W2 SKEWED-RECT END-PAIR CAP** (post-solve pre-write, corridor write
   loop): the chain solve caps a rect's drop over the MOUTH-MID span but
   the emitted plane's binding run is its SHORTEST LONG EDGE — skewed
   ends carry the full delta over less distance (CYXY stub A: 0.81 m
   legal over the ~50 m span = 1.74 % on the 48.9 m edge).  Minimal pair
   clamp (free ends first, frozen pairs split, hard never moves).
   ★ MUST run AFTER the per-chain solve — the pre-solve version was
   re-solved away (a singleton's near mouth is a MID-chain station).
   **CYXY per-axis 1→0 (FULL GREEN 0/0/0); SPJC per-axis 11→0 (FULL
   GREEN — its junction marginals were the same skew class).**
2. **W3 `_insert_junction_corners_into_grazing_apron_edges`** (pre-solve,
   pipeline; junction↔apron counterpart of the rect-corner pass): a
   junction vertex 0.73 m off an apron edge BETWEEN two shared corners
   (HECA #199↔#194) stepped off the apron's lerp at emit.  Route the
   apron boundary THROUGH the corner.  **HECA steps 2→0** (was the grade
   gate's first-failing assert).
3. **W1 DIAGNOSIS — the "write layer" was INNOCENT** (trace machinery
   kept, env `O4_TRACE_LL="lat,lon;…"`: per-node corridor write trace +
   enforce band trace + provenance Dijkstra for the binding lo-anchor).
   MEASURED: frozen tie values ARE delivered verbatim (G's 101.42
   station-written); the pass-2 re-consensus legitimately re-derives
   them post-relief; and the #261-class junction "cliffs" are per-axis
   EXEMPT diagonals (standalone check_grade without taxi_axes_ll
   OVER-REPORTS — always audit with /tmp/probes/s75_axis_audit.py).
   ★★ THE REAL apron-residue driver: the HARD 05C contact (108.74,
   n656) reaches the A-mouth area through ~2,500 m of CHAINED VISIBILITY
   CHORDS — ~580 m SHORT of the real taxi route — demanding ≥71.2 where
   the route-justified level is ~62.5 (edge-graph band [71.2, 62.7]
   = infeasible by 8.5 m, distributed as ~70 small #190/#194 apron
   violations).  This is exactly the km-scale chord under-measurement
   the USER-APPROVED route-field model (s73-p3 item #3) eliminates:
   visibility chords demoted to a LOCAL smoothness cap (~60-100 m),
   long-range law = taxi-route distance from anchors, validator changed
   identically.  **→ NEXT = BUILD #3.  It should drain the bulk of
   HECA's remaining per-axis within=216 (apron chains + terminal4's
   geodesic-vs-route tension + J3/U class).**
**HECA per-axis after 10g: within 216 / cross 0 / steps 0; runways
exact (05C 108.7, 05L 57.9-60.7, A4 climb, A5 flat).
★★ SUITE 307p/2f — CYXY GATE GREEN, SPJC GATE GREEN (session started
at 4f).  Remaining: SPLP (user-deferred lump) + HECA (within only —
route-field #3 territory).
Builds: /tmp/HECA_w3.osm /tmp/SPJC_w2.osm /tmp/CYXY_w2b.osm.
★★ NEXT SESSION STARTS AT `docs/route_field_model.md` (dev @ac72add) —
the FULL #3 implementation design: model statements, measured evidence,
per-site implementation map (solver + validator), route-graph known
gaps, noise margin, terminal transition, validation protocol, rulings
register (incl. the one SUPERSEDED ruling), ops notes.  User confirmed
the analysis 2026-06-10; the doc is the handover.**

## ★ SESSION 73 PART 10f (2026-06-10) — `corridor-curves` @0038375: P1-P4 BUILT — SPJC GATE GREEN; suite 306p/3f ★
Implemented the p10e plan P0-P4 (all general, no airport-specific code).
**SUITE 306p/3f (was 305p/4f): SPJC grade gate GREEN.**  Runways held
everywhere (05C 108.7 / 05L 57.9-60.7 / A4 / A5).  ⚠ ANALYSIS-CONTEXT
BUG found mid-session: a `cd` to the main repo made every relative-path
command run against DEV — the p10e "CYXY=flake / HECA 296 / SPJC 13+1"
audit numbers were DEV numbers.  True branch numbers re-measured below.
1. **P0 determinism: VERIFIED** — CYXY byte-identical across
   PYTHONHASHSEED 1/2/7 and across the P1-P4 changes (none fire there).
   CYXY's gate red is NOT a flake: it is ONE real per-axis-audited
   violation (stub A 1.84 % over 48.9 m, 693.5→692.6).  Root: a CHAINED
   singleton whose junction-shared mouth nodes were FIRST-WRITTEN by
   another corridor — same span-vs-tie-distance class as R1/R2 but for
   chained rects (the co-located-mouth tie allows it over the in-junction
   geodesic; the emitted rect runs it over span).  NEXT: mouth ties
   between stations whose rects SHARE nodes must use the lateral offset,
   not the in-junction geodesic.
2. **P1a co-level reconcile** (`_reconcile_level_coupling`, pre-writeback):
   post-enforce single-vertex passes (edge-plane snap) decohere rect
   flat-end pairs → plane emit averages → 0.2 m cross step at d=0 (SPJC
   V3↔#97, the gate's hard-fail).  Re-level: hard wins, else mean
   projected into external cap edges.  **SPJC cross 1→0.**
3. **P1b bridge ties at SPAN** (not ga+span+gb): the rect emits ONE plane
   over span; the twist paints junctions from corridor values so the legs
   never carry grade.  **SPJC R1/R2 4.4-4.9 % → 0; within 18→11** (all
   remaining ≤0.7 m junction marginals).
4. **P3 unified blocker rescue** in the tie freeze (subsumes p10d):
   per-blocker classify — apron-mouth untied terminus → minimal
   projection; frozen-tie member → move the GROUP into the tie's reach
   (vetoed by member-chain hard anchors + neighbour cap ties); others
   keep the veto.  ★ No side-effect moves (a clamp that pushes PAST the
   tie = don't move — T d=302 went 104.82→106.45 chasing 102.00 in the
   first build) and ★ project 1 cm INSIDE the limit (exact-boundary
   moves fail the float re-check — G's tie re-skipped at over-by 0.05).
   Mixed blocker sets now resolve (G = frozen ties + apron tail; the
   two-branch version required ALL blockers per kind → both passed).
   HECA #261's G tie now ACCEPTED (head freezes 101.4) — **but the
   junction still reads 100.0-vs-103.0: the WRITE layer (first-writer-
   wins, twist painting, per-chain band clamps) does not deliver agreed
   freeze values to the junction nodes.  That is the next layer; the
   freeze machinery itself is done.**
5. **P2 apron band-exemption** near held corridor writes: MEASURED NO-OP
   (HECA #190/#194 pairs unchanged) — band pinning is NOT what holds
   those pairs (kept: harmless; the p10d floor-noise numerology was
   suggestive but wrong as mechanism — the write layer again).
6. **P4 same-apron terminus projection** (post-freeze pairwise, replaces
   the cap-tie variant which dragged termini toward their chains'
   INTERIOR wishes — J fell ~2 m, seam grew to 1.9 m; measured,
   rejected): small safe moves; J/G2 seam itself still pending the
   write layer.
**HECA net: 372→375 standalone (noise); the gate now trips on 2 STEPS
(junction #199 ↔ apron #194 graze seam, pre-existing 0.49-0.54 m,
nudged to 0.51-0.59 by the T-tail apron-fill move) BEFORE within.
NEXT (priority): (1) the WRITE LAYER — deliver frozen-tie values to
junction nodes (first-writer-wins arbitration, twist vs station write
ordering); (2) CYXY shared-node mouth ties at lateral offset → its last
violation; (3) #199↔#194 graze conformance (s73-p3/p8 family);
(4) terminal4 (#3) lumps + the T4-wall arbitration (user ruling may be
needed).  Builds: /tmp/HECA_p6.osm /tmp/SPJC_final.osm
/tmp/CYXY_final2.osm; probe s75_spjc_r1r2.py (band/sharer audit).**

## ★ SESSION 73 PART 10e (2026-06-10) — FAILURE-CLASS MAP: SPJC/CYXY/HECA gates share one root ★
ANALYSIS ONLY (no code).  Per-axis audit numbers (the gates' own metric,
caps=0): HECA 296 within + 1 plane, SPJC 13 within + 1 CROSS (the assert
that actually trips: stub V3 #19 15.9 vs junction #97 16.1 at d=0 — the
p9 0.2 m emit step), CYXY = CLEAN standalone (3-test run PASSES; its
full-suite RED = the s72 intermittent parallel-suite flake; its 10
standalone-only viol are per-axis audit artifacts).  ★★ UNIFIED ROOT:
the surface is assembled from HELD subsystems (chains, twist writes,
runway contacts, band pins, apron edges); wherever two meet at different
levels the disagreement stays IN the surface on whatever element spans
the seam — because ties either (1) freeze-skip against NON-HARD blockers
(HECA #261: junction inherits T@103.2/T@101.6/G@100.0 at shared corners;
G's closing tie 101.42 died against G's own mid-chain FROZEN-TIE anchor
91.34), (2) use the wrong distance (SPJC R1/R2: bridge ties cap at
ga+span+gb through-path but the rect EMITS one plane over span 23-28 m →
4.4-4.9 %; probe s75_spjc_r1r2.py: corners shared with junctions
#117/#104 #118/#103, bands 25 m wide = NOT band-pinned), (3) never form
(apron not a tie medium: J tail vs G2 head 0.9 m, 50 m apart in #190;
V3 emit conflict), (4) pin free neighbours at band noise (HECA
#190/#194: floors 0.02 % route-noise above held writes), (5) hard-flex
blocked by the ONE-round limit (T4-wall: #242 = T4@106.0/T@103.6/
U@103.1/T@102.4 around one junction, 21 viol; 05C already at its route
ceiling 108.7 → residual CANNOT come from more runway dip).
PLAN (priority order, each step re-measured vs the ruled invariants
05C 108.7/05L/A4/A5/T-monotone/terminals-don't-rise):
P0 determinism (CYXY flake: repeat-build hash, find order-dependent
iteration, sort);  P1 SPJC: (a) V3↔#97 coincident-write reconcile (the
p9 twin pass misses it), (b) bridge ties ALSO cap the rect's own
end-pair at cap·SPAN;  P2 band-noise deadband/exempt for free apron
verts near held corridor writes (≈−60-80 HECA viol);  P3 freeze-skip
REDISTRIBUTION: when the blocker is a non-hard frozen-tie anchor,
re-open both groups + local feasibility re-solve (anchor
self-consistency extended to tie groups) — kills the #261 class;
P4 apron as TIE MEDIUM: same-apron chain ends ≤~100 m tie at in-apron
route distance (#290/J-G2 class; generalizes p10d);  P5 remainder:
T4-wall arbitration (#242 family — may need the part-1 option-(b) USER
ruling: route demand vs junction network at a ceiling-pinned runway),
terminal4 lumps, #247 plane 2.16 %, #199 v2e 0.54 m.

## ★ SESSION 73 PART 10d (2026-06-10) — `corridor-curves` @7e3a8ed: A-GAP ROOT CAUSE REVISED + FIXED (apron-mouth terminus relax) ★
HECA apt.dat was updated (Custom Scenery 15:31) — re-measured first: the
two A chains and their values are essentially unchanged (the cliff is
now junction #232 in the new numbering, 2.6 m, 63.3 vs 60.7; was #284).
**p10c's framing REVISED by measurement: the two A chains are 1.86 km
apart and apron #190 (59.2→91.7, 1.6×2 km) carries the taxi route
between them — NO same-ref end merge can bridge that (junction-adjacent
adjacency / ≤150 m end ties don't apply), and none is needed: the high
chain was ALREADY coupled to the A5 complex through its tie group
(consensus 61.76; route-band ceiling at the junction-side mouth 62.4 =
the user's predicted ~62.5; terminal7 is 749 m by route through the
apron lanes, floor ~58.8).**  TRUE blocker: a chain terminus at an
APRON MOUTH (mouth junc=None, ring nodes shared with a free apron)
anchors at the apron edge's DEM-settled value at station construction
(~L3346) and NOTHING downstream can move it — ties form chain↔chain
only, anchor self-consistency is intra-chain, the freeze veto's only
escape (runway flex demand) requires a runway blocker → the tie
freeze-skipped and the corridor wrote ~63.3 against the 60.4-60.7 carve.
**FIX (@7e3a8ed, in the tie freeze): when EVERY blocking anchor is a
non-hard UNTIED terminus whose mouth opens into a free apron, project it
MINIMALLY into the tie's cap reach and accept the tie.  ★ PROJECTION,
NOT UN-ANCHORING — the un-anchor variant was measured and REJECTED: J's
tail was 0.33 m infeasible and the flat extension dropped it 4.7 m
(68.1→63.4) = a manufactured 5.3 m wall against G2's legitimate
68.7-70.9 writes in the same apron.**
MEASURED: A-complex cliffs CLEARED (#232/#233/#228 gone; A profile
62.5..61.8(tie)..63.4); 05C 108.7 ✓ 05L 57.9-60.7 ✓ A4 carries the
climb ✓ A5 flat ✓.  The relax also fires on T head/tail, G, J — all
≤1.6 m nudges.  #261 grew 2.4→3.0 m (T-corridor junction spanning the
100-level mouth system vs the 103-level corridor — T4-wall family, NOT
new).  CYXY: relax never fires, 10 pre-existing exit-junction viol
unchanged.  SPJC: untouched (R1/R2 residual).  Suite 305p/4f = exact
branch baseline (same 4 gates, HECA+SPJC vertical-curve XPASS kept).
**REMAINING (new, precisely measured class): apron band-noise residue —
within 343→372 (#190 21→35, #194 4→23, ALL ≤1.7 m, most ≤1 m): free
apron vertices pinned at their route-band FLOORS 0.6-1.7 m above the
moved corridor writes (e.g. floor 64.0 = 108.7 − 1.5 %·~2,980 m route
from the 05C contact vs the corridor's 63.4 — a ~0.02 % route-graph
disagreement over 3 km).  The p4/p7 band-pinning class → NEXT: extend
the junction `band_exempt` treatment (or a route-noise deadband on
floors) to apron vertices near corridor writes; then CYXY exit
junctions, #350/#317, re-check the merge gate.**  apt.dat note: the
conformance WARN (4 edge crossings) pre-exists the fix and improved
from 5 with the new apt.dat.  Probes: /tmp/probes/s75_a25_band.py
(route bands + terminal routes at a mouth); builds /tmp/HECA_relax2.osm
(fixed) vs /tmp/HECA_aptupdate2.osm (pre-fix baseline, new apt.dat).

## ★ SESSION 73 PART 10c (2026-06-10) — `corridor-curves` @f6561c4: MERGE GATED (CYXY red); A-GAP DIAGNOSED ★
User asked to merge for in-sim testing → merge-gate checks run: **suite
on the branch = 4 failed (CYXY newly RED: 10 junction violations at the
exit junctions #64/#72/#75, 0.5-1.2 m — from per-axis + extension
effects), HECA #284 3.7 m unresolved, #350/#317 un-triaged → NOT merged**
(dev stays at part 9 = the user's current in-sim state).
★★ USER RULING (A-area model): 23R threshold ≈ 60 m → taxiway A near
A5 stays FLAT (~60) until it reaches the APRON — A5 flat at 60.4 is
CORRECT; the p10 attempts to anchor A5's top HIGH were the wrong
direction.  The #284 cliff = taxiway A's TWO chains never merging
(59.2-59.8 vs 62.9-65.9, 4.3 m apart across the A5 junction complex);
ONE chain ramps <1 % end-to-end.  Their mid-chain anchors are FROZEN
CONSENSUS TIES (not hard nodes — chain debug now prints provenance
H/R/A).  Same-ref phase-B merges no longer need a stub/wide bridge
(committed) BUT the A chains still don't merge: their ends don't share
a junction (adjacency = same-junc or shared-nodes; the gap spans the
complex).  NEXT (priority order): (1) extend phase-B adjacency across
junction-ADJACENT junctions (or tie same-ref chain ends ≤~150 m apart
at their in-network path distance) → A merges → #284 closes; (2) CYXY
exit-junction 10 violations (likely corridor writes at the exits
over-cap on within-junction chords the per-axis rule still keeps —
needs per-pair reading); (3) #350/#317; (4) full re-measure + merge.
Branch state otherwise GOOD: 05C 108.7 ✓, 05L 57.9-60.7 ✓, A4 1.1 % w/
#282 carrying the climb ✓.

## ★ SESSION 73 PART 10b (2026-06-10) — `corridor-curves` @cc4e7c6: T4 DIP RESTORED (108.7) ★
The part-10 "05C regression" was three separable bugs, all found by
reading the demand instrumentation (not a design flaw):
(1) THROAT fallback now SINGLETON-only — HECA's diagonal runways made
T4+U read tangential and the 216 m throat budget dissolved its demand;
(2) virtual-blocker demands carry the CONTACT need to runway vertices
≤60 m (adding the contact→vertex leg diluted 107.9→109.6; p9 semantics
restored); (3) route-reach bands relax to the chain's own curve-aware
distance at virtual-anchored stations (the reach graph cuts the same
curve corners and re-clamped the granted climb).  **05C min 108.7 ✓
(user ~108), 05L 57.9-60.7 ✓, A4 1.1 % with #282 carrying the climb ✓.**
STILL OPEN before merge: A5/#284 3.7 m — THREE anchor sources tried for
A5's far terminus (own nodes 60.5 = its own carve; far-junction median
60.5 = junction fully carved; far-junction max = still 60.5!) ⇒ the
64.4 uncarved level lives on a DIFFERENT shape than A5's mouth junction
— next probe: map A5's far mouth juncs-index → shape id, find which
shape holds 64.4, anchor against THAT neighbourhood.  Also #350 1.9 m /
#317 1.6 m un-triaged; within 493 — full SPJC/CYXY/suite re-measure
needed before any merge.

## ★★ SESSION 73 PART 10 (2026-06-10) — branch `corridor-curves` (worktree
## `.claude/worktrees/joint-corridor-solve`, commit `f5f81a8`, NOT merged):
## CURVE-AWARE JUNCTION GRADING + RUNWAY-EXIT EXTENSION ★★
★★ USER RULING (model, authoritative): the 1.5 % grade cap applies along
the taxi CENTERLINE.  Cross-axis junction diagonals are an UNREGULATED
direction (ICAO Annex 14 §3.9 / EASA CS-ADR-DSN.D.265/.280 regulate
longitudinal-along-route + transverse); the INSIDE edge of a curve is
shorter than the centerline and must be allowed to EXCEED 1.5 % for the
centerline to carry 1.5 %.  Straight chords under-measure turning routes
— that is why high-speed exit junctions (#282/#283) pinned flat and why
#291 (75's axis bending into 95's) can't blend.  A high-speed exit
junction must CARRY the climb from the runway to its rect (user: #282 ≈
1.8 m of rise from where the centerline leaves the runway to the A4
mouth; same for #283/A5).
BUILT on the branch (all measured at HECA):
1. `_PER_AXIS_JUNCTIONS=True` + the rule applied in
   `_build_shape_constraints` (the flag previously only affected the
   _build_edges path, NOT the visibility-chord junction constraints the
   enforce actually uses — the binding edge at #282 was a 47 m straight
   chord from A4's mouth to a runway vertex, cap 0.71 m, headroom 0.01):
   along-axis pairs cap at the ARC between projections, cross-axis
   diagonals DROP, ring-adjacent pairs always survive.
2. Curve-aware corridor distances (`_junc_axis_arc` ∨ multi-bend
   geodesic) in hard bands / twist clamps / ties.
3. RUNWAY-EXIT EXTENSION: chain termini at runway-touching junctions get
   a virtual HARD station at the runway contact (singletons like A4/A5
   survive chaining for this).  ★ Contact derivation matters: apt.dat's
   A4 line is 167 m but cuts the fan corner (40 m in-junction where the
   flow runs ~190 m — the curved exit line is among the 12 ingest-dropped
   runway-crossing centerlines and `layout.apt_taxi_centerlines` keeps
   the full set but does NOT contain it either); fallback = the fan's
   THROAT (farthest runway-adjacent vertex by in-junction geodesic).
   Value = runway-edge interpolation (corner-within-10 m silently
   skipped everything).  The virtual gap is a twist line source.
**MEASURED WIN: #282 now carries the climb — 59.4 (throat) → 60.4 → 61.3
→ 62.6 (A4 mouth) ≈ 1.5 % along the fan, and A4 7 % → 1.4 % — exactly
the user-specified behaviour.**
**WHY PARKED (not merged): (a) A5/#283 unchanged — its far-mouth nodes
were ALREADY relief-crushed to ~60.5 before the corridor pass ran
(different mechanism; #284 holds 3.9 m); (b) NEW residuals #302 3.7 m /
#192 1.2 m (per-axis freed surfaces moved); (c) the 05C T4 dip regressed
107.9 → 110.4 (virtual runway anchors changed the corridor demand
measurement — the freeze-skip demand path needs reconciling with the
virtual-station path).  NEXT: fix (c) first (keep the user-approved
~107.9), then A5's pre-corridor crush, then re-measure #291/#302/#192;
check_grade junction handling must mirror the per-axis rule (tests pass
`taxi_axes_ll` automatically when the flag is on; standalone runs
over-report).  dev is UNTOUCHED — the user's in-sim state stays part-9.**


## ★★ SESSION 73 PART 9 (2026-06-10) — dev `f7aa5ad`+`dc8f787`: GATE ON + CORRIDOR→RUNWAY FLEX FEEDBACK ★★
User flipped `TAXI_CORRIDOR_PROFILE` ON for in-sim evaluation and reported
(HECA): #291 flat at the crossing + dip/hump at its rect mouths; U(#32)
meets apron #241 steep/uneven; "once pavement reaches max grade why isn't
the runway flexing — dipping to 108 at T4 might resolve it."  All three
traced to ONE root: the T4+U corridor's anchors were infeasible (runway
contact ~110.4 vs the 101-104 network), every tie freeze-skipped, the
conflicting writes were guard-skipped and the enforce flattened the
junction interiors.  BUILT (dc8f787):
1. **CORRIDOR→RUNWAY FLEX FEEDBACK** — a freeze-skipped tie blocked
   at/THROUGH a runway contact (terminus on the runway-adjacent junction;
   budget += in-junction geodesic to the runway vertex) becomes a flex
   demand → restore pre-corridor surface → `_resmooth_runways_in_elev`
   through the bounds → full relief re-grade → corridor pass re-run.
   **ONE round only** (re-measuring after the relief chases the dip
   circularly: 107.9→106.3→wants 105.5 = the s68 over-dip class).
   **DIP demands only** (rises trace to DEM-settled free pavement that
   must fill toward the runway — the J chain re-manufactured the 05L
   +1.1 rise the p3 deadband killed).  ★ HECA: 05C contact 110.9 →
   **107.9 (user-predicted ~108)**, T4+U feasible end-to-end, #290 cliff
   1.9→~1.0 m, #291 = coherent tilted surface 101.8→105.4, U↔#241 seam
   continuous (apron edge == U low end 97.7); 05L untouched 57.9-60.7,
   thresholds intact (116.5).
2. **Twin + coupling writes** — corridor writes respect ≤0.1 m
   near-coincident twins (first writer wins) and sloping rects' END-PAIR
   co-level coupling (`_build_level_coupling` passed in; the plane emit
   carries ONE altitude per end — individually-twisted corners read as
   0.2 m cross steps at d=0).  HECA cross 3→0.
3. **Bridge ties** — an UNCHAINED rect linking two junctions is a real
   grade path: tie station pairs across it at the through-path geodesic
   (per-node legs), else the corridor descends legally along its route
   while the 23 m bridge reads the whole drop (SPJC R1 4.8 %).
4. **Twist = SEED, not hold** — per-junction local cap-projection sweep
   (two LOW-variance vertices near different sources evade the
   per-vertex disagreement guard: SPJC #107 1.4 m), then twist writes
   stay FREE for the enforce (corridor junctions are band-exempt; POCS
   knows the bridges/chords the twist cannot see).
**Suite 306p/3f**: SPLP+HECA pre-existing; **SPJC red gate-on** =
the V/Q/R complex residual (R1/R2 bridge flanks 0.9-1.4 m — persists
across held/free twist variants, so the flanks are pinned by something
OUTSIDE the corridor machinery; #107 0.5 m; one 0.2 m V3 emit step) —
NEXT: diagnose what pins R1/R2's flanking junction surfaces.  CYXY fully
clean (0 within / 0 cross / 0 steps).  ⚠ RESTART Ortho4XP before in-sim
builds (module cache).


## ★★ SESSION 73 PART 8 (2026-06-10) — dev `bc04e91`: HECA 2 GRAZE STEPS → 0 (exact-corner pinch split) ★★
The part-6 "REMAINING" item, root-caused by the prescribed in-build
instrumentation (`O4_GRAZE_DEBUG` + solve-time ring dump,
`/tmp/probes/s74_graze_site.py`).  The s73-p3 description was STALE: at
HEAD the corners ARE attached — junction -10193's ring traces TX29
corner-to-corner on the inner run (all 4 corners shared, the 144 m run
coincident with TX29's long edge) — but the ring's OUTER boundary edge
(e6, 314 m, alts 120.10→122.30) STILL runs collinear 0–0.5 m outside
that same long edge, enclosing a ~35 m² sliver tongue.  e6's 314 m lerp
vs TX29's plane (120.7→122.1 over the 144 m stretch) = exactly the
0.51/0.66 m mid-edge steps.  The corner-insertion pass skipped it
because its "already attached (exact ring vertex)" gate doesn't know the
corner is attached on a DIFFERENT, non-incident edge.
**FIX (pavement/vertices.py, `_insert_rect_corners_into_grazing_junction_edges`):**
1. Exact corners now search NON-INCIDENT edges for a graze (incident
   edges excluded — they legitimately use the corner).
2. Inserting there pinches the ring at the corner (coordinate appears
   twice → invalid).  The invalid-ring branch now resolves a deliberate
   pinch via buffer(0): largest part keeps the slot, other parts ≥50 m²
   re-added as junction shapes (vertex-push recovered-pieces contract,
   NN-resampled altitudes), the zero-area sliver vanishes; area-
   conservation guard (1 % + 50 m²) so a ring-fold that EATS a lobe
   (s70 lesson) is never accepted.  Non-pinch invalids still skip.
**MEASURED (HECA): exactly ONE pinch fires airport-wide — -10193 splits
into its two real components (43,102 m² + 824 m²), each conformed
corner-to-corner on its own short end of TX29; the long edge is now the
true shared boundary.  Mid-edge steps 2→0, within 293 / cross 0 / v2e 0
unchanged, verify byte-identical.  Suite 307p/2f, same 2 fails, NO new
failures: HECA's gate is now red on the within-293 evaluation-state
residue ONLY (caps are 0; pre-fix it failed at the steps assert first);
SPLP = the deferred lump.  SPJC + CYXY green, HECA/SPJC vertical-curve
XPASS kept.**  Note: suite ran on dev AFTER the part-7 joint-corridor
merge (`8db08fb`) — validates the combined tree.

## ★★ SESSION 73 PART 7 (2026-06-10) — branch `joint-corridor-solve`
## (worktree `.claude/worktrees/joint-corridor-solve`, commit `8306827`):
## JOINT CORRIDOR-NETWORK SOLVE BUILT — CYXY gate-on GREEN ★★
⚠ Worked in a WORKTREE off dev `61d8c33` because the part-6 shadow-edge
session was concurrently editing the main tree; their work landed as dev
`13531fd` and is MERGED into this branch (code auto-merged cleanly —
disjoint regions of unified_jacobi.py).  Worktree env: OSM_data +
Elevation_data rsynced.

**BUILT (the p5 missing piece — corridor graph as ONE system, all inside
`_taxi_corridor_profiles`, still `TAXI_CORRIDOR_PROFILE=False`):**
1. **STAGE A/B split** — stations are built for ALL chains first (geometry
   only), then inter-chain TIES: crossing gap-segments → ONE shared station
   inserted into BOTH chains (equality — one physical point); terminus
   against another chain's crossing run → projected station + grade-cap tie
   over the lateral offset; shared canonical nodes → equality; co-located
   mouths at one junction → grade-cap tie over the in-junction geodesic.
   The old sequential `"fix"`-station reconciliation (first-writer-wins) is
   REMOVED.
2. **Damped consensus solve** over tie groups: wish = flat interpolation
   between each chain's pins; group value = mean(wishes) clamped into
   route bands ∩ every member chain's anchor-feasibility; cap ties project
   pairs; FREEZE anchors the consensus into each chain, rejecting values a
   chain cannot cap-reach (honest conflict beats a manufactured cliff).
   Undamped it never converged (cap-tie projection vs wish averaging).
3. **Anchor self-consistency** — non-hard termini project onto pairwise
   cap-feasibility first (T4+U was pinned 110.4-HARD↔98.4-DEM over 611 m =
   1.96 %: NOTHING in between was satisfiable; G 1.7 %, J 1.93 % same).
4. **Junction HARD bands on TRUE geodesics** — corridor stations (per
   member NODE, not mouth-mid) and twist writes clamp against the crossed
   junction's hard ring vertices over in-polygon visibility-graph Dijkstra
   distances (`_junc_geo_table`).  Measured ladder at CYXY #74 (L-shaped
   junction E×runway 14R/32L): no band → 14 % (corridor flat-seeded 2.9 m
   above the runway vertex one junction-width away); direct-chord band →
   3.1 % (concave pair skipped); one-bend → 2.2-2.4 %; multi-bend → 0 ✓
   (one-bend OVER-estimates around double corners, so two writes both "at
   cap" violated their mutual chord).
5. **Enforce band-exemption** — corridor-touched junctions' free vertices
   are exempt from per-vertex route-band pinning in
   `_enforce_within_shape_grade` (new `band_exempt` param): the threaded
   corridor profile is the route truth there; the per-vertex bands
   (route-graph artifacts) held free vertices metres above held corridor
   writes — the p4 cliff class.
**MEASURED gate-on: CYXY 19 viol/14 % → 0 ✓✓ (the gate-on RED blocker —
which turned out NOT to be chain disagreement: CYXY has zero tie groups;
it was the corridor pass ignoring junction-local hard caps).  HECA: #217
0 violations ✓ (the p5 named case), #291 64 % (p4) → 13.6 %, T monotone
kept.  Suite gate-off 306p/3f (same 3 gates, 2 xpass) — byte-path
unchanged off-gate.**
**REMAINING (why the gate still defaults OFF): HECA gate-on within ≈459
vs 246 gate-off, dominated by the T4-WALL ROUTE TENSION — the freeze
isolates T/T4+U ties as infeasible by ~1.1-1.7 m (the 110.9 05C contact
forces ≥106.9 at 234 m down T4 while the crossing network sits at
102-104; #290 holds the 1.9 m cliff between the two corridors' mouths
7.4 m apart).  That is the part-1 arbitration (route demand vs junction
network — runway-flex / option-(b) displacement-demand territory), NOT a
corridor-disagreement bug; the joint solve now MEASURES it precisely
(`O4_CORRIDOR_DEBUG=1` prints per-tie freeze-skips with over-by).
Debug: `[corr] joint network: N tie group(s) … freeze-skipped=K`.**

## ★★ SESSION 73 PART 6 (2026-06-10) — dev `13531fd`: SPJC GRADE GATE GREEN (shadow-edge reconciliation) ★★
User: confirm the SPJC/HECA gates real, fix root cause (SPLP deferred to the
joint corridor solve).  Both CONFIRMED byte-identical at HEAD.  Root cause =
two blind spots in the vertex-based push/snap machinery where a junction
borders a rect/runway without shared nodes:
1. **Corner-into-grazing-edge insertion** (pavement/vertices.py, runs in
   Phase-1 geometry AND in `_unify_airside_geometry` before the weld): a
   junction EDGE grazing past a rect/runway CORNER with no junction vertex
   nearby now routes THROUGH the corner (taxi 0.6 m / runway 1.5 m capture).
2. **Edge-plane snap** (unified_jacobi, post-enforce; `O4_STEP_DEBUG` prints
   counts): junction vertices ≤2.0 m off a sloping rect's OR RUNWAY'S long
   segment take the edge-plane altitude + local re-project.  The first build
   of this pass (removed earlier in s73 as "0 applicable") had EXCLUDED
   runway edges — which is exactly SPJC's case: a SHADOW edge from a shared
   runway corner whose far endpoint sat 1.74 m off holding 15.1 vs plane
   16.26; both endpoints on the plane ⇒ the straight edge lerps along it.
**SPJC gate GREEN (2 steps → 0). Suite 307p/2f, no new failures.**
REMAINING:
- **HECA's 2 graze steps** (junction -10193 ↔ TX29) — ✅ FIXED in part 8
  (dev `bc04e91`, exact-corner pinch split; the "solve-time differs"
  hypothesis was wrong — the corners were exact ring vertices on the
  inner run and the gate skipped them).
- SPLP 1 × 0.4 m junction lump — deferred (user) pending the JOINT
  CORRIDOR-NETWORK SOLVE (part 5).

## ★★ SESSION 73 PART 5 (2026-06-10) — dev `c61ba6e`: ROUTE-FIELD #3 PIECES BUILT (gated) ★★
User ratified the full surface model: rects slope in ONE direction; junction
arms TWIST from flat mouth cross-sections to the max COMPOUND slope at the
center and back out; DEM = starting point, correct grade is king.  Built into
`_taxi_corridor_profiles` (still `TAXI_CORRIDOR_PROFILE=False`):
1. **Route-band threading** — per-station bands from `_runway_reach_bands`
   (taxi-route Dijkstra from runway anchors; seam intersect TODO); flat seed
   clamped into bands + iterative worst-violation anchoring with a taxi-cap
   consistency guard (the runway bounded-re-smooth pattern ported).
2. **Junction TWIST pass** — crossed junctions' vertices blend crossing
   corridors' profiles (line sources) + mouth station values (point sources)
   by inverse-square lateral distance; weighted-variance DISAGREEMENT GUARD
   skips genuinely conflicting vertices (leaves them to the enforce).
3. Cross-ref merges restricted to stub/wide-short bridges (T4→U class).
**MEASURED (HECA, gate on): T monotone through -10292 ✓, T4+U+U ~2 % ✓,
#291 internal 64 %→25 %, runways unchanged.  REMAINING BLOCKER: independent
same-ref chains disagree at SHARED junctions (#217 3.9 m / 49 % — every
chain flat-seeds between its OWN termini, no joint consistency; crossing
reconciliation is sequential, not simultaneous), within 552, and gate-on
flips CYXY's grade gate RED → default stays OFF.**
**→ NEXT (the one missing piece): JOINT CORRIDOR-NETWORK SOLVE — treat the
corridor graph as ONE system: shared-junction crossing elevations are
common variables; solve all chain profiles simultaneously (route bands +
taxi caps + Δg as constraints), then twist-blend.  Everything else
(chains, stations, bands, twist, relief/enforce holds) is in the tree.**
Gate-off suite: 306p/3f (CYXY green re-verified).

## ★★ SESSION 73 PART 4 (2026-06-10) — dev `3853b9a`: TAXI-CORRIDOR PROFILES BUILT, GATED OFF ★★
User follow-up on #291: a corridor must carry ONE continuous grade through
junctions ("can't distinguish where they join" — T through junction -10292;
T4 flowing into U).  Root cause measured: nothing in the solver PREFERS a
monotone corridor — shapes settle DEM-near and a V-notch at a junction is
per-pair cap-legal (T read 111.7 → 104.5 ↘NE-mouth, 105.0 ↗SW-mouth → 103.5
= flat-to-REVERSED where one ~1 % ramp exists; `faa_joint_solve` is a
feasibility PROJECTOR — fed the V it keeps it, since Δg over 100s-of-metre
segments is far inside the 1/3000 rate).
**BUILT (`_taxi_corridor_profiles`, config `TAXI_CORRIDOR_PROFILE`, ⛔ OFF):**
- CONNECTIVITY-driven rect mouths (shared-node clusters vs junctions/rects —
  a wide-short connector's taxi axis is junction→junction across its SHORT
  side: U is 12.7×75 m; its geometric axis mis-profiled it at 9.4 %).
- Same-ref chains bind first; then chain ENDS merge cross-ref (every rect is
  a phase-A seed, so candidates are CHAIN ENDS — that merge is how T4+U+U
  formed); wide-short ends use the whole-chain travel direction (their own
  axis is noise).
- FLAT SEED between anchored stations (runway model: flattest line through
  pins, DEM lowest), then faa_joint_solve at taxi caps
  (`TAXIWAY_MAX_GRADE_CHANGE_PER_M` now in config; driver re-exports).
- Crossing-reconciliation stations (runway-crossing rule: later corridor
  bends THROUGH the earlier one's value); rect bodies + junction crossing
  bands take the profile; corridor nodes held through a full
  `_directional_relief` re-run + the enforce (`held_extra` param added).
**MEASURED gate-on (HECA): the named cases LAND** — T 101.3→113.0 monotone
~0.8 % straight through -10292 ✓; T4+U+U chain forms, T4's wall 3.4-4.8 % →
~2 % steady ✓; the corridor writes even fixed one TX29 graze step.  **BUT
within 293→604 and junctions crossed by TWO corridors get up to 64 %
internal cliffs (#291: T's band ~103.3 vs T4→U's band 107.1, 6 m apart)** —
two missing pieces, BOTH route-field-model parts:
  (a) **TILTED-PLANE junction crossings** — the user's "roll and yaw near
      equal": each band write is laterally flat; two crossing corridors on a
      diagonally-sloping junction are consistent only at the crossing point.
      The junction needs a fitted plane (or the band writes need lateral
      gradients) honouring both corridor profiles.
  (b) **Route-floor-aware corridor seeds** — T's flat seed ignores the
      05C-route demand entering via T4 (the junction area must be ≥~106-109
      to climb to the runway at ≤1.5 %); the enforce's runway-reach bands
      then hold free junction nodes UP against held corridor nodes = the
      cliffs.  Corridor station bands must intersect the route bands.
**→ This IS STATUS #3 (route-field model).  Start there next session; the
machinery (chains, stations, crossing reconciliation, profile writeback,
relief integration) is in the tree, gated.**
Suite at close: 306p/3f — SPJC 2 runway edge-steps (pre-existing), HECA 2
junction↔TX29 graze mid-edge steps (part-3 known residual), SPLP 1 NEW
marginal (junction #19 internal 3.02 %/0.4 m — the POCS-lump class, appeared
in the mixed tree with the concurrent DSF session's SPLP changes; its
historical stub/B class stays cured).  ⚠ Concurrent-session note: the DSF
session (051eebc/aee3129) and this one interleaved uncommitted config.py
edits; their `DSF_PAVEMENT_MATERIAL_TOKENS` was briefly clobbered by a
config reset here and re-added by them — the committed list is theirs and
authoritative.  ⚠ RESTART Ortho4XP before in-sim builds.

## ★★ SESSION 73 PART 3 (2026-06-10) — dev `bb74ed4`: JUNCTION VISIBILITY + ROUTE-NOISE DEADBAND ★★
User analysis session ("why is T4 110.9 / A4 rising / #291 flat?") produced two
fixes + the #3 design direction (deferred until after in-sim test):
1. **Junctions grade through `_visible_grade_edges`** with chords tested
   against the **AIRSIDE-PAVEMENT UNION** (a cross-notch chord over a
   neighbour's pavement = real grade path; over a true void = excluded).
   The all-pair web had pinned long-armed junctions near-flat with fictitious
   cross-arm chords (#291: 71/228 chords outside the polygon; its 6 tightest
   all fictitious) so the taxiway-T grade piled into #75 (4.1 %) instead of
   flowing #16→#291→#75.  check_grade has visibility-gated junctions since
   s62 — solver now matches.  Result: #291 slopes, #75 4.12→3.44 %, U
   12.6→10.1 %, **SPLP grade gate GREEN (first since s62 — its stub/B class
   was junction-pinned). Suite 307p/2f.**
2. **Route-noise deadband** (`_ROUTE_NOISE_FRAC = 0.04`) in the demand
   synthesis: traced the A4 floor to the T4 contact at 3,217 m route where
   reality (A4≈60, T4≈110, ≤1.5 %) needs ≥3,353 m — the route graph
   under-counts ~4 % (straight endpoint stubs: 125 m on this corridor;
   uncurved row joins).  A route demand below 4 %·cap·route_d no longer
   flexes a runway; clearing demands keep FULL depth (threshold semantics —
   shrinking by noise under-flexes T4).  **05L back to exactly 57.9–60.7
   (user real-world ✓), T4 keeps 110.9, Exit-3 stays clean** (its dip now
   0.66 m chain-driven; the chain drains via sloping junctions/pads).
   `_flex_route_bands` returns per-bound binding-anchor distances.
   Per-role cap integration along routes = currently a NO-OP (every pavement
   role caps 1.5 % in config) — only worth building when caps diverge.
- **05C now also carries a small route-justified rise near 23C (max 117.1,
  +0.6)** — the 05R-network corridor demand; symmetric flex, env-feasible.
- **★ KNOWN NEW RESIDUAL (geometry, next session): 2 mid-edge steps (0.66 m
  worst)** — junction `-10193`'s 314 m edge GRAZES rect TX29's long edge for
  ~140 m (lateral 0.49→0.01 m) with NO shared vertices, so its straight lerp
  cannot follow the rect plane once the junction slopes (the other side of
  the same junction shares the edge line and matches the plane EXACTLY).
  Altitude-only snap measured 0 applicable vertices (deviation peaks
  mid-edge).  FIX = pre-solve conformance: conform grazing junction edges to
  the rect corner projections (the s68 `_near_edge_line` coincident-run
  class).  HECA's grade gate now fails on exactly these 2 steps.
- **#3 (user-approved direction, START AFTER IN-SIM VERDICT): route-field
  model** — long-range pavement law = taxi-route distance from all anchors
  (per-role caps integrated along the path), visibility geodesic demoted to
  a LOCAL smoothness cap (~60–100 m pairs), validator changed identically.
  Derivation in the s73 conversation: real-world T4≈110/terminal7≈70 with a
  2,030 m in-polygon chord is only possible if the real pavement is
  interrupted — km-scale chords systematically under-measure; terminals then
  EMERGE at their route positions (no seeds needed).

## ★★ SESSION 73 PART 2 (2026-06-10) — dev `6be62f6`: EVALUATION STATE LIVE ★★
User call after part 1's arbitration writeup: **see it in-sim with the gate on
and terminals allowed to slope.**  Merged `flex-demand-synthesis` → dev
(`1b58781`, clean — the concurrent KPHX session had committed 4cf9db8..1bee887
by then), then `6be62f6`:
- **`O4_FLEX_MIN_CLAMP` now DEFAULTS ON** (`=0` restores the legacy
  combined-band flex; the crossing guard still routes CYXY-class airports to
  the legacy path).
- **`config.TERMINAL_PADS_SLOPE = True`** (new): every pad may slope up to
  `TERMINAL_MAX_GRADE` via the apron visibility path (the s66-designed
  config switch, now actually wired in `_build_shape_constraints`); `False`
  restores rigid-flat with squeezed-pad exceptions.  `check_terminal_flat`
  already self-disables (cap > 0); terminals emit per-vertex.
- **Per-pad isolated polish** after the final enforce (seam-preserving:
  shared/hard nodes held) — global POCS leaves sloping-pad interiors lumpy;
  pads converge in isolation (s67 lab).  ⚠ measured nearly no-op at HECA
  (294→identical worst pairs): the terminal4 lump pairs are BOTH-ends-shared
  with apron #249 = the s66 band-conflict class, not pad-internal.
**Measured (HECA, this state): 05C/23C min 110.9 ✓, Exit-3 cleared ✓, 05L
+1.6 at A4/23R, runways FAA-clean — `test_runway_vertical_curve` HECA+SPJC
flip to XPASS.  within 36→294** (115 of them ≤0.5 % over; worst: U connector
12.6 %, terminal4 interior 12.5 % (≈1-2 m lumps, see above), apron #249 T4
side 10 %, A4 stub 7.0 %).  Sloping pads drained STEP2 480→325 model edges
but ADDED pad-interior/band-conflict residue vs flat-pads gate-on (185).
**Suite 306p/3f — the SAME 3 pre-existing gates** (SPJC 2 runway edge-steps
0.61 m, SPLP stub/B ~1.98 %, HECA within), 2 xpass as above; compare_target
green (fixtures tolerate the changes).  ⚠ RESTART Ortho4XP before building
(module cache).  Revert levers: `O4_FLEX_MIN_CLAMP=0` env;
`TERMINAL_PADS_SLOPE=False` config; both → byte-equal s68 shipping.
NEXT after in-sim verdict: the within-294 residue classes (band-conflict
pairs #249/terminal4, U connector, A4 stub route-floor under-prediction —
part-1 option (b), the (a)-displacement demand term).

## ★★ SESSION 73 PART 1 (2026-06-10) — branch `flex-demand-synthesis` (worktree
## `.claude/worktrees/flex-demand-synthesis`), commit `a515cea` ★★
⚠ Worked in a WORKTREE because another session was concurrently editing the
main tree (bridges/KPHX: bridges.py, layout.py, pipeline.py, finalize.py,
verification.py uncommitted there).  Do NOT merge this branch to dev without
checking that session's state.  Worktree env: OSM_data rsynced; the 2 extra
suite skips are tile_cut_parity (raw HGT tiles absent here), environmental.

**Suite 304p/3f (same 3 intentional gates, CYXY green).  Gate-off HECA on this
code = byte-equivalent shipping baseline: within 36, 05C min 104.4.**

### WHAT WAS BUILT (s68 NEXT #1, all gated `O4_FLEX_MIN_CLAMP`)
Per-runway demand at each pavement contact = **min over the measures that
EXIST** (a one-sided "no demand" is absence of evidence, not a veto):
1. **CHAIN measure (term 1)** — `_grade_bands` over the pavement edges
   WITHOUT apron shapes' visibility chords, anchored at {held terminal seeds
   + thresholds/seam + OTHER runways' nodes at current values}, own runway
   unpinned.  Carries pavement-internal pins the route cannot see (Exit-3 via
   #207↔L → 05R dips 3.6 m, Exit-3 cleared ✓).  Apron chords excluded because
   they manufactured +10.5 m floors on 05L (cross-apron chains from high
   anchors where the real route needs none — the s66 artifact class).
2. **ROUTE measure (term 2)** — `_flex_route_bands` at every contact;
   carries apron-borne demand (T4 → 05C ceiling 110.92 via n762) and CAPS
   the chain where both measure (route is authoritative, user 2026-06-09).
Mechanics that made it land (each fixed a measured failure):
- **Sequential per-ref commit, deepest demand first** (`only_refs` param on
  `_resmooth_runways_in_elev`): 05L measured against the unflexed 05C read
  inflated anchors.
- **Pairwise bound-consistency pruning** in the re-smooth: a rise floor and
  dip ceiling chain-infeasible at the cap (05C: floor 118.1 two metres from
  ceiling 113.2) anchored in turn = 255 % wall → drop the SHALLOWER demand.
- **Anchor-consistency guard**: a bound must be cap-reachable from every
  already-added anchor (envelope filter only checks the initial ones).
- **End-grade-aware slack** (0.8 % in the end fraction) in all three
  consistency layers — uniform 1.5 % accepted end-region anchors the FAA
  solve could not legalise (1.56 % at 05L d=2963 → whole flex reverted).
- **Sub-5 m station merge** (demand path only): `faa_rate_of_change_pass`
  spirals on 2 m junction-sliver stations (solved 28.85 between 71/67
  anchors).
- **(a) combined-band settle SKIPPED under the gate** (its chord-graph
  over-dip is what the synthesis replaces); demands measured at snapshot0 =
  the held-runway saturation state.  Post-commit re-grade = **full
  `_directional_relief` re-run** against the committed profiles.
- Initial mistake worth remembering: contact-EDGE excess at snapshot0 is ~0
  everywhere — the directional relief pushes violations OUTWARD, so the
  saturation demand is only visible as a BAND quantity, not on the edge.

### MEASURED (HECA, gate on): profiles ✓, within ✗
- **05C/23C min 110.9** (the user-expected value), 05L +1.6 at the A4/23R
  zone, 05R 139.6→136.0 at the Exit-3 chain.  Runways FAA-clean: worst
  adjacent grade 1.24 %, curvature 0, `improved=True`, committed.
- **Exit-3 GONE from check_grade** (was 2.07–2.81 %).
- **BUT within 36 → 185** (89 of them ≤0.5 % over; 9 over 5 %): worst
  U-connector 11.7 %, A4 stub 7.95 % (3.5 m/44 m), apron #249 chains, stub
  T4 3.8 %.  Cross/v2e/mid/plane all 0 in both builds.

### ★ THE ARBITRATION THE USER MUST MAKE (quantified this session)
The two rulings collide at HECA: **(i) 05C dips only to the route-justified
~110.9** and **(ii) within-shape grade is measured on the geodesic visibility
graph with terminals held at their seeds**.  Every metre of dip the route cap
refuses (104.4 → 110.9 = 6.5 m at T4) reappears as pavement violations on the
geodesic chains (terminal7@70 + 1.5 %·~2,030 m geodesic ⇒ apron ceiling
~100.5 at T4 vs runway 110.9).  Gate-off "within 36" is only achievable
because the (a) solve's 104.4 over-dip absorbs that tension into the runway —
the very dip the user rejected visually.  Options measured/identified:
  a. Accept the honest residual at 110.9 (gate ON, within ≈185, runway right).
  b. Next design lever: per-contact demand from the **(a)-settled saturation
     displacement** min route depth — captures A4's true +3.5 (the route
     floor under-predicts +1.64 because the apron is pinned ABOVE its
     route floor by its own chains); does NOT close T4 without breaking the
     route cap.
  c. Terminal-cluster slope extension / apron-metric arbitration (the T4
     chain ceiling comes from the held seeds + geodesic; user has ruled
     terminals must not rise and within-apron grade stays geodesic — those
     two plus 110.9 are jointly infeasible; one must bend).
**Gate stays OFF until the user rules.**

### Worktree / measurement notes
- Build+measure exactly as s68 (`/tmp/probes/build_heca_synth.py` builds from
  the worktree; `tools/check_grade.py`; `s69_runway_mins.py` per-runway
  min/max).  Debug: `O4_FLEX_DEBUG=1` now prints per-contact demand synthesis
  (`[flex]   contact …`), per-round re-smooth anchors/bans, and final
  worst adjacent grade per ref; `O4_FLEX_BAND_DEBUG=1` prints the binding
  route anchor per banded node.
- `demand_ref` (s64 single-anchor mechanism) REMOVED from
  `_resmooth_runways_in_elev` — superseded by the bound-driven anchor loop.

---

# Auto-Patch Status — session 68 = GEOMETRY SWEEP (Exit-2/3, U seam, hole-router v2, blast pad) + GROUNDSIDE RULE + FLEX DEMAND R&D (gated)

## ★★ SESSION 68 (2026-06-09/10) — branch `dev`, all committed through `7961235` ★★
Suite **306 passed / 3 failed** (intentional HECA/SPLP/SPJC grade gates; CYXY
green; the +2 over s67 = hole-router unit tests + flat-edge invariant).  dev
also absorbed two EXTERNAL sessions mid-stream: s69/s70 `Smart-data-cleanup`
(HEAZ verify clean, ribbon T-junctions, phantom-WARN fix) and the Phoenix
triage (short-edge keep-largest fixes) — see their memory files.

**HECA shipping state (gate off): within 36 (~18 unique), cross/v2e/mid 0/0/0,
05C/23C min 104.4, coverage gaps 7 (groundside-emit class).**  User verdict on
the build: "best HECA yet."

### Commits (ours, in order)
3845907 Exit-2/3 junction recovery + runway coincident-run conform (vertices.py)
f64750b/65e2cba/37423b2/2de3ad4/7961235 STATUS updates
1f8e849 MERGE `redesign-hole-router-v2` (`plan_hole_cuts_v2`, `O4_HOLE_ROUTER_V2` ON)
ba2305d U-connector seam: all-rect conform + flush-contact span ends
d99907c plane profile restored (spline experiment reverted)
642c8b2 runway-disconnected aprons → groundside (+SPJC target re-cut, apron 19)
5537626 flex demand anchor + groundside chord grade limit
844a59a demand anchor GATED OFF (locked a false 05C over-dip 102.1)
3f8093a route-graph augmentation + demand-path audit
c78e9b7 bounded demand re-smooth (iterative anchors + threshold-envelope filter)
e39537e #174 blast-pad spike: ORPHAN-NODE SYNC in the re-smooth
9488cff per-runway ROUTE-BAND flex (gated WIP) + measured results

### Geometry fixes (all live, all verified in-sim by user)
1. **Exit-2/3 ↔ 05R/23L junction restored.** The vertex-push's buffer(0)
   keep-largest silently deleted the 11,568 m² connector piece; pieces ≥50 m²
   are now re-added; junction conforms to the runway corner-to-corner
   (`_near_edge_line` coincident-run collapse + span-end corner snap ≤10 m).
2. **U-connector seam closed** (user caught it: NOT a source void — apt.dat
   covers 61/68 m²). Same push pathology vs a TAXI rect; flush-contact span
   ends on sloping edges are now LEFT for `_split_sloped_rects_at_violations`
   to convert into shared corners. U steps 8/5/20 → 0/0/0.
3. **Hole-router v2 merged.** Prim min-spanning-forest slits + polygonize
   application + sibling-merge instead of drops. Radial fan gaps (670+2,543 m²)
   GONE. Remaining 7 coverage gaps = groundside-emit class (diagnosed, open).
4. **#174 blast-pad single-node spike fixed** (the 5.76 % worst pair). Blast
   pads ARE modelled flat (g=0 chain segments) and #174 solved flat — except a
   mid-edge conformance insert the flex freed and the re-smooth couldn't write
   (only chain STATIONS are written). Fix = orphan-node sync: non-station ring
   nodes interpolate the smoothed profile. ⛔ hold-don't-free variant froze
   multi-vertex pieces (within 39→105) — documented, reverted.
5. **Groundside rule (user-authoritative): an apron must have a touch-chain to
   a runway, else it is GROUNDSIDE.** `_reclassify_runway_disconnected_to_
   groundside` (junction_repair; STRtree BFS from runways; DEM-follow with
   simplify_tol=0; runs BEFORE tile_cut — the clip severs cross-tile chains)
   + `_grade_limit_groundside_chords` (groundside.py, called LAST in finalize —
   `_separate_groundside_from_airside` re-derives altitudes, ORDER MATTERS):
   4 % Lipschitz pull-down over chord pairs, cluster-unified shared nodes.
   CYXY 9 → groundside (grade test stays green), SPJC 2 (target re-cut),
   HECA 38 (+15 disconnected TX service-lane rects reported, left airside —
   user call pending).

### Model rulings this session (user-authoritative — overrides older notes)
- **Runway flex is SYMMETRIC** (dip OR rise the minimum).
- **Terminals must NOT rise**; terminal7 belongs ~70; the 6/7/10 cluster
  resolves tension by SLOPING (per-node seeds exist since s67) — the
  "raise terminals to the chain metric" proposal is REJECTED.
- **ROUTE BANDS are the authoritative runway-reachability metric.** The
  21-hop constraint-graph audit chain (ceiling 104.16 at T4) rides an 839 m
  cross-apron geodesic hop = the s66 measurement-artifact class. 05C at T4
  should be ~108-110, not 104.

### Flex demand R&D — what was measured (ALL gated `O4_FLEX_MIN_CLAMP`, default OFF)
- **Why narrow demands vanish:** the flex band solve DOES dip runways at
  junction demands (417→57 edges), but the unanchored FAA re-smooth fills
  NARROW notches (lifting one station is the minimal move) while BROAD dips
  survive — that's why 05C keeps its 104.4 dip today with no anchor, and 05R
  shows nothing at Exit-3 (2.81 %).
- **Demand anchor at the settled value = over-dip locked** (05C → 102.1; 05R
  exit → −3.3 m where 0.9 m is needed). The POCS 50/50 split overshoots below
  true demand; the re-smooth refill used to hide ~2 m of it.
- **Per-runway ROUTE-BAND flex (9488cff)**: bounds from {held terminals + all
  thresholds + OTHER runways' pavement contacts at current values}, own
  centerline EXCLUDED (else the threshold rides the freed interior as a
  fictitious 1.5 % rise-corridor — false 102.09), bounds applied ONLY through
  the bounded re-smooth (raw clamping lifted 05L to a 7.3 % wall 30 m from
  its locked threshold). RESULTS: 05C → 110.9 ✓ (user-expected), 05L +3 m at
  A4 ✓ (the s65 split rule)… BUT Exit-3 unchanged (its pin is PAVEMENT-
  INTERNAL saturation #207↔L — no hard anchor on the chain, invisible to
  route-to-anchor bounds BY CONSTRUCTION) and within 41→167 (one re-grade
  cannot redistribute 3-6 m profile moves).
- Machinery in the tree, all gated: `_flex_route_bands` (route graph +
  runway-centerline augmentation + per-ref anchors/exclusion), bounded
  `_resmooth_runways_in_elev` (per-station demand bounds, iterative anchor
  addition ≤8 rounds, threshold-envelope filter), orphan-node sync (LIVE,
  ungated), per-ref reset-and-bound loop in `_relax_runway_and_resolve`.

### NEXT (priority order)
1. **FLEX DEMAND SYNTHESIS** — per-runway demand at each pavement contact =
   **min( contact-edge EXCESS after a HELD-runway pavement-saturation solve,
   route-band justified depth )**. First term = the user model verbatim
   ("pavement to max grade first, then the runway flexes the minimum so every
   junction meets it") — gives direction+locality, captures Exit-3's 0.9 m;
   second term caps magnitude per the route doctrine (T4 ~108-110, never the
   chord 12 m). Feed as bounds to the bounded re-smooth, then a FULL relief
   re-run against committed profiles (not just one re-grade). Validate: 05C
   ~108-110, Exit-3 ≤1.5 %, within ≤ 36 and falling, CYXY/SPJC/SPLP suite
   unchanged. Then default the gate ON.
2. **s65 crossing-anchor injection fix** (runway_segments ~L973-1331): land the
   agreed crossing E_x in the SECOND runway's profile (CYXY 14L/32R floats
   695.9 vs 693.7); then lift the flex crossing guard.
3. **Blast-pad segment seams** — extend the pavement-intersection seam
   collection into the blast-pad zone (beyond thresholds) so junctions snap at
   segment corners there (the #174/#286 join was a mid-edge insert because no
   corner existed; user expects a segment).
4. **Easy +0.22 m at #207** (enforcement convergence polish) — trims Exit-3
   2.81→~2.5 % independent of item 1.
5. **Groundside-emit coverage class** (HECA 7 / SPJC 1 / CYXY 2 gaps):
   deconflict keep-largest + simplify(2.0) boundary movement (agent-diagnosed).
6. **Short-span micro-bumps** #313/#252/#312/#314 (0.3-0.4 m over 7-16 m) +
   the 23R-end squeeze cluster around #315 (A held by chain vs locked 60.7
   threshold — shared corners verified EXACT; the "0.1 m" the user saw is
   #315's over-cap interior descent, not a seam mismatch).
7. Disconnected TX service-lane rects (15 at HECA) — groundside? (user call).

### Handover notes for the next session
- **Tree state**: dev `7961235`, clean. Everything experimental is behind
  `O4_FLEX_MIN_CLAMP` (default off) — shipping behavior = the user-approved
  state. `O4_HOLE_ROUTER_V2` defaults ON (the new router IS shipping).
- **Numbering**: shapeIDs in user conversation = standalone build numbering
  (`/tmp/HECA_review.osm`-era); production patch = standalone+2 (memory).
- **Measure**: build ≈90-120 s; `tools/check_grade.py /tmp/X.osm --top-n 40`;
  runway profile probe pattern in /tmp/probes/s68_05c_profile.py; demand-path
  audit /tmp/probes/s68_pathaudit.py; per-pass void/coverage attribution
  pattern /tmp/probes/s68_allfn3.py + s68_vtrace2.py (function-wrap with a
  RING-AREA metric — min-distance metrics get masked by legitimate touches).
- **Restart Ortho4XP** after edits (module cache) before any in-sim check.
- ⚠ Do NOT: raise terminals; use chord/constraint-graph chains as runway
  demand magnitudes; apply flex bounds outside the bounded re-smooth; trust
  `improved=rwy-grade-only` as a pavement gate (it commits do-nothing flexes).
---

# Auto-Patch Status — session 67 = DEMAND-DRIVEN RUNWAY FLEX + TAXI-ROUTE TERMINAL SEED + SLOPING SQUEEZED TERMINALS (HECA 168→28)

## ★★ SESSION 67 (2026-06-09) — branch `dev`, UNCOMMITTED → committing this milestone ★★
A large session that reworked the runway/terminal elevation model to the user's
spec. **HECA within-shape 168 → 28**, **CYXY 17→0 (grade test now GREEN)**, SPJC 0,
SPLP 4. Suite **302 passed / 3 failed** (HECA/SPJC/SPLP pre-existing; CYXY flipped
red→green; no green→red). Build WARN and `tools/check_grade.py` now AGREE (28=28).

### THE MODEL (user-authoritative, this session)
1. **Pavement grades to max grade first** (terminals yielded), THEN the runway
   flexes the MINIMUM — dip OR rise — so every junction meets it. End state = 0
   violations; any residual = a measurement bug or broken geometry.
2. **Runway flex is SYMMETRIC — it may DIP or RISE the minimum** (user revision
   2026-06-09, superseding the earlier dip-only wording). Code agrees:
   `_relax_runway_and_resolve` step-(a) combined band solve is direction-agnostic
   — it dips toward a saturated-low junction and rises toward a saturated-high one.
3. **Terminal flatness is LOWER priority than grade.** A pad seeded at its
   taxi-route grade-feasible level is FLAT where it can be, and SLOPES only when it
   straddles a low and a high runway and cannot be one level in grade to both.
4. **Metrics are config-driven**: the validator + runtime WARN read
   `ROLE_GRADE_LIMITS[role]` per shape, so changing a cap in config updates the test.

### WHAT LANDED (all in `elevation_per_surface/unified_jacobi.py` unless noted)
1. **Demand-driven runway flex.** `_relax_runway_and_resolve` step (a) already
   solved the combined runway+pavement band with terminals held — that settled
   interior IS the minimum demand. The old code threw it away for the inter-runway
   route-band; now it KEEPS it and just re-smooths for FAA curve/end-grade. Removed
   the inter-runway `_runway_route_bands` (it was over-flexing 05L/23R +5 m toward a
   far higher runway). 05L/23R bulge 67 → ~63; A4 descends instead of a 21.8% cliff.
2. **Taxi-route terminal seed** (`_seed_terminals_from_taxi_routes`, called in
   `per_surface_solve` after `_seed_elevations`). Each terminal vertex routes (via
   `taxi_routing`) to each runway; band = ∩ `[E_R ∓ cap·route_R]`. Written into BOTH
   `elev` and `dem_elev` (so Phase 1's DEM-seed + every fit use it). FLAT clusters
   (feasible combined band) seed one level; SQUEEZED clusters (infeasible band) seed
   PER-NODE → slope. HECA 6/7/10: terminal7 ~71.5 (low, near 05L/23R) → terminal6
   ~76 (high, near 05C), 0.97 % over 318 m.
3. **Squeezed terminals GRADE, others stay flat.** `_build_shape_constraints`: a
   terminal in `layout._sloped_terminal_nodes` (marked by the seed) gets the apron
   visibility-graph cap; every other terminal is rigid flat (cap 0) regardless of
   the config cap. `config.TERMINAL_MAX_GRADE = APRON_MAX_GRADE` is now the MAX a
   terminal MAY slope (used by the validator), NOT a mandate to grade every pad.
4. **Removed the upward-ratcheting terminal yield.** `_yield_terminals_alternating`
   (raised terminals toward high held neighbours) and the reverse-pass
   `_rigid_shift_terminal` are GONE — the seed sets the level; the apron grades DOWN
   to it. (~250 lines of dead code removed: those two + `_runway_route_bands` +
   `_flex_demand_anchors`.)
5. **Config-driven WARN + shapeIDs** (`elevation._report_within_shape_violations`):
   reports the per-role config cap (not hardcoded 1.5 %), lists the top-8 worst
   shapes with `[#shapeID]` + role/ref + grade (matches `verify_and_log` /
   check_grade). `verify_and_log` already emits + runs the check_grade engine and
   reports shapeIDs — that IS the test's metric.

### REMAINING (HECA 28, the geometry/measurement residue)
- worst = **runway-end GEOMETRY sliver** runway/05L/23R `[#174]` ↔ junction `[#313]`
  5.8 % (0.8 m / 13.9 m) — a junction-vertex cut at the 23R threshold; NOT a solver
  gap. The other ~26 are apron/junction marginals (20 of 28 are ≤0.5 % over cap).
- SPJC fails on 2 runway edge-steps; SPLP on the seam-tilt runway (5). Both
  pre-existing classes.
- one xpass→xfail shifted (a runway vertical-curve test — the profile changed with
  the new flex); worth a look.

### Diagnostics / probes (`/tmp/probes/`)
`O4_FLEX_DEBUG` (flex commit + combined-band (a) count), `O4_SEED_DEBUG` (per-cluster
flat/SLOPED seed). Probes: term_seed/term_seed_flat (band per terminal),
cluster_geom (slope feasibility), saturation, check3, all_grade, check_fix.

### NEXT
- the runway-end sliver (#174/#313) — junction-cut geometry fix at the 23R threshold.
- keep driving HECA 28 → 0 (apron/junction marginals); confirm the xfail shift.

# Auto-Patch Status — session 66 = WITHIN-SHAPE measurement fix (CENTERLINE bands) + config-driven terminal grade + inverted-hi/lo fix

## ★★ SESSION 66 (2026-06-08) — branch `claude/lock-thresholds-centerline-dist` ★★
Committed: **`f88b6bc`** (inverted altitude_high/low fix) + **`10b3d8b`** (centerline
runway bands + config-driven terminal grade). Working tree clean. Suite **301
passed / 4 pre-existing grade gates fail (CYXY/SPLP/SPJC/HECA — identical on the
clean branch, NOT a regression) / 2 skip / 2 xfail / 2 xpass.**

### ★ THE BIG CORRECTION (user-authoritative) — the "giant apron" is NOT infeasible
STOP concluding HECA's mega-apron is infeasible / must be SPLIT. It is a REAL
airport, thresholds correct, ALL pavement within grade in reality (the real airport
grades aprons to the STRICTER EASA **1%**). Any runway "squeeze"/band-pinning is a
**DISTANCE-MEASUREMENT bug**: the within-shape VISIBILITY geodesic shortcuts ACROSS
a big apron's interior, so propagating a runway's elevation through it under-counts
distance and FALSELY band-pins pavement between two runways (measured: a node 114 m
from 05L read only 2554 m geodesic to 05C; its real **centerline** route is 3093 m →
feasible). Memory fully corrected: `apron_grade_euclidean_vs_geodesic.md`,
`runway_flat_profile_route_band_flex.md`, 3 MEMORY.md index entries.

### WHAT LANDED
1. **Inverted altitude_high/low FIXED** (`f88b6bc`): `layout.canonicalize_high_low_ring`
   rotates a 4-corner ring by 2 when the (0,3) pair is the LOWER end → emit
   `altitude_high ≥ altitude_low` (X-Plane positional contract; surface bit-
   identical). +4 tests. (The original session-66 trigger bug.)
2. **Centerline runway-reachability bands** (`10b3d8b`): `_runway_reach_bands`
   (unified_jacobi) measures runway connections along the taxiway CENTERLINE route
   (`taxi_routing`), not the cross-apron geodesic; within-apron grade stays
   geodesic; seam stays geodesic. **HECA band-pinned 443 → 11** (the rest were
   false). Used by the final `_enforce_within_shape_grade` pass.
3. **Config-driven terminal grade**: `_role_grade` reads `config.ROLE_GRADE_LIMITS`
   (single source of truth); `flat = (cap == 0)`; `config.TERMINAL_MAX_GRADE` (0 =
   flat default, behaviour-preserving; raise → terminals grade like aprons via the
   same visibility path, no flag). `_writeback` + `check_terminal_flat` honour it.
4. Diagnostics (env-gated): `O4_STEP_DEBUG` (per-pass within-edge counts —
   STEP1 forward 1632 → STEP2 relief 379 → STEP3 flex 296), `O4_ENFORCE_DEBUG`.
   Perf finding: the difference-constraint Dijkstra band is **6 ms**; the within-
   shape POCS projection PLATEAUS at ~2000 sweeps (NOT 20000) — perf is a non-issue.

### ★ NEGATIVE RESULT — do NOT retry the Bellman-Ford projection as-is
Replacing the POCS projection with a difference-constraint (Lipschitz inf/sup-
convolution) solve REGRESSED (edge-viol 168→824). Root cause (corrects the earlier
"POCS plateaus above the floor" hypothesis): the residual is a genuine **CONFLICT
between the per-node centerline bounds and the visibility edges**, not a convergence
limitation. `_runway_reach_bands` bounds EVERY node by its own nearest-centerline
distance; two visible-adjacent apron nodes near DIFFERENT taxiways (one→05L, one→05C)
get bounds ~14 m apart, while the short visibility edge demands ≤cap·d — unsatisfiable
by ANY solver at ~239 edges. POCS at 2000 sweeps is already near that floor. The
band MODEL is the issue, not the fixed-point finder. (DC-project code was written +
reverted; don't resurrect without fixing the band model first.)

### ★★ NEXT TASK — HYBRID band model (centerline-to-connection + geodesic-inward)
The within-shape COUNT is still 168 (unchanged) — the measurement is now right and
the false squeeze is gone, but the actual reduction needs this. Make the bounds and
the visibility edges CONSISTENT: apply the centerline runway-reachability bound at
the apron's CONNECTION nodes only (where taxiways meet the apron boundary), then let
the interior grade INWARD from there via the within-apron visibility geodesic. So the
interior is not independently over-bounded; it follows its boundary. Implement as a
combined-graph Dijkstra (centerline-route edges between shapes + visibility edges
within a shape) seeded from runways — OR seed the geodesic band-Dijkstra from the
connection-node centerline values. Then the band-clamp + a short projection should
reach the true floor (≈ the 11 genuinely band-pinned), and graded terminals (EASA 1%)
should make HECA fully compliant.

### ★ IDEA TO EXPLORE (user 2026-06-08) — push residual conflicts to GROUNDSIDE terminal edges
Where a terminal genuinely must absorb a grade conflict (it sits between
incompatible levels), bias the terminal's slope so the STEEP part lands on its
GROUNDSIDE / landside edge (curbside / roads — `ROLE_GROUNDSIDE_PAVEMENT`, already a
separate 4 % role) and the AIRSIDE (apron-facing) edge stays at the apron-compatible
level. That pushes any residual grade issue AWAY from where aircraft taxi/park, onto
the car side where 4 % is fine. Worth testing as the disposal route for the last
irreducible conflicts: terminals slope toward groundside, airside edge held compliant.

### Probes (`/tmp/probes/`): geodesic_vs_centerline, centerline_dist, accurate_split,
### conv_centerline, speed_test, apron_feasibility, find_inverted_osm, squeeze_split.
### Measure: `O4_ENFORCE_DEBUG=1` + grep `[enforce]`; `tools/check_grade.py /tmp/*.osm`.

---

# Auto-Patch Status — session 65 = RUNWAY FLAT PROFILE + MINIMUM INTER-RUNWAY FLEX (centerline route-band)

## ★★ SESSION 65 (2026-06-06) — branch `claude/lock-thresholds-centerline-dist` ★★
Full model + every dead-end + correction: **memory `runway_flat_profile_route_band_flex.md`**
(indexed top of MEMORY.md). READ IT before touching runway elevation — we
re-derived this many times.

### THE MODEL (user-authoritative — do not re-derive)
Runway PROFILE priority: **1. CIFP thresholds + tile seam (HARD). 2. Runway-runway
CROSSINGS (shared point; closest-threshold runway sets `E_x`, the other bends).
3. Pavement junctions at MAX grade — the runway flexes the MINIMUM toward a
junction pinned at its FAR end by ANOTHER runway/seam. 4. DEM = LOWEST** (texture
only). Initial profile = flattest piecewise-linear through {thresholds, seam,
crossings}, NO DEM; the solver then flexes the minimum toward another-runway/seam
pins. **Distance for inter-runway feasibility MUST be the taxiway CENTERLINE
route** (`auto_patch/taxi_routing.py`), NOT the solver's within-shape grade graph
(it chord-cuts junctions / shortcuts across aprons → under-counts → over-flexes;
HECA T4→23R: graph 3014 m vs centerline 3236 m).

### WHAT LANDED (committed: `9cf59ae` thresholds-locked + `24c5290` flat-profile+route-band; rollback = `dev 9ef97ba`)
- **Thresholds locked**: removed step-3/step-5 threshold-relief re-solve passes
  (pipeline.py) + pruned dead code (runway_redistribute.py). CIFP thresholds
  never move.
- **Flat seed**: `config.RUNWAY_DEM_FOLLOW_BAND_M = 0.0` (was hardcoded 5.0); wired
  in `runway_segments.generate_patch_osm`. Cross-runway threshold-PROJECTION
  anchors disabled (`_SEED_CROSS_RUNWAY_PROJECTION_ANCHORS=False`) — they pinned a
  runway to a parallel runway's DEM (CYXY 14R/32L's 4.5 m dip). Crossing
  reconciliation kept.
- **Centerline route-band flex** (`unified_jacobi`): `taxi_routing` (+`distances_from`);
  `_runway_route_bands` (per-runway most-binding inter-runway anchor, symmetric
  dip/rise, ∩ own-threshold envelope); `_runway_crossing_nodes` held in re-smooth
  + pavement re-grade; `_relax_runway_and_resolve` interior branch uses the
  route-band, NO flat-seed (minimum move), **gate is GRADE-only** (the discrete
  curvature kink is a FALSE POSITIVE — the long rects emit with the SPLINE profile
  which smooths the joint; `layout._slope_profile_for` → "spline" >300 m).

### RESULT (NOT in the default suite; measure per-airport)
- **HECA**: 05C/23C dips to **~108 at T4** (the 05L/23R-route-feasible level), grade
  0.98% compliant, thresholds intact; within-shape **211→168**. 05L/23R & 05R/23L
  flat. ✓ the user's intended profile.
- **CYXY**: 14R/32L now flat 14R→crossing, **stub A compliant**. BUT within 22 /
  cross 10 / steps 52 — the **02/20+14L/32R crossing tears**: `agreed` E_x reaches
  02/20 but NOT 14L/32R (second-runway anchor-injection bug, runway_segments
  ~L973-976; 14L/32R floats to 695.9 vs 02/20's 693.7).
- **SPJC**: within **0** ✓; 4 residual edge/mid-edge steps (runway-vertex-near-
  junction-edge geometry).
- Suite: CYXY/SPJC now FAIL the grade gate (were green) — by-design honest
  exposure of pavement-side residuals the old DEM-following masked. dev `9ef97ba`
  is verified-green rollback (299p/2 intentional gates).

### OPEN (priority order)
1. **CYXY crossing-anchor injection** — make the `agreed` crossing E_x land in
   BOTH runways' profiles (14L/32R loses its copy). Smallest, clearest bug.
2. **Apron SOLVER ENFORCEMENT GAP** (the dominant HECA within residual) — ★ NOT
   "apron-fill", NOT all-pair, NOT the apron's 48 m span. Aprons grade by GEODESIC
   in-pavement VISIBILITY (`_visible_grade_edges`), which handles big spans. The
   violations are tiny LOCAL bumps (0.4–1.0 m over 6–18 m) between CLOSE, FREE,
   apron-only points → the visibility-grade pass leaves a few constrained pairs
   non-compliant (no final polish / incomplete convergence). See memory
   `solver_apron_enforcement_gap.md`. Solver-quality, fix in the solver.
3. **`check_runway_profile` spline-awareness** — the STRICT curvature CHECK (test
   gate) still flags the 05C dip as a kink (same discrete false positive the flex
   gate now ignores). Make the check model the spline to green the test.
4. Connector endpoint tension (cross_connector G 1.66% — ends pinned slightly too
   far apart) + SPJC junction-edge slivers.

Probes `/tmp/probes/`: heca_runway_waviness, cyxy_stubA, heca_why_within,
heca_apron_viol_detail, taxi_route_distance, centerline_route, flex_binding.
USER FEEDBACK (durable): do NOT revert experiments before review; do NOT keep
proposing "apron-fill"/"all-pair"/"split the apron".

---

# Auto-Patch Status — session 64 = RUNWAY-FLEX DEMAND-ANCHOR (taxiway pulls runway middle down) + curvature-noise + step-5 detection

## ★★ SESSION 64 RESULT (2026-06-05, dev — commits 0d383ba/4a278e7/33bcfeb/b86328d) ★★
Memory: **`step5_tension_finder_distance_bug.md`** (full detail).

**Trigger:** HECA shape #74 (ref T, a 44 m primary_parallel rect) graded 2.06 %, invisible
to the build WARN. Root cause was the runway-FLEX re-smooth, NOT a threshold problem.

**What landed:**
1. **Runway-flex demand-anchor (THE fix, 4a278e7).** The flex band-solve correctly pulls a
   runway centerline node DOWN where a taxiway connects (05C/23C middle 110→101.8, serving
   T via the T4 join), but `_resmooth_runways_in_elev` filled the lone dip back up to terrain
   (104) — `faa_joint_solve` only enforces grade between samples, so it raised the single low
   node toward its 110 m neighbours. Fix = **`_flex_demand_anchors`**: find the SINGLE worst
   point a taxiway pulled the runway below terrain and add it to the re-smooth's anchor set
   (user's tile-seam analogy); the whole-runway regrade then carves ONE smooth FAA profile
   threshold→low-point→threshold. **HECA within 47→22, #74 FIXED, 05C middle 104.2→101.92,
   NO threshold change, runway curvature unchanged.** Anchor only the WORST point — a shallow
   0.6 m dip just past the deep one pins the runway high → 1.85 % grade → reverts.
2. **Step-5 cap-weighted detection (33bcfeb).** `relieve_grade_via_inter_runway_split` used
   geometric centroid-route distance → over-estimated grade budget → never fired. Now uses the
   true difference-constraint band (cap-weighted Dijkstra from runway anchors) to name the
   binding runway connection points; accepts on the geodesic min-sum metric. **DORMANT at HECA**
   (the flex resolves the tension first); kept as the last-resort lever. Threshold-lowering is
   the WRONG lever for a MID-runway connection (proven — `faa_joint_solve` keeps the terrain
   interior; only the flex lowers the middle), right only for END connections.
3. **Curvature "kinks" were a TEST artifact (b86328d).** `check_runway_profile` reported 3 HECA
   kinks (1.06–1.33× the 1/30000 rate) — all within 0.1 m EMIT quantization. Runway altitudes
   round to 0.1 m; grade-change noise ~0.1·(1/Ll+1/Lr); the check used `noise_m=0.05` (HALF emit
   precision). Bumped to 0.10 → 0 violations; CYXY/HECA/SPJC `test_runway_vertical_curve` XPASS,
   SPLP stays XFAIL (genuine 1.83 % seam tilt > noise). The flex's own `_runway_profile_compliance`
   (same algo, UNROUNDED elev) already saw it compliant.
4. **Observability (0d383ba).** `_report_within_shape_violations` skipped all rects → WARN 6 vs
   validator 47. Now audits every role with a non-None `ROLE_GRADE_LIMITS` cap. WARN 6→47.

**Suite: 299 passed / 2 failed (intentional HECA+SPLP grade gates) / 2 skipped / 1 xfailed /
3 xpassed.** No regression; SPLP not regressed (~8→7).

**OPEN / NEXT:** remaining HECA within (22) = apron/stub/cross_connector classes (stub/B 3.04,
cross_connector/D 3.54, apron #303 3.61, etc.) — the giant-apron coherent-fill + steep-short-span
classes, NOT runway-related. SPLP seam-tilt runway (1.83 %, the seam-curvature follow-up). Probes
`/tmp/probes/s64_*.py`; flex debug `O4_FLEX_DEBUG=1` ([flexanchor] line). Anchor (30.10895832, 31.43477812).

---

# Auto-Patch Status — session 63 = RUNWAY GRADE+CURVATURE COMPLIANCE (re-smoothing guard, seam, step-5)

## ★★ ACTIVE (2026-06-05) — runway grade+curvature through the solver ★★
Memory: **`runway_curvature_solver_redesign.md`** (full detail + remaining work).

**Problem found:** the per-surface solver's runway-flex (`_relax_runway_and_resolve`,
unified_jacobi) BROKE runways — pulled 05C/23C interior ~8 m down to meet low aprons,
shattering the vertical-curve rate (1→9 kinks, |Δg| 0.0095→0.0285/m) while keeping grade
≤1.5%. Its acceptance checked grade only (meter-tolerant), no curvature.

**Shipped (commits dde52af, ccbc139, + this one):**
1. **Re-smoothing flex guard** (`_runway_profile_compliance` / `_resmooth_runways_in_elev`
   / `_runway_centerline_chain`). The flex re-smooths the flexed runway to an FAA
   grade+vertical-curve compliant profile (full centerline, `faa_joint_solve`), re-grades
   pavement against it, accepts only if the runway stays compliant. CYXY stub-A flatten
   survives → CYXY runway fully compliant (test_runway_vertical_curve XPASSES).
2. **Seam priority** — when a tile seam crosses an airport it is the HARD anchor; thresholds
   yield to keep the runway compliant WITH the seam (SPLP seam level keeps the band's tilt,
   ≤1.5% absolute; `_SEAM_CURV_KINK_ALLOWANCE=2` interim for the seam-crossing kink).
3. **Step 5 = inter-runway threshold split** (`relieve_grade_via_inter_runway_split`, gated
   `O4_INTER_RUNWAY_SPLIT`). SHAPE-ADJACENCY route graph (`_build_pavement_graph`,
   `_dijkstra_from`) for accurate taxi-route distances; GEODESIC violation detection
   (`run_grade_checks`, NOT all-pair); per violation, route to each runway, use the
   EXIT-POINT elevation (not the far threshold — that was the measurement bug that invented
   false 05-end gaps), feasible band, split the binding pair. Resets clean pre-solve
   geometry before each re-solve (solver is NOT idempotent — pipeline snapshots
   `_presolve_shapes`).
4. **check_runway_profile invariant** + 2 tests (`test_runway_longitudinal_grade` hard gate
   green on all; `test_runway_vertical_curve` xfail tracking; CYXY XPASSES). Profile-sample
   merge widened to 5 m so junction-cut slivers aren't mis-read as grade kinks.
5. **★ Terrain-extrema cuts OFF by default** (`config.SPLIT_LONG_RECTS_ENABLED` now defaults
   off; `O4_SPLIT_LONG_RECTS=1` restores). Was splitting straight taxiway/runway sections at
   terrain peaks/valleys; verified NOT needed for grade (HECA/CYXY/SPLP runways stay ≤1.5%,
   CYXY fully compliant). Gated BOTH the taxi `split_long_rects_along_terrain` AND the runway
   peak/valley seams (runway_segments.py, previously always-on). compare_target RE-CUT.

**Current HECA runway state:** uniform grade 0 violations; 3 marginal curvature kinks
(1.1–1.4× the very tight 1/30000 rate) + 1 end-grade 0.83% — redistribute discretization,
the "tighten initial profile" follow-up. Pavement within-shape ~46 = LOCAL taxiway/apron +
the mega-apron coherent-fill problem (a DIFFERENT class than runway/threshold gaps).

**HECA finding (re T4):** with the corrected EXIT-elevation measurement there is NO
inter-runway threshold gap — taxiways connect to the runway's naturally-low MIDDLE
(~104–110 m), attainable. The old flex pulled the runway INTERIOR down (not the threshold);
the guard prevents that. **User to confirm in X-Plane whether T4 truly needs 23C moved.**

**OPEN / NEXT:** step-5 multi-gap convergence (a 23-end split currently reverts); the
mega-apron coherent-fill (HECA ~46); vertical-curve smoothing of the marginal kinks
(memory `vertical_curve_extrema.md`); seam-curvature follow-up. UNCOMMITTED: layout.py
`_slope_profile_for` spline-on-long-rects experiment (user evaluating — now more visible
with extrema cuts off).

**Deferred (was the session-63 ACTIVE, now parked):** the HOLE-ROUTER GAP redesign
(global node set + min slits) — memory `hole_router_gap_redesign.md`. Kept
`verification.check_source_coverage` (interior-gap detector). The single-bridge slit attempt
was rejected (turned the apron into one polygon with embedded rects — violated "maintain the
holes"); reverted. **Re-investigation showed the wedge gaps are TERMINAL↔APRON conformance
strips, NOT router-caused** (present router on AND off; aprons have no holes) — see below.

---

## ★★ SESSION 63 RESULT (2026-06-04) — TERMINAL-YIELD SOLVED (commit 118695b, dev) ★★

The session-62 NEXT task is DONE. The HECA T8 apron ramp (T8 at 78.2 ≈ DEM 79.3,
bridged over the 1.5 M m² mega-apron to the 6/7/10 group at 73.2, a ~2.9 % ramp)
is fixed. **HECA within-shape grade 12 → 9** (the 3 T8-ramp pairs gone); suite
unchanged **295 passed / 2 failed (intentional HECA+SPLP grade gates) / 2 skipped**;
SPJC/SPLP/CYXY grade + geometry + compare_target green; `O4_GEOM_GUARD=1` stays 0.

**The fix** = new post-pass `_yield_terminals_alternating` (unified_jacobi.py,
called at the END of `_directional_relief`, after the held band-projection). It
reconciles aprons-bridged terminals by BLOCK-COORDINATE ALTERNATION (NOT the
joint free-terminal relaxation that sloshed in every prior attempt). Each round:
(1) yield each terminal the MINIMUM from its current solved level into the
grade-feasible interval its neighbours allow; (2) re-grade the aprons/junctions
(`_project_within_bands(held_extra=terminal_nodes)`) to follow. Converges in ONE
round at HECA.

**Why the coordinated plan in the old session-62 handover (below) did NOT work as
written, and what actually did** — two findings, each load-bearing:
  1. **Bound the terminal interval by HELD neighbours ONLY** (`is_hard[j] or
     j in terminal_nodes`): other terminals + runway/seam. A FREE apron node is
     NOT a constraint — step 2 regrades it to FOLLOW the terminal (the apron
     fills/cuts). Counting free apron nodes was the bug behind all the drift the
     prior attempts hit: the mega-apron's north descent pins a free apron node at
     79.2 just 24 m from T8, which ratcheted T8 UP to match instead of letting it
     drop → the whole network slowly inflated toward the high anchors and never
     converged (seen via `O4_YIELD_DEBUG=1`: every terminal creeping up each round).
  2. **Clamp toward the CURRENT solved level, NOT the DEM centroid**
     (`min(max(cur,tlo),thi)`). T8's DEM (79.3) sits ABOVE the compliant zone, so
     a DEM-anchored clamp (`term_level0`) pulls it back up and resists the drop.
Visibility (geodesic) adjacency built from the shape-constraint edges, NOT all-pair
`cap_adj` (which fabricated a phantom 1.2 km chord to far high terminals). Removed
the superseded `O4_FREE_TERMINALS` flag. Resolution: the GROUP rises 73.2→75.6 to
meet T8 (stays 78.2), gap 2.6 m = 1.5 % (both splits are valid fixed points).
Measure harness `/tmp/probes/heca_grade_measure.py`; apron dump
`/tmp/probes/heca_apron275.py`. Memory: `runtime_vs_test_grade_gap.md` (#3 ✅ SOLVED).

## ★★ REVERTED — apron flanking-corner weld (commit 17abbc7 → reverted 08699cf) ★★
The `weld_flanking_corners` attempt (weld the #303↔#369 0.3 m cross-step by
snapping two ~0.5 m-apart corners flanking a shared corner onto one coordinate)
FIXED the cross step (cross 1→0) but **introduced 2 T-junctions + 2 edge
crossings** — moving a vertex in the dense conforming partition lands it on a
third shape's edge → Triangle4XP mesh slivers (the exact thing conformance
prevents). Re-running conformance after the weld cleared the T-junctions but left
1 edge crossing (conformance fixes T-junctions, not crossings). Not worth a
geometry defect for one grade step → REVERTED; geometry clean again (0/0).
**Proper fix (deferred):** #369 is a 91 m² apron FRAGMENT — MERGE it into host
#303 (the "merged 8 small apron fragments" pass missed it; lower its threshold /
widen its criteria) so the sliver never exists, instead of moving vertices.

## ★★ DESIGN — smooth vertical curves at taxiway/runway extrema (Task D, READY to build) ★★
**Problem:** runways AND taxiways now emit as long `profile=plane` sloping rects
sliced at terrain extrema; a plane meeting a plane at an extremum = a sharp
hump/valley. Verified on HECA: kinks up to **1.78%** Δg at peaks (primary_parallel
F/TX41, runway 05R/23L), flanking pieces 100–945 m long, needed vertical-curve
length `L=K·|Δg|` only ~40–53 m (taxi, K≈30 m) / ~200–400 m (runway, K≈305 m). So
there's ample room for a short transition zone. Probe: `/tmp/probes/extrema_kink.py`.
Dormant constants ready: `driver.MAX_TAXIWAY_GRADE_CHANGE_PER_M = 1/3000`,
`config.RUNWAY_MAX_GRADE_CHANGE_PER_M = 1/30000`.

**Profile mechanism (no Ortho4XP change needed):** `O4_Vector_Map.include_patches`
densifies each `altitude_high/low` rect every `cell_size` (10 m) and sets each
interior point to `altitude_high − rnw_profile(x)·(high−low)`. Profiles available:
`plane(x)=x` (linear/kink), `spline(x)=3x²−2x³` and `tanh` (BOTH symmetric S, flat
at both ends). Emit (`layout.py` ~779/797/828) currently hardcodes
`profile=PATCH_SLOPE_PROFILE`="plane" on every rect.

**Two findings that shaped the chosen approach (user: HYBRID):**
1. tanh/spline are symmetric (flat BOTH ends) → suited to flat→ramp→flat, NOT
   slope→slope: between two sloped sections they leave residual kinks at both
   boundaries (end-grade ~0 ≠ approach grade). The discretized PARABOLA (plane
   micro-cascade) is the only correct shape for all cases w/o a new O4 profile.
2. Smoothing is NOT just a profile tag NOR just geometry cuts — the transition
   ALTITUDES must follow the vertical curve, and `L=K·|Δg|` needs the SOLVED
   approach grades. So the feature is two-part: PRE-solve geometry (carve the
   transition-zone cuts) + POST-solve altitude (fit the curve). Post-solve part is
   altitude-only → `O4_GEOM_GUARD` stays 0.

**Chosen build = HYBRID:** monotonic BENDS → single tanh transition rect (fewer
shapes); PEAK/VALLEY apexes (non-monotonic, the worst kinks) → plane micro-cascade
(N short rects, grades stepping through the curve length; fully smooth, grade-
compliant by construction). Shared runway+taxiway helper.
**Implementation steps:**
  (a) Emit plumbing: add `Shape.slope_profile`(+`steepness`); emit it instead of
      the hardcoded `PATCH_SLOPE_PROFILE` (layout.py). Needed only for the tanh path.
  (b) Pre-solve: in `split_long_rects_along_terrain` (+ a runway sibling, factored
      to one helper), at a peak/valley slice carve a transition zone of length
      `L_vc` (use a terrain-grade estimate for L since the solve hasn't run);
      tag pieces so the post-solve pass can find them.
  (c) Post-solve altitude pass (altitude-only): for each tagged extremum, read the
      solved approach grades + apex, fit the parabola; bends → set the tanh rect's
      endpoints+steepness; peaks/valleys → set the micro-cascade rects' altitudes
      to the stepping grade. Guard every rect ≤1.5% (taxi)/runway cap.
  (d) Prototype on a real HECA extremum, dump along-axis grade continuity + view in
      X-Plane, tune, then generalize. Add a grade-CONTINUITY check to check_grade?
**Gate:** SPJC/SPLP/CYXY unchanged; `O4_GEOM_GUARD=1` stays 0; HECA extrema visibly
smooth in X-Plane.

## ★★ NEXT — remaining HECA/SPLP within-shape grade (pre-existing) ★★
HECA grade gate is still RED on within-shape (9) + plane (1), now UNRELATED to
terminal-yield or the corner step. Per the ★★ USER PRINCIPLE (below) all are
solver gaps. HECA: stub/B 3.04 %, cross_connector/D 3.54 %, stub/S 1.99 %, apron
#303 3.61 %, primary_parallel/S 1.59 % (runway-parallel); + plane apron #267
1.79 %. SPLP: stub B / primary_parallel A ~1.8 % (runway-parallel taxiway
following the ~1.9 % sloped runway). Two sub-problems remain: (a) connector/stub
rects grading too steep over short spans, (b) runway-parallel taxiways inheriting
the runway slope. Build ≈55 s; `venv/bin/python /tmp/probes/heca_grade_measure.py`
reports within/plane/cross + terminal levels.

---

## ★★ SESSION 62 RESULTS (2026-06-04) ★★

Two bodies of work landed (commit this session); the third (terminal-yield) is
**investigated + characterized but NOT implemented** — detailed plan below.

### A. PRE-SOLVE GEOMETRY REFACTOR — COMPLETE (docs/presolve_geometry_refactor.md)
All airside node-unification now runs BEFORE `per_surface_solve`; post-solve is
altitude-only + new non-airside shapes. `O4_GEOM_GUARD=1` HECA build reports
**0 post-solve airside geometry changes — invariant HOLDS**. New `geom_guard.py`
(env-gated dev guard; rotation/reflection-invariant ring hash — the solver
reorders rect rings to [high,low,low,high] at altitude assignment, which is NOT a
geometry change). Moved pre-solve: shape drops, airside boundary clip (against a
geometric ribbon footprint `boundary._compute_boundary_ribbon_interior` /
`_ribbon_segment_geometry`), groundside emit + apron-island absorb + orphan-junction
reclassify, and the new `pipeline._unify_airside_geometry` (discovered-connect +
weld + FULL conformance + corner snaps). Post-solve conformance is now ONE-SIDED
(`owner_roles=_POSTSOLVE_FEATURE_OWNER_ROLES`) so features conform TO frozen airside.
**Surprise: Phase 4 (apron-island absorb pre-solve) was the cliff fix, not Phase 7**
— the #291↔#371 4.4 m cliff was an apron ISLAND emitted flat post-solve.
Ribbon + DEM-bridge STAY post-solve (their runway-distance clamp anchors to ALL
airside pavement incl. solved aprons → placement is solve-dependent). compare_target
re-cut (all 3 fixtures) + re-enabled.

### B. RUNTIME↔TEST GRADE ALIGNMENT + NO LENGTH CAP (#1, #2)
- The Ortho4XP-window WARN used all-pair Euclidean → **3569** phantom HECA
  violations; the test validator uses the visibility geodesic → **8** real. Aligned
  `elevation._report_within_shape_violations` to the visibility graph; moved
  `GRADE_VISIBILITY_BUFFER_M` / `ELEV_ROUNDING_NOISE_M` to config.py as the single
  source of truth (check_grade + runtime audit both import).
- **Removed the 60 m `WITHIN_SHAPE_MAX_PAIR_DIST_M` cap** from check_grade + the
  runtime audit (per user: visible pairs at ANY distance must comply; the solver's
  `_visible_grade_edges` was already uncapped). Visibility now gates apron AND
  junction. Surfaced real far-pair violations the cap hid (HECA T8 ramp; SPLP
  runway-parallel taxiway ~1.8%).
- **Added HECA to the grade-test gate** (`test_pavement_grade`, scoped to the grade
  test only). HECA + SPLP grade now FAIL by design (real violations); SPJC/CYXY pass.

### Suite state at session close
**295 passed / 2 failed / 2 skipped.** The 2 failures are INTENTIONAL grade gates:
`test_pavement_grade[HECA]` (T8 apron ramp + others) and `test_pavement_grade[SPLP]`
(runway-parallel taxiway ~1.8%). Per the ★★ user principle (below) both are solver
gaps to close, NOT terrain limits. The 2 skips are the pre-existing environmental
ones (test_elevation_terrain_following needs O4_TEST_TILE; test_boundary CYXY).

## ★★ USER PRINCIPLE (durable) ★★
**There is NEVER a legitimately infeasible airport.** We set the elevations, so every
airport is solvable; the elevation solver must have the tools + fallbacks to grade
ANY airport. Never accept a grade violation as "terrain-dictated / infeasible" —
it is a solver GAP. (Supersedes the "genuinely-infeasible mega-apron" framing in
older notes.) The solver model is 3 steps; NOTHING is "held/hard" except true anchors
(tile-seam vertices + runway CIFP thresholds):
  1. **Forward** (terminals→runway): grade connected shapes toward the runway; every
     piece compliant except runway-connected junctions (violations pushed there).
  2. **Reverse** (runway→terminals): pull slack out of shapes that didn't reach max
     grade; push remaining violations INTO the terminals. Aprons graded compliant
     → terminals MOVE to whatever elevation the apron needs.
  3. **Final smoothing**: balance connected leaves at the same hierarchy level.

## ★★ NEXT SESSION — TERMINAL-YIELD coordinated solver fix (DETAILED PLAN) ★★

**Problem (HECA, fully diagnosed):** terminal8 solves to 78.2 (its DEM ~79.3, on a
terrain rise); the 6/7/10 group fuses to 73.2. The apron bridging them ramps
78.2→73.2 over ~173 m visible = ~2.9% (and a local 78.2→75.6 over 8.8 m = 29.6%).
The solver allows it because **terminals are pinned at DEM** and never become the
free leaves step 2 pushes violations into.

**ROOT CAUSE — terminals are erroneously held at THREE independent points** (each
single-point fix is undone by the other two — verified empirically, all reverted):

| # | Location (unified_jacobi.py) | What it does wrong |
|---|---|---|
| 1 | Reverse pass, apron grading (~L887): `held \|= terminal_nodes.intersection(...)` | apron conforms to the DEM-level terminal instead of pushing the violation INTO it |
| 2 | `_rigid_shift_terminal` (~L829): anchors at `t0=DEM`; `if j in terminal_nodes: continue` skips cross-terminal apron edges; recomputes from local (high-terrain) neighbours | re-flattens the terminal back to ~DEM, OVERWRITING any apron grading |
| 3 | Band-projection (~L914): `_held_extra = terminal_nodes` (gated by `_FREE_TERMINALS`, default off) | step-3 smoothing can't balance terminal leaves |

**Failed single-point attempts (all reverted — solver is byte-clean baseline):**
- Remove #1 only → #2 (`_rigid_shift_terminal`) overwrites T8 back to 78.2; net regressed 8→12.
- Un-skip cross-terminal edges in #2, using `cap_adj` (ALL-PAIR Euclidean) → T8 sees
  far HIGH terminals (terminal1/2/9 @≈102, ~1.2 km, phantom chords) → band infeasible
  (lo 83.8 > hi 72.6) → midpoint, no move.
- Build `cap_adj` from VISIBLE (geodesic) edges instead → T8 drops 78.2→76.8 BUT net
  8→13, worst 29.6%→31.9% (terminal moved partway; its surrounding apron stayed → new cliff).
- Free terminals in band-projection (#3) + couple terminal pads → 168 violations
  (uncontrolled global sloshing, terminals drift +9 m above DEM; no minimal-shift).

**THE COORDINATED FIX (do all of these TOGETHER, not piecemeal):**
1. **Reverse pass (#1):** do NOT hold terminal nodes when grading an apron — let the
   apron grade freely from the runway so the violation flows to the terminal leaf.
2. **`_rigid_shift_terminal` (#2):** set the terminal to the level its GRADED apron
   boundary needs — (a) honour cross-terminal apron edges, but via the **VISIBILITY
   (geodesic)** graph, NOT all-pair Euclidean `cap_adj` (build a visible adjacency from
   `shape_constraints` edges); (b) skip only SAME-group internal edges (`if j in g`);
   (c) keep the DEM-anchor only as a tie-break (minimal shift) when the band is loose.
3. **Band-projection (#3):** free terminals, but as **rigid flat COUPLED units**
   (extend `_build_level_coupling` with `terminal_groups`) AND with a **DEM minimal-shift
   bias** (pull each freed terminal group toward its DEM-centroid within its feasible
   band each sweep) so they settle at the closest-to-terrain level the aprons permit —
   NO global sloshing.
4. T8 sits on a rise ~5 m above the 73 m network, so its flat level must BALANCE both
   abutting aprons' lengths (high side needs ~267 m to shed 4 m at 1.5%; low side
   ~133 m to shed 2 m). The band-projection (global feasible bands from hard anchors)
   is the right place to make this global decision — once terminals are free+coupled+
   minimal-shift everywhere.

**Code touchpoints:** `_rigid_shift_terminal` (L829), reverse-pass else-branch (L885-889),
`_build_level_coupling` (L1427, add `extra_groups`), cap_adj construction (L816-823),
band-projection call (L909-919). Prototype probes: `/tmp/probe_heca_terminals.py`,
`/tmp/probe_held.py`, `/tmp/probe_solver_edge.py`. Memory: `runtime_vs_test_grade_gap.md`
(full attempt log).

**Validation gates:** HECA `test_pavement_grade` must go GREEN (T8 ramp resolved),
SPJC/SPLP/CYXY terminal handling must NOT regress (the terminal tests + compare_target),
and `O4_GEOM_GUARD=1` must stay 0. Build HECA + `tools/check_grade.py`; confirm T8 pulls
to a level where both abutting aprons grade.

---

# Auto-Patch Status — session 60 CLOSE → session 61 = APRON COHERENT-FILL + 3 QUEUED TASKS

## ★★ SESSION 60 RESULTS (2026-06-02) — apron-edge coupling, robust neck cut, runway held in grade ★★

Suite stayed **289 passed / 2 skipped / 0** throughout. All commits are GENERAL
builder/solver fixes (HECA is the manual target; not in the baseline suite).

### What landed (commits, dev — newest first)
1. **Runway must not be pulled out of grade compliance** (9c87a64). The final
   per-surface runway-flex (`_relax_runway_and_resolve`, unified_jacobi) accepted
   a move purely on reducing the *pavement* violation count, so at HECA it pulled
   05C/23C ~11 m BELOW its own terrain (chasing lower valley aprons), driving the
   first/last quarter past the 0.8% end-grade. Now gate acceptance on the RUNWAY
   staying compliant (`rwy_c == 0`). 05C/23C now holds its terrain profile
   (~111–117 m, ends ≤0.76%, mid ≤0.96%).
2. **Runway-flex applies the 0.8% end-grade cap** (c77b92a). `_build_runway_
   constraints` computed a per-ref runway axis and uses `RUNWAY_END_GRADE` for
   axial edges in the first/last `RUNWAY_END_FRACTION` (was a flat 1.5%, so a
   moved chain silently went steep at the ends).
3. **Conform apron/junction T-junctions BEFORE the solver** (e6a3826). The solver
   couples shapes only via SHARED canonical nodes; T-junction conformance ran
   AFTER the solve, so abutting aprons that met at a vertex-on-edge-interior
   shared no node and graded independently → steps. New `enforce_conformance(
   owner_roles={apron,junction})` runs pre-solve (rects untouched → keep their
   4-corner planar form). **HECA cross 11→0, steps 41→5.**
4. **Robust gap-free neck cut** (a3ce6b7). `neck_cuts` found HECA's two obvious
   13.5 m apron necks but `shapely.ops.split` silently returned 1 face (chord
   stays inside past a non-square wall). New `_cut_at_mouth` ring-splits the two
   boundary arcs (shared chord, no gap); split/buffer fallbacks. Giant apron now
   splits at the user-identified necks.
5. **Drop degenerate (sub-quad) clipped taxi rects** (1a34fc7). A taxi rect whose
   clip collapses to a triangle (HECA J1, an apron-embedded ramp) is dropped →
   apron residue. **steps 69→41, cross 11→8.**

### ★ KEY FINDING — the runway is NOT in a valley; the aprons are in lower terrain
The smoothed DEM (what the build uses) at 05C/23C's T4-join is **~111–113 m** (the
runway is mild, ~5 m dip between the 116/114 thresholds). The earlier "16.7 m
valley dip to 99.7" was the SOLVER wrongly pulling the runway 11 m below its
terrain to chase the aprons — now blocked. The connecting aprons/taxiways sit at
their own LOWER terrain (cross_connector U 102.6, J/S/T/W apron 103.1, giant
A/G/J apron #289 spans **75 m at the south terminals → ~111 m at the runway, a
36 m range**). So the runway is correct; the aprons must **fill UP** to meet it.

HECA now: overlap 0 / source 0 / cross 0 / steps 5 / **within 89** /
vertex_on_flat_edge 1 / short_edge 2. The within JUMPED 20→89 because the
runway-compliance gate stopped the runway masking the apron-fill gap (honest
state: runway compliant, aprons not yet filled).

## ★★ OPEN ITEMS FOR SESSION 61 ★★

### A. Elevation-solver enhancement — APRON/TAXIWAY COHERENT-FILL (the dominant one)
The 89 within-violations are aprons/taxiways/connectors that follow the DEM DOWN
instead of FILLING UP to meet the now-compliant runway (and each other). Root =
the band-solve (`_project_within_bands`, cyclic 50/50 cap-projection) does NOT
converge on a large COUPLED all-pair region (HECA's merged apron + the runway
chain): 4.4 m residual at 15000 sweeps. **Ruled out this session:** clean ≤200 m
half-plane subdivision (within 20→41, broke CYXY/SPJC — reverted); node-wise
clamp projection (within→30, risky — reverted); per-node / movable-rigid
terminals (regressed — reverted). **The terminal coupling DID work** (terminals
rigid-shift −5.9…+6.6 m as coupled clusters; they are NOT the blocker). Next:
a CONVERGENT difference-constraint solve for the coupled region so a free apron
FILLS toward its held runway/terminal edge (DEM = preference only), + the giant
apron (#289, 36 m span) genuinely decomposed so all-pair is feasible. Also add a
**runway-axis grade CHECK** (0.8% ends / 1.5% mid, per-segment) to check_grade +
suite — currently the 60 m pair-gate skips runway diagonals so runway grade is
never validated (and HECA isn't in the baseline set).

### B. Task — coordinate standardization to (lat, lon) everywhere
Survey done this session (mostly consistent already). Only real offenders:
`layout._projection.to_m(lon, lat)` and the four `bridges._to_m(lon, lat)` take
(lon, lat); `dsf_reader` RETURNS (lon, lat). ~15–25 call sites (osm_load,
pipeline, terminals, bridges). Mechanical, suite-guarded. Rule to adopt: meters
never cross a module boundary — only lat/lon does, always (lat, lon). (I hit the
(lon,lat) trap twice this session.)

### C. Task — neck-split / apron-on-rect-FLAT-EDGE root fix
HECA apron (#303/#308) is split with a shared vertex landing mid-W2's FLAT (end)
edge (t=0.32, ~20 m from a corner) — junctions/aprons must meet rects only at
corners. Post-hoc corner-snapping FAILS (snapping the 20 m pokes the apron into
taxiway T 6.4 m² + float-noise into terminals; can't re-clip post-emit without
desyncing node_altitudes). Tried + reverted. Real fix = at the SOURCE: the neck/
residue cut must not place a vertex on a rect flat edge (cut only at corners /
free boundary). `_snap_near_corner_vertices_to_rect_corners` + the new
`_rect_flat_edge_indices` helper were the experiment (reverted).

### D. Task — curved extrema transition for rect terrain-slicing
Instead of a single slice at a terrain extremum (sharp transition), insert a 20 m
rect CENTERED on the slice and use the high-point / low-point vertical-curve
profile for a smooth curved transition. (`split_long_rects_along_terrain`.) Not
started.

### Probes (this session, /tmp/probes/): heca_rwy_profile, heca_diag2 (terminal
shifts + within breakdown), heca_w2/heca_w2b (flat-edge), heca_bump_precise,
compare_builds (standalone == production, +2 shapeID offset). Anchor
(30.10895832, 31.43477812). Build ≈55 s; cross-check production patch at
`Patches/+30+030/+30+031/HECA_auto.patch.osm` (shapeID = standalone + 2).

---

## ★★ SESSION 59 RESULTS (2026-06-01) — overlap+source CLEARED, cross/steps slashed ★★

HECA issue list, start → end of session 59 (suite stayed **289/0/2** throughout;
the 5 commits below are GENERAL builder/solver fixes, NOT HECA-tuned):

| check                | s58 | s59 | how |
|----------------------|----:|----:|-----|
| overlap              |   4 |   0 | apron-aware overlap-clip + groundside deconflict |
| off-source           |   2 |   0 | drop off-source residue |
| vertex_on_flat_edge  |   1 |   1 | (open) |
| short_edge           |   2 |   2 | (open — likely real apt.dat gaps) |
| cross-shape          |  37 |  11 | terminal coupling + airside↔groundside skip |
| within-shape         |  15 |  14 | (terminal coupling; rest = dense-cluster grade) |
| edge-steps           | 229 |  69 | terminal coupling |

### What landed (commits, dev)
1. **Couple abutting terminals into ONE rigid flat unit** (unified_jacobi.py).
   The reverse-pass relief shifted each terminal to its OWN DEM centroid and
   IGNORED inter-terminal constraints, so HECA's south complex (terminals
   6/7/10) and north (2/9) — which SHARE EDGES — left 1.4–3.8 m cliffs at those
   shared edges. Union-find the flat shapes by shared canonical node → an
   edge-connected cluster is one flat group at one AREA-WEIGHTED level (dominant
   pad anchors). Singletons = bit-identical to old behaviour (baseline
   unchanged). **cross 37→18, steps 229→69.** This was the deferred "coherent
   terminal fill" — the clean half of it; no backfire (within stayed 15).
2. **Drop off-source residue** + **airside↔groundside skip in cross-shape**:
   `_drop_off_source_residue` (junction_repair, called in pipeline after the
   floating-orphan drop) drops small (<2000 m²) apron/junction <50 % on
   `source_pavement_union` — HECA #258/#228 were thin strips beside
   shoulder-widened runways. check_grade cross-shape now applies the same
   airside↔groundside skip the STEP checks use (was an oversight; groundside has
   a 4 % cap not None). **source 2→0, cross 18→11, within 15→14.**
3. **Apron-aware overlap-clip** (`_drop_overlap_against_fixed_shapes(...,
   include_aprons=True)` called in pipeline.py right BEFORE the final solve).
   The mid-finalize overlap-clip runs BEFORE reclassify-to-apron + neck-split,
   and aprons weren't in its priority list → the dense S/T/W/J/R cluster left
   apron∩junction (957/144 m²) + apron∩apron (150 m²) overlaps. New
   `include_aprons` mode adds apron to the junction residue tier (off by default
   → early call sites unchanged); final solver re-derives node_altitudes on the
   clipped pieces. **overlap 4→1.**
4. **Groundside deconflict** (groundside.py): the groundside emit clipped each
   poly vs terminals/airside but not vs OTHER groundside → a 0.1 m² self-overlap.
   Clip each vs the union of already-accepted (largest first). **overlap 1→0.**

### ★ THE REMAINING HECA WORK (session 60) = UNDER-DECOMPOSED GIANT APRONS
The leftover cross(11)/within(14)/steps(69) are **two dense apron regions**, NOT
a solver-coupling gap:
- **Giant apron #287 (A/G/J meet) = 1.4 M m², alt 62.0–87.6 m** with **stub J1
  [#90]** (978 m², 156 m thin node_altitudes stub) embedded along/through it →
  **~41 of the 69 steps** + several cross are J1↔#287 (up to 3.4 m), at
  ~(30.129–132, 31.406–407). J1 shares its near edge w/ #287 (verts coincide,
  ~68–69 m) but its FAR edge runs alongside the apron at a different graded level.
- **Dense S/T/W/J cluster**: apron #294 (J,S,T,W) + junction #373 (S,T) hold most
  of the within violations (2.0–13.5 %). apron #316 (B,C,E,S) 3.0 %.
- **stub C #149**: a malformed 7-vertex node_altitudes ring with tiny 0.97/2.25/
  4.36 m hook edges → 20.1 %/1.0 m + 8.1 %/2.5 m within (degenerate sliver, not a
  real slope). A conformance/absorption hook-cleanup candidate.
The fix is the long-deferred **giant-apron decomposition / coherent fill**
(STATUS s55 #2): the 1.4 M m² apron must be split into convex pads (neck-split
left it whole) so stubs like J1 meet a small apron piece at one level, and the
all-pair grade is over a smaller span. `pavement/apron_necks.py` is the module.
Regression-guard: SPJC/SPLP/CYXY grade tests + the HECA verify above.

### Still open (low priority, likely SOURCE not builder)
- **short_edge (2)**: stub Exit-2 [#94] / Exit-3 [#116] end_A ends ~42 m short of
  runway 05R/23L (end_B connects to a junction at 0.0 m). Genuine apt.dat
  network gap at the runway exits OR a missing runway-exit connector.
- **vertex_on_flat_edge (1)**: an apron vertex on stub C [#103]'s flat cross-edge
  at t=0.675 (mid-edge, d=1.0 m) — not near-corner, so the existing
  `_snap_near_corner_vertices_to_rect_corners` can't catch it.

### How to measure (unchanged)
```
venv/bin/python -c "import sys; sys.path.insert(0,'src'); sys.path.insert(0,'.'); sys.path.insert(0,'tests')
from conftest import cached_airport_layout
from auto_patch.verification import verify_and_log
verify_and_log(cached_airport_layout('HECA'),'HECA')" 2>&1 | grep '\[verify\]'
```
Probes this session: `/tmp/probes/heca_*.py` (cluster, offsource, overlap,
grade_detail, steps2, j1, stubc). Anchor (30.10895832, 31.43477812).

---

# Auto-Patch Status — session 58 CLOSE → session 59 = DEBUG REMAINING HECA ISSUES

## ★★ SESSION 59 TASK: fix the remaining HECA patch issues ★★

The build now SELF-VERIFIES and lists HECA's exact problems with shapeIDs +
taxiway names + lat/lon. Your job: work that list down to zero. Run:

```
venv/bin/python -c "
import sys; sys.path.insert(0,'src'); sys.path.insert(0,'.'); sys.path.insert(0,'tests')
from conftest import cached_airport_layout
from auto_patch.verification import verify_and_log
verify_and_log(cached_airport_layout('HECA'),'HECA')" 2>&1 | grep '\[verify\]'
```
(~55s build, cached after the first call.) Or the pytest gate on HECA:
`O4_TEST_AIRPORTS=HECA venv/bin/python -m pytest tests/test_pavement_geometry.py
tests/test_pavement_grade.py -o addopts="" -q`. HECA grade is now COVERED (no
longer a gap). The shipped patch dump is `/tmp/HECA_grade.osm` (probe
`/tmp/probes/heca_grade.py`). Anchor (30.10895832, 31.43477812). shapeID == the
`shapeID` tag in the patch OSM == index in `layout.shapes`.

### THE CURRENT HECA ISSUE LIST (2026-05-31; total: overlap 4 / off-source 2 /
### flat-edge 1 / short-edge 2 / cross-shape 37 / within-shape 15 / edge-steps 229)

**★ DOMINANT ROOT CAUSE = the terminal-cluster coherent-fill (deferred HECA #2
solver redesign — see session-55 catalogue below + memory).** terminal7 [#6],
terminal10 [#9], terminal6 [#5], terminal2 are flat pads at DIFFERENT levels
sharing corners (3.4–3.8 m apart) → drives most of the 37 cross-shape + 229
edge-steps. The fix is the coherent terminal+apron fill in
`elevation_per_surface/unified_jacobi`: terminal groups must lift as MOVABLE
COUPLED units within their grade band (NOT shape-by-shape — that backfired,
169 viol). Regression-guard: SPJC/SPLP/CYXY grade tests (all green at zero).

1. **OVERLAP (4)** — apron/junction footprint overlaps near the S/T/R/W taxiway
   cluster: 957 m² apron[#309]∩junction[#310] @30.10571,31.40253; 150 m²
   apron[#298]∩apron[#302]; 144 m² apron[#298]∩junction[#300]; 0.1 m²
   groundside∩groundside. Likely junction/apron decomposition over the wide
   blob (see s56 neck-split / apron-reclassify). Pre-existing (the s55 #5
   self-overlap, now LOCATED).
2. **OFF-SOURCE (2)** — apron[#258] 487 m² (8% on source) @30.10723,31.43105;
   apron[#228] 204 m² (8% on source). Aprons emitted where apt.dat/DSF has
   almost no pavement → spurious synthesis OR a non-pavement polygon tagged as
   pavement. NEW finding (the per-shape source-adjacency check located these;
   the old coverage ratio never could). Investigate the apt.dat/DSF near there.
3. **FLAT-EDGE (1)** — apron vertex on stub C[#103] flat (cross) edge
   (t=0.675, d=1.00 m) @30.10482,31.39360. Builder issue (a junction/apron
   vertex on a rect cross-edge interior).
4. **SHORT-EDGE (2)** — stub Exit-2[#94] + Exit-3[#116] end_A connect to nothing
   (rects end mid-air) @ ~30.108,31.433 / 30.110,31.435. Likely a gap in the
   apt.dat taxi network at the runway exits, OR a missing connecting junction.
5. **WITHIN-SHAPE (15)** — steep aprons/stub: apron[#308] 17.5%/3.4 m (tiny neck);
   apron[#296] 13.5%/8.2 m (where J,S,T,W meet); stub S[#126] 6.5%/50 m. Apron
   decomposition (narrow necks) + the coherent-fill issue.
6. **EDGE-STEPS (229)** + **CROSS-SHAPE (37)** — mostly the terminal cluster
   (root cause above).

### Session-58 build-VERIFICATION + diagnostics architecture (use it)
- `auto_patch/verification.py` = the SINGLE home for every invariant check;
  `verify_and_log(layout, icao)` runs them and logs WHAT/WHERE(shapeID+taxiways+
  lat,lon)/CAUSE/FIX. Called per in-tile airport by `driver.generate_auto_patches`
  (production) AND by the pytest gate (tests delegate to the same functions — no
  duplication). Grade reuses `tools/check_grade.py` (the engine).
- Checks shared: grade (cross/within/steps), self-overlap, source-adjacency,
  terminal-flat, vertex-on-sloping-edge, vertex-on-flat-edge, axis-tilt,
  short-edge. NOT yet extracted (pytest-only): junction rules
  (`test_junction_rules.py` / `test_junction_invariants.py`).
- `config.LOG_VERBOSITY` = 0 (Ortho4XP window quiet unless a patch has issues;
  set 2 for debug). ALL per-airport test caps/baselines REMOVED → universal zero
  (they were stale; baselines clean). Commits this session: 024f50b dedup,
  cbcf344 RESA-from-source, c42a340/136bc1b/dcd872c/44bbeea/6c18da6/bcbb9c4/
  7b03bb3/f8e8c90/b965775 verification. Plus 3509845 compare_target re-cut.

### Suite state
Default suite **289 passed / 2 skipped / 0 failed** (SPJC/SPLP/CYXY, compare_target
incl). HECA is NOT in the default suite; its issues above only show via
`O4_TEST_AIRPORTS=HECA` or the build verification. Earlier session-58 work: taxi-rect
dedup keeps R/B sections (commit 024f50b, [[taxi_rect_dedup_sections]]); RESA anchored
on apt.dat geometry (cbcf344, [[heca_resa_nondeterminism]]). Architecture detail:
memory [[build_verification_architecture]].

---

# Auto-Patch Status — session 57 HANDOVER (★ NEW DIRECTION: line-marking centerlines → drop curves → rects)

## ★★ SESSION 57 HANDOVER → NEXT AGENT: build the line-marking centerline extractor ★★

### THE ALGORITHM TO IMPLEMENT (user's formula, 2026-05-31 — authoritative)
Replace the *synthesis* of curves (the `r/tan(α/2)` fillet approach — abandoned,
see below) with **reading the real curves out of the apt.dat line markings**:

1. **Taxi route network → intersections.** A route node with ≥2 distinct taxi
   names OR degree ≥3 is an INTERSECTION; that point is ALWAYS inside a junction.
2. **Line markings → the real curved centerlines.** apt.dat **row-120** linear
   features, type **code 60** ("Single Taxi Wide" = taxiway centerline), carry
   the actual geometry INCLUDING beziers (the curves). Filter: code 60 (HECA);
   cross-airport, confirm centerline by ALIGNMENT to the route net (median dist
   <~6m + parallel). Edges = "Double Solid" (code 53, offset ~21m); holds/broken
   are codes 52/61/62 — all separable.
3. **Identify the junction's curves.** Trace the route OUT from each intersection
   point; the junction is bounded by the **nodes where the bezier curves begin**
   (where a centerline marking leaves the straight route). JUNCTIONS ARE
   DIFFERENT SIZES — do NOT use a fixed radius; the curve-start nodes define the
   extent.
4. **Drop the straight chords BETWEEN curve ends** (the straight bits *inside* the
   junction — these wrongly survive an alignment-only filter; see #3 example).
5. **Drop ALL the bezier curves** (the curved marking segments — a segment is a
   curve iff an endpoint is a 112/114/116 bezier node; this is the reliable,
   heuristic-free curve test the user landed on: "can't you just drop all the
   bezier curves?").
6. **Remainder = the straight centerlines → feed the rect builder.**

### What's built (probes in /tmp/probes/, NOT committed — exploratory)
- `linemark_extract.py` — parses row-120 features (samples beziers via the
  X-Plane mirror convention), filters to code-60 centerlines, route-cross-ref
  align+parallel, drops curves, writes `/tmp/HECA_linemark_straights.osm`.
  STATE: code-60 filter is clean (938 centerlines vs 106 edge/hold/broken).
  Curve-dropping via per-segment alignment got #1 (keep stub middle ✓) and #2
  (drop curve ✓) right but #3 WRONG (a straight chord that is on+parallel to a
  junction-internal route edge survives — needs step-4: drop straights between
  curve ends). **The clean fix per the user = step 5 (drop bezier segments
  directly) + step 4 (drop straight chords between curve ends near an
  intersection), NOT the alignment/deflection heuristics.**
- HECA apt.dat: `/Users/noah/X-Plane 12/Custom Scenery/HECA Cairo/Earth nav
  data/apt.dat`. Row counts: 1044 row-120 features, beziers present (889×112,
  272×116). The reader does NOT parse row-120 line markings yet — add it.
- `/tmp/HECA_target_centerlines.osm` = 125 hand-tuned target spines (validation).
- Example coords (lat,lon): #1 keep stub-middle 30.1199249,31.4477182 →
  30.1197105,31.4479466 ; #2 curve-to-drop 30.1201791,31.4467661 ; #3
  junction-internal straight to drop 30.1190203,31.3825056 → 30.1194462,31.3830776.

### REJECTED this session — the curve SYNTHESIS approach (don't revive)
Spent a long arc trying to SYNTHESISE curves from the straight route network
(`/tmp/probes/curve_mask.py`, `junction_bounds.py`): clip each edge back by the
fillet tangent `t=r/tan(wedge/2)`, r=50 (Code E, ICAO Doc 9157 curve radius),
high-speed exits R_HS≈370. It half-worked (T2 calibrated to the target) but was
fundamentally fragile: acute-gore vs real-turn ambiguity, runway-exit transitions
under/over-masked, multi-vertex curve `arc[j]` bugs, and NO single local signal
(width / through-angle / convergence) separates off-target from real taxiways
because HECA pavement is a merged blob and apt.dat over-labels. The line-marking
data has the REAL curves — use them instead.

### Committed this session (suite GREEN 285/0/2 except compare_target — see below)
- `d46034e` cap cross-connector end margin 50m (HECA L coverage 71→99.7%).
- `02677a3` keep through-taxiways whole in the corridor trim (R 70→99%).
- `c51fa12` off-corridor drop (`_drop_offcorridor_centerlines`: runway-crossing
  >5m + junction-buried median-nearer-edge-halfwidth ≥50m) + bend-hook trim
  (`_trim_short_bend_hooks`) + runway-centering margins (perp 30→25, diag 15→25,
  junction 15→20; tunable module globals `_RWY_JUNCTION_BUFFER_M` etc. in
  pipeline.py, `_CHART_JUNCTION_MARGIN_M`/`_BEND_ENDPOINT_MARGIN_M` in
  centerlines.py).
- `93eab1d` bend trim: split at the corner into two straights, never keep a bent
  piece (drop short hook <45%, else keep both straights).
- HECA named-only centerline coverage 95.5 → 98.4%. These are CENTERLINE-quality
  improvements to the EXISTING route-network path; the line-marking approach
  above will likely SUPERSEDE much of the off-corridor/bend-hook logic once it
  lands — keep them until the new extractor proves out.

### ⚠️ STILL OPEN / GOTCHAS
- **compare_target fixtures (SPJC + SPLP×2) need RE-CUTTING** — the centerline
  geometry shifted this session; they're the only suite failures. User approved
  the re-cut (dropping short rects like SPJC R1/R2 is OK). Use `/tmp/recut.py` /
  `tools/build_target_osm.py`, refresh floors = target−round(5%).
- **CONCURRENT AGENT** owns the uncommitted `unified_jacobi.py` WIP (HECA #2
  grade) + committed `803761b` (runway geometry). LEAVE unified_jacobi.py ALONE;
  `git add` explicit paths only.
- Named-only is the agreed HECA metric (exclude TX/discovered + refless both
  sides). Probe anchor (30.10895832, 31.43477812). HECA build ≈55s.

---

# Auto-Patch Status — session 56 HANDOVER (★ default suite GREEN: 281 passed / 0 failed / 2 skipped, compare_target INCLUDED)

## ★★ SESSION 56 HANDOVER → NEXT AGENT: collinear-fragment MERGE (the remaining rect-quality lever) ★★

### Where we are
Session 56 was a taxi-rect QUALITY pass driven by a hand-verified target.
Guiding model (user): **a taxi rect = its centerline segment, widened** — so
align the centerline segmentation+extent and the rects follow. Junctions only
at real curves/intersections. Rules must be GENERAL (geometric, all airports),
not HECA-tuned.

Committed this session (all suite-green, no regressions):
- `e9889a4` source_axis-aware sloping-edge split (stop two-parallel-lane rects)
  + `56fc95e` no-overlap guards on junction-vertex-moving passes (replaced a
  bad flat-end trim that broke junctions). [memory/two_parallel_rects_rotated_ring.md]
- `f205856` **width-aware endpoint trim** (`_trim_axis_to_narrow_corridor` in
  pavement/rects.py): trims a rect's axis back where pavement widens past
  1.3×strip half-width → rect ends at the junction mouth. CAP GOTCHA: the
  half-width probe caps at 40m and saturates at wide airports; the trim
  re-measures the strip with a HIGH cap (120m).
- `077b17a` apron-blob rejection: drop a rect whose mean width > 1.7×(2*narrow_hw)
  AND > 50m (corner-snap inflated it into apron — HECA U1 570×162).
- `674d5e8` corridor trim keeps through-taxi crossings (was discarding R's
  670m middle section).
Cumulative HECA vs target: rect IoU 0.704→0.749, matched 98→108/125, ~half
the over-length gap closed. FULL DETAIL: **memory/rect_centerline_quality.md**.

### THE NEXT PHASE (your task): collinear-fragment MERGE, replacing the stub-dedup
**Goal:** close the remaining gap. Current centerline-level score (rect axes vs
target): **153 axes / 29,730 m vs target 125 / 25,375 m** — over-segmented
(+28) and ~17% too long overall (T/S/G over-fragmented), while R/B are *under*-
built. One coherent lever fixes BOTH directions.

**Diagnosis (verified, do not re-litigate):**
- apt.dat is FINE — R = 1918 m (7 edges), B = 883 m (5 edges), full length.
  `apt_dat_reader.taxi_centerlines` preserves them.
- The builder `_build_taxi_rects` EMITS all of R's segments (instrumented: 5
  "R EMITTED" stubs). They are then COLLAPSED to 1 by the **stub-ref dedup**
  post-pass: `src/auto_patch/pavement/rects.py` ~lines 351-457
  (`_should_dedup` + the overlap/proximity cluster dedup + the "diagonal-parent
  → exactly one rect" rule). That logic keeps only the LONGEST fragment per
  ref, assuming letter-only stubs are single short stubs (correct for SPJC
  B/C/E/G ≤580 m; WRONG for a long 2-section taxiway like R).
- R's true shape (user): TWO straight sections + a bend. Its fragments are
  section-1 (db≈30° to runway: 447+194 m) and section-2 (db≈89°: 271+129+94 m).
  Correct output ≈ 2 rects (one per section), target has 3.

**The fix to build:** replace the keep-longest stub-dedup with a **MERGE of
adjacent COLLINEAR same-ref rect axes** into one rect:
- MERGE when fragments are same-ref + roughly collinear (bearing within ~15°)
  + adjacent/end-to-end (touching, ~0 area overlap). → R section-1's 447+194
  merge to ~641 m; section-2's 271+129+94 merge to ~494 m → R = 2 rects.
- Do NOT merge across a real bend (different bearing → R's 2 sections stay
  separate).
- STILL drop genuine DUPLICATES (same footprint, real area overlap — SPJC's
  fragmented OSM ways / V2's 3 pieces). Keep that behaviour; only stop
  collapsing distinct collinear-adjacent SECTIONS.
- This same merge also fixes the over-fragmentation (T 14→11, S 7→4, G 9→7).

### How to measure (scoring harness — reuse it, don't rebuild it)
- `/tmp/HECA_target_centerlines.osm` — 125 user-verified target spines (one per
  intended rect). `/tmp/HECA_initial_rects_target.osm` — the 125 target rects.
- `venv/bin/python /tmp/probes/clscore.py` — builds HECA, captures rect AXES
  (entry[1] of `_build_taxi_rects` output), reports per-ref count+length vs
  target + TOTAL. PRIMARY metric (1-D, robust).
- `/tmp/probes/score.py` — rect IoU vs target (NOISY: target hand-drawn ~approx
  coords, ~0.7 even when right; secondary trend only).
- Anchor for /tmp probes: HECA = (30.10895832, 31.43477812).
- Score per-ref after EACH change; converge cur→target.

### Risks / guardrails
- **Do NOT regress SPJC's short stubs** (B/C/E/G, V2) — they rely on the dedup.
  Run the full suite + `O4_TEST_AIRPORTS=SPJC,SPLP,CYXY` checks at each step.
- Broad geometry shift → **re-cut compare_target fixtures + refresh floors**
  when done. Workflow: `venv/bin/python /tmp/recut.py` (re-cuts SPJC + both SPLP
  tiles, prints floors = count−round(5%)); update `tests/test_compare_target.py`
  baselines + totals. (Done twice this session — see git log.)
- The `_rect_long_edges_at_pavement_boundary` both-embedded gate (rects.py ~479)
  currently LIMITS over-fragmentation; the merge interacts with it — measure.
- Earlier REJECTED dead-end (don't repeat): tightening `split_merged_centerline`
  bend-split (more splitting) REGRESSED — target wants FEWER rects, not more.
  The lever is MERGE (post-build), not more centerline bend-splitting.

### ⚠️ Pre-existing WIP in the tree — LEAVE IT ALONE
`src/auto_patch/elevation_per_surface/unified_jacobi.py` has ~29 lines of
UNCOMMITTED WIP (a `_directional_relief` terminal grade-band "FILL terminals up
to their grade-feasible band" block) belonging to ANOTHER agent's HECA #2
grade work. Do NOT stage/commit it. **Avoid `git stash`** with it in the tree
(it was accidentally reverted+reconstructed once this session — see
memory/two_parallel_rects_rotated_ring.md incident note). Use
`git add <explicit paths>` only.

### Key file map
- `pavement/rects.py`: `_build_taxi_rects` (gates + the stub-dedup at ~351-457),
  `_trim_axis_to_narrow_corridor`, `_natural_half_width`, apron-blob gate.
- `pavement/centerlines.py`: `split_merged_centerline` (bend-split),
  `_split_centerlines_at_points` (intersection split + margins).
- `pipeline.py` ~1846: diagonal-stub corridor trim (just fixed). ~1930: split call.
- `apt_dat_reader.py:1209` `taxi_centerlines` (source — confirmed clean).

---

# Auto-Patch Status — session 55 HANDOVER (★ default suite GREEN: 281 passed / 0 failed / 2 skipped, compare_target INCLUDED)

## Session 55 CLOSE (2026-05-29)
Two threads ran: (A) build/test PERF + (B) HECA correctness. Net:
- **HECA invariant failures 9 → 2** (`O4_TEST_AIRPORTS=HECA`): fixed coverage,
  terminal flatness, 170 orphans, rect-short-edge TX52, retired Rule-2
  proximity, vertex-on-sloping-edge snap, neighbour-corner insert. The
  junction-connectivity cluster (#3) is fully closed.
- **Remaining HECA: #2 within-shape grade (~79, DIAGNOSED — needs a solver
  redesign, SPAWNED as a separate task) and #5 self-overlap (3 pairs).**
- **Perf:** build 70.7→54.6 s; suite builds 14→8; default suite ~104→~73 s.
- Default-suite count dropped 284→281 only because the retired Rule-2 test
  had 3 parametrizations (SPJC/SPLP/CYXY); nothing regressed.
- ⚠️ ANOTHER AGENT has uncommitted WIP in `junction_repair.py`
  (`_orient_rect_sloping_edge_first`). LEAVE IT ALONE.
- The #2 solver work is handed to a fresh session (see the "PLAN for the
  solver session" under HECA failure #2 below). Do NOT rush it into a
  mixed session — the naive terminal-lift backfired (169 viol); it needs a
  coherent-fill redesign with the grade tests as the regression guard.

## Session 55 — build/test PERFORMANCE pass (committed 6140f46, 6d8900d, fc53573, f6ad4fb) + shared build cache (bd728b5)
Profiled the per-airport build (the dominant suite cost; tests themselves
are ~10s/airport). HECA build 70.7s → 54.6s; full HECA suite 590s (pre-cache)
→ 285s → 204s; default suite ~104s → ~84s. All changes verified
output-identical (no behaviour change); suite stays 284/0/2.
- **Where the build time goes (HECA, real):** elevation solver (Jacobi)
  ~30s, clearance emit ~26s, terrain-transition ~7s, discover-taxiways ~4s,
  apt.dat select ~2.5s. DEM load already cached (`_DEM_CACHE`).
- **#1 clearance vectorize (6140f46):** `clearance._resample_alts_over_strips`
  was O(V·all-edges) 11.7M shapely ops → STRtree `dwithin` query for the few
  candidate edges + same projection. Proven identical: 4000 randomized A/B
  trials, 0 mismatch. 20.9s → 7.9s.
- **#2 apt index persist (6d8900d):** `apt_dat_reader` now pickles its
  header index to a temp file (`_APT_DAT_PERSIST_PATH`, keyed by
  path+mtime+size, self-healing/corruption-tolerant). First scan 2.67s →
  warm 23ms across processes/workers/sessions. Bump the `_v1` filename if
  the cached tuple's meaning changes.
- **#3 solver hoist (fc53573):** `_project_within_bands`/`_project_shape`
  precompute loop-invariant `held`/`members` once instead of per-edge-
  per-sweep. Bit-identical (HECA altitude sha unchanged). 59.7s → 54.6s.
- **#4 grade-reuse (f6ad4fb):** `test_pavement_grade` reuses the cached
  layout for SINGLE-TILE airports (per-tile build is bit-identical when no
  integer line crosses the footprint). Saves a redundant ~55s build per
  single-tile airport. Multi-tile (SPLP) still builds per tile.
- **#5 smoothed migration + SPLP dedup (049a7d3):** compare_target +
  tile_cut_parity built per-tile with a RAW `O4DEM(fill_nodata='to zero')`;
  grade + production use the SMOOTHED `_load_airport_dem`. Raw≠smoothed
  (SPLP tile-77 259 vs 250 shapes; primary_parallel 5→7) — so
  compare_target was gating NON-SHIPPED geometry (correctness gap, not just
  perf). Unified all per-tile builds on smoothed via
  `cached_airport_layout` (tile path raw→`_load_airport_dem`); grade +
  tile_cut now build through the cache; tile_cut tests + compare_target_splp
  pinned `xdist_group("SPLP")`. Re-cut SPLP_target_tile fixtures + floors
  (primary_parallel 5→7/4→7, secondary 3→4, totals 236→238/317→325).
  **SPLP builds 10→4, total suite builds 14→8, default suite ~104s→~73s.**
  Note: with only 3 baseline airports + loadgroup, serial `-n0` (~63s) is
  competitive with parallel (~73s); parallelism wins on larger airport sets.
- **Test infra (bd728b5):** one shared session layout cache in
  `conftest.cached_airport_layout` (lru, keyed icao+compute_elevations+tile)
  replaces the per-module/per-test rebuilds; `pytest.ini` adds
  `--dist loadgroup` + a collection hook tagging each airport-parametrised
  test `xdist_group=<icao>` so an airport builds once per run. NOTE: plain
  `--dist load` is SLOWER (178s vs 102s) — it duplicates the same build
  across workers; loadgroup is correct. Remaining full-suite bottleneck =
  **SPLP's genuinely-distinct per-tile builds** (grade/compare/tile_cut,
  different DEMs) serialized on one worker; only a cross-worker disk cache
  would parallelize them (deferred — high risk, uncertain gain).

## Session 55 — HECA issue catalogue + coverage fix (committed 0ffb8f6)
HECA is NOT in the automated baseline (`_BASELINE_AIRPORTS` = SPJC/SPLP/CYXY);
it's a manual build/X-Plane target. Built standalone (no crash, 2407 shapes,
all valid) and ran the invariant suite via `O4_TEST_AIRPORTS=HECA`:
Started at 9 failures; **2 remain**. The junction-connectivity cluster
(#3) is FULLY resolved: coverage #1; terminal #4; 170 orphans (DSF
boundary as source); Rule-2 proximity retired; rect_short_edges TX52
(pavement-tip exemption); vertex-on-sloping-edge (post-conformance
near-corner snap onto rect corners); neighbour_corners (post-conformance
insert of unshared neighbour corners into junction edges). Remaining:
within-junction grade (#2, ~79 — DIAGNOSED; needs a coherent-fill solver
redesign, deferred to a spawned solver session — see #2 below), self-
overlap (#5, 3 pairs 1.9 m²). The original HEAZ-over-collection
X-Plane crash appears RESOLVED by the committed boundary gate (build
reports 0 off-airport / 0 overlay dropped). HECA failures (tasks 2-5):
1. **Coverage (#1) — FIXED (0ffb8f6):** `test_coverage_within_source_envelope`
   measured emitted vs apt.dat+runways ONLY, omitting DSF pavement (a
   first-class source). HECA emitted +54.9% vs apt-only but only +7.4% vs
   apt+DSF, 0.3% outside boundary = legitimate DSF, not over-collection.
   `_source_pavement_union` now adds boundary-clipped DSF. Adding source
   area only lowers overage → no airport can newly fail.
2. **Within-shape grade — ~79 viol — DIAGNOSED, NOT FIXED (s55, deferred to
   a solver session — see spawned task).** NOT apron decomposition, NOT
   neighbour-holding. Full diagnosis 2026-05-29:
   - Cap = ≤1.5% between ANY two vertices of a junction/apron (all-pair
     Euclidean). `_PER_AXIS_JUNCTIONS=False` so aprons use pure Euclidean
     (matches the test); junctions get arc-length relaxation but the worst
     pairs are too short for that to matter — so the violations are genuine.
   - SPLIT (83 pairs / 12 shapes): **31 "below-floor"** (node seeded at the
     too-low DEM, below its grade-feasible band — FILL fixes) + **52
     "infeasible" (band lo>hi)**, mostly the ~1 km² apron. ALL infeasibility
     gaps are SMALL (≤2.60 m; many exactly 2.60 m).
   - ROOT: `_grade_bands` is seeded ONLY from the 15 CIFP runway THRESHOLDS
     (the 171 interior runway nodes are NOT hard). HECA's thresholds span
     58-142 m, so a node squeezed between a CLOSE high threshold (e.g.
     136.5 m, ~200 m away → forces ≥133.5) and a FAR low one (60.7 m,
     ~4700 m → ≤131.0) gets an infeasible band — the two extreme runways are
     ~1.55% apart over their connecting pavement path (~2.6 m over 1.5%).
   - USER FRAMING (authoritative, 2026-05-29): the DEM is the LEAST-accurate
     input (low-res + smoothed); CIFP thresholds are CORRECT; real taxiways
     follow grade; fill/cut are normal. So a ≤1.5% surface ALWAYS exists and
     "infeasible" just means the DEM is wrong there. The band's all-`lo`
     assignment IS grade-compliant (triangle ineq) — so the fix is to FILL
     toward the band, treating DEM as a within-band preference only.
   - ATTEMPT THAT BACKFIRED (reverted): lifting each terminal group to its
     band floor in isolation → 169 viol, worst 62.2%. Lifting a terminal's
     shared edge ~7 m while its far edge stays at terrain makes a cliff
     INSIDE the shape. LESSON: **fill must be COHERENT across the whole
     connected sub-network** (terminal + abutting aprons + connecting
     taxiways lift together), not shape-by-shape.
   - PLAN for the solver session: a coherent global fill in the final
     difference-constraint pass (`unified_jacobi._project_within_bands` /
     `_directional_relief`) — make terminal groups MOVABLE coupled units
     within the band-projection and alternate cap-projection ∩ band-clamp
     over ALL soft nodes (incl. terminal units), so below-floor nodes lift
     to their floor and infeasible nodes resolve to midpoint, with the whole
     region moving together. Regression-guard: SPJC/SPLP/CYXY grade tests.
   ⚠️ An OTHER AGENT has uncommitted WIP in `junction_repair.py`
   (`_orient_rect_sloping_edge_first` — fixes a rotated-rect mis-split,
   HECA taxiway A #446/447). LEAVE IT ALONE; coordinate before touching
   `_split_sloped_rects_at_violations`.
3. **Junction connectivity cluster:** 2 of 5 FIXED.
   - ✓ 170 orphan vertices (d78e6c4): all within 1.5m of the apt+DSF
     pav_union boundary — junction perimeters following the DSF edge. Test's
     `apt_pavement_boundary` captured row-110 only (built before the DSF
     loop). Fix: union the final pav_union boundary into it (test-only).
   - ✓ rect_short_edges TX52 (5b0d100): discovered lane dead-ending AT the
     pavement tip (both dangling corners 0.03/0.06m from the apt+DSF
     boundary). s54's 25m-isolation exemption missed it (a junction vertex
     18.8m away); added an explicit pavement-tip exemption (dangling edge on
     the boundary). TX15-style interior near-misses still flag.
   - ✓ Rule-2 proximity (6) — TEST RETIRED (32a7718, user design call
     2026-05-29). `test_junction_no_long_edge_proximity` flagged junction
     vertices within 20m PERPENDICULAR of a sloping rect edge — a proximity
     proxy. The REAL invariant is "a node ON the sloping edge breaks the
     rect; proximity is fine as long as only CORNER nodes are shared," which
     is tested directly by `test_no_vertex_on_sloping_rect_edge` (geometry)
     + grade/step (elevation). HECA 213/250/357 were thin connectors
     (5.8-9.4m wide; 213 is a SERVICE ROAD, not a taxiway) running 2-14m
     alongside a stub = false positives. Builder snap + SLOPING_EDGE_SNAP_M
     kept. NOTE: general rule — pavement is a taxiway only with a centerline,
     else undesignated.
   - ✓ vertex-on-sloping-edge (2) — FIXED (3046d8e). ROOT CAUSE: a junction
     vertex left ~0.5m off a sloped rect corner, ON the edge interior
     (un-splittable near-corner; nudged there by weld/conformance AFTER the
     split passes). NEW pass `_snap_near_corner_vertices_to_rect_corners`
     runs LAST (post-conformance, on emitted geometry): snaps any non-rect
     vertex on a sloped 4-corner rect's edge within 1.5m of a corner ONTO
     that corner (all 4 edges). General — prevents at all airports.
   - ✓ neighbour_corners (1) — FIXED (ffd18c4). stub J3's corner sat on
     junction #369's edge 0.52m from vertex v4; conformance's endpoint
     guard (0.5m along-edge) skipped it (t*L≈0.4999) though it's >0.10m
     (test tol) from v4. NEW post-conformance pass
     `_share_neighbour_corners_into_junctions` INSERTS an unshared
     neighbour corner on a junction edge into that junction (test's
     tolerances; junction-scoped; INSERT not snap → +0.49m² vs -20m²).
     CLUSTER #3 NOW FULLY RESOLVED.
4. ✓ **terminal#9 (terminal10)** (491ed20): conformance vertex insertion
   converted the flat terminal to uniform node_altitudes (H26 violation).
   Fix: keep single-altitude shapes flat after insertion.
5. **Self-overlap** 3 pairs 1.9 m²; + build warnings (6 T-junctions + 3 edge
   crossings → mesh slivers; 8 dropped sliver/invalid polygons; DEM extrema
   −19/425 vs real ~42-165m).

## Session 55 — dead-code prune in unified_jacobi (committed 701a463)
Suite remained fully green; this session removed superseded solver
machinery only (behaviour-neutral). Removed from
`elevation_per_surface/unified_jacobi.py`:
- `_RELIEF_OUTER_SWEEPS = 60` — referenced ONLY by a comment; the old
  60-sweep relaxation it bounded is gone (replaced by the single reverse
  pass + difference-constraint bands solve).
- `_USE_LEAF_HIERARCHY` + the `parent_held` block in `_directional_relief`
  — built a per-shape parent-interface hold set that the live reverse pass
  NEVER consumes. The live pass (the `for k, sc in enumerate(order)` loop)
  computes `held` inline from `settled` + `terminal_nodes`. `parent_held`
  was assigned and discarded.
- **Retained** (still live): `rank`, `mrank`, `depth`, `node_owners` — they
  feed the `order_idx` hop-depth sort that orders the reverse pass.
- **Verification:** repo-wide grep confirmed all 3 symbols were confined to
  this one file; full suite 284 passed / 2 skipped / 0 failed (unchanged).

**Remaining open / nice-to-have (suite green, none blocking):**
- SPLP -78 taxiway A SW leg trim (geometry quality; LENGTH/TRIM at
  `_split_centerlines_at_points`; off-center + absorption-guard both ruled
  out — see MEMORY).
- Runway seam clip directive 3 (runway-aware, no terrain-pin on slice nodes).
- HECA over-collection / X-Plane crash (boundary-scope HEAZ surface attach).
- Runway-flex Level-2 (seam>CIFP) implemented but unexercised by fixtures.

## Session 54 FINAL — full suite green
`venv/bin/python -m pytest tests/ -q -n auto` → **284 passed / 2 skipped / 0 failed**
(~1:45), compare_target included. This session: runway-flex 3rd pass (all 3 grade
tests), CYXY Rule-1 + SPJC short-edge geometry fixes, SPJC stub-B two-rect fix, and
the SPJC/SPLP compare_target re-cut.

### SPJC stub B two-rect fix (committed e21cbae)
B (long ICAO-F diagonal) emitted as TWO parallel rects sharing a long edge. The
length-independent fixed-30 m diagonal trim left B's apron end in the apron mouth
(two apron pieces meet at a bend vertex) → `_split_sloped_rects_at_violations`
split it lengthwise. **Fix (2 parts):**
- `pavement/centerlines.py`: diagonal-stub end margin `max(30 m, 0.20·gap)` (was flat
  30 m). Long diagonals (gap>150 m: B/C/E) trim back enough to clear the junction
  curve; short ones keep 30 m (V3 unaffected). B → one rect.
- `junction_repair._drop_thin_orphan_slivers`: trimming C left a thin residue hugging
  C's straight long edge vs the CURVED pavement boundary — touching C at ONLY ONE
  corner (chord-vs-arc), so the "≥2 shared corners" drop gate missed it. **Relaxed:**
  also drop a thin junction whose EVERY vertex is within 5 m perpendicular of one
  rect's long (sloping) edge (`_hugs_long_edge`). General fix for any straight-rect-
  against-curved-boundary sliver, not just C.

### compare_target re-cut (user re-cut fixtures; floors refreshed this session)
User replaced `SPJC_target.osm` + `SPLP_target_tile-13-{77,78}.osm` with fuller
re-cut targets (boundary ribbon densified, runways re-cut), and removed stale
`CYXY_guide.osm` / `HECA_guide.osm` / `SPJC_target.osm.zip`. The hardcoded per-role
floors in `test_compare_target.py` were refreshed to `target − round(0.05·target)`
(SPJC total 904→1330 target / 1263 floor; SPLP-77 164→248/236; SPLP-78 213→335/317).
All 3 compare_target tests GREEN. **Re-cut workflow reminder:** after
`tools/build_target_osm.py`, update BOTH the per-role baseline dict AND the
`*_TOTAL` (run the test, read the printed `target=/out=/matched=` table, set
floor = target − round(0.05·target)).

(Earlier session-54 sections below — runway-flex, CYXY Rule-1, SPJC TX20/TX15 — remain accurate.)

# (prior header) Auto-Patch Status — session 54 (runway-flex + geometry fixes; non-compare_target was 281/0)

## Session 54 — geometry fixes after the runway-flex work (committed a5159ae + 62321ff)
The two remaining pre-existing GEOMETRY failures are FIXED; the non-compare_target
suite is now **281 passed / 2 skipped / 0 failed**.
- **CYXY `test_junction_runway_node_sharing`** (a5159ae): junction#51 had an
  orphan vertex 1.0 m off the 14L/32R runway edge, 1.89 m from the corner — a
  `boundary_dem_bridge` edge-clearance vertex (bridge clears rects by
  `buffer(1.0)`) that `_insert_bridge_contacts_into_junctions` planted on the
  junction edge. `_snap_bridge_vertices_to_runway_corners`'s `snap_tol_m` was
  1.5 m < 1.89 m, so it missed it. **Fix = widen `snap_tol_m` to 2.0 m** so the
  vertex collapses onto the runway corner (satisfies Rule 1 + neighbour_corners).
- **SPJC `test_rect_short_edges_connect`** (62321ff): two discovered (medial-axis
  "TX") lanes with a dangling short edge. **TX20** dead-ends ~74 m from anything
  — a genuine isolated dead-end (user confirmed real pavement); the TEST now
  EXEMPTS a discovered lane's dangling end when it connects at the other end AND
  both corners are > 25 m from any vertex. **TX15** ends 9.9 m SHORT of residue
  junction #132 (medial centerline terminates early; connected pre-solve, severed
  by a post-solve reshaping pass) — a MISSING CONNECTION. New
  `junction_repair._connect_discovered_lane_dead_ends_to_junctions` (post-solve,
  pre-weld) bridges the lane's end corners to the junction's nearest EXISTING
  edge (sourced vertices only) + resamples node_altitudes; weld/emit reconcile.

## NEXT: open / nice-to-have (suite is fully green — no blockers)
- compare_target re-cut + floor refresh is DONE (see FINAL section at top).
- Candidate cleanups (none blocking): SPLP stub/A apron-side residual was solved by
  the runway-flex; the old dead-code in `unified_jacobi` (`_RELIEF_OUTER_SWEEPS`,
  `_USE_LEAF_HIERARCHY` Dijkstra `rank`) is a candidate prune once stable. The
  runway-flex Level-2 (seam>CIFP threshold release) is implemented but unexercised
  by fixtures.

## (s54 earlier) runway-flex third pass (all 3 grade tests now PASS; suite 5→2)

> **READ FIRST:**
> 1. `docs/pipeline_invariants.md` — the agreed working spec (8 invariant sections, A1–H28).
> 2. `docs/elevation_solver.md` — solver model (the directional two-pass + difference-constraint solve below supersede the old cascade/relief framing).
> 3. This file — what sessions 52–54 changed and what's next.
>
> **Working tree:** session-54 work committed (15298cb, 89a2b84). Suite:
> **2 failed / 279 passed / 2 skipped** (`venv/bin/python -m pytest tests/ -q -k "not compare_target" -n auto` ≈ 1:31).
> The 2 remaining are PRE-EXISTING GEOMETRY tests (no grade test fails anymore).

## Session 54 — runway-flex third pass (committed 89a2b84 + 15298cb)
The runway profile is DERIVED from the DEM (interpolated between CIFP threshold
anchors); the DEM is the least-accurate input. When a junction/stub can't reach
grade because it's wedged between a soft apron and a runway-anchored node the
DEM dipped (CYXY 14R/32L dips ~3 m to 691.4 at the 02/20 intersection → stub A
8.9 %), the impossible connection has nowhere to go while EVERY runway node is
HARD. **Fix = a gated third pass `_relax_runway_and_resolve` in unified_jacobi
(after the reverse pass):**
- **Level 1 — free the runway INTERIOR** (CIFP thresholds + seam-pinned nodes
  stay HARD), add the runway grade-chain (`_build_runway_constraints`: long
  edges axial @1.5%, short edges flat+coupled; crossings all-pair), re-run the
  difference-constraint band solve. The DEM dip rises toward the junction and
  the gap spreads over the runway's length.
- **Level 2 — seam > CIFP last resort** (user 2026-05-28): ONLY when a
  tile-boundary seam exists and Level 1 didn't help, ALSO release the CIFP
  THRESHOLD endpoints so the whole runway yields to the seam terrain when the
  runway↔seam connection is physically infeasible (band lo>hi). Seam-pinned
  runway nodes never move. **Currently UNEXERCISED by fixtures** (SPLP solves at
  Level 1) — it's the defined safety net, low-risk but untested-by-suite.
- **Commit metric (the SPLP unlock):** accept a level iff it reduces the
  violation COUNT/TOTAL without worsening the worst (`_within_excess_stats`).
  The old "worst must improve" guard let an unrelated stubborn junction VETO a
  real fix. On commit, moved runway/crossing shapes are written back as
  `node_altitudes` (writeback skips clean runway rects / never touches
  crossings) so the runway side agrees with the shared junction.

**Results:** CYXY 14R/32L interior 691.4→~694.5 (thresholds 693.8/706.3 pinned);
SPLP runway interior near stub A 73.3→70.8 (interior, NOT a threshold — Level 1).
grade[CYXY] + grade[SPJC] + grade[SPLP] all PASS. **Suite 5→2**, no regressions.

**Correction to prior STATUS hypotheses:** CYXY and SPLP stub A were NOT "one
shared mechanism." CYXY = runway-DEM-dip (Level-1 fix). SPLP = a runway-interior
node 2.5 m too high vs a seam node (62 m) too close to grade; the blocker was the
commit METRIC, not threshold pinning. Both the "STEP 3 shift runway thresholds"
note and the SPLP-emit-consensus NEXT-ACTION are now resolved (SPLP = 0 cross +
0 within).

## Session 53 — SPJC junction-along-sloping-edge cliff (committed)
User report: SPJC `primary_parallel/V` (shape #15) survived as a sloping rect with
junction #146 running its WHOLE long edge, leaving a small bare-pavement cliff ("past
builds had this as one large junction").

**Root cause (fully traced):** `junction = pav_union − rects` is FLUSH against the rect
edge (raw residue shares 304/332 m at distance 0.00). The cliff is opened later by
`_push_junction_vertices_off_taxi_rect_edges` (`pavement/vertices.py:201`, called inside
`_compute_elevations` ~elevation.py 1198/1229): `edge_gap_m=1.0` shoves each junction
vertex within 0.5 m of a rect sloping-edge INTERIOR 1.0 m outside → ~0.79 m bare strip.
The push is correct for a STRAY vertex (would split the rect hi/lo plane) but wrong when a
junction runs the WHOLE edge. V survived construction-time dropping only because the
corridor heuristic in `_drop_primary_parallels_embedded_in_pavement` preserves any rect
within 160 m of a runway (V is runway-anchored).

**Fix (per user directive "clip/drop the rect, don't push the junction"):** ONE focused
change in `pavement/absorption.py` — added `CORRIDOR_OVERRIDE_FRAC=0.6`; corridor
preservation is overridden when a junction/apron runs flush along ≥60% of a long edge
(longest contiguous `either_adj` run). V is then dropped/clipped at CONSTRUCTION and
`junction = pav − rects` wraps the area cleanly — no merge/bridge/interior-edge artifacts.
Runway-side corridors are unaffected (runway is subtracted from `junction_pav`, never reads
adjacent). Result: cliff gone; **suite 7→5** (also cleared baseline
`vertices_outside_pavement[SPJC]` + `no_long_edge_proximity[SPJC]`); zero new failures.

**Rejected (see memory `junction_along_sloping_edge_cliff.md`):** (1) re-enable
`ABSORB_RECTS_ALONGSIDE_APRONS` — `source_axis` mis-ID premise is STALE (1 rect now, not
14); absorb-ON went 7→11, post-absorb-reclassify recovered to 8, rest = strip-corner float.
(2) post-emit `_absorb_rects_fully_under_junction` merge — fixed cliff but 7→7 (swapped
failures) from interior-short-edge + bridge artifacts. Construction-time override is
strictly better.

**compare_target:** still 3 fails (SPJC + SPLP×2) — PRE-EXISTING (identical on clean HEAD),
but SPJC geometry shifted (V dropped) so they'll need re-cutting once the suite is otherwise
green (`tools/build_target_osm.py`).

## TL;DR / where to start
Session 52 built the elevation solver out and cleaned up spurious discovered
rects; the grade violations have collapsed from dozens to a handful:
1. **Terminal = rigid flat unit** (conform forward, rigid-shift reverse).
2. **Sloped-rect flat ends = rigid coupled level** + grade-checker fixes
   (airside↔groundside wall exemption; only-where-shapes-touch).
3. **Boundary ribbon sliced like every shape** (don't cut `airport_boundary` at
   the seam) — fixed SPLP self-overlap + dropped SPLP grade 20→2.
4. **★ Direct difference-constraint solve** (`_grade_bands` +
   `_project_within_bands`, in `unified_jacobi.py`) replaced the non-converging
   relief relaxation. Grade = a difference-constraint system; multi-source
   shortest-path bands from the HARD anchors give each node's feasible
   `[lo,hi]` (multi-path handled natively, infeasible nodes flagged), then a
   bounded cap-projection over WITHIN-SHAPE edges (terminals held, rect flat
   ends coupled, both-HARD skipped) settles the rest. Per-tile within-shape:
   **CYXY 86→5, SPJC 22→2, SPLP 2→4.**
5. **Drop spurious discovered (TX) rects** (`pavement/discovered_taxiways.py`):
   (a) wider-than-long apron blobs (SPJC #44); (b) runway-parallel apron/runway-
   edge medial artifacts within 15°+25m of a runway (SPJC TX24/25/27). Both
   leave the pavement as a single junction/apron. SPJC cross-shape 6→0.

**Per-tile grade-test status now** (the binding numbers — build per-tile with
SMOOTHED DEM; whole-airport `build_airport_pavement` MIS-SAMPLES the seam DEM on
cross-tile airports and fabricates phantom seam violations — always measure
per-tile via the grade test or `/tmp/grade_detail.py`):
- SPLP: 4 cross @ 0.2 m (emit rounding) + 4 within (stub) + 2 barely-over junctions (1.6–1.9 %).
- SPJC: **0 cross** + 2 within (apron).
- CYXY: **0 cross** + 5 within (stub 4, apron 1).

## NEXT ACTION — the small residuals (no longer architectural)
1. **Cross-shape emit-consensus at shared corners** (SPLP 4 @ 0.2 m).  In the
   SOLVER a shared node has ONE elevation; the disagreement is at EMIT — a flat
   terminal writes one altitude, a sloped rect writes a 2-value plane (hi/lo
   collapse), and at the shared corner those differ from the neighbour's per-node
   value.  Make the rect/terminal emit honour the exact solved node elevation at
   shared corners (emit per-node there, or only collapse when it preserves them).
   Probe: `/tmp/diag_rect.py`.  (SPJC's terminal↔rect 1.8 m case was the spurious
   rect #44 — already gone.)
2. **A few barely-over stubs/aprons** (CYXY 5, SPLP 4 within): bands-solve
   residuals at tight spots; re-triage real vs emit-rounding.  `/tmp/grade_detail.py`.

## Solver knobs (unified_jacobi.py)
`_project_within_bands` cap-projection sweep cap is hard-coded **1000** at the
call site in `_directional_relief` (fast; grade build ≈ 21 s).  `_grade_bands`
returns `(-inf,+inf)` for nodes unreachable from a HARD anchor (left at DEM).
The old `_RELIEF_OUTER_SWEEPS=60` / `_USE_LEAF_HIERARCHY` / Dijkstra-`rank`
machinery is still present but now only feeds the single reverse pass + the
bands convergence — candidate dead-code cleanup once stable.

## The directional two-pass model (user 2026-05-28, CONFIRMED)
Priorities: if a tile seam crosses the pavement union it is highest priority;
else runway + junctions touching it = priority 1, increasing with hop-distance
outward. Terminals/leaves are outermost. Ties by area.

**Forward pass — terminal/leaves → runway** (`_phase1_hop_priority`, descending
hop-depth): START at the terminal (flat, rigid). Aprons CONFORM to the
terminal's vertices; each shape inward follows DEM clamped to its grade cap,
holding the vertices its leaf-ward neighbour settled. NEVER average. The
accumulated violation is pushed into the final junction→runway connection.

**Reverse pass — runway → terminal** (`_directional_relief`, ascending depth,
leaf-hierarchy holds parent-interface): pull the runway-touching junction the
MINIMUM to reach grade, propagate outward (stub→junction→primary→apron), each
pulled the minimum; finally RIGID-SHIFT the whole terminal (staying flat) if
needed. → grade-compliant everywhere.

## What session 52 changed (committed)

### 1. Terminal = RIGID FLAT UNIT — `elevation_per_surface/unified_jacobi.py`
`_directional_relief` (the reverse pass) now treats each terminal as ONE rigid
flat variable (lines ~748–805):
- `terminal_groups` = node-sets of each flat (terminal) shape; `terminal_nodes`
  = their union.
- Every NON-terminal shape HOLDS its terminal-shared vertices (`held |=
  terminal_nodes ∩ nodes`) → aprons CONFORM, never flex the terminal boundary.
- When the terminal shape itself is reached, `_rigid_shift_terminal(gi)`
  translates the WHOLE group to the level closest to its forward-pass DEM
  centroid (`term_level0`) that keeps every connection to a settled
  NON-terminal neighbour within grade (band from `cap_adj` = per-edge grade-cap
  adjacency over `edge_grade`). Feasible band → clamp to it (minimum shift);
  infeasible → midpoint (minimise worst violation).
- **Why this and not the freeze-pin tried first:** freezing all terminal nodes
  enforced conformance but BROKE the "rigid-shift if needed" half of the model
  (an apron squeezed between a frozen terminal and the runway couldn't reach
  grade → spurious within-apron violations). The rigid-shift fixes BOTH the
  shared-vertex consensus AND lets the relief pull the terminal up/down.

### 2. Grade-checker false positives — `tools/check_grade.py`
The two STEP checks (`_check_vertex_to_edge_step`, `_check_edge_midpoint_step`)
asserted vertical continuity between ANY two shapes within 5 m horizontally.
Two corrections (user 2026-05-28):
- **Airside↔groundside skip:** `_is_groundside` / `_airside_groundside_pair`.
  Groundside pavement (`groundside_pavement`/`service_road`/`service_junction`)
  is deliberately separated from airside by a clearance gap + retaining/vertical
  wall, often several metres — NOT meant to be flush. Skip those pairs. (This
  alone cleared ~161 CYXY false `apron↔groundside` steps.)
- **Contact tolerance `_STEP_CONTACT_TOL_M = 1.0`:** only flag a step where the
  two edges actually TOUCH (shared boundary); a gap (no pavement between, 2–5 m
  apart) may legitimately differ in height. Gate `best_d2 > tol²` in both
  checks. (`_pair_grade_limit`'s docstring wrongly claimed groundside was already
  skip-listed — it isn't; `ROLE_GRADE_LIMITS['groundside_pavement']=0.04`.)

## NEXT ACTION — sloped-rect shared-vertex consensus
The remaining grade failures are the sloped-rect EMIT gap. In the SOLVER a
shared vertex is ONE node with ONE elevation (consistent). But a sloped rect is
EMITTED as a 2-value plane (`altitude_high`/`altitude_low`, collapsed within
`_RECT_COLLAPSE_TOL_M`); at a shared corner that plane interpolates to a value
that differs from the adjacent junction/apron's per-node `node_altitudes`. Same
emit-consensus class just solved for terminals.

**Confirm the mechanism first** (don't assume): pick a worst SPJC pair, e.g.
`primary_parallel/-10030 (17.9) ↔ apron/-10112 (16.6)` at d=0.00, and check
whether they share a canonical node, what the SOLVER value at that node is, and
why the rect emits 17.9 vs the apron's 16.6. Reusable probe template:
`/tmp/diag_term_apron.py` (node-sharing + per-vertex emit dump) and
`/tmp/grade_detail.py` (per-tile build + `check_grade.run_checks`, cross/within
by role-pair).

Then make the rect's emitted corner value agree with the shared-node solved
value — either emit rects with per-node altitudes at shared corners, or make the
hi/lo collapse honour the exact solved node elevation at every shared vertex.

After that: CYXY apron/junction INTERNAL over-grade (apron `-10078` spans
703.8→701.8 over its own width; the giant east apron grades flat far from its
edge) — needs apron decomposition, a separate piece.

## Still deferred (from session 51, re-confirm after sloped rects)
- **Phase-1 "no averaging"** at the first leaf (`_project_shape` else-branch
  splits an over-cap edge 50/50). For a rect this only averages the cap-0 CROSS
  edge, which is forced + correct (a taxiway cross-section must be level), so
  it's lower priority than STATUS-51 implied. Revisit if a leaf rect still
  emits flat-at-mean when it should slope along-axis.
- **Seam-priority BFS seeding** (`_runway_node_set` seeds runway only): for
  cross-tile airports (SPLP/MMOX) seam-hard-but-not-runway nodes should ALSO
  seed BFS (`seam ∈ base_hard AND ∉ runway_nodes`). SPJC's seam doesn't cross
  runway, so runway stays top priority there.

## Current test failures: NONE — FULL suite green
`venv/bin/python -m pytest tests/ -q -n auto` → **284 passed / 2 skipped / 0
failed** (~1:45), compare_target INCLUDED. The 2 skips are env-gated
(`test_elevation_terrain_following` needs O4_TEST_TILE; `test_boundary` CYXY
ribbon-share). First fully-green full suite this session.
Build per-tile with smoothed DEM to reproduce grade numbers; whole-airport build
mis-samples the seam — use the grade test or `/tmp/grade_detail.py`.

## User algorithm spec (verbatim, 2026-05-28) — keep within reach
> Terminals must be flat, and aprons must conform to their vertices. The
> terminal should be the starting point of our solver pass which should be
> grading aprons FROM the terminal inward to the runway, then the reverse pass
> comes back enforcing grade from runway to terminal and adjusts the whole
> terminal if needed.

> The first pass working from leaves towards the runway should continue
> following DEM and clamping to grade right up to the runway … push the whole
> violation into that last connection. Then the reverse pass pulls the runway
> junction just the minimum required to be within grade, and works it's way
> back out the leaves … and finally at the very end leaves we force them into
> grade compliance.

> If a tile seam crosses airport pavement union, then it's the highest
> priority, and runway thresholds become second.

## Build & test workflow (unchanged)
- Repo `/Users/noah/Ortho4XP-novemberlima`. Venv `venv/`. **No system Python.**
- Single airport: `from auto_patch.pipeline import build_airport_pavement;
  build_airport_pavement("CYXY", xplane_root(), compute_elevations=True)`
  (needs `src/`, repo root, `tests/` on `sys.path`; `from conftest import
  xplane_root`). Build ≈ 15 s.
- Full suite: `venv/bin/python -m pytest tests/ -q -k "not compare_target" -n auto`
  (~2:18). Skip compare_target during dev; re-cut only when otherwise green
  (`tools/build_target_osm.py`).
- Grade audit on an emitted patch: `tools/check_grade.py`. The grade test builds
  PER-TILE with smoothed DEM (production-like), not the whole-airport build.

## Gotchas (unchanged but bite every session)
- **Ortho4XP caches `auto_patch` imports** — full quit+relaunch after edits.
- **Import cycle:** `junction_repair` ↔ `elevation` — go through
  `auto_patch.pipeline`, never import `junction_repair` first.
- **Bash CWD persists** — run probes from repo root, not `src/auto_patch`.
- **Temp/debug scripts + generated OSMs go in `/tmp`**, never the repo.
- **DEM smoothing:** production gets Ortho4XP's `apt_smoothing_pix=8`-smoothed
  `tile.dem` via `override_dem`; the standalone path replicates it. Raw
  `tile_dem=DEM(...)` probes use UNsmoothed elevations (geometry is
  DEM-independent; altitudes differ from production).
- **`git stash` bit me this session:** an interrupted `git stash && … ; git
  stash pop` left the pop un-run, silently reverting an edit. Prefer an env-gated
  toggle or a scratch copy over stash for baseline A/B comparisons.

# On-pavement service-road carve (the #198 decomposition)

User directive (2026-06-11, STATUS s78 close): the switchback strip
beside HECA apron #198 (emit-#197, ~30.110943, 31.402940) is a ROAD —
it should have been SPLIT AT ITS MOUTH and decomposed: 4 % road
grading, a JUNCTION at the far south-west bend, SLOPING RECTS for the
two main legs.  The s78p5 apron terrain-break edge retreat treats the
symptom; this carve is the real fix and is expected to replace the
retreat at that site.

## 1. Why phase-1 misses it today (s79 investigation, measured)

The road exists in source data ONLY as apt.dat **1206 truck-route
edges** (edge 98→96 runs 100 % inside the strip) plus its own 110
polygon pav[57] (`asphalt_D3`, ~20 m wide, 19.5 k m²; the touching
aprons are `concrete_D`).  No OSM road within 150 m; no 1202 edge
within 135 m.  Four independent gates each suffice to miss it:

1. `ENABLE_SERVICE_ROADS = False` — `service_road_centerlines` (the
   1206 → centerline derivation, pipeline.py ~1289) is skipped.
   (`Airport.truck_edges` itself is always parsed — the gate only
   skips the derivation.)
2. Even enabled, `service_roads.build_service_road_network` emits ONLY
   outside aircraft pavement BY DESIGN; the strip is 110 pavement.
3. `discovered_taxiways` runs on pav_union, where the strip is fused
   into the 4.19 M m² airside slab — clearance at the strip point is
   160.7 m vs the 3–16 m medial band.  No narrowness exists at union
   level (on pav[57] ALONE the same code finds the lane instantly).
4. The phase-2 apron-neck split needs a narrow neck; the strip is rim
   pavement of the blob.

Identity is recoverable BEFORE `unary_union`: strip-shaped source
polygon + 1206 route inside + zero 1202 edges (+ surface code differs
from the touching aprons).

## 2. Detection rule (USER RULINGS 2026-06-11, KML-confirmed loop)

★★ USER: roads = the **1206 lines ONLY** — every polygon-based
candidate (the s79 audit's strip-shaped 110 polys at HECA and CYXY)
is an APRON, not a road.  Only pavement **< 10 m wide** is classified,
and **nothing near a terminal**.

Line-driven rule (probe /tmp/probes/s79_road_segments_kml3.py is the
reference): sample the MERGED 1206 polylines
(`apt_dat_reader.service_road_centerlines`) every ~6 m against the
PRE-DECOMPOSITION pav_union; a sample qualifies when
  (a) it lies inside pavement,
  (b) the PERPENDICULAR pavement cross-section ≤
      `ROAD_CARVE_MAX_WIDTH_M` (13 m — calibration below),
  (c) it is ≥ 30 m (`ROAD_CARVE_TERMINAL_CLEAR_M`) from any terminal.
Qualifying runs ≥ ~20 m become road; the non-qualifying stretches
BETWEEN runs (wide spots) become junction territory.

Calibration at the #198 switchback (user-specified geometry, verified
against the data — 1206 chain 265→91→131→132→133, node 133 = the
user's "top" point ±7 m): leg 1 cross-section 8.2–9.4 m; the U-turn
bulge 11–24 m (junction, per the user); leg 2 a steady **12.2 m** in
the fused DSF pavement (the user calls it < 10 m — shoulders).  Hence
13 m, NOT a literal 10.0 (which drops leg 2) — confirm via KML.

The road pavement at #198 is **DSF-sourced**, not an apt.dat 110 poly
— detection must run on the fused pav_union, never on apt.pavements.

Audit at 13 m: HECA 7 runs / 798 m; CYXY 5 runs / 623 m (named
routes — "Props fuel truck", "Crew cars", "D" — apt.dat confirms
road-ness); SPJC 193 truck edges / SPLP 7 / MMOX 54 / HEAZ 0 to be
re-audited with the line rule before ship.  KML confirm artifacts:
/tmp/probes/s79_road_segments_{HECA,CYXY}.kml (red = carve runs,
yellow = 1206 context, grey = terminal guard).

## 3. Design

Gate: `SERVICE_ROAD_CARVE` (config.py; default OFF during bring-up,
ON at ship; OFF = byte-identical).  Detection lives in
`pavement/service_roads.py` (alongside the existing off-pavement
builder); the OSM small-road machinery stays gated by
`ENABLE_SERVICE_ROADS` (unrelated, still deferred).

**Step A — detection (line-driven, §2).**  Run the §2 sampling rule on
the merged 1206 polylines × the fused pav_union (+ guard: never carve
within a 3 m runway halo — the s69 carve lesson).  Output per
qualifying run: its centerline piece + the run's measured local width
(the swept perpendicular cross-sections, ≤13 m) — the width the rect
builder will use.

**Step B — ride the taxiway pipeline (★★ USER RULING 2026-06-11):
"Roads should work like taxiways: rects → junctions and absorbed when
inside or running along the edge of other pavement."**  The qualifying
1206 runs join `osm_centerlines` as ref-tagged entries (`SVC1`, … —
the discovered-taxiways precedent, pipeline.py ~1455) and flow through
the SINGLE existing decomposition: `_build_taxi_rects` makes SLOPING
RECTS on straight runs (at the run's real ≤13 m width), the standard
junction machinery forms JUNCTIONS at bends / crossings / mouths (the
#198 U-turn bulge and the top connection to taxiway S), and the
EXISTING absorption passes dissolve road rects that are inside or
long-edge-adjacent to other pavement — exactly how an apron-embedded
discovered taxiway dissolves today.  No separate carve/subtraction
path: the rect+junction build IS the carve.  Expected #198 output
(user-specified): two sloping rects on the legs, a junction at the
U-turn, a junction at the top connection to S.

Role: a `ref → role` override so SVC refs classify as `service_road`
(already in `ROLE_GRADE_LIMITS` at `SERVICE_ROAD_MAX_GRADE` = 4 %) —
geometry-based classification would read them as stubs/connectors.
The §2 eligibility rule (width ≤ 13 m, terminal guard) stays the
SCOPE limiter: it keeps the 153-edge HECA truck network from
generating rect/junction churn across every apron only for the
absorption pass to dissolve it again.

**Step C — elevation = the taxiway path with the 4 % law.**  Road
rects/junctions are airside shapes solved like any taxiway, but their
centerline edges enter the NETWORK-PROFILE FIELD with per-edge
``eff_cap`` = `SERVICE_ROAD_MAX_GRADE` (the field's adjacency already
carries per-edge caps), and the validator reads `ROLE_GRADE_LIMITS`
["service_road"] as it already does.  At 4 % the solve is
DEM-following in practice (seeded at DEM, caps rarely bind).  The
#198 cliff then emerges WITHOUT special machinery: the road and the
apron rim are separate pavements with grass between, and under the
across-grass ruling (s79 item 2 — interior-path entries) nothing
couples them straight across; the road follows terrain at 4 % while
the apron holds its plane, and the clearance machinery renders the
face.  ⚠ DEPENDENCY: that cliff behaviour assumes the item-2
across-grass kill lands; until then the law's straight-gap entries
would couple road↔apron across the grass gap — build item 2 first or
measure the interaction explicitly.

**Step D — clearance semantics.**  Verify `service_road` is EXCLUDED
from aircraft wingtip/strip clearance generation (clearance tables are
aircraft-code keyed; trucks get none) and from RESA logic.  The s78p5
terrain-break retreat stays gated ON; expectation at #198 is its
trigger disappears (the route-pinned rim becomes a real road surface)
— verify, don't assume.

## 4. Validation protocol

1. Re-baseline FIRST — a concurrent session is editing
   apt_dat_reader / junction_rules / junction_repair / apron_necks /
   pipeline (row-120 painted-line centerlines + holed-apron residue
   work); build this on top of their landed state.
2. Site invariants (HECA): road profile climbs ~103.8→108.7 within
   4 %; apron #198 rim stays at the s78p5-blessed 104.3–105.1 with the
   cliff face along the road; the strip's ~12 within-violations gone;
   05C 108.70, 05L 57.9–62.8 smooth, A4/A5, terminals unchanged.
3. HECA within ≤ baseline (68 at s78p5; re-measure at re-baseline).
4. CYXY + SPJC grade gates stay 0/0/0 — at CYXY first run with the
   three carve sites EXCLUDED (HECA-only allowlist), then a separate
   measured run carving them (§6).
5. compare_target gates (HEAZ/SPLP/MMOX unaffected per audit — assert
   that), full suite vs re-baseline, PYTHONHASHSEED 1==2 determinism,
   gate-off byte-identical.
6. In-sim verdict: #198 road + cliff (user), then the other two HECA
   sites (pav[56], pav[61]).

## 5. Work order

1. Audit probe → committed as a test-adjacent tool (it is the
   regression guard for the qualifying-run set).
2. Gate + detection (Step A) + unit tests on the six fixtures'
   qualifying runs.
3. Centerline feed + role override (Step B), geometry-only builds:
   #198 emits 2 leg rects + U-turn junction + top junction; absorption
   dissolves any on-apron road rects; junction/shape-count churn
   measured at all fixtures.
4. Elevation (Step C: field edges at 4 % eff_cap) + clearance
   semantics (Step D); full battery (§4).  Sequence AFTER (or
   measured against) the item-2 across-grass fix — the #198 cliff
   depends on it.
5. Retire/confirm the s78p5 retreat interaction at #198.

## 6. Detection rule evolution (user KML-verdict rounds, 2026-06-11)

Final qualifying rule for a sample on a merged 1206 route — ON
pavement, outside the runway 3 m halo, and ANY of:
* **Mode A** — perpendicular cross-section ≤ `ROAD_CARVE_MAX_WIDTH_M`
  (13 m; #198 legs 8.2-12.2 m).
* **Mode B** — inside a NARROW **apt.dat-authored** strip polygon
  (mean 2A/P ≤ cap; CYXY pav[1] "New Taxiway 40" blends into apron so
  chords read 16-34 m).  ⛔ allowing DSF source polys here added 5
  unapproved HECA rects — apt-only is load-bearing.
* **Mode C** — EDGE-HUGGING: ≤ `ROAD_CARVE_EDGE_HUG_MAX_M` (8.5 m)
  from the pavement boundary ("not surrounded by apron", round 3) —
  UNLESS within `ROAD_CARVE_TERMINAL_RIM_M` (300 m) of a terminal AND
  moving perpendicular to the direction toward it (the RADIAL
  "alongside the terminal row" test, round 4: those rim roads absorb
  into the apron; ring-tangent variant was too noisy on pad rings).
* Terminal close-guard (30 m) = same radial-alongside test — roads
  passing a terminal CORNER keep (HECA terminal4 → junction #168).
* **Strip extension** — ≥15 m of 1206 route inside a narrow apt strip
  ⇒ the strip's MEDIAL lane continues the road past the polyline's end
  (CYXY pav[1] ramp: 'Crew cars' enters 97 m, ramp runs ~250 m more).

Pipeline mechanics (all SVC-guarded, gate-off byte-identical): TX
discovered lines covered ≥60 % by road runs yield at the feed; SVC
exempt from off-corridor drop / dedup-vs-emitted / interior corner +
long-edge gates / stub-ref dedup; rect width floor = the run's
measured width (`svc_widths` threaded into `_build_taxi_rects`); SVC
half-width floor 2.5 m (CYXY 'D' 5.3 m ramp); rect overlap: SVC beats
TX/unref, REFERENCED taxi beats SVC (CYXY 'Props fuel truck' inside
taxiway A2 = absorbed, user-consistent), SVC-vs-SVC clips the loser
(out-and-back routes); SVC rect area floor 100 m²; road-only junction
re-role → `service_junction`.  `O4_SVC_DEBUG=1` traces builder drops.

## 7. Status

Geometry (rounds 1-4) @f40835c; interior-path entries @b1e36c5;
**Steps C/D @b391e27**: field 4 % law (SVC edge-length scaling),
designed-wall exemption (road-family pairs in check_grade — the
exactly-one-groundside test missed road↔groundside and fired 151
false steps at the CYXY ramp), groundside separation from roads,
ROAD-BLIND apron corridors (corridor-seeding an apron from a
descending road split HECA #266 into two write families), wingtip/
RESA verified road-free.

Measured: **CYXY roads-on 0/0/0**; HECA roads-on 57/0/0 vs 51
roads-off — the +6 = sub-metre pairs on the re-cut #198-area apron
#271; invariants held; suite 325p/2f.  The remaining items were
USER VERDICTS, not code: (a) in-sim look at the road cliffs/walls
(#198, the CYXY ramp), (b) the #271 residual, (c) then flip the
default ON.

> ✅ **RESOLVED (2026-06-30 audit): `SERVICE_ROAD_CARVE` now defaults ON**
> (`O4_SERVICE_ROAD_CARVE` = "1", config.py). The verdicts above were taken and the
> default flipped; nothing in this plan remains unbuilt. The 4 % road law lives in the
> current `grade_graph.py` (`SERVICE_ROAD_MAX_GRADE`, `service_road`/`service_junction`
> in `ROLE_GRADE_LIMITS`); the across-grass dependency on interior-path entries is moot
> (that concern is handled by the grade_law reach-band rework).

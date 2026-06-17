# Runway shoulder segmentation reach (segment at the apt.dat shoulder edge)

**Status:** IMPLEMENTED (2026-06-17, dev). **Gate:** `RUNWAY_SHOULDER_SEGMENT`
(`O4_SHOULDER_SEGMENT`, default ON); gate-off byte-identical to baseline
(proven at OMAA).

## ★ RESOLUTION (user 2026-06-17) — the scope is SEGMENTATION, not width
The original plan below assumed the shoulder *width* was wrong and needed a
variable, measured, centerline-graph-aware re-derivation. **That premise was
wrong for OMAA.** Investigation found:
- OMAA's runway shoulders have **NO vector source** — apt.dat row-110 has no
  polygons along the runways, and the DSF pavement (1.5 M m²) is all 318 m away
  on the apron/terminal side (zero coverage along either runway). The ~97 m /
  ~75 m widths the user saw exist only in the orthophoto.
- Both runways are **byte-identical in apt.dat**: `60.05 m` + shoulder code
  `2029` (⇒ 20 m/side ⇒ 100 m). **The user confirmed apt.dat is authoritative:
  treat both as 60 m runways with 20 m shoulders (100 m).** So the fixed
  widening to 100 m is CORRECT and must NOT change. Measuring admitted pavement
  gives a *misleading* answer (it catches the adjacent parallel D/E taxiway
  pavement, not the shoulder).
- The real bug is purely **segmentation**: the runway must be split where
  pavement/taxiway polygon edges intersect the runway **shoulder edge** (the
  100 m extent), not just the bare 60 m runway.

### What was built
The runway-segmentation breakpoint collector (`pipeline.py` ~L505-780) splits
the runway where adjacent pavement boundaries CONTACT it, by intersecting each
pavement polygon's boundary with a proximity band around the runway rect. That
band used a **fixed generic 7.6 m** shoulder budget (`INTERSECTION_PROX_M`
= 12 m), reaching only ~42 m from a 60 m runway's centerline — so an exit
connecting at the 50 m shoulder edge was never seen, no seam fired there, and
the segment boundary landed at the wrong longitudinal position (the OMAA
13R/31L gap; wedge apron filled the misalignment, breaking segmentation).

**Fix:** when apt.dat declares an explicit shoulder (`shoulder_code // 100 ≥ 1`),
the per-runway contact budget becomes that coded shoulder + chart tolerance
(`max(7.6, coded) + 4.4`), so seams land where pavement meets the shoulder edge
as defined in apt.dat. Runways with no coded shoulder (code < 100: SPJC/CYXY/
KPHL) keep the 7.6 m budget ⇒ **byte-identical**. Only OMAA + HECA are touched.

### Why the FAA fallback stays (the 7.6 m is NOT a blind guess)
The reach for a runway with no apt.dat-coded shoulder is the FAA standard
shoulder (7.6 m) + chart tol = 12 m.  This is the *determination* for the case
"apt.dat declares a shoulder SURFACE but no width" — SPJC row-100 codes shoulder
surface 27/28, and its row-110 shoulder pavement sits ~11 m past the rect but is
NOT folded into the runway width (it lives in the junction cut, by design).
**It is load-bearing:** the "no generic — reach only the determined edge"
variant was built and tested; it REGRESSES SPJC (`test_pavement_rests_on_
source[SPJC]` fails — no seam fires at the shoulder's end).  And the
pavement-detected cases (KPHL extent-merged ring, CYXY whole-polygon absorption)
already fold their shoulder INTO the runway rect, so the rect is the determined
edge everywhere it is consumed; their segmentation has no gap (KPHL inter-shape
steps 0→0), so the tighter reach there only adds within-shape churn (+2) and
makes HECA worse (79→87) — measured, rejected (user ruling 2026-06-17: keep
surgical).  So: coded shoulder ⇒ reach to it; detected-and-folded shoulder ⇒
already in the rect; shoulder present-but-unsized ⇒ FAA standard; truly no
shoulder ⇒ also FAA standard (harmless, no pavement out there to catch).

### Results (authoritative `tools/check_grade.py`)
- **OMAA**: inter-shape STEPS 4→0 (vertex-to-edge 1→0, mid-edge 3→0 — the gap
  signature), cross-shape 9→7; runway segments 31→48 (now split at the
  shoulder-edge connections); the wedge apron at the wrong position is gone and
  the runway seam shares a node with the connecting aprons. Within-shape apron
  grade +4 (apron-interior grading is separate WIP, left alone).
- **HECA**: inter-shape 0→0 (perfect), within-shape 81→79 (−2) — net neutral.
- **SPJC/CYXY/KPHL**: byte-identical. Suite: no new failures (4 pre-existing).

---

## Original design plan (superseded by the resolution above)

**Gate:** reuse/extend `RUNWAY_SHOULDER_EXTENT`
(`O4_SHOULDER_EXTENT`); keep gate-off byte-identical until shipped.

## Problem (OMAA 13R/31L, user-reported)
Row-110 pavement runs along the runway wider/more variably than the widening
captured, leaving residue thin apron strips (`-10226` 445×74 m, `-10227`,
`-10260`, `-10258`, `-10228`). Two passes both miss it:
- `_detect_runway_shoulders` (apt.dat shoulder **code** 2029) → widens to a
  **fixed** width (60→100 m); doesn't adapt to the real extent.
- `_detect_runway_shoulder_extent` → measures extent but **clamps to
  `max_w=15 m`** and **defers apt row-110** (`MAX_APT_FRAC=0.5`).
Result: pavement beyond the fixed/clamped width becomes apron strips, which then
**break runway segmentation** → segment boundaries land at the wrong longitudinal
positions (gap: apron `-10236` node at 24.4357404,54.6481929 should be at
24.4353954,54.6486788 where a taxiway connects and a runway segment should be).

## ★ Domain model (user 2026-06-16) — the key to robustness
Pavement beside a runway is NOT uniform shoulder. Leverage the **taxiway
centerline graph**:
- **Diagonal stubs / high-speed exits** meet the runway at SHALLOW angles → their
  pavement runs **up to ~180 m from the centerline, parallel to the runway edge**
  to accommodate the angle. Two exits meeting at steep angles from both directions
  → a wide continuous stretch.
- **Between taxiway connections the stretches are typically MUCH LONGER** and carry
  only the true (narrow, consistent) shoulder.
So: the wide pavement near a centerline-graph connection is the **exit/junction**
(owned by those shapes); the consistent pavement in the **long between-connection
stretches** is the **shoulder**. Don't infer shoulder width from a fixed code or a
blanket percentile over ALL stations — the exits inflate it.

## Robust, variable model
1. **Connection map from the centerline graph:** find every taxiway/stub/exit
   centerline that meets runway 13R/31L (and each runway). Project its runway-
   contact onto the runway axis → a set of connection arc-positions, each with a
   pavement *reach* (how far along the runway edge that exit's pavement extends —
   up to ~180 m for shallow exits; derive from the exit's angle/geometry or measure
   the contiguous wide run around the contact).
2. **Mask connection zones.** Stations within a connection's reach are EXIT/junction
   pavement — exclude from the shoulder-extent statistic (they're expected wide).
3. **Measure shoulder variably in the between-connection stretches.** Over the
   masked-clean stations, take a robust per-side extent (e.g. median/75th-pct with
   run-length consistency) and widen the runway to it — NO fixed `max_w` clamp;
   the cap is "is this consistent along the long stretch", not an absolute metre.
4. **Unify apt row-110 + DSF.** Don't defer apt row-110 to other passes (they leave
   residue); the extent pass handles both. apt.dat shoulder code is a CONFIRMING
   signal that shoulders exist, not the width source.
5. **Segment the runway at the connection arc-positions** (runway_segments.py) so
   junctions join at the real taxiway contacts → closes gaps like `-10236`.

## Files
- `src/auto_patch/pavement/runways.py` — `_detect_runway_shoulders` (apt-code),
  `_detect_runway_shoulder_extent` (~L1029, the variable measurer to extend),
  `_absorb_crossing_vertices_into_adjacent_rects`.
- `src/auto_patch/pavement/runway_segments.py` — runway segmentation at connections.
- `src/auto_patch/config.py` — `RUNWAY_SHOULDER_EXTENT_*` params (MAX_M=15 clamp,
  MAX_APT_FRAC=0.5 deferral, STATION_M=25, MIN_COVERAGE=0.8).
- Centerline graph: `layout.apt_taxi_centerlines` (+ `_discovered_centerlines`),
  runway polygons in `layout.shapes`.

## Build & test
- `O4_SHOULDER_EXTENT=1 PYTHONHASHSEED=0 venv/bin/python3 tools/build_target_osm.py OMAA --out /tmp/x.osm`
- Verify: no thin apron strips along 13R/31L (aspect>4, dist<5m to runway); the
  gap at 24.4353954,54.6486788 closes (runway segment + junction there); SPJC/HECA/
  CYXY/KPHL shoulders unchanged or improved; gate-off byte-identical; suite ≤ base.
- Probe: list aprons/junctions within 5 m of a runway with aspect>3 (the residue
  strips) before/after.

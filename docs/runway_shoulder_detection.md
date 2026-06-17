# Robust runway shoulder detection + segmentation (variable, centerline-graph-aware)

**Status:** in progress (2026-06-16, dev). Replaces the brittle fixed-width
shoulder widening. **Gate:** reuse/extend `RUNWAY_SHOULDER_EXTENT`
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

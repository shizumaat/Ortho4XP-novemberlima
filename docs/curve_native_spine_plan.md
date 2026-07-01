# Curve-native taxi spine — recognized painted centerlines, retiring bend-split

Status: **design + prototype (recognition DONE & validated; grade/pavement
integration BLOCKED on curve-tolerant junction slicing).** All gated off.

## Goal

Replace the sparse, straight apt.dat 1201/1202 taxi routes with the **real
curved taxiway centerlines** (from the row-120 painted lines) as the grading
spine, so taxiway/junction/apron pavement grades along true curves instead of
straight chords with hard corners.

## What is built and validated (gated `O4_RECOGNIZED_CENTERLINES`)

`src/auto_patch/centerline_recognition.py` — **route-anchored recognition**:

* **Discriminator (user-approved via KML):** a real centerline RIDES a taxi
  route (within `_RIDE_TOL_M=7`, tangent-aligned `≥_MIN_RIDE_M=8` m) AND
  **TOUCHES** it (`_TOUCH_TOL_M=3.5` m) — the touch test rejects EDGE lines
  (offset a half-width) and hold bars (perpendicular). A pure medial-axis /
  width classifier FAILS on a dense field (abutting pavements have no internal
  boundary → everything reads "interior"); route-anchoring is the keystone.
* Feed each recognized painted line **as-is** (the KML geometry), **no runway
  clip** (the pipeline clips at runway edges), stray paint (rides no route) is
  dropped. `linemerge` + endpoint-snap join touching pieces; `_resample` to
  `SPINE_STEP_M` gives even node spacing (curves matched to straights).
* **One source:** `pipeline.py` sets `osm_centerlines = apt_taxi_centerlines`
  after recognition so RECTS and SPINE build from the SAME centerlines (no
  straight-vs-curved conflict); synthetic `~SJ` junction spines are disabled
  when recognition is on.

Diagnostic tools (all new): `tools/recognize_centerlines_kml.py`,
`tools/classify_centerlines_kml.py`, `tools/dump_route_network_kml.py`
(`--global` forces Global Airports apt.dat via `O4_FORCE_APT_DAT`).

SPJC: 1825 painted → 157 recognized centerlines; edge lines rejected; geometry
confirmed correct in Google Earth.

## The blocker — bend-split vs curves (the two representations)

`TaxiCenterline` stores the SAME centerline twice:

* `line` — a **bend-split piece** (short, roughly-straight segment from
  `split_merged_centerline`), consumed by the **rect builder**.
* `route_line` (`chained_line`) — the **continuous** parent line, consumed by
  the **grade solver** for the anisotropic Δs∥ arc-credit.

Bend-split exists ONLY because the pavement model is **straight quadrilateral
rects** — a curved taxiway can't be one straight quad, so it is chopped into
straight segments.

**Why curves break:** the continuous recognized line DOES cross an apron (the
KML is correct — measured 24 crossings vs the network's 28; the extra dead-ends
are stand lead-in lines that legitimately drop). But the apron/junction slice
(`junction_spine._full_centerlines` → `_partition_junction`) reads the bend-split
**pieces** (`item.line`). A curved line splits into several pieces, and an
interior piece lies wholly inside the apron (`boundary_crossings=0`) → the slice
finds no boundary-to-boundary through-path → `single_face` → **no spine**. A
straight route stays ONE piece and crosses cleanly. So it is **curve → multiple
pieces → slice sees a piece that doesn't cross**, NOT a discontinuity and NOT a
"straight is required" rule.

Slicing on the continuous `chained_line` instead regressed grade and broke the
per-junction invariants (`vertices_have_source`, `corners_shared`) — the same
wall the parallel `curved_runway_crossing_spine` work hit.

## The curve-native model (the real fix, retiring bend-split)

1. **Buffered-centerline pavement:** a taxiway = its centerline **buffered by the
   half-width** — a polygon that follows the true curve exactly. No bend-split;
   the pavement follows the curve; the spine IS the curve end-to-end.
2. **Curve-tolerant junction slice:** `_partition_junction` must accept a
   **curved** cut-line — re-derive corners/sources ALONG the arc so the
   per-junction invariants hold for curves (not just straight cut-lines). This is
   the keystone the whole thing waits on (shared with `curved_runway_crossing_spine`).
3. **Retire bend-split** for recognized centerlines once (1)+(2) land: `line`
   becomes a curve, `route_line == line`, one representation.
4. **Grade:** already curve-ready via `route_line` arc-credit; verify it improves
   once the spine is genuinely continuous through junctions/aprons.

## Phasing

* **P0 (done):** recognition + tools + one-source plumbing + even resample. Gated.
* **P1 (blocker):** curve-tolerant `_partition_junction` (curved cut-lines,
  per-junction-local invariant repair). Coordinate with `curved_runway_crossing_spine`.
* **P2:** buffered-centerline pavement model (replace straight-rect for recognized
  taxiways); retire bend-split.
* **P3:** re-baseline grade/conformance across fixtures (SPJC/HECA/CYXY); flip
  default on if it beats the straight-route baseline.

## Current numbers (SPJC, recognition on, straight-rect pipeline)

within-grade 486 (raw) → ~545 (recognized); conformance ~0–1 T-junction. The
recognized spine grades slightly WORSE ONLY because of the P1 blocker (curved
pieces don't slice aprons → those stay flat). P1 is expected to flip this.

"""Structural-fidelity gates against the reference fixture outputs.

``tests/fixtures/SPJC_target.osm`` and the per-tile
``tests/fixtures/SPLP_target_tile*.osm`` are the canonical builds
(regenerated 2026-07-05 with the curve-native / route-arc global-slice
pipeline).  Every code change must continue
to reproduce these outputs: the tests compare the produced layout
against each target shape-for-shape via
``tools/compare_target.match_by_role`` and assert that each role's
match count stays at or above an established baseline.

If a future change drops matched shapes below the baseline — even
if every invariant test still passes — these gates fail.  That makes
regressions visible the way the comparison tool does manually.

Baseline reset 2026-05-13 after:
  * diagonal V3-style stub trapezoid emission (Approach A) +
    ``db_local ≥ 15°`` digit→STUB classifier tightening.
  * Seam-anchor architecture: ``split_pavement_at_seams`` +
    ``apply_seam_dem_anchors`` + Stage A runway regrade +
    unified-Jacobi seam-HARD override (seam wins).
  * Tile-cut bridge polygons removed.
  * ``_resample_node_altitudes_nn`` upgraded to edge interpolation
    (cut-edge vertices use linear gradient of the underlying old
    edge instead of nearest-neighbour).

Baseline re-cut 2026-05-20 (SPJC + SPLP) after: grade[SPLP]
runway-corner nudge, floating-orphan junction drop, and the Rule-2
sloping-edge re-snap.  Per-role floors set ~5 % below the new target
counts.

Add new airport baselines as ``tests/fixtures/<ICAO>_target.osm``
files come online.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

from conftest import xplane_available, xplane_root


_HERE = Path(__file__).resolve().parent
_TOOLS = _HERE.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))


pytestmark = [
    pytest.mark.skipif(
        not xplane_available(),
        reason="X-Plane install not found (set XPLANE_ROOT to override)",
    ),
]


# Per-role floors are set ~5 % below target counts to absorb the
# run-to-run non-determinism in node-ID assignment / sliver-drop
# ordering.  A regression that drops more than ~5 % of any role's
# shapes vs target trips the gate.
# Floors RE-CUT 2026-06-05 against fresh SPJC / SPLP target fixtures after the
# terrain-extrema cuts were turned OFF by default (the extrema cuts had been
# splitting long parallels / runway segments at terrain peaks/valleys, so
# disabling them reduced the segment count: SPJC primary_parallel 31->28,
# runway 31->30; SPLP-77 primary_parallel 11->8; SPLP-78 primary_parallel
# 8->5).  Floors = target - round(5%).  (The cut path was deleted 2026-06-28.)
# SPJC RE-CUT 2026-06-09 (session 68) for the conforming-cuts hole-router
# redesign (config.HOLE_ROUTER_V2): the Prim min-spanning-forest planner
# opens residue holes with MINIMUM chained slits instead of per-hole
# balanced cuts, so the apron residue partitions into FEWER, larger pieces
# (apron 30->21; junction 34->42 via the sliver-merge anchor veto keeping
# rect-end connector pieces separate).  Total emitted unchanged at 1312.
# SPJC RE-CUT again 2026-06-09 (later): runway-disconnected aprons →
# groundside (user rule: an apron must have a touch-chain to a runway);
# 2 terminal-curbside aprons reclassified (apron 21->19, groundside 3->5).
# SPJC RE-CUT 2026-06-10: third-party DSF pavement admission by MATERIAL
# DESCRIPTOR (config.DSF_PAVEMENT_MATERIAL_TOKENS — "asphalt"/"concrete"
# + FR/DE/ES/IT/PT equivalents, per user): the pack's CDB-Library /
# aericaps asphalt .pol polygons are now part of the pavement source,
# which re-shapes the rect/junction split (junction 42->26,
# primary_parallel 28->19, stub 18->11, apron 19->16; total 1312->1271).
# SPJC RE-CUT 2026-06-12 (s79): SERVICE_ROAD_CARVE default ON —
# apt.dat 1206 truck routes emit as ``service_road`` rects and
# legitimately CLAIM lanes the medial machinery used to discover as
# TX taxiways (TX6/TX9 secondary_parallels + TX12 stub ride SVC8's
# road now: roads beat DISCOVERED rects at the overlap pass, while
# apt.dat aircraft rows — named or unnamed — always beat roads), and
# the carve re-cuts the surrounding apron/junction residue
# (secondary_parallel 6->4, stub 11->9, junction 26->24, apron
# 16->19).
# RE-CUT 2026-06-20 against a fresh SPJC_target.osm after the seam /
# spine-slice / rect-end-cap / decompose work landed: the airside partition
# is now sliced into many more (smaller) pieces — apron 19->86, junction
# 24->180 — while boundary/runway are unchanged (1028/30).  Floors = current
# count - 5%.
#
# retaining_wall: SPJC has 4 tunnel-portal clusters (the s82 continuous-wall
# rework cfa6d33 emits ONE DEM-following wall ring per cluster — the old 68
# were per-segment/cap/fan polygons from before cfa6d33).  The NW cluster is
# a Y-fork whose offset band has TWO holes (central + crotch wedge); the
# single-hole slit left it filled-into-a-disc and the wall-vs-ramp clip
# dropped it -> only 3 emitted.  FIXED 2026-06-20 (bridges.py: slit EVERY
# hole) so all 4 clusters emit a valid hole-free wall.  Walls are
# DETERMINISTIC (DEM-driven cluster geometry), so the floor is the exact
# count — no -5% slack — to guard the fork wall against re-regression.
#
# RE-CUT 2026-07-05c: full-width service-corridor consolidation (user
# 2026-07-05 full-width corridor, O4_FULL_WIDTH_SERVICE_CORRIDOR) merges
# the half-strips flanking each truck-route spine + along-route fragment
# chains into single full-width corridor shapes — SPJC's service
# partition intentionally coarsened (service_junction 22 → 14,
# service_road 5 → 6); every other role's count unchanged.  Fixture +
# floors recut the same day, same 0.95 convention.
# RE-CUT 2026-07-05b: adaptive sparse tessellation (O4_ADAPTIVE_BEZIER —
# sagitta-capped bezier subdivision + Douglas-Peucker source-ring
# resampling) reshaped the partition again; fixtures + floors recut the
# same day.  Same 0.95 floor convention.
# RE-CUT 2026-07-05 (both SPJC and SPLP, tools/build_target_osm.py from
# repo root): the previous fixtures dated 2026-06-21, FOUR airside
# partition reshapes ago.  The curve-native / route-arc global slice is
# now the default pipeline (rects / junction_emit bypassed), so the
# rect-family roles (primary_parallel, secondary_parallel, stub,
# cross_connector) no longer exist in the partition; the boundary ribbon
# skips at-DEM rects (SPJC emits 0 boundary pieces, SPLP far fewer); SPJC
# now emits 6 retaining walls and a service_junction role.  Floors =
# int(0.95 * current fixture count) — the same "current − 5 %" convention
# as every previous re-cut — except retaining_wall (deterministic, exact).
SPJC_BASELINE: Dict[str, int] = {
    "apron":              94,   # of  99 current
    "building":           29,   # of  31 current
    "groundside_pavement": 9,   # of  10 current
    "junction":          189,   # of 199 current
    "retaining_wall":      6,   # of   6 current (one per tunnel cluster; deterministic, exact floor)
    "runway":             33,   # of  35 current
    "runway_clearance":    6,   # of   7 current
    "service_junction":   13,   # of  14 current
    "service_road":        5,   # of   6 current
    "taxiway_clearance":  18,   # of  19 current
    "tunnel_ramp":        38,   # of  41 current
}
SPJC_BASELINE_TOTAL = 443  # int(0.95 * 467) of 467 current (emitted)

# SPLP is cross-tile (spans -13/-77 and -13/-78).  Each tile-half has
# its own baseline; a regression in either half trips the gate.
# Re-cut 2026-05-29 against the SMOOTHED (apt_smoothing_pix=8) DEM — the
# surface production ships.  Previously cut with a RAW O4DEM, which adds
# terrain roughness X-Plane never renders and produced different rect
# splits (e.g. primary_parallel 5->7 here).  Floors = target - round(5%).
# Re-cut 2026-06-10: the multi-tile DSF read now loads the pack's
# -13-078.dsf as well (a cross-tile airport ships one DSF per tile; the
# anchor-tile-only read missed half the DSF pavement — the same bug that
# hid KPHX's south aprons).  The added pavement re-shapes the rect /
# junction split on both halves.
# Re-cut 2026-06-11 (-78 half only): small SYNTHESIZED strips (TX#/P#)
# isolated on a groundside island now ride the island into groundside
# (user auto-correct ruling; KOQN TX10's dangling short-edge class) —
# SPLP's TX53/TX54 (437/1,994 m², on the landside parking island whose
# two big aprons were ALREADY groundside in the previous target) moved
# secondary_parallel → groundside_pavement.  Same total shape count.
# RE-CUT 2026-07-05 (curve-native global slice default; see the SPJC
# re-cut note above) — floors = int(0.95 * current fixture count).
# RE-CUT 2026-07-06 (seam values sample the SMOOTHED DEM per the user
# ruling — runway seam anchors moved cm-scale, flipping six boundary
# ribbon at-DEM skips).  Floors = int(0.95 * current fixture count).
SPLP_BASELINE_TILE_M77: Dict[str, int] = {
    "apron":               9,   # of  10 current
    "boundary":           31,   # of  33 current
    "building":            2,   # of   3 current
    "junction":           11,   # of  12 current
    "runway":              8,   # of   9 current
    "runway_clearance":    5,   # of   6 current
    "taxiway_clearance":   8,   # of   9 current
}
SPLP_BASELINE_TILE_M77_TOTAL = 77  # int(0.95 * 82) of 82 current (emitted)

# RE-CUT 2026-07-05 (curve-native global slice default; see the SPJC
# re-cut note above) — floors = int(0.95 * current fixture count).
SPLP_BASELINE_TILE_M78: Dict[str, int] = {
    "apron":              36,   # of  38 current
    "boundary":           17,   # of  18 current
    "building":            7,   # of   8 current
    "groundside_pavement": 2,   # of   3 current
    "junction":           15,   # of  16 current
    "runway":              7,   # of   8 current
    "runway_clearance":    0,   # of   1 current
    "taxiway_clearance":  17,   # of  18 current
}
SPLP_BASELINE_TILE_M78_TOTAL = 104  # int(0.95 * 110) of 110 current (emitted)


def _build_layout(icao: str, tile_lat=None, tile_lon=None):
    # Shared session cache (conftest) — built once per (airport, tile)
    # per run; the DEM is constructed inside the cache from tile_lat/lon.
    from conftest import cached_airport_layout
    return cached_airport_layout(
        icao, tile_lat=tile_lat, tile_lon=tile_lon)


def _run_compare(tmp_path: Path, icao: str,
                 baseline: Dict[str, int],
                 baseline_total: int,
                 target_path: Optional[Path] = None,
                 tile_lat: Optional[int] = None,
                 tile_lon: Optional[int] = None) -> None:
    import compare_target as CT

    if target_path is None:
        target_path = _HERE / "fixtures" / f"{icao}_target.osm"
    assert target_path.is_file(), (
        f"{icao} target fixture missing at {target_path}")

    layout = _build_layout(icao, tile_lat=tile_lat, tile_lon=tile_lon)
    suffix = (f"_tile{tile_lat:+d}{tile_lon:+d}"
              if tile_lat is not None else "")
    out_path = tmp_path / f"{icao}{suffix}_out.osm"
    layout.to_osm(str(out_path))

    anchor = CT.pick_anchor(target_path)
    target_shapes = CT.load_shapes(target_path, anchor, "target")
    output_shapes = CT.load_shapes(out_path, anchor, "output")
    pairs = CT.match_by_role(target_shapes, output_shapes)

    matched_by_role: Dict[str, int] = {}
    for p in pairs:
        if p.target is None or p.output is None:
            continue
        if p.iou <= 0.0:
            continue
        role = p.target.role
        matched_by_role[role] = matched_by_role.get(role, 0) + 1

    target_counts: Dict[str, int] = {}
    for s in target_shapes:
        target_counts[s.role] = target_counts.get(s.role, 0) + 1
    output_counts: Dict[str, int] = {}
    for s in output_shapes:
        output_counts[s.role] = output_counts.get(s.role, 0) + 1

    summary_lines = []
    for role in sorted(set(target_counts) | set(output_counts)):
        n_t = target_counts.get(role, 0)
        n_o = output_counts.get(role, 0)
        n_m = matched_by_role.get(role, 0)
        floor = baseline.get(role)
        floor_str = f" (floor {floor})" if floor is not None else ""
        summary_lines.append(
            f"  {role:20s} target={n_t:3d}  out={n_o:3d}  "
            f"matched={n_m:3d}{floor_str}")
    summary = "\n".join(summary_lines)

    failures = []
    for role, floor in baseline.items():
        n_m = matched_by_role.get(role, 0)
        if n_m < floor:
            failures.append(
                f"{role}: matched={n_m} < floor={floor}")
    total_matched = sum(matched_by_role.values())
    if total_matched < baseline_total:
        failures.append(
            f"total: matched={total_matched} < "
            f"floor={baseline_total}")

    assert not failures, (
        f"{icao} structural-fidelity regression vs target:\n"
        f"  failures: {'; '.join(failures)}\n"
        f"  per-role detail:\n{summary}")


@pytest.mark.xdist_group("SPJC")
def test_compare_target_spjc(tmp_path):
    """SPJC structural fidelity vs ``tests/fixtures/SPJC_target.osm``.

    See ``SPJC_BASELINE`` for current per-role floors.
    """
    _run_compare(tmp_path, "SPJC",
                 SPJC_BASELINE, SPJC_BASELINE_TOTAL)


@pytest.mark.xdist_group("SPLP")
@pytest.mark.parametrize("tile_lat,tile_lon,baseline,baseline_total", [
    (-13, -77, SPLP_BASELINE_TILE_M77, SPLP_BASELINE_TILE_M77_TOTAL),
    (-13, -78, SPLP_BASELINE_TILE_M78, SPLP_BASELINE_TILE_M78_TOTAL),
])
def test_compare_target_splp(tmp_path, tile_lat, tile_lon,
                              baseline, baseline_total):
    """SPLP structural fidelity, validated per tile half.

    SPLP is a cross-tile airport (spans -13/-77 and -13/-78); each
    tile build emits a different subset of pavement after the
    tile-boundary cut.  The fixtures ``SPLP_target_tile-13-77.osm``
    and ``SPLP_target_tile-13-78.osm`` are the canonical outputs for
    each half; a regression in EITHER half trips this gate.
    """
    target_path = (_HERE / "fixtures"
                   / f"SPLP_target_tile{tile_lat:+d}{tile_lon:+d}.osm")
    _run_compare(tmp_path, "SPLP", baseline, baseline_total,
                 target_path=target_path,
                 tile_lat=tile_lat, tile_lon=tile_lon)

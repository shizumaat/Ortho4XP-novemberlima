"""Structural-fidelity gates against the reference fixture outputs.

``tests/fixtures/SPJC_target.osm`` and ``tests/fixtures/SPLP_target.osm``
are the canonical builds (regenerated 2026-05-13 with the seam-anchor
+ diagonal-stub trapezoid pipeline).  Every code change must continue
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
# terrain-extrema cuts were turned OFF by default (config.SPLIT_LONG_RECTS_ENABLED
# now defaults off): the extrema cuts had been splitting long parallels / runway
# segments at terrain peaks/valleys, so disabling them reduces the segment count
# (SPJC primary_parallel 31->28, runway 31->30; SPLP-77 primary_parallel 11->8;
# SPLP-78 primary_parallel 8->5).  Floors = target - round(5%).
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
SPJC_BASELINE: Dict[str, int] = {
    "apron":              18,   # of  19 target
    "boundary":          977,   # of 1028 target
    "cross_connector":     8,   # of   8 target
    "junction":           23,   # of  24 target
    "primary_parallel":   18,   # of  19 target
    "retaining_wall":     65,   # of  68 target
    "runway":             28,   # of  30 target
    "secondary_parallel":  4,   # of   4 target
    "service_road":        6,   # of   6 target
    "stub":                9,   # of   9 target
    "building":            2,   # of   2 target (role renamed
                                #   from "terminal" 2026-06-12;
                                #   loader normalizes legacy tags)
    "tunnel_ramp":        34,   # of  36 target
}
SPJC_BASELINE_TOTAL = 1192  # of 1263 target (emitted)

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
SPLP_BASELINE_TILE_M77: Dict[str, int] = {
    "apron":               6,   # of   7 target (1 apron is invalid-dropped at
                                #   emit; allow ±1 for that nondeterminism)
    "boundary":          200,   # of 211 target
    "cross_connector":     1,   # of   1 target
    "junction":            3,   # of   3 target
    "primary_parallel":    2,   # of   2 target
    "runway":              8,   # of   8 target
    "stub":                3,   # of   3 target
}
SPLP_BASELINE_TILE_M77_TOTAL = 230  # of 242 target (emitted)

SPLP_BASELINE_TILE_M78: Dict[str, int] = {
    "apron":              19,   # of  20 target (±1 emit nondeterminism)
    "boundary":          281,   # of 296 target
    "cross_connector":     1,   # of   1 target
    "junction":            9,   # of   9 target
    "primary_parallel":    4,   # of   4 target
    "runway":              8,   # of   8 target
    "secondary_parallel":  2,   # of   2 target (TX53/54 → groundside,
                                #   re-cut 2026-06-11)
    "stub":               12,   # of  12 target
    "building":            1,   # of   1 target (legacy
                                #   "terminal" -- see above)
}
SPLP_BASELINE_TILE_M78_TOTAL = 345  # of 365 target


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

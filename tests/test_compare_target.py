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


pytestmark = pytest.mark.skipif(
    not xplane_available(),
    reason="X-Plane install not found (set XPLANE_ROOT to override)",
)


# Per-role floors are set ~5 % below target counts to absorb the
# run-to-run non-determinism in node-ID assignment / sliver-drop
# ordering.  A regression that drops more than ~5 % of any role's
# shapes vs target trips the gate.
# Floors refreshed 2026-05-31 against the re-cut SPJC / SPLP target
# fixtures.  Latest: the session-57 centerline-quality commits
# (93eab1d bend-trim, c51fa12 off-corridor drop / bend-hook / runway
# centering, 02677a3 through-taxiway corridor trim, d46034e
# cross-connector end-margin cap) shifted the centerline segmentation,
# changing junction/apron/stub decomposition (SPJC apron 41->32,
# primary_parallel 34->31; SPLP-77 junction 4->3 / stub 3->4; SPLP-78
# secondary_parallel 7->0 reclassified).  Floors = target - round(5%).
SPJC_BASELINE: Dict[str, int] = {
    "apron":              30,   # of  32 target
    "boundary":          977,   # of 1028 target
    "cross_connector":     8,   # of   8 target
    "junction":           31,   # of  33 target
    "primary_parallel":   29,   # of  31 target
    "retaining_wall":     65,   # of  68 target
    "runway":             29,   # of  31 target
    "secondary_parallel":  6,   # of   6 target
    "stub":               17,   # of  18 target
    "terminal":            2,   # of   2 target
    "tunnel_ramp":        34,   # of  36 target
}
SPJC_BASELINE_TOTAL = 1250  # of 1316 target

# SPLP is cross-tile (spans -13/-77 and -13/-78).  Each tile-half has
# its own baseline; a regression in either half trips the gate.
# Re-cut 2026-05-29 against the SMOOTHED (apt_smoothing_pix=8) DEM — the
# surface production ships.  Previously cut with a RAW O4DEM, which adds
# terrain roughness X-Plane never renders and produced different rect
# splits (e.g. primary_parallel 5->7 here).  Floors = target - round(5%).
SPLP_BASELINE_TILE_M77: Dict[str, int] = {
    "apron":              10,   # of  11 target
    "boundary":          200,   # of 211 target
    "junction":            3,   # of   3 target
    "primary_parallel":   10,   # of  11 target
    "runway":              8,   # of   8 target
    "stub":                4,   # of   4 target
}
SPLP_BASELINE_TILE_M77_TOTAL = 241  # of 254 target

SPLP_BASELINE_TILE_M78: Dict[str, int] = {
    "apron":               3,   # of   3 target
    "boundary":          281,   # of 296 target
    "cross_connector":     1,   # of   1 target
    "junction":            8,   # of   8 target
    "primary_parallel":    8,   # of   8 target
    "runway":              8,   # of   8 target
    "stub":                4,   # of   4 target
    "terminal":            1,   # of   1 target
}
SPLP_BASELINE_TILE_M78_TOTAL = 320  # of 337 target


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

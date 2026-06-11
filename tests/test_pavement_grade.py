"""End-to-end grade validation for the pavement builder.

Skipped automatically unless an X-Plane install is available (the
builder needs CIFP + DEM tiles).  When run, it builds SPJC + SPLP,
writes the output OSM, then invokes ``tools.check_grade.run_checks``
to assert:

* No cross-shape proximity violations (shared corners agree on elev).
* No vertex-to-edge steps > 0.5 m (no visible drops between
  adjacent shapes — the user-reported "1 m drop" regression).
* Within-shape grade violations stay below a soft cap (the long-
  thin-apron-triangle case is a known limitation documented for
  follow-up; this test guards against new regressions).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import baseline_airports, airports_under_test

_HERE = Path(__file__).resolve().parent
_TOOLS = _HERE.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))


def _xplane_root() -> str:
    return os.environ.get("XPLANE_ROOT", "/Users/noah/X-Plane 12")


def _xplane_available() -> bool:
    root = _xplane_root()
    return (Path(root).is_dir()
            and (Path(root) / "Custom Data" / "CIFP").is_dir())


pytestmark = pytest.mark.skipif(
    not _xplane_available(),
    reason="X-Plane install not found (set XPLANE_ROOT to override)",
)


# Grade thresholds are UNIVERSAL and ZERO — no per-airport caps, no
# baselines, no soft exceptions (user 2026-05-31).  A within-shape
# vertex pair > 1.5 %, a cross-shape elevation disagreement, or a
# mid-edge step > 0.5 m means the elevation solver / source geometry
# produced a non-compliant surface; the fix is there, not in the
# threshold.  Any airport with residual violations FAILS until fixed.
WITHIN_SHAPE_CAP = 0
MID_EDGE_CAP = 0


def _airport_tiles(icao: str, root: str):
    """Integer ``(lat, lon)`` tiles the airport's pavement occupies.

    A cheap geometry-only build (``compute_elevations=False`` skips the
    elevation / runway-segmenter / seam / tile_cut pipeline) gives the
    footprint without paying for the full build.  The grade audit then
    builds each of these tiles the way Ortho4XP SHIPS them — per-tile,
    with that tile's DEM and ``current_tile_lat/lon`` — so we grade the
    final cut-and-adjusted geometry, not the pre-cut whole-airport
    superset (user 2026-05-23).
    """
    import math
    # Shared session cache (conftest) — geometry-only footprint build.
    from conftest import cached_airport_layout
    layout = cached_airport_layout(icao, compute_elevations=False)
    lats: list = []
    lons: list = []
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        for (x, y) in s.polygon.exterior.coords:
            lat, lon = layout.m_to_ll(x, y)
            lats.append(lat)
            lons.append(lon)
    if not lats:
        return []
    tiles = []
    for la in range(int(math.floor(min(lats))),
                    int(math.floor(max(lats))) + 1):
        for lo in range(int(math.floor(min(lons))),
                        int(math.floor(max(lons))) + 1):
            tiles.append((la, lo))
    return tiles


# HECA is grade-checked here even though it is NOT in the global
# ``baseline_airports()`` invariant set (adding it there would pull HECA into
# every geometry invariant at once).  Scoping it to the GRADE gate makes the
# real within-shape / cross-shape violations the Ortho4XP-window WARN already
# reports become CI-visible — closing the runtime-vs-test gap where HECA's
# grade was never asserted (terminal-8 apron ramp, steep stub/cross_connector
# rects, a shared-corner step).  This gate is RED until the solver pulls
# apron-bridged terminals to a grade-compatible level (see the T8 investigation
# in docs/presolve_geometry_refactor.md / memory); it turns GREEN when the
# violations are fixed, proving the fix.
_GRADE_TEST_AIRPORTS = sorted(
    set(baseline_airports()) | {"HECA"} | set(airports_under_test()))


@pytest.mark.parametrize("icao", _GRADE_TEST_AIRPORTS)
def test_pavement_grade(tmp_path, icao):
    from auto_patch.elevation_per_surface import unified_jacobi as _uj
    import check_grade

    tiles = _airport_tiles(icao, _xplane_root())
    assert tiles, f"{icao}: no pavement footprint tiles discovered"

    # Audit each shipped per-tile patch; aggregate violations.
    within: list = []
    cross: list = []
    steps: list = []
    # All builds go through the shared cache (conftest.cached_airport_layout),
    # which uses the SMOOTHED (apt_smoothing_pix=8) DEM production ships.
    # Single-tile airport: no integer line crosses the footprint, so
    # tile_cut is a no-op and the per-tile build is bit-identical to the
    # whole-airport cached layout — reuse it directly (no current_tile).
    # Multi-tile (e.g. SPLP): build per tile, but via the SAME cache key
    # (icao, tile) the compare_target / tile_cut tests use, so the tile is
    # built ONCE per run instead of once per consuming test.
    from conftest import cached_airport_layout
    single_tile = len(tiles) == 1
    for (tlat, tlon) in tiles:
        if single_tile:
            layout = cached_airport_layout(icao)
        else:
            layout = cached_airport_layout(
                icao, tile_lat=tlat, tile_lon=tlon)
        if not layout.shapes:
            continue  # airport doesn't reach this corner tile
        out = tmp_path / f"{icao}_tile{tlat:+d}{tlon:+d}.osm"
        layout.to_osm(str(out))

        # When the solver grades junctions PER-AXIS, the audit must
        # match — using the SAME apt.dat centerlines the build used
        # (layout.apt_taxi_centerlines), passed as lat/lon so the
        # audit's mean-centred meter frame lines up.  NEVER re-derive
        # from the OSM.
        taxi_axes_ll = None
        if getattr(_uj, "_PER_AXIS_JUNCTIONS", False):
            letters = getattr(layout, "apt_taxi_letters", {}) or {}
            taxi_axes_ll = []
            for ln, name in (
                    getattr(layout, "apt_taxi_centerlines", []) or []):
                if ln is None or ln.is_empty:
                    continue
                letter = letters.get(name)
                cL = 0.03 if letter in ("A", "B") else 0.015
                cT = 0.02 if letter in ("A", "B") else 0.015
                pts = [layout.m_to_ll(x, y) for (x, y) in ln.coords]
                taxi_axes_ll.append((pts, cL, cT))

        # ROUTE-FIELD MODEL: the long-range law is the route-band check —
        # pass the builder's centerlines (the within-shape pairs above the
        # local window are no longer graded; the route bands replace them).
        from auto_patch.verification import route_ctx_from_layout
        route_ctx = route_ctx_from_layout(layout)

        w, c, s = check_grade.run_checks(
            out,
            max_grade_pct=1.5,
            proximity_m=1.0,
            edge_search_m=5.0,
            edge_step_m=0.5,
            top_n=5,
            taxi_axes_ll=taxi_axes_ll,
            route_ctx=route_ctx,
        )
        within += w
        cross += c
        steps += s

    # Hard fails — cross-shape continuity must be perfect.
    assert not cross, (
        f"{icao}: {len(cross)} cross-shape proximity violations "
        f"(shared corners disagree on elevation).  Worst: "
        f"{max(v.de_m for v in cross):.2f} m step.")
    # Soft cap on vertex-to-edge + mid-edge steps combined.  Vertex
    # continuity at shared boundaries should be ~perfect; mid-edge
    # discontinuities (sliver triangles whose plane tilts away from
    # neighbouring triangles' surfaces) are the known background.
    step_cap = MID_EDGE_CAP
    assert len(steps) <= step_cap, (
        f"{icao}: {len(steps)} edge/mid-edge steps > 0.5 m exceeds "
        f"cap {step_cap}.  Worst: {max(s.step_m for s in steps):.2f} "
        f"m step.")
    # Hard fail — within-shape grade violations indicate an
    # infeasible elevation field; fix the solver / geometry, not
    # the threshold.
    cap = WITHIN_SHAPE_CAP
    if len(within) > cap:
        within.sort(key=lambda v: -v.grade_pct)
        worst = "\n  ".join(
            f"{check_grade._label(v.way_a)} -> "
            f"{check_grade._label(v.way_b)}: {v.grade_pct:.2f}% over "
            f"{v.distance_m:.1f} m ({v.elev_a:.1f} -> {v.elev_b:.1f})"
            for v in within[:5])
        pytest.fail(
            f"{icao}: {len(within)} within-shape grade/plane "
            f"violations (cap {cap}).  Worst:\n  {worst}")


def _fmt_rwy(vios) -> str:
    out = []
    for kind, ref, val, cap, ll in vios[:6]:
        if kind == "grade":
            out.append(f"{ref}: {val * 100:.2f}% > {cap * 100:.2f}% @ {ll}")
        else:
            out.append(
                f"{ref}: |Δg|={val:.5f}/m > {cap:.5f}/m @ {ll} (kink)")
    return "\n  ".join(out)


@pytest.mark.parametrize("icao", _GRADE_TEST_AIRPORTS)
def test_runway_longitudinal_grade(icao):
    """The emitted runway centerline profile must not exceed the uniform
    ``RUNWAY_MAX_GRADE`` (1.5%) longitudinal cap anywhere — the binding
    longitudinal limit the runway solver + runway-flex enforce today.  Guards
    against a runway-flex MOVE (or any solver change) pulling a runway steeper
    than 1.5% along its axis.  Reconstructs the profile from the whole-airport
    layout (a runway is continuous; not per-tile)."""
    from conftest import cached_airport_layout
    from auto_patch.verification import check_runway_profile

    layout = cached_airport_layout(icao)
    if not any((s.role or "") == "runway" for s in layout.shapes):
        pytest.skip(f"{icao}: no runway shapes")
    vios = check_runway_profile(
        layout, end_grade_cap=None, check_curvature=False)
    assert not vios, (
        f"{icao}: {len(vios)} runway longitudinal-grade violation(s) "
        f"> {1.5:.1f}%.  Worst:\n  {_fmt_rwy(vios)}")


@pytest.mark.xfail(
    reason="runway vertical-curve smoothing (STATUS item D) not built + the "
           "0.8% end-grade cap is opt-in, so runways emit as plane rects with "
           "sharp kinks at extrema and 1.5% end zones — RED until those land; "
           "flips to XPASS when a runway becomes fully compliant.",
    strict=False)
@pytest.mark.parametrize("icao", _GRADE_TEST_AIRPORTS)
def test_runway_vertical_curve(icao):
    """The emitted runway profile must also obey the EASA 0.8% end-grade cap and
    the FAA vertical-curve rate-of-grade-change limit
    (``RUNWAY_MAX_GRADE_CHANGE_PER_M``).  This is the runway counterpart of the
    taxiway vertical-curve smoothing (STATUS item D): currently RED on every
    airport because plane rects meet at sharp kinks at terrain extrema.  Kept as
    an ``xfail`` tracking target — it turns XPASS per airport as the smoothing /
    end-grade enforcement lands."""
    from conftest import cached_airport_layout
    from auto_patch.verification import check_runway_profile

    layout = cached_airport_layout(icao)
    if not any((s.role or "") == "runway" for s in layout.shapes):
        pytest.skip(f"{icao}: no runway shapes")
    vios = check_runway_profile(layout)
    assert not vios, (
        f"{icao}: {len(vios)} runway end-grade/vertical-curve violation(s).  "
        f"Worst:\n  {_fmt_rwy(vios)}")

"""Runway-end skirt AIRSIDE PRECEDENCE over groundside pavement.

Noah ruling 2026-07-10 (docs/slice_b_solver_absorption_design.md): the
runway-end skirt area is inherently AIRSIDE — nothing there can
legitimately be groundside.  A skirt must NEVER clip its footprint
against groundside pavement; where the two approach, GROUNDSIDE is
trimmed AROUND the skirt (exact footprint, shared chain verbatim, no
buffer gap).  Buildings keep their existing precedence — the skirt still
yields to a building (unchanged, and exercised by the full-build gates,
not here).

These are unit-scale tests of the trim helper
``clearance._trim_groundside_pavement_around_skirts`` — the one piece of
new geometry logic — with a hand-built layout, so they run in
milliseconds and do not need a DEM or a solved airport.
"""
from __future__ import annotations

from shapely.geometry import Polygon

from auto_patch.clearance import _trim_groundside_pavement_around_skirts
from auto_patch.layout import BuiltShape, PavementLayout


def _sloped_groundside(minx, miny, maxx, maxy, *, base=100.0, slope=0.02):
    """A rectangular groundside_pavement shape whose node_altitudes follow
    a DEM gradient ``base + slope * x`` at every ring vertex (closed)."""
    poly = Polygon([(minx, miny), (maxx, miny),
                    (maxx, maxy), (minx, maxy)])
    alts = [base + slope * x for x, _y in poly.exterior.coords]
    return BuiltShape(polygon=poly, role="groundside_pavement",
                      ref="gs", node_altitudes=alts)


def _groundside(layout):
    return [s for s in layout.shapes if s.role == "groundside_pavement"]


class TestSkirtAirsidePrecedence:
    def test_skirt_footprint_not_reduced(self):
        """The skirt shape is untouched — the helper only trims
        groundside; the skirt keeps its full footprint."""
        skirt_poly = Polygon([(80.0, -20.0), (140.0, -20.0),
                              (140.0, 120.0), (80.0, 120.0)])
        gs = _sloped_groundside(0.0, 0.0, 100.0, 100.0)
        skirt = BuiltShape(polygon=skirt_poly, role="runway_clearance",
                           ref="runway_end_skirt",
                           node_altitudes=[50.0] * 5)
        layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
        layout.shapes.extend([gs, skirt])

        _trim_groundside_pavement_around_skirts(layout, skirt_poly)

        survivors = [s for s in layout.shapes
                     if s.ref == "runway_end_skirt"]
        assert len(survivors) == 1
        assert survivors[0].polygon.equals(skirt_poly), \
            "the skirt footprint must NOT be reduced by the trim"

    def test_groundside_trimmed_with_skirt_chain_verbatim(self):
        """Groundside is cut to ``gs.difference(skirt)``; the skirt's
        boundary coordinates appear VERBATIM in the trimmed groundside
        ring (zero minted near-parallel geometry)."""
        skirt_poly = Polygon([(80.0, -20.0), (140.0, -20.0),
                              (140.0, 120.0), (80.0, 120.0)])
        gs = _sloped_groundside(0.0, 0.0, 100.0, 100.0)
        layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
        layout.shapes.append(gs)

        n_trim, n_drop = _trim_groundside_pavement_around_skirts(
            layout, skirt_poly)
        assert (n_trim, n_drop) == (1, 0)

        remnants = _groundside(layout)
        assert len(remnants) == 1
        trimmed = remnants[0]
        # Exact difference geometry (area 0..80 x 0..100 = 8000 m²).
        assert trimmed.polygon.equals(
            gs.polygon.difference(skirt_poly))
        assert abs(trimmed.polygon.area - 8000.0) < 1e-6

        # The shared chain vertices (skirt boundary ∩ groundside) appear
        # verbatim in the trimmed ring — no offset, no buffer gap.
        ring = set(trimmed.polygon.exterior.coords)
        for shared in ((80.0, 0.0), (80.0, 100.0)):
            assert any(abs(vx - shared[0]) < 1e-9
                       and abs(vy - shared[1]) < 1e-9
                       for vx, vy in ring), \
                f"shared skirt-chain vertex {shared} missing from remnant"

    def test_trimmed_ring_altitudes_follow_groundside_semantics(self):
        """Trimmed-ring node altitudes are re-derived from the groundside
        field (DEM gradient ``100 + 0.02·x``), NOT copied from the skirt.
        Shared-chain vertices sit on original groundside edges, so the
        resample edge-interpolates the groundside value there."""
        skirt_poly = Polygon([(80.0, -20.0), (140.0, -20.0),
                              (140.0, 120.0), (80.0, 120.0)])
        gs = _sloped_groundside(0.0, 0.0, 100.0, 100.0,
                                base=100.0, slope=0.02)
        layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
        layout.shapes.append(gs)

        _trim_groundside_pavement_around_skirts(layout, skirt_poly)
        trimmed = _groundside(layout)[0]

        assert trimmed.node_altitudes is not None
        coords = list(trimmed.polygon.exterior.coords)
        assert len(trimmed.node_altitudes) == len(coords)
        for (vx, _vy), alt in zip(coords, trimmed.node_altitudes):
            # Every vertex of the remnant lies on an original groundside
            # edge (the difference preserved the bottom/top/left edges and
            # the shared chain is the vertical x=80 line that meets the
            # unchanged y=0 / y=100 edges), so the groundside field value
            # is 100 + 0.02·x — never the skirt's 50 m.
            assert abs(alt - (100.0 + 0.02 * vx)) < 0.05, \
                f"vertex ({vx}) altitude {alt} does not follow groundside"

    def test_split_groundside_yields_two_remnants(self):
        """A skirt band through the middle SPLITS groundside; both
        surviving parts are kept as groundside with resampled altitudes."""
        skirt_poly = Polygon([(40.0, -20.0), (60.0, -20.0),
                              (60.0, 120.0), (40.0, 120.0)])
        gs = _sloped_groundside(0.0, 0.0, 100.0, 100.0)
        layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
        layout.shapes.append(gs)

        n_trim, n_drop = _trim_groundside_pavement_around_skirts(
            layout, skirt_poly)
        assert (n_trim, n_drop) == (1, 0)
        remnants = _groundside(layout)
        assert len(remnants) == 2
        for r in remnants:
            assert r.node_altitudes is not None
            assert r.role == "groundside_pavement"
            assert abs(r.polygon.area - 4000.0) < 1e-6

    def test_subresidue_remnant_dropped_whole(self):
        """A groundside shape reduced below 50 m² by the skirt is dropped
        entirely — no sub-50 m² sliver welded onto the skirt chain."""
        gs = _sloped_groundside(0.0, 0.0, 100.0, 6.0)   # 600 m² total
        # Skirt leaves only 0..100 x 0..0.4 = 40 m² < 50.
        skirt_poly = Polygon([(-20.0, 0.4), (120.0, 0.4),
                              (120.0, 20.0), (-20.0, 20.0)])
        layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
        layout.shapes.append(gs)

        n_trim, n_drop = _trim_groundside_pavement_around_skirts(
            layout, skirt_poly)
        assert (n_trim, n_drop) == (0, 1)
        assert _groundside(layout) == []

    def test_no_overlap_is_inert(self):
        """A skirt that does not touch groundside changes nothing (the
        CYXY/KCLT/HECA production case — byte-inert)."""
        gs = _sloped_groundside(0.0, 0.0, 100.0, 100.0)
        skirt_poly = Polygon([(500.0, 500.0), (560.0, 500.0),
                             (560.0, 560.0), (500.0, 560.0)])
        layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
        layout.shapes.append(gs)
        before = gs.polygon

        n_trim, n_drop = _trim_groundside_pavement_around_skirts(
            layout, skirt_poly)
        assert (n_trim, n_drop) == (0, 0)
        remnants = _groundside(layout)
        assert len(remnants) == 1
        assert remnants[0].polygon is before, \
            "no-overlap trim must leave the groundside shape identical"

"""Unit tests for the formation-time SOURCE-CLIP pass
(``junction_repair.source_clip_partial_coverage_shapes``, Fix C).

Pure synthetic geometry — no X-Plane install, no airport build.  A
partial-coverage (< 50 % on source) apron/junction is clipped back to the
recorded source pavement (∪ runway halo); a well-covered (≥ 50 %) shape is
left untouched; and the gate (``config.SOURCE_CLIP_PARTIAL_COVERAGE``) is
byte-inert when off.
"""
from __future__ import annotations

from shapely.geometry import Polygon

# Import cycle gotcha (junction_repair <-> elevation): import through
# auto_patch.pipeline FIRST so the modules initialise in the right order
# (src/auto_patch/CLAUDE.md).
import auto_patch.pipeline  # noqa: F401
import auto_patch.config as config
from auto_patch.junction_repair import source_clip_partial_coverage_shapes
from auto_patch.layout import (
    PavementLayout, BuiltShape, ROLE_APRON, ROLE_JUNCTION,
)

# Source pavement is the unit-scaled square x,y in [0, 100].
_SOURCE = Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)])


def _layout() -> PavementLayout:
    """A 30 %-on-source junction, a 60 %-on-source apron, and a source union.

    * junction spans x in [70, 170] (10 000 m²); 3 000 m² (30 %) is on source.
    * apron spans x in [40, 140] (10 000 m²); 6 000 m² (60 %) is on source.
    """
    junction = BuiltShape(
        polygon=Polygon([(70.0, 0.0), (170.0, 0.0),
                         (170.0, 100.0), (70.0, 100.0)]),
        role=ROLE_JUNCTION, ref="J")
    apron = BuiltShape(
        polygon=Polygon([(40.0, 0.0), (140.0, 0.0),
                         (140.0, 100.0), (40.0, 100.0)]),
        role=ROLE_APRON, ref="A")
    layout = PavementLayout(icao="TEST", anchor=(35.22, -80.94),
                            shapes=[junction, apron])
    layout.source_pavement_union = _SOURCE
    layout.runway_union = None
    return layout


def test_partial_coverage_shape_is_clipped_to_source():
    layout = _layout()
    # A pre-existing (stale) node_altitudes list must be cleared by the clip.
    layout.shapes[0].node_altitudes = [10.0, 10.0, 10.0, 10.0, 10.0]

    n = source_clip_partial_coverage_shapes(layout, icao="TEST")
    assert n == 1, "exactly the 30 %-on-source junction should clip"

    # The junction is clipped back to (source ∪ runway) buffered by the
    # runway-frontage halo (3 m): x in [70, 103], ~3 300 m², and now mostly
    # on source.
    junction = layout.shapes[0]
    on = junction.polygon.intersection(_SOURCE).area
    frac = on / junction.polygon.area
    assert junction.polygon.area < 4000.0, junction.polygon.area
    assert frac > 0.85, f"clipped junction should be near-fully on source: {frac}"
    assert junction.polygon.bounds[2] <= 103.0 + 1e-6, junction.polygon.bounds
    # node_altitudes cleared (geometry changed; the solver reassigns).
    assert junction.node_altitudes is None


def test_off_source_remainder_is_dropped_not_re_minted():
    layout = _layout()
    source_clip_partial_coverage_shapes(layout, icao="TEST")
    # The clipped-away off-source remainder (x in [103, 170], RESA-grass
    # analogue) must NOT survive as any shape — dropping it uncovers no real
    # source, so nothing covers a deep-off-source probe point.
    remainder_pt = (150.0, 50.0)
    from shapely.geometry import Point
    p = Point(*remainder_pt)
    assert not any(s.polygon is not None and s.polygon.contains(p)
                   for s in layout.shapes), \
        "off-source remainder was re-minted instead of dropped"


def test_well_covered_shape_is_untouched():
    layout = _layout()
    apron_before = layout.shapes[1].polygon
    source_clip_partial_coverage_shapes(layout, icao="TEST")
    # The 60 %-on-source apron is above the 0.5 threshold → not a candidate.
    apron_after = next(s for s in layout.shapes if s.ref == "A")
    assert apron_after.polygon.equals(apron_before), \
        "a >= 50 %-on-source shape must not be clipped"


def test_gate_off_is_inert(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_CLIP_PARTIAL_COVERAGE", False)
    layout = _layout()
    before = [s.polygon for s in layout.shapes]
    n = source_clip_partial_coverage_shapes(layout, icao="TEST")
    assert n == 0
    after = [s.polygon for s in layout.shapes]
    assert len(after) == len(before)
    assert all(a.equals(b) for a, b in zip(after, before)), \
        "gate-off must leave every shape untouched"

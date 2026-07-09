"""Placement-reader tests for the W-R1 workstream of
``docs/object_terrain_features_spec.md`` (§5 row W-R1).

Focused on the DSF object-placement reader's three row kinds — plain
``OBJECT``, ``OBJECT_AGL`` (amendment A18) and the newly readable
``OBJECT_MSL`` — and the column-layout trap that shifts the heading one
token right when an elevation column is present.  A new file rather than
an edit to ``tests/test_obj8_reader.py`` (which already owns placement
tests) because W-R1 owns this path exclusively; the pre-existing MSL-skip
and AGL-accept cases there are left untouched and must stay green (they
assert the default, opt-out behaviour this change preserves).

Both the production reader (``auto_patch.obj8_reader``) and its prototype
parity copy (``tools/obj8_geometry.py``) are exercised through the same
cases so the two cannot drift.  Fixtures are synthetic DSFTool-text
snippets generated inline (ruling R6): no third-party pack content enters
the repository.  The column layout the snippets encode was verified on
2026-07-09 against DSFTool dumps of EGLL (TaiModels) and KBNA (Nimbus).
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
_TOOLS = os.path.normpath(os.path.join(_HERE, "..", "tools"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _TOOLS not in sys.path:
    sys.path.append(_TOOLS)

from auto_patch import obj8_reader as production_reader  # noqa: E402
import obj8_geometry as prototype_reader  # noqa: E402

# The two modules under test share this reader; every case runs against
# both so the production/prototype parity cannot silently break.
READERS = [
    pytest.param(production_reader, id="production"),
    pytest.param(prototype_reader, id="prototype"),
]

# One OBJECT_DEF per kind, then one row of each placement kind.  Column
# layout (verified against DSFTool dumps 2026-07-09):
#
#   OBJECT     <def> <lon> <lat>              <heading>
#   OBJECT_AGL <def> <lon> <lat> <agl_offset> <heading>
#   OBJECT_MSL <def> <lon> <lat> <msl_elev>   <heading>
#
# The heading value 111.0 is REPEATED on every row so a test can prove
# the reader took it from the correct (shifted) column: reading token 4
# on an AGL/MSL row would instead yield the elevation.
SYNTHETIC_DSF_TEXT_LINES = [
    "PROPERTY sim/west -87\n",
    "OBJECT_DEF Airport/Tunnel/2.obj\n",             # def 0
    "OBJECT_DEF Airport/Tunnel/2a.obj\n",            # def 1
    "OBJECT_DEF Airport/Bridge/deck_fixture.obj\n",  # def 2
    "OBJECT 0 -86.700000000 36.100000000 111.0\n",
    "OBJECT_AGL 1 -86.710000000 36.110000000 5.5 111.0\n",     # above grade
    "OBJECT_AGL 1 -86.720000000 36.120000000 -7.5 111.0\n",    # below grade
    "OBJECT_MSL 2 -86.730000000 36.130000000 166.9994 111.0\n",
]

# ``\t`` separators as XPlane2Blender / DSFTool sometimes emit them
# (invariant I-17: split on whitespace, never fixed columns).
TAB_SEPARATED_DSF_TEXT_LINES = [
    "OBJECT_DEF\tAirport/Tunnel/2a.obj\n",
    "OBJECT_AGL\t0\t-86.7\t36.1\t-1.0\t111.0\n",
]


def _by_kind(placements):
    kinds = {}
    for placement in placements:
        kinds.setdefault(placement.placement_kind, []).append(placement)
    return kinds


@pytest.mark.parametrize("reader", READERS)
def test_plain_object_unchanged(reader):
    placements = reader.read_dsf_object_placements(SYNTHETIC_DSF_TEXT_LINES)
    plain = _by_kind(placements)["OBJECT"]
    assert len(plain) == 1
    row = plain[0]
    assert row.definition_index == 0
    assert row.resource_path == "Airport/Tunnel/2.obj"
    assert row.longitude == pytest.approx(-86.7)
    assert row.latitude == pytest.approx(36.1)
    assert row.heading_degrees == pytest.approx(111.0)
    assert row.above_ground_level_metres == 0.0
    assert row.mean_sea_level_elevation_m is None


@pytest.mark.parametrize("reader", READERS)
def test_object_agl_positive_offset(reader):
    placements = reader.read_dsf_object_placements(SYNTHETIC_DSF_TEXT_LINES)
    above_grade = [
        p
        for p in placements
        if p.placement_kind == "OBJECT_AGL"
        and p.above_ground_level_metres > 0
    ]
    assert len(above_grade) == 1
    row = above_grade[0]
    assert row.above_ground_level_metres == pytest.approx(5.5)
    # Heading came from token 5, NOT the elevation in token 4.
    assert row.heading_degrees == pytest.approx(111.0)
    assert row.mean_sea_level_elevation_m is None


@pytest.mark.parametrize("reader", READERS)
def test_object_agl_negative_offset_is_signed(reader):
    """A negative AGL offset is the below-grade signal (EGLL tunnels
    6/7/10); the sign must survive parsing exactly."""
    placements = reader.read_dsf_object_placements(SYNTHETIC_DSF_TEXT_LINES)
    below_grade = [
        p
        for p in placements
        if p.placement_kind == "OBJECT_AGL"
        and p.above_ground_level_metres < 0
    ]
    assert len(below_grade) == 1
    row = below_grade[0]
    assert row.above_ground_level_metres == pytest.approx(-7.5)
    assert row.heading_degrees == pytest.approx(111.0)


@pytest.mark.parametrize("reader", READERS)
def test_object_msl_skipped_by_default(reader):
    """Opt-out is the default: callers that do not ask for MSL rows see
    exactly the pre-change behaviour (no MSL rows)."""
    placements = reader.read_dsf_object_placements(SYNTHETIC_DSF_TEXT_LINES)
    assert all(p.placement_kind != "OBJECT_MSL" for p in placements)
    assert all(p.mean_sea_level_elevation_m is None for p in placements)
    # Plain OBJECT + two OBJECT_AGL only.
    assert len(placements) == 3


@pytest.mark.parametrize("reader", READERS)
def test_object_msl_included_on_opt_in(reader):
    placements = reader.read_dsf_object_placements(
        SYNTHETIC_DSF_TEXT_LINES, include_object_msl=True
    )
    msl = _by_kind(placements)["OBJECT_MSL"]
    assert len(msl) == 1
    row = msl[0]
    assert row.definition_index == 2
    assert row.resource_path == "Airport/Bridge/deck_fixture.obj"
    # Absolute elevation lands in its own field; the AGL offset stays 0.
    assert row.mean_sea_level_elevation_m == pytest.approx(166.9994)
    assert row.above_ground_level_metres == 0.0
    # Heading came from token 5, not the MSL elevation in token 4.
    assert row.heading_degrees == pytest.approx(111.0)
    # The opt-in only ADDS the MSL row; the other three are unchanged.
    assert len(placements) == 4


@pytest.mark.parametrize("reader", READERS)
def test_heading_column_trap(reader):
    """Regression guard for the one-column heading shift: were the reader
    to take the heading from token 4 on an AGL/MSL row it would read the
    elevation (5.5 / -7.5 / 166.9994) instead of 111.0."""
    placements = reader.read_dsf_object_placements(
        SYNTHETIC_DSF_TEXT_LINES, include_object_msl=True
    )
    assert {round(p.heading_degrees, 4) for p in placements} == {111.0}
    for placement in placements:
        assert placement.heading_degrees != pytest.approx(
            placement.above_ground_level_metres
        )
        if placement.mean_sea_level_elevation_m is not None:
            assert placement.heading_degrees != pytest.approx(
                placement.mean_sea_level_elevation_m
            )


@pytest.mark.parametrize("reader", READERS)
def test_tab_separated_agl_row(reader):
    """Whitespace-split (invariant I-17) must handle tab-separated AGL
    rows, offset and heading included."""
    placements = reader.read_dsf_object_placements(
        TAB_SEPARATED_DSF_TEXT_LINES
    )
    assert len(placements) == 1
    row = placements[0]
    assert row.placement_kind == "OBJECT_AGL"
    assert row.above_ground_level_metres == pytest.approx(-1.0)
    assert row.heading_degrees == pytest.approx(111.0)


@pytest.mark.parametrize("reader", READERS)
def test_accept_resource_filter_applies_to_all_kinds(reader):
    """The resource filter gates OBJECT / OBJECT_AGL / OBJECT_MSL alike;
    opting into MSL does not bypass it."""
    placements = reader.read_dsf_object_placements(
        SYNTHETIC_DSF_TEXT_LINES,
        accept_resource=lambda resource: resource.endswith("2a.obj"),
        include_object_msl=True,
    )
    assert len(placements) == 2
    assert {p.resource_path for p in placements} == {"Airport/Tunnel/2a.obj"}
    assert {p.placement_kind for p in placements} == {"OBJECT_AGL"}


@pytest.mark.parametrize("reader", READERS)
def test_out_of_range_definition_index_skipped(reader):
    lines = [
        "OBJECT_DEF only/one.obj\n",
        "OBJECT_MSL 9 -86.7 36.1 100.0 30.0\n",  # index past the defs
    ]
    assert reader.read_dsf_object_placements(
        lines, include_object_msl=True
    ) == []


def test_production_prototype_field_parity():
    """The two ObjectPlacement definitions must not drift."""
    assert (
        production_reader.ObjectPlacement._fields
        == prototype_reader.ObjectPlacement._fields
    )
    assert (
        production_reader.ObjectPlacement._field_defaults
        == prototype_reader.ObjectPlacement._field_defaults
    )

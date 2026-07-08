"""Contract tripwire for the DSF object integration (amendment A8).

The workstream-W1 contracts in ``docs/dsf_object_integration_spec.md``
(section 3, as amended by section 10 / A10) are frozen the moment W1
merges: seven modules, their signatures, their data-shape fields, and the
config flags.  Every downstream workstream (W2–W7) builds against them in
parallel, so silent drift in one agent's branch breaks a sibling's
integration two workstreams later — unless it breaks THIS file first.

Where this file and any prose section disagree, this file is the tiebreak
(amendment A10).  Changing a contract requires amending the spec, then
this file, in the same commit.

Pure hermetic: imports and ``inspect`` only — no X-Plane install, no
filesystem fixtures, no geometry.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from auto_patch import (
    config,
    mesh_sampler,
    obj8_partition,
    obj8_reader,
    object_anchor,
    object_footprints,
    object_rebake,
    post_mesh,
)


def _parameter_names(callable_object) -> list[str]:
    return list(inspect.signature(callable_object).parameters)


# ---------------------------------------------------------------------------
# config flags — name, type, default (spec section 4-W1 + amendment A10)
# ---------------------------------------------------------------------------

FLAG_EXPECTATIONS = [
    ("DSF_OBJECT_BUILDINGS", bool, False),
    ("DSF_OBJECT_FOOTPRINT_UNION", bool, False),
    ("DSF_OBJECT_REANCHOR", bool, False),
    ("DSF_OBJECT_ALLOW_ANIM", bool, False),
    ("DSF_OBJECT_MIN_REACH_M", float, 25.0),
    ("DSF_OBJECT_CONTACT_EPSILON_M", float, 0.25),
    ("DSF_OBJECT_FOOTPRINT_HEIGHT_M", float, 1.5),
    ("DSF_OBJECT_ELEVATED_BASE_M", float, 0.5),
    ("DSF_OBJECT_MAX_FOOTPRINT_AREA_M2", float, 0.0),
    ("DSF_OBJECT_MIN_BUILDING_HEIGHT_M", float, 2.5),
    ("DSF_OBJECT_PAD_FLAG_SPAN_M", float, 2.0),
]


@pytest.mark.parametrize(
    "name, expected_type, expected_default",
    FLAG_EXPECTATIONS,
    ids=[name for name, _, _ in FLAG_EXPECTATIONS],
)
def test_config_flag(name, expected_type, expected_default, monkeypatch):
    value = getattr(config, name)
    assert isinstance(value, expected_type)
    # Defaults hold in a clean environment (the suite does not set O4_*
    # overrides; if a developer's shell does, this catches it loudly).
    assert value == expected_default, (
        f"{name} != documented default — is an O4_* environment override "
        f"set in this shell?"
    )
    assert name in config.__all__


def test_amendment_a10_retired_flags_do_not_exist():
    """A10: the contact graph retired the gap and anchor-proximity knobs.
    Reintroducing them means someone implemented the superseded section 3
    text instead of the amendments."""
    for retired in (
        "DSF_OBJECT_STRUCTURE_GAP_M",
        "DSF_OBJECT_ANCHOR_PROXIMITY_M",
        "DSF_OBJECT_HEADING_TOLERANCE_DEG",
    ):
        assert not hasattr(config, retired), retired


# ---------------------------------------------------------------------------
# obj8_reader (spec section 3.1)
# ---------------------------------------------------------------------------

def test_object_placement_fields():
    assert obj8_reader.ObjectPlacement._fields == (
        "definition_index",
        "resource_path",
        "longitude",
        "latitude",
        "heading_degrees",
    )


def test_positional_command_fields():
    assert obj8_reader.PositionalCommand._fields == (
        "line_index",
        "keyword",
        "x",
        "y",
        "z",
        "y_token_index",
    )


def test_object_geometry_fields():
    assert obj8_reader.ObjectGeometry._fields == (
        "vertices",
        "solid_triangles",
        "draped_triangles",
        "positional_commands",
        "animation_block_count",
        "level_of_detail_count",
        "vertex_line_indices",
    )
    assert isinstance(
        inspect.getattr_static(
            obj8_reader.ObjectGeometry, "has_solid_geometry"
        ),
        property,
    )
    assert isinstance(
        inspect.getattr_static(
            obj8_reader.ObjectGeometry, "has_mixed_draped_solid_vertices"
        ),
        property,
    )
    assert callable(obj8_reader.ObjectGeometry.solid_reach_metres)


OBJ8_READER_SIGNATURES = [
    ("load_object_file", ["path"]),
    ("area_weighted_centroid", ["vertices", "triangles"]),
    ("horizontal_bounding_box", ["vertices", "triangles"]),
    (
        "local_offset_to_lonlat",
        [
            "anchor_latitude",
            "anchor_longitude",
            "heading_degrees",
            "local_x",
            "local_z",
        ],
    ),
    (
        "lonlat_to_local_offset",
        [
            "anchor_latitude",
            "anchor_longitude",
            "heading_degrees",
            "latitude",
            "longitude",
        ],
    ),
    ("read_dsf_object_placements", ["dsf_text_lines", "accept_resource"]),
    (
        "resolve_object_resource",
        ["resource_path", "pack_root", "xplane_root"],
    ),
]


@pytest.mark.parametrize(
    "name, expected_parameters",
    OBJ8_READER_SIGNATURES,
    ids=[name for name, _ in OBJ8_READER_SIGNATURES],
)
def test_obj8_reader_signature(name, expected_parameters):
    assert _parameter_names(getattr(obj8_reader, name)) == expected_parameters


# ---------------------------------------------------------------------------
# obj8_partition (amendment A1)
# ---------------------------------------------------------------------------

OBJ8_PARTITION_SIGNATURES = [
    ("weld_parts", ["vertices", "triangles"]),
    ("contact_graph", ["vertices", "parts", "epsilon_metres"]),
    ("connected_structures", ["part_count", "contact_edges"]),
]


@pytest.mark.parametrize(
    "name, expected_parameters",
    OBJ8_PARTITION_SIGNATURES,
    ids=[name for name, _ in OBJ8_PARTITION_SIGNATURES],
)
def test_obj8_partition_signature(name, expected_parameters):
    assert _parameter_names(getattr(obj8_partition, name)) == (
        expected_parameters
    )


def test_superseded_grouping_not_ported():
    """A10: ``group_components_into_structures`` (the 2 m bounding-box gap
    heuristic) is deliberately not ported; ``weld_parts`` subsumes
    ``connected_components``."""
    for module in (obj8_reader, obj8_partition):
        assert not hasattr(module, "group_components_into_structures")
        assert not hasattr(module, "connected_components")


# ---------------------------------------------------------------------------
# mesh_sampler (spec section 3.2)
# ---------------------------------------------------------------------------

def test_mesh_sampler_contract():
    assert issubclass(mesh_sampler.OutsideMeshError, Exception)
    init_parameters = _parameter_names(
        mesh_sampler.MeshElevationSampler.__init__
    )
    assert init_parameters == [
        "self",
        "mesh_path",
        "bounds",
        "margin_degrees",
    ]
    assert _parameter_names(
        mesh_sampler.MeshElevationSampler.elevation_at
    ) == ["self", "latitude", "longitude"]
    assert _parameter_names(
        mesh_sampler.MeshElevationSampler.elevation_at_or_none
    ) == ["self", "latitude", "longitude"]


# ---------------------------------------------------------------------------
# object_anchor (spec section 3.3, amended by A1/A3/A10)
# ---------------------------------------------------------------------------

def _dataclass_field_names(dataclass_type) -> tuple[str, ...]:
    return tuple(
        each_field.name for each_field in dataclasses.fields(dataclass_type)
    )


def test_object_pool_fields():
    assert _dataclass_field_names(object_anchor.ObjectPool) == (
        "placements",
        "resolved_paths",
    )


def test_structure_fields():
    assert _dataclass_field_names(object_anchor.Structure) == (
        "triangles_by_resource",
        "surface_area_square_metres",
        "centroid_latitude",
        "centroid_longitude",
        "minimum_base_y_by_resource",
        "is_ground_touching",
        "ground_span_metres",
        "needs_pad",
        "skip_reason",
        "inherited_from_structure_index",
    )


def test_rebake_decision_fields():
    assert _dataclass_field_names(object_anchor.RebakeDecision) == (
        "structures",
        "delta_by_resource_and_vertex",
        "anchor_ground_by_resource",
        "skipped",
    )


OBJECT_ANCHOR_SIGNATURES = [
    (
        "discover_object_pools",
        [
            "placements",
            "resolved_paths",
            "geometry_by_resource",
            "epsilon_metres",
        ],
    ),
    (
        "partition_structures",
        ["pool", "geometry_by_resource", "epsilon_metres"],
    ),
    (
        "structure_deltas",
        ["pool", "geometry_by_resource", "structures", "sampler"],
    ),
]


@pytest.mark.parametrize(
    "name, expected_parameters",
    OBJECT_ANCHOR_SIGNATURES,
    ids=[name for name, _ in OBJECT_ANCHOR_SIGNATURES],
)
def test_object_anchor_signature(name, expected_parameters):
    assert _parameter_names(getattr(object_anchor, name)) == (
        expected_parameters
    )


def test_amendment_a10_anchor_group_renamed():
    """A10: pooling is a world-geometry property, not an anchor property."""
    assert not hasattr(object_anchor, "AnchorGroup")
    assert not hasattr(object_anchor, "discover_anchor_groups")


# ---------------------------------------------------------------------------
# object_rebake (spec section 4-W5, amended by A2/A6)
# ---------------------------------------------------------------------------

def test_rebake_report_fields():
    assert _dataclass_field_names(object_rebake.RebakeReport) == (
        "objects_written",
        "vertices_offset_total",
        "structures_baked",
        "structures_needing_pad",
        "skipped",
        "orphaned_backups",
        "provenance_path",
    )


def test_object_rebake_signatures():
    assert _parameter_names(object_rebake.apply) == [
        "decision",
        "pack_root",
        "mesh_path",
    ]
    assert _parameter_names(object_rebake.check) == [
        "pack_root",
        "mesh_path",
    ]
    assert _parameter_names(object_rebake.restore) == ["pack_root"]


# ---------------------------------------------------------------------------
# object_footprints (spec section 4-W6)
# ---------------------------------------------------------------------------

def test_object_footprints_signature():
    assert _parameter_names(object_footprints.structure_ring) == [
        "structure",
        "geometry_by_resource",
        "placements",
    ]


# ---------------------------------------------------------------------------
# post_mesh (spec section 4-W7, amended by A4/A5)
# ---------------------------------------------------------------------------

def test_post_mesh_signature():
    assert _parameter_names(post_mesh.rebake_dsf_objects) == ["tile"]


# ---------------------------------------------------------------------------
# stubs stay stubs until their workstream lands
# ---------------------------------------------------------------------------

def test_unimplemented_stubs_raise_not_implemented():
    """Each contract body raises ``NotImplementedError`` until its owning
    workstream replaces it.  When a workstream lands, DELETE its entry
    here (its real tests take over) — a lingering entry that suddenly
    fails is a reminder, not a defect."""
    # obj8_reader and obj8_partition landed in workstream W2;
    # mesh_sampler landed in workstream W3.  Their entries are deleted
    # per this test's docstring; tests/test_obj8_reader.py,
    # tests/test_obj8_partition.py and tests/test_mesh_sampler.py have
    # taken over.
    # object_anchor landed in workstream W4, object_rebake in workstream
    # W5, and object_footprints in workstream W6;
    # tests/test_object_anchor.py, tests/test_object_rebake.py and
    # tests/test_dsf_object_buildings.py have taken over.
    with pytest.raises(NotImplementedError):
        post_mesh.rebake_dsf_objects(None)

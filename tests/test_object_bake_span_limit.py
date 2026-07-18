"""Tests for the rigid-seat ground-span limit (design 2026-07-17).

A co-baked payware pack chains real buildings into airport-scale contact
components through connector objects (perimeter fences, parked-car texture
fields).  The Phase-2 y-bake sees each component as ONE rigid unit and
bakes a single vertical offset for it; when the terrain under the
component's ground-contact parts spans more than
``DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M`` metres, one end floats or sinks past
the seating tolerance no matter where that single offset lands.  Such a
structure is left at its AUTHORED elevations instead (its real buildings
are carried by their own Phase-1 pads).

These tests are hermetic: synthetic geometry against a small hand-written
plane mesh in ``tmp_path`` (reusing the construction helpers and the
``plane_sampler`` fixture from :mod:`test_object_anchor`), plus a direct
exercise of :func:`auto_patch.object_rebake.apply`'s reversion pass
against a fake pack directory — no network, no X-Plane install.
"""

from __future__ import annotations

import pytest

from auto_patch import config, object_rebake
from auto_patch.object_anchor import (
    ObjectPool,
    RebakeDecision,
    Structure,
    partition_structures,
    structure_deltas,
)

# Reuse the invariant-tested construction helpers and the plane-mesh
# fixture.  Importing ``plane_sampler`` into this module's namespace makes
# pytest resolve it for tests here.
from test_object_anchor import (  # noqa: F401 (plane_sampler is a fixture)
    CONTACT_EPSILON_METRES,
    PLANE_ANCHOR_LATITUDE,
    PLANE_ANCHOR_LONGITUDE,
    PLANE_ELEVATION_PER_DEGREE_LONGITUDE,
    compound_geometry,
    make_placement,
    metres_per_degree_longitude_at,
    plane_ground,
    plane_sampler,
    two_foot_gantry_geometry,
)

GROUND_SPAN_SKIP_PHRASE = "exceeds the rigid-seat limit"


def _single_resource_decision(geometry, sampler, resource="chain.obj"):
    """Partition ``geometry`` (one resource at the plane anchor) into
    structures and solve the seating decision against ``sampler``."""
    placement = make_placement(
        resource, PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
    )
    geometry_by_resource = {resource: geometry}
    pool = ObjectPool(
        placements=[placement],
        resolved_paths={resource: f"/nonexistent/{resource}"},
    )
    structures = partition_structures(
        pool, geometry_by_resource, epsilon_metres=CONTACT_EPSILON_METRES
    )
    assert len(structures) == 1
    decision = structure_deltas(
        pool, geometry_by_resource, structures, sampler
    )
    return structures, decision


class TestGroundSpanBakeLimit:
    def test_high_span_chained_structure_is_left_at_authored_elevations(
        self, plane_sampler
    ):
        # One structure of three parts chained by sub-epsilon gaps: two
        # ground slabs 50 m apart on the plane's slope (an EGGW-style
        # connector chain in miniature) plus an elevated beam bridging
        # them.  The ground span (~14 m) far exceeds the 3.0 m rigid-seat
        # limit, so the whole structure is skipped and carries no delta.
        geometry = compound_geometry(
            (0.0, 10.0, 0.0, 1.0, 0.0, 10.0),        # west ground slab
            (10.1, 49.9, 0.6, 1.0, 0.0, 10.0),       # elevated beam
            (50.0, 60.0, 0.0, 1.0, 0.0, 10.0),       # east ground slab
        )
        _structures, decision = _single_resource_decision(
            geometry, plane_sampler
        )
        updated = decision.structures[0]

        assert updated.skip_reason is not None
        assert GROUND_SPAN_SKIP_PHRASE in updated.skip_reason
        assert "left at authored elevations" in updated.skip_reason
        # The reason quotes the measured span to one decimal.
        assert (
            f"ground span {updated.ground_span_metres:.1f} m"
            in updated.skip_reason
        )
        assert (
            updated.ground_span_metres
            > config.DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M
        )
        # Skipped structures carry NO delta — nothing is baked for them,
        # so the byte-idempotent rewrite (and the reversion pass) leave
        # every vertex at its authored y.
        assert "chain.obj" not in decision.delta_by_resource_and_vertex

    def test_span_at_or_under_the_limit_bakes_exactly_as_before(
        self, plane_sampler
    ):
        # Two ground slabs 6 m apart on the plane slope (span ~1.7 m,
        # comfortably under the 3.0 m limit) chained by an elevated beam.
        # The structure bakes normally, exactly as it did before the
        # limit existed: a delta for every vertex and no skip.
        geometry = compound_geometry(
            (0.0, 2.0, 0.0, 1.0, 0.0, 4.0),          # west ground slab
            (2.05, 5.95, 0.6, 1.0, 0.0, 4.0),        # elevated beam
            (6.0, 8.0, 0.0, 1.0, 0.0, 4.0),          # east ground slab
        )
        _structures, decision = _single_resource_decision(
            geometry, plane_sampler
        )
        updated = decision.structures[0]

        assert updated.skip_reason is None
        assert (
            updated.ground_span_metres
            <= config.DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M
        )
        # Baked: every vertex of the three boxes received a delta.
        deltas = decision.delta_by_resource_and_vertex["chain.obj"]
        assert set(deltas) == set(range(24))

    def test_the_limit_gates_the_decision(self, plane_sampler, monkeypatch):
        # The same high-span structure that skips at the default 3.0 m
        # limit bakes when the limit is raised past its span — proving the
        # constant, not some incidental property, is the gate (and that a
        # structure at/under the threshold is untouched by the new code).
        geometry = compound_geometry(
            (0.0, 10.0, 0.0, 1.0, 0.0, 10.0),
            (10.1, 49.9, 0.6, 1.0, 0.0, 10.0),
            (50.0, 60.0, 0.0, 1.0, 0.0, 10.0),
        )
        monkeypatch.setattr(
            config, "DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M", 1000.0
        )
        _structures, decision = _single_resource_decision(
            geometry, plane_sampler
        )
        updated = decision.structures[0]
        assert updated.skip_reason is None
        assert updated.ground_span_metres > 3.0     # still a wide span
        assert "chain.obj" in decision.delta_by_resource_and_vertex

    def test_foot_anchored_high_span_keeps_the_per_foot_path(
        self, plane_sampler
    ):
        # A foot-anchored structure (author-baked vertical offset; two
        # ground-contact feet 38-40 m apart across the plane's slope, so
        # the ground under the feet spans > 3 m) must NOT be caught by the
        # rigid-seat span skip: each foot seats independently, which is
        # exactly what the per-foot machinery is for.
        structures, decision = _single_resource_decision(
            two_foot_gantry_geometry(span_axis="east"),
            plane_sampler,
            resource="gantry.obj",
        )
        assert not structures[0].is_ground_touching
        updated = decision.structures[0]

        # Foot-anchored, not skipped by the span limit.
        feet = decision.foot_clusters_by_structure_index.get(0)
        assert feet is not None and len(feet) == 2
        if updated.skip_reason is not None:
            assert GROUND_SPAN_SKIP_PHRASE not in updated.skip_reason
        # The feet path still bakes a rigid offset for the object.
        assert "gantry.obj" in decision.delta_by_resource_and_vertex


class TestStaleBakeRestoreOnSkip:
    """The reversion pass in :func:`object_rebake.apply` un-bakes any
    object excluded from the current decision (a skipped structure carries
    no delta) whose live file still differs from its ``.anchor_bak`` — the
    stale-bake restore the span limit relies on."""

    def _skipped_decision(self, resource):
        skipped_structure = Structure(
            triangles_by_resource={resource: [(0, 1, 2)]},
            surface_area_square_metres=100.0,
            centroid_latitude=0.0,
            centroid_longitude=0.0,
            minimum_base_y_by_resource={resource: 0.0},
            is_ground_touching=True,
            ground_span_metres=12.0,
            needs_pad=True,
            skip_reason=(
                "ground span 12.0 m exceeds the rigid-seat limit — left "
                "at authored elevations"
            ),
            inherited_from_structure_index=None,
        )
        return RebakeDecision(
            structures=[skipped_structure],
            delta_by_resource_and_vertex={},
            anchor_ground_by_resource={},
            skipped=[],
        )

    def _write_mesh(self, tmp_path):
        mesh_path = tmp_path / "Data+00-000.mesh"
        mesh_path.write_text("MeshVersionFormatted 2\nEnd\n")
        return str(mesh_path)

    def test_skip_restores_stale_bake_byte_identical_to_backup(
        self, tmp_path
    ):
        resource = "objects/tower.obj"
        pack_root = tmp_path / "pack"
        live_path = pack_root / resource
        live_path.parent.mkdir(parents=True)
        backup_bytes = b"AUTHORED original object bytes\n"
        stale_bytes = b"STALE baked object bytes (wrong offset)\n"
        # The pack on disk was mutated by an earlier build: live differs
        # from its .anchor_bak original.
        live_path.write_bytes(stale_bytes)
        backup_path = live_path.with_name(live_path.name + ".anchor_bak")
        backup_path.write_bytes(backup_bytes)

        report = object_rebake.apply(
            self._skipped_decision(resource),
            str(pack_root),
            self._write_mesh(tmp_path),
        )

        assert resource in report.objects_reverted
        assert live_path.read_bytes() == backup_bytes
        assert report.reversions_missing_backup == []

    def test_skip_with_no_backup_leaves_the_file_untouched(self, tmp_path):
        resource = "objects/tower.obj"
        pack_root = tmp_path / "pack"
        live_path = pack_root / resource
        live_path.parent.mkdir(parents=True)
        original_bytes = b"some baked object with no backup beside it\n"
        live_path.write_bytes(original_bytes)
        # No .anchor_bak: the safety rule forbids writing a pack file
        # without its backup, so the file is left exactly as-is.

        report = object_rebake.apply(
            self._skipped_decision(resource),
            str(pack_root),
            self._write_mesh(tmp_path),
        )

        assert resource not in report.objects_reverted
        assert live_path.read_bytes() == original_bytes

    def test_skip_leaves_an_already_authored_file_untouched(self, tmp_path):
        # A skipped object whose live file already equals its backup has
        # no stale bake to undo: the reversion pass leaves it alone (no
        # needless rewrite, byte-identical).
        resource = "objects/tower.obj"
        pack_root = tmp_path / "pack"
        live_path = pack_root / resource
        live_path.parent.mkdir(parents=True)
        authored_bytes = b"AUTHORED object, never baked\n"
        live_path.write_bytes(authored_bytes)
        backup_path = live_path.with_name(live_path.name + ".anchor_bak")
        backup_path.write_bytes(authored_bytes)

        report = object_rebake.apply(
            self._skipped_decision(resource),
            str(pack_root),
            self._write_mesh(tmp_path),
        )

        assert resource not in report.objects_reverted
        assert live_path.read_bytes() == authored_bytes

"""Tidal / lagoon water stays INLAND and overrides the coastline
(2026-07-17, second iteration).

OpenStreetMap maps the Ria Formosa both as a coastline that reaches
through the inlets AND as a ``water=lagoon`` + ``tidal=yes`` relation.
The contract under test: tidal water polygons keep the classic inland
rendering (orthophoto at ``ratio_water`` transparency with X-Plane
water on top), and the SEA attribute is never seeded inside them, so
the deep-water fade begins at the true coast — never inside a lagoon.
(The first iteration routed these polygons to SEA_EQUIV; that printed
permanently wet water as opaque dark imagery and was reverted.)

Headless: layers are built by hand, no network, no tile build.
"""

from __future__ import annotations

import pytest
from shapely import geometry

import O4_OSM_Utils as OSM
import O4_Vector_Map as VMAP


def _square(lon0, lat0, side=0.001):
    """Closed-square corner coordinates as (lon, lat) tuples."""
    return [
        (lon0, lat0),
        (lon0 + side, lat0),
        (lon0 + side, lat0 + side),
        (lon0, lat0 + side),
        (lon0, lat0),
    ]


def _layer_with_ponds(pond_tags_by_wayid):
    """An OSM_layer holding one small closed-way square per entry."""
    layer = OSM.OSM_layer()
    node_id = 1
    for index, (wayid, tags) in enumerate(
        sorted(pond_tags_by_wayid.items())
    ):
        corners = _square(-7.98 + 0.01 * index, 37.01)
        node_ids = []
        for lon, lat in corners[:-1]:
            layer.dicosmn[node_id] = (lon, lat)
            node_ids.append(node_id)
            node_id += 1
        layer.dicosmw[wayid] = node_ids + [node_ids[0]]
        layer.dicosmfirst["w"].add(wayid)
        if tags:
            layer.dicosmtags["w"][wayid] = dict(tags)
    return layer


class TestTidalPredicate:
    def test_tidal_yes_is_tidal(self):
        assert VMAP.water_polygon_is_tidal(
            5, {5: {"tidal": "yes", "water": "reservoir"}}
        )

    def test_lagoon_is_tidal(self):
        assert VMAP.water_polygon_is_tidal(5, {5: {"water": "lagoon"}})

    def test_plain_reservoir_is_not_tidal(self):
        assert not VMAP.water_polygon_is_tidal(
            5, {5: {"water": "reservoir"}}
        )

    def test_untagged_is_not_tidal(self):
        assert not VMAP.water_polygon_is_tidal(5, {})

    def test_tidal_no_is_not_tidal(self):
        assert not VMAP.water_polygon_is_tidal(5, {5: {"tidal": "no"}})


class TestTidalUnionExtraction:
    def test_filter_splits_tidal_from_inland(self):
        """The layer split _tidal_water_area relies on: tidal / lagoon
        polygons separate cleanly from ordinary inland water."""
        layer = _layer_with_ponds(
            {
                11: {"tidal": "yes", "water": "reservoir"},
                12: {"water": "lagoon"},
                13: {"water": "reservoir"},
                14: {},
            }
        )
        (inland, tidal) = OSM.OSM_to_MultiPolygon(
            layer,
            37,
            -8,
            lambda pol, osmid, dicosmtags: VMAP.water_polygon_is_tidal(
                osmid, dicosmtags
            ),
        )
        assert len(tidal.geoms) == 2
        assert len(inland.geoms) == 2


class TestSeaSeedAreas:
    """The coastline override: no SEA seed inside tidal water."""

    def _lagoon_inside_sea(self):
        # A sea polygon reaching through an inlet into a lagoon: two
        # barrier strips leave a channel at x 0.45..0.55, and the lagoon
        # body above them dominates the area, so the naive
        # representative point of the contiguous sea lands inside it.
        everything = geometry.box(0.0, 0.0, 1.0, 1.0)
        barriers = geometry.MultiPolygon([
            geometry.box(0.0, 0.10, 0.45, 0.14),
            geometry.box(0.55, 0.10, 1.0, 0.14),
        ])
        sea = everything.difference(barriers)
        lagoon = geometry.box(0.0, 0.14, 1.0, 1.0).intersection(sea)
        as_multi = lambda shape: geometry.MultiPolygon(
            [shape] if shape.geom_type == "Polygon" else list(shape.geoms)
        )
        return (as_multi(sea), as_multi(lagoon))

    def test_seeds_avoid_the_lagoon(self):
        (sea_area, lagoon) = self._lagoon_inside_sea()
        seed_area = VMAP.sea_seed_areas(sea_area, lagoon)
        assert not seed_area.is_empty
        for piece in seed_area.geoms:
            point = piece.representative_point()
            assert not lagoon.contains(point)

    def test_empty_tidal_water_changes_nothing(self):
        (sea_area, _lagoon) = self._lagoon_inside_sea()
        assert VMAP.sea_seed_areas(
            sea_area, geometry.MultiPolygon()
        ) is sea_area
        assert VMAP.sea_seed_areas(sea_area, None) is sea_area

    def test_fully_tidal_sea_yields_no_seeds(self):
        (sea_area, _lagoon) = self._lagoon_inside_sea()
        seed_area = VMAP.sea_seed_areas(sea_area, sea_area)
        assert seed_area.is_empty

    def test_geometry_failure_falls_back_to_the_sea(self):
        (sea_area, _lagoon) = self._lagoon_inside_sea()

        class _Broken:
            is_empty = False

        assert VMAP.sea_seed_areas(sea_area, _Broken()) is sea_area


class TestCacheSchemaWiring:
    def test_tags_of_interest_carry_tidal_and_water(self):
        assert "tidal" in VMAP.WATER_TAGS_OF_INTEREST
        assert "water" in VMAP.WATER_TAGS_OF_INTEREST

    def test_prefetch_specification_carries_the_schema(self):
        class FakeTile:
            road_level = 0
            lat = 37
            lon = -8

        specifications = VMAP._osm_layer_prefetch_specifications(FakeTile())
        water_specs = [s for s in specifications if s[0] == "water"]
        if not water_specs:
            pytest.skip("custom water data present for +37-008")
        (_, _, tags_of_interest, _, cache_schema) = water_specs[0]
        assert tags_of_interest == VMAP.WATER_TAGS_OF_INTEREST
        assert cache_schema == VMAP.WATER_CACHE_TAG_SCHEMA
        assert cache_schema  # non-empty: stale caches must re-download

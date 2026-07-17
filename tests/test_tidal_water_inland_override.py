"""SEA_EQUIV routing of tidal ponds and lagoons (LPFR salinas, 2026-07-17).

OpenStreetMap water polygons tagged ``tidal=yes`` or ``water=lagoon``
must leave the classic inland-water class (constant ``ratio_water``
alpha, depth ratio pinned deep) and join the SEA_EQUIV class, which is
masked and depth-graded like the sea.  Headless: the layer is built by
hand, no network, no tile build.
"""

from __future__ import annotations

import pytest

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


class TestMultiPolygonRouting:
    def test_tidal_and_lagoon_split_from_inland(self):
        layer = _layer_with_ponds(
            {
                11: {"tidal": "yes", "water": "reservoir"},
                12: {"water": "lagoon"},
                13: {"water": "reservoir"},
                14: {},
            }
        )
        (inland, sea_equivalent) = OSM.OSM_to_MultiPolygon(
            layer,
            37,
            -8,
            lambda pol, osmid, dicosmtags: VMAP.water_polygon_is_tidal(
                osmid, dicosmtags
            ),
        )
        assert len(sea_equivalent.geoms) == 2
        assert len(inland.geoms) == 2


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

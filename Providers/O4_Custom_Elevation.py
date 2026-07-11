"""Escape hatch for elevation-inset providers that defy the declarative fields.

This mirrors ``Providers/O4_Custom_URL.py`` for imagery: most elevation
providers are fully described by a ``Providers/Elevation/<CODE>.elv``
definition file plus one of the named access strategies registered in
``src/O4_Airport_Elevation_Insets.py``.  A few sources need real logic
that does not fit a flat ``key=value`` file -- rotating session tokens,
request signing, multi-step discovery, or a bespoke mosaic assembly.

For those, add the provider ``CODE`` to ``custom_elevation_list`` below and
implement ``custom_elevation_fetch``.  The orchestration in
``O4_Airport_Elevation_Insets`` consults this module ONLY when a provider's
``access_strategy`` is ``custom`` (or absent) and its code is listed here;
otherwise this file is never imported into the hot path.

Absent or empty file is a no-op: the feature simply has no custom
providers.  Phase A ships this stub -- the United States Geological Survey
3D Elevation Program provider is fully declarative via the ``tnm_cog``
strategy and needs nothing here.

Contract (implement when you need it)
-------------------------------------
``custom_elevation_discover(definition, bounding_box_wgs84)``
    Return a list of opaque source descriptors (any JSON-serialisable
    objects the matching fetch understands), or ``None`` for no coverage.

``custom_elevation_fetch(definition, bounding_box_wgs84,``
``                       target_resolution_metres, destination_path)``
    Write an EPSG:4326 float32 GeoTIFF (with a nodata value) to
    ``destination_path`` and return a provenance metadata dictionary.

``definition`` is the parsed ``.elv`` dictionary for the provider.
``bounding_box_wgs84`` is ``(west, south, east, north)`` in degrees.
"""

# Provider codes routed through the custom hook instead of a declarative
# access strategy.  Empty in Phase A.
custom_elevation_list = ()


def custom_elevation_discover(definition, bounding_box_wgs84):
    """Placeholder: no custom providers are defined in Phase A."""
    return None


def custom_elevation_fetch(
    definition,
    bounding_box_wgs84,
    target_resolution_metres,
    destination_path,
):
    """Placeholder: no custom providers are defined in Phase A."""
    raise NotImplementedError(
        "No custom elevation provider is implemented; add one to "
        "custom_elevation_list and implement custom_elevation_fetch."
    )

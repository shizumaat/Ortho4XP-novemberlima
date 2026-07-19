"""Parametric X-Plane OBJ8 building generator.

Vocabulary: an atlas (bands that tile horizontally + stretch rects), a
Mesh with explicit-outward-normal faces (clockwise-front winding), a
placement Frame, and composable primitives (wall runs, footprint
extrusion, polygon caps, gable/shed roofs, canopies). See geometry.py
for coordinate conventions.

Build-time impact: none — not part of the tile build pipeline.
"""
from .atlas import AtlasBand, AtlasRect, stack_bands
from .geometry import (
    Frame,
    Mesh,
    box,
    canopy,
    extrude_footprint,
    footprint_area,
    gable_roof,
    normalize_footprint,
    oriented_slab,
    prism,
    polygon_cap,
    shed_roof,
    triangulate_footprint,
    wall_run,
)
from .obj8_writer import write_obj8
from .texture import AtlasPainter

__all__ = [
    "AtlasBand",
    "AtlasPainter",
    "AtlasRect",
    "Frame",
    "Mesh",
    "box",
    "canopy",
    "extrude_footprint",
    "footprint_area",
    "gable_roof",
    "normalize_footprint",
    "oriented_slab",
    "prism",
    "polygon_cap",
    "shed_roof",
    "stack_bands",
    "triangulate_footprint",
    "wall_run",
    "write_obj8",
]

"""Airport-elevation-inset acceptance + performance harness (steps 1 and 2).

Builds one tile's vector (step 1) and mesh (step 2) headlessly -- the same
initialisation as ``tools/run_tile_mesh_only.py`` -- while (a) pinning the
Phase C1 ``working_grid_arc_seconds`` so a 1 arc-second baseline and a
densified run can be compared, and (b) recording the guardrail metrics the
spec asks for: step-1 and step-2 wall time, the written ``.alt`` size and
grid, and the Triangle4XP vertex/triangle counts.  It then probes the
written ``.alt`` (with the pipeline's own ``DEM.alt_nostrict`` interpolation)
and the built ``Data<tile>.mesh`` (barycentric, via
``tools/mesh_elevation_sampler.MeshElevationSampler``) at the spec section 5
KBNA acceptance probes and reports a transect.

Run under ``/usr/bin/time -l`` to capture peak memory (the harness prints a
hint).  Everything is emitted as one JSON blob on stdout after a human table,
so successive runs can be diffed.

Usage (from the checkout root):
    venv/bin/python tools/airport_inset_acceptance.py <lat> <lon> \\
        [working_grid_arc_seconds]

Example -- 1 arc-second baseline then the automatic densified run for KBNA:
    /usr/bin/time -l venv/bin/python tools/airport_inset_acceptance.py \\
        36 -87 1
    /usr/bin/time -l venv/bin/python tools/airport_inset_acceptance.py \\
        36 -87 auto
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

sys.path.append(os.path.join(".", "src"))

import numpy

import O4_File_Names as FNAMES

sys.path.append(FNAMES.Provider_dir)
import O4_Imagery_Utils as IMG
import O4_Vector_Map as VMAP
import O4_Mesh_Utils as MESH
import O4_Config_Utils as CFG
import O4_DEM_Utils as DEM
import O4_Airport_Elevation_Insets as INSETS

sys.path.append(os.path.join(".", "tools"))
from mesh_elevation_sampler import MeshElevationSampler

# Spec section 5 KBNA acceptance probes: (name, latitude, longitude, truth m).
KBNA_PROBES = (
    ("SW foot", 36.1374844, -86.6760939, 156.8),
    ("anchor", 36.1376421, -86.6759065, 155.7),
    ("NE foot", 36.1377853, -86.6757619, 156.0),
    ("twy M plateau", 36.13715, -86.67650, 166.7),
)
# Mesh transect (spec section 5): shelf segment 45-100 m along should mean
# within +/-2 m of 155.7 with no monotone ramp.
TRANSECT_START = (36.13715, -86.67650)
TRANSECT_END = (36.13815, -86.67525)


def _sample_alt_raster(alt_path, latitude, longitude, geometry):
    """Sample a written ``.alt`` raster with the pipeline's own
    two-triangle ``DEM.alt_nostrict`` interpolation (what the mesher sees)."""
    (tile_latitude, tile_longitude, x0, x1, y0, y1) = geometry
    array = numpy.fromfile(alt_path, dtype=numpy.float32)
    side = int(round(math.sqrt(array.size)))
    array = array.reshape(side, side)
    number_x = side - 1
    number_y = side - 1
    x = min(max(longitude - tile_longitude, x0), x1)
    y = min(max(latitude - tile_latitude, y0), y1)
    px = (x - x0) / (x1 - x0) * number_x
    py = (y - y0) / (y1 - y0) * number_y
    column = int(px)
    n_minus_ny = number_y - int(py)
    rx = px - column
    ry = py + n_minus_ny - number_y
    column_plus = (column + 1) * (column < number_x) + number_x * (
        column == number_x
    )
    row_up = (n_minus_ny - 1) * (n_minus_ny >= 1)
    t1 = array[n_minus_ny, column]
    t2 = array[row_up, column_plus]
    t3 = array[n_minus_ny, column_plus]
    t4 = array[row_up, column]
    if rx >= ry:
        return float((1 - rx) * t1 + ry * t2 + (rx - ry) * t3)
    return float((1 - ry) * t1 + rx * t2 + (ry - rx) * t4)


def _read_mesh_counts(mesh_path):
    vertices = triangles = None
    with open(mesh_path) as handle:
        while True:
            line = handle.readline()
            if not line:
                break
            token = line.strip()
            if token == "Vertices":
                vertices = int(handle.readline())
            elif token == "Triangles":
                triangles = int(handle.readline())
                break
    return (vertices, triangles)


def main():
    latitude = int(sys.argv[1])
    longitude = int(sys.argv[2])
    grid_override = sys.argv[3] if len(sys.argv) > 3 else None

    IMG.initialize_extents_dict()
    IMG.initialize_color_filters_dict()
    IMG.initialize_providers_dict()
    IMG.initialize_combined_providers_dict()

    tile = CFG.Tile(latitude, longitude, "")
    tile.read_from_config()
    if grid_override is not None:
        tile.working_grid_arc_seconds = grid_override
    print(
        "build directory:",
        tile.build_dir,
        "| working_grid_arc_seconds =",
        tile.working_grid_arc_seconds,
        flush=True,
    )

    timings = {}
    for step_name, step in (
        ("step1_vector", VMAP.build_poly_file),
        ("step2_mesh", MESH.build_mesh),
    ):
        print(f"=== {step_name} ===", flush=True)
        started = time.time()
        result = step(tile)
        timings[step_name] = time.time() - started
        if not result:
            raise SystemExit(f"{step_name} FAILED (returned {result})")

    alt_path = FNAMES.alt_file(tile)
    mesh_path = FNAMES.mesh_file(tile.build_dir, latitude, longitude)
    alt_bytes = os.path.getsize(alt_path)
    alt_side = int(round(math.sqrt(alt_bytes / 4)))
    (vertex_count, triangle_count) = _read_mesh_counts(mesh_path)

    # Reconstruct the exact written-grid geometry the same way step 2 does
    # (base source + Phase C1 densification), so the .alt probe samples the
    # right cells regardless of the base source (NED1, Viewfinder, ...).
    composite = INSETS.assemble_inset_composite_source(tile, tile.custom_dem)
    base_source = composite.split(";")[0] if ";" in composite else composite
    tile.dem = DEM.DEM(
        latitude,
        longitude,
        base_source,
        tile.fill_nodata or "to zero",
        info_only=True,
    )
    INSETS.densify_tile_dem_for_insets(tile)
    geometry = (
        latitude,
        longitude,
        tile.dem.x0,
        tile.dem.x1,
        tile.dem.y0,
        tile.dem.y1,
    )

    sampler = MeshElevationSampler(
        mesh_path,
        (
            min(p[2] for p in KBNA_PROBES) - 0.002,
            min(p[1] for p in KBNA_PROBES) - 0.002,
            max(p[2] for p in KBNA_PROBES) + 0.002,
            max(p[1] for p in KBNA_PROBES) + 0.002,
        ),
    )

    probe_rows = []
    for (name, probe_lat, probe_lon, truth) in KBNA_PROBES:
        alt_value = _sample_alt_raster(alt_path, probe_lat, probe_lon, geometry)
        mesh_value = sampler.elevation_at(probe_lat, probe_lon)
        probe_rows.append(
            {
                "name": name,
                "lat": probe_lat,
                "lon": probe_lon,
                "truth": truth,
                "alt": round(alt_value, 2),
                "alt_error": round(alt_value - truth, 2),
                "mesh": round(mesh_value, 2),
                "mesh_error": round(mesh_value - truth, 2),
            }
        )

    transect = []
    steps = 20
    for index in range(steps + 1):
        fraction = index / steps
        lat = TRANSECT_START[0] + fraction * (
            TRANSECT_END[0] - TRANSECT_START[0]
        )
        lon = TRANSECT_START[1] + fraction * (
            TRANSECT_END[1] - TRANSECT_START[1]
        )
        along_m = fraction * math.hypot(
            (TRANSECT_END[0] - TRANSECT_START[0]) * 111320.0,
            (TRANSECT_END[1] - TRANSECT_START[1])
            * 111320.0
            * math.cos(math.radians(lat)),
        )
        transect.append(
            {
                "along_m": round(along_m, 1),
                "mesh": round(sampler.elevation_at(lat, lon), 2),
            }
        )

    summary = {
        "tile": [latitude, longitude],
        "working_grid_arc_seconds": tile.working_grid_arc_seconds,
        "alt_grid": [alt_side, alt_side],
        "alt_bytes": alt_bytes,
        "alt_megabytes": round(alt_bytes / 1e6, 1),
        "vertices": vertex_count,
        "triangles": triangle_count,
        "step1_seconds": round(timings["step1_vector"], 1),
        "step2_seconds": round(timings["step2_mesh"], 1),
        "probes": probe_rows,
        "transect": transect,
    }

    print("\n=== ACCEPTANCE SUMMARY ===")
    print(
        f"grid {alt_side}x{alt_side}  .alt {summary['alt_megabytes']} MB  "
        f"vertices {vertex_count}  triangles {triangle_count}"
    )
    print(
        f"step1 {summary['step1_seconds']} s   "
        f"step2 {summary['step2_seconds']} s"
    )
    print(
        f"{'probe':16s} {'truth':>7s} {'.alt':>8s} {'err':>7s} "
        f"{'mesh':>8s} {'err':>7s}"
    )
    for row in probe_rows:
        print(
            f"{row['name']:16s} {row['truth']:7.1f} {row['alt']:8.2f} "
            f"{row['alt_error']:+7.2f} {row['mesh']:8.2f} "
            f"{row['mesh_error']:+7.2f}"
        )
    shelf = [
        point["mesh"]
        for point in transect
        if 45 <= point["along_m"] <= 100
    ]
    if shelf:
        print(
            "transect shelf 45-100 m mean:",
            round(sum(shelf) / len(shelf), 2),
            "(target 155.7 +/- 2)",
        )
    print("\n(Run under /usr/bin/time -l for peak memory.)")
    print("JSON " + json.dumps(summary))


if __name__ == "__main__":
    main()

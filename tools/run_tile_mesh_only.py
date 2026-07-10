"""Headless vector + mesh build (steps 1 and 2 only, no masks/textures).

Same initialisation as ``tools/run_tile_build.py`` (which see for why the
tile config must be loaded explicitly), but stops after the mesh step.
This is the loop for consumer-side mesh changes: it needs only
Elevation_data, OSM_data caches and Patches — no imagery.

Usage: run_tile_mesh_only.py <latitude> <longitude>   (from the checkout root)
"""
import os
import sys

sys.path.append(os.path.join(".", "src"))

import O4_File_Names as FNAMES

sys.path.append(FNAMES.Provider_dir)
import O4_Imagery_Utils as IMG
import O4_Vector_Map as VMAP
import O4_Mesh_Utils as MESH
import O4_Config_Utils as CFG

# The auto_patch driver uses a ProcessPool; macOS spawn re-imports the
# main module, so an unguarded body re-runs the ENTIRE build in every
# worker (see run_tile_build.py).
if __name__ == "__main__":
    IMG.initialize_extents_dict()
    IMG.initialize_color_filters_dict()
    IMG.initialize_providers_dict()
    IMG.initialize_combined_providers_dict()

    latitude = int(sys.argv[1])
    longitude = int(sys.argv[2])
    tile = CFG.Tile(latitude, longitude, "")
    tile.read_from_config()
    print("build directory:", tile.build_dir)

    for step_name, step in (
        ("1 vector", VMAP.build_poly_file),
        ("2 mesh", MESH.build_mesh),
    ):
        print(f"=== step {step_name} ===", flush=True)
        result = step(tile)
        if not result:
            raise SystemExit(f"step {step_name} FAILED (returned {result})")
    print("mesh build complete", flush=True)

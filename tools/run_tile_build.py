"""Headless full tile build that actually loads the per-tile config.

Ortho4XP.py's single-tile command-line path never calls
``tile.read_from_config()`` — only the GUI batch path does — so a
headless run silently builds with GLOBAL defaults (empty
``default_website`` produced provider-less terrain names on the first
KCLT cycle).  This runner mirrors Ortho4XP.py's initialisation, loads
the tile config explicitly, verifies the provider resolves, and runs
all four steps.

Usage: run_tile_build.py <latitude> <longitude>   (from the checkout root)
"""
import os
import sys

sys.path.append(os.path.join(".", "src"))

import O4_File_Names as FNAMES
import O4_UI_Utils as UI

sys.path.append(FNAMES.Provider_dir)
import O4_Imagery_Utils as IMG
import O4_Vector_Map as VMAP
import O4_Mesh_Utils as MESH
import O4_Mask_Utils as MASK
import O4_Tile_Utils as TILE
import O4_Config_Utils as CFG

# The auto_patch driver uses a ProcessPool; macOS spawn re-imports
# the main module, so an unguarded body re-runs the ENTIRE build in
# every worker (observed live: a rogue child restarted step 1 after
# the real build completed).
if __name__ == "__main__":
    IMG.initialize_extents_dict()
    IMG.initialize_color_filters_dict()
    IMG.initialize_providers_dict()
    IMG.initialize_combined_providers_dict()

    latitude = int(sys.argv[1])
    longitude = int(sys.argv[2])
    first_step = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    tile = CFG.Tile(latitude, longitude, "")
    tile.read_from_config()
    print("build directory:", tile.build_dir)
    print("default_website:", tile.default_website, "default_zl:", tile.default_zl)
    if not tile.default_website:
        raise SystemExit(
            "tile config resolves to an EMPTY default_website — step 4 would "
            "produce provider-less texture names; aborting before any work"
        )

    for step_number, (step_name, step) in enumerate((
        ("1 vector", VMAP.build_poly_file),
        ("2 mesh", MESH.build_mesh),
        ("3 masks", MASK.build_masks),
        ("4 tile", TILE.build_tile),
    ), start=1):
        if step_number < first_step:
            continue
        print(f"=== step {step_name} ===", flush=True)
        step(tile)
        # Ortho4XP's own build loop checks UI.red_flag, NOT return values —
        # a successful masks step returns None.
        if UI.red_flag:
            raise SystemExit(f"step {step_name} raised the red flag — stopping")
    print("all four steps complete")

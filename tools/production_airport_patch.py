"""Single-airport PRODUCTION patch rebuild — the same DEM the tile uses.

The standalone lab loop (``tools/full_airport_build.py`` /
``build_airport_pavement`` with no ``tile_dem``) loads the RAW base DEM:
no elevation insets, different smoothing — its patches can differ from
production in both values AND geometry (2026-07-18: a production-only
zero-length weld segment broke Triangle4XP while every standalone probe
patch was clean).  This tool instead runs the tile pipeline's own
prelude (``O4_Vector_Map.load_airports_and_prepare_dem`` — insets,
overlay, densification, airport smoothing) and then the production
generation call (``run_auto_patch_generation``) for ONE airport, by
removing that airport's patch so the freshness gate rebuilds exactly it.

The result is the REAL ``Patches/<...>/<ICAO>_auto.patch.osm``,
byte-identical to what a full tile build would write.  Expect the DEM
prelude to take a couple of minutes on an inset-heavy tile; the airport
build itself matches production timing.

Usage (from the checkout root):
    venv/bin/python tools/production_airport_patch.py ICAO LAT LON

Example:
    venv/bin/python tools/production_airport_patch.py EGGW 51 -1
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.append(os.path.join(ROOT, "src"))

import O4_File_Names as FNAMES

sys.path.append(FNAMES.Provider_dir)
import O4_Imagery_Utils as IMG
import O4_Vector_Map as VMAP
import O4_Config_Utils as CFG

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(2)
    icao = sys.argv[1].upper()
    latitude = int(sys.argv[2])
    longitude = int(sys.argv[3])

    IMG.initialize_extents_dict()
    IMG.initialize_color_filters_dict()
    IMG.initialize_providers_dict()
    IMG.initialize_combined_providers_dict()

    tile = CFG.Tile(latitude, longitude, "")
    tile.read_from_config()

    patch_path = os.path.join(
        FNAMES.patch_dir(tile.lat, tile.lon), f"{icao}_auto.patch.osm")
    if os.path.isfile(patch_path):
        os.remove(patch_path)
        print(f"removed stale {patch_path} (freshness gate now rebuilds it)")

    t0 = time.time()
    print("-> Production DEM prelude (insets + overlay + smoothing)")
    airport_layer, dico_airports = VMAP.load_airports_and_prepare_dem(tile)
    if airport_layer is None:
        print("ERROR: airports layer could not be loaded")
        raise SystemExit(1)
    print(f"   prelude done in {time.time() - t0:.0f}s; "
          "-> production auto-patch generation")
    VMAP.run_auto_patch_generation(tile, airport_layer, dico_airports)
    if os.path.isfile(patch_path):
        print(f"\nPRODUCTION PATCH {patch_path} "
              f"({os.path.getsize(patch_path)} bytes) "
              f"in {time.time() - t0:.0f}s total")
    else:
        print(f"\nERROR: {patch_path} was not produced — check the log "
              "above (CIFP path? auto_patch mode? airport not on tile?)")
        raise SystemExit(1)

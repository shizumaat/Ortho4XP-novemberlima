#!/usr/bin/env python3
import sys
import os

Ortho4XP_dir = '..' if getattr(sys, 'frozen', False) else '.'

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    _proj_data_path = os.path.join(sys._MEIPASS, "pyproj", "proj_dir", "share", "proj")
    _lib_path = os.path.join(sys._MEIPASS, "_internal")
    os.environ["PROJ_DATA"] = _proj_data_path
    os.environ["DYLD_LIBRARY_PATH"] = _lib_path + ":" + os.environ.get("DYLD_LIBRARY_PATH", "")

from pyproj import datadir

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    datadir.set_data_dir(_proj_data_path)

sys.path.append(os.path.join(Ortho4XP_dir, 'src'))

# JSON-lines engine transport (docs/specs/engine-protocol-multi-gui.md §5):
# a subprocess front end runs `Ortho4XP.py --engine-jsonl` and speaks the
# protocol over stdio.  Handle it here, BEFORE any GUI toolkit import below,
# so the engine-only path never pulls in Tkinter/PySide6.  The __name__
# guard is LOAD-BEARING: multiprocessing spawn helpers (the auto-patch
# airport pool and its Manager) re-import this module as "__mp_main__"
# with the parent's argv restored — without the guard the helper becomes
# a second engine server blocked on its pipe, the Manager handshake
# never completes, and the build wedges at zero CPU (live 3-tile run,
# 2026-07-17).
if __name__ == '__main__' and '--engine-jsonl' in sys.argv:
    from o4_engine import jsonl
    # owns_process: the transport bounds this process's life — front-end
    # death (stdin EOF, SIGTERM, ppid change) stops any in-flight build
    # and exits, so no orphan engine can keep building headless.
    jsonl.serve(sys.stdin, sys.stdout, owns_process=True)
    sys.exit(0)

import O4_File_Names as FNAMES
sys.path.append(FNAMES.Provider_dir)
import O4_Imagery_Utils as IMG
import O4_Vector_Map as VMAP
import O4_Mesh_Utils as MESH
import O4_Mask_Utils as MASK
import O4_Tile_Utils as TILE
import O4_GUI_Utils as GUI
import O4_Config_Utils as CFG  # CFG imported last because it can modify other modules variables

cmd_line = "USAGE: Ortho4XP.py lat lon imagery zl (won't read a tile config)\n  OR:  Ortho4XP.py lat lon (with existing tile config file)"

if __name__ == '__main__':
    if not os.path.isdir(FNAMES.Utils_dir):
        print("Missing ", FNAMES.Utils_dir, "directory, check your install. Exiting.")
        sys.exit()
    for directory in (FNAMES.Preview_dir, FNAMES.Provider_dir, FNAMES.Extent_dir, FNAMES.Filter_dir, FNAMES.OSM_dir,
                      FNAMES.Mask_dir, FNAMES.Imagery_dir, FNAMES.Elevation_dir, FNAMES.Geotiff_dir, FNAMES.Patch_dir,
                      FNAMES.Tile_dir, FNAMES.Tmp_dir):
        if not os.path.isdir(directory):
            try:
                os.makedirs(directory)
                print("Creating missing directory", directory)
            except:
                print("Could not create required directory", directory, ". Exit.")
                sys.exit()
    IMG.initialize_extents_dict()
    IMG.initialize_color_filters_dict()
    IMG.initialize_providers_dict()
    IMG.initialize_combined_providers_dict()
    if len(sys.argv) == 1:  # switch to the graphical interface
        Ortho4XP = GUI.Ortho4XP_GUI()
        Ortho4XP.mainloop()
        print("Bon vol!")
    else:  # sequel is only concerned with command line
        if len(sys.argv) < 3:
            print(cmd_line); sys.exit()
        try:
            lat = int(sys.argv[1])
            lon = int(sys.argv[2])
        except:
            print(cmd_line); sys.exit()
        if len(sys.argv) == 3:
            try:
                tile = CFG.Tile(lat, lon, '')
            except Exception as e:
                print(e)
                print("ERROR: could not read tile config file."); sys.exit()
        else:
            try:
                provider_code = sys.argv[3]
                zoomlevel = int(sys.argv[4])
                tile = CFG.Tile(lat, lon, '')
                tile.default_website = provider_code
                tile.default_zl = zoomlevel
            except:
                print(cmd_line); sys.exit()
        try:
            VMAP.build_poly_file(tile)
            MESH.build_mesh(tile)
            MASK.build_masks(tile)
            TILE.build_tile(tile)
            print("Bon vol!")
        except:
            print("Crash!")
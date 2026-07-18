import logging
import os
import time
import shutil
import queue
import threading
from collections import defaultdict
import O4_UI_Utils as UI
import O4_File_Names as FNAMES
import O4_Imagery_Utils as IMG
import O4_Vector_Map as VMAP
import O4_Mesh_Utils as MESH
import O4_Mask_Utils as MASK
import O4_DSF_Utils as DSF
import O4_Overlay_Utils as OVL
from O4_Parallel_Utils import (
    effective_convert_slots,
    effective_download_slots,
    parallel_launch,
    parallel_join,
)

max_download_slots = 1
max_convert_slots = 4

skip_downloads = False
skip_converts = False

################################################################################
def download_textures(
    tile,
    download_queue,
    convert_queue,
    workers=None,
    producer_done_event=None,
    stats=None,
):
    """``stats`` (optional dict) is filled with the final "done"/"failed"
    counts so the caller can distinguish a completed step from one that
    silently dropped textures (a permanently failed download is otherwise
    only visible as a per-texture console line)."""
    worker_count = max(
        1, workers or effective_download_slots(max_download_slots)
    )
    UI.vprint(1, f"-> Opening download queue with {worker_count} worker(s).")

    progress_lock = threading.Lock()
    progress_state = {"done": 0, "pending": 0, "failed": 0}
    attempts = defaultdict(int)
    interrupted = False
    max_attempts = 3

    def _update_progress_locked():
        finished = progress_state["done"] + progress_state["failed"]
        denom = (
            finished
            + progress_state["pending"]
            + download_queue.qsize()
        )
        UI.progress_bar(2, int(100 * finished / denom) if denom else 100)

    def _download_task(*attrs):
        nonlocal interrupted

        if UI.red_flag:
            interrupted = True
            return 0

        attrs = tuple(attrs)
        with progress_lock:
            progress_state["pending"] += 1
            _update_progress_locked()

        try:
            ok = IMG.build_jpeg_ortho(tile, *attrs)
        except Exception as err:
            UI.vprint(2, f"Download failed: {err}")
            ok = 0

        should_retry = False
        with progress_lock:
            progress_state["pending"] -= 1
            if ok:
                progress_state["done"] += 1
                attempts.pop(attrs, None)
            else:
                attempt = attempts[attrs] + 1
                attempts[attrs] = attempt
                should_retry = attempt < max_attempts and not UI.red_flag
                if not should_retry:
                    attempts.pop(attrs, None)
                    progress_state["failed"] += 1
            _update_progress_locked()

        if ok:
            # Color harmonization statistics are collected here, on the
            # download worker, so the target field is complete the moment
            # the last download lands (spec section 3.4).  The lock
            # attribute only exists when the tile build activated the
            # feature and its convert barrier.
            if getattr(tile, "color_harmonization_lock", None) is not None:
                IMG.collect_color_statistics_for_harmonization(tile, *attrs)
            convert_queue.put((tile, *attrs))
        elif should_retry:
            download_queue.put(attrs)
            with progress_lock:
                _update_progress_locked()

        if UI.red_flag:
            interrupted = True

        return 1 if ok else 0

    if producer_done_event is None:
        producer_done_event = threading.Event()
        producer_done_event.set()

    workers_list = parallel_launch(_download_task, download_queue, worker_count)

    while not producer_done_event.is_set() and not UI.red_flag:
        time.sleep(0.05)

    while not UI.red_flag:
        with progress_lock:
            pending = progress_state["pending"]
        if download_queue.empty() and pending == 0:
            break
        time.sleep(0.05)

    for _ in range(worker_count):
        download_queue.put("quit")

    parallel_join(workers_list)

    UI.progress_bar(2, 100)
    if stats is not None:
        stats["done"] = progress_state["done"]
        stats["failed"] = progress_state["failed"]
    if interrupted or UI.red_flag:
        UI.vprint(1, "Download process interrupted.")
        return 0
    if progress_state["failed"] and progress_state["done"]:
        UI.lvprint(
            0,
            "WARNING:",
            progress_state["failed"],
            "texture(s) failed to download after",
            max_attempts,
            "attempts — run step 3 again to retry the missing ones.",
        )
    if progress_state["done"]:
        UI.vprint(1, " *Download of textures completed.")
    return 1

################################################################################
def build_tile(tile):
    if UI.is_working:
        return 0
    UI.is_working = 1
    UI.red_flag = False
    UI.logprint(
        "Step 3 for tile lat=", tile.lat, ", lon=", tile.lon, ": starting."
    )
    UI.vprint(
        0,
        "\nStep 3 : Building DSF/Imagery for tile "
        + FNAMES.short_latlon(tile.lat, tile.lon)
        + " : \n--------\n",
    )

    if not os.path.isfile(FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)):
        UI.lvprint(
            0, "ERROR: A mesh file must first be constructed for the tile!"
        )
        UI.exit_message_and_bottom_line("")
        return 0

    timer = time.time()

    if (tile.default_website not in IMG.providers_dict
            and tile.default_website not in IMG.combined_providers_dict):
        UI.lvprint(
            0,
            "ERROR: imagery source '%s' is not a known provider — "
            "select an imagery source in the interface, or fix "
            "default_website in the tile config." % (tile.default_website,),
        )
        UI.exit_message_and_bottom_line("")
        return 0

    tile.write_to_config()

    if not IMG.initialize_local_combined_providers_dict(tile):
        UI.exit_message_and_bottom_line("")
        return 0

    try:
        if not os.path.exists(
            os.path.join(
                tile.build_dir,
                "Earth nav data",
                FNAMES.round_latlon(tile.lat, tile.lon),
            )
        ):
            os.makedirs(
                os.path.join(
                    tile.build_dir,
                    "Earth nav data",
                    FNAMES.round_latlon(tile.lat, tile.lon),
                )
            )
        if not os.path.isdir(os.path.join(tile.build_dir, "textures")):
            os.makedirs(os.path.join(tile.build_dir, "textures"))
        if UI.cleaning_level > 1 and not tile.grouped:
            for f in os.listdir(os.path.join(tile.build_dir, "textures")):
                if f[-4:] != ".png":
                    continue
                try:
                    os.remove(os.path.join(tile.build_dir, "textures", f))
                except:
                    pass
        if not tile.grouped:
            try:
                shutil.rmtree(os.path.join(tile.build_dir, "terrain"))
            except:
                pass
        if not os.path.isdir(os.path.join(tile.build_dir, "terrain")):
            os.makedirs(os.path.join(tile.build_dir, "terrain"))
    except Exception as e:
        UI.lvprint(0, "ERROR: Cannot create tile subdirectories.")
        UI.vprint(3, e)
        UI.exit_message_and_bottom_line("")
        return 0

    download_queue = queue.Queue()
    convert_queue = queue.Queue()

    download_launched = False
    convert_launched = False

    # Default X-Plane texture mode uses no orthophotos: build_dsf queues
    # nothing, so the imagery download/convert stage is a clean no-op for this
    # tile (see docs/specs/texture-mode-spec.md, work package 2).
    imagery_needed = getattr(tile, "texture_mode", "full_ortho") != "default_xplane"

    # build_dsf runs in its own thread; a raise there (e.g. default_xplane
    # mode with no Global Scenery DSF to read) must fail the tile loudly
    # instead of dying silently and letting the activation step below report
    # a misleading rename error.
    dsf_build_error: list[str] = []

    def _build_dsf_guarded() -> None:
        try:
            DSF.build_dsf(tile, download_queue)
        except Exception as exc:
            dsf_build_error.append(str(exc))
            UI.vprint(0, "ERROR during DSF construction:", str(exc))
            UI.red_flag = True

    build_dsf_thread = threading.Thread(target=_build_dsf_guarded)
    producer_done_event = threading.Event()

    download_stats = {}
    # workers=None: download_textures resolves the Auto count itself and
    # keeps re-resolving it mid-step (an explicit workers value pins it).
    download_thread = threading.Thread(
        target=download_textures,
        args=[
            tile,
            download_queue,
            convert_queue,
            None,
            producer_done_event,
            download_stats,
        ],
    )
    # Color harmonization needs every texture's statistics before any
    # conversion runs (the target field is a whole-tile consensus), so with
    # the feature on the convert workers are launched only after the
    # download thread joins; the convert queue accumulates in the meantime.
    # See docs/specs/color-harmonization-spec.md section 3.4.
    harmonization_active = (
        imagery_needed
        and not skip_downloads
        and not skip_converts
        and getattr(tile, "color_harmonization", False)
    )
    if harmonization_active:
        IMG.initialize_color_harmonization(tile)

    convert_workers = []

    def _launch_convert_workers():
        worker_count = effective_convert_slots(max_convert_slots)
        UI.vprint(
            1,
            "-> Opening convert queue and",
            worker_count,
            "conversion workers.",
        )
        convert_workers.extend(parallel_launch(
            IMG.convert_texture,
            convert_queue,
            worker_count,
            progress=dico_conv_progress,
        ))
        return convert_workers

    dico_conv_progress = {"done": 0, "bar": 3}
    build_dsf_thread.start()
    if not skip_downloads and imagery_needed:
        download_thread.start()
        download_launched = True
        if not skip_converts and not harmonization_active:
            _launch_convert_workers()
            convert_launched = True
    build_dsf_thread.join()
    producer_done_event.set()
    if download_launched:
        download_thread.join()
        if harmonization_active and not UI.red_flag:
            IMG.compute_color_harmonization_targets(tile)
            _launch_convert_workers()
            convert_launched = True
        if convert_launched:
            for _ in range(len(convert_workers)):
                convert_queue.put("quit")
            parallel_join(convert_workers)
            if UI.red_flag:
                UI.vprint(1, "DDS conversion process interrupted.")
            elif dico_conv_progress["done"] >= 1:
                UI.vprint(1, " *DDS conversion of textures completed.")
    if dsf_build_error:
        UI.exit_message_and_bottom_line("")
        return 0
    if (
        download_launched
        and not UI.red_flag
        and download_stats.get("failed")
        and not download_stats.get("done")
    ):
        UI.lvprint(
            0,
            "ERROR: every texture download failed (imagery source '%s') — "
            "the new DSF was not activated." % (tile.default_website,),
        )
        UI.exit_message_and_bottom_line("")
        return 0
    UI.vprint(1, " *Activating DSF file.")
    dsf_file_name = os.path.join(
        tile.build_dir,
        "Earth nav data",
        FNAMES.long_latlon(tile.lat, tile.lon) + ".dsf",
    )
    try:
        os.replace(dsf_file_name + ".tmp", dsf_file_name)
    except:
        UI.vprint(0, "ERROR : could not rename DSF file, tile is not active.")
    if UI.red_flag:
        UI.exit_message_and_bottom_line()
        return 0
    if UI.cleaning_level > 1:
        try:
            os.remove(FNAMES.alt_file(tile))
        except:
            pass
        try:
            os.remove(FNAMES.input_node_file(tile))
        except:
            pass
        try:
            os.remove(FNAMES.input_poly_file(tile))
        except:
            pass
        # The previous DSF generation (renamed to .bak by build_dsf before
        # the rewrite) is a rollback copy only; X-Plane never reads it.
        try:
            os.remove(dsf_file_name + ".bak")
        except OSError:
            pass
    if UI.cleaning_level > 2:
        try:
            os.remove(FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon))
        except:
            pass
        try:
            os.remove(FNAMES.apt_file(tile))
        except:
            pass
    if UI.cleaning_level > 1 and not tile.grouped:
        remove_unwanted_textures(tile)
    if UI.cleaning_level >= 1:
        remove_dsftool_dump_leftovers(tile)
    UI.timings_and_bottom_line(timer)
    UI.logprint(
        "Step 3 for tile lat=", tile.lat, ", lon=", tile.lon, ": normal exit."
    )
    return 1

################################################################################
def build_all(tile):
    UI.reset_total_elapsed()
    VMAP.build_poly_file(tile)
    if UI.red_flag:
        UI.exit_message_and_bottom_line("")
        return 0
    MESH.build_mesh(tile)
    if UI.red_flag:
        UI.exit_message_and_bottom_line("")
        return 0
    MASK.build_masks(tile)
    if UI.red_flag:
        UI.exit_message_and_bottom_line("")
        return 0
    build_tile(tile)
    tile_coords = FNAMES.short_latlon(tile.lat, tile.lon)
    if tile_coords in IMG.incomplete_imgs:
        UI.lvprint(
            1,
            f"Attempting to rebuild textures with white squares: "
            f"{IMG.incomplete_imgs[tile_coords]}",
        )
        delete_incomplete_imgs(tile)
        build_tile(tile)
    if UI.red_flag:
        UI.exit_message_and_bottom_line("")
        return 0
    UI.is_working = 0
    UI.total_bottom_line(tile.lat, tile.lon)
    if IMG.incomplete_imgs:
        UI.lvprint(
            0,
            f"\nERROR: Parts of the following images could not be obtained "
            f"and have been filled with white: {IMG.incomplete_imgs}",
        )
    return 1

################################################################################
def build_tile_list(
    tile, list_lat_lon, do_osm, do_mesh, do_mask, do_dsf, do_ovl, override_cfg
):
    if UI.is_working:
        return 0
    UI.red_flag = 0
    timer = time.time()
    UI.lvprint(
        0, "Batch build launched for a number of", len(list_lat_lon), "tiles."
    )
    k = 0
    for (lat, lon) in list_lat_lon:
        k += 1
        UI.reset_total_elapsed()
        UI.vprint(
            1,
            "Dealing with tile ",
            k,
            "/",
            len(list_lat_lon),
            ":",
            FNAMES.short_latlon(lat, lon),
        )
        (tile.lat, tile.lon) = (lat, lon)
        tile.build_dir = FNAMES.build_dir(
            tile.lat, tile.lon, tile.custom_build_dir
        )
        tile.dem = None
        if override_cfg:
            tile.read_from_config(use_global=True)
        else:
            tile.read_from_config()
        if do_osm or do_mesh or do_dsf:
            tile.make_dirs()
        if do_osm:
            VMAP.build_poly_file(tile)
            if UI.red_flag:
                UI.exit_message_and_bottom_line()
                return 0
        if do_mesh:
            MESH.build_mesh(tile)
            if UI.red_flag:
                UI.exit_message_and_bottom_line()
                return 0
        if do_mask:
            MASK.build_masks(tile)
            if UI.red_flag:
                UI.exit_message_and_bottom_line()
                return 0
        if do_dsf:
            tile_coords = FNAMES.short_latlon(lat, lon)
            build_tile(tile)
            if tile_coords in IMG.incomplete_imgs:
                UI.lvprint(
                    1,
                    f"Attempting to rebuild textures with white squares: "
                    f"{IMG.incomplete_imgs[tile_coords]}",
                )
                delete_incomplete_imgs(tile)
                build_tile(tile)
            if UI.red_flag:
                UI.exit_message_and_bottom_line()
                return 0
        if do_ovl:
            OVL.build_overlay(lat, lon)
            if UI.red_flag:
                UI.exit_message_and_bottom_line()
                return 0
        UI.total_bottom_line(lat, lon)
        try:
            UI.gui.earth_window.canvas.delete(
                UI.gui.earth_window.dico_tiles_todo[(lat, lon)]
            )
            UI.gui.earth_window.dico_tiles_todo.pop((lat, lon), None)
        except:
            pass
    UI.lvprint(
        0, "Batch process completed in", UI.nicer_timer(time.time() - timer)
    )
    if IMG.incomplete_imgs:
        UI.lvprint(
            0,
            f"\nERROR: Parts of the following images could not be obtained "
            f"and have been filled with white: {IMG.incomplete_imgs}",
        )
    return 1

################################################################################
def remove_unwanted_textures(tile):
    """Delete .dds textures no longer referenced by any terrain file.

    Terrain file names encode their texture: ``<tex>.ter``,
    ``<tex>_sea.ter``, ``<tex>_water.ter``, and (with or without those)
    an ``_overlay`` suffix — all reference ``<tex>.dds``.  Fade-mask and
    transition ``.png`` files are never touched.
    """
    terrain_dir = os.path.join(tile.build_dir, "terrain")
    textures_dir = os.path.join(tile.build_dir, "textures")
    if not os.path.isdir(terrain_dir) or not os.path.isdir(textures_dir):
        return
    texture_list = []
    for f in os.listdir(terrain_dir):
        if f[-4:] != ".ter":
            continue
        stem = f[:-4]
        for suffix in ("_overlay", "_sea", "_water"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        texture_list.append(stem + ".dds")
    for f in os.listdir(textures_dir):
        if f[-4:] != ".dds":
            continue
        if f not in texture_list:
            print("Removing obsolete texture", f)
            try:
                os.remove(os.path.join(textures_dir, f))
            except:
                pass


def remove_dsftool_dump_leftovers(tile):
    """Remove DSFTool decompile artifacts from the pack's Earth nav data.

    ``DSFTool --dsf2text`` writes a ``<dsf>.text`` dump (hundreds of
    megabytes) plus ``.raw`` raster sidecars next to its input; none of
    them is ever read by X-Plane.  Current code caches dumps under
    ``FNAMES.Default_dsf_cache_dir`` instead, so anything matching these
    patterns inside the pack is a leftover from an earlier run.
    """
    nav_data_dir = os.path.join(tile.build_dir, "Earth nav data")
    for dirpath, _, filenames in os.walk(nav_data_dir):
        for f in filenames:
            if ".dsf.text" in f:
                try:
                    os.remove(os.path.join(dirpath, f))
                    UI.vprint(2, "   Removed DSFTool dump leftover", f)
                except OSError:
                    pass

def delete_incomplete_imgs(tile):
    """Delete orthophoto jpegs and dds that have white squares."""
    tile_coords = FNAMES.short_latlon(tile.lat, tile.lon)
    if tile_coords not in IMG.incomplete_imgs:
        return
    file_name_list = IMG.incomplete_imgs[tile_coords]
    for file_name in file_name_list:
        # Delete the orthophoto jpegs with white squares
        for root, _, files in os.walk(FNAMES.Imagery_dir):
            if file_name in files:
                file_path = os.path.join(root, file_name)
                os.remove(file_path)
                UI.lvprint(1, f"Deleted: {file_name} in {file_path}")

        # Delete the tile dds textures with white squares
        # file_name has .jpg extension, so create a variable for .dds extension as well
        base_name, _ = os.path.splitext(file_name)
        file_name_dds = f"{base_name}.dds"
        for root, _, files in os.walk(tile.build_dir):
            if file_name_dds in files:
                file_path = os.path.join(root, file_name_dds)
                os.remove(file_path)
                UI.lvprint(1, f"Deleted: {file_name_dds} in {file_path}")

    IMG.incomplete_imgs.pop(tile_coords, None)

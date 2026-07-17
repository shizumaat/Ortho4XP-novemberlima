#!/usr/bin/env python3
"""A REAL engine child process for the lifecycle tests
(tests/test_engine_child_lifecycle.py).

Unlike ``stub_engine_worker.py`` (a scripted protocol impersonator for
the scheduler tests), this harness runs the genuine transport and
session — ``o4_engine.jsonl.serve`` in ``owns_process`` mode, exactly as
the ``--engine-jsonl`` entry points do — with only the heavy pipeline
step modules replaced by stubs, so the tests exercise the true
process-lifecycle code paths (stdin end-of-file, terminate signal,
parent-death watchdog) headlessly and fast.

The stubbed vector step runs for ``SLOW_STEP_SECONDS`` (default 120),
polling ``O4_UI_Utils.red_flag`` every 50 ms like a cooperative pipeline
step; set ``SLOW_STEP_IGNORES_RED_FLAG`` to make it deaf to the flag so
a test can prove the hard exit deadline
(``O4_ENGINE_SHUTDOWN_GRACE_SECONDS``) fires even for a step that never
honors the polled cancellation contract.
"""

import os
import sys
import time
import types

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                    "src"))

import O4_UI_Utils as UI  # noqa: E402

SLOW_STEP_SECONDS = float(os.environ.get("SLOW_STEP_SECONDS", "120"))
IGNORES_RED_FLAG = bool(os.environ.get("SLOW_STEP_IGNORES_RED_FLAG"))


class _StubTile:
    def __init__(self, lat, lon, custom_build_dir):
        self.lat = lat
        self.lon = lon
        self.custom_build_dir = custom_build_dir
        self.default_website = ""
        self.default_zl = 16

    def read_from_config(self):
        pass

    def make_dirs(self):
        pass


def _slow_vector_step(tile):
    deadline = time.time() + SLOW_STEP_SECONDS
    while time.time() < deadline:
        if UI.red_flag and not IGNORES_RED_FLAG:
            return 0
        time.sleep(0.05)
    return 1


def _install_stub_pipeline():
    """Replace the heavy pipeline modules the session's build worker (and
    the transport's registry initialization) would import."""
    config_module = types.ModuleType("O4_Config_Utils")
    config_module.Tile = _StubTile
    vector_module = types.ModuleType("O4_Vector_Map")
    vector_module.build_poly_file = _slow_vector_step
    mesh_module = types.ModuleType("O4_Mesh_Utils")
    mesh_module.build_mesh = lambda tile: 1
    mask_module = types.ModuleType("O4_Mask_Utils")
    mask_module.build_masks = lambda tile: 1
    tile_module = types.ModuleType("O4_Tile_Utils")
    tile_module.build_tile = lambda tile: 1
    overlay_module = types.ModuleType("O4_Overlay_Utils")
    overlay_module.build_overlay = lambda lat, lon: 1
    imagery_module = types.ModuleType("O4_Imagery_Utils")
    imagery_module.initialize_extents_dict = lambda: None
    imagery_module.initialize_color_filters_dict = lambda: None
    imagery_module.initialize_providers_dict = lambda: None
    imagery_module.initialize_combined_providers_dict = lambda: None
    imagery_module.shared_tile_cache_dir = ""
    for module in (config_module, vector_module, mesh_module, mask_module,
                   tile_module, overlay_module, imagery_module):
        sys.modules[module.__name__] = module


def main():
    _install_stub_pipeline()
    from o4_engine import jsonl

    jsonl.serve(sys.stdin, sys.stdout, owns_process=True)
    sys.exit(0)


if __name__ == "__main__":
    main()

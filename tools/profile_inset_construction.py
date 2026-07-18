"""Phase-attribute a COLD airport elevation inset construction.

Drives :func:`O4_Airport_Elevation_Insets.ensure_airport_insets` for a set of
airport bounding boxes (the OTHH / Doha, Qatar set by default) with a scratch
``Elevation_data`` directory, wrapping the module's phase functions with
wall-clock timers so the ~15 minute cold build can be attributed:

    discover (HEAD existence probes) | warp (windowed /vsicurl S3 download +
    resample) | osm_footprint_fetch (Overpass) | buffer | rasterize |
    inpaint (gdal.FillNodata or the distance-transform fill)

It IMPORTS the module (never edits it) and runs the exact orchestration the
tile build performs.  The scratch cache is written under
``--elevation-data-dir`` (default ``/tmp/inset_prof/Elevation_data``) so the
real, symlinked ``Elevation_data`` is never touched; ``--fresh`` deletes that
scratch subtree first for a genuinely cold run.

Usage:
    venv/bin/python tools/profile_inset_construction.py --fresh \\
        --out /tmp/inset_profile_before.txt

The default airport set is representative of the Qatar tile +25+051 build
(one large hub, one medium field, two small fields), all inside the single
Copernicus GLO-30 cell N25/E051.  Boxes are RAW boundary bounds; the standard
2000 m inset margin is added here exactly as ``_airport_bounding_boxes`` does.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import sys
import time

_TOOLS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
_SOURCE_DIRECTORY = os.path.join(os.path.dirname(_TOOLS_DIRECTORY), "src")
if _SOURCE_DIRECTORY not in sys.path:
    sys.path.insert(0, _SOURCE_DIRECTORY)

import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO
import O4_Airport_Elevation_Insets as INSETS

# Raw airport boundary bounds (WEST, SOUTH, EAST, NORTH) in EPSG:4326.
# Representative of the tile +25+051 (Qatar) inset set; all in cell N25/E051.
DEFAULT_AIRPORTS = {
    "OTHH": (51.590, 25.235, 51.640, 25.300),   # Hamad International (large)
    "OTBT": (51.540, 25.250, 51.575, 25.280),   # Doha-area field (medium)
    "OTR6": (51.495, 25.297, 51.510, 25.308),   # small field
    "QatarRCSports": (51.440, 25.420, 51.446, 25.426),  # tiny model field
}

TILE_LAT = 25
TILE_LON = 51

# phase name -> [total_seconds, call_count]
_PHASE_TOTALS: dict[str, list] = {}
# per (airport-ish, phase) not tracked separately; the warp/mask calls carry
# no airport label, so we attribute by call order in the printed table.
_EVENTS: list[tuple] = []


def _timed(phase_name, function):
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - start
            bucket = _PHASE_TOTALS.setdefault(phase_name, [0.0, 0])
            bucket[0] += elapsed
            bucket[1] += 1
            _EVENTS.append((phase_name, elapsed))
    return wrapper


def _install_phase_timers():
    INSETS.warp_vsicurl_sources_to_geotiff = _timed(
        "warp_download", INSETS.warp_vsicurl_sources_to_geotiff
    )
    INSETS.openstreetmap_building_footprints = _timed(
        "osm_footprint_fetch", INSETS.openstreetmap_building_footprints
    )
    INSETS._buffer_footprints_in_metres = _timed(
        "buffer_footprints", INSETS._buffer_footprints_in_metres
    )
    INSETS._rasterize_footprint_mask = _timed(
        "rasterize_mask", INSETS._rasterize_footprint_mask
    )
    # Discovery existence probe (HEAD) — memoised, so only the first per cell
    # actually hits the network.
    INSETS.DegreeNamedCogStrategy._url_exists = _timed(
        "discover_head", INSETS.DegreeNamedCogStrategy._url_exists
    )
    # The inpaint call itself (whichever fill the module ends up using).
    if hasattr(INSETS, "gdal") and INSETS.gdal is not None:
        INSETS.gdal.FillNodata = _timed(
            "inpaint_fillnodata", INSETS.gdal.FillNodata
        )
    # The new distance-transform fill, if present.
    if hasattr(INSETS, "_fill_masked_by_distance_transform"):
        INSETS._fill_masked_by_distance_transform = _timed(
            "inpaint_distance_transform",
            INSETS._fill_masked_by_distance_transform,
        )


def _margined_boxes(raw_boxes, margin_m):
    metres_per_degree_latitude = GEO.lat_to_m
    metres_per_degree_longitude = GEO.lon_to_m(TILE_LAT + 0.5)
    margin_lon = margin_m / metres_per_degree_longitude
    margin_lat = margin_m / metres_per_degree_latitude
    result = {}
    for icao, (west, south, east, north) in raw_boxes.items():
        result[icao] = (
            west - margin_lon,
            south - margin_lat,
            east + margin_lon,
            north + margin_lat,
        )
    return result


def _box_pixels(box, resolution_m):
    (west, south, east, north) = box
    centre_latitude = (south + north) / 2.0
    width_m = (east - west) * GEO.lon_to_m(centre_latitude)
    height_m = (north - south) * GEO.lat_to_m
    return (
        int(round(width_m / resolution_m)),
        int(round(height_m / resolution_m)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--elevation-data-dir", default="/tmp/inset_prof/Elevation_data"
    )
    parser.add_argument("--margin-m", type=float, default=2000.0)
    parser.add_argument("--resolution-m", type=float, default=3.0)
    parser.add_argument("--fresh", action="store_true",
                        help="delete the scratch cache subtree first")
    parser.add_argument("--out", default=None)
    parser.add_argument("--provider", default="auto")
    arguments = parser.parse_args()

    scratch = os.path.abspath(arguments.elevation_data_dir)
    if arguments.fresh and os.path.isdir(scratch):
        shutil.rmtree(scratch)
    os.makedirs(scratch, exist_ok=True)
    FNAMES.Elevation_dir = scratch

    if not INSETS.has_gdal:
        print("ERROR: GDAL bindings unavailable.")
        return 2

    _install_phase_timers()

    provider_definitions = INSETS.select_provider_definitions(
        arguments.provider
    )
    print("Providers considered:",
          ", ".join(d["code"] for d in provider_definitions))

    boxes = _margined_boxes(DEFAULT_AIRPORTS, arguments.margin_m)
    lines = []
    lines.append("Airport bounding boxes (with %.0f m margin), resolution "
                 "%.1f m:" % (arguments.margin_m, arguments.resolution_m))
    for icao, box in boxes.items():
        (px, py) = _box_pixels(box, arguments.resolution_m)
        lines.append("  %-14s %d x %d px  (%.1f Mpx)  box=%s"
                     % (icao, px, py, px * py / 1e6,
                        tuple(round(v, 5) for v in box)))

    wall_start = time.perf_counter()
    INSETS.ensure_airport_insets(
        TILE_LAT,
        TILE_LON,
        boxes,
        provider_definitions,
        arguments.resolution_m,
        refresh=True,
    )
    wall_total = time.perf_counter() - wall_start

    lines.append("")
    lines.append("=" * 64)
    lines.append("PHASE ATTRIBUTION (cold)   wall total = %.1f s" % wall_total)
    lines.append("=" * 64)
    lines.append("%-30s %10s %6s %8s" %
                 ("phase", "seconds", "calls", "% wall"))
    lines.append("-" * 64)
    accounted = 0.0
    for phase in sorted(_PHASE_TOTALS, key=lambda p: -_PHASE_TOTALS[p][0]):
        seconds, count = _PHASE_TOTALS[phase]
        accounted += seconds
        lines.append("%-30s %10.2f %6d %7.1f%%" %
                     (phase, seconds, count, 100.0 * seconds / wall_total))
    lines.append("-" * 64)
    lines.append("%-30s %10.2f %6s %7.1f%%" %
                 ("(sum of timed phases)", accounted, "",
                  100.0 * accounted / wall_total))
    lines.append("%-30s %10.2f %6s %7.1f%%" %
                 ("(unattributed / overhead)", wall_total - accounted, "",
                  100.0 * (wall_total - accounted) / wall_total))

    # Per-inset masked-pixel summary from the provenance sidecars.
    lines.append("")
    lines.append("Per-inset masked-pixel summary:")
    for icao in DEFAULT_AIRPORTS:
        for definition in provider_definitions:
            prov_path = FNAMES.airport_inset_provenance(
                TILE_LAT, TILE_LON, icao, definition["code"])
            if os.path.isfile(prov_path):
                import json
                with open(prov_path) as handle:
                    prov = json.load(handle)
                summary = prov.get("surface_model_building_masking", {})
                lines.append("  %-14s %-14s footprints=%s masked_px=%s "
                             "frac=%s"
                             % (icao, definition["code"],
                                summary.get("footprint_count"),
                                summary.get("masked_pixel_count"),
                                summary.get("masked_fraction")))
                break

    report = "\n".join(lines)
    print(report)
    if arguments.out:
        with open(arguments.out, "w") as handle:
            handle.write(report + "\n")
        print("\nWrote", arguments.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

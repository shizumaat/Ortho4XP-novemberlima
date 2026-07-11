"""Fetch and cache a high-resolution airport elevation inset from the terminal.

Standalone command-line front end to
``src/O4_Airport_Elevation_Insets.py``.  It runs the SAME declarative
provider discovery + access-strategy fetch + cache/index/provenance chain
the tile build performs (this tool IMPORTS the module, never edits it), so a
human can pre-warm or refresh the ``Elevation_data/`` inset cache for one
airport and inspect the result before a full tile build.

The cache is written under ``Elevation_data/<block>/<tile>_airport_insets/``
relative to the current working directory (Ortho4XP's standard resource
root), exactly where the build looks for it.  ``--elevation-data-dir`` points
the cache elsewhere (for example a specific worktree checkout).

What it does
------------
1. Resolves the target 1 degree tile from ``--tile`` or the bounding-box
   centre.
2. Selects providers via ``airport_elevation_providers`` semantics
   (``auto`` = every enabled ``role=airport_inset`` definition by priority,
   or an explicit comma-separated list of provider codes).
3. Runs discovery + fetch for the airport bounding box, writing the
   EPSG:4326 float32 GeoTIFF, its provenance sidecar, and the tile
   ``index.json`` (negative results included).
4. Prints the cache paths, the provenance, and -- when ``gdallocationinfo``
   is on the PATH -- a bilinear elevation sample at an optional probe point.

Requires the GDAL python bindings (osgeo) and network access.

Usage:
    venv/bin/python tools/fetch_airport_elevation_insets.py \\
        --airport ICAO --bbox WEST,SOUTH,EAST,NORTH [options]

Options:
    --airport ICAO            Airport identifier used as the cache key (required).
    --bbox W,S,E,N            Bounding box in EPSG:4326 degrees (required).
    --tile LAT,LON            Integer tile corner; default = bbox-centre floor.
    --provider CODES          "auto" (default) or a comma-separated code list.
    --resolution-m FLOAT      Warp target resolution in metres (default 3.0).
    --refresh                 Ignore cached results and re-query/re-fetch.
    --probe LAT,LON           Sample the fetched raster at this point and print it.
    --elevation-data-dir DIR  Write the cache under DIR instead of ./Elevation_data.

Examples:
    # Nashville (KBNA), tile +36-087, a box around the water-treatment shelf:
    venv/bin/python tools/fetch_airport_elevation_insets.py \\
        --airport KBNA --tile 36,-87 \\
        --bbox -86.72,36.10,-86.62,36.16 \\
        --probe 36.1376,-86.6759

    # Pin an explicit provider and force a refresh:
    venv/bin/python tools/fetch_airport_elevation_insets.py \\
        --airport KBNA --bbox -86.72,36.10,-86.62,36.16 \\
        --provider USGS3DEP --refresh
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys

_TOOLS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
_SOURCE_DIRECTORY = os.path.join(
    os.path.dirname(_TOOLS_DIRECTORY), "src"
)
if _SOURCE_DIRECTORY not in sys.path:
    sys.path.insert(0, _SOURCE_DIRECTORY)

import O4_File_Names as FNAMES
import O4_Airport_Elevation_Insets as INSETS


def _parse_pair(text, label):
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            label + " must be two comma-separated numbers, got: " + text
        )
    return (float(parts[0]), float(parts[1]))


def _parse_bounding_box(text):
    parts = text.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "--bbox must be WEST,SOUTH,EAST,NORTH, got: " + text
        )
    return tuple(float(part) for part in parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch one airport elevation inset into the cache.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--airport", required=True, help="cache-key identifier")
    parser.add_argument(
        "--bbox",
        required=True,
        type=_parse_bounding_box,
        help="WEST,SOUTH,EAST,NORTH in EPSG:4326 degrees",
    )
    parser.add_argument(
        "--tile",
        default=None,
        help="integer tile corner LAT,LON (default: bbox-centre floor)",
    )
    parser.add_argument("--provider", default="auto")
    parser.add_argument("--resolution-m", type=float, default=3.0)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--probe",
        default=None,
        help="LAT,LON to sample from the fetched raster",
    )
    parser.add_argument("--elevation-data-dir", default=None)
    arguments = parser.parse_args()

    if arguments.elevation_data_dir:
        FNAMES.Elevation_dir = os.path.abspath(arguments.elevation_data_dir)

    if not INSETS.has_gdal:
        print(
            "ERROR: the GDAL python bindings (osgeo) are unavailable; "
            "install them to fetch elevation insets."
        )
        return 2

    (west, south, east, north) = arguments.bbox
    if arguments.tile:
        (tile_latitude_raw, tile_longitude_raw) = _parse_pair(
            arguments.tile, "--tile"
        )
        tile_latitude = int(math.floor(tile_latitude_raw))
        tile_longitude = int(math.floor(tile_longitude_raw))
    else:
        tile_latitude = int(math.floor((south + north) / 2.0))
        tile_longitude = int(math.floor((west + east) / 2.0))

    provider_definitions = INSETS.select_provider_definitions(
        arguments.provider
    )
    if not provider_definitions:
        print(
            "ERROR: no airport-inset providers matched",
            repr(arguments.provider),
        )
        return 2
    print(
        "Providers (in order):",
        ", ".join(
            definition["code"] for definition in provider_definitions
        ),
    )
    print(
        "Tile: {:+d},{:+d}   bbox: {}".format(
            tile_latitude, tile_longitude, arguments.bbox
        )
    )

    index = INSETS.ensure_airport_insets(
        tile_latitude,
        tile_longitude,
        {arguments.airport: arguments.bbox},
        provider_definitions,
        arguments.resolution_m,
        refresh=arguments.refresh,
    )

    print("\nindex.json entry:")
    print(json.dumps(index.get(arguments.airport, {}), indent=2))

    fetched_path = None
    for definition in provider_definitions:
        candidate = FNAMES.airport_inset_dem(
            tile_latitude,
            tile_longitude,
            arguments.airport,
            definition["code"],
        )
        if os.path.isfile(candidate):
            fetched_path = candidate
            provenance_path = FNAMES.airport_inset_provenance(
                tile_latitude,
                tile_longitude,
                arguments.airport,
                definition["code"],
            )
            print("\nCached GeoTIFF:", candidate)
            if os.path.isfile(provenance_path):
                print("Provenance:", provenance_path)
                with open(provenance_path, "r") as handle:
                    print(json.dumps(json.load(handle), indent=2))
            break

    if fetched_path is None:
        print("\nNo inset was fetched (no provider reported coverage).")
        return 1

    if arguments.probe:
        (probe_latitude, probe_longitude) = _parse_pair(
            arguments.probe, "--probe"
        )
        _print_probe_sample(fetched_path, probe_latitude, probe_longitude)

    return 0


def _print_probe_sample(raster_path, latitude, longitude):
    """Print a bilinear elevation sample via gdallocationinfo, if available."""
    executable = shutil.which("gdallocationinfo")
    if not executable:
        print(
            "\n(gdallocationinfo not on PATH; skipping the probe sample.)"
        )
        return
    command = [
        executable,
        "-wgs84",
        "-b",
        "1",
        "-valonly",
        raster_path,
        repr(longitude),
        repr(latitude),
    ]
    try:
        output = subprocess.check_output(
            command, stderr=subprocess.STDOUT
        ).decode()
    except subprocess.CalledProcessError as error:
        print("\nProbe failed:", error.output.decode())
        return
    print(
        "\nProbe at (lat={}, lon={}): {} m".format(
            latitude, longitude, output.strip()
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""Acceptance + phase benchmark for the airport-inset masked-hole fill.

Fetches the building footprints ONCE (from local extracts when they cover
the box, else Overpass), rasterizes the true building mask, then applies
BOTH fills to the same fetched Copernicus GLO-30 inset raster:

  * the legacy ``gdal.FillNodata`` inpaint, and
  * the default vectorized distance-transform fill,

and reports, on the TRUE building (masked) set:

  * per-fill wall time and the footprint-fetch / rasterize time;
  * masked-cell agreement -- mean / max ``|new - legacy|`` over the masked
    cells (the elevation change under buildings);
  * the byte-identity guarantee on UNMASKED cells: both fills must leave
    every non-building cell exactly equal to the fetched original.

It imports the module (never edits it) and does not mutate the input tif.

Usage:
    venv/bin/python tools/inset_fill_acceptance.py \\
        --tif .../OTHH_copernicusglo30.tif --bbox W,S,E,N
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy
from osgeo import gdal

import O4_Airport_Elevation_Insets as INSETS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tif", required=True)
    parser.add_argument("--bbox", required=True, help="W,S,E,N degrees")
    parser.add_argument("--out", default=None)
    arguments = parser.parse_args()
    (west, south, east, north) = (float(v) for v in arguments.bbox.split(","))
    bbox = (west, south, east, north)
    definition = {
        "code": "COPERNICUSGLO30", "footprint_mask_buffer_m": "35",
        "surface_model_building_masking": True,
    }
    buffer_m = 35.0
    centre_latitude = (south + north) / 2.0

    # --- footprint fetch + mask rasterize (the dominant cold-build phase) ---
    t = time.perf_counter()
    footprints, source_label = INSETS._collect_inset_building_footprints(
        bbox, definition)
    fetch_seconds = time.perf_counter() - t

    dataset = gdal.Open(arguments.tif)
    original = dataset.GetRasterBand(1).ReadAsArray().astype(numpy.float64)
    nodata = dataset.GetRasterBand(1).GetNoDataValue()
    if nodata is None:
        nodata = -32768.0

    t = time.perf_counter()
    buffered = INSETS._buffer_footprints_in_metres(
        footprints, buffer_m, centre_latitude)
    building_mask = INSETS._rasterize_footprint_mask(buffered, dataset)
    rasterize_seconds = time.perf_counter() - t
    dataset = None

    genuine_nodata = (original == nodata) | ~numpy.isfinite(original)
    source_mask = ~building_mask & ~genuine_nodata
    masked = building_mask & ~genuine_nodata  # the cells a fill must change

    # --- legacy gdal.FillNodata (in a scratch MEM raster) ---
    t = time.perf_counter()
    mem = gdal.GetDriverByName("MEM").Create(
        "", original.shape[1], original.shape[0], 1, gdal.GDT_Float32)
    band = mem.GetRasterBand(1)
    band.WriteArray(original.astype(numpy.float32))
    smask = gdal.GetDriverByName("MEM").Create(
        "", original.shape[1], original.shape[0], 1, gdal.GDT_Byte)
    smask.GetRasterBand(1).WriteArray(source_mask.astype(numpy.uint8) * 255)
    gdal.FillNodata(targetBand=band, maskBand=smask.GetRasterBand(1),
                    maxSearchDist=100.0, smoothingIterations=2)
    legacy = band.ReadAsArray().astype(numpy.float64)
    legacy[genuine_nodata] = nodata
    legacy_seconds = time.perf_counter() - t
    mem = None
    smask = None

    # --- new distance-transform fill ---
    t = time.perf_counter()
    new = INSETS._fill_masked_by_distance_transform(
        original, source_mask, smoothing_iterations=2)
    new[genuine_nodata] = nodata
    new_seconds = time.perf_counter() - t

    delta = numpy.abs(new - legacy)
    masked_delta = delta[masked]

    lines = []
    lines.append("Inset fill acceptance -- %s" %
                 os.path.basename(arguments.tif))
    lines.append("  raster %s  (%d cells)  footprints=%d  source=%s"
                 % (original.shape, original.size, len(footprints),
                    source_label))
    lines.append("  masked (building) cells: %d  (%.3f%% of raster)"
                 % (int(masked.sum()), 100.0 * masked.sum() / original.size))
    if masked_delta.size:
        lines.append("  |new - legacy| on MASKED cells:  mean=%.4f m  "
                     "max=%.4f m  median=%.4f m  p99=%.4f m"
                     % (float(masked_delta.mean()), float(masked_delta.max()),
                        float(numpy.median(masked_delta)),
                        float(numpy.percentile(masked_delta, 99))))
    # Byte-identity on UNMASKED cells: both fills leave non-building cells
    # exactly equal to the fetched original.
    unmasked = ~masked
    legacy_unmasked_changed = int(numpy.count_nonzero(
        legacy[unmasked] != original[unmasked]))
    new_unmasked_changed = int(numpy.count_nonzero(
        new[unmasked] != original[unmasked]))
    lines.append("  UNMASKED cells changed vs original: legacy=%d %s   "
                 "new=%d %s"
                 % (legacy_unmasked_changed,
                    "OK" if legacy_unmasked_changed == 0 else "FAIL",
                    new_unmasked_changed,
                    "OK" if new_unmasked_changed == 0 else "FAIL"))
    lines.append("")
    lines.append("  Phase times (seconds):")
    lines.append("    footprint_fetch (%s)  %8.2f"
                 % (source_label[:24], fetch_seconds))
    lines.append("    rasterize_mask                %8.2f" % rasterize_seconds)
    lines.append("    inpaint_gdal_fillnodata       %8.2f" % legacy_seconds)
    lines.append("    inpaint_distance_transform    %8.2f" % new_seconds)

    report = "\n".join(lines)
    print(report)
    if arguments.out:
        with open(arguments.out, "w") as handle:
            handle.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

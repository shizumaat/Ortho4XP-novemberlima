"""Convert an MSFS airport scenery package into an X-Plane Custom Scenery pack.

Orchestrates the full "Convert MSFS airport" flow used by the Qt GUI
Tools menu (and usable headless):

  1. read the MSFS package: carve glTF models (with their GUIDs) out of
     the compiled model-library BGLs and parse object placements out of
     the scenery BGLs          -> O4_MSFS_Package (models + placements);
  2. convert each placed model to X-Plane OBJ8 objects + PNG textures
     -> tools/msfs_to_obj8 (one OBJ8 per material, winding auto-detect);
  3. create a new pack folder in Custom Scenery, copy the airport's
     apt.dat block out of Global Airports, write an overlay DSF that
     places every converted object and excludes the default gateway 3D
     (sim/exclude_obj|fac|agp rectangles) where the MSFS objects go
     -> O4_MSFS_XPlane_Pack.

The Global Airports gateway files are never edited: default facades and
objects are suppressed with exclusion zones in the new pack's own
overlay DSF, which is the standard, reversible X-Plane mechanism.

Core module: no GUI-toolkit imports. Progress is reported through
O4_UI_Utils.progress_bar plus stdout prints, and the polled
O4_UI_Utils.red_flag cancels between steps.

Converted third-party scenery is for PERSONAL USE unless the original
author grants redistribution rights.

Build-time impact: none - not part of the tile build pipeline.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import O4_File_Names as FNAMES
import O4_MSFS_Package as MSFS_PKG
import O4_MSFS_XPlane_Pack as XP_PACK
import O4_UI_Utils as UI

_TOOLS_DIRECTORY = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIRECTORY))
from msfs_to_obj8 import convert as msfs_convert  # noqa: E402


@dataclass
class ConversionReport:
    """Everything the UI needs to show when a conversion finishes."""

    package_path: Path
    airport_icao: Optional[str]
    models_converted: int
    objects_written: int
    placements_written: int
    placements_skipped: int
    exclusion_rectangles: int
    apt_dat_copied: bool
    warnings: List[str] = field(default_factory=list)


def convert_msfs_airport(
    msfs_package_directory: str | Path,
    custom_scenery_directory: str | Path,
    global_airports_directory: str | Path,
    dsftool_path: str | Path,
    package_name: Optional[str] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> ConversionReport:
    """Run the whole conversion; returns a ConversionReport.

    ``progress_callback(percent, message)`` is optional on top of the
    O4_UI_Utils reporting so the Qt worker can mirror progress natively.
    """
    msfs_package_directory = Path(msfs_package_directory)
    custom_scenery_directory = Path(custom_scenery_directory)
    global_airports_directory = Path(global_airports_directory)
    warnings: List[str] = []

    def report(percent: int, message: str) -> None:
        UI.vprint(1, message)
        UI.progress_bar(1, percent)
        if progress_callback is not None:
            progress_callback(percent, message)

    # ------------------------------------------------------------------
    # 1. Read the MSFS package.
    # ------------------------------------------------------------------
    report(5, f"Reading MSFS package {msfs_package_directory.name}")
    models, placements, package_warnings = MSFS_PKG.read_package(msfs_package_directory)
    warnings.extend(package_warnings)
    if not models:
        raise ValueError("no model libraries found in the MSFS package")
    if not placements:
        raise ValueError("no object placements found in the MSFS package")

    models_by_guid: Dict[str, MSFS_PKG.ModelEntry] = {m.guid: m for m in models}
    placed_guids = [p.guid for p in placements]
    usable_placements = [p for p in placements if p.guid in models_by_guid]
    skipped = len(placements) - len(usable_placements)
    if skipped:
        warnings.append(
            f"{skipped} placement(s) reference models not in this package "
            "(external library objects) and were skipped"
        )
    if not usable_placements:
        raise ValueError("no placement references a model inside this package")
    if UI.red_flag:
        raise InterruptedError("cancelled")

    # ------------------------------------------------------------------
    # 2. Identify the airport and create the pack skeleton.
    # ------------------------------------------------------------------
    centroid_latitude = sum(p.latitude for p in usable_placements) / len(usable_placements)
    centroid_longitude = sum(p.longitude for p in usable_placements) / len(usable_placements)
    global_apt_dat = global_airports_directory / "Earth nav data" / "apt.dat"
    airport_icao = None
    if global_apt_dat.is_file():
        report(15, "Locating airport in Global Airports apt.dat")
        airport_icao = XP_PACK.find_airport_near(
            global_apt_dat, centroid_latitude, centroid_longitude
        )
    else:
        warnings.append(f"Global Airports apt.dat not found at {global_apt_dat}")
    if package_name is None:
        base = airport_icao or msfs_package_directory.name
        package_name = f"MSFS Convert - {base}"
    pack_directory = custom_scenery_directory / package_name
    objects_directory = pack_directory / "objects"
    objects_directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 3. Copy the default airport (apt.dat block) from Global Airports.
    # ------------------------------------------------------------------
    apt_dat_copied = False
    if airport_icao is not None:
        report(20, f"Copying {airport_icao} apt.dat from Global Airports")
        airport_block = XP_PACK.extract_airport_from_global_apt_dat(
            global_apt_dat, airport_icao
        )
        if airport_block:
            XP_PACK.write_pack_apt_dat(pack_directory, airport_block)
            apt_dat_copied = True
        else:
            warnings.append(f"airport {airport_icao} not found in Global apt.dat")
    else:
        warnings.append(
            "no Global Airports airport within range of the placements; "
            "pack is written without an apt.dat"
        )
    if UI.red_flag:
        raise InterruptedError("cancelled")

    # ------------------------------------------------------------------
    # 4. Convert every model that is actually placed.
    # ------------------------------------------------------------------
    placed_unique_guids = sorted(set(p.guid for p in usable_placements))
    # Per GUID: (obj file name, horizontal footprint bounds_xz or None).
    object_files_by_guid: Dict[str, List[tuple]] = {}
    converted = 0
    for index, guid in enumerate(placed_unique_guids):
        if UI.red_flag:
            raise InterruptedError("cancelled")
        entry = models_by_guid[guid]
        report(
            25 + int(55 * index / max(len(placed_unique_guids), 1)),
            f"Converting model {index + 1}/{len(placed_unique_guids)}",
        )
        staging = pack_directory / "_msfs_staging"
        staging.mkdir(exist_ok=True)
        glb_path = staging / f"{guid}.glb"
        glb_path.write_bytes(entry.glb_bytes)
        # Textures referenced by relative URI resolve against the BGL's
        # own texture directory: stage links next to the temporary glb.
        MSFS_PKG.stage_texture_directory(entry, staging)
        manifest = msfs_convert.convert(glb_path, objects_directory, base_name=guid[:12])
        for warning in manifest["warnings"]:
            if "auto-detected" not in warning:
                warnings.append(f"{guid[:12]}: {warning}")
        object_files_by_guid[guid] = [
            (o["file"], o.get("bounds_xz")) for o in manifest["objects"]
        ]
        converted += 1
    shutil.rmtree(pack_directory / "_msfs_staging", ignore_errors=True)

    # ------------------------------------------------------------------
    # 5+6. Place converted objects in the overlay DSF, with exclusion
    #      rectangles suppressing the default gateway 3D underneath.
    # ------------------------------------------------------------------
    report(85, "Writing overlay DSF with placements and exclusions")
    placed_objects: List[XP_PACK.PlacedObject] = []
    for placement in usable_placements:
        for object_file, bounds_xz in object_files_by_guid.get(placement.guid, []):
            placed_objects.append(
                XP_PACK.PlacedObject(
                    object_relative_path=f"objects/{object_file}",
                    longitude=placement.longitude,
                    latitude=placement.latitude,
                    heading_degrees_true=placement.heading_degrees_true,
                    altitude_meters=placement.altitude_meters,
                    is_above_ground=placement.is_above_ground,
                    bounds_xz=tuple(bounds_xz) if bounds_xz else None,
                )
            )
    exclusions = XP_PACK.compute_exclusion_rectangles(placed_objects)
    XP_PACK.write_overlay_dsf(
        pack_directory, placed_objects, exclusions, Path(dsftool_path)
    )

    report(100, f"Done: {pack_directory.name}")
    return ConversionReport(
        package_path=pack_directory,
        airport_icao=airport_icao,
        models_converted=converted,
        objects_written=sum(len(v) for v in object_files_by_guid.values()),
        placements_written=len(placed_objects),
        placements_skipped=skipped,
        exclusion_rectangles=len(exclusions),
        apt_dat_copied=apt_dat_copied,
        warnings=warnings,
    )

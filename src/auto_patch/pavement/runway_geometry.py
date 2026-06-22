"""Runway pairing and corner geometry from CIFP / apt.dat data.

Pure-function utilities operating on RW##L/RW##R designators and
threshold lat/lon coordinates.  All inputs/outputs are in
WGS-84 lat/lon degrees plus meter widths/distances.

Public API:
    get_reciprocal(designator)       — RW##L -> reciprocal designator
    pair_runways(runways)            — group reciprocal pairs from a CIFP runway dict
    runway_corners(lat1, lon1, lat2, lon2, width_m)
                                     — 4 lat/lon corners of a runway rectangle
    extend_point(lat_from, lon_from, lat_to, lon_to, distance_m)
                                     — extrapolate a point along a great-circle bearing
    parse_aptdat_runway_widths(aptdat_path, icao)
                                     — read row-100 widths from an X-Plane apt.dat

Used by:
    * O4_Auto_Patch.generate_auto_patches
    * O4_Airport_Pavement_Builder (runway construction)
    * O4_Pavement_Runway_Segments (in slice 3)
"""
from __future__ import annotations

import os
import re
from math import cos, sin, pi, sqrt, atan2, acos
from typing import TypeVar

import O4_UI_Utils as UI

# Opaque per-runway record (e.g. a CIFP threshold dict). ``pair_runways``
# only passes these values through, so the exact shape is irrelevant —
# the TypeVar preserves whatever the caller's mapping holds.
_RunwayData = TypeVar("_RunwayData")

__all__ = [
    "get_reciprocal",
    "pair_runways",
    "match_runway_ends_by_geometry",
    "runway_corners",
    "extend_point",
    "parse_aptdat_runway_widths",
]


DEG_TO_M = 111120.0  # approximate meters per degree of latitude
DEFAULT_RUNWAY_WIDTH = 45.0  # meters - typical for major runways


# ──────────────────────────────────────────────────────────────────────
# Apt.dat Runway Width Parsing
# ──────────────────────────────────────────────────────────────────────
def parse_aptdat_runway_widths(aptdat_path: str, icao: str) -> dict[str, float]:
    """Extract runway widths from an X-Plane apt.dat file for a given airport.

    Apt.dat row code 100 format:
        100 <width_m> <surface> <shoulder> <smoothness> ... <rwy1_name> <lat> <lon> ...
                                                             <rwy2_name> <lat> <lon> ...

    Scans the (potentially large) apt.dat for the airport header (row 1 with
    matching ICAO), then reads its row-100 runways until the next airport.

    Args:
        aptdat_path: Path to apt.dat file.
        icao: ICAO code to search for (e.g. 'SPJC').

    Returns:
        dict: {designator: width_m} e.g. {'RW16R': 45.0, 'RW34L': 45.0}
              Empty dict if airport not found or on error.
    """
    widths: dict[str, float] = {}
    if not aptdat_path or not os.path.isfile(aptdat_path):
        return widths
    try:
        in_airport = False
        with open(aptdat_path, "r", errors="replace") as f:
            for line in f:
                fields = line.strip().split()
                if not fields:
                    continue
                row_code = fields[0]

                # Airport header: row code 1 (land), 16 (seaplane), 17 (heliport)
                if row_code in ("1", "16", "17"):
                    if in_airport:
                        break  # We've passed our airport, stop
                    # Check if this is our airport (ICAO is field 4)
                    if len(fields) >= 5 and fields[4].upper() == icao.upper():
                        in_airport = True
                    continue

                if not in_airport:
                    continue

                # Row code 100 = runway definition
                if row_code == "100" and len(fields) >= 9:
                    try:
                        width_m = float(fields[1])
                    except ValueError:
                        continue
                    # Runway end 1 name is field 8, end 2 name is further along
                    rwy1_name = fields[8]
                    # Runway end 2: the field index depends on the format.
                    # After rwy1's fields (name, lat, lon, displaced, overrun,
                    # markings, approach_lights, tdz, reil) = 9 fields starting
                    # at index 8, so rwy2 name is at index 17.
                    if len(fields) >= 18:
                        rwy2_name = fields[17]
                    else:
                        rwy2_name = None

                    # Normalize to our RWxx format
                    desig1 = "RW" + rwy1_name if not rwy1_name.startswith("RW") else rwy1_name
                    widths[desig1] = width_m
                    if rwy2_name:
                        desig2 = "RW" + rwy2_name if not rwy2_name.startswith("RW") else rwy2_name
                        widths[desig2] = width_m

    except OSError as e:
        UI.vprint(
            2,
            "   Auto-patch: Could not read apt.dat runway widths:",
            str(e),
        )
    return widths



# ──────────────────────────────────────────────────────────────────────
# Runway Pairing
# ──────────────────────────────────────────────────────────────────────
def get_reciprocal(designator: str) -> str | None:
    """Get the reciprocal runway designator. RW16L → RW34R, RW09 → RW27."""
    match = re.match(r"RW(\d{2})([LRC]?)", designator)
    if not match:
        return None
    num = int(match.group(1))
    suffix = match.group(2)
    recip_num = num + 18
    if recip_num > 36:
        recip_num -= 36
    recip_suffix = {"L": "R", "R": "L", "C": "C", "": ""}.get(suffix, "")
    return "RW{:02d}{}".format(recip_num, recip_suffix)


def pair_runways(
    runways: dict[str, _RunwayData],
) -> list[tuple[str, _RunwayData, str | None, _RunwayData | None]]:
    """Match runway thresholds into pairs.

    Returns list of tuples:
        (desig_a, data_a, desig_b, data_b)
    where a is the higher-numbered threshold (higher heading number) by
    convention, and b is the reciprocal. If unpaired, desig_b/data_b are None.
    """
    paired: set[str] = set()
    pairs: list[tuple[str, _RunwayData, str | None, _RunwayData | None]] = []
    for desig in sorted(runways.keys()):
        if desig in paired:
            continue
        data = runways[desig]
        recip = get_reciprocal(desig)
        if recip and recip in runways:
            paired.add(desig)
            paired.add(recip)
            pairs.append((desig, data, recip, runways[recip]))
        else:
            pairs.append((desig, data, None, None))
    return pairs


def match_runway_ends_by_geometry(
    cifp_a_lat: float, cifp_a_lon: float,
    cifp_b_lat: float, cifp_b_lon: float,
    apt_ends: list[tuple[float, float, float, float]],
    max_mid_dist_m: float = 350.0,
) -> tuple[int, bool] | None:
    """Match a CIFP runway pair to an apt.dat runway by GEOMETRY.

    Designators cannot always reconcile a CIFP runway to its apt.dat
    footprint: magnetic-variation drift renumbers runways, so the same
    physical strip is e.g. ``03/21`` in apt.dat but ``RW04/RW22`` in the
    CIFP (SSUM Umuarama).  ``canonical_runway_desig`` only strips the
    ``RW`` prefix and zero padding — it cannot bridge a ±1 heading-number
    change — so the segmenter's designator lookups miss, ``have_apt_geom``
    is False, and the runway falls back to coarse CIFP geometry instead of
    segmenting at its apt.dat pavement joins.

    The physical runway is identical regardless of its label, so reconcile
    by position instead.  Given a CIFP pair's two threshold coordinates and
    a list of apt.dat runways (each ``(lat_a, lon_a, lat_b, lon_b)`` — the
    physical ends), return ``(index, swapped)`` of the apt.dat runway whose
    centre lies closest to the CIFP pair's centre, where ``swapped`` is
    True when CIFP end-a corresponds to apt end-b (reversed numbering).
    Returns None if no apt.dat runway centre is within ``max_mid_dist_m``
    (CIFP thresholds are displaced inward from the physical ends, so the
    pair centre can sit ~100 m off the apt.dat centre — but always far
    nearer its own runway than any neighbour).
    """
    cifp_mid_lat = (cifp_a_lat + cifp_b_lat) / 2.0
    cifp_mid_lon = (cifp_a_lon + cifp_b_lon) / 2.0
    cos_lat = cos(cifp_mid_lat * pi / 180.0)
    if cos_lat < 1e-6:
        cos_lat = 1e-6

    def _dist_m(lat1, lon1, lat2, lon2):
        dx = (lon2 - lon1) * cos_lat * DEG_TO_M
        dy = (lat2 - lat1) * DEG_TO_M
        return sqrt(dx * dx + dy * dy)

    best_idx = None
    best_mid = max_mid_dist_m
    best_swapped = False
    for idx, (alat_a, alon_a, alat_b, alon_b) in enumerate(apt_ends):
        apt_mid_lat = (alat_a + alat_b) / 2.0
        apt_mid_lon = (alon_a + alon_b) / 2.0
        mid_d = _dist_m(cifp_mid_lat, cifp_mid_lon, apt_mid_lat, apt_mid_lon)
        if mid_d >= best_mid:
            continue
        # Orientation: does CIFP end-a line up with apt end-a or end-b?
        straight = (_dist_m(cifp_a_lat, cifp_a_lon, alat_a, alon_a)
                    + _dist_m(cifp_b_lat, cifp_b_lon, alat_b, alon_b))
        swapped = (_dist_m(cifp_a_lat, cifp_a_lon, alat_b, alon_b)
                   + _dist_m(cifp_b_lat, cifp_b_lon, alat_a, alon_a))
        best_idx = idx
        best_mid = mid_d
        best_swapped = swapped < straight
    if best_idx is None:
        return None
    return best_idx, best_swapped



# ──────────────────────────────────────────────────────────────────────
# Runway Corner Geometry
# ──────────────────────────────────────────────────────────────────────
def runway_corners(
    lat1: float, lon1: float, lat2: float, lon2: float, width_m: float,
) -> list[tuple[float, float]] | None:
    """Compute the 4 corners of a runway rectangle.

    The corners are ordered for the altitude_high/altitude_low patch convention:
        node0 (high-left)  → node1 (low-left) → node2 (low-right) → node3 (high-right)

    In include_patches():
        short_high = way[-2:] = [node3, node0]   (the "high" altitude side)
        short_low  = way[1:3] = [node1, node2]   (the "low" altitude side)

    The "high" side is at (lat1, lon1) and the "low" side at (lat2, lon2).

    Returns:
        list of 4 (lat, lon) tuples, or None if degenerate.
    """
    mid_lat = (lat1 + lat2) / 2.0
    cos_lat = cos(mid_lat * pi / 180.0)
    if cos_lat < 1e-6:
        cos_lat = 1e-6

    # Direction vector in meters
    dx_m = (lon2 - lon1) * cos_lat * DEG_TO_M
    dy_m = (lat2 - lat1) * DEG_TO_M
    length_m = sqrt(dx_m ** 2 + dy_m ** 2)
    if length_m < 1.0:
        return None

    # Perpendicular unit vector (90° clockwise rotation)
    perp_dx_m = -dy_m / length_m
    perp_dy_m = dx_m / length_m

    # Half-width offset in degrees
    half_w = width_m / 2.0
    perp_dlon = (perp_dx_m * half_w) / (cos_lat * DEG_TO_M)
    perp_dlat = (perp_dy_m * half_w) / DEG_TO_M

    # Corners: high-left, low-left, low-right, high-right
    c0 = (lat1 + perp_dlat, lon1 + perp_dlon)
    c1 = (lat2 + perp_dlat, lon2 + perp_dlon)
    c2 = (lat2 - perp_dlat, lon2 - perp_dlon)
    c3 = (lat1 - perp_dlat, lon1 - perp_dlon)
    return [c0, c1, c2, c3]


def extend_point(
    lat_from: float, lon_from: float, lat_to: float, lon_to: float,
    distance_m: float,
) -> tuple[float, float]:
    """Extend a point beyond lat_to/lon_to by distance_m meters
    along the direction from lat_from/lon_from to lat_to/lon_to.

    Returns (lat, lon) of the extended point.
    """
    mid_lat = (lat_from + lat_to) / 2.0
    cos_lat = cos(mid_lat * pi / 180.0)
    if cos_lat < 1e-6:
        cos_lat = 1e-6

    dx_m = (lon_to - lon_from) * cos_lat * DEG_TO_M
    dy_m = (lat_to - lat_from) * DEG_TO_M
    length_m = sqrt(dx_m ** 2 + dy_m ** 2)
    if length_m < 1.0:
        return (lat_to, lon_to)

    # Unit direction vector
    ux = dx_m / length_m
    uy = dy_m / length_m

    ext_dlon = (ux * distance_m) / (cos_lat * DEG_TO_M)
    ext_dlat = (uy * distance_m) / DEG_TO_M
    return (lat_to + ext_dlat, lon_to + ext_dlon)

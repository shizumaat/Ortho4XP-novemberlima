#!/usr/bin/env python3
"""Dump the completed taxi ROUTE NETWORK (``layout.apt_taxi_centerlines``) to KML,
colouring the synthetic turn-fillet arcs distinctly from the source routes so the
new arcs can be verified against the real taxiway geometry.

  * source taxi routes  → cyan
  * synthetic fillets (name=="fillet") → red, thicker
  * service roads       → grey (dashed-ish, thin)

Fillets are ON by default here (sets O4_TAXI_FILLET=1 unless already set).

Usage:  venv/bin/python tools/dump_route_network_kml.py SPJC \
            --out /Users/noah/Ortho4XP-troubleshoot/SPJC_route_network.kml
"""
from __future__ import annotations

import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests")]

_R = 6378137.0


def _to_lonlat(x, y, lat0, lon0):
    lat = lat0 + math.degrees(y / _R)
    lon = lon0 + math.degrees(x / (_R * math.cos(math.radians(lat0))))
    return lon, lat


def _placemark(name, style, coords_lonlat):
    cs = " ".join(f"{lon:.8f},{lat:.8f},0" for lon, lat in coords_lonlat)
    return (f'<Placemark><name>{name}</name><styleUrl>#{style}</styleUrl>'
            f'<LineString><tessellate>1</tessellate>'
            f'<coordinates>{cs}</coordinates></LineString></Placemark>')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-fillet", action="store_true",
                    help="dump the source network WITHOUT synthetic fillets")
    ap.add_argument("--global", dest="use_global", action="store_true",
                    help="force the Global Airports apt.dat instead of a custom pack")
    args = ap.parse_args()

    os.environ["O4_TAXI_FILLET"] = "0" if args.no_fillet else "1"

    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement

    root = xplane_root()
    if args.use_global:
        for base in (("Global Scenery", "Global Airports"),
                     ("Custom Scenery", "Global Airports")):
            g = os.path.join(root, *base, "Earth nav data", "apt.dat")
            if os.path.isfile(g):
                os.environ["O4_FORCE_APT_DAT"] = g
                print(f"forcing Global Airports apt.dat: {g}")
                break

    lay = build_airport_pavement(args.icao, root, compute_elevations=True)
    lat0, lon0 = lay.anchor
    cls = getattr(lay, "apt_taxi_centerlines", []) or []

    src = fil = svc = 0
    marks = []
    for i, tcl in enumerate(cls):
        ln = getattr(tcl, "line", None)
        if ln is None or ln.is_empty:
            continue
        coords = [_to_lonlat(x, y, lat0, lon0) for (x, y) in ln.coords]
        if getattr(tcl, "is_service", False):
            style, svc = "svc", svc + 1
            nm = f"svc{i}"
        elif getattr(tcl, "name", "") == "fillet_paint":
            style, fil = "fillet_paint", fil + 1
            nm = f"fillet_paint{i} ({tcl.dominant_size() or '?'})"
        elif getattr(tcl, "name", "") == "fillet":
            style, fil = "fillet", fil + 1
            nm = f"fillet{i} ({tcl.dominant_size() or '?'})"
        elif getattr(tcl, "name", "").startswith("~"):
            style = "synth"
            nm = f"synth{i} ({tcl.name})"
        else:
            style, src = "route", src + 1
            nm = f"route{i} ({tcl.dominant_size() or '?'})"
        marks.append(_placemark(nm, style, coords))

    styles = (
        '<Style id="route"><LineStyle><color>ffffff00</color>'
        '<width>2</width></LineStyle></Style>'
        '<Style id="fillet"><LineStyle><color>ff0000ff</color>'
        '<width>4</width></LineStyle></Style>'
        '<Style id="fillet_paint"><LineStyle><color>ff00a5ff</color>'
        '<width>4</width></LineStyle></Style>'
        '<Style id="svc"><LineStyle><color>ff888888</color>'
        '<width>1</width></LineStyle></Style>'
        '<Style id="synth"><LineStyle><color>ff00ffff</color>'
        '<width>2</width></LineStyle></Style>')
    doc = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           f'<name>{args.icao} taxi route network'
           f'{" +fillets" if not args.no_fillet else ""}</name>'
           + styles + "".join(marks) + "</Document></kml>")

    out = args.out or os.path.join(ROOT, f"{args.icao}_route_network.kml")
    with open(out, "w") as f:
        f.write(doc)
    print(f"WROTE {out}")
    print(f"  source routes={src}  fillet arcs={fil}  service roads={svc}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build the pavement-derived spine for one airport and dump it as
JOSM-previewable OSM layers.

    venv/bin/python tools/pav_skeleton_osm.py SPJC [--cache] [--medial-only]

Default mode is route-guided SYNTHESIS (``pavement/spine_synthesis.py``):
straight through-lines from the apt.dat taxi route network, EASA/FAA fillet
arcs at turns, constant half-width loops around pavement holes, building
stubs/rings, medial-axis fallback.  ``--medial-only`` gives the pure
Voronoi medial skeleton (``pavement/pav_skeleton.py``).

Writes ``<out>_skeleton.osm`` (ways tagged with the construct kind) and
``<out>_pavement.osm`` (the pav_union minus runways it was derived from) in
the same lat/lon frame as the emitted patch.

Also prints an accuracy self-check against the RECOGNIZED painted
centerlines where those exist (distance from recognized-line samples to the
nearest spine way, split corridor vs apron-interior).

``--cache`` pickles the extracted geometry (pavement, runways, routes,
buildings, recognized lines, anchor) so iteration runs in seconds instead of
the ~90 s pipeline build.
"""

import argparse
import math
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

import numpy as np
from shapely import wkb
from shapely.geometry import LineString
from shapely.ops import unary_union

_CACHE_VERSION = 2


class _Frame:
    """m_to_ll shim over a cached anchor (equirectangular, layout.py)."""

    def __init__(self, anchor):
        self.anchor = anchor

    def m_to_ll(self, x, y):
        from auto_patch.layout import R_EARTH
        lat0, lon0 = self.anchor
        cos0 = math.cos(math.radians(lat0))
        return (lat0 + math.degrees(y / R_EARTH),
                lon0 + math.degrees(x / (R_EARTH * cos0)))


class _Route:
    """Cached stand-in for TaxiCenterline (what spine_synthesis reads)."""

    def __init__(self, line, size, service):
        self.chained_line = line
        self.line = line
        self.is_service = service
        self._size = size

    def dominant_size(self):
        return self._size


def _extract(icao: str, xplane: str):
    """Full pipeline build → the geometry bundle the spine builders need."""
    from auto_patch.pipeline import build_airport_pavement
    layout = build_airport_pavement(icao, xplane, compute_elevations=False)
    pav = getattr(layout, "source_pavement_union", None)
    rwy = getattr(layout, "runway_union", None)

    # Routes FIRST (recognition below replaces apt_taxi_centerlines with the
    # painted lines; we want the straight apt.dat network as the guide).
    routes, seen = [], set()
    for tc in getattr(layout, "apt_taxi_centerlines", None) or []:
        ln = getattr(tc, "chained_line", None) or getattr(tc, "line", None)
        if ln is None or ln.is_empty or id(ln) in seen:
            continue
        seen.add(id(ln))
        routes.append((ln, getattr(tc, "dominant_size", lambda: "")() or "",
                       bool(getattr(tc, "is_service", False))))

    buildings = []
    for s in getattr(layout, "shapes", []) or []:
        if getattr(s, "role", "") in ("building", "terminal") \
                and getattr(s, "polygon", None) is not None \
                and not s.polygon.is_empty:
            buildings.append((s.polygon, s.role))

    recog = []
    try:
        from auto_patch.centerline_recognition import (
            recognize_curved_centerlines)
        os.environ.setdefault("O4_RECOGNIZED_CENTERLINES", "1")
        recognize_curved_centerlines(layout, icao)
        for tc in getattr(layout, "apt_taxi_centerlines", None) or []:
            if getattr(tc, "is_service", False):
                continue
            ln = getattr(tc, "chained_line", None) or getattr(tc, "line", None)
            if isinstance(ln, LineString) and not ln.is_empty \
                    and ln.length > 20:
                recog.append(ln)
    except Exception:
        pass

    return {
        "version": _CACHE_VERSION,
        "anchor": layout.anchor,
        "pav": wkb.dumps(pav),
        "rwy": wkb.dumps(rwy) if rwy is not None else None,
        "routes": [(wkb.dumps(l), s, sv) for (l, s, sv) in routes],
        "buildings": [(wkb.dumps(b), r) for (b, r) in buildings],
        "recog": [wkb.dumps(r) for r in recog],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--xplane", default="/Users/noah/X-Plane 12")
    ap.add_argument("--out", default=None,
                    help="output path prefix (default /tmp/<ICAO>_skel)")
    ap.add_argument("--medial-only", action="store_true",
                    help="pure Voronoi medial skeleton (no route guidance)")
    ap.add_argument("--setback", type=float, default=100.0,
                    help="terminal/large-building ring setback (m)")
    ap.add_argument("--cache", action="store_true",
                    help="reuse cached geometry (skips the ~90s build; "
                         "cache is written on first run)")
    args = ap.parse_args(argv)
    prefix = args.out or f"/tmp/{args.icao}_skel"
    cache_path = f"/tmp/{args.icao}_skel_geom.pkl"

    from auto_patch.pavement.global_slice import _osm_write

    c = None
    if args.cache and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            c = pickle.load(f)
        if c.get("version") != _CACHE_VERSION:
            c = None
    if c is None:
        c = _extract(args.icao, args.xplane)
        with open(cache_path, "wb") as f:
            pickle.dump(c, f)

    frame = _Frame(c["anchor"])
    pav = wkb.loads(c["pav"])
    rwy = wkb.loads(c["rwy"]) if c["rwy"] else None
    routes = [_Route(wkb.loads(b), s, sv) for (b, s, sv) in c["routes"]]
    buildings = [(wkb.loads(b), r) for (b, r) in c["buildings"]]
    recog = [wkb.loads(b) for b in c["recog"]]
    if pav is None or pav.is_empty:
        print("NO pav_union", file=sys.stderr)
        return 1
    pav_eff = pav.difference(rwy) if rwy is not None and not rwy.is_empty \
        else pav

    # ── build the spine ─────────────────────────────────────────────────────
    entries = []
    if args.medial_only:
        from auto_patch.pavement.pav_skeleton import build_pavement_skeleton
        chains = build_pavement_skeleton(pav, runway_union=rwy)
        lines = [ch.line for ch in chains]
        for i, ch in enumerate(chains):
            entries.append((ch.line, {
                "layer": "skeleton", "kind": "medial", "chain": str(i),
                "len_m": f"{ch.line.length:.0f}",
                "halfwidth_mean": f"{float(np.mean(ch.radii)):.1f}"}))
        kinds = {"medial": len(chains)}
    else:
        from auto_patch.pavement.spine_synthesis import synthesize_spine
        ways = synthesize_spine(pav, runway_union=rwy, routes=routes,
                                buildings=buildings,
                                terminal_setback=args.setback)
        lines = [w.line for w in ways]
        kinds = {}
        for i, w in enumerate(ways):
            kinds[w.kind] = kinds.get(w.kind, 0) + 1
            tags = {"layer": "skeleton", "kind": w.kind, "way": str(i),
                    "len_m": f"{w.line.length:.0f}"}
            if w.size:
                tags["icao_size"] = w.size
            if w.service:
                tags["service"] = "yes"
            entries.append((w.line, tags))

    _osm_write(frame, entries, f"{prefix}_skeleton.osm")
    _osm_write(frame, [(pav_eff, {"layer": "pav_union"})],
               f"{prefix}_pavement.osm")

    # ── stats ───────────────────────────────────────────────────────────────
    total = sum(ln.length for ln in lines)
    print(f"# {args.icao} spine ({'medial' if args.medial_only else 'synth'})")
    print(f"  ways                   : {len(lines)}  (total {total:.0f} m)")
    print(f"  kinds                  : " +
          ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    if recog and lines:
        skel = unary_union(lines)
        corr, apr = [], []
        bnd = pav_eff.boundary
        for r in recog:
            n = max(2, int(r.length / 10))
            for k in range(n + 1):
                p = r.interpolate(k * r.length / n)
                if not pav_eff.contains(p):
                    continue
                (corr if bnd.distance(p) < 20.0 else apr).append(
                    skel.distance(p))
        for name, ds in (("corridor pts", corr), ("apron-interior pts", apr)):
            if not ds:
                continue
            a = np.asarray(ds)
            print(f"  vs recognized CLs ({name:>18}): n={len(a)}  "
                  f"mean {a.mean():.2f}  median {np.median(a):.2f}  "
                  f"p95 {np.percentile(a, 95):.2f}  max {a.max():.2f} m")
    print(f"  wrote {prefix}_skeleton.osm + {prefix}_pavement.osm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build the pavement-derived medial-axis spine for one airport and dump it
as JOSM-previewable OSM layers.

    venv/bin/python tools/pav_skeleton_osm.py SPJC [--out /tmp/SPJC_skel]

Writes ``<out>_skeleton.osm`` (the faired spine chains, tagged with chain id,
end kinds, and min/mean half-width) and ``<out>_pavement.osm`` (the pav_union
minus runways it was derived from) in the same lat/lon frame as the emitted
patch, so both overlay the recognized centerlines / built OSM exactly.

Also prints an accuracy self-check against the RECOGNIZED painted centerlines
where those exist (mean/p95 distance from each recognized line to the nearest
skeleton chain): ground truth for "does the inferred spine ride the real
centerline" without eyeballing.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

import numpy as np
from shapely.geometry import LineString
from shapely.ops import unary_union


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--xplane", default="/Users/noah/X-Plane 12")
    ap.add_argument("--out", default=None,
                    help="output path prefix (default /tmp/<ICAO>_skel)")
    ap.add_argument("--step", type=float, default=2.0,
                    help="boundary sampling step (m)")
    ap.add_argument("--no-extend", action="store_true",
                    help="do not extend leaf tips to the pavement boundary")
    ap.add_argument("--cache", action="store_true",
                    help="reuse cached pavement geometry (skips the ~90s "
                         "pipeline build; cache is written on first run)")
    args = ap.parse_args(argv)
    prefix = args.out or f"/tmp/{args.icao}_skel"

    from auto_patch.pavement.pav_skeleton import build_pavement_skeleton
    from auto_patch.pavement.global_slice import _osm_write

    import pickle
    from shapely import wkb

    class _Frame:
        """m_to_ll shim over a cached anchor (equirectangular, layout.py)."""
        def __init__(self, anchor):
            self.anchor = anchor

        def m_to_ll(self, x, y):
            import math
            from auto_patch.layout import R_EARTH
            lat0, lon0 = self.anchor
            cos0 = math.cos(math.radians(lat0))
            return (lat0 + math.degrees(y / R_EARTH),
                    lon0 + math.degrees(x / (R_EARTH * cos0)))

    cache_path = f"/tmp/{args.icao}_skel_geom.pkl"
    layout = None
    if args.cache and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            c = pickle.load(f)
        layout = _Frame(c["anchor"])
        pav = wkb.loads(c["pav"])
        rwy = wkb.loads(c["rwy"]) if c["rwy"] else None
        recog_cached = [wkb.loads(b) for b in c["recog"]]
    if layout is None:
        from auto_patch.pipeline import build_airport_pavement
        layout = build_airport_pavement(
            args.icao, args.xplane, compute_elevations=False)
        pav = getattr(layout, "source_pavement_union", None)
        rwy = getattr(layout, "runway_union", None)
        recog_cached = None
    if pav is None or pav.is_empty:
        print("NO pav_union", file=sys.stderr)
        return 1

    chains = build_pavement_skeleton(
        pav, runway_union=rwy, boundary_step=args.step,
        extend_tips=not args.no_extend)

    # ── dump layers ─────────────────────────────────────────────────────────
    entries = []
    for i, ch in enumerate(chains):
        entries.append((ch.line, {
            "layer": "skeleton", "chain": str(i), "piece": str(ch.piece),
            "ends": f"{ch.end_a_kind}/{ch.end_b_kind}",
            "len_m": f"{ch.line.length:.0f}",
            "halfwidth_min": f"{min(ch.radii):.1f}",
            "halfwidth_mean": f"{float(np.mean(ch.radii)):.1f}",
        }))
    _osm_write(layout, entries, f"{prefix}_skeleton.osm")
    pav_eff = pav.difference(rwy) if rwy is not None and not rwy.is_empty else pav
    _osm_write(layout, [(pav_eff, {"layer": "pav_union"})],
               f"{prefix}_pavement.osm")

    # ── stats ───────────────────────────────────────────────────────────────
    total = sum(ch.line.length for ch in chains)
    leaves = sum((ch.end_a_kind == "leaf") + (ch.end_b_kind == "leaf")
                 for ch in chains)
    pieces = len({ch.piece for ch in chains})
    print(f"# {args.icao} pavement-skeleton spine")
    print(f"  chains                 : {len(chains)}  "
          f"(total {total:.0f} m, pavement pieces {pieces})")
    print(f"  leaf tips              : {leaves}")

    # Accuracy self-check vs recognized painted centerlines (where they exist).
    if recog_cached is not None:
        recog = recog_cached
    else:
        try:
            from auto_patch.centerline_recognition import (
                recognize_curved_centerlines)
            os.environ.setdefault("O4_RECOGNIZED_CENTERLINES", "1")
            recognize_curved_centerlines(layout, args.icao)
        except Exception:
            pass
        recog = []
        for tc in getattr(layout, "apt_taxi_centerlines", None) or []:
            if getattr(tc, "is_service", False):
                continue
            ln = getattr(tc, "chained_line", None) or getattr(tc, "line", None)
            if isinstance(ln, LineString) and not ln.is_empty and ln.length > 20:
                recog.append(ln)
        with open(cache_path, "wb") as f:
            pickle.dump({
                "anchor": layout.anchor,
                "pav": wkb.dumps(pav),
                "rwy": wkb.dumps(rwy) if rwy is not None else None,
                "recog": [wkb.dumps(r) for r in recog],
            }, f)
    if recog and chains:
        skel = unary_union([ch.line for ch in chains])
        # Only score points on TAXI pavement (recognized lines are fed with no
        # runway clip; the skeleton deliberately stops at the runway edge).
        # Split corridor-ish points (near-boundary, i.e. narrow pavement) from
        # apron interiors: on a corridor the medial axis must RIDE the painted
        # line; across an open apron the skeleton is a different (by-design)
        # path, so distance there is not an error signal.
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

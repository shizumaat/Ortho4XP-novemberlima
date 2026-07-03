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
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

_CACHE_VERSION = 4


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

    # Shoulder-inclusive runway union for the thru-runway trace (user
    # 2026-07-02: "full runway rect" incl. shoulders).  The pipeline's
    # three shoulder passes (whole-polygon absorption, coded row-100
    # width, DSF extent) already widened layout.runway_union where they
    # fire; for runways whose shoulder is declared only as an XP12
    # surface-type code (SPJC: 27/28 — exists, no width digit) X-Plane
    # renders it procedurally at 25% of runway width per side — widen
    # by that here, capped at 75 m total (CS-ADR-DSN.B.080).
    rwy_full = getattr(layout, "runway_union", None)
    try:
        from auto_patch.apt_dat_reader import (
            find_airport_apt_dat, load_airport)
        from auto_patch.pavement.runways import _widen_runway_rect
        lat0, lon0 = layout.anchor
        cos0 = math.cos(math.radians(lat0))
        R = 6378137.0

        def to_m(lon, lat):
            return (math.radians(lon - lon0) * R * cos0,
                    math.radians(lat - lat0) * R)

        apt = load_airport(find_airport_apt_dat(xplane, icao), icao)
        extra = []
        for r in apt.runways:
            code = r.shoulder_code or 0
            if code <= 0:
                continue
            per_side = max(code // 100, 0.25 * r.width_m)
            total = min(r.width_m + 2.0 * per_side, 75.0)
            half = total / 2.0
            rect = _widen_runway_rect(r, layout.anchor, -half, half, to_m)
            if rect is not None and not rect.is_empty:
                extra.append(rect)
        if extra:
            rwy_full = unary_union(
                ([rwy_full] if rwy_full is not None else []) + extra)
    except Exception:
        pass

    # apt.dat parking positions (rows 1300/15) — the movement-demand
    # endpoints: paint that only distributes into stands is parking
    # guidance, not taxi spine (user deletion criterion 2026-07-02)
    ramps = []
    try:
        from auto_patch.apt_dat_reader import (
            find_airport_apt_dat, _read_airport_block)
        path = find_airport_apt_dat(xplane, icao)
        lat0, lon0 = layout.anchor
        cos0 = math.cos(math.radians(lat0))
        R = 6378137.0
        for row in _read_airport_block(path, icao) or []:
            toks = row.split()
            if not toks or toks[0] not in ("1300", "15"):
                continue
            try:
                lat, lon = float(toks[1]), float(toks[2])
            except (ValueError, IndexError):
                continue
            ramps.append((math.radians(lon - lon0) * R * cos0,
                          math.radians(lat - lat0) * R))
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
        "ramps": ramps,
        "rwy_full": wkb.dumps(rwy_full) if rwy_full is not None else None,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--xplane", default="/Users/noah/X-Plane 12")
    ap.add_argument("--out", default=None,
                    help="output path prefix (default /tmp/<ICAO>_skel)")
    ap.add_argument("--medial-only", action="store_true",
                    help="pure Voronoi medial skeleton (no route guidance)")
    ap.add_argument("--v7", action="store_true",
                    help="previous-generation heuristic synthesis "
                         "(spine_synthesis.synthesize_spine)")
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
    # working pavement = buildings subtracted (user 2026-07-02: chords must
    # never pass through buildings; this is the deciding footprint)
    bldg_union = unary_union([b for b, _r in buildings]) if buildings else None
    pav_nav = pav.difference(bldg_union.buffer(0.5)) \
        if bldg_union is not None else pav
    pav_eff = pav_nav.difference(rwy) if rwy is not None and not rwy.is_empty \
        else pav_nav

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
        if args.v7:
            from auto_patch.pavement.spine_synthesis import synthesize_spine
            kwargs = {}
        else:
            # DEFAULT = the route-arc spine (v13, production model)
            from auto_patch.pavement.route_arcs import (
                synthesize_spine_v13 as synthesize_spine)
            kwargs = {"recognized": recog,
                      "ramps": c.get("ramps") or [],
                      "rwy_full": wkb.loads(c["rwy_full"])
                      if c.get("rwy_full") else None}
        ways = synthesize_spine(pav, runway_union=rwy, buildings=buildings,
                                routes=routes,  # size letters ONLY
                                terminal_setback=args.setback, **kwargs)
        lines = [w.line for w in ways]
        kinds = {}
        for i, w in enumerate(ways):
            kinds[w.kind] = kinds.get(w.kind, 0) + 1
            tags = {"layer": "skeleton", "kind": w.kind, "way": str(i),
                    "len_m": f"{w.line.length:.0f}"}
            if w.size:
                tags["icao_size"] = w.size
            if w.halfwidth:
                tags["halfwidth"] = f"{w.halfwidth:.1f}"
            entries.append((w.line, tags))

    # grading wants a node roughly every SPINE_STEP_M (12 m) along the spine
    dense_entries = []
    for geom, tags in entries:
        try:
            dense_entries.append((geom.segmentize(12.0), tags))
        except Exception:
            dense_entries.append((geom, tags))
    _osm_write(frame, dense_entries, f"{prefix}_skeleton.osm")
    _osm_write(frame, [(pav_eff, {"layer": "pav_union"})],
               f"{prefix}_pavement.osm")

    # ── stats + QA gates ────────────────────────────────────────────────────
    total = sum(ln.length for ln in lines)
    print(f"# {args.icao} spine ({'medial' if args.medial_only else 'synth'})")
    print(f"  ways                   : {len(lines)}  (total {total:.0f} m)")
    print(f"  kinds                  : " +
          ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    # GATE 1 — connectivity: every endpoint must touch another way, the
    # pavement boundary, or a building edge (lead-in door ends and lanes
    # terminating at a pad are legitimate).
    floating = 0
    bnd = pav_eff.boundary
    for i, ln in enumerate(lines):
        for tip in (ln.coords[0], ln.coords[-1]):
            p = Point(tip)
            dmin = min((lines[j].distance(p)
                        for j in range(len(lines)) if j != i), default=99.0)
            if dmin <= 0.5 or bnd.distance(p) <= 2.0:
                continue
            if bldg_union is not None and bldg_union.distance(p) <= 2.0:
                continue
            floating += 1
    print(f"  GATE floating ends     : {floating}  (target 0)")

    # GATE 2 — arc radius histogram (should spike at the per-size standards)
    def _circ_r(cs):
        cs = np.asarray(cs)
        a, b, c2 = cs[0], cs[len(cs) // 2], cs[-1]
        ab = np.hypot(*(b - a)); bc = np.hypot(*(c2 - b))
        ca = np.hypot(*(a - c2))
        ar2 = abs((b[0] - a[0]) * (c2[1] - a[1])
                  - (b[1] - a[1]) * (c2[0] - a[0]))
        return ab * bc * ca / (2 * ar2) if ar2 > 1e-9 else float("inf")
    if not args.medial_only:
        rads = sorted(_circ_r(w.line.coords) for w in ways
                      if w.kind in ("arc", "rwy_turn"))
        rads = [r for r in rads if r < 300]
        if rads:
            h, edges_ = np.histogram(
                rads, bins=[0, 10, 14, 18, 25, 33, 40, 50, 60, 300])
            print("  arc radii              : " + ", ".join(
                f"{int(edges_[i])}-{int(edges_[i+1])}m:{int(h[i])}"
                for i in range(len(h)) if h[i]))

    # GATE 3 — diff vs the HAND-EDITED target fixture, when present (the
    # authoritative reference; the user edited a generated OSM to the ideal).
    fixture = os.path.join(os.path.dirname(__file__), "..", "tests",
                           "fixtures", "spine_targets",
                           f"{args.icao}_spine_target.osm")
    if os.path.exists(fixture) and lines:
        import xml.etree.ElementTree as _ET
        from auto_patch.layout import R_EARTH as _RE
        lat0, lon0 = c["anchor"]
        cos0 = math.cos(math.radians(lat0))
        root = _ET.parse(fixture).getroot()
        nds = {n.get("id"): (math.radians(float(n.get("lon")) - lon0)
                             * _RE * cos0,
                             math.radians(float(n.get("lat")) - lat0) * _RE)
               for n in root.findall("node")}
        tgt_lines = []
        for wy in root.findall("way"):
            pts = [nds[r.get("ref")] for r in wy.findall("nd")
                   if r.get("ref") in nds]
            if len(pts) >= 2:
                tgt_lines.append(LineString(pts))
        if tgt_lines:
            skel_u = unary_union(lines)
            tgt_u = unary_union(tgt_lines)
            ds = []
            for t in tgt_lines:
                n = max(2, int(t.length / 8))
                ds.extend(skel_u.distance(t.interpolate(k * t.length / n))
                          for k in range(n + 1))
            a = np.asarray(ds)
            over = tot = 0.0
            for ln in lines:
                n = max(2, int(ln.length / 8))
                for k in range(n):
                    p0 = ln.interpolate(k * ln.length / n)
                    seg = ln.length / n
                    tot += seg
                    if tgt_u.distance(p0) > 10.0:
                        over += seg
            cov = sum(1 for d in ds if d <= 5.0) / max(len(ds), 1)
            matched = [d for d in ds if d <= 5.0]
            med_aligned = float(np.median(matched)) if matched else 99.0
            print(f"  GATE coverage          : {100*cov:.1f}%  "
                  f"(target within 5 m of spine; goal >=98%)")
            print(f"  GATE economy           : {100*(1-over/max(tot,1e-9)):.1f}%  "
                  f"(spine within 10 m of target; goal >=98%)")
            print(f"  GATE alignment         : median {med_aligned:.2f} m on "
                  f"matched portions (goal <=1 m) | all-median "
                  f"{np.median(a):.2f}  p95 {np.percentile(a,95):.2f}")

    # GATE 3b — diff vs the approved target KML, when present.
    kml_path = f"/Users/noah/Ortho4XP-troubleshoot/{args.icao}_curved_spine.kml"
    if os.path.exists(kml_path) and lines:
        import re as _re
        from auto_patch.layout import R_EARTH
        lat0, lon0 = c["anchor"]
        cos0 = math.cos(math.radians(lat0))
        tgt = []
        txt = open(kml_path).read()
        for m in _re.finditer(
                r"<styleUrl>#(\w+)</styleUrl><LineString>.*?"
                r"<coordinates>([^<]+)</coordinates>", txt, _re.S):
            if m.group(1) == "svc":
                continue
            pts = []
            for tok in m.group(2).split():
                lon, lat = float(tok.split(",")[0]), float(tok.split(",")[1])
                pts.append((math.radians(lon - lon0) * R_EARTH * cos0,
                            math.radians(lat - lat0) * R_EARTH))
            if len(pts) >= 2:
                tgt.append(LineString(pts))
        if tgt:
            skel = unary_union(lines)
            ds = []
            for t in tgt:
                n = max(2, int(t.length / 10))
                for k in range(n + 1):
                    p = t.interpolate(k * t.length / n)
                    if pav_eff.buffer(0.5).contains(p):
                        ds.append(skel.distance(p))
            a = np.asarray(ds)
            print(f"  GATE vs target KML     : n={len(a)}  "
                  f"mean {a.mean():.2f}  median {np.median(a):.2f}  "
                  f"p95 {np.percentile(a, 95):.2f}  max {a.max():.2f} m")

    # Cross-reference vs the apt.dat taxi ROUTE graph (development yardstick,
    # never a construction input): how much of the route network does the
    # pavement-derived spine reproduce?
    if routes and lines:
        skel = unary_union(lines)
        pav_probe = pav_eff.buffer(0.5)
        covered = total_r = 0.0
        for rt in routes:
            ln = rt.chained_line
            if ln is None or ln.is_empty or getattr(rt, "is_service", False):
                continue
            n = max(2, int(ln.length / 8))
            for k in range(n):
                a = ln.interpolate(k * ln.length / n)
                b = ln.interpolate((k + 1) * ln.length / n)
                if not pav_probe.contains(a):
                    continue          # runway-riding stretch etc.
                seg = a.distance(b)
                total_r += seg
                if skel.distance(a) <= 6.0:
                    covered += seg
        if total_r > 0:
            print(f"  route-graph coverage   : "
                  f"{100.0 * covered / total_r:.1f}%  "
                  f"({covered:.0f}/{total_r:.0f} m of aircraft routes "
                  f"within 6 m of spine)")

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

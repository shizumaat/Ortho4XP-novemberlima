"""Runway-join crown-anchor probe (user ruling 2026-07-16: taxi joins
anchor to the RUNWAY EDGE value — the crowned edge — never the
centerline/crown profile).

Builds one airport in-process and reports, for EVERY taxi-centerline
runway contact (the same contact set ``grade_graph._runway_anchors`` and
the validator's runway-join check use):

  * the contact point (runway-edge crossing via the shared law
    ``grade_law.runway_join_contact``) and its lat/lon;
  * the CENTERLINE PROFILE value at the contact station (from
    ``layout._runway_redistributed_profiles``) — what SPINE CROWN v2
    anchored joins at;
  * the CROWN DROP at the contact (the crown field value at the nearest
    runway ring vertex, i.e. the drop the runway edge actually emitted
    with — dome-blended / seam-tapered where applicable);
  * the CROWNED EDGE value = profile − drop (the ruling's anchor target);
  * the nearest emitted NON-runway-surface node (the validator's pick):
    its distance and emitted value;
  * the emitted runway-surface sample at the contact
    (``_sample_runway_segment_elev`` post-writeback);
  * deltas: node − crowned_edge (the join step the ruling bans) and
    node − profile (zero ⇒ the node anchored at the profile).

Additionally sweeps for NEAR-COINCIDENT cross-shape pairs on runway
edges (a non-runway emitted vertex within ``--pair-radius`` of a runway
ring vertex but with a different emitted value) — the class the join
validator's ``d < 1e-6`` skip hid.

Usage:
    PYTHONHASHSEED=0 venv/bin/python tools/runway_join_crown_probe.py \
        KBNA [--refs 13 31] [--pair-radius 0.75] [--json /tmp/out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

os.environ.setdefault("O4_LOG_VERBOSITY", "0")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests"),
             os.path.join(ROOT, "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)


def _open_ring(coords):
    pts = list(coords)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _shape_ring_elevs(s):
    """Emitted per-vertex values of a shape's exterior ring (open), or
    None — mirrors grade_graph_validate._shape_elevs for the probe."""
    if s.polygon is None or s.polygon.is_empty:
        return None, None
    ring = _open_ring(list(s.polygon.exterior.coords))
    n = len(ring)
    if s.node_altitudes is not None:
        na = list(s.node_altitudes)
        if len(na) == n + 1:
            na = na[:-1]
        if len(na) == n and all(v is not None for v in na):
            return ring, [float(v) for v in na]
        return None, None
    if s.altitude is not None:
        return ring, [float(s.altitude)] * n
    if (s.altitude_high is not None and s.altitude_low is not None
            and n == 4):
        hi, lo = float(s.altitude_high), float(s.altitude_low)
        return ring, [hi, lo, lo, hi]
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--refs", nargs="*", default=None,
                    help="only report contacts on these runway refs")
    ap.add_argument("--pair-radius", type=float, default=0.75)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement
    from auto_patch.layout import ROLE_RUNWAY, ROLE_RUNWAY_CROSSING
    from auto_patch.grade_law import (RUNWAY_CONTACT_M, RUNWAY_JOIN_NEAR_M,
                                      runway_join_contact)
    from auto_patch.pavement.runways import _sample_runway_segment_elev
    from auto_patch.runway_redistribute import _interp_profile
    from auto_patch.crown import crown_drop_at
    from shapely.geometry import Point

    t0 = time.time()
    layout = build_airport_pavement(args.icao, xplane_root(),
                                    compute_elevations=True)
    print(f"BUILD {args.icao} {time.time() - t0:.1f}s", flush=True)

    profiles = getattr(layout, "_runway_redistributed_profiles", None) or {}
    runway_surface = [s for s in layout.shapes
                     if s.role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)
                     and s.polygon is not None and not s.polygon.is_empty]
    runways = [s for s in runway_surface if s.role == ROLE_RUNWAY]

    def _ref_match(ref):
        if not args.refs:
            return True
        parts = str(ref or "").replace("/", "+").split("+")
        return any(r in parts or r == ref for r in args.refs)

    def profile_value_at(ref, x, y):
        """Centerline profile value at the station of (x, y), per member
        ref for crossing slabs (min station distance member wins)."""
        best = None
        for part in str(ref or "").split("+"):
            p = profiles.get(part)
            if not p:
                continue
            ax, ay = p["axis_a"]
            dx, dy = p["axis_d"]
            t = ((x - ax) * dx + (y - ay) * dy) / p["axis_len2"]
            t = min(1.0, max(0.0, t))
            v = _interp_profile(p["fractions"], p["elevs"], t)
            # perpendicular distance to this member's axis
            axlen = math.sqrt(p["axis_len2"])
            perp = abs(-(x - ax) * dy + (y - ay) * dx) / max(axlen, 1e-9)
            if best is None or perp < best[0]:
                best = (perp, v, part)
        return (best[1], best[2]) if best else (None, None)

    # emitted non-runway-surface vertices (the validator's node set)
    nx, ny, ne = [], [], []
    for s in layout.shapes:
        if s.role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING):
            continue
        ring, elevs = _shape_ring_elevs(s)
        if ring is None:
            continue
        for (x, y), e in zip(ring, elevs):
            nx.append(x); ny.append(y); ne.append(e)

    # runway ring vertices with emitted values (for drop lookup + pairs)
    rwy_verts = []          # (x, y, value, ref, role)
    for s in runway_surface:
        ring, elevs = _shape_ring_elevs(s)
        if ring is None:
            continue
        for (x, y), e in zip(ring, elevs):
            rwy_verts.append((x, y, e, s.ref, s.role))

    contacts = []
    for entry in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = (entry.line if hasattr(entry, "line")
              else (entry[0] if isinstance(entry, (tuple, list)) else entry))
        is_svc = (entry.is_service if hasattr(entry, "is_service") else False)
        if ln is None or ln.is_empty or is_svc:
            continue
        cs = list(ln.coords)
        for (ex, ey) in (cs[0], cs[-1]):
            P = Point(ex, ey)
            # SOLVER-VIEW target set (lockstep with _runway_anchors +
            # the join validator): runways AND runway-crossing slabs.
            rwy = min(runway_surface, key=lambda r: r.polygon.distance(P))
            if rwy.polygon.distance(P) > RUNWAY_CONTACT_M:
                continue
            if not _ref_match(rwy.ref):
                continue
            c = runway_join_contact(ln, (ex, ey), rwy.polygon)
            cx, cy = c if c is not None else (ex, ey)
            prof_v, prof_ref = profile_value_at(rwy.ref, cx, cy)
            emit_sample = _sample_runway_segment_elev(rwy, cx, cy)
            # crown drop at the contact: crown field at the nearest
            # runway ring vertex (the drop the edge actually emitted with)
            best_rv = None
            for (x, y, e, ref, role) in rwy_verts:
                d2 = (x - cx) ** 2 + (y - cy) ** 2
                if best_rv is None or d2 < best_rv[0]:
                    best_rv = (d2, x, y, e, ref, role)
            drop = crown_drop_at(layout, best_rv[1], best_rv[2]) \
                if best_rv else 0.0
            crowned_edge = (prof_v - drop) if prof_v is not None else None
            # validator's nearest node
            best_d2, best_e, best_xy = (RUNWAY_JOIN_NEAR_M ** 2, None, None)
            for k in range(len(nx)):
                d2 = (nx[k] - cx) ** 2 + (ny[k] - cy) ** 2
                if d2 < best_d2:
                    best_d2, best_e, best_xy = d2, ne[k], (nx[k], ny[k])
            la, lo = layout.m_to_ll(cx, cy)
            contacts.append({
                "ref": rwy.ref, "target_role": rwy.role,
                "profile_ref": prof_ref,
                "contact_xy": [round(cx, 2), round(cy, 2)],
                "contact_ll": [round(la, 7), round(lo, 7)],
                "profile_value": (round(prof_v, 3)
                                  if prof_v is not None else None),
                "crown_drop": round(drop, 3),
                "crowned_edge": (round(crowned_edge, 3)
                                 if crowned_edge is not None else None),
                "emit_runway_sample": (round(float(emit_sample), 3)
                                       if emit_sample is not None else None),
                "node_dist": round(math.sqrt(best_d2), 3)
                             if best_e is not None else None,
                "node_value": (round(best_e, 3)
                               if best_e is not None else None),
                "node_xy": ([round(best_xy[0], 2), round(best_xy[1], 2)]
                            if best_xy else None),
                "delta_vs_crowned_edge": (
                    round(best_e - crowned_edge, 3)
                    if best_e is not None and crowned_edge is not None
                    else None),
                "delta_vs_profile": (
                    round(best_e - prof_v, 3)
                    if best_e is not None and prof_v is not None else None),
                # THE lockstep assertion: join vertex vs the EMITTED edge
                # of the solver-view anchor target (runway or slab).
                "delta_vs_emitted_edge": (
                    round(best_e - float(emit_sample), 3)
                    if best_e is not None and emit_sample is not None
                    else None),
            })

    # near-coincident cross-shape pairs: non-runway vertex vs runway ring
    # vertex within pair-radius with diverging emitted values
    pairs = []
    r2 = args.pair_radius ** 2
    for (x, y, e, ref, role) in rwy_verts:
        if not _ref_match(ref):
            continue
        for k in range(len(nx)):
            d2 = (nx[k] - x) ** 2 + (ny[k] - y) ** 2
            if d2 <= r2 and abs(ne[k] - e) > 0.05:
                la, lo = layout.m_to_ll(x, y)
                pairs.append({
                    "ref": ref, "role": role,
                    "rwy_xy": [round(x, 2), round(y, 2)],
                    "rwy_ll": [round(la, 7), round(lo, 7)],
                    "dist": round(math.sqrt(d2), 3),
                    "rwy_value": round(e, 3),
                    "node_value": round(ne[k], 3),
                    "step": round(ne[k] - e, 3),
                })
    # dedup pairs by rounded location
    seen = set()
    pairs_dedup = []
    for p in sorted(pairs, key=lambda p: -abs(p["step"])):
        key = (round(p["rwy_xy"][0], 1), round(p["rwy_xy"][1], 1))
        if key in seen:
            continue
        seen.add(key)
        pairs_dedup.append(p)

    # threshold end-zone grades: max |grade| across profile samples in the
    # first/last strict band of each matched ref (KBNA 2026-07-16 fix G:
    # the band must stay ≤ RUNWAY_END_GRADE).
    from auto_patch.config import RUNWAY_THRESHOLD_STRICT_M
    end_zones = []
    for ref, p in profiles.items():
        if not _ref_match(ref):
            continue
        L = math.sqrt(p["axis_len2"])
        fr, el = p["fractions"], p["elevs"]
        for (name, lo_s, hi_s) in (("A", 0.0, RUNWAY_THRESHOLD_STRICT_M),
                                   ("B", L - RUNWAY_THRESHOLD_STRICT_M, L)):
            worst = 0.0
            for k in range(1, len(fr)):
                s0, s1 = fr[k - 1] * L, fr[k] * L
                if s1 <= lo_s or s0 >= hi_s or s1 - s0 < 1e-6:
                    continue
                g = abs(el[k] - el[k - 1]) / (s1 - s0)
                worst = max(worst, g)
            end_zones.append({"ref": ref, "end": name,
                              "band_m": RUNWAY_THRESHOLD_STRICT_M,
                              "max_grade_pct": round(worst * 100.0, 3)})

    report = {"icao": args.icao, "contacts": contacts,
              "near_coincident_pairs": pairs_dedup,
              "end_zones": end_zones}
    print(f"\n== threshold end-zone grades (first/last "
          f"{RUNWAY_THRESHOLD_STRICT_M:.0f} m) ==")
    for z in end_zones:
        print(f"  {z['ref']:>8} end {z['end']}: "
              f"max {z['max_grade_pct']:.3f}%")
    print(f"\n== {len(contacts)} runway-join contact(s) ==")
    for c in contacts:
        flag = ""
        d = c["delta_vs_emitted_edge"]
        if d is not None and abs(d) > 0.05:
            flag = "  <-- STEP vs emitted edge"
        print(f"  {c['ref']:>8} {c['target_role']:<15} "
              f"contact {c['contact_ll']} "
              f"profile={c['profile_value']} drop={c['crown_drop']} "
              f"edge={c['emit_runway_sample']} "
              f"node@{c['node_dist']}m={c['node_value']} "
              f"dEdge={c['delta_vs_emitted_edge']} "
              f"dProf={c['delta_vs_profile']}{flag}")
    # INSPECT: for every contact off the emitted edge by > 0.05 m, dump
    # every emitted vertex within 1.2 m (role/ref/value/field drop) —
    # answers whether the join is one welded node or several, and which
    # writer put it off the edge.
    inspected = set()
    for c in contacts:
        d = c.get("delta_vs_emitted_edge")
        if d is None or abs(d) <= 0.05:
            continue
        key = tuple(c["contact_ll"])
        if key in inspected:
            continue
        inspected.add(key)
        cx, cy = c["contact_xy"]
        print(f"\n-- INSPECT {key} dEdge={d} --")
        for (ax, ay, av, ac, asx, asy) in (
                getattr(layout, "_runway_join_anchor_debug", None) or []):
            if (ax - cx) ** 2 + (ay - cy) ** 2 <= 4.0:
                print(f"   SOLVER-ANCHOR node=({ax:.2f},{ay:.2f}) "
                      f"value={av:.3f} drop={ac:.3f} "
                      f"sample=({asx:.2f},{asy:.2f})")
        for s in layout.shapes:
            ring, elevs = _shape_ring_elevs(s)
            if ring is None:
                continue
            for (x, y), e in zip(ring, elevs):
                if (x - cx) ** 2 + (y - cy) ** 2 <= 1.44:
                    from auto_patch.crown import crown_drop_at as _cda
                    print(f"   {s.role:<16} ref={str(s.ref)[:24]:<24} "
                          f"v=({x:.2f},{y:.2f}) value={e:.3f} "
                          f"drop={_cda(layout, x, y):.3f}")

    print(f"\n== {len(pairs_dedup)} near-coincident cross-shape pair(s) "
          f"with step > 0.05 m ==")
    for p in pairs_dedup[:40]:
        print(f"  {p['ref']:>8} {p['role']:<16} {p['rwy_ll']} "
              f"d={p['dist']} rwy={p['rwy_value']} node={p['node_value']} "
              f"step={p['step']}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)
        print(f"\nJSON -> {args.json}")


if __name__ == "__main__":
    main()

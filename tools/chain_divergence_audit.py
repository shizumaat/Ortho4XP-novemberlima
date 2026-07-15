"""Chain-divergence audit: measure how far a patch OSM is from a
CONFORMING PLANAR PARTITION — the property that any boundary shared by
two ways is the IDENTICAL vertex chain in both.

Divergence classes reported (each one is Ruppert-refinement food for
Triangle4XP — the CYXY weld bake exploded 26,727 -> 1,552,854
airport-region triangles on exactly these):

1. T-VERTEX: a node of way A lies ON (or near) the INTERIOR of a way-B
   edge without being a node of way B.  Binned by perpendicular offset:
     - exact  (< 0.1 mm): colinear overlap — Triangle4XP's colinear
       re-dicing usually survives these, but they still mint duplicate
       sub-segment constraints;
     - sub-mm / mm-cm / cm+ : NEAR-parallel non-colinear constrained
       pairs — the encroachment ping-pong class (the killers).
2. NEAR-PARALLEL PAIR: two edges from different ways, angle < 0.5 deg,
   with overlapping spans separated by 0 < d <= 15 cm (the sliver lens
   itself, independent of whether the ways share a node).
3. COINCIDENT NODES: distinct node ids at the same coordinates
   (deliberate wall node-splits are lawful; a high count on non-wall
   role pairs is a dedup failure).
4. INTERIOR EDGE CROSSING: two edges from DIFFERENT ways whose interiors
   properly cross — a single intersection point that lies strictly
   inside both edges (more than the endpoint tolerance away from every
   endpoint), NOT an endpoint touch and NOT a shared-vertex join.  Every
   such crossing forces Triangle4XP to insert a Steiner point at the
   intersection and re-dice both constrained edges, so it is the same
   refinement food as the T-vertex and near-parallel classes but arising
   from transversal geometry rather than from parallel slivers.  Not
   every crossing is a defect: at CYXY (2026-07-10 attribution) all 18
   are the crown-ridge crossing-continuity mechanism BY DESIGN — the
   crown_spine breakline crossing runway~runway_crossing internal seam
   edges (16) plus two intersecting runway ridges crossing without a
   shared node.  Attribute before treating a count as a violation.

Usage:
    venv/bin/python tools/chain_divergence_audit.py PATCH.osm [PATCH2.osm ...]
        [--tol 0.15] [--top 12]

Runs in seconds; prints per-class tables by way-role pair plus the worst
individual sites with lat/lon for probing.  Companion to
tools/wedge_audit.py (which only sees pairs sharing a node).
"""
import argparse
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

# Repo-RELATIVE and APPENDED (round-9 fix): the previous hardcoded
# main-checkout path at sys.path[0] hijacked ``auto_patch`` imports in
# any process that also runs worktree code (the known worktree
# isolation trap — importing this tool made test sessions resolve
# auto_patch from the MAIN repo).
import os as _os
sys.path.append(_os.path.join(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))), "src"))
from shapely import STRtree
from shapely.geometry import LineString, Point


def _load(path):
    tree = ET.parse(path)
    root = tree.getroot()
    first_lat = None
    nodes = {}
    for n in root.findall("node"):
        la, lo = float(n.get("lat")), float(n.get("lon"))
        if first_lat is None:
            first_lat = la
        nodes[n.get("id")] = (la, lo)
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(first_lat or 0.0))
    xy = {nid: (lo * mlon, la * mlat) for nid, (la, lo) in nodes.items()}
    ways = []
    for w in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        cls = tags.get("o4_feature") or tags.get("role") or "?"
        refs = [nd.get("ref") for nd in w.findall("nd")]
        ways.append((w.get("id"), cls, refs))
    return nodes, xy, ways


def analyze(path, tol=0.15, top=12):
    nodes, xy, ways = _load(path)
    _first_lat = next(iter(nodes.values()))[0] if nodes else 0.0
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(_first_lat))
    node_ways = defaultdict(set)          # nid -> set(way index)
    edges = []                            # (wi, ax, ay, bx, by)
    for wi, (_wid, _cls, refs) in enumerate(ways):
        for nid in refs:
            node_ways[nid].add(wi)
        for a, b in zip(refs, refs[1:]):
            ax, ay = xy[a]
            bx, by = xy[b]
            if (ax - bx) ** 2 + (ay - by) ** 2 < 1e-18:
                continue
            edges.append((wi, a, b, ax, ay, bx, by))
    edge_geoms = [LineString([(e[3], e[4]), (e[5], e[6])]) for e in edges]
    tree = STRtree(edge_geoms)

    # ── 1. T-vertices ────────────────────────────────────────────────
    BIN_EDGES = [(1e-4, "exact<0.1mm"), (1e-3, "0.1-1mm"),
                 (1e-2, "1mm-1cm"), (5e-2, "1-5cm"), (1e9, "5cm+")]

    def _bin(d):
        for lim, name in BIN_EDGES:
            if d < lim:
                return name
        return "5cm+"

    tv_by_bin = Counter()
    tv_by_pair = Counter()
    tv_worst = []
    for nid, owners in node_ways.items():
        px, py = xy[nid]
        pt = Point(px, py)
        for gi in tree.query(pt.buffer(tol)):
            wi, a, b, ax, ay, bx, by = edges[gi]
            if wi in owners:
                continue
            if a == nid or b == nid:
                continue
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = ((px - ax) * dx + (py - ay) * dy) / L2
            if t <= 0.0 or t >= 1.0:
                continue
            L = math.sqrt(L2)
            # skip projections that land near an endpoint: those are
            # near-coincident-node cases, class 3's territory
            if t * L < 1e-3 or (1.0 - t) * L < 1e-3:
                continue
            perp = abs((px - ax) * dy - (py - ay) * dx) / L
            if perp >= tol:
                continue
            # is there a node of way wi at the same spot? then the ways
            # DO share the chain here (two nids one coord — class 3)
            cls_owner = "+".join(sorted({ways[o][1] for o in owners}))
            cls_edge = ways[wi][1]
            key = tuple(sorted((cls_owner, cls_edge)))
            tv_by_bin[_bin(perp)] += 1
            tv_by_pair[(key, _bin(perp))] += 1
            la, lo = nodes[nid]
            tv_worst.append((perp, key, la, lo, nid))

    # ── 2. near-parallel pairs (angle < 0.5deg, separation <= tol) ──
    np_pairs = Counter()
    np_worst = []
    seen = set()
    COS_LIM = 0.9999619   # cos(0.5 deg)
    for gi, g in enumerate(edge_geoms):
        wi, a, b, ax, ay, bx, by = edges[gi]
        v1x, v1y = bx - ax, by - ay
        n1 = math.hypot(v1x, v1y)
        for gj in tree.query(g.buffer(tol)):
            if gj <= gi:
                continue
            wj, c, d, cx, cy, dx_, dy_ = edges[gj]
            if wj == wi:
                continue
            # shared node -> wedge_audit's domain; still count (it is
            # the same lens) but mark shared
            v2x, v2y = dx_ - cx, dy_ - cy
            n2 = math.hypot(v2x, v2y)
            cosv = abs(v1x * v2x + v1y * v2y) / (n1 * n2)
            if cosv < COS_LIM:
                continue
            # separation: max over the shorter edge's endpoints of
            # distance to the longer edge, requiring span overlap
            if n1 >= n2:
                lg, s0, s1 = g, (cx, cy), (dx_, dy_)
            else:
                lg = edge_geoms[gj]
                s0, s1 = (ax, ay), (bx, by)
            d0 = lg.distance(Point(s0))
            d1 = lg.distance(Point(s1))
            sep = max(d0, d1)
            if not (1e-9 < sep <= tol):
                continue
            # projection overlap of the shorter onto the longer
            key = tuple(sorted((ways[wi][1], ways[wj][1])))
            pk = (min(gi, gj), max(gi, gj))
            if pk in seen:
                continue
            seen.add(pk)
            np_pairs[key] += 1
            mx, my = (s0[0] + s1[0]) / 2, (s0[1] + s1[1]) / 2
            np_worst.append((sep, key, my / mlat, mx / mlon))

    # ── 4. interior edge crossings ───────────────────────────────────
    # Two edges from DIFFERENT ways whose interiors transversally cross
    # at a single point that sits strictly inside both edges.  Solved
    # analytically (segment-segment) for speed and to apply the same
    # endpoint tolerance the T-vertex class uses (1 mm): a crossing whose
    # intersection lands within that distance of any endpoint is really an
    # endpoint touch or a T-vertex and belongs to classes 1/3, not here.
    ENDPOINT_TOLERANCE = 1e-3   # metres
    xing_pairs = Counter()
    xing_worst = []
    for gi, g in enumerate(edge_geoms):
        wi, a, b, ax, ay, bx, by = edges[gi]
        rx, ry = bx - ax, by - ay
        for gj in tree.query(g):
            if gj <= gi:
                continue
            wj, c, d, cx, cy, ddx, ddy = edges[gj]
            if wj == wi:
                continue                     # same way: not a foreign cross
            sx, sy = ddx - cx, ddy - cy
            denom = rx * sy - ry * sx
            if abs(denom) < 1e-12:
                continue                     # parallel -> classes 1/2
            qx, qy = cx - ax, cy - ay
            t = (qx * sy - qy * sx) / denom  # param on edge gi
            u = (qx * ry - qy * rx) / denom  # param on edge gj
            if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
                continue                     # segments miss each other
            len_r = math.hypot(rx, ry)
            len_s = math.hypot(sx, sy)
            # intersection must sit in the interior of BOTH edges by more
            # than the endpoint tolerance, else it is an endpoint touch
            depth = min(t * len_r, (1.0 - t) * len_r,
                        u * len_s, (1.0 - u) * len_s)
            if depth < ENDPOINT_TOLERANCE:
                continue
            ix, iy = ax + t * rx, ay + t * ry
            key = tuple(sorted((ways[wi][1], ways[wj][1])))
            xing_pairs[key] += 1
            xing_worst.append((depth, key, iy / mlat, ix / mlon,
                               ways[wi][0], ways[wj][0], len_r, len_s))

    # ── 4b. SAME-WAY SELF-CROSSINGS (round-9 order, 2026-07-14) ──────
    # A way's own edges properly crossing — a self-intersecting loop.
    # The class-4 detector deliberately skips same-way pairs (foreign
    # crossings only), which let 9 self-crossing gap interior rings
    # through at CYXY.  Same analytic segment-segment test, same 1 mm
    # endpoint tolerance; ADJACENT edges (sharing a node id, including
    # a closed ring's first/last) are a shared endpoint, not a cross.
    self_x = Counter()
    self_x_worst = []
    for gi, g in enumerate(edge_geoms):
        wi, a, b, ax, ay, bx, by = edges[gi]
        rx, ry = bx - ax, by - ay
        for gj in tree.query(g):
            if gj <= gi:
                continue
            wj, c, d, cx, cy, ddx, ddy = edges[gj]
            if wj != wi:
                continue                     # foreign cross: class 4
            if len({a, b} & {c, d}):
                continue                     # adjacent edges share a node
            sx, sy = ddx - cx, ddy - cy
            denom = rx * sy - ry * sx
            if abs(denom) < 1e-12:
                continue
            qx, qy = cx - ax, cy - ay
            t = (qx * sy - qy * sx) / denom
            u = (qx * ry - qy * rx) / denom
            if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
                continue
            len_r = math.hypot(rx, ry)
            len_s = math.hypot(sx, sy)
            depth = min(t * len_r, (1.0 - t) * len_r,
                        u * len_s, (1.0 - u) * len_s)
            if depth < ENDPOINT_TOLERANCE:
                continue
            ix, iy = ax + t * rx, ay + t * ry
            self_x[ways[wi][1]] += 1
            self_x_worst.append((depth, ways[wi][1], iy / mlat,
                                 ix / mlon, ways[wi][0]))

    # ── 3. coincident node ids ───────────────────────────────────────
    by_coord = defaultdict(list)
    for nid, (la, lo) in nodes.items():
        by_coord[(la, lo)].append(nid)
    co = {c: ids for c, ids in by_coord.items() if len(ids) > 1}

    # ── report ───────────────────────────────────────────────────────
    print(f"== {path}")
    print(f"   nodes {len(nodes)}  ways {len(ways)}  edges {len(edges)}")
    total_tv = sum(tv_by_bin.values())
    print(f"   T-VERTICES (node on foreign edge interior, perp<{tol}m): "
          f"{total_tv}")
    for _lim, name in BIN_EDGES:
        if tv_by_bin.get(name):
            print(f"     {name:>12}: {tv_by_bin[name]}")
    pair_tot = Counter()
    for (key, _b), c in tv_by_pair.items():
        pair_tot[key] += c
    for key, c in pair_tot.most_common(top):
        bins = {b: n for (k, b), n in tv_by_pair.items() if k == key}
        print(f"     {key[0]} ~ {key[1]}: {c}   {bins}")
    print(f"   NEAR-PARALLEL PAIRS (angle<0.5deg, 0<sep<={tol}m): "
          f"{sum(np_pairs.values())}")
    for key, c in np_pairs.most_common(top):
        print(f"     {key[0]} ~ {key[1]}: {c}")
    print(f"   COINCIDENT-NODE coords (>=2 nids): {len(co)}")
    print(f"   INTERIOR EDGE CROSSINGS (foreign edges properly cross, "
          f"depth>={ENDPOINT_TOLERANCE * 1000:g}mm): "
          f"{sum(xing_pairs.values())}")
    for key, c in xing_pairs.most_common(top):
        print(f"     {key[0]} ~ {key[1]}: {c}")
    if xing_worst:
        print("   crossings (deepest interior first):")
        for depth, key, la, lo, wa, wb, lr, ls in sorted(
                xing_worst, reverse=True)[:max(top, len(xing_worst))]:
            print(f"     depth={depth * 1000:8.3f}mm {key[0]}~{key[1]} "
                  f"@ {la:.7f},{lo:.7f} ways={wa}/{wb} "
                  f"edgelen={lr:.2f}/{ls:.2f}m")
    print(f"   SAME-WAY SELF-CROSSINGS (a way's own edges properly "
          f"cross, depth>={ENDPOINT_TOLERANCE * 1000:g}mm): "
          f"{sum(self_x.values())}")
    for key, c in self_x.most_common(top):
        print(f"     {key}: {c}")
    if self_x_worst:
        print("   self-crossings (deepest interior first):")
        for depth, key, la, lo, wa in sorted(
                self_x_worst, reverse=True)[:max(top, len(self_x_worst))]:
            print(f"     depth={depth * 1000:8.3f}mm {key} "
                  f"@ {la:.7f},{lo:.7f} way={wa}")
    if np_worst:
        print("   worst near-parallel pairs:")
        for sep, key, la, lo in sorted(np_worst, reverse=True)[:top]:
            print(f"     sep={sep * 1000:8.3f}mm {key[0]}~{key[1]} "
                  f"@ {la:.7f},{lo:.7f}")
    print("   worst T-vertices:")
    for perp, key, la, lo, nid in sorted(tv_worst, reverse=True)[:top]:
        print(f"     perp={perp * 1000:8.3f}mm {key[0]}~{key[1]} "
              f"@ {la:.7f},{lo:.7f} nid={nid}")
    print()
    return (total_tv, sum(np_pairs.values()), sum(xing_pairs.values()),
            sum(self_x.values()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--tol", type=float, default=0.15)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    for p in args.paths:
        analyze(p, tol=args.tol, top=args.top)

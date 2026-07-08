"""Count near-zero-angle wedges: pairs of constrained edges sharing a node,
angle < 0.5 deg, where the shorter edge's far endpoint is NOT a shared node
and lies within 20 cm of the longer edge (epsilon divergence)."""
import sys, math
import xml.etree.ElementTree as ET
sys.path[:0] = ["/Users/noah/Ortho4XP-novemberlima/src"]
from shapely.geometry import LineString, Point
from collections import Counter, defaultdict

def analyze(path, label, LAT):
    tree = ET.parse(path); root = tree.getroot()
    mlat = 111320.0; mlon = 111320.0*math.cos(math.radians(LAT))
    nodes = {}
    for n in root.findall("node"):
        la, lo = float(n.get("lat")), float(n.get("lon"))
        nodes[n.get("id")] = (lo*mlon, la*mlat, la, lo)
    incident = defaultdict(list)   # nodeid -> (otherid, wayid, cls)
    for w in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        cls = tags.get("o4_feature") or tags.get("role") or "?"
        refs = [nd.get("ref") for nd in w.findall("nd")]
        for a, b in zip(refs, refs[1:]):
            incident[a].append((b, w.get("id"), cls))
            incident[b].append((a, w.get("id"), cls))
    wedges = []
    for nid, inc in incident.items():
        if len(inc) < 2: continue
        x0, y0 = nodes[nid][:2]
        for i in range(len(inc)):
            for j in range(i+1, len(inc)):
                (b1, w1, c1), (b2, w2, c2) = inc[i], inc[j]
                if b1 == b2: continue           # same far node = shared edge, fine
                if w1 == w2: continue           # same-way kink, usually fine
                v1 = (nodes[b1][0]-x0, nodes[b1][1]-y0)
                v2 = (nodes[b2][0]-x0, nodes[b2][1]-y0)
                n1 = math.hypot(*v1); n2 = math.hypot(*v2)
                if not n1 or not n2: continue
                cosv = (v1[0]*v2[0]+v1[1]*v2[1])/(n1*n2)
                if cosv < 0.9999619: continue   # angle > 0.5 deg
                ang = math.degrees(math.acos(min(1, cosv)))
                # divergence of the shorter far endpoint from the longer edge
                if n1 < n2:
                    e = LineString([(x0,y0), (nodes[b2][0], nodes[b2][1])])
                    d = e.distance(Point(nodes[b1][:2]))
                else:
                    e = LineString([(x0,y0), (nodes[b1][0], nodes[b1][1])])
                    d = e.distance(Point(nodes[b2][:2]))
                if 1e-9 < d < 0.2:
                    wedges.append((ang, d, c1, c2, nid, nodes[nid][2], nodes[nid][3]))
    pairs = Counter(tuple(sorted((c1, c2))) for _,_,c1,c2,_,_,_ in wedges)
    print(f"== {label}: {len(wedges)} near-zero-angle wedges (angle<0.5deg, divergence<20cm)")
    for p, c in pairs.most_common(6): print(f"    {p}: {c}")
    for w in sorted(wedges)[:4]:
        print(f"    ang={w[0]:.4f}deg div={w[1]*1000:.2f}mm {w[2]}~{w[3]} @ {w[5]:.5f},{w[6]:.5f}")
    print()
    return len(wedges)

import os


def _lat_of(path, override=None):
    """Best-effort tile latitude for the meter projection: use the OSM's
    first node lat (the projection only needs the cos(lat) scale, so any
    node in the patch is fine), or an explicit override."""
    if override is not None:
        return override
    try:
        for _ev, el in ET.iterparse(path, events=("start",)):
            if el.tag == "node":
                return float(el.get("lat"))
    except (ET.ParseError, OSError):
        pass
    return 0.0


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args:
        # CLI: one or more patch OSM files; optional trailing LAT override
        # (``wedge_audit.py file1.osm [file2.osm ...] [--lat 35]``).
        lat = None
        files = []
        it = iter(args)
        for a in it:
            if a in ("--lat", "-l"):
                lat = float(next(it))
            else:
                files.append(a)
        for f in files:
            analyze(f, os.path.basename(f), _lat_of(f, lat))
    else:
        # Default batch: the main-tree Patches fixtures (the KJQF/+35-081
        # regression set + HECA).  Latitude is read per-file so any tile
        # works.
        base = "/Users/noah/Ortho4XP-novemberlima/Patches/+30-090/+35-081"
        for a in ["KJQF", "KCLT", "KEXX", "KSVH", "KVUJ", "KRUQ",
                  "KEQY", "KAFP"]:
            p = f"{base}/{a}_auto.patch.osm"
            if os.path.exists(p):
                analyze(p, f"{a} fresh", _lat_of(p, 35))
        heca = ("/Users/noah/Ortho4XP-novemberlima/Patches/"
                "+30+030/+30+031/HECA_auto.patch.osm")
        if os.path.exists(heca):
            analyze(heca, "HECA (17:23)", _lat_of(heca, 30))

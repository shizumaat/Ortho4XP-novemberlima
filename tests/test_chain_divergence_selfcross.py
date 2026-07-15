"""Audit class 4b — SAME-WAY SELF-CROSSING (round-9 order 2026-07-14).

``tools/chain_divergence_audit.py``'s class-4 detector deliberately
skips same-way pairs, which let 9 self-crossing gap interior rings
through at CYXY.  Class 4b closes the hole: a way whose OWN edges
properly cross (1 mm endpoint tolerance, adjacent edges exempt) is
reported with its way id, feature class and intersection position.

Synthetic pins:
  * a bowtie (self-crossing) way -> exactly one 4b finding;
  * a plain simple ring          -> zero findings;
  * a closed ring's first/last shared node is NOT a self-cross.
"""
import os
import sys

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from chain_divergence_audit import analyze          # noqa: E402


def _write_osm(path, ways):
    """Minimal OSM in the auditor's expected single-quote format.
    ``ways`` = list of coordinate rings (closed by repeating the
    first node id)."""
    lines = ["<osm version='0.6' generator='test'>"]
    nid = 0
    way_blocks = []
    for coords in ways:
        refs = []
        for lat, lon in coords:
            nid -= 1
            lines.append(
                f"<node id='{nid}' lat='{lat:.9f}' lon='{lon:.9f}' />")
            refs.append(nid)
        way_blocks.append(refs)
    wid = -1000
    for refs in way_blocks:
        wid -= 1
        lines.append(f"<way id='{wid}'>")
        for r in refs + [refs[0]]:
            lines.append(f"<nd ref='{r}'/>")
        lines.append("<tag k='o4_feature' v='gap_interior_ring'/>")
        lines.append("</way>")
    lines.append("</osm>")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def test_bowtie_is_one_self_crossing_and_square_is_clean(tmp_path):
    scale = 1.0 / 111320.0                 # ~1 m in degrees
    # Bowtie: (0,0) -> (10,10) -> (10,0) -> (0,10) -> close: the two
    # diagonals properly cross at (5,5).
    bowtie = [(0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0)]
    # Simple square ring: no findings (closed-ring first/last repeat
    # shares a node id — adjacency exemption, not a cross).
    square = [(100.0, 100.0), (100.0, 120.0), (120.0, 120.0),
              (120.0, 100.0)]
    p = tmp_path / "selfcross.osm"
    _write_osm(str(p), [
        [(y * scale, x * scale) for x, y in bowtie],
        [(y * scale, x * scale) for x, y in square],
    ])
    _tv, _np, _x, self_x = analyze(str(p))
    assert self_x == 1, (
        f"expected exactly the bowtie's one self-crossing, got {self_x}")

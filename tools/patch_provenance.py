"""Read and print the provenance stamp from auto_patch ``.patch.osm`` files.

Every patch ``PavementLayout.to_osm`` writes carries a provenance block on its
``<osm>`` root (see ``auto_patch.provenance``): the git sha + dirty flag of the
source tree at build time, the active gate configuration, which airport-
elevation insets baked into the DEM the patch was graded on (or a loud RAW
marker when none did), and the build timestamp.  This tool decodes that block
so you can tell — WITHOUT forensics — how any bake was produced.

It reads only the root line of each file, so it is seconds-fast on any patch
size and never triggers a build.

Usage (from anywhere):
    python tools/patch_provenance.py PATCH.patch.osm [MORE.patch.osm ...]
    python tools/patch_provenance.py path/to/Patches/zOrtho4XP_<tile>/
    python tools/patch_provenance.py PATCH.patch.osm --json

Exit status (for CI gating):
    0  every input carries a provenance stamp and none was built dirty
    1  at least one input lacks a stamp, or was built from a dirty tree,
       or was graded on the raw base DEM (no inset) -- use --strict-raw to
       treat raw-DEM as a failure; by default raw-DEM only warns
    2  a path could not be read / no patches found
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# Import the canonical parser from the source tree next to this tool, so the
# tag schema has ONE definition.  The tool lives in ``<root>/tools/``.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from auto_patch.provenance import parse_patch_provenance  # noqa: E402


def _collect_patch_files(paths: list[str]) -> list[str]:
    """Expand directories to their ``*.patch.osm`` files; keep files as-is."""
    files: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            files.extend(sorted(glob.glob(os.path.join(path, "*.patch.osm"))))
        else:
            files.append(path)
    return files


def _print_human(path: str, prov: dict | None) -> None:
    name = os.path.basename(path)
    if prov is None:
        print(f"{name}: NO PROVENANCE STAMP (unstamped or unreadable)")
        return
    sha = prov["sha"] or "absent"
    dirty = prov["dirty"]
    dirty_note = {
        "true": "  DIRTY TREE",
        "false": "",
        "unknown": "  (dirty unknown)",
    }.get(dirty, f"  (dirty={dirty})")
    print(f"{name}:")
    print(f"    icao       : {prov['icao'] or '?'}")
    print(f"    built      : {prov['built'] or '?'}")
    print(f"    source sha : {sha}{dirty_note}")
    on = prov["gates_on"]
    print(f"    gates ON   : {len(on)}/{prov['gates_total']} "
          f"({', '.join(on) if on else 'none'})")
    nondefault = prov["gates_nondefault"]
    if nondefault:
        print(f"    gate drift : {', '.join(nondefault)}")
    else:
        print("    gate drift : none (all gates at default)")
    if prov["dem_raw"]:
        print(f"    elevation  : {prov['dem']}  <-- WARNING: raw base DEM")
    else:
        print(f"    elevation  : {prov['dem']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the provenance stamp of auto_patch .patch.osm files.")
    parser.add_argument("paths", nargs="+",
                        help="patch files and/or Patches tile directories")
    parser.add_argument("--json", action="store_true",
                        help="emit the decoded records as one JSON array")
    parser.add_argument("--strict-raw", action="store_true",
                        help="also fail (exit 1) when a patch was graded on "
                             "the raw base DEM (no inset)")
    args = parser.parse_args(argv)

    files = _collect_patch_files(args.paths)
    if not files:
        print("no .patch.osm files found in the given paths", file=sys.stderr)
        return 2

    records = []
    any_missing = False
    any_dirty = False
    any_raw = False
    unreadable = False
    for path in files:
        if not os.path.isfile(path):
            print(f"{path}: NOT FOUND", file=sys.stderr)
            unreadable = True
            continue
        prov = parse_patch_provenance(path)
        records.append({"path": path, "provenance": prov})
        if prov is None:
            any_missing = True
        else:
            if prov["dirty"] == "true":
                any_dirty = True
            if prov["dem_raw"]:
                any_raw = True

    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        for record in records:
            _print_human(record["path"], record["provenance"])

    if unreadable:
        return 2
    if any_missing or any_dirty or (args.strict_raw and any_raw):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

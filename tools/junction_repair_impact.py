#!/usr/bin/env python3
"""M7a — measure the per-pass impact of junction_repair on a real build.

Auto-wraps every ``junction_repair`` top-level function whose first parameter is
``layout`` (the layout-mutating "passes"), runs a full build, and reports each
pass's delta in (#shapes, #ring-vertices, per-role shape counts) summed over its
invocations.  This separates LOAD-BEARING passes from NO-OPs to guide the M7b
modularisation (docs/cleanup_consolidation_plan.md).

    venv/bin/python tools/junction_repair_impact.py [ICAO ...]
        # default fixtures: CYXY OEMA HECA SPJC

Lazy ``from .junction_repair import _pass`` calls inside pipeline pick up the
wrapped module attributes, so patching the module before the build suffices.
"""
from __future__ import annotations

import inspect
import sys
from collections import Counter

DEFAULT_ICAOS = ["CYXY", "OEMA", "HECA", "SPJC"]


def _snapshot(layout):
    n_shapes = len(layout.shapes)
    n_verts = 0
    for s in layout.shapes:
        p = getattr(s, "polygon", None)
        if p is not None and not p.is_empty:
            try:
                n_verts += len(p.exterior.coords) - 1
            except Exception:
                pass
    roles = Counter(s.role for s in layout.shapes)
    return n_shapes, n_verts, roles


def _find_layout(args):
    for a in args:
        if hasattr(a, "shapes") and hasattr(a, "to_osm"):
            return a
    return None


def _install_wrappers(JR, stats):
    """Wrap each top-level fn whose first param is 'layout'.  Returns names."""
    wrapped = []
    for name, fn in list(vars(JR).items()):
        if not (inspect.isfunction(fn) and fn.__module__ == JR.__name__):
            continue
        try:
            params = list(inspect.signature(fn).parameters)
        except (ValueError, TypeError):
            continue
        if not params or params[0] != "layout":
            continue

        def make(orig, nm):
            def wrapper(*args, **kwargs):
                layout = _find_layout(args) or kwargs.get("layout")
                if layout is None:
                    return orig(*args, **kwargs)
                b = _snapshot(layout)
                ret = orig(*args, **kwargs)
                a = _snapshot(layout)
                st = stats.setdefault(
                    nm, {"calls": 0, "d_shapes": 0, "d_verts": 0,
                         "roles": Counter(), "ret": 0})
                st["calls"] += 1
                st["d_shapes"] += a[0] - b[0]
                st["d_verts"] += a[1] - b[1]
                for r in set(a[2]) | set(b[2]):
                    dv = a[2][r] - b[2][r]
                    if dv:
                        st["roles"][r] += dv
                if isinstance(ret, int):
                    st["ret"] += ret
                elif ret:
                    st["ret"] += 1
                return ret
            return wrapper
        wrapper = make(fn, name)
        setattr(JR, name, wrapper)
        # Rebind any OTHER already-loaded auto_patch module that imported this
        # pass at module-load time (e.g. elevation.py top-level imports), whose
        # binding points at the original and would otherwise bypass the wrapper.
        for mod in list(sys.modules.values()):
            if (mod is None or mod is JR
                    or not getattr(mod, "__name__", "").startswith("auto_patch")):
                continue
            for attr, val in list(vars(mod).items()):
                if val is fn:
                    setattr(mod, attr, wrapper)
        wrapped.append(name)
    return wrapped


def main():
    icaos = sys.argv[1:] or DEFAULT_ICAOS
    from conftest import xplane_root
    # Import pipeline FIRST — junction_repair <-> elevation is a cycle that only
    # resolves when entered via pipeline (CLAUDE.md gotcha); a direct
    # ``import auto_patch.junction_repair`` first raises a partial-init ImportError.
    from auto_patch.pipeline import build_airport_pavement
    import auto_patch.junction_repair as JR

    stats: dict = {}
    wrapped = _install_wrappers(JR, stats)
    print(f"wrapped {len(wrapped)} layout-passes in junction_repair\n")

    for icao in icaos:
        try:
            build_airport_pavement(icao, xplane_root(), compute_elevations=True)
        except Exception as e:                                  # pragma: no cover
            print(f"  !! {icao} build failed: {e}")

    # Report
    print("\n=== junction_repair per-pass impact "
          f"(summed over {','.join(icaos)}) ===")
    hdr = f"{'pass':<48}{'calls':>6}{'Δshapes':>9}{'Δverts':>9}{'ret':>7}  roles"
    print(hdr)
    print("-" * len(hdr))
    load_bearing, noops = [], []
    for name in sorted(stats, key=lambda n: (-abs(stats[n]["d_shapes"]),
                                             -abs(stats[n]["d_verts"]),
                                             -stats[n]["ret"])):
        st = stats[name]
        roledesc = ", ".join(f"{r}{'+' if v > 0 else ''}{v}"
                             for r, v in st["roles"].most_common())
        changed = (st["d_shapes"] or st["d_verts"] or st["roles"] or st["ret"])
        print(f"{name:<48}{st['calls']:>6}{st['d_shapes']:>9}"
              f"{st['d_verts']:>9}{st['ret']:>7}  {roledesc}")
        (load_bearing if changed else noops).append(name)
    # passes that were wrapped but never called at all
    never = [n for n in wrapped if n not in stats]
    print(f"\nLOAD-BEARING (changed geometry/roles): {len(load_bearing)}")
    print(f"NO-OP when called (0 delta, 0 ret): {len(noops)}")
    for n in noops:
        print(f"    {n}")
    print(f"NEVER CALLED on these fixtures: {len(never)}")
    for n in never:
        print(f"    {n}")


if __name__ == "__main__":
    main()

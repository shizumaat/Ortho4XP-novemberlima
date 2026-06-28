#!/usr/bin/env python3
"""Transitive module-level symbol closure within a single Python module.

Given a module file and a set of SEED top-level names, compute every other
top-level name (function / class / assignment) that the seeds reach via
name references — i.e. the minimal set you must KEEP if you want those seeds
to work, and (by complement) the set that is safe to DELETE.

Used by the M2 cleanup (docs/cleanup_consolidation_plan.md) to carve the
elevation-neutral primitives out of ``unified_jacobi`` before deleting the
legacy cascade.  Reusable: point it at any module + seed list.

    venv/bin/python tools/symbol_closure.py <module.py> NAME [NAME ...]

Prints: closure (keep) set, complement (delete) set, and line totals for each
(approximate, summed over each top-level definition's span).
"""
from __future__ import annotations

import ast
import sys


def _toplevel_defs(tree: ast.Module):
    """Map top-level name -> (node, set_of_names_referenced_inside)."""
    defs: dict[str, tuple[ast.AST, set[str]]] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names += [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        for nm in names:
            defs[nm] = (node, refs)
    return defs


def closure(path: str, seeds: list[str]):
    src = open(path).read()
    tree = ast.parse(src)
    defs = _toplevel_defs(tree)
    universe = set(defs)

    keep: set[str] = set()
    stack = [s for s in seeds if s in defs]
    missing = [s for s in seeds if s not in defs]
    while stack:
        cur = stack.pop()
        if cur in keep:
            continue
        keep.add(cur)
        _, refs = defs[cur]
        for r in refs:
            if r in universe and r not in keep:
                stack.append(r)

    def span(name: str) -> int:
        node, _ = defs[name]
        end = getattr(node, "end_lineno", node.lineno)
        return end - node.lineno + 1

    delete = universe - keep
    return keep, delete, missing, defs, span


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    path, seeds = sys.argv[1], sys.argv[2:]
    keep, delete, missing, defs, span = closure(path, seeds)

    keep_lines = sum(span(n) for n in keep)
    del_lines = sum(span(n) for n in delete)

    print(f"module: {path}")
    print(f"seeds ({len(seeds)}): {sorted(seeds)}")
    if missing:
        print(f"!! seeds NOT found as top-level defs: {sorted(missing)}")
    print(f"\nKEEP closure: {len(keep)} symbols, ~{keep_lines} lines")
    for n in sorted(keep):
        print(f"    {n}  ({span(n)}L)")
    print(f"\nDELETE complement: {len(delete)} symbols, ~{del_lines} lines")
    for n in sorted(delete):
        print(f"    {n}  ({span(n)}L)")


if __name__ == "__main__":
    main()

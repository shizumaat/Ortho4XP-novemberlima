# Pipeline geometry-mutation audit (2026-06-18)

Audit of every geometry-modifying step from taxi-rect creation to the saved
OSM patch, captured to inform a **v2 architecture** that finalizes geometry
once and solves once. Traced on a full SPJC build (gate-off baseline) by
wrapping every pass that takes `layout` and recording call order + shape /
vertex / area deltas + pre/post-solve status.

> This is a snapshot of the CURRENT flow and its problems. It is the
> reference for simplifying the flow later — not a description of the
> intended end state. Re-trace before acting on it (`/tmp/audit.py`
> pattern: import `pipeline` first to avoid the `junction_repair`↔`elevation`
> import cycle, then monkeypatch-wrap the pass functions).

## Intended architecture (the rule)
Finalize **all** geometry → solve elevations **once** → emit terrain
features (which carry their own DEM elevations) → never move airside
geometry again. There is a guard for the last clause:
`snapshot_airside_geometry` (`pipeline.py`, just before the solve) +
`report_post_solve_changes` (end) report any airside vertex that moved
after the solve.

**The post-solve half honors the rule. The pre-solve half is a long,
doubled-up repair chain split across two files.**

## Execution order (SPJC trace)

### A. Build (no elevations yet)
- `_build_taxi_rects` + transforms: pad-clip, drop-degenerate sub-quads,
  `_snap_rect_sloping_edges_to_holes`, `split_long_rects_along_terrain`.
- emit rects → `layout.shapes`.
- **`emit_junctions`** (`junction_emit.py`): residue =
  `pav_union − rects − terminals − runway`, hole-decompose, drop orphan
  strips / slivers, snap to canonical registry. Creates ALL junctions.
- service-road rects + service_junctions.

### B. Pre-solve repair chain — split across `_compute_elevations` and `pipeline`

`✱` = mutated geometry on this build.

Inside `elevation._compute_elevations`:

| # | pass | ✱ | why / origin |
|---|------|---|--------------|
| 2 | `_resolve_runway_crossings` | | runway×pavement crossings |
| 3 | `_push_junction_vertices_off_taxi_rect_edges` | | Rule 2 (junction verts off rect sloping edges) |
| 4 | `_insert_rect_corners_into_grazing_junction_edges` | ✱ | conformance |
| 5 | `_enforce_shared_vertices` | ✱ | cluster near-coincident verts |
| 6 | `_drop_overlap_against_fixed_shapes` | ✱ | **overlap-clip #1** |
| 7 | `_enforce_shared_vertices` | ✱ | **re-run of #5** after #6 |
| 8 | `_push_junction_vertices_off_taxi_rect_edges` | | **re-run of #3** |
| 9 | `_merge_sliver_junctions_into_neighbours` | ✱ | merge small junctions (HECA) |
| 10 | `_absorb_wedge_rects_into_junctions` | | wedge rect → junction (KPHL K5) |
| 11 | `_drop_thin_orphan_slivers` | ✱ | thin residue along rect long edges (SPJC stub C) |

(also in this window, not in the wrapped set: runway seam/redistribute,
`_reclassify…→groundside`, groundside DEM emit, neck-split.)

Back in `pipeline` (after `compute_elevations_and_repair_geometry` returns):

| # | pass | ✱ | why / origin |
|---|------|---|--------------|
| 13 | `widen_junctions_to_runway_corners` | ✱ | runway-corner sharing |
| 14 | `stitch_pavement_to_flat_runways` | | |
| 15 | `stitch_pavement_to_terminals` | ✱ | weld pavement to terminal pads |
| 16 | `stitch_pavement_polygons` | | |
| 17 | `_split_sloped_rects_at_violations` | ✱ | split rect at junction-vertex sloping-edge violation |
| 18 | `_snap_junction_vertices_to_rect_flat_edge_corners` | | |
| 19 | `_absorb_wedge_rects_into_junctions` | | **re-run of #10** |
| 20 | `_reclassify_apron_junctions` | | junction↔apron re-role (+service, +groundside) |
| 21 | `_snap_to_sloping_edge_corners` | ✱ | snap junction verts to rect corners |
| 22 | `nudge_runway_corners_at_seam_junctions` | | tile-seam |
| 23 | `_drop_overlap_against_fixed_shapes` | ✱ | **overlap-clip #2** |
| 24 | `merge_small_apron_fragments` | | |
| 25 | `_compute_boundary_ribbon_interior` | | computes ribbon region |
| 26 | `_drop_floating_orphan_junctions` | | drop orphan wedges (SPLP #33) |
| 27 | `_drop_off_source_residue` | | drop off-pavement residue |
| 28 | `_decompose_airside_holed_shapes` | ✱ | hole-free decompose |
| 29 | `_drop_off_source_residue` | | **re-run of #27** |
| **30** | **`apply_junction_centerline_spine`** | ✱ | **slice junctions along centerlines** |
| 36 | `_unify_airside_geometry` → `weld_layout_vertices` + **`enforce_conformance`** | ✱ | **rects flip to `node_altitudes` here** (inserts shared-boundary verts) |
| 37 | `_drop_overlap_against_fixed_shapes` | ✱ | **overlap-clip #3** |

### C. Solve (once)
`per_surface_solve` — assigns every airside altitude in one pass.

### D. Post-solve
| pass | airside? | note |
|------|----------|------|
| `_smooth_junction_ring_curvature` | **YES** | in-plane nudge of *free* junction ring verts AFTER the solve — the one rule violation |
| `emit_terrain_transition_features` | no | boundary ribbon, groundside, tunnels, clearance cuts → new feature shapes, own DEM elevations |
| `enforce_conformance` (one-sided, owner=features) | no | inserts verts into features only; airside frozen |
| ribbon conform / bridge-neck flatten / boundary clip | no | features only |
| `_dedup_coincident_ring_vertices` | yes | removes zero-length edges, altitude-preserving |
| `report_post_solve_changes` | — | guard: reports any airside vertex that moved |

## Findings (targets for v2)

1. **Doubled passes** — the "nudge → re-fix → nudge → re-fix" signature of
   incremental repair: `_drop_overlap_against_fixed_shapes` ×3,
   `_enforce_shared_vertices` ×2, `_push_junction_vertices_off_taxi_rect_edges`
   ×2, `_absorb_wedge_rects_into_junctions` ×2, `_drop_off_source_residue` ×2.
   Plus a whole legacy snap chain in `finalize.py` (≈ lines 246–274,
   `_snap_junction_altitudes_to_rect_corners` ×3, `_enforce_shared_vertex_altitudes`
   ×2, adjacent-pair smoother) gated OFF under the per-surface solver but
   still resident.

2. **Geometry finalize split across two files** — `_compute_elevations`
   (steps 2–11) then `pipeline` (13–37), with the *big* ops (split rects,
   reclassify, the **spine**, unify/conformance) on the `pipeline` side and
   the solve at the very end. "Finalize, then solve" is the intent but it's a
   ~35-step chain spread across `elevation._compute_elevations` and `pipeline`.
   v2 should make Phase-1 geometry one ordered module that ends with a single
   `enforce_conformance`, then hand a frozen mesh to the solver.

3. **Shapes are repeatedly torn down and rebuilt, losing metadata.**
   Junctions get reconstructed as fresh `BuiltShape`s in at least
   `pavement/vertices.py:660` & `:933`, `elevation.py:939`,
   `junction_repair.py:1669` — none copy non-default fields. This is why the
   `is_rect_cap` marker is gone by step 9. v2 should either carry shape
   identity/metadata through rebuilds (e.g. `dataclasses.replace`) or stop
   rebuilding shapes mid-chain.

4. **One genuine post-solve airside move**: `_smooth_junction_ring_curvature`
   (`pipeline.py`, right after `per_surface_solve`) nudges free junction ring
   vertices in-plane. Small and shared-vertex-safe, but it violates the freeze
   rule — fold it into the pre-solve geometry or justify it explicitly.

5. **The spine runs LAST in the pre-solve chain.** Any geometry it depends on
   (e.g. rect end-caps that keep its slice off a rect's sloping edge) must
   either be created right before it, or survive every dissolve/merge/rebuild
   pass between creation and step 30. The 2026-06-18 rect-end-cap work carves
   the caps **immediately before the spine** for exactly this reason — see
   `rect_end_caps.py` and STATUS.md.

## Re-trace recipe
`/tmp/audit.py`: import `auto_patch.pipeline` first (import-cycle), wrap each
pass name (harvest with `grep -hoE "[A-Za-z_]+\(\s*layout"` over `pipeline.py`,
`finalize.py`, `elevation.py`), record `(len(shapes), total_verts,
round(total_area,1), n_with_elevation)` before/after each call, print
depth-0/1 entries in call order. `per_surface_solve` lives in
`elevation_per_surface` (wrap there if you need the exact solve boundary).

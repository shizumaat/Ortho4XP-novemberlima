# DSF object anchors: floating and sunken scenery objects

**Status:** investigated, prototyped, verified in-sim at KCLT. Not yet integrated into auto_patch.
**Prototype tools:** `tools/obj8_geometry.py`, `tools/mesh_elevation_sampler.py`,
`tools/dsf_object_anchor_audit.py`, `tools/reanchor_kclt_terminal_bakes.py`
**Baseline:** dev @ `6ac66cd`. All line references below are from that commit.
**Author's note to the next agent:** the two hardest facts in this document are
[§4](#4-why-the-obvious-fixes-do-not-work) (why moving the handle to the centroid is wrong) and
[§7.3](#73-why-both-phases-are-required) (why footprint grading alone changes nothing). Everything
else follows from those.

---

## 1. TL;DR

X-Plane renders a terrain-draped DSF `OBJECT` by sampling the terrain elevation **under the object's
anchor** — the placement lon/lat, which is the object's local origin — and placing the entire rigid
mesh relative to that one elevation. It never looks at where the geometry actually is.

Scenery authors routinely bake dozens of buildings into a single `.obj` whose local origin sits
hundreds of metres from any geometry, or share one anchor across a whole family of objects. On flat
terrain this is invisible. On Ortho4XP's real, graded terrain every structure inherits the anchor's
elevation and floats or sinks.

At KCLT (Nimbus pack) the eight `Charlotte_Airport_00X_ALB.obj` bakes **share one anchor whose
nearest solid vertex is 693 m away**. Pooled, they contain ~105 real structures spread over 2.2 km.
Measured per mesh component, `|rendered base − ground beneath it|` had a median of 3.06 m and a
maximum of 15.07 m; 92% of components were more than 0.5 m off.

The working correction adds `ground_under_structure − ground_under_anchor` to the **y** coordinate of
each structure's vertices. It touches only `.obj` files, rewrites no DSF, adds no draw calls, and is
byte-idempotent and reversible. After it: median 0.12 m, mean 0.31 m.

Two capabilities fall out of this, and **auto_patch needs both** ([§7](#7-integration-plan)):

* **Phase 1 — footprints.** OBJ8 structure footprints are a building-pad source auto_patch is blind
  to today. At KCLT that is ~105 buildings it cannot currently see.
* **Phase 2 — the y-bake.** A post-build step that corrects the pack against the *final* mesh.

---

## 2. The physics

DSF placement commands, as emitted by `DSFTool --dsf2text`:

| command | columns | elevation |
|---|---|---|
| `OBJECT` | `idx lon lat heading` | terrain under the anchor — **the affected case** |
| `OBJECT_MSL` | `idx lon lat elev heading` | absolute MSL |
| `OBJECT_AGL` | `idx lon lat elev heading` | height above ground |

Note the heading column moves. `src/auto_patch/dsf_reader.py:701` already handles this.

OBJ8 local coordinates, before the placement heading is applied:

```
+x = east      +y = up      +z = south
```

The heading rotates the object clockwise from north:

```
east  = x·cos(heading) − z·sin(heading)
south = x·sin(heading) + z·cos(heading)
```

This matches `keep_obj8` at `src/O4_Vector_Map.py:1175`, which is the only existing OBJ8 reader in
the tree. **Verify the sign against a known object before trusting any new code**: at KCLT the
correct sign places a hangar 39 m from its independently-measured position, and the wrong sign places
it 1021 m away.

A vertex therefore renders at absolute elevation `terrain(anchor) + v.y`. That is the whole problem
in one line: `v.y` is authored relative to a ground plane the author assumed was flat.

---

## 3. Evidence at KCLT

Pack: `Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12`, DSF
`Earth nav data/+30-090/+35-081.dsf`.

* 334 `OBJECT_DEF`; 12,320 draped `OBJECT` placements; 2,480 `OBJECT_AGL`; 0 `OBJECT_MSL`.
* 296 defs have solid geometry; 38 are light-only (`POINT_COUNTS 0 0 0 0`).
* **57 defs have a solid reach > 25 m.** That is the whole actionable list — a short one.

The eight bakes share one placement exactly:

```
OBJECT 30 -80.935041390 35.207360571 86.095674
```

* Nearest solid vertex to that anchor: **693 m**.
* `007` alone: 21,182 vertices, 245 components, bounding box 1257 × 1626 m.
* Pooled across all eight: 3,193 components → 105 structures at a 2 m grouping gap.
* Separately, **41** terminal-layer objects (`paredes` walls, `techos` roofs, `vidrios` glass,
  `suelos` floors, `modulos`) share a *second* anchor. These are **not yet corrected.**

**The eight bakes are split by texture page, not by building.** Four of `007`'s five structures share
vertices at **0.00 m** with `001`/`002`/`004`/`006`/`008`. They are the same buildings. Any correction
applied to one file alone tears its walls away from its roof.

---

## 4. Why the obvious fixes do not work

### 4.1 "Move the handle to the geometry centroid" — wrong

This is the intuitive fix and it fails, because **a placement carries exactly one elevation** while
these objects span terrain that varies by tens of metres. Measured:

| strategy | worst error, `007` | worst error, `003` |
|---|---|---|
| current anchor | 9.95 m | 18.24 m |
| move anchor → centroid | **10.18 m (worse)** | 15.57 m |
| one anchor per structure | ~0 | ~0 |

For `007` the centroid lands in the 1644 m gap between two building groups and is close to neither.
The fix is only correct for the special case of *one compact structure with an offset origin* — which
describes **none** of the KCLT offenders.

### 4.2 "Flatten the terrain under the object to the anchor's elevation" — destructive

This is what the author implicitly assumed. Honouring it means flattening 2.2 km of airfield to a
single elevation, obliterating the runway profile and every apron grade. Reject.

A subtler variant — flatten each *structure's* pad to the anchor elevation — yields ~105 plateaus at
219.83 m with retaining walls up to 15 m, several of them inside runway and apron jurisdiction. Also
reject.

### 4.3 "Split the object into one placement per structure" — correct, but heavy

Verified working. `tools/` history contains a splitter that turned `007`'s 245 components into 5
per-structure `.obj` files plus 5 `OBJECT` placements, with **world position preserved to 6.6 mm** and
surface area conserved exactly (43,542 m²). Costs:

* Rewrites the third-party DSF. `DSFTool --dsf2text` → `--text2dsf` round-trips at ~7 cm positional /
  0.005° heading (pool quantisation) — acceptable, but it is a rewrite.
* Turns 1 draw call into ~50 per object. Batching is *why* the author baked them.
* `TEXTURE` paths in the OBJ header are relative to the `.obj`, so split parts must stay in the same
  directory or have their paths rewritten.

Keep as a fallback. It is the "production-shaped" fix if the y-bake ever proves insufficient.

### 4.4 "Convert `OBJECT` → `OBJECT_MSL`" — unverified, and insufficient

Would fix a single-structure object with a distant anchor without touching the `.obj` at all. Does
**not** fix a multi-structure object (still one elevation). **I never verified that X-Plane honours
`OBJECT_MSL` for scenery objects.** The pack uses `OBJECT_AGL` 2,480 times, so the command family is
real, but treat this as an untested idea.

---

## 5. The correction that works: bake the offset into `y`

For each structure `S`:

```
delta(S) = ground_under(centroid(S)) − ground_under(anchor)
v.y += delta(S)   for every vertex v of S
```

X-Plane still places the object's `y=0` plane at `terrain(anchor)`, so structure `S` now renders its
base at `terrain(anchor) + delta(S) = ground_under(S)`. It sits on its own ground.

Properties, all verified:

* Only `.obj` files change. No DSF edit, no new objects, **no extra draw calls**.
* Only the `y` column of `VT` lines changes. `x`/`z`/normals/UVs and every non-`VT` line are
  byte-identical; line counts unchanged; no non-finite values.
* **Byte-idempotent** — geometry is always read from `<name>.anchor_bak`, never from the live file, so
  re-running reproduces identical bytes rather than stacking offsets.
* **Reversible** — `--restore` yields files byte-identical to the backups.

### 5.1 Results (KCLT, 8 bakes, current mesh)

Per-component `|rendered base − ground beneath it|`, over 576 ground-touching components (837 elevated
components excluded — they rest on other structures, not on terrain):

| | median | p90 | max | >0.5 m | mean |
|---|---|---|---|---|---|
| before | 3.06 m | 10.11 m | 15.07 m | 531 (92%) | 3.97 m |
| **after** | **0.12 m** | **0.84 m** | 8.08 m | 127 (22%) | **0.31 m** |

### 5.2 The irreducible residual

The 8.08 m maximum, and most of the remaining 22%, belong to **one genuinely connected 513 m terminal
complex** (112,230 m², 6,515 triangles) sitting on ~8 m of mesh slope. It is one rigid mesh. No anchor
scheme can make a rigid body follow a slope.

**This is precisely what Phase 1 fixes** — flatten a building pad beneath it and the residual goes to
zero. See [§7.3](#73-why-both-phases-are-required).

---

## 6. What exists today

### `tools/obj8_geometry.py`
The library. OBJ8 parse (solid vs draped triangles kept apart), `connected_components` (with position
welding), `group_components_into_structures` (grid-accelerated single-link on bounding-box gap),
`area_weighted_centroid`, `local_offset_to_lonlat`, `read_dsf_object_placements`,
`resolve_object_resource`.

`resolve_object_resource` is the missing link: `agp_reader.resolve_library_path`
(`src/auto_patch/agp_reader.py:173`) only consults `library.txt`, so a pack-relative resource such as
`Terminals/Hangar/Charlotte_Airport_007_ALB.obj` resolves to **nothing** today. The pack-relative-wins
probe already exists at `src/auto_patch/dsf_reader.py:199` (`_resource_surface_attribute`) and just
needs lifting into a shared helper.

### `tools/mesh_elevation_sampler.py`
Point-in-triangle barycentric lookup over a built `Data<tile>.mesh`. Format per
`src/O4_Mesh_Utils.py:339`: vertices are `lon lat elevation/100000 tag`; triangle indices are 1-based.

**Use the mesh, not the DEM.** The mesh is the terrain after auto_patch's grading. At the KCLT anchor
the mesh reads 219.83 m where the source DEM reads 218.95 m.

Self-tested: querying at 300 random mesh vertices returns each vertex's own elevation to 0.000000 m,
and 200/200 edge-midpoint queries fall within their endpoints' bracket.

### `tools/dsf_object_anchor_audit.py`
The detector. Per definition: `reach`, `structures`, `components`, `anchored`, `separation`, and with
`--mesh`, `base err`. Warns when definitions share an anchor. Known limitations are documented in its
docstring — most importantly, `base err` assumes `y=0` is the ground plane, which stops holding once a
y-bake has been applied.

### `tools/reanchor_kclt_terminal_bakes.py`
The prototype, **hard-coded** to the eight KCLT bakes. `--dry-run`, `--check`, `--restore`. Writes a
provenance sidecar (`.o4_reanchor_provenance.json`) recording the mesh identity, gap, structure count
and anchor.

**Current live state:** the eight `.obj` files in the Nimbus pack **are baked** against the mesh built
2026-07-08 11:16. `--check` reports `CURRENT`. Originals are `*.anchor_bak`.

---

## 7. Integration plan

### 7.1 Phase 1 — OBJ8 structures as a building-pad source (no mesh required)

Runs at layout time, alongside the existing DSF readers.

`read_dsf_buildings` (`src/auto_patch/dsf_reader.py:720`) already walks `OBJECT` placements
(`_read_dsf_object_placements`, `:670`) but accepts only `.agp` hangars
(`agp_reader.is_agp_building_def`, `:328`), which explicitly rejects `.obj`. So the walker is there and
the `.obj` branch is simply missing.

Add a parallel branch producing, per structure, a **footprint ring** in lon/lat:

1. Group placements by anchor (see [§8.1](#81-anchor-group-discovery)).
2. Resolve each resource; parse OBJ8; discard draped triangles.
3. Project every solid vertex to a local ENU metric frame using **its own** placement.
4. Partition into structures in that shared world frame.
5. For each structure, take vertices with `y ≤ FOOTPRINT_HEIGHT` (a small value — the roof overhang
   must not enter the footprint) and build a ring: convex hull is the safe first cut; an alpha shape
   or the union of projected triangles is more faithful for L-shaped terminals.
6. Emit with an appropriate building/hangar role, honouring the existing **buildings-are-FLAT** ruling.

This is a strict improvement independent of Phase 2: auto_patch currently cannot see ~105 KCLT
buildings, which affects pad grading, clearance, pocket logic and terminal attraction.

Gate behind a new config flag (e.g. `O4_DSF_OBJECT_BUILDINGS`), default **off** until measured on the
gate airports. Compare with `DSF_BUILDINGS` (`config.py:1290`) and `AGP_BUILDINGS` (`:1304`).

### 7.2 Phase 2 — post-build y-bake against the final mesh

Cannot run before the mesh exists. Sequence:

```
layout → auto_patch grading (uses Phase 1 footprints) → Ortho4XP mesh build
       → sample Data<tile>.mesh → per-structure delta → y-bake pack .obj files
       → write provenance
```

Generalise `reanchor_kclt_terminal_bakes.py` into `tools/reanchor_dsf_objects.py` taking a DSF, a
mesh and a pack root. Everything hard-coded there becomes discovered ([§8](#8-correctness-rules)).

### 7.3 Why both phases are required

This is the crux, and it is counter-intuitive in both directions.

**Phase 1 alone changes nothing visually.** Flattening a pad under structure `S` to some elevation
`E_pad(S)` does not move the object: X-Plane still renders it at `terrain(anchor)`. The building now
floats above a flat pad instead of sloped ground. *Phase 2 is mandatory for the visual fix.*

**Phase 2 alone leaves 22% of components >0.5 m off**, because a structure's ground is not flat, and
one rigid mesh gets one elevation.

**Together they are exact.** Phase 1 makes `ground_under(S)` a single value; Phase 2 sets the offset to
exactly that value. Residual → ~0, including for the 513 m terminal complex.

Order matters: Phase 1 feeds the grading that produces the mesh that Phase 2 samples.

### 7.4 Staleness is a first-class concern

The offsets encode one specific built mesh. **This is not hypothetical:** during this very
investigation a parallel session rebuilt the KCLT tile 30 minutes after the first bake, and the
anchor's ground moved 1.19 m — silently invalidating every offset.

Hence the provenance sidecar and `--check`. Any pipeline integration must either re-bake after every
mesh build, or hard-fail when provenance does not match. Precedent: the rebuild-skip provenance stamp
from s79.

### 7.5 The redistribution question — needs a user ruling

Phase 2 modifies files inside a **third-party, possibly paid** scenery pack. Backups, provenance and
`--restore` make it safe and reversible locally, but a corrected pack must never be redistributed.
Decide before shipping:

* Is auto_patch allowed to write outside its own build directory at all?
* Should this be an opt-in post-build step, a separate user-invoked tool, or refused entirely in
  favour of [§4.3](#43-split-the-object-into-one-placement-per-structure--correct-but-heavy)?

---

## 8. Correctness rules the general facility must obey

Each of these was learned the hard way; several silently corrupt a naive implementation.

### 8.1 Anchor-group discovery
Objects sharing an anchor share buildings. Group placements by proximity **and** identical heading.

The prototype exploits a shared local frame, valid only because all eight anchors are bit-identical.
**Do not generalise that way.** The 41 terminal-layer objects share a heading but have two anchors ~10 m
apart. Instead, **project every object's vertices into a common world ENU frame using its own
placement, and partition there.** Frame-independent, handles differing anchors and headings, and the
`y` offset is unaffected by rotation. Map back per-object, per-vertex.

### 8.2 One placement per definition
A `.obj` placed N times cannot be y-baked: the correction differs per placement. **Skip and report any
definition with more than one `OBJECT` placement**, or emit a per-placement copy. All eight KCLT bakes
have exactly one. `otros/cone_short.obj` has 512 — never touch it.

### 8.3 Grouping gap: use ~2 m, verify per-component
Single-link chaining is the trap. Measured on the pooled bakes:

| gap | structures | worst residual | p95 | area-weighted |
|---|---|---|---|---|
| 0.5 m | 116 | 7.93 m | 1.09 m | 0.18 m |
| **2.0 m** | **105** | 7.93 m | 1.13 m | **0.19 m** |
| 5.0 m | 87 | 8.11 m | 1.17 m | 0.21 m |
| 20.0 m | 50 | 7.53 m | 2.34 m | 0.40 m |

At 20 m the geometry collapses into structures sprawling 2.2 km, and one elevation for such a structure
leaves sub-buildings 11 m off.

**Never verify per-structure-centroid.** That check is tautological — it proves only that the offset was
applied. Verify per **component**: `|(terrain(anchor) + min_y(component)) − ground_under(component)|`.
My own first verification pass reported a flawless `0.000000 m` residual while the audit simultaneously
showed 001/002/003 more than 7 m off.

### 8.4 Elevated structures inherit
A structure whose lowest vertex sits above ~0.5 m rests on something else — rooftop clutter, a canopy,
a jetbridge. Re-grounding it on the terrain beneath detaches it from its roof. It must inherit the
offset of the ground-touching structure supporting it. (The KCLT bakes pooled at 2 m happen to contain
none, but 837 *components* are elevated.)

### 8.5 Draped geometry is immune
`ATTR_draped` triangles conform to the terrain mesh. Exclude them from partitioning and never offset
their vertices. Skipping this produced a bogus 10 m finding on `ground_marks/llantas_conjunto.obj`,
which is a flat decal (y range 0.000..0.000) spanning a runway. If a vertex is shared between draped
and solid triangles, leave the object alone and report.

### 8.6 Non-`VT` positional commands must move too
`LIGHT_*`, `VLIGHT`, `SMOKE_*`, `EMITTER`, `MAGNET` carry their own y coordinates. Offsetting `VT` but
not these detaches an apron floodlight from its mast. Neither KCLT family contains any, so the
prototype ignores them — **the general facility must not.**

### 8.7 `ANIM_begin` and `ATTR_LOD`
The 41 terminal-layer objects (the obvious next target) contain **12 `ANIM_begin` blocks and 1
`ATTR_LOD`**; the eight bakes contain neither.

* **`ANIM_begin`:** a per-structure delta applied to geometry inside an animation block can break
  rotation pivots. Give each animation block a **single** delta (from the structure containing its
  geometry), or skip the object.
* **`ATTR_LOD`:** LOD copies of one building overlap in XZ, so they group into the same structure and
  receive the same delta automatically. Self-consistent — but do not let overlapping copies distort the
  area-weighted centroid.

### 8.8 Parsing gotchas
* **`VT`/`IDX` lines are often TAB-separated** (XPlane2Blender). `line.startswith("VT ")` silently
  dropped **232 of 334** definitions on my first pass. Always split on whitespace. `keep_obj8`'s
  `line[0:2] == "VT"` is accidentally safe.
* Light-only objects declare `POINT_COUNTS 0 0 0 0` — 38 defs at KCLT.
* `OBJECT_MSL` / `OBJECT_AGL` shift the heading column and are not terrain-draped.
* Preserve formatting when rewriting: replace only the `y` token, keeping the original whitespace and
  decimal precision, so the diff stays minimal and reviewable.

---

## 9. Test plan

Unit (a synthetic-DSF-text harness already exists at `tests/test_agp_reader.py:189`):

* tab-separated `VT`/`IDX` parse
* `ATTR_draped` triangles excluded from solid geometry
* rotation sign — golden: `007`'s hangar lands at `35.216591, -80.929272`; the wrong sign is 1021 m out
* vertex welding merges duplicated seam positions into one component
* gap chaining: two boxes 3 m apart are one structure at gap 5, two at gap 2
* elevated structure inherits its supporter's delta
* multi-placement definitions are refused

Integration:

* KCLT before/after per-component metric (§5.1) as a regression gate
* y-bake idempotency: two consecutive runs produce byte-identical files — **verified**
* `--restore` yields files byte-identical to the backups — **verified**
* provenance `--check` flips to `STALE` after a mesh rebuild — **verified in the wild**

---

## 10. Open questions

1. **Redistribution ruling** (§7.5). Blocks Phase 2 shipping. Needs the user.
2. **Footprint ring shape.** Convex hull is safe but wrong for L-shaped terminals. Alpha shape? Union
   of projected triangles? Affects pad quality, not correctness.
3. **`FOOTPRINT_HEIGHT`** — what y cutoff separates wall from roof overhang?
4. **Does X-Plane honour `OBJECT_MSL`** for scenery objects (§4.4)? If yes, it is a cheaper fix for the
   single-structure-with-distant-anchor case and needs no `.obj` edit.
5. **The 41 terminal-layer objects** are uncorrected and contain `ANIM_begin` — the first real test of
   §8.6/§8.7.
6. **Does Phase 1 actually drive the Phase 2 residual to zero?** Argued in §7.3, not yet measured.
   Measure it before believing it.
7. **Other packs.** Everything here was derived from one pack. Run
   `tools/dsf_object_anchor_audit.py` over KDFW, CYUL, HECA before assuming the pattern generalises.

---

## 11. Reproduction

```bash
# Audit any pack (57 hits at KCLT with --min-reach 25)
venv/bin/python tools/dsf_object_anchor_audit.py \
  "$XP/Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12/Earth nav data/+30-090/+35-081.dsf" \
  --mesh "$XP/Custom Scenery/zOrtho4XP_+35-081/Data+35-081.mesh"

# Inspect, apply, check, undo the KCLT bake
venv/bin/python tools/reanchor_kclt_terminal_bakes.py --dry-run
venv/bin/python tools/reanchor_kclt_terminal_bakes.py
venv/bin/python tools/reanchor_kclt_terminal_bakes.py --check
venv/bin/python tools/reanchor_kclt_terminal_bakes.py --restore
```

`--check` after any tile rebuild. If it says `STALE`, re-run the bake.

---

## 12. Appendix — key code references (dev @ `6ac66cd`)

| what | where |
|---|---|
| `OBJECT` placement walker (heading column handling) | `src/auto_patch/dsf_reader.py:670`, `:701` |
| building reader that already calls it (agp only) | `src/auto_patch/dsf_reader.py:720`, `:769` |
| `.agp` accept predicate that rejects `.obj` | `src/auto_patch/agp_reader.py:328` |
| pack-relative resource resolution (the pattern to lift) | `src/auto_patch/dsf_reader.py:199` |
| `library.txt`-only resolution (insufficient alone) | `src/auto_patch/agp_reader.py:173` |
| pack root from a DSF path | `src/auto_patch/dsf_reader.py:259` |
| DSFTool text dump, memoized | `src/auto_patch/dsf_reader.py:354` |
| the only existing OBJ8 reader; rotation convention | `src/O4_Vector_Map.py:1175` |
| mesh file format (`elevation/100000`, 1-based tris) | `src/O4_Mesh_Utils.py:339` |
| config flags to sit beside | `src/auto_patch/config.py:1290`, `:1304` |
| synthetic DSF-text test harness to copy | `tests/test_agp_reader.py:189` |

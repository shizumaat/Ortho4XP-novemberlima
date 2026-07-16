# Research: default-landclass terrain mode (custom mesh, X-Plane default textures)

Status: research / feasibility — no implementation yet.
Date: 2026-07-15.

## Question

Can Ortho4XP build a tile DSF that uses our custom triangulated mesh but, instead
of orthophoto textures, textures the mesh with X-Plane's own default landclass
terrain — learning *which* terrain goes *where* by reading the default Global
Scenery DSF for the same tile?

## Verdict

**Feasible, and the model is proven.** This is exactly what alpilotx's HD Mesh
V3/V4 was (custom high-density mesh, textured entirely with default landclass
`.ter` terrain, distributed as freeware referencing the default textures via
library paths), and what Laminar's own MeshTool does (custom mesh plus landclass
assignment, emitting terrain by virtual library path). The DSF format binds one
terrain type per patch, terrain definitions are plain string paths resolved
through the X-Plane library system (`lib/g10/terrain10/....ter`), and no texture
files need to be copied or redistributed.

The work splits cleanly into a **reader** (extract terrain-per-triangle from the
default Global Scenery DSF), a **sampler** (assign a terrain name to each of our
mesh triangles), and a **writer mode** (emit library-path terrain definitions
instead of generated orthophoto `.ter` files). A first version with hard terrain
edges is modest in scope; reproducing X-Plane's soft terrain-to-terrain blending
(border overlay patches) is the one genuinely hard part and can be deferred.

## How the default scenery encodes terrain (verified against developer.x-plane.com)

- A DSF declares its terrain palette as `TERRAIN_DEF <path>` entries, indexed in
  declaration order. Geometry is emitted in *patches*; each patch is bound to
  exactly one terrain index, so "terrain per triangle" means grouping
  same-terrain triangles into patches. Patch flags: bit 1 = physical (collision),
  bit 2 = overlay.
- Every mesh vertex carries at least five values: longitude, latitude, elevation,
  normal-x, normal-z. Terrains whose `.ter` file contains `PROJECTED` derive
  texture placement from world position and need **no** per-vertex texture
  coordinates; non-projected terrains need an explicit s,t pair per vertex.
  Most default landclass base terrains are projected (this is what lets a
  custom-density mesh reuse them) — to be confirmed per-terrain by inspecting
  the stock `.ter` files (`terrain_info.txt` lists the legal set).
- Default terrain paths are **public library virtual paths** — a custom scenery
  DSF references `lib/g10/terrain10/<name>.ter` and X-Plane resolves it from the
  shipped Global Scenery. No textures are copied. (HD Mesh shipped this way.)
- Terrain-to-terrain blending in default scenery is done with **overlay border
  patches**: non-physical patches drawn above the base, carrying an extra
  texture-coordinate pair for a border alpha mask (`BORDER_TEX`), with the
  layering order following `TERRAIN_DEF` file order. Without them, terrain
  changes show as hard edges along triangle boundaries.
- Water: patches bound to `terrain_Water` (or a water `.ter` with `WET`).
  X-Plane 12 three-dimensional water uses a `.ter` with `WATER_COLOR_MASK` and
  four extra per-vertex values (fetch ratio, bathymetry flag, plus a UV pair) —
  Ortho4XP's writer already implements this (`water_tech = "XP12"`).
- Legality constraints on any custom base mesh: the physical layer must cover
  every point of the 1°×1° tile exactly once (no holes, no overlaps); overlay
  border patches must not carry the physical flag. Our mesh already satisfies
  the coverage rule.

Two facts were *not* verifiable from documentation and must be confirmed by
decompiling one stock X-Plane 12 tile: (a) the exact terrain library namespace
X-Plane 12 default DSFs use (`lib/g10/...` vs a newer namespace), and (b) the
concrete X-Plane 12 water `.ter` path and the ordering of its extra vertex
values. Both are answerable in minutes with DSFTool (see next section).

## What we already have in the repo

### Reading DSFs — `src/auto_patch/dsf_reader.py`

- It is a **DSFTool-text** reader: it shells out to the bundled
  `Utils/{mac,win,lin}/DSFTool --dsf2text` (`_load_dsf_text`,
  `dsf_reader.py:354`) and parses the text dump. DSFTool transparently reads the
  7z-compressed default Global Scenery DSFs, so no new decompression code is
  needed.
- Today it parses only overlay content: `POLYGON_DEF`/`OBJECT_DEF` blocks and
  their placements. It ignores `TERRAIN_DEF`, `BEGIN_PATCH`, `PATCH_VERTEX` —
  the base-mesh lines pass through unparsed. Adding a terrain-definition table
  pass and a patch/vertex walker to this existing pipeline is the cheapest path
  to base-mesh extraction; DSFTool's text output already carries the terrain
  index, physical/overlay flag, and per-vertex planes per patch.
- There is also a dormant full binary parser in `src/Unused/C/dsf_io.c`
  (terrain-definition table, run-length and differential pool decoding, command
  walk). It only reconstructs plain triangle patches (command 23) — strips,
  fans, ranges and cross-pool primitives are stubs — and it discards texture
  coordinates. Porting it is *not* the recommended route; the DSFTool-text route
  wins on effort and matches existing architecture.
- The repo already reaches the default Global Scenery DSF on disk today:
  `extract_elevation_and_bathymetry_data` (`src/O4_DSF_Utils.py:361`) opens
  `custom_overlay_src/Earth nav data/<tile>.dsf` to transplant elevation and
  bathymetry rasters, sniffing and un-7z-ing via the bundled 7-Zip binary. The
  X-Plane install is located via the `xplane_dir` preference
  (`src/O4_Settings_Model.py:66`) and `custom_overlay_src`
  (`src/O4_Cfg_Vars.py:110`).

### Writing DSFs — `src/O4_DSF_Utils.py::build_dsf`

- The terrain palette is a plain dictionary `dico_terrains` keyed by an
  arbitrary attribute tuple; the byte table `bTERT` is a running list of
  NUL-terminated path strings. Index 0 is always the name-only `terrain_Water`
  (`O4_DSF_Utils.py:637`) — the existing proof that a library terrain can be
  referenced by name with no generated `.ter` file, no texture, no download.
- Per-triangle texture assignment happens in exactly two places: the triangle
  loops look up the triangle **centroid** in `dico_customzl` (the orthogrid
  cell → imagery mapping from `zone_list_to_ortho_dico`) at
  `O4_DSF_Utils.py:672` (sea pass) and `:905` (land pass), then create/reuse a
  terrain entry and a generated `.ter` (`create_terrain_file`,
  `O4_DSF_Utils.py:263`) with `BASE_TEX_NOWRAP` and per-vertex s,t from
  `GEO.st_coord`.
- Vertex pools are banded by role with fixed plane counts
  (`dsf_pool_plane`, `O4_DSF_Utils.py:600`): 7 planes for textured land
  (position, normal, s, t), 9 for masked/overlay water, 7 for `terrain_Water`
  (position, dummy normal, fetch, bathymetry). Patch flags (physical versus
  overlay, level-of-detail) are emitted at `O4_DSF_Utils.py:1218` from the
  `overlay_terrains` set.
- The `.mesh` file's per-triangle attribute is a bitmask (WATER=1, SEA=2,
  SEA_EQUIV=4, INTERP_ALT=8 — `O4_Vector_Utils.py:44`); there is no landclass
  field on triangles today, and none is needed — the assignment can stay
  centroid-lookup-at-write-time exactly as the ortho path does it.

## Proposed architecture

### Phase 0 — probe (hours)

Decompile one stock X-Plane 12 tile with the bundled DSFTool and confirm:
the terrain library namespace its `TERRAIN_DEF` lines use; which of those
`.ter` files are `PROJECTED` (read them out of the Global Scenery pack, plus
`terrain_info.txt`); the water terrain path and its per-vertex plane count.
This resolves every open format question before code is written.

### Phase 1 — reader: terrain map from the default DSF

Extend `src/auto_patch/dsf_reader.py` (or a sibling module, e.g.
`src/O4_Default_Terrain_Map.py`, since this is core-pipeline not auto_patch)
with:

1. A `TERRAIN_DEF` table pass over the existing DSFTool text dump (the dump is
   already cached keyed on path+mtime).
2. A `BEGIN_PATCH` / `BEGIN_PRIMITIVE` / `PATCH_VERTEX` walker that keeps only
   **physical** patches (flag bit 1) and expands triangles, strips and fans
   into triangles tagged with their patch's terrain index. Overlay border
   patches are skipped for now (they matter only for Phase 3 blending).
3. A query structure: triangles indexed spatially (shapely STRtree, already a
   dependency), answering `terrain_path_at(lon, lat)` by point-in-triangle.
   Default-scenery triangles are large, so a few hundred thousand triangles per
   tile is the expected scale; the text dump parse should stream line-by-line
   (dumps of full base meshes run to hundreds of megabytes).

The default mesh covers the tile exactly once by format law, so every centroid
query has exactly one answer; fall back to nearest-triangle for edge-of-tile
floating-point misses.

### Phase 2 — writer mode: library terrains instead of orthophotos

A tile-level mode (config variable, e.g. `default_landclass_terrain=True`, in
the style of `skip_downloads`) that changes `build_dsf`'s two triangle loops:

1. For land triangles, replace the `dico_customzl` centroid lookup with
   `terrain_path_at(centroid)` against the Phase-1 map; key `dico_terrains` by
   the terrain path string; append the library path to `bTERT` directly —
   no `create_terrain_file`, no `download_queue.put`, no mask logic.
2. Add a projected-terrain pool band: 5 planes (position + normal, no s,t) for
   triangles bound to `PROJECTED` terrains, alongside the existing 7-plane
   band. Non-projected default terrains (if the probe finds any in practice)
   either get s,t computed per the `.ter` projection parameters or, simplest
   v1, are substituted by the nearest projected terrain.
3. Keep the physical flag (do not add these terrains to `overlay_terrains`).
4. Water is unchanged: the existing `tri_types` water/sea classification and
   `water_tech` machinery (`terrain_Water`, X-Plane 12 `WATER_COLOR_MASK`)
   already emit correct default-style water — arguably *better* than the
   default DSF's own water assignment, since our vector-map coastlines are
   fresher. This is a genuine advantage of keeping our mesh pipeline.
5. Skip the whole imagery stage for such tiles (no downloads, no DDS
   conversion, no masks) — build time drops to mesh + DSF write.

Because the assignment is centroid-per-triangle through the same lookup shape
as `dico_customzl`, a later refinement can make this **per-zone rather than
per-tile**: reuse `zone_list` so a tile can be orthophoto near selected areas
and default landclass elsewhere (mixed tiles), with the landclass entries
simply mapping to terrain paths instead of imagery attribute tuples.

### Phase 3 — border blending (the hard part, deferrable)

Version 1 ships with hard terrain transitions at triangle edges. This is valid
scenery and is what a naive assignment yields; visually it reads as a landclass
patchwork at transition lines. To approach the default look:

- Emit non-physical **overlay border patches** along terrain-boundary edges of
  our mesh: for each boundary triangle, an overlay patch bound to the
  neighboring terrain with the extra texture-coordinate pair driving its
  `BORDER_TEX` alpha mask (0 at the far edge, 1 at the shared edge), layered by
  `TERRAIN_DEF` order per the format rules.
- The default DSF's own border patches cannot be reused — they are shaped to
  the default mesh's triangles, not ours. HD Mesh's border algorithm was never
  published; this must be original work.
- Scope guard: hard edges first, evaluate in the simulator, and only then decide
  how much border machinery the visual result actually demands. Sampling by
  centroid against the default mesh already reproduces the default scenery's
  *placement* of transitions; only their softness is at stake.

## Risks and open items

- **X-Plane 12 namespace and water specifics** — unresolved until the Phase-0
  probe; everything else is insensitive to the answer.
- **Projected versus explicit texture coordinates** — the plan assumes most
  default terrains are projected; the probe enumerates the exceptions.
- **Text-dump size/time** — DSFTool decompile of a base mesh is slow (tens of
  seconds) and large; cache per tile (the reader already caches) and parse
  streaming. If it ever becomes the bottleneck, the binary route
  (`src/Unused/C/dsf_io.c` as reference) is the escape hatch, not the start.
- **Tile seams** — neighboring default-landclass tiles sample the same default
  DSFs, so terrain assignment agrees across seams by construction; elevation
  seams are already handled by the existing mesh pipeline.
- **X-Plane 11 versus 12** — pick the namespace from the scenery actually
  installed (the reader reads whatever `custom_overlay_src` points at), and
  record it per-tile.
- **Future-proofing** — Laminar has signaled a long-term move of landclass to
  raster form ("We Are All Raster-farians Now", 2024 developer blog). The
  `.ter`-per-patch mechanism is what X-Plane 12 ships and loads today, but the
  feature should be framed as targeting the current DSF contract.

## Test plan (headless, per repo policy)

- Reader: synthetic DSFTool-text fixtures (small hand-written dumps with
  `TERRAIN_DEF` + triangle/strip/fan patches, physical and overlay) →
  assert exact triangle/terrain extraction; no X-Plane install needed.
- Sampler: known triangle map, assert `terrain_path_at` on interior, edge and
  fallback points.
- Writer: build a toy tile with the mode on, decode the emitted DSF's terrain
  table and patch flags (DSFTool round-trip in a tool under `tools/`), assert
  library paths present, no `.ter` files generated, no imagery queued, pool
  plane counts correct.

## Sources

- developer.x-plane.com: DSF File Format Specification; DSF Usage In X-Plane;
  Understanding and Building DSF Base Meshes; Terrain Type (.ter) File Format
  Specification; Library (library.txt) File Format Specification; MeshTool
  Manual; "MeshTool, Water and Land Class" (Supnik, 2009); "We Are All
  Raster-farians Now" (Supnik, 2024).
- xptools repository: DSFTool README.dsf2text (text grammar, 7z handling).
- alpilotx.net: HD Mesh Scenery v4 release notes (prior art).
- Open-source DSF mesh readers for reference: xptools DSFTool, muxp
  (nofaceinbook), Marginal's water/land extraction gist.

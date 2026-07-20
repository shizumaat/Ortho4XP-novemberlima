# MSFS converter — first real-package run (ETNL + LMML), vs. synthetic findings

Status: empirical results, 2026-07-20 (cloud session). Companion to
`docs/msfs_converter_diagnosis.md` (synthetic-package diagnosis) and
`docs/msfs_converter_handoff.md` (task list). This session had the two
candidate GitHub packages named in the diagnosis doc cloned in-repo:
`msfs-etnl` (Rostock-Laage) and `LMML-MSFS` (Malta, Viking Studios).

## TL;DR

- **msfs-etnl is not a usable test package**: it is airport-*ground-layout*
  sources only (runway/taxiway XML + optional aerial-imagery CGL). No
  compiled BGLs, no glTF models, zero `SceneryObject`/`LibraryObject`
  entries even in source ("custom objects" is an unchecked roadmap item
  upstream; the compiled release zip exists only on the upstream repo's
  Releases page, not in the fork in session scope).
- **LMML-MSFS is the real-package test the diagnosis asked for**: its
  `Packages/` tree ships a genuine fspackagetool-compiled
  `modelLibrary.BGL` + `LMML.bgl`. The production pipeline ran end-to-end
  on it and the **package reader is validated field-exact against the
  package's own `PackageSources/` ground truth**.
- The synthetic conclusions hold on real data: the decode/placement core
  is correct; **library reliance is even more extreme than KRDM** (96.8%
  of placements external vs. KRDM's 89%).
- Two **new real-package findings** the synthetic packages could not
  surface: (1) MSFT_texture_dds URI resolution has no fallback, so real
  compiled packages get placeholder textures; (2) shipped `Packages/`
  can be internally inconsistent with their own sources (stale compile),
  which produces grotesque converted geometry that is *not* a converter
  bug — worth a bbox sanity warning.

## 1. msfs-etnl: documented behavior

`read_package` → `no .bgl files found under /home/user/msfs-etnl` (warning),
`convert_msfs_airport` → `ValueError: no model libraries found in the MSFS
package`. Correct and graceful for a source-only project. The repo contains
only `scene/airport/airport-etnl.xml` (FSData ground layout: 29 taxiway
points, runway 09/27, no placements) and shapefiles for the imagery CGL.
The in-scope fork has no releases and no `no_aerial_image` branch, so no
compiled BGL is reachable in this session.

## 2. LMML: full-pipeline run and reader validation

Package: `LMML-MSFS/Packages/vikingstudios-airport-lmml-malta` (compiled,
99 MB). Sources for ground truth:
`PackageSources/Scenery/.../modelLibrary/` (3 light-pole models:
Apron9Tall, LightPoleApron9Mid, Apron9SmallLightPole) and
`PackageSources/.../scenery/LMML.xml` (220 SceneryObjects, 218 with a
LibraryObject child).

Reader validation, parsed BGL vs. source XML/glTF:

| Field | Result |
|---|---|
| Models carved from modelLibrary.BGL | **3/3** (GUIDs match the three `<ModelInfo>` XMLs exactly) |
| Placements parsed from LMML.bgl | **218/218**; per-GUID counts identical (12 distinct GUIDs) |
| Position match (lat/lon, per placement) | 217/218 matched at 1e-5° rounding; 0 heading mismatches, 0 pitch mismatches |
| Scale | Exact: 214×1.0 + 4×1.3, matching the source `scale=` attributes |
| GUID/scale tail heuristic (handoff task 5 risk) | All 218 records are plain 64-byte LibraryObjects → heuristic correct on every record, 0 garbage GUIDs |
| Altitude/AGL | All placements alt=0 AGL in source and parsed |
| Non-LibraryObject records | 2 records of type 0x18 (the 2 SceneryObjects without a LibraryObject child) — ignored, consistent |

Conversion (`convert_msfs_airport`, bundled Linux DSFTool):
**2 models converted** (the 2 custom GUIDs that are actually placed; the
third, Apron9Small, is never placed and is skipped by design),
**3 OBJ8s written** (Mid splits into a textured + palette material pair),
**10 OBJECT rows** in the overlay DSF (7 placements × their objects),
**211 placements skipped** as external-library, **1 exclusion rectangle**
(covers exactly the Apron 9 pole cluster, ~420 m × 380 m).

Geometry validation: the Mid pole's converted OBJ8 world bbox
(x −4.14..4.19, y 0.04..16.55, z −3.57..3.47) matches an independent
scene-graph walk of the source glTF **to the centimetre** — the
byte-exact-decode conclusion from the synthetic run holds on real
compiled data.

## 3. Comparison against the synthetic-package findings

| Synthetic finding (diagnosis doc) | Real-package (LMML) outcome |
|---|---|
| §1 decode core byte-exact | **Confirmed on real data** (Mid pole exact to source; UVs/normals render correctly in previews) |
| §2 library reliance dominant (KRDM 89%) | **Confirmed, stronger: 96.8%** (211/218 external). Converted LMML is 7 light poles on an empty airport — the "empty-feeling airport" is exactly reproduced by a real freeware package |
| Bug 1: per-node winding (mirrored nodes) | **Not exercised** — no negative-scale nodes in any LMML model |
| Bug 2: placement scale dropped | Parser reads scale correctly (4×1.3 present) but all 1.3s are external → skipped; the 7 written placements are scale 1.0, so the DSF-writer drop has **no visible effect here** (bug still real, still task 4) |
| Bug 3: altitude/AGL dropped | **Not exercised** — every LMML placement is alt=0 AGL |
| Bug 4: GUID/scale tail misread on extended records | **No extended records in LMML** — heuristic correct 218/218. Synthetic repro remains the only evidence; fix priority unchanged |
| Exclusion from placement points (task 2) | Reproduced in kind: one rectangle around the pole cluster. For LMML this is actually *appropriate* (only poles are custom); the KRDM-style problem (default terminal not excluded) simply cannot arise when 97% of content is skipped |

## 4. New findings only a real package could surface

1. **MSFT_texture_dds URI resolution has no fallback (new task).** The
   compiled GLBs reference textures via `MSFT_texture_dds` with names
   like `METAL_0081_COLOR_4K.JPG.DDS`. `gltf_reader._resolve_images`
   does a single exact-path read — no case-insensitive match, no
   "strip `.DDS` and try the underlying `.jpg`/`.png`" fallback. With
   the source textures staged, `metal_0081_roughness_4k.png` (plain URI)
   resolves, but the color/normal (`*.JPG.DDS`/`*.PNG.DDS` URIs) still
   fail → placeholder albedo + gray normal. Note also: this package
   ships **no texture directory at all** in `Packages/` (textures exist
   only as loose JPG/PNG in `PackageSources/`), and the DDS *decode*
   path (Pillow) is still unexercised end-to-end.
2. **Shipped compiled packages can disagree with their own sources
   (stale compile), producing wild geometry that is NOT a converter
   bug.** Apron9Tall's compiled GLB keeps the source node transforms
   (mast node scale (0.3, 18, 0.3), translation +18 m) but contains
   re-authored mesh vertices (mast mesh ±2.903 m instead of the unit
   cylinder the node scale was built for). A faithful scene-graph walk —
   ours, and any other consumer's — yields a 104 m tall, 3 cm wide mast
   spanning 34 m underground, while the light-fixture arms of the *same
   model* land at the correct ~36.5 m. The independent walk of the
   compiled GLB reproduces the converter's output exactly, so the
   converter is faithful to the file; the file itself is inconsistent
   (meshes edited in Blender after the shipped compile — `Packages/`
   and `PackageSources/` were committed together but encode different
   model revisions). Suggested cheap task: warn when a converted
   object's bbox is implausible (e.g. > 80 m tall or extends far below
   y=0) so users can tell "bad source data" from "converter bug".
3. **Runtime deps**: `material_fidelity.bake_palette_image` imports PIL
   at call time — a fresh environment needs `pillow` installed or every
   real conversion dies mid-model (synthetic tests that skip palette
   baking won't catch it).

## 5. Artifacts and repro

Previews (scratchpad, not committed): `lmml_pack_preview.html` /
`lmml_pack_textured_preview.html` (obj8_to_html `--pack` world scene:
7 poles in situ on the Apron 9 line) and `lmml_objects_preview.html`
(per-object scene, 106 949 vertices / 112 152 triangles).

```
# pipeline (repo root; needs pillow)
python3 - <<'PY'
import sys; sys.path.insert(0, 'src')
import O4_MSFS_Airport_Convert as CONV
CONV.convert_msfs_airport(
    '<...>/LMML-MSFS/Packages/vikingstudios-airport-lmml-malta',
    '<custom_scenery>', '<global_airports>', 'Utils/lin/DSFTool')
PY
python3 tools/obj8_preview/obj8_to_html.py --pack \
    '<custom_scenery>/MSFS Convert - LMML' \
    --dsftool Utils/lin/DSFTool -o lmml_pack_preview.html
```

Build-time impact: none — diagnostic run only, nothing in the tile build
pipeline changed.

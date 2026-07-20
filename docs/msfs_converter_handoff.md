# MSFS → X-Plane airport converter — cloud-session handoff

State of the work as of 2026-07-19 (branch `dev`). This document exists so
a claude.ai cloud session (driven from any device) can continue the work
with full context. Read this before touching the converter.

## What this is

"Convert MSFS airport" (Qt Tools menu, `src/O4_Qt_MSFS_Convert.py`)
converts an MSFS airport scenery package into an X-Plane Custom Scenery
pack. Pipeline (orchestrated by `src/O4_MSFS_Airport_Convert.py`):

1. `src/O4_MSFS_Package.py` — reads compiled BGLs: carves embedded GLB
   models (with GUIDs) from model-library section 0x2b, parses placement
   records from SceneryObject section 0x25.
2. `tools/msfs_to_obj8/` — glTF → OBJ8: `gltf_reader.py` (parser, incl.
   ASOBO quantization quirks), `convert.py` (axis map, winding,
   per-texture grouping), `atlas_pack.py` (per-model texture atlasing),
   `material_fidelity.py` (factor palettes, gloss, glass, TEXTURE_LIT).
3. `src/O4_MSFS_XPlane_Pack.py` — Global Airports apt.dat extraction,
   overlay DSF with placements + exclusion zones (DSFTool).

Preview without X-Plane: `tools/obj8_preview/obj8_to_html.py` renders
single objects, multi-object scenes, or whole packs (`--pack DIR`) with
DSF placements applied, as a self-contained three.js HTML.

Test suites (all headless): `tests/test_msfs_package.py`,
`test_msfs_to_obj8.py`, `test_msfs_xplane_pack.py`,
`test_msfs_airport_convert.py`, `test_obj8_preview.py`,
`test_obj8_building_gen.py`. Run serially with `-n0`. NOTE: integration
tests referencing `scratchpad/KRDM_Redmond` (the BullfrogSim test
package) are `skipif`-guarded — that third-party package is NOT in the
repo (license) and lives only on the local machine, so cloud sessions
exercise the synthetic-fixture tests only.

## Empirical facts, validated 2026-07-19 by a five-agent research pass

All confirmed against primary sources (Khronos spec, FSDeveloper wiki,
fs-parse source, FS2XPlane source, MSFS SDK docs, X-Plane developer
docs) unless marked otherwise:

- X-Plane OBJ8 is CLOCKWISE-front; +X east, +Y up, +Z south, meters.
- Axis map: `(x,y,z)_gltf → (−x, y, −z)` — a 180° rotation about Y;
  placement headings pass through. Verified by fitting converted
  terminal walls to the airport's OSM footprint at 2.8 m mean error.
- Placement records (section 0x25, record 0x0b LibraryObject):
  lon = raw·240/2²⁹ − 180 (this IS the classic FSX formula, = 360/(3·2²⁸)),
  lat = 90 − raw·180/2²⁹, PBH = raw·360/2¹⁶, altitude s32 in
  MILLIMETERS, flags bit0 = IsAboveAGL, GUID at fixed offset 0x2C,
  scale float at 0x3C.
- BGL model library (section 0x2b): flat index GUID(16, MS mixed-endian)
  + offset + size → RIFF container with chunks `GXML` (XML descriptor),
  `GLBD` (collection of GLBs, ONE PER LOD), `GLB` (binary glTF). Our
  code's docstrings say form "GLTF" — that FourCC is a misnomer; carving
  by `glTF` magic works regardless.
- ASOBO quantization in BGL-embedded GLBs: TEXCOORD accessors are
  declared componentType 5122 (SHORT, non-normalized) but store FLOAT16
  bit patterns (fixed in `gltf_reader._decode_accessor`); NORMAL/TANGENT
  are raw int8 (renormalized on read); POSITION stays float32. UV
  origin: standard glTF top-left; `v' = 1 − v` confirmed correct.
- Winding: glTF front-face is CCW relative to sign(det(node world
  transform)). MSFS optimizer output measures CW in raw index order.
  Current code uses whole-file majority auto-detection
  (`detect_source_winding`) — see task 1 below for the correct per-node
  rule.
- MSFS LODs are separate `_LODnn` glTF files selected by `minSize`
  (percent of screen height) in model.xml — NOT MSFT_lod.
- DSF placements support `OBJECT`, `OBJECT_MSL`, `OBJECT_AGL` — no
  scale. X-Plane 12 PBR: `TEXTURE_MAP normal|material_gloss|gloss`,
  `NORMAL_METALNESS` (blue channel = F0), `GLOBAL_luminance` for LIT
  calibration; `ATTR_shiny_rat` is the legacy scalar path we currently
  emit.

## Prioritized task list (each with the evidence behind it)

Tasks 1–5 were implemented and landed 2026-07-20 (cloud session, branch
`claude/msfs-xplane-scenery-research-bg9mdm`), each with headless tests;
all validated end-to-end against the synthetic packages
(`tools/make_synth_msfs_packages.py`) and regression-checked against the
real LMML compiled package (see
`docs/msfs_converter_real_package_run.md`). Notes per task below.

1. **[DONE] Per-node winding correction.** `gltf_reader` now marks every
   primitive instance with ``mirrored`` (sign of det(node world
   transform)); reversal is per-primitive (base convention XOR mirror)
   and `detect_source_winding` inverts votes from mirrored primitives.
   Synthetic mirrored-wing model: 48/48 triangles correct (was 24/48).
2. **[DONE] Exclusion zones from model extents.** `convert()` manifests
   now carry per-object OBJ8 bounds; `PlacedObject.bounds_obj8` feeds
   `compute_exclusion_rectangles`, which rotates the XZ footprint by the
   placement heading and pads that (padded-point fallback when bounds
   are unknown).
3. **[DONE] `OBJECT_AGL`/`OBJECT_MSL`.** DSFTool text row order verified
   by round-trip with the bundled binary: elevation comes BEFORE
   rotation (`OBJECT_AGL <def> <lon> <lat> <elev m> <rot deg>`); the DSF
   encoding quantizes elevation to ~1 m pool steps. AGL-0 placements
   keep the plain `OBJECT` form.
4. **[DONE] Bake placement scale.** Per-(guid, scale rounded to 2 dp)
   OBJ variants via `convert.write_scaled_obj8` (uniform VT-position
   scaling); files named `<base>_s<scale>.obj` (e.g. `_s1_50`).
   Non-positive scales fall back to 1.0 with a warning.
5. **[DONE] Fixed offsets 0x2C/0x3C + record census.** GUID/scale read
   at fixed offsets (AttachedObject-extended records now parse; the
   synthetic extended record survives the full pipeline). All 0x25
   record types are counted (`read_object_placements_with_stats`) and
   `read_package` warns per BGL with per-type counts. New empirical
   fact: MSFS compiles `<Windsock>` to record type **0x18** (LMML: 2
   source windsocks = exactly its 2 type-0x18 records); FSX-era 0x0C
   kept in the name table too.
6. **Glass treatment for dark glass textures.** Hold-room/window texture
   groups (SKY*, WINDOW*) render matte near-black in X-Plane; split them
   into BLEND_GLASS objects with high gloss so XP12 reflections read
   like MSFS.
7. **XP12 PBR upgrade.** Emit `TEXTURE_MAP material_gloss` (per-pixel
   gloss = 1 − roughness) instead of scalar ATTR_shiny_rat when a
   roughness source exists; carry metallicFactor via NORMAL_METALNESS
   blue channel; consider GLOBAL_luminance for LIT objects.
8. **LOD translation.** Parse GLBD's multiple GLBs (one per LOD) and the
   GXML LOD metadata; emit X-Plane `ATTR_LOD` bands (convert minSize
   screen-% to meters using bounding-sphere radius).
9. **Stock-library GUID mapping** (the big fidelity win: 798/893
   placements in the test package are stock objects — jetways, fences,
   vehicles, lights). Adopt FS2XPlane's proven two-table architecture:
   data file GUID→name (generate with ModelConverterX's object report
   over fs-base modelLib.bgl; no published table exists), plus curated
   name→X-Plane-object table with per-mapping position/heading bias.
   Prefer NATIVE apt.dat entities for jetways/windsocks/beacons/lights;
   route the rest to `lib/airport/…`, OpenSceneryX, Handy Objects; omit
   unmatched. Seed from FS2XPlane's Resources/substitutions.txt.
10. **Fix the "GLTF" FourCC misnomer** in O4_MSFS_Package docstrings
    (actual chunks: GXML/GLBD/GLB).

## Working agreements

- Converted third-party scenery is PERSONAL USE unless the author grants
  redistribution; never commit third-party packages or textures.
- Tests must stay headless; run via
  `venv/bin/python tools/run_with_ledger.py -- venv/bin/python -m pytest <files> -n0`
  locally (plain pytest is fine in cloud environments without the venv).
- Parallel sessions share the git index on the local machine: commit by
  explicit paths (`git commit -- <paths>`), never reset.
- In-sim verification happens on the local machine only (X-Plane 12 at
  `/Users/noah/X-Plane 12`); cloud sessions should mark changes that
  need a sim check in their PR/handoff notes.

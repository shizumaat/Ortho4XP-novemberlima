# MSFS → X-Plane airport converter — cloud-session handoff

State of the work as of 2026-07-20 (branch `dev`). This document exists so
a claude.ai cloud session (driven from any device) can continue the work
with full context. Read this before touching the converter.

## 2026-07-20 cloud session — tasks 1, 2, 3, 5, 10 DONE

All landed with headless tests (73 green in the MSFS/obj8 suites):

1. ~~Per-node winding~~ DONE: `gltf_reader` records per-primitive
   `mirrored` (sign of det of the node world transform); detection votes
   are sign-corrected so mirrored instances can't outvote the authored
   convention; per-primitive reversal = file decision XOR mirror flag.
2. ~~Exclusion zones from model extents~~ DONE: converter manifests carry
   `bounds_xz` (horizontal footprint, OBJ8 meters); `PlacedObject` takes
   `bounds_xz`; `compute_exclusion_rectangles` covers the heading-rotated
   footprint + padding (point+padding fallback preserved byte-identically
   when bounds are absent).
3. ~~Placement altitude~~ DONE: `PlacedObject` carries
   `altitude_meters`/`is_above_ground`; the DSF writer emits `OBJECT_AGL`
   (flag set) / `OBJECT_MSL` (clear) for non-zero altitudes; altitude 0
   stays a ground-draped `OBJECT` row regardless of the flag (the safe
   reading of the ambiguous flag-false zero). The obj8 previewer parses
   the new rows (AGL = y offset; MSL previews at ground).
5. ~~Fixed offsets~~ DONE: GUID/scale read at 0x2C/0x3C (end-relative
   reads removed); unconverted 0x25 record types (GenericBuilding,
   Windsock, Effect, TaxiwaySign, ExtrusionBridge, unknown) are counted
   per BGL and reported as warnings.
10. ~~FourCC docstring~~ DONE (RIFF form `GLTF` misnomer named; GLBD =
    one GLB per LOD).

**Needs a sim / local check** (this environment has neither X-Plane nor
the KRDM package): (a) DSFTool round-trip of `OBJECT_AGL`/`OBJECT_MSL`
rows — the round-trip test was extended and runs only on the local
macOS-arm64 machine; (b) KRDM reconversion: mirrored-node objects no
longer inside-out, exclusion boxes now cover the full terminal complex
(watch for over-exclusion), the ~16 m tower now rides at its authored
altitude; (c) the skipped-record-type warning counts on the real package
(expect windsocks/taxiway signs).

Remaining open tasks: 4 (bake placement scale), 6 (glass), 7 (XP12 PBR),
8 (LOD translation), 9 (stock-library GUID mapping).

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

1. **Per-node winding correction.** Replace whole-file
   `detect_source_winding` majority vote with per-primitive reversal
   decided by sign(det(node world transform)) × source convention.
   Mirrored (negative-scale) nodes currently render inside-out.
2. **Exclusion zones from model extents, not placement points.**
   `O4_MSFS_Airport_Convert` pads placement POINTS by 20 m; a 630 m
   terminal complex keeps only a 40 m exclusion box, so default gateway
   3-D (e.g. the stock KRDM tower) still draws. Compute each placed
   object's world bbox (object bounds, rotated by heading, at the
   placement) and merge those rectangles.
3. **Honor placement altitude via `OBJECT_AGL`/`OBJECT_MSL`.** Altitude
   (mm→m) and the AGL flag are parsed but dropped; DSF supports both
   forms. Small change in `O4_MSFS_XPlane_Pack.write_overlay_dsf` +
   orchestrator plumbing.
4. **Bake placement scale.** DSF has no scale; emit per-(guid, rounded
   scale) OBJ variants with pre-multiplied vertices. Test package has
   scales 0.6–1.8, all currently rendered at 1.0.
5. **Read GUID/scale at fixed offsets 0x2C/0x3C** in LibraryObject
   records (the current size−20/size−4 heuristic breaks when
   AttachedObject (0x1002) sub-records extend the record). Also dispatch
   other 0x25 record types by (type, size) — 0x0A GenericBuilding,
   0x0C Windsock, 0x0D Effect, 0x0E TaxiwaySign, 0x12 ExtrusionBridge —
   at minimum warn with counts instead of silently skipping.
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

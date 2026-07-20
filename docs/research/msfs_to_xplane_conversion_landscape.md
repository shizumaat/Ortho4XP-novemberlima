# MSFS → X-Plane scenery conversion — tooling landscape & community practice

Status: research report, 2026-07-20. No code changed. Author: research agent
(deep-research pass, 100 sub-agents, 18 sources fetched, 25 claims
adversarially verified — 23 confirmed / 2 refuted).
Scope: what tools, formats, workflows, and community practice exist for
converting Microsoft Flight Simulator (MSFS 2020/2024) scenery packages into
X-Plane 11/12 scenery — and what that means for this repo's in-progress
`O4_MSFS_*` airport converter (branch `dev`).

Companion documents: `docs/msfs_converter_handoff.md` (the prototype's own
byte-level format notes and task list — the *inside* view), and this file (the
*outside* view: the third-party ecosystem the prototype sits inside).

---

## 0. Bottom line up front

**There is no mature, one-click pipeline that converts a modern MSFS 2020/2024
scenery package into a finished X-Plane 11/12 scenery pack.** The ecosystem
splits into three non-overlapping tools, none of which closes the loop:

| Tool | Direction | Reaches finished X-Plane pack? |
|------|-----------|-------------------------------|
| **FS2XPlane** | legacy FS2004/FSX BGL → X-Plane DSF overlay | Yes, but only for *legacy* (pre-2020) input |
| **ModelConverterX (MCX)** | MSFS glTF/BGL ⇄ modelling formats / MSFS package | No — MSFS-in / MSFS-out bridge |
| **msfs2blend** | MSFS 2020 glTF → Blender | No — "quick and dirty" livery-painting importer |

The practical consequence: for MSFS-quality X-Plane scenery the community path
is **manual rebuild** (airport layout re-authored in WED; 3-D objects extracted
as *assets* via MCX/Blender then re-exported through Blender2XPlane) **plus
orthophoto/mesh generation** — the Ortho4XP domain — for the ground. Direct
whole-package conversion of modern MSFS content does not exist as a shipping
product.

This is exactly the gap the repo's `O4_MSFS_*` prototype targets: a
**modern-MSFS-BGL → finished-X-Plane-pack** path (glTF/GLB carving → OBJ8 +
overlay DSF + apt.dat). The research below both validates several of the
prototype's low-level decisions and maps the ceiling it will hit.

---

## 1. Tools inventory & maintenance status

### 1.1 FS2XPlane (Jonathan Harris / "Marginal")

- **What it is.** The tool most often named when people say "MSFS to X-Plane
  converter." Its own documentation states verbatim that it converts *"MS
  Flight Simulator 2004 and FSX add-on scenery packages to X-Plane DSF overlay
  scenery packages."* ⚠️ **Terminology hazard:** here "MSFS" means the *legacy*
  Microsoft Flight Simulator (FS2004 / FSX), **not** MSFS 2020/2024. FSX (2006)
  and FS2004 (2003) both predate the modern sim and its entirely different
  package format. [primary: github.com/Marginal/FS2XPlane; .../Resources/FS2XPlane.html]
- **Output.** X-Plane DSF **overlay** packages — objects/autogen laid *over* an
  existing base mesh, not a base mesh of their own. The repo's `convbgl.py`
  confirms legacy BGL as the input format.
- **What it converts vs. drops** (this is the canonical statement of the hard
  limits of *any* BGL→DSF conversion):
  - Converts: FS2004/FSX runways & taxiways, procedural scenery (e.g.
    buildings), and library objects from BGL / Rwy12 / EZ-Scenery, with
    textures re-exported to DDS (v9) or PNG.
  - Drops: FSX photoscenery, jetways, and fences are discarded; **most stock
    objects are ignored**; **animated (incl. billboarded) objects are converted
    as static**.
  - Notorious artefact: ground-level textures supplied as objects on flattened
    terrain **"float above the ground in X-Plane"** — a mesh-system mismatch
    that generates whole YouTube tutorials on fixing floating objects.
    [primary: .../Resources/FS2XPlane.html]
- **Platform / cost.** Cross-platform Python app (Python 69.7% / Assembly
  26.7%), Windows/Linux/macOS build configs; freeware (GPLv2, some source under
  CC-BY-NC-SA / CC-BY-SA).
- **Maintenance.** **Dormant.** Created 2012-01-24, last pushed ~2016-03-20
  (~9 years cold), 218 commits, 48 releases. It will never gain MSFS 2020/2024
  support. [primary: github.com/Marginal/FS2XPlane]

### 1.2 ModelConverterX (MCX) — Arno Gerretsen / SceneryDesign.org

- **What it is.** A Windows (.NET) flight-sim tool whose core function is
  **converting 3-D scenery objects between simulator formats**; also an object
  viewer/editor. Typical uses: SketchUp/COLLADA → FS-readable MDL, or MDL back
  into a modelling-tool format. Format list includes MDL, COLLADA, and X-Plane
  OBJ. [primary: scenerydesign.org/modelconverterx/]
- **MSFS support.** MCX **can read glTF files and the MSFS BGL files that
  contain object models**, and can export either a *plain glTF* (no MSFS
  extensions, for modelling tools) or a full *MSFS scenery package*
  (PackageSources folder with XML + glTF describing objects and placement),
  targeting MSFS 2020 or 2024. **Direction is primarily MSFS-in / MSFS-out** —
  it is not a finished-X-Plane exporter.
  [primary: scenerydesign.org/2020/09/gltf-support-for-modelconverterx/]
- **Hard limitation — behaviour does not round-trip.** glTF export handles
  **geometry, materials, LODs, and lights only**; *animations, mouse
  rectangles, and visibility conditions are not exported.* A January 2026
  SceneryDesign post confirms MCX still "cannot yet write skin and bone
  animations to glTF files, so on export they are still ignored" — the
  limitation has persisted ~6 years. [primary: .../gltf-support-for-modelconverterx/]
- **Platform / cost.** Windows, freeware; hosted on FSDeveloper, actively
  developed via frequent dev releases.

> ⚠️ Two plausible-sounding claims were **refuted** during verification and are
> excluded from the findings above:
> (a) that MCX exports X-Plane OBJ *read-only* — its format list actually shows
> OBJ **read/write** (0–3 refuted); and
> (b) that MSFS 2024 BGL support is dev-release-only (1–2 refuted). Do not
> repeat either as fact.

### 1.3 msfs2blend (bestdani)

- **What it is.** A Blender 3.0+ importer for MSFS 2020 glTF/GLB models —
  self-described as a **"quick and dirty importer … intended to be used for
  painting liveries."** It imports *"most meshes with a UV map and nothing
  more"*, is *"not intended to fully reconstruct the original model files"*,
  does **not** support bone-animated rotations, and shells out to Microsoft's
  **`texconv.exe`** to convert MSFS DDS textures to PNG.
  [primary: github.com/bestdani/msfs2blend]
- **Platform / cost.** Python 100%, Apache-2.0.
- **Maintenance.** Appears unmaintained — last release v0.4 (2022-07-10), 21
  open issues.

### 1.4 The missing tool

No source surfaced a tool that exports MSFS glTF/BGL models **directly to
X-Plane OBJ8** (as opposed to into Blender, or back into MSFS). The community
route to OBJ8 is therefore a **chain**: MSFS asset → (MCX or msfs2blend) →
Blender → **Blender2XPlane** exporter → OBJ8. Every hop is manual and lossy.

---

## 2. File formats & technical barriers

**MSFS package** (what the prototype must read): compiled **BGL** containers
holding **glTF/GLB** models, **DDS** textures, XML descriptors, plus streamed
photogrammetry / aerial imagery and autogen that never lives in the package at
all. **X-Plane target** (what the prototype must write): **DSF** (mesh +
placements), **OBJ8** objects, `.ter`/`.pol`/`.for`, DDS/PNG textures,
`apt.dat`, `library.txt`.

Verified barriers, in rough order of how much they bite:

1. **Non-standard / quantized texture coordinates.** *"When reading BGL files
   generated by MSFS's package tool, ModelConverterX will show the texture
   mapping incorrectly because MSFS stores the texture coordinates in a
   non-standard way"* — reduced UV resolution outside the 0–1 range, severe
   enough that MCX ships a dedicated "texture distortion" diagnostic render
   mode. [primary: .../gltf-support-for-modelconverterx/] **This independently
   corroborates the prototype's single hardest low-level finding** — ASOBO
   TEXCOORD accessors declared `SHORT` but storing FLOAT16 bit patterns
   (`gltf_reader._decode_accessor`; see `docs/msfs_converter_handoff.md`). Two
   separate reverse-engineering efforts hit the same wall.

2. **MSFS 2024 re-containerised the glTF with zstd.** *"MSFS 2024 does store
   your glTF models differently in the BGL file than MSFS 2020 did … the BGL
   files are also smaller … because a compression is applied to the glTF
   data."* Before patching, MCX errored that objects were *"not recognised"*;
   it was updated to **decompress zstd**.
   [blog: scenerydesign.org/2025/02/reading-msfs-2024-library-bgl-files/;
   corroborated on FSDeveloper] The prototype currently carves uncompressed
   GLBs (MSFS 2020 path); **MSFS 2024 input will require a zstd-decompress step
   before the existing GLB carver runs.**

3. **Behaviour is not portable.** Animations, mouse/interaction rectangles, and
   visibility conditions do not survive even MSFS→glTF export (MCX, §1.2).
   Anything the prototype emits is static by construction — matching FS2XPlane's
   "animated → static" behaviour.

4. **Streamed / photogrammetry ground content is not in the package.** MSFS
   aerial imagery and CGL/streamed photogrammetry are delivered from the cloud,
   not shipped as convertible assets. No source demonstrated extracting them for
   X-Plane; this is *why* the realistic ground path is orthophoto regeneration
   (Ortho4XP), not conversion. (Marked as an open question in §6 — we found no
   tool that does it, which is evidence of difficulty, not proof of
   impossibility.)

5. **Ground-texture "float."** Objects/ground supplied as flattened-terrain
   overlays float above X-Plane's mesh (FS2XPlane, §1.1). The prototype
   side-steps this by **not** porting ground polygons — it uses apt.dat +
   exclusion zones and leaves the base mesh to X-Plane/Ortho4XP.

---

## 3. Process / workflow in practice

Synthesising the tool docs and the community's implied workflow (direct forum
transcripts were thin — see caveats):

- **3-D objects.** Extract from BGL as an *asset* (MCX or msfs2blend) → clean up
  in Blender (re-do materials, drop animations, rebuild UVs distorted by the
  quantization) → export OBJ8 via Blender2XPlane. Manual, per-object.
- **Airport layout (runways/taxiways/aprons/parking).** No evidence of any
  automated MSFS-layout → `apt.dat` path; in practice this is **re-authored by
  hand in WorldEditor (WED)**, often seeded from the X-Plane Global Airports
  gateway entry. (The prototype automates exactly this seam by copying the
  airport's apt.dat block out of Global Airports.)
- **Orthophoto / aerial imagery.** Not converted — **regenerated** from
  independent imagery sources (Ortho4XP), because MSFS's imagery is streamed.
- **Autogen / vegetation.** Not converted (FS2XPlane drops it; MSFS autogen is
  a different system). Rebuilt with X-Plane's own `.for` forests / autogen.
- **Ground polygons.** Avoided where possible due to the float problem; native
  X-Plane draped polygons authored instead.

---

## 4. Developer & community feedback

Honest scoping note: the verified, high-confidence corpus is **primary-source
heavy** (tool repos + the MCX developer's blog) and **light on direct
X-Plane.org / AVSIM / Reddit user quotes** — several forum threads that clearly
exist (e.g. an X-Plane.org "MSFS Payware and Conversion Results" multi-page
thread; FSDeveloper "MSFS sceneries conversion to XP11"; FSDeveloper
"MSFS24 – Wrong ground texture coordinates") were located but returned
low-extractable content to automated fetching. What *is* firmly established:

- **"Varying degrees of success" is the honest baseline.** Even for the
  supported legacy path, FS2XPlane's own docs hedge the output quality.
- **The recurring pain points** are consistent across sources: floating ground
  textures, lost animations, ignored stock objects, and texture-coordinate
  distortion on MSFS input.
- **Legal — the load-bearing constraint.** FS2XPlane's documentation states
  plainly: *"ownership of the derived (X-Plane format) files resides with the
  copyright holder of the original (MSFS format) files"* and *"you may not
  redistribute the derived files without the express permission of the copyright
  holder."* [primary: .../Resources/FS2XPlane.html] The tool being freeware is
  orthogonal to the *content's* copyright. **Porting payware for redistribution
  is not permitted without author consent** — personal use only. This matches
  the prototype's stated working agreement (convert = personal use unless the
  author grants redistribution; never commit third-party packages/textures).

---

## 5. Relevance to this repo's `O4_MSFS_*` prototype

The prototype (`src/O4_MSFS_Package.py`, `tools/msfs_to_obj8/`,
`src/O4_MSFS_XPlane_Pack.py`; see `docs/msfs_converter_handoff.md`) is doing
something the surveyed ecosystem does **not** ship: a modern-MSFS-BGL →
finished-X-Plane-Custom-Scenery-pack path. Cross-reading the research against
the prototype:

**Validated by the research**
- *ASOBO texture-coordinate quantization is real and load-bearing.* MCX's
  documented "non-standard texture coordinates / distortion mode" is the same
  wall the prototype's FLOAT16-as-SHORT decode solves (§2.1). Independent
  corroboration of a fiddly reverse-engineered fact.
- *Overlay-DSF + exclusion is the right structural model.* FS2XPlane also
  produces DSF **overlay** packages; the prototype's "exclude the gateway 3-D,
  place converted objects in our own overlay DSF" is the standard, reversible
  X-Plane mechanism, not a hack.
- *Static-only output is acceptable, because nothing does better.* MCX can't
  export animations either; the prototype loses no ground relative to
  best-in-class.
- *Personal-use licensing posture is correct and non-negotiable.*

**Ceilings the research says to expect (and design around)**
- *Stock objects dominate and need a GUID→library map.* FS2XPlane "ignores most
  stock objects"; the prototype's own handoff notes 798/893 test-package
  placements are stock. Research reinforces that this (prototype task #9 —
  GUID→name table + curated name→X-Plane-object substitution, seeded from
  FS2XPlane's `substitutions.txt`) is the single biggest fidelity lever, and
  that FS2XPlane's two-table architecture is the proven prior art to copy.
- *MSFS 2024 input needs a zstd pre-pass* (§2.2) — a concrete, scoped forward
  item, currently out of scope for the 2020-targeted GLB carver.
- *Ground imagery is a non-goal — hand it to Ortho4XP.* Streamed MSFS imagery
  isn't in the package; the prototype correctly stays out of ground textures and
  lets Ortho4XP/X-Plane own the base. This is the natural division of labour and
  the strongest argument for the converter living **inside** Ortho4XP: objects +
  apt.dat from MSFS, mesh + orthophoto from Ortho4XP.
- *Animations/jetways/interaction are lost* — prefer **native** apt.dat
  entities (jetways, windsocks, beacons, lights) over ported geometry wherever a
  native form exists (already prototype task #9's guidance; research supports
  it).

**Positioning.** The prototype is best framed not as "yet another converter" but
as the **missing fourth tool** in §1.4: the modern-MSFS→OBJ8+DSF+apt.dat step
that MCX (MSFS-out), msfs2blend (Blender-out), and FS2XPlane (legacy-in) each
leave undone — with Ortho4XP supplying the ground the whole ecosystem otherwise
regenerates by hand.

---

## 6. Open questions (unresolved by this pass)

1. Is there **any** current tool/workflow that exports MSFS glTF/BGL models
   directly to X-Plane **OBJ8** (not via Blender or back to MSFS), and how well
   does **Blender2XPlane** close the gap after an MCX/msfs2blend import?
2. How are MSFS **airport layouts** actually ported to `apt.dat` in practice —
   any automation, or entirely manual WED rebuild?
3. Is MSFS **streamed/CGL photogrammetry & aerial imagery** technically
   extractable at all, or fully blocked by streaming/DRM — making Ortho4XP-style
   orthophoto generation the only viable ground path?
4. What do X-Plane.org / AVSIM / Reddit developers and **payware authors**
   actually report about conversion quality, effort, and licensing enforcement?
   (Needs targeted human forum reading — automated fetch under-extracted these.)

---

## 7. Caveats & source quality

- **Moving target.** MSFS is actively changing; the MSFS 2024 zstd-BGL findings
  are Feb 2025 and MCX evolves via frequent dev releases, so exact current
  capabilities may differ. FS2XPlane, by contrast, is permanently dormant.
- **Self-reported capability.** Nearly all confirmed claims rest on the tools'
  own repos/docs and the MCX developer's blog — authoritative for what a tool
  *claims* to do, but few independent adversarial benchmarks of *conversion
  quality* were captured.
- **Access.** scenerydesign.org returned HTTP 403 to direct fetch; its quotes
  were verified via search-engine extraction + FSDeveloper corroboration
  (reproduced verbatim). Multiple FSDeveloper/X-Plane.org/threshold threads were
  located but rated low-extractable by automated fetch — hence the thin direct
  forum-quote corpus flagged in §4.
- **Terminology.** FS2XPlane's "MSFS" = legacy FS2004/FSX. Never conflate with
  MSFS 2020/2024.

### Primary sources
- FS2XPlane — repo + docs: `github.com/Marginal/FS2XPlane`,
  `github.com/Marginal/FS2XPlane/blob/master/Resources/FS2XPlane.html`
- ModelConverterX — `scenerydesign.org/modelconverterx/`,
  `scenerydesign.org/2020/09/gltf-support-for-modelconverterx/`,
  `scenerydesign.org/2025/02/reading-msfs-2024-library-bgl-files/`,
  FSDeveloper thread `.../gltf-support-for-modelconverterx.448834/`
- msfs2blend — `github.com/bestdani/msfs2blend`
- Located but low-extractable (for follow-up human reading): X-Plane.org
  "MSFS Payware and Conversion Results (2 of 2)"; FSDeveloper
  "MSFS sceneries conversion to XP11", "Reading MSFS 2024 library BGL files",
  "converting scenery files … into scenery files for X-Plane"; X-Plane KB
  "Importing Scenery"; thresholdx.net conversion articles.

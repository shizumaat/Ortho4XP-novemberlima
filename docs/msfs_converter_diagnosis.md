# MSFS converter — bad-result diagnosis: bugs vs. library reliance

Status: empirical diagnosis, 2026-07-20 (cloud session). Companion to
`docs/msfs_converter_handoff.md` (task list) and
`docs/research/msfs_to_xplane_conversion_landscape.md` (ecosystem survey).

## The question

In-sim results converting the BullfrogSim KRDM package have been poor. Is
that (a) bugs in our code, (b) missing code, or (c) the package relying on
stock-library resources as core components?

## Answer: it is all three, in measurable proportions — and the core
## custom-object conversion itself is NOT one of the problems.

Method: this cloud environment cannot fetch third-party packages (see
"Download blockage" below), so the diagnosis was run on **synthetic
compiled-BGL packages** built byte-for-byte in the real formats (ASOBO
FLOAT16-in-SHORT TEXCOORDs, raw int8 normals, CW winding, GLBs in
RIFF/GLBD blobs, LibraryObject records incl. an AttachedObject-extended
one) and pushed through the **actual production pipeline**
(`convert_msfs_airport` → OBJ8 + overlay DSF via the bundled Linux
DSFTool → `obj8_to_html` previews). Generator:
`tools/make_synth_msfs_packages.py` (writes to `scratchpad/synth_msfs/`,
untracked). All 55 existing converter unit tests pass in this
environment.

### 1. The decode core is byte-exact (not a bug source)

A terminal authored twice — once ASOBO-style (quantized UVs, int8
normals, CW winding), once spec-standard (float32, CCW) — converts to
**byte-identical OBJ8 geometry**: max deltas 0.00000 on positions,
normals, and UVs; identical index streams. Texture/PBR grouping, the
axis map, UV flip, and majority winding are correct for normal models.
"We are converting actual custom objects correctly" — for models without
mirrored nodes.

### 2. Library reliance is the dominant visual gap (KRDM is built that way)

- KRDM's recorded numbers (handoff, validated 2026-07-19): **798 of 893
  placements (89%) reference stock-library GUIDs** — jetways, fences,
  vehicles, lights — which the converter *by design* skips with only a
  warning. The flightsim.to listing confirms the design: it **requires
  World Update 10 (KTVL) ground textures and recommends the free UK2000
  object library** for statics/extras. The custom handcrafted content
  (terminal, tower, fire station, FBO, tanker base) IS embedded — the
  21 carved GLBs — but it is the minority of placements.
- Synthetic reproduction (SYNTH_LIBMIX, 13 placements = 3 custom + 9
  stock + 1 extended): pipeline output keeps **3 of 13** placements.
  The result is a recognizable but *empty-feeling* airport. This is what
  KRDM looks like with 89% of its placements dropped — no bug required.

So: even a perfect converter produces a sparse KRDM until stock-GUID
mapping (handoff task 9) exists. That task is the single biggest
fidelity lever, exactly as the handoff ranked it.

### 3. Four concrete bugs corrupt the custom 11% (all reproduced)

| # | Bug (handoff task) | Synthetic reproduction |
|---|---|---|
| 1 | Per-node winding (task 1) | Mirrored-node building: **24 of 48 triangles inside-out** — exactly the mirrored instance. Any symmetric MSFS model using negative-scale instancing renders half inside-out/invisible. |
| 2 | Placement scale dropped (task 4) | scale=1.5 terminal parsed correctly but written as plain `OBJECT` → rendered at 1.0. KRDM has scales 0.6–1.8, so converted objects are wrongly sized. |
| 3 | Altitude/AGL dropped (task 3) | +8 m AGL tower placed at ground level in the DSF (no `OBJECT_AGL`/`OBJECT_MSL`). |
| 4 | GUID/scale tail heuristic (task 5) | AttachedObject-extended record: GUID misread as `3f800000…` (the scale float read into the GUID), scale 0.0 → placement silently misclassified as "external library object" and dropped. Real packages with attached effects/lights lose those placements. |

Additionally reproduced as designed-but-observable: exclusion zones from
placement *points* (task 2) produce one small rectangle; at a real
airport the stock gateway terminal/tower still draw and z-fight or
duplicate next to converted models — a big part of "looks wrong in sim".

### 4. What this means for the "bad result so far"

Expected in-sim symptoms, ranked by visual weight, all accounted for:
1. ~89% of objects missing (stock library, by design — needs task 9).
2. Default gateway 3-D still present under/next to converted buildings
   (exclusion too small — task 2).
3. Some custom buildings half-invisible/inside-out (mirrored nodes —
   task 1).
4. Wrong sizes on scaled placements (task 4); objects at wrong height
   where MSL/AGL mattered (task 3).
5. Dark matte glass (task 6) and legacy-scalar gloss (task 7).

None of these implicate the glTF decode/axis/UV core, which is verified
byte-exact. The fix order in the handoff stands; tasks 1–5 are small and
mechanical, task 9 is the big lever.

## Download blockage (cloud) and comparison packages

- **flightsim.to is egress-policy-blocked** in this environment (CONNECT
  403 policy denial; also Cloudflare-403 to the web fetcher). KRDM has
  no mirror. General file hosts (Drive/Dropbox/Mega/archive.org) are
  also blocked; GitHub outside the session repo needs an interactive
  `add_repo` approval that was not granted this session (2 attempts).
- KRDM already exists on the local machine (`scratchpad/KRDM_Redmond`,
  per handoff); the `_needs_krdm` integration tests run there.
- Candidate free comparison airports with custom objects, GitHub-hosted
  (fetchable in-session after an `add_repo` approval, or downloadable
  locally): `Michaelvsk/msfs-etnl` (ETNL Rostock-Laage),
  `schembriaiden/LMML-MSFS` (LMML Malta, Viking Studios),
  `pasque/MSFS20-Airports` (several airports incl. project files).
  Drop any of them into `scratchpad/<name>` locally and run
  `read_package` + `convert_msfs_airport` + `obj8_to_html --pack` as
  done here.

## Repro commands

```
python3 tools/make_synth_msfs_packages.py     # build synthetic packages
# full pipeline (Linux DSFTool bundled at Utils/lin/DSFTool):
#   convert_msfs_airport(scratchpad/synth_msfs/SYNTH_*, <custom_scenery>,
#                        <global_airports>, Utils/lin/DSFTool)
python3 tools/obj8_preview/obj8_to_html.py --pack <pack> \
        --dsftool Utils/lin/DSFTool -o preview.html
```

Build-time impact: none — diagnostic tooling only, not part of the tile
build pipeline.

# Implementation spec: texture modes — Full Ortho / Airport Ortho / Default X-Plane

Status: ready for delegation.
Research background: `docs/specs/default-landclass-terrain-mode.md` (read it first —
it carries the format facts, prior art, and file:line anchors this spec builds on).
Date: 2026-07-15.

## 1. Feature summary

A per-tile **texture mode** selecting what the base mesh is textured with:

| Mode (config value) | User-visible label | Behavior |
|---|---|---|
| `full_ortho` | Full Ortho | Today's behavior, unchanged. Default. |
| `airport_ortho` | Airport Ortho | Orthophoto only on and around airports; it fades out over a configurable band (default 1 km) beyond the airport boundary into X-Plane's default landclass terrain, which covers the rest of the tile. Only the ortho textures actually used are downloaded. |
| `default_xplane` | Default X-Plane | No orthophotos at all. The custom mesh is textured entirely with X-Plane's default landclass terrain, learned by reading the default Global Scenery DSF for the tile. No imagery downloads. |

The mesh pipeline (vector map, triangulation, elevation, auto_patch) is identical
in all three modes — only DSF texturing and the imagery stage change.

## 2. Frozen architecture decisions (do not revisit in work packages)

1. **Terrain source of truth** — terrain-type-per-location is read from the
   default Global Scenery DSF of the same tile via the bundled DSFTool
   (`--dsf2text`), which transparently handles the 7z compression. No binary
   DSF parser is written. The dormant `src/Unused/C/dsf_io.c` stays dormant.
2. **Library references** — default terrains are emitted into the terrain
   definition table (`bTERT`) as the exact path strings found in the default
   DSF's `TERRAIN_DEF` lines (library virtual paths, e.g.
   `lib/g10/terrain10/<name>.ter`). No `.ter` files are generated for them, no
   textures copied, no downloads queued. Precedent in the writer: the name-only
   `terrain_Water` at index 0 (`src/O4_DSF_Utils.py:637`).
3. **Assignment granularity** — per triangle, sampled at the triangle
   **centroid**, exactly where the ortho lookup happens today
   (`src/O4_DSF_Utils.py:672` sea pass, `:905` land pass).
4. **Physical base in `airport_ortho` mode** — default landclass terrain is the
   **physical** layer for *all* land triangles, tile-wide. Ortho near airports
   is emitted as **non-physical overlay patches** (flag 2) drawn above it, with
   the fade carried by an alpha mask. Rationale: one uniform physical layer
   trivially satisfies the exactly-once coverage rule; overlay draw with
   masks is the mechanism the writer already uses for sea.
5. **Fade mechanism** — reuse the existing masked-terrain path in
   `create_terrain_file` (`src/O4_DSF_Utils.py:307-340`): the airport-ortho
   `.ter` gets `LOAD_CENTER_BORDER` + `BORDER_TEX <fade mask png>`, the patch
   gets the overlay flag, vertices use the existing 9-plane pool band with the
   second texture-coordinate pair equal to the first (the fade mask is
   georeferenced identically to the ortho texture). The DDS files are not
   modified (`imprint_masks_to_dds` is ignored by this feature).
6. **Fade geometry** — alpha = 1 (opaque ortho) everywhere inside the airport
   boundary polygon; alpha ramps linearly 1 → 0 from the boundary outward over
   `airport_ortho_fade_width` meters; 0 beyond. Triangles whose centroid lies
   beyond boundary + fade width get no overlay at all.
7. **Airport boundary source** — the same airport-area geometry already used by
   `cover_airports_with_highres` / `cover_zl` (consumed in
   `zone_list_to_ortho_dico`, `src/O4_DSF_Utils.py:219-220`). Work package 3
   locates and reuses that machinery; it must not invent a new airport model.
8. **Water is untouched in every mode** — the existing `tri_types`
   water/sea classification and `water_tech` machinery (`terrain_Water`,
   X-Plane 12 `WATER_COLOR_MASK`) already produce default-style water. In
   `default_xplane` mode the sea-mask branch is skipped entirely (all sea
   triangles take the plain water path); in `airport_ortho` mode sea masking
   applies only where ortho overlays exist near a coastal airport, else plain
   water.
9. **Projected terrains only, with fallback** — vertices bound to default
   terrains are emitted in a new 5-plane pool band (longitude, latitude,
   elevation, normal-x, normal-y; no texture coordinates), which is valid for
   `PROJECTED` terrains. If the default DSF assigns a non-projected terrain
   (work package 0 will enumerate whether any exist), substitute the most
   common projected terrain among that triangle's patch neighbors and log a
   one-line warning with a count at the end of the build. Do not implement
   explicit texture-coordinate synthesis for default terrains in version 1.
10. **No border blending between default terrains in version 1** — hard
    terrain transitions at triangle edges are accepted (see research doc,
    Phase 3). Do not emit border overlay patches between default terrains.
11. **Seams** — neighboring tiles sample the same default DSFs, so cross-tile
    terrain assignment agrees by construction. No new seam machinery.
12. **Caching** — the DSFTool text dump cache already used by
    `src/auto_patch/dsf_reader.py::_load_dsf_text` (keyed on absolute path +
    modification time) is reused as-is. Any new derived cache files go under
    the data root per the existing cache policy, never inside a scenery pack.

## 3. Frozen configuration and UI

### 3.1 Config variables (register in `src/O4_Cfg_Vars.py`, tile-scoped)

| Name | Type | Default | Values / range | Hint text (frozen copy) |
|---|---|---|---|---|
| `texture_mode` | str (enum) | `"full_ortho"` | `full_ortho`, `airport_ortho`, `default_xplane` | "What the base mesh is textured with. Full Ortho: orthophotos everywhere (classic). Airport Ortho: orthophotos on and around airports only, fading into X-Plane default terrain. Default X-Plane: no orthophotos; the custom mesh uses X-Plane default landclass terrain read from the installed Global Scenery." |
| `airport_ortho_fade_width` | float | `1000.0` | 0 – 5000 (meters) | "Airport Ortho mode: width in meters of the band beyond the airport boundary over which orthophoto fades into default terrain." |

Both persist in per-tile config files like any other tile variable, and become
`tile.texture_mode` / `tile.airport_ortho_fade_width` attributes through the
existing mechanism. They must also be registered in
`src/O4_Settings_Model.py` so the Qt settings surface knows them.

### 3.2 Qt UI (frozen copy and placement)

In the PySide6 app build area (`src/O4_Qt_GUI.py`), **after the existing build
checkboxes**: a label `Textures:` followed by a popup menu (`QComboBox`) with
items, in order: `Full Ortho`, `Airport Ortho`, `Default X-Plane` — mapping
one-to-one onto the config values above. Reads from and writes to the active
tile's config like the neighboring controls. When `Default X-Plane` is
selected, imagery-download-related progress for that tile will simply report
nothing to do; no controls are hidden or disabled in version 1.

The Tkinter app gets **no dedicated UI work** (legacy, fixes only); the config
variable appearing in its generic config window via `O4_Cfg_Vars` registration
is sufficient.

## 4. Frozen public interfaces

### 4.1 New module `src/O4_Default_Terrain_Map.py`

Core-pipeline module (not under `auto_patch/`). **Must not import any GUI
toolkit.** May import `O4_UI_Utils` for prints/progress and reuse
`src/auto_patch/dsf_reader.py`'s DSFTool location and text-dump helpers
(refactor those helpers into small shared functions if needed, without
changing `dsf_reader`'s existing public behavior).

```python
class DefaultTerrainMap:
    """Terrain-type lookup built from a default Global Scenery base-mesh DSF."""

    @classmethod
    def from_dsf(cls, dsf_path: str) -> "DefaultTerrainMap":
        """Parse the DSFTool text dump of dsf_path (7z handled by DSFTool)."""

    @classmethod
    def from_tile(cls, lat: int, lon: int) -> "DefaultTerrainMap | None":
        """Locate the default DSF for tile (lat, lon) under
        O4_Overlay_Utils.custom_overlay_src (same resolution logic as
        O4_DSF_Utils.extract_elevation_and_bathymetry_data, src/O4_DSF_Utils.py:363-379).
        Returns None with a clear printed error if not found."""

    @property
    def terrain_paths(self) -> list[str]:
        """TERRAIN_DEF table, index order preserved."""

    def terrain_index_at(self, lon: float, lat: float) -> int:
        """Terrain index of the physical triangle containing the point.
        Nearest-triangle fallback for out-of-coverage floating-point misses."""

    def terrain_path_at(self, lon: float, lat: float) -> str:
        """Convenience: terrain_paths[terrain_index_at(...)]."""

    def is_projected(self, terrain_index: int) -> bool | None:
        """True/False if the .ter file could be inspected under the X-Plane
        install; None if the terrain resource was not resolvable."""
```

Parsing contract: stream the text dump line by line (dumps run to hundreds of
megabytes); keep only patches with the physical flag bit set; expand
primitive types 0 (triangles), 1 (strip), 2 (fan); store triangle vertices as
(longitude, latitude) with the patch terrain index; spatial index via shapely
`STRtree`. Water terrains are kept like any other (callers filter if needed).

### 4.2 Writer entry points (modified, signatures unchanged)

`src/O4_DSF_Utils.py::build_dsf(tile, download_queue)` keeps its signature.
Mode dispatch happens inside, on `tile.texture_mode`. New internal helpers may
be added; existing helper signatures (`create_terrain_file`,
`zone_list_to_ortho_dico`) may gain **optional keyword arguments only**.

### 4.3 New module `src/O4_Airport_Fade_Masks.py` (work package 3)

```python
def build_airport_ortho_geometry(tile) -> "AirportOrthoGeometry":
    """Airport boundary polygons (from the same source cover_zl uses) plus the
    buffered fade band, in tile-local lon/lat. Empty geometry => no airports."""

class AirportOrthoGeometry:
    def covers(self, lon: float, lat: float) -> bool:
        """True inside boundary + fade width (i.e. this centroid gets ortho overlay)."""
    def alpha_at(self, lon: float, lat: float) -> float:
        """1.0 inside boundary; linear ramp to 0.0 across the fade band; 0.0 beyond."""
    def write_fade_mask(self, til_x: int, til_y: int, zoomlevel: int,
                        provider_code: str, mask_path: str) -> None:
        """Rasterize alpha into a grayscale PNG georeferenced exactly like the
        ortho texture tile (same web-mercator extent as the DDS, 4096x4096)."""
```

Fade mask files are named and located by a new `O4_File_Names` helper
`airport_fade_mask_name(til_x, til_y, zoomlevel, provider_code)` and must not
collide with sea-mask filenames.

## 5. Work packages

General constraints for every package (repeat in each delegation prompt):
type hints and docstrings on new code; no `exec`/`eval`; core modules never
import a GUI toolkit; no public-interface changes — if blocked, report back
instead of improvising; tests headless (`tmp_path`, no network, no X-Plane
install; anything invoking the bundled DSFTool must
`pytest.mark.skipif` on the binary's absence); work in the foreground and
report results directly.

### Work package 0 — probe tool and format facts

**Files:** new `tools/probe_default_terrain.py` (persistent tool with
docstring, per repo policy).

**Task:** a command-line tool that, given an X-Plane root (default: read the
`xplane_dir` preference) and a tile (lat, lon), runs the bundled DSFTool on the
default Global Scenery DSF and reports: every `TERRAIN_DEF` path with patch
and triangle counts; per-patch flag and plane-count histogram; for each
referenced `.ter` resolvable under the install, whether it contains
`PROJECTED` and whether it is water (`WET`); the water terrain paths and their
patch plane counts. Also `--dump-fixture` mode: write a truncated,
anonymized text-dump excerpt (a few patches of each primitive type, physical
and overlay) suitable as a pytest fixture.

**Acceptance:** running against the user's X-Plane 12 install on one tile
produces the report; the report answers: (a) exact terrain namespace used by
X-Plane 12 default DSFs, (b) count of non-projected land terrains in that
tile, (c) water terrain path and plane count. Deliver the report text back.

### Work package 1 — `DefaultTerrainMap`

**Files:** new `src/O4_Default_Terrain_Map.py`; new
`tests/test_default_terrain_map.py`; minimal shared-helper extraction in
`src/auto_patch/dsf_reader.py` (existing tests
`tests/test_dsf_object_buildings.py` etc. must stay green).

**Task:** implement the frozen interface in §4.1.

**Acceptance tests (author them):** synthetic hand-written DSFTool-text
fixtures covering: terrain table indexing; triangles/strip/fan expansion
(known winding); physical-only filtering (overlay patches ignored);
`terrain_index_at` on interior points, points on shared edges (any incident
triangle acceptable), and nearest-triangle fallback outside coverage;
`from_tile` returning None cleanly when no DSF exists. If work package 0's
fixture is available, an additional test parses it end-to-end.

### Work package 2 — writer: `default_xplane` mode

**Files:** `src/O4_DSF_Utils.py`; `src/O4_Cfg_Vars.py` (the two variables in
§3.1); `src/O4_Settings_Model.py` (registration only); new
`tests/test_dsf_texture_modes.py`; new `tools/decode_dsf_terrain_table.py`
(small persistent tool: run DSFTool on an emitted DSF and print its terrain
table and per-patch flags, for tests and manual verification).

**Task:** in `build_dsf`, when `tile.texture_mode == "default_xplane"`:
build a `DefaultTerrainMap.from_tile(...)` once (hard error with a clear
message naming `custom_overlay_src` if unavailable); in the land triangle loop
(`src/O4_DSF_Utils.py:905` area) replace the `dico_customzl` lookup with
`terrain_path_at(centroid)`; key `dico_terrains` by the terrain path; append
the path to `bTERT` with no `.ter` generation and no `download_queue.put`;
keep the physical flag (never in `overlay_terrains`). Add the 5-plane pool
band (decision 9) alongside the existing bands in `dsf_pool_plane`
(`src/O4_DSF_Utils.py:600`) with its own vertex-emission branch. In the sea
pass (`:672`), skip mask logic entirely and route all sea triangles to the
existing plain-water path. Skip the imagery/download stage for such tiles at
whatever call sites queue or await textures (report the exact sites touched).
The `full_ortho` path must be **byte-identical** to before the change
(guarded by test, see below).

**Acceptance:** (a) new tests build a tiny synthetic tile (reuse the smallest
existing mesh-fixture pattern in the test suite) in `default_xplane` mode and,
via `tools/decode_dsf_terrain_table.py` (skip-if-no-DSFTool), assert: terrain
table = `terrain_Water` + expected library paths, all land patches physical,
no generated `.ter` files, empty download queue; (b) a `full_ortho`-mode
regression test asserting the emitted DSF bytes for the same synthetic tile
are unchanged with the feature merged (build once on the parent commit via
stash A/B if needed — value determinism rules apply, pin `PYTHONHASHSEED`);
(c) full existing suite green.

### Work package 3 — writer: `airport_ortho` mode

**Files:** new `src/O4_Airport_Fade_Masks.py`; `src/O4_DSF_Utils.py`;
`src/O4_File_Names.py` (mask-name helper); new
`tests/test_airport_fade_masks.py`; extend `tests/test_dsf_texture_modes.py`.

**Task:** implement §4.3 (boundary source per decision 7 — locate the
airport-area machinery `cover_zl` uses and reuse it; report back if it turns
out unusable rather than inventing a new source). In `build_dsf`, when
`tile.texture_mode == "airport_ortho"`: physical base for all land triangles
as in work package 2; additionally, for land triangles whose centroid
satisfies `covers(...)`, emit a second, overlay ortho patch exactly like
today's masked-sea overlay path (9-plane band, second texture-coordinate pair
equal to the first, `create_terrain_file` with the fade-mask file as
`BORDER_TEX` — decisions 4–6). Ortho terrain creation queues downloads as
today, so only airport-area textures download. Fade masks are written once
per (texture tile, airport geometry) via `write_fade_mask`.

**Acceptance:** (a) unit tests for `alpha_at` (inside = 1, midpoint of the
fade band ≈ 0.5, beyond = 0) and for the mask PNG (correct size, monotone
ramp along a ray crossing the boundary, correct georeferencing of a known
point); (b) tile-level test: synthetic tile with a small square "airport"
polygon injected — assert both a physical default-terrain patch and an
overlay ortho patch exist for the same region, ortho patches carry the
overlay flag, download queue contains exactly the texture tiles intersecting
boundary + fade width and nothing else; (c) existing suite green.

### Work package 4 — configuration surface and Qt UI

**Files:** `src/O4_Qt_GUI.py`; `src/O4_Settings_Model.py`;
extend `tests/test_qt_progress.py`-style offscreen tests in a new
`tests/test_qt_texture_mode.py`.

**Task:** the control specified in §3.2 (copy and placement are frozen).
Follow the existing pattern used by the neighboring build-area checkboxes for
reading/writing the active tile's config value and persisting it. Offscreen
Qt test pattern per the repo's existing Qt tests (`QT_QPA_PLATFORM=offscreen`).

**Acceptance:** offscreen test: the combo box exists after the checkboxes,
shows the three labels in order, defaults to Full Ortho, and selecting
Default X-Plane round-trips `texture_mode="default_xplane"` into the tile
config and back on reload. Existing Qt tests green.

**Note:** UI copy or placement changes require coming back to the lead
session; they are not the agent's call.

### Work package 5 — integration, real-tile verification, docs (lead session, not delegated)

End-to-end build of the primary test airport tile (CYXY per standing ruling)
in each mode; in-simulator screenshot check for `airport_ortho` fade quality;
decide follow-ups (fade width default, Phase-3 border blending between default
terrains); update `docs/specs/default-landclass-terrain-mode.md` status and
the user docs. Fast-harness rule applies: no repeated full builds for
debugging — extend `tools/` harnesses instead.

## 6. Dependency order

Work packages 0, 1 and 4 are independent and can run in parallel. Work
package 2 needs 1 (and 0's facts to finalize decision 9's namespace details —
its code can start against the frozen interface). Work package 3 needs 2.
Work package 5 is last, lead-only.

## 7. Explicitly out of scope (version 1)

- Border blending between default terrains (hard edges accepted).
- Explicit texture-coordinate synthesis for non-projected default terrains.
- Mixed per-zone modes beyond airport buffers (e.g. `zone_list`-driven
  landclass zones) — natural follow-on, not now.
- X-Plane 11 support beyond "whatever `custom_overlay_src` points at".
- Any Tkinter UI beyond automatic config-window exposure.

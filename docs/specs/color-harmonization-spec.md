# Tile-adaptive color harmonization — specification

Status: FROZEN 2026-07-16 (implementation wave 1 delegated).
Owner: lead session. Config: `color_harmonization` (bool, default off).

## 1. Problem

A tile's textures come from imagery flown on different dates, by different
campaigns, sometimes from different providers. The result is a visible
patchwork: adjacent 4096-pixel textures with different color casts, and hard
color steps at texture seams. This is the top visual complaint about ortho
scenery after water masking.

## 2. Prior art and why we deviate

The Ypsos/ORTHO4XP_V3 fork ships an automatic normalizer that shifts every
texture's per-channel means additively toward one hardcoded target triple
(86.5, 96.5, 86.9) calibrated on 48,753 European source images, with the
shift strength scheduled by zoom level (strong at ZL13-16, nearly off at
ZL18+). Its GUI produces only optional manual residual corrections; the
automatic path is fully headless. Two lessons carry over: additive
mean-shifting with a zoom-level strength schedule is cheap and effective,
and excluding near-black/near-white pixels from the statistics keeps water
and nodata from poisoning them.

We deviate on the target. A fixed global constant imposes one continent's
"look" everywhere and washes out legitimate large-scale geography (desert
tiles get pulled green-ward, boreal tiles get brightened). Instead the
target is derived from the tile's own textures, locally.

### Why mean-compression works at all

At ZL16 one texture covers roughly 2.4 km x 2.4 km. Neighboring blocks of
land at that scale normally look alike; when they do not, the dominant cause
is acquisition (date, sensor, post-processing), not geography. Pulling
texture means toward a local consensus therefore removes acquisition
patchwork almost exclusively. At ZL17+ textures are small enough that real
content variance dominates, so the strength schedule backs off hard.

## 3. Design

### 3.1 Statistics pass

For every texture (til_x, til_y, zoomlevel, provider) of the tile, from its
assembled source image (the cached 4096-pixel source JPEG):

- Downsample to 512 x 512 with `Image.BOX`.
- Validity mask: Rec. 601 luminance strictly between 10 and 248 (excludes
  water-black, nodata-white, cloud cores).
- If valid pixels < 20% of the thumbnail, the texture is *excluded*: it
  contributes nothing to the target field and receives no shift (mostly
  water; the mask pipeline owns its look).
- Record per-channel **median** of valid pixels (median, not mean: robust to
  content outliers within the thumbnail) plus the valid fraction.

Statistics are persisted as one JSON file per tile build directory
(`color_statistics.json`, keyed by "til_x_til_y_zoomlevel_provider") so
rebuilds and step reruns do not rescan unchanged JPEGs (key includes the
JPEG's modification time).

### 3.2 Target field (the tile-adaptive part)

Textures of one zoom level form a grid. For each texture, the target is the
per-channel **median of the recorded medians over its (2r+1) x (2r+1) grid
neighborhood** (default r = 2, i.e. 5 x 5 textures, approximately 12 km x
12 km at ZL16), clamped to available neighbors at tile borders, excluded
textures skipped.

Properties: a single odd-one-out acquisition is pulled toward its
neighborhood consensus; a genuine geographic gradient (coast to desert
across the tile) survives because the neighborhood median follows it; the
field is deterministic and order-independent.

Cross-tile seams: v1 computes the field per tile. The neighborhood median
already keeps border textures close to their inner neighbors; feeding
neighbor-tile statistics into border neighborhoods is future work.

### 3.3 Shift

For a texture with recorded median m, target t, zoom level z:

    shift = clip(strength(z) * (t - m), -20, +20)   per channel

Strength schedule (module constants, not config):
ZL <= 16: 0.70; ZL 17: 0.40; ZL 18: 0.20; ZL >= 19: 0.10.

The +-20 cap bounds worst-case damage from any statistics failure mode.

Application: three 256-entry lookup tables (`Image.point`), one full-image
pass, at texture conversion time on the assembled image, before the water
mask alpha is applied and before DDS compression. Alpha, when present, is
preserved untouched. Fully deterministic; no randomness anywhere.

### 3.4 Pipeline integration

Step 3 currently overlaps downloads and conversions. The target field needs
every texture's statistics, so when `color_harmonization` is on:

- each download completion immediately computes and stores that texture's
  statistics (a few milliseconds: the JPEG is opened with `Image.draft` at
  1/8 scale, so no full 4096 decode happens);
- the convert workers are LAUNCHED only after the download thread joins and
  the target field has been computed; the convert queue simply accumulates
  in the meantime (same barrier semantics as an event, with no state inside
  the workers);
- textures with no cached source JPEG of their own (combined-provider
  compositions assembled at convert time) are excluded from statistics and
  receive no shift in v1;
- with the feature off (default), nothing changes and no barrier exists.

The wall-time cost of the barrier is bounded by the longest single download
tail; downloads dominate step 3 wall time, so the loss of overlap is small
and only paid when the feature is enabled.

### 3.5 Module layout

New pure leaf module `src/O4_Color_Harmonization.py` (imports numpy and PIL
only; no GUI toolkits, no O4_* imports). Frozen public interface:

```python
def compute_texture_color_statistics(image: Image.Image) -> dict | None
    # {"channel_medians": [r, g, b], "valid_fraction": f} or None (<20% valid)

def compute_target_field(
    statistics_by_grid_key: dict[tuple[int, int], "numpy.ndarray"],
    neighborhood_radius: int = 2,
) -> dict[tuple[int, int], "numpy.ndarray"]
    # grid key = (til_x, til_y); value = per-channel target

def compute_harmonization_shift(
    channel_medians: "numpy.ndarray",
    target: "numpy.ndarray",
    zoomlevel: int,
) -> "numpy.ndarray"
    # strength-scheduled, capped at +-20 per channel

def apply_color_shift(image: Image.Image, shift: "numpy.ndarray") -> Image.Image
    # LUT application; preserves mode and alpha; returns a new image
```

Orchestration (statistics collection, the JSON sidecar, the event barrier,
the convert_texture call) lives in `O4_Tile_Utils` / `O4_Imagery_Utils` and
is wired by the lead session, not by the module.

## 4. Config

One tile-level boolean `color_harmonization`. Qt settings: Imagery category,
"Harmonize texture colors". Default True as of 2026-07-16 for live-tile
testing (user decision); revisit after the live A/B review — it changes
every output byte, so a regression here shows up fleet-wide.

## 5. Testing (headless, mandatory)

- statistics: validity exclusions (black water / white nodata ignored);
  mostly-water thumbnail returns None; medians correct on synthetic images.
- target field: synthetic 8x8 grid with a linear gradient plus one outlier;
  the outlier's target tracks its neighborhood, gradient endpoints keep
  their local values; border clamping.
- shift: schedule values, cap behavior, zero shift when medians equal target.
- apply: LUT matches direct arithmetic, alpha preserved, input not mutated.
- end-to-end micro: two synthetic "textures" with different casts and one
  shared seam converge (post-shift medians within a few counts of each
  other at strength 0.7).

## 6. Future work

- Feed neighbor-tile statistics into border neighborhoods (cross-tile seams).
- Cloud handling: the same 512 thumbnail scan can cheaply flag
  cloud-suspect textures (bright + low saturation + locally smooth);
  replacement needs a second imagery source and is specified separately
  once harmonization is live.
- Per-provider statistics separation when a tile genuinely mixes providers
  through zone_list (v1 treats the grid uniformly; the neighborhood median
  already absorbs most of it).

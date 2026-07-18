"""Headless, exec-free settings model for the Qt settings window.

This module is the single source of truth about *which* Ortho4XP settings
the new settings window exposes, how they are grouped, and how their values
are read from / written to the flat ``key=value`` config files
(``Ortho4XP.cfg`` and the per-tile ``Ortho4XP_+XX+YYY.cfg``).

It deliberately contains **no GUI-toolkit imports**, no ``exec``/``eval``
(``ast.literal_eval`` is used only to validate list-typed values in
:func:`coerce`), and no prints.  All type/default/allowed-value/hint
metadata is sourced from the registry in :mod:`O4_Cfg_Vars`; this module
only adds presentation grouping (categories, labels, advanced flags) and
the "preference" pseudo-settings that live outside the registry.

The heavy :mod:`O4_Config_Utils` module (which has import side effects,
including creating the global config file) is imported *lazily* inside
:func:`apply_runtime` only.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass

import O4_Cfg_Vars
import O4_File_Names as FNAMES


@dataclass(frozen=True)
class Setting:
    """A single user-facing setting.

    :param name: cfg var name, or pref key when ``scope == "pref"``.
    :param label: human-readable label.
    :param scope: one of ``"app"``, ``"tile"`` or ``"pref"``.
    :param category: category key (see :data:`CATEGORIES`).
    :param advanced: whether the setting belongs to the advanced group.
    :param vtype: value type — ``bool``, ``int``, ``float``, ``str`` or ``list``.
    :param default: default value rendered as a string.
    :param values: allowed values as strings, ``()`` when free-form.
    :param hint: full hint text from the registry (``""`` for prefs).
    :param value_labels: ``((value, label), ...)`` pairs giving a
        human-readable menu title per allowed value; ``()`` when the raw
        values are shown as-is. Stored values are never affected — labels
        are display-only.
    """

    name: str
    label: str
    scope: str
    category: str
    advanced: bool
    vtype: type
    default: str
    values: tuple
    hint: str
    value_labels: tuple = ()

    def label_for(self, value: str) -> str:
        """Menu title for *value* (the raw value when unlabeled)."""
        for raw, label in self.value_labels:
            if raw == value:
                return label
        return value


# ---------------------------------------------------------------------------
# Presentation layout.
#
# Each category is (key, title, members); each member is either
#   (name, label, scope, advanced)                -- registry-backed
#   (name, label, "pref", advanced, hint)         -- preference (not in registry)
# Order within a category is significant and preserved.
# ---------------------------------------------------------------------------
_LAYOUT: list = [
    ("general", "General", [
        ("xplane_dir", "X-Plane installation", "pref", False,
         "Your X-Plane folder. Sets the Custom Scenery target, overlay "
         "source and the airport search index."),
        ("output_dir", "Output folder", "pref", False,
         "Where finished tiles are stored. Empty uses the default Tiles "
         "folder."),
        ("custom_scenery_dir", "X-Plane Custom Scenery folder", "app", False),
        ("custom_overlay_src", "Overlay source scenery folder", "app", False),
        ("custom_overlay_src_alternate", "Alternate overlay source", "app", True),
        ("cifp_data_path", "CIFP/AIRAC data folder", "app", True),
        ("verbosity", "Console output", "app", False),
        ("cleaning_level", "Build file cleanup level", "app", False),
    ]),
    ("network", "Performance & Network", [
        ("max_build_slots", "Parallel tile builds", "app", False),
        ("max_download_slots", "Parallel orthophoto downloads", "app", False),
        ("max_convert_slots", "Parallel DDS conversions", "app", False),
        ("overpass_server_choice", "OSM Overpass server", "app", False),
        ("osm_regional_extracts", "OSM regional extracts", "app", False),
        ("osm_extract_refresh_days", "Extract refresh age (days)", "app", True),
        ("http_timeout", "HTTP timeout (s)", "app", True),
        ("max_connect_retries", "Connection retries", "app", True),
        ("max_baddata_retries", "Bad-data retries", "app", True),
        ("check_tms_response", "Retry on imagery server errors", "app", True),
    ]),
    ("imagery", "Imagery & Zoom Levels", [
        ("texture_mode", "Texture mode", "tile", False),
        ("airport_ortho_fade_width", "Airport ortho fade width (m)", "tile", False),
        ("cover_airports_with_highres", "Airport imagery upgrade", "tile", False),
        ("cover_zl", "Airport coverage ZL", "tile", False),
        ("cover_extent", "Airport coverage extent (km)", "tile", False),
        ("sea_texture_blur", "Sea texture blur (m)", "tile", True),
        ("sea_nodata_fill", "Repair imagery no-data over water", "tile", False),
        ("color_harmonization", "Harmonize texture colors", "tile", False),
        ("skip_downloads", "Skip imagery downloads", "app", True),
        ("skip_converts", "Skip DDS conversion", "app", True),
    ]),
    ("mesh", "Mesh", [
        ("curvature_tol", "Curvature tolerance", "tile", False),
        ("apt_curv_tol", "Airport curvature tolerance", "tile", False),
        ("apt_curv_ext", "Airport curvature extent (km)", "tile", False),
        ("coast_curv_tol", "Coastline curvature tolerance", "tile", False),
        ("coast_curv_ext", "Coastline curvature extent (km)", "tile", False),
        ("limit_tris", "Max triangles (millions)", "tile", False),
        ("min_angle", "Min triangle angle (°)", "tile", True),
        ("sea_smoothing_mode", "Sea surface smoothing", "tile", True),
        ("water_smoothing", "Inland water smoothing passes", "tile", True),
        ("mesh_zl", "Max imagery zoom the mesh allows", "tile", True),
    ]),
    ("elevation", "Elevation", [
        ("elevation_level", "Elevation detail level", "tile", False),
        ("elevation_coastline_band_km", "Coastline lidar band width (km)", "tile", True),
        ("base_elevation_source", "Base elevation source", "app", False),
        ("custom_dem", "Custom elevation data (DEM)", "tile", False),
        ("fill_nodata", "Fill missing elevation data", "tile", False),
        ("auto_patch", "Auto-patch airports (runway slopes)", "tile", False),
        ("airport_elevation_insets", "Fetch airport lidar insets", "tile", False),
        ("airport_elevation_inset_margin_m", "Lidar extent beyond airport (m)", "tile", False),
        ("airport_elevation_inset_feather_m", "Lidar edge blend width (m)", "tile", False),
        ("airport_elevation_inset_resolution_m", "Inset storage resolution (m)", "tile", True),
        ("airport_elevation_providers", "Inset providers", "tile", True),
        ("airport_inset_water", "Detect ponds in lidar", "tile", True),
        ("apt_smoothing_pix", "Airport elevation smoothing (px)", "tile", True),
        ("apt_smoothing_auto", "Scale smoothing to data quality", "tile", True),
        ("working_grid_arc_seconds", "Working grid spacing", "tile", True),
        ("iterate", "Iterative DEM refinement step", "tile", True),
    ]),
    ("vector", "Roads & OSM Data", [
        ("road_level", "Road detail level", "tile", False),
        ("road_banking_limit", "Road banking limit (m)", "tile", True),
        ("lane_width", "Road lane width (m)", "tile", True),
        ("max_levelled_segs", "Max levelled road segments", "tile", True),
        ("clean_bad_geometries", "Repair bad OSM geometries", "tile", True),
    ]),
    ("water", "Water & Masks", [
        ("water_tech", "Water rendering tech", "tile", False),
        ("water_simplification", "Water node simplification (m)", "tile", True),
        ("min_area", "Min water area (km²)", "tile", True),
        ("max_area", "Max unmasked water area (km²)", "tile", True),
        ("ratio_water", "Water transparency ratio", "tile", False),
        ("ratio_bathy", "Bathymetry multiplier", "tile", False),
        ("mask_zl", "Water mask resolution", "tile", False),
        ("masks_width", "Mask width (m)", "tile", False),
        ("masking_mode", "Coastline mask style", "tile", False),
        ("inland_shore_feather_m", "Inland shore feather (m)", "tile", False),
        ("coastal_foam_edge", "Wavy shoreline with foam band", "tile", False),
        ("use_masks_for_inland", "Mask inland water", "tile", True),
        ("imprint_masks_to_dds", "Imprint masks into DDS", "tile", True),
        ("distance_masks_too", "Build distance masks", "tile", True),
        ("masks_custom_extent", "Custom mask extent", "tile", True),
    ]),
    ("bathymetry", "Bathymetry", [
        ("masks_use_DEM_too", "Measured depth in masks", "tile", False),
        ("bathymetry_airport_radius_km", "Fetch radius around anchors (km)", "tile", False),
        ("bathymetry_near_icao_airports", "Near ICAO airports", "tile", False),
        ("bathymetry_near_other_airports", "Near small airfields (no ICAO)", "tile", False),
        ("bathymetry_near_seaplane_bases", "Near seaplane bases", "tile", False),
        ("bathymetry_near_heliports", "Near heliports", "tile", False),
        ("reef_visibility_depth", "Reef visibility depth (m)", "tile", False),
        ("osm_shallow_water_fallback", "Mapped shallow-water fallback", "tile", False),
        ("bathymetry_band_km", "Band width along shoreline (km)", "tile", True),
        ("dsf_bathymetry", "DSF sea level raster source", "tile", True),
    ]),
    ("rendering", "Rendering & Overlays", [
        ("overlay_lod", "Overlay draw distance (m)", "tile", False),
        ("terrain_casts_shadows", "Terrain casts shadows", "tile", False),
        ("use_decal_on_terrain", "Terrain decal detail", "tile", False),
        ("normal_map_strength", "Normal map strength", "tile", True),
        ("ovl_exclude_pol", "Exclude overlay polygon types", "app", True),
        ("ovl_exclude_net", "Exclude overlay road types", "app", True),
    ]),
]


def _build_registry() -> tuple:
    """Materialise Setting objects from :data:`_LAYOUT` + the cfg registry.

    Registry-backed vars missing from ``O4_Cfg_Vars.cfg_vars`` are skipped
    gracefully (omitted).  Preference members are constructed directly.
    """
    cfg_vars = O4_Cfg_Vars.cfg_vars
    ordered: list = []
    categories: list = []
    for entry in _LAYOUT:
        cat_key, cat_title, members = entry
        categories.append((cat_key, cat_title))
        for member in members:
            if member[2] == "pref":
                name, label, scope, advanced, hint = member
                ordered.append(Setting(
                    name=name, label=label, scope=scope, category=cat_key,
                    advanced=advanced, vtype=str, default="", values=(),
                    hint=hint,
                ))
                continue
            name, label, scope, advanced = member
            spec = cfg_vars.get(name)
            if spec is None:
                # Var not present in the registry — omit gracefully.
                continue
            raw_values = spec.get("values", ())
            ordered.append(Setting(
                name=name, label=label, scope=scope, category=cat_key,
                advanced=advanced, vtype=spec["type"],
                default=str(spec["default"]),
                values=tuple(str(v) for v in raw_values),
                hint=spec.get("hint", ""),
                value_labels=tuple(
                    (str(value), title)
                    for value, title
                    in spec.get("value_labels", {}).items()
                ),
            ))
    return ordered, categories


_ALL_SETTINGS, CATEGORIES = _build_registry()
_BY_NAME: dict = {s.name: s for s in _ALL_SETTINGS}


# ---------------------------------------------------------------------------
# Registry access
# ---------------------------------------------------------------------------
def settings() -> list:
    """Return all settings in category order then declaration order."""
    return list(_ALL_SETTINGS)


def settings_for(category: str) -> list:
    """Return the settings belonging to *category* (declaration order)."""
    return [s for s in _ALL_SETTINGS if s.category == category]


def get_setting(name: str) -> Setting:
    """Return the :class:`Setting` named *name*; raise ``KeyError`` if unknown."""
    return _BY_NAME[name]


# ---------------------------------------------------------------------------
# Config file parsing helpers
# ---------------------------------------------------------------------------
def _strip_legacy_quotes(value: str) -> str:
    """Strip a single leading/trailing quote (config <= 1.20 compatibility)."""
    if value and value[0] in ('"', "'"):
        value = value[1:]
    if value and value[-1] in ('"', "'"):
        value = value[:-1]
    return value


def _parse_cfg(path: str) -> dict:
    """Parse a flat ``key=value`` config file into an ordered dict.

    Blank lines and ``#`` comments are skipped, values have legacy quotes
    stripped.  Returns ``{}`` if the file is absent.  Later duplicate keys
    win (matching the legacy loader) while keeping first-seen order.
    """
    result: dict = {}
    if not os.path.isfile(path):
        return result
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line[0] == "#":
                continue
            if "=" not in line:
                continue
            var, value = line.split("=", 1)
            result[var] = _strip_legacy_quotes(value)
    return result


def _default_global_cfg() -> str:
    """Path to the default global config file."""
    return FNAMES.data_path("Ortho4XP.cfg")


def _write_atomic_with_backup(path: str, data: dict) -> None:
    """Write ``key=value`` lines to *path* atomically, backing up any prior file.

    Parent directories are created as needed.  An existing file at *path* is
    moved to ``path + ".bak"`` (via :func:`os.replace`) only after the new
    content is fully staged in a temporary file, which is then moved into
    place with :func:`os.replace`.
    """
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for key, value in data.items():
            f.write(key + "=" + str(value) + "\n")
    if os.path.isfile(path):
        os.replace(path, path + ".bak")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------
def read_global_raw(cfg_file: str | None = None) -> dict:
    """Return the raw ``{key: value}`` contents of the global config file.

    :param cfg_file: path to read; defaults to the standard ``Ortho4XP.cfg``.
    :returns: parsed dict (``{}`` when the file is missing).
    """
    if cfg_file is None:
        cfg_file = _default_global_cfg()
    return _parse_cfg(cfg_file)


def write_global(values: dict, cfg_file: str | None = None) -> None:
    """Merge *values* into the global config file and write it back.

    Existing keys keep their order and any unknown keys are preserved
    (pass-through); new keys are appended in *values* iteration order.  The
    prior file is backed up to ``cfg_file + ".bak"``.

    :raises ValueError: if any key in *values* is a known preference
        (scope ``"pref"``), which does not belong in the global config file.
    """
    if cfg_file is None:
        cfg_file = _default_global_cfg()
    for key in values:
        setting = _BY_NAME.get(key)
        if setting is not None and setting.scope == "pref":
            raise ValueError(
                "%r is a preference and cannot be written to the global "
                "config file" % (key,)
            )
    data = _parse_cfg(cfg_file)
    for key, value in values.items():
        data[key] = str(value)
    _write_atomic_with_backup(cfg_file, data)


# ---------------------------------------------------------------------------
# Tile config
# ---------------------------------------------------------------------------
def _tile_cfg_path(lat: int, lon: int, custom_build_dir: str) -> str:
    """Return the path to the per-tile config file."""
    return os.path.join(
        FNAMES.build_dir(lat, lon, custom_build_dir),
        "Ortho4XP_" + FNAMES.short_latlon(lat, lon) + ".cfg",
    )


def read_tile_raw(lat: int, lon: int, custom_build_dir: str) -> dict | None:
    """Return the raw ``{key: value}`` contents of a tile config file.

    :returns: parsed dict, or ``None`` when the tile config file is absent.
    """
    path = _tile_cfg_path(lat, lon, custom_build_dir)
    if not os.path.isfile(path):
        return None
    return _parse_cfg(path)


# Vars whose value is never taken from the caller-supplied ``values`` and is
# preserved from the existing tile file when present.
_TILE_PRESERVED = ("zone_list", "default_website", "default_zl")


def values_equivalent(name: str, first: str, second: str) -> bool:
    """Whether two raw strings mean the same value for setting *name*.

    Comparison happens on the coerce-normalized forms so ``25000`` and
    ``25000.0`` are one value for a float setting — otherwise the sparse
    override diffing would store phantom overrides.  Unknown names or
    non-coercible values fall back to plain string equality.
    """
    if first == second:
        return True
    try:
        ok_first, normalized_first, _ = coerce(name, first)
        ok_second, normalized_second, _ = coerce(name, second)
    except KeyError:
        return False
    if ok_first and ok_second:
        return normalized_first == normalized_second
    return False


def global_effective_value(name: str, global_cfg: dict | None = None) -> str:
    """The value a tile INHERITS for *name*: global config, else default.

    :param global_cfg: pre-parsed global config (re-read when omitted).
    """
    if global_cfg is None:
        global_cfg = read_global_raw()
    if name in global_cfg:
        return global_cfg[name]
    return str(O4_Cfg_Vars.cfg_vars[name]["default"])


def write_tile(lat: int, lon: int, custom_build_dir: str, values: dict) -> None:
    """Write the tile config file as SPARSE OVERRIDES (blended model).

    Only settings that DIFFER from the value the tile would inherit (the
    global config value, else the registry default) are written — every
    other setting is pulled from global, live, at build time
    (``O4_Config_Utils.Tile`` seeds every var from the global scope and
    the tile file overwrites only the keys it contains).  Consequences:

    * Setting a var to exactly its inherited value REMOVES the override.
    * Legacy full-snapshot tile configs shrink to their true differences
      on their next write (reads of either format behave identically).
    * ``zone_list`` / ``default_website`` / ``default_zl`` are build
      provenance, not settings: they are preserved verbatim from the
      existing file (never taken from *values*, never diffed away).

    Value resolution per var: *values* when present, else the existing
    tile file value; a var in neither stays inherited.  An existing file
    is backed up to ``*.cfg.bak``; the write is atomic and parent
    directories are created as needed.

    :raises ValueError: if any key in *values* is not a tile var.
    """
    tile_vars = O4_Cfg_Vars.list_tile_vars
    for key in values:
        if key not in tile_vars:
            raise ValueError("%r is not a tile config var" % (key,))
    path = _tile_cfg_path(lat, lon, custom_build_dir)
    file_exists = os.path.isfile(path)
    existing = _parse_cfg(path) if file_exists else {}
    global_cfg = read_global_raw()

    out: dict = {}
    for var in tile_vars:
        if var in _TILE_PRESERVED:
            if file_exists and var in existing:
                out[var] = existing[var]
            continue
        if var in values:
            candidate = str(values[var])
        elif var in existing:
            candidate = existing[var]
        else:
            continue
        if values_equivalent(
            var, candidate, global_effective_value(var, global_cfg)
        ):
            continue  # equal to inherited: no override to store
        out[var] = candidate
    _write_atomic_with_backup(path, out)


# Tile-scope settings most commonly customized per tile (the pinned
# "This tile" section of the blended settings window): texture source
# mode, elevation quality, the coastline lidar band, road detail, the
# coastline mask blur, and the airport imagery zoom.
CURATED_TILE_SETTINGS = (
    "texture_mode",
    "elevation_level",
    "elevation_coastline_band_km",
    "road_level",
    "masks_width",
    "cover_zl",
)


def effective_tile_settings(
    lat: int, lon: int, custom_build_dir: str
) -> dict:
    """Blended view of every tile-scope setting for one tile.

    :returns: ``{name: (value, origin)}`` for each tile-scope setting in
        the window registry, where origin is ``"tile"`` (overridden in
        the tile file — present AND different from the inherited value,
        so legacy full-snapshot files report only true differences),
        ``"global"`` (inherited from the global config file) or
        ``"default"`` (inherited from the registry default).
    """
    tile_raw = read_tile_raw(lat, lon, custom_build_dir) or {}
    global_cfg = read_global_raw()
    blended = {}
    for setting in settings():
        if setting.scope != "tile":
            continue
        inherited = global_effective_value(setting.name, global_cfg)
        tile_value = tile_raw.get(setting.name)
        if tile_value is not None and not values_equivalent(
            setting.name, tile_value, inherited
        ):
            blended[setting.name] = (tile_value, "tile")
        elif setting.name in global_cfg:
            blended[setting.name] = (inherited, "global")
        else:
            blended[setting.name] = (inherited, "default")
    return blended


def tile_override_names(
    lat: int, lon: int, custom_build_dir: str
) -> tuple:
    """Names of the settings genuinely customized on this tile."""
    return tuple(
        name
        for name, (_value, origin) in effective_tile_settings(
            lat, lon, custom_build_dir
        ).items()
        if origin == "tile"
    )


# ---------------------------------------------------------------------------
# Value validation / normalisation
# ---------------------------------------------------------------------------
def coerce(name: str, text: str) -> tuple:
    """Validate and normalise *text* against the setting *name*'s type.

    :returns: ``(ok, normalized, error)`` — on success ``ok`` is ``True``,
        ``normalized`` is the canonical string form and ``error`` is ``""``;
        on failure ``ok`` is ``False``, ``normalized`` echoes the input and
        ``error`` is a human-readable message.
    :raises KeyError: if *name* is not a known setting.
    """
    setting = get_setting(name)
    if setting.scope == "pref":
        return (True, text, "")

    vtype = setting.vtype
    if vtype is bool:
        token = text.strip()
        if token in ("True", "true", "1"):
            normalized = "True"
        elif token in ("False", "false", "0"):
            normalized = "False"
        else:
            return (False, text, "Expected a boolean (True/False), got %r" % (text,))
    elif vtype is int:
        try:
            normalized = str(int(text.strip()))
        except (ValueError, TypeError):
            return (False, text, "Expected an integer, got %r" % (text,))
    elif vtype is float:
        try:
            normalized = str(float(text.strip()))
        except (ValueError, TypeError):
            return (False, text, "Expected a number, got %r" % (text,))
    elif vtype is list:
        try:
            parsed = ast.literal_eval(text.strip())
        except (ValueError, SyntaxError):
            return (False, text, "Expected a list, got %r" % (text,))
        if isinstance(parsed, bool):
            return (False, text, "Expected a list, got %r" % (text,))
        if isinstance(parsed, list):
            normalized = str(parsed)
        elif isinstance(parsed, (int, float)):
            # Legacy quirk: e.g. ``masks_width=100`` is a bare number.
            normalized = str(parsed)
        else:
            return (False, text, "Expected a list, got %r" % (text,))
    else:  # str
        normalized = text.strip()

    if setting.values and normalized not in setting.values:
        return (
            False, text,
            "%r is not one of: %s" % (normalized, ", ".join(setting.values)),
        )
    return (True, normalized, "")


# ---------------------------------------------------------------------------
# Runtime application
# ---------------------------------------------------------------------------
def apply_runtime(values: dict) -> list:
    """Apply *values* to the running process via ``O4_Config_Utils``.

    For each setting the module-level variable is updated through
    ``set_global_variables``; tile-scoped settings additionally get their
    ``global_``-prefixed mirror set (matching the legacy loader).  Preference
    settings are skipped silently.

    :returns: the list of setting names that failed to apply.
    """
    import O4_Config_Utils as CFG  # lazy: heavy imports + file side effects

    prefix = O4_Cfg_Vars.global_prefix
    failed: list = []
    for name, value in values.items():
        setting = _BY_NAME.get(name)
        if setting is not None and setting.scope == "pref":
            continue
        try:
            CFG.set_global_variables(name, value)
            if setting is not None and setting.scope == "tile":
                CFG.set_global_variables(prefix + name, value)
        except Exception:
            failed.append(name)
    return failed

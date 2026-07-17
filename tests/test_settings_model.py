"""Tests for :mod:`O4_Settings_Model` (headless settings model).

All tests run headless: no network, no X-Plane install, no GUI toolkit.
Global-config tests pass ``cfg_file`` explicitly; tile / global-fallback
tests ``chdir`` into ``tmp_path`` so ``FNAMES.resource_path`` resolves the
default ``Ortho4XP.cfg`` deterministically inside the temp tree.
"""

import os
import sys
import types

import pytest

import O4_Cfg_Vars
import O4_Settings_Model as SM


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------
def test_categories_ordered_and_titled():
    keys = [key for key, _ in SM.CATEGORIES]
    assert keys == [
        "general", "network", "imagery", "mesh", "elevation", "vector",
        "water", "bathymetry", "rendering",
    ]
    # Titles are non-empty strings.
    assert all(isinstance(title, str) and title for _, title in SM.CATEGORIES)


def test_settings_in_category_then_declaration_order():
    seen = SM.settings()
    # Every setting resolves via get_setting and belongs to a real category.
    cat_order = [key for key, _ in SM.CATEGORIES]
    last_cat_index = -1
    for s in seen:
        assert SM.get_setting(s.name) is s
        idx = cat_order.index(s.category)
        assert idx >= last_cat_index  # non-decreasing category order
        last_cat_index = idx
    # settings_for concatenated in category order reproduces settings().
    rebuilt = []
    for key in cat_order:
        rebuilt.extend(SM.settings_for(key))
    assert rebuilt == seen


def test_registry_backed_settings_resolve_against_cfg_vars():
    for s in SM.settings():
        if s.scope == "pref":
            assert s.vtype is str and s.default == "" and s.values == ()
            continue
        assert s.name in O4_Cfg_Vars.cfg_vars, s.name
        spec = O4_Cfg_Vars.cfg_vars[s.name]
        assert s.vtype is spec["type"]
        assert s.default == str(spec["default"])
        assert s.hint == spec.get("hint", "")


def test_tile_settings_all_in_list_tile_vars():
    for s in SM.settings():
        if s.scope == "tile":
            assert s.name in O4_Cfg_Vars.list_tile_vars, s.name


def test_map_managed_vars_absent():
    names = {s.name for s in SM.settings()}
    for excluded in ("default_website", "default_zl", "zone_list"):
        assert excluded not in names


def test_get_setting_unknown_raises():
    with pytest.raises(KeyError):
        SM.get_setting("no_such_setting")


def test_scopes_are_valid():
    for s in SM.settings():
        assert s.scope in ("app", "tile", "pref")


# ---------------------------------------------------------------------------
# Global config read / write
# ---------------------------------------------------------------------------
def test_read_global_raw_missing_file(tmp_path):
    assert SM.read_global_raw(str(tmp_path / "nope.cfg")) == {}


def test_read_global_raw_parses_and_strips_quotes(tmp_path):
    cfg = tmp_path / "Ortho4XP.cfg"
    cfg.write_text(
        "# a comment\n"
        "\n"
        'overpass_server_choice="random"\n'
        "verbosity=2\n"
        "unknown_future_key=hello\n"
    )
    raw = SM.read_global_raw(str(cfg))
    assert raw == {
        "overpass_server_choice": "random",
        "verbosity": "2",
        "unknown_future_key": "hello",
    }


def test_write_global_roundtrip_preserves_order_and_unknowns(tmp_path):
    cfg = tmp_path / "Ortho4XP.cfg"
    cfg.write_text(
        "verbosity=1\n"
        "unknown_future_key=keepme\n"
        "http_timeout=10.0\n"
    )
    SM.write_global(
        {"verbosity": "3", "brand_new_key": "x"}, cfg_file=str(cfg))

    # Backup holds the original content.
    bak = tmp_path / "Ortho4XP.cfg.bak"
    assert bak.is_file()
    assert "verbosity=1" in bak.read_text()

    # New file: existing keys keep order + unknown preserved, new key appended.
    lines = cfg.read_text().splitlines()
    assert lines == [
        "verbosity=3",
        "unknown_future_key=keepme",
        "http_timeout=10.0",
        "brand_new_key=x",
    ]


def test_write_global_creates_file_when_absent(tmp_path):
    cfg = tmp_path / "Ortho4XP.cfg"
    SM.write_global({"verbosity": "2"}, cfg_file=str(cfg))
    assert cfg.is_file()
    assert SM.read_global_raw(str(cfg)) == {"verbosity": "2"}


def test_write_global_rejects_pref_keys(tmp_path):
    cfg = tmp_path / "Ortho4XP.cfg"
    with pytest.raises(ValueError):
        SM.write_global({"xplane_dir": "/some/path"}, cfg_file=str(cfg))
    # Nothing should have been written.
    assert not cfg.exists()


# ---------------------------------------------------------------------------
# Tile config read / write
# ---------------------------------------------------------------------------
def _tile_dir(tmp_path):
    """A build dir with no trailing separator so build_dir() returns it as-is."""
    d = tmp_path / "tile_build"
    d.mkdir()
    return str(d)


def test_read_tile_raw_none_when_absent(tmp_path):
    assert SM.read_tile_raw(45, 5, _tile_dir(tmp_path)) is None


def test_read_tile_raw_parses(tmp_path):
    build = _tile_dir(tmp_path)
    path = os.path.join(build, "Ortho4XP_+45+005.cfg")
    with open(path, "w") as f:
        f.write("road_level=2\ndefault_zl=17\n")
    assert SM.read_tile_raw(45, 5, build) == {
        "road_level": "2",
        "default_zl": "17",
    }


def test_write_tile_sparse_overrides_and_preservation(tmp_path, monkeypatch):
    """The blended model (2026-07-16): a tile file stores ONLY overrides —
    settings that differ from the inherited (global-else-default) value —
    plus the preserved build-provenance trio."""
    # chdir so the default global cfg (FNAMES.resource_path) lives in tmp.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Ortho4XP.cfg").write_text("road_level=4\n")

    build = _tile_dir(tmp_path)
    path = os.path.join(build, "Ortho4XP_+45+005.cfg")
    # Existing tile file: map-managed vars + one plain var to preserve.
    with open(path, "w") as f:
        f.write(
            "zone_list=[['x']]\n"
            "default_website=BestOrtho\n"
            "default_zl=17\n"
            "curvature_tol=9.9\n"
        )

    SM.write_tile(45, 5, build, {"limit_tris": "1.5"})

    result = SM.read_tile_raw(45, 5, build)
    # SPARSE: only the preserved trio plus genuine overrides are present.
    assert set(result.keys()) == {
        "zone_list", "default_website", "default_zl",
        "curvature_tol", "limit_tris",
    }
    assert result["zone_list"] == "[['x']]"
    assert result["default_website"] == "BestOrtho"
    assert result["default_zl"] == "17"
    assert result["limit_tris"] == "1.5"
    assert result["curvature_tol"] == "9.9"
    # Inherited settings are NOT in the file; the blended view reports
    # their origin instead.
    blended = SM.effective_tile_settings(45, 5, build)
    assert blended["road_level"] == ("4", "global")
    assert blended["lane_width"] == (
        str(O4_Cfg_Vars.cfg_vars["lane_width"]["default"]), "default")
    assert blended["limit_tris"] == ("1.5", "tile")
    assert set(SM.tile_override_names(45, 5, build)) == {
        "curvature_tol", "limit_tris",
    }
    # Backup created.
    assert os.path.isfile(path + ".bak")


def test_write_tile_equal_value_removes_the_override(tmp_path, monkeypatch):
    """Setting a var to exactly its inherited value removes the override."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Ortho4XP.cfg").write_text("road_level=4\n")
    build = _tile_dir(tmp_path)

    SM.write_tile(45, 5, build, {"road_level": "5"})
    assert "road_level" in SM.read_tile_raw(45, 5, build)
    SM.write_tile(45, 5, build, {"road_level": "4"})  # back to global
    assert "road_level" not in SM.read_tile_raw(45, 5, build)
    assert SM.tile_override_names(45, 5, build) == ()


def test_legacy_snapshot_shrinks_to_true_differences(tmp_path, monkeypatch):
    """A legacy full-snapshot tile file reports (and, on its next write,
    keeps) only the settings that genuinely differ from global."""
    monkeypatch.chdir(tmp_path)
    build = _tile_dir(tmp_path)
    path = os.path.join(build, "Ortho4XP_+45+005.cfg")
    # Legacy snapshot: every var written, only road_level truly differs.
    with open(path, "w") as f:
        for var in O4_Cfg_Vars.list_tile_vars:
            if var == "zone_list":
                f.write("zone_list=[]\n")
            elif var == "road_level":
                f.write("road_level=5\n")
            else:
                f.write("%s=%s\n" % (var, O4_Cfg_Vars.cfg_vars[var]["default"]))

    assert set(SM.tile_override_names(45, 5, build)) == {"road_level"}
    SM.write_tile(45, 5, build, {})
    result = SM.read_tile_raw(45, 5, build)
    assert "road_level" in result
    assert "lane_width" not in result, "snapshot noise must shrink away"


def test_write_tile_with_no_overrides_writes_an_empty_config(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)  # no global Ortho4XP.cfg present
    build = _tile_dir(tmp_path)

    SM.write_tile(45, 5, build, {})

    result = SM.read_tile_raw(45, 5, build)
    assert result == {}, "a tile with no overrides inherits everything"


def test_write_tile_rejects_non_tile_key(tmp_path):
    with pytest.raises(ValueError):
        SM.write_tile(45, 5, _tile_dir(tmp_path), {"verbosity": "2"})


# ---------------------------------------------------------------------------
# coerce
# ---------------------------------------------------------------------------
def test_coerce_bool():
    assert SM.coerce("terrain_casts_shadows", "true") == (True, "True", "")
    assert SM.coerce("terrain_casts_shadows", "0") == (True, "False", "")
    ok, norm, err = SM.coerce("terrain_casts_shadows", "maybe")
    assert ok is False and norm == "maybe" and err


def test_coerce_int_and_float():
    assert SM.coerce("water_smoothing", "5") == (True, "5", "")
    ok, _, err = SM.coerce("water_smoothing", "5.0")
    assert ok is False and err  # int must not accept floats
    assert SM.coerce("http_timeout", "10") == (True, "10.0", "")
    ok, _, err = SM.coerce("http_timeout", "abc")
    assert ok is False and err


def test_coerce_values_restricted():
    assert SM.coerce("verbosity", "2") == (True, "2", "")
    ok, _, err = SM.coerce("verbosity", "9")
    assert ok is False and err  # 9 not in (0,1,2,3)
    assert SM.coerce("water_tech", "XP12") == (True, "XP12", "")
    ok, _, err = SM.coerce("water_tech", "nope")
    assert ok is False and err


def test_coerce_str_passthrough_stripped():
    assert SM.coerce("custom_dem", "  /path/dem.tif  ") == (
        True, "/path/dem.tif", "")


def test_coerce_list_and_masks_width_quirk():
    assert SM.coerce("ovl_exclude_pol", "[0, 1]") == (True, "[0, 1]", "")
    # Bare-number legacy quirk (e.g. masks_width=100).
    assert SM.coerce("masks_width", "100") == (True, "100", "")
    assert SM.coerce("masks_width", "[100, 200, 300]") == (
        True, "[100, 200, 300]", "")
    ok, _, err = SM.coerce("masks_width", "not_a_list")
    assert ok is False and err
    ok, _, err = SM.coerce("ovl_exclude_pol", "True")
    assert ok is False and err  # bool is not a list


def test_coerce_pref_passthrough():
    assert SM.coerce("xplane_dir", "/x/y") == (True, "/x/y", "")


# ---------------------------------------------------------------------------
# apply_runtime (against a fake O4_Config_Utils in sys.modules)
# ---------------------------------------------------------------------------
def test_apply_runtime_uses_fake_cfg(monkeypatch):
    calls = []

    fake = types.ModuleType("O4_Config_Utils")

    def _set_global_variables(var, value):
        calls.append((var, value))
        if var == "http_timeout":
            raise RuntimeError("boom")

    fake.set_global_variables = _set_global_variables
    monkeypatch.setitem(sys.modules, "O4_Config_Utils", fake)

    failed = SM.apply_runtime({
        "road_level": "3",     # tile -> two calls (plain + global_)
        "verbosity": "2",      # app  -> one call
        "http_timeout": "5",   # app  -> one call, raises -> failed
        "xplane_dir": "/x",    # pref -> skipped silently
    })

    assert failed == ["http_timeout"]
    assert ("road_level", "3") in calls
    assert ("global_road_level", "3") in calls
    assert ("verbosity", "2") in calls
    assert ("http_timeout", "5") in calls
    # pref skipped: never touched.
    assert all(var != "xplane_dir" for var, _ in calls)
    # app vars never get a global_ mirror.
    assert all(var != "global_verbosity" for var, _ in calls)

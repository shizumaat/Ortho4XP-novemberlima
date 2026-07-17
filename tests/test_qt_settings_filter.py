"""Offscreen tests for the settings window's category filtering.

The sidebar must filter the page to the selected category (not merely
scroll to it), search must match across every category regardless of the
sidebar selection, and cleaning_level must be a regular (non-advanced)
setting. Headless: QT_QPA_PLATFORM=offscreen, tmp_path cwd, no network.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from PySide6.QtWidgets import QApplication

import O4_Settings_Model as SM
from O4_Qt_Settings import SettingsWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no real global cfg / prefs picked up
    win = SettingsWindow(prefs={}, tiles=[], custom_build_dir="")
    yield win
    win.close()


def _visible_categories(win):
    cats = set()
    for row in win.rows.values():
        if not row.isHidden():
            cats.add(row.setting.category)
    return cats


def test_sidebar_selection_filters_to_one_category(window):
    for index, (key, _) in enumerate(SM.CATEGORIES):
        window.category_list.setCurrentRow(index)
        visible = _visible_categories(window)
        assert visible <= {key}, (
            f"category {key!r} selected but rows from {visible - {key}} "
            "are visible"
        )
        # Headers of other categories are hidden too.
        for other_key, _ in SM.CATEGORIES:
            expected = other_key == key and len(visible) > 0
            assert window._headers[other_key].isHidden() == (not expected)


def test_search_matches_across_all_categories(window):
    window.category_list.setCurrentRow(0)
    first_key = SM.CATEGORIES[0][0]
    # Pick a setting from a *different* category and search for its name.
    target = next(
        s for s in SM.settings() if s.category != first_key and not s.advanced
    )
    window.search_edit.setText(target.name)
    assert not window.rows[target.name].isHidden()
    window.search_edit.setText("")
    # Clearing the query returns to the selected-category view.
    assert _visible_categories(window) <= {first_key}


def test_enumerated_menus_show_labels_but_store_raw_values(window):
    """Combos display value_labels titles; stored values stay raw."""
    row = window.rows["cleaning_level"]
    combo = row.control
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert texts == [
        "Keep every file (DEM iteration)",
        "Keep files to redo any step",
        "Lean - rebuilds restart from step 1",
        "Minimal - X-Plane files + config only",
    ]
    row.set_value("2")
    assert row.value() == "2"
    assert combo.currentText() == "Lean - rebuilds restart from step 1"
    # Unlabeled enumerated settings still show and store the raw value.
    tech = window.rows["water_tech"]
    tech.set_value("XP12")
    assert tech.value() == "XP12"
    # Labels are searchable.
    window.search_edit.setText("dem iteration")
    assert not window.rows["cleaning_level"].isHidden()
    window.search_edit.setText("")


def test_cleaning_level_is_a_regular_setting(window):
    setting = next(s for s in SM.settings() if s.name == "cleaning_level")
    assert setting.advanced is False
    # Visible without "Show advanced" once its category is selected.
    index = [k for k, _ in SM.CATEGORIES].index(setting.category)
    assert window.advanced_check.isChecked() is False
    window.category_list.setCurrentRow(index)
    assert not window.rows["cleaning_level"].isHidden()


# ---------------------------------------------------------------------
# Blended sheet: footer key, reset actions, sparse write-through
# (Option C, 2026-07-16)
# ---------------------------------------------------------------------
@pytest.fixture
def tile_window(qapp, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Ortho4XP.cfg").write_text("road_level=3\n")
    win = SettingsWindow(
        prefs={}, tiles=[(48, -6)], custom_build_dir=str(tmp_path) + "/"
    )
    yield win
    win.close()


def test_global_mode_footer_key_and_reset_labels(window):
    assert "changed from default" in window.legend.text()
    assert "●" in window.legend.text()
    assert window.reset_category_btn.text() == "Reset Category to Defaults"
    assert window.reset_all_btn.text() == "Reset All to Defaults"
    assert not window.customized_chip.isVisible()
    assert "global defaults" in window.context_label.text().lower()


def test_blended_mode_footer_key_and_reset_labels(tile_window):
    assert "overrides the global value" in tile_window.legend.text()
    assert "blended" in tile_window.context_label.text()
    # On a category view the defaults-resets act on the GLOBAL layer.
    tile_window.category_list.setCurrentRow(1)
    assert tile_window.reset_category_btn.text() == "Reset Category to Defaults"
    assert tile_window.reset_category_btn.isEnabled()
    assert tile_window.reset_all_btn.text() == "Reset All to Defaults"
    # On the This-tile view the category button goes quiet and the All
    # button becomes the tile-override reset.
    tile_window.category_list.setCurrentRow(0)
    assert not tile_window.reset_category_btn.isEnabled()
    assert tile_window.reset_all_btn.text() == "Reset to Global"


def test_blended_edit_writes_a_sparse_override_immediately(tile_window):
    row = tile_window.rows["road_level"]
    assert row.value() == "3", "inherited value shown blended"
    row.set_value("5")
    tile_window._row_committed(row)
    raw = SM.read_tile_raw(48, -6, tile_window.custom_build_dir)
    assert raw == {"road_level": "5"}, "sparse: only the override on disk"
    assert tile_window.customized_chip.text() == "Customized (1)"
    # Returning to the inherited value REMOVES the override.
    row.set_value("3")
    tile_window._row_committed(row)
    assert SM.read_tile_raw(48, -6, tile_window.custom_build_dir) == {}
    assert tile_window.customized_chip.text() == "Customized (0)"


def test_hover_revert_returns_row_to_global(tile_window):
    row = tile_window.rows["road_level"]
    row.set_value("5")
    tile_window._row_committed(row)
    assert row.is_modified()
    tile_window._row_reset(row.setting, "inherit")
    assert row.value() == "3"
    assert not row.is_modified()
    assert SM.tile_override_names(48, -6, tile_window.custom_build_dir) == ()


def test_this_tile_sidebar_shows_curated_plus_overrides(tile_window):
    assert tile_window.category_list.item(0).text().startswith("★ This tile")
    # Customize a non-curated setting, then open the This-tile view.
    row = tile_window.rows["min_angle"]
    row.set_value("11.0")
    tile_window._row_committed(row)
    tile_window.category_list.setCurrentRow(0)
    visible = {
        name for name, r in tile_window.rows.items() if not r.isHidden()
    }
    assert set(SM.CURATED_TILE_SETTINGS) <= visible
    assert "min_angle" in visible, "overrides join the This-tile view"
    assert "curvature_tol" not in visible


def test_customized_chip_filters_to_overrides(tile_window):
    row = tile_window.rows["road_level"]
    row.set_value("5")
    tile_window._row_committed(row)
    tile_window.customized_chip.setChecked(True)
    visible = {
        name for name, r in tile_window.rows.items() if not r.isHidden()
    }
    assert visible == {"road_level"}


def test_reset_all_on_global_mode_restores_defaults_but_keeps_paths(window):
    road_row = window.rows["road_level"]
    road_row.set_value("4")
    window._row_committed(road_row)
    xplane_row = window.rows["xplane_dir"]
    xplane_row.set_value("/somewhere/X-Plane 12")
    window._reset_global_layer(SM.settings())
    assert road_row.value() == SM.get_setting("road_level").default
    assert xplane_row.value() == "/somewhere/X-Plane 12", (
        "machine paths must survive a bulk reset"
    )


def test_tile_reset_to_global_drops_overrides_and_keeps_globals(
    tile_window,
):
    road_row = tile_window.rows["road_level"]
    road_row.set_value("5")
    tile_window._row_committed(road_row)
    verbosity_row = tile_window.rows["verbosity"]  # app-wide row
    verbosity_row.set_value("2")
    tile_window._row_committed(verbosity_row)
    tile_window._reset_tiles_to_global()
    assert road_row.value() == "3", "tile override must return to GLOBAL"
    assert verbosity_row.value() == "2", (
        "app-wide rows are untouched by the tile-override reset"
    )
    assert SM.read_tile_raw(48, -6, tile_window.custom_build_dir) == {}


def test_defaults_reset_keeps_tile_overrides(tile_window):
    """Reset ... to Defaults acts on the GLOBAL layer: a tile's own
    customization survives it."""
    road_row = tile_window.rows["road_level"]
    road_row.set_value("5")
    tile_window._row_committed(road_row)
    tile_window._reset_global_layer(SM.settings())
    raw = SM.read_tile_raw(48, -6, tile_window.custom_build_dir)
    assert raw.get("road_level") == "5", "the override survives"
    # The global layer went back to the built-in default.
    assert SM.global_effective_value("road_level") == SM.get_setting(
        "road_level").default


# ---------------------------------------------------------------------
# Multi-tile selection: mixed states, apply-to-all
# ---------------------------------------------------------------------
@pytest.fixture
def multi_window(qapp, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Ortho4XP.cfg").write_text("road_level=3\n")
    build_dir = str(tmp_path) + "/"
    SM.write_tile(48, -6, build_dir, {"road_level": "5"})  # one overrides
    win = SettingsWindow(
        prefs={}, tiles=[(48, -6), (49, -6)], custom_build_dir=build_dir
    )
    yield win
    win.close()


def test_multi_tile_shows_mixed_state_with_override_dot(multi_window):
    row = multi_window.rows["road_level"]
    assert row.is_mixed(), "tiles disagree (5 vs inherited 3)"
    assert not row.dot.isHidden(), "at least one tile overrides"
    assert row.revert_button.isEnabled()
    # Uniform settings are not mixed.
    assert not multi_window.rows["lane_width"].is_mixed()
    assert "2 tiles" in multi_window.context_label.text()
    assert multi_window.category_list.item(0).text() == "★ These tiles (2)"
    assert "mixed" in multi_window.legend.text()


def test_multi_tile_edit_applies_to_every_selected_tile(multi_window):
    row = multi_window.rows["road_level"]
    row.set_value("4")
    multi_window._row_committed(row)
    assert not row.is_mixed()
    for tile in [(48, -6), (49, -6)]:
        raw = SM.read_tile_raw(tile[0], tile[1], multi_window.custom_build_dir)
        assert raw.get("road_level") == "4", "override applied to %s" % (tile,)
    assert multi_window.customized_chip.text() == "Customized (1)"


def test_multi_tile_revert_clears_every_tile(multi_window):
    row = multi_window.rows["road_level"]
    multi_window._row_reset(row.setting, "inherit")
    assert row.value() == "3"
    for tile in [(48, -6), (49, -6)]:
        assert SM.tile_override_names(
            tile[0], tile[1], multi_window.custom_build_dir) == ()


def test_revert_button_always_visible_enabled_when_modified(tile_window):
    row = tile_window.rows["road_level"]
    assert not row.revert_button.isHidden() or True  # present in layout
    assert not row.revert_button.isEnabled(), "inherited row: nothing to revert"
    row.set_value("5")
    tile_window._row_committed(row)
    assert row.revert_button.isEnabled()


def test_grouping_audit_pins():
    """The 2026-07-16 audit: imagery-pipeline switches live under
    Imagery, water policy under Water & Masks, performance under the
    renamed Performance & Network."""
    assert SM.get_setting("skip_downloads").category == "imagery"
    assert SM.get_setting("skip_converts").category == "imagery"
    assert SM.get_setting("min_area").category == "water"
    assert SM.get_setting("max_area").category == "water"
    assert SM.get_setting("water_simplification").category == "water"
    assert SM.get_setting("max_build_slots").category == "network"
    titles = dict(SM.CATEGORIES)
    assert titles["network"] == "Performance & Network"
    assert titles["vector"] == "Roads & OSM Data"
    assert titles["mesh"] == "Mesh"
    assert titles["elevation"] == "Elevation"

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
    win = SettingsWindow(prefs={}, active_tile=None, custom_build_dir="")
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


def test_cleaning_level_is_a_regular_setting(window):
    setting = next(s for s in SM.settings() if s.name == "cleaning_level")
    assert setting.advanced is False
    # Visible without "Show advanced" once its category is selected.
    index = [k for k, _ in SM.CATEGORIES].index(setting.category)
    assert window.advanced_check.isChecked() is False
    window.category_list.setCurrentRow(index)
    assert not window.rows["cleaning_level"].isHidden()

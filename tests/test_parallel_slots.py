"""Tests for the machine-aware "0 = Auto" slot resolution
(docs/specs/parallel-tile-builds.md §2, extended 2026-07-16).

Each parallelism knob resolves through :mod:`O4_Parallel_Utils` with the
formula its bottleneck warrants: tile builds bind on cores AND memory,
DDS conversion on cores, downloads on the network (a fixed Auto).
Headless, no network.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import O4_Parallel_Utils as PARALLEL_UTILS


def _machine(monkeypatch, cores, memory_gigabytes):
    monkeypatch.setattr(
        PARALLEL_UTILS, "machine_core_count", lambda: cores
    )
    monkeypatch.setattr(
        PARALLEL_UTILS, "machine_memory_gigabytes",
        lambda: float(memory_gigabytes),
    )


# ---------------------------------------------------------------------
# Build slots: min(cores // 3, gigabytes // 6), clamped to 1..6 in Auto;
# explicit settings are honoured beyond the Auto ceiling.
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "cores,memory,expected",
    [
        (16, 64, 5),    # big workstation: cores allow 5, memory allows 10
        (24, 128, 6),   # the Auto server-politeness ceiling holds
        (10, 32, 3),    # typical laptop: cores are the binding constraint
        (12, 16, 2),    # memory is the binding constraint (16 // 6 = 2)
        (4, 8, 1),      # small machine floors at 1
        (2, 4, 1),
        (64, 512, 6),
    ],
)
def test_auto_build_slots(monkeypatch, cores, memory, expected):
    _machine(monkeypatch, cores, memory)
    assert PARALLEL_UTILS.effective_build_slots(0) == expected


def test_explicit_build_slots_pass_through_beyond_auto_cap(monkeypatch):
    _machine(monkeypatch, 4, 8)  # auto would say 1
    assert PARALLEL_UTILS.effective_build_slots(3) == 3
    assert PARALLEL_UTILS.effective_build_slots("2") == 2
    # Explicit big-memory settings are honoured past the Auto ceiling.
    assert PARALLEL_UTILS.effective_build_slots(8) == 8


# ---------------------------------------------------------------------
# Convert slots: (cores - 2) shared among concurrent tile builds,
# clamped to 2..16.
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "cores,expected",
    [(10, 8), (4, 2), (2, 2), (32, 16)],
)
def test_auto_convert_slots(monkeypatch, cores, expected):
    _machine(monkeypatch, cores, 32)
    monkeypatch.delenv(
        PARALLEL_UTILS.PARALLEL_SIBLINGS_ENVIRONMENT_KEY, raising=False
    )
    assert PARALLEL_UTILS.effective_convert_slots(0) == expected


@pytest.mark.parametrize(
    "cores,siblings,expected",
    [
        (16, 4, 3),   # (16-2)//4 = 3
        (10, 4, 2),   # floors at 2
        (32, 2, 15),  # (32-2)//2 = 15
        (12, 6, 2),
    ],
)
def test_auto_convert_slots_share_among_siblings(
    monkeypatch, cores, siblings, expected
):
    _machine(monkeypatch, cores, 64)
    monkeypatch.setenv(
        PARALLEL_UTILS.PARALLEL_SIBLINGS_ENVIRONMENT_KEY, str(siblings)
    )
    assert PARALLEL_UTILS.effective_convert_slots(0) == expected


def test_explicit_convert_slots_pass_through(monkeypatch):
    _machine(monkeypatch, 32, 32)
    monkeypatch.setenv(
        PARALLEL_UTILS.PARALLEL_SIBLINGS_ENVIRONMENT_KEY, "4"
    )
    assert PARALLEL_UTILS.effective_convert_slots(4) == 4


# ---------------------------------------------------------------------
# Download slots: network-bound, Auto is a fixed two
# ---------------------------------------------------------------------
def test_auto_download_slots_is_two_regardless_of_cores(monkeypatch):
    monkeypatch.delenv(
        PARALLEL_UTILS.PARALLEL_SIBLINGS_ENVIRONMENT_KEY, raising=False
    )
    _machine(monkeypatch, 32, 128)
    assert PARALLEL_UTILS.effective_download_slots(0) == 2
    _machine(monkeypatch, 2, 4)
    assert PARALLEL_UTILS.effective_download_slots(0) == 2


def test_auto_download_slots_drop_to_one_with_siblings(monkeypatch):
    monkeypatch.setenv(
        PARALLEL_UTILS.PARALLEL_SIBLINGS_ENVIRONMENT_KEY, "4"
    )
    assert PARALLEL_UTILS.effective_download_slots(0) == 1
    # Explicit values are untouched by sibling sharing.
    assert PARALLEL_UTILS.effective_download_slots(3) == 3


def test_explicit_download_slots_pass_through():
    assert PARALLEL_UTILS.effective_download_slots(1) == 1
    assert PARALLEL_UTILS.effective_download_slots(4) == 4


# ---------------------------------------------------------------------
# Probes behave on this platform; session + settings wiring
# ---------------------------------------------------------------------
def test_machine_probes_return_positive_values():
    assert PARALLEL_UTILS.machine_core_count() >= 1
    assert PARALLEL_UTILS.machine_memory_gigabytes() > 0.0


def test_session_resolves_auto_through_configuration(monkeypatch):
    from o4_engine import session as SESSION

    _machine(monkeypatch, 16, 64)
    import O4_Config_Utils as CFG

    monkeypatch.setattr(CFG, "max_build_slots", 0, raising=False)
    assert SESSION._configured_build_slots() == 5
    monkeypatch.setattr(CFG, "max_build_slots", 1, raising=False)
    assert SESSION._configured_build_slots() == 1


def test_settings_model_shows_auto_label():
    import O4_Settings_Model as SM

    setting = SM.get_setting("max_build_slots")
    assert setting is not None
    assert setting.default == "0"
    assert setting.label_for("0").startswith("Auto")

"""Tests for the MSFS-airport-conversion orchestrator.

O4_MSFS_Airport_Convert is exercised against STUB implementations of its
two collaborator modules (O4_MSFS_Package, O4_MSFS_XPlane_Pack) injected
into sys.modules, so these tests pin the frozen API from the consumer
side and run even while the collaborator modules evolve. The real
tools/msfs_to_obj8 converter runs on a real (synthetic) GLB.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from test_msfs_to_obj8 import _build_test_glb  # noqa: E402  (same tests dir)


@dataclass(frozen=True)
class StubModelEntry:
    guid: str
    glb_bytes: bytes
    source_bgl: str
    texture_directory: str | None = None


@dataclass(frozen=True)
class StubPlacement:
    guid: str
    latitude: float
    longitude: float
    altitude_meters: float
    is_above_ground: bool
    heading_degrees_true: float
    pitch_degrees: float
    bank_degrees: float
    scale: float
    source_bgl: str


@dataclass(frozen=True)
class StubPlacedObject:
    object_relative_path: str
    longitude: float
    latitude: float
    heading_degrees_true: float
    altitude_meters: float = 0.0
    is_above_ground: bool = True
    bounds_xz: tuple | None = None


def _install_stub_modules(monkeypatch, models, placements, recorded):
    package_module = types.ModuleType("O4_MSFS_Package")
    package_module.ModelEntry = StubModelEntry
    package_module.ObjectPlacement = StubPlacement
    package_module.read_package = lambda directory: (models, placements, ["pkg-warning"])
    package_module.stage_texture_directory = lambda entry, staging: None

    pack_module = types.ModuleType("O4_MSFS_XPlane_Pack")
    pack_module.PlacedObject = StubPlacedObject

    def find_airport_near(apt_dat_path, latitude, longitude, max_kilometers=5.0):
        recorded["near"] = (latitude, longitude)
        return "KTST"

    def extract_airport(apt_dat_path, airport_icao):
        recorded["extracted"] = airport_icao
        return "1 900 0 0 KTST Test airport\n100 ...\n"

    def write_pack_apt_dat(pack_directory, block):
        recorded["apt_block"] = block
        target = Path(pack_directory) / "Earth nav data"
        target.mkdir(parents=True, exist_ok=True)
        (target / "apt.dat").write_text("I\n1100\n" + block + "99\n")

    def compute_exclusions(placed_objects, padding_meters=20.0):
        recorded["exclusion_input"] = list(placed_objects)
        return [(-121.2, 44.2, -121.1, 44.3)]

    def write_overlay_dsf(pack_directory, placed_objects, exclusions, dsftool):
        recorded["dsf_objects"] = list(placed_objects)
        recorded["dsf_exclusions"] = list(exclusions)
        target = Path(pack_directory) / "Earth nav data" / "+40-130"
        target.mkdir(parents=True, exist_ok=True)
        (target / "+44-122.dsf").write_bytes(b"stub-dsf")
        return Path(pack_directory) / "Earth nav data"

    pack_module.find_airport_near = find_airport_near
    pack_module.extract_airport_from_global_apt_dat = extract_airport
    pack_module.write_pack_apt_dat = write_pack_apt_dat
    pack_module.compute_exclusion_rectangles = compute_exclusions
    pack_module.write_overlay_dsf = write_overlay_dsf

    monkeypatch.setitem(sys.modules, "O4_MSFS_Package", package_module)
    monkeypatch.setitem(sys.modules, "O4_MSFS_XPlane_Pack", pack_module)
    # Force a re-import of the orchestrator against the stubs.
    sys.modules.pop("O4_MSFS_Airport_Convert", None)


def _make_fixture(tmp_path):
    glb_bytes = _build_test_glb()
    models = [
        StubModelEntry("aaaa0001", glb_bytes, "modellib.bgl"),
        StubModelEntry("bbbb0002", glb_bytes, "modellib.bgl"),  # never placed
    ]
    placements = [
        StubPlacement("aaaa0001", 44.2531, -121.1608, 0.0, True, 62.4, 0, 0, 1.0, "p.bgl"),
        StubPlacement("aaaa0001", 44.2535, -121.1601, 0.0, True, 242.4, 0, 0, 1.0, "p.bgl"),
        StubPlacement("cccc0003", 44.2530, -121.1600, 0.0, True, 0.0, 0, 0, 1.0, "p.bgl"),
    ]
    msfs_directory = tmp_path / "msfs_package"
    msfs_directory.mkdir()
    custom_scenery = tmp_path / "Custom Scenery"
    custom_scenery.mkdir()
    global_airports = tmp_path / "Global Airports"
    (global_airports / "Earth nav data").mkdir(parents=True)
    (global_airports / "Earth nav data" / "apt.dat").write_text("I\n1100\n99\n")
    return models, placements, msfs_directory, custom_scenery, global_airports


def test_full_orchestration_flow(tmp_path, monkeypatch):
    models, placements, msfs_dir, custom_scenery, global_airports = _make_fixture(tmp_path)
    recorded: dict = {}
    _install_stub_modules(monkeypatch, models, placements, recorded)
    import O4_MSFS_Airport_Convert as CONVERT

    progress_messages = []
    report = CONVERT.convert_msfs_airport(
        msfs_dir, custom_scenery, global_airports, "/usr/bin/true",
        progress_callback=lambda pct, msg: progress_messages.append((pct, msg)),
    )

    # Only the placed model was converted; the orphan placement skipped.
    assert report.models_converted == 1
    assert report.placements_skipped == 1
    assert report.airport_icao == "KTST"
    assert report.apt_dat_copied is True
    assert recorded["extracted"] == "KTST"
    # Two placements x objects-per-model, forwarded to the DSF writer.
    objects_per_model = report.objects_written
    assert objects_per_model >= 1
    assert len(recorded["dsf_objects"]) == 2 * objects_per_model
    assert report.placements_written == len(recorded["dsf_objects"])
    assert recorded["dsf_exclusions"] == [(-121.2, 44.2, -121.1, 44.3)]
    for placed in recorded["dsf_objects"]:
        assert placed.object_relative_path.startswith("objects/")
    # Model footprints flow from the converter manifest into the
    # exclusion computation (extent-sized exclusion zones).
    for placed in recorded["exclusion_input"]:
        assert placed.bounds_xz is not None
        min_x, min_z, max_x, max_z = placed.bounds_xz
        assert min_x < max_x and min_z < max_z
    # Converted objects landed inside the pack.
    pack = report.package_path
    assert pack.parent == custom_scenery
    assert list((pack / "objects").glob("*.obj"))
    assert not (pack / "_msfs_staging").exists()
    # apt.dat written with the extracted block.
    assert "KTST" in (pack / "Earth nav data" / "apt.dat").read_text()
    # Warnings propagate (package warning + skipped placement).
    assert any("pkg-warning" in w for w in report.warnings)
    assert any("skipped" in w for w in report.warnings)
    assert progress_messages and progress_messages[-1][0] == 100


def test_missing_global_airports_still_produces_pack(tmp_path, monkeypatch):
    models, placements, msfs_dir, custom_scenery, _ = _make_fixture(tmp_path)
    recorded: dict = {}
    _install_stub_modules(monkeypatch, models, placements, recorded)
    import O4_MSFS_Airport_Convert as CONVERT

    report = CONVERT.convert_msfs_airport(
        msfs_dir, custom_scenery, tmp_path / "nonexistent", "/usr/bin/true",
        package_name="Test Pack",
    )
    assert report.apt_dat_copied is False
    assert report.package_path.name == "Test Pack"
    assert any("apt.dat" in w for w in report.warnings)
    assert list((report.package_path / "objects").glob("*.obj"))


def test_cancellation_between_steps(tmp_path, monkeypatch):
    models, placements, msfs_dir, custom_scenery, global_airports = _make_fixture(tmp_path)
    recorded: dict = {}
    _install_stub_modules(monkeypatch, models, placements, recorded)
    import O4_MSFS_Airport_Convert as CONVERT
    import O4_UI_Utils as UI

    def cancel_on_first_progress(percent, message):
        UI.red_flag = True

    with pytest.raises(InterruptedError):
        CONVERT.convert_msfs_airport(
            msfs_dir, custom_scenery, global_airports, "/usr/bin/true",
            progress_callback=cancel_on_first_progress,
        )
    UI.red_flag = False

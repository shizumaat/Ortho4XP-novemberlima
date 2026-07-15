"""Unit tests for the auto_patch patch-provenance feature.

Covered surfaces:
  * config-gate introspection (synthetic source + the real config.py);
  * the no-inset LOUD case (dem label / log-line warning / RAW flag);
  * baked-inset provenance recording (the O4_Airport_Elevation_Insets helper);
  * the reader round-trip (tags -> file -> parse) incl. percent-encoding;
  * git provenance graceful-absent outside a checkout.

These are fast, build-free unit tests -- no airport build, no DEM, no network.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from auto_patch import provenance as P


# ── config-gate introspection ─────────────────────────────────────────────────
_SYNTHETIC_CONFIG = '''
import os as _os
FOO = _os.environ.get("O4_FOO", "1") == "1"
BAR = _os.environ.get("O4_BAR", "0") == "1"
RUN_M = float(_os.environ.get("O4_RUN_M", "3000"))
PINNED = _os.environ.get("O4_PARALLEL_N")  # no default -> not a gate
DUP = _os.environ.get("O4_FOO", "1")       # duplicate name, first wins
'''


def _write(tmp_path, text):
    path = tmp_path / "synthetic_config.py"
    path.write_text(text)
    return str(path)


def test_introspect_finds_gates_and_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("O4_FOO", raising=False)
    monkeypatch.delenv("O4_BAR", raising=False)
    monkeypatch.delenv("O4_RUN_M", raising=False)
    src = _write(tmp_path, _SYNTHETIC_CONFIG)
    gates = P.introspect_config_gates(src)
    assert set(gates) == {"O4_FOO", "O4_BAR", "O4_RUN_M"}
    assert gates["O4_FOO"] == {"default": "1", "value": "1", "is_boolean": True}
    assert gates["O4_BAR"]["is_boolean"] is True
    assert gates["O4_RUN_M"]["is_boolean"] is False  # non-boolean default
    # A bare environ.get with no string default is not a gate.
    assert "O4_PARALLEL_N" not in gates


def test_gate_provenance_on_and_nondefault(tmp_path, monkeypatch):
    src = _write(tmp_path, _SYNTHETIC_CONFIG)
    # Flip BAR on (default 0) and RUN_M off-default; leave FOO at default-on.
    monkeypatch.setenv("O4_BAR", "1")
    monkeypatch.setenv("O4_RUN_M", "5000")
    monkeypatch.delenv("O4_FOO", raising=False)
    prov = P.gate_provenance(src)
    assert prov["on"] == ["O4_BAR", "O4_FOO"]          # both boolean + "1"
    assert ("O4_BAR", "1") in prov["nondefault"]
    assert ("O4_RUN_M", "5000") in prov["nondefault"]
    assert ("O4_FOO", "1") not in prov["nondefault"]   # at default
    assert prov["total"] == 3


def test_introspect_missing_source_is_empty():
    assert P.introspect_config_gates("/no/such/config.py") == {}
    prov = P.gate_provenance("/no/such/config.py")
    assert prov == {"on": [], "nondefault": [], "total": 0}


def test_real_config_gates_enumerated():
    """The real config.py must yield the known slice-B + rings gates ON."""
    prov = P.gate_provenance()  # default source = auto_patch.config
    assert prov["total"] > 10   # dozens of gates are declared
    # These are documented default-ON gates (the 'bundle' + 'rings' example).
    assert "O4_ONE_SOLVE_TERRAIN" in prov["on"]
    assert "O4_GAP_FILL_INTERIOR_RINGS" in prov["on"]


# ── the no-inset LOUD case ─────────────────────────────────────────────────────
class _FakeDem:
    pass


def test_dem_provenance_absent_attribute_is_raw():
    prov = P.dem_provenance_from_dem(_FakeDem())
    assert prov == {"insets": [], "raw": True}


def test_dem_provenance_empty_list_is_raw():
    dem = _FakeDem()
    dem.airport_inset_provenance = []
    assert P.dem_provenance_from_dem(dem)["raw"] is True


def test_dem_label_and_log_line_raw_reads_as_warning():
    prov = P.assemble_provenance("CYXY", {"insets": [], "raw": True})
    assert P.dem_label(prov["dem"]) == "base RAW (no inset baked)"
    line = P.format_log_line(prov)
    assert "CYXY" in line
    assert "RAW" in line
    assert "WARNING" in line


def test_dem_label_happy_path():
    dem = _FakeDem()
    dem.airport_inset_provenance = [
        {"icao": "CYXY", "provider": "HRDEM",
         "source_ids": ["YT-Whitehorse_2019-1m"], "fetch_date": "2026-07-11"},
    ]
    meta = P.dem_provenance_from_dem(dem, icao="CYXY")
    assert meta["raw"] is False
    assert P.dem_label(meta) == "base+HRDEM(YT-Whitehorse_2019-1m)"


def test_dem_provenance_filters_by_icao():
    dem = _FakeDem()
    dem.airport_inset_provenance = [
        {"icao": "CYXY", "provider": "HRDEM", "source_ids": ["a"]},
        {"icao": "CYWH", "provider": "HRDEM", "source_ids": ["b"]},
    ]
    meta = P.dem_provenance_from_dem(dem, icao="CYXY")
    assert [e["source_ids"] for e in meta["insets"]] == [["a"]]


# ── reader round-trip ──────────────────────────────────────────────────────────
def _write_patch(tmp_path, prov):
    tags = P.provenance_tags(prov)
    root = "<osm version='0.6' upload='false' generator='O4'"
    for k, v in tags.items():
        root += f" {k}='{v}'"
    root += ">"
    text = "<?xml version='1.0' encoding='UTF-8'?>\n" + root + "\n</osm>\n"
    path = tmp_path / "TEST_auto.patch.osm"
    path.write_text(text)
    return str(path)


def test_reader_round_trip_happy(tmp_path):
    dem = _FakeDem()
    dem.airport_inset_provenance = [
        {"icao": "CYXY", "provider": "HRDEM",
         "source_ids": ["YT-Whitehorse 2019/1m"], "fetch_date": "2026-07-11"},
    ]
    prov = P.assemble_provenance(
        "CYXY", P.dem_provenance_from_dem(dem, icao="CYXY"))
    path = _write_patch(tmp_path, prov)
    got = P.parse_patch_provenance(path)
    assert got is not None
    assert got["icao"] == "CYXY"
    assert got["dem_raw"] is False
    # The source_id had a space + slash -> percent-encoding must survive.
    assert got["dem"] == "base+HRDEM(YT-Whitehorse 2019/1m)"
    assert got["gates_total"] == str(prov["gates"]["total"])


def test_reader_round_trip_raw(tmp_path):
    prov = P.assemble_provenance("CYXY", {"insets": [], "raw": True})
    path = _write_patch(tmp_path, prov)
    got = P.parse_patch_provenance(path)
    assert got["dem_raw"] is True
    assert "RAW" in got["dem"]


def test_reader_unstamped_patch_returns_none(tmp_path):
    path = tmp_path / "old_auto.patch.osm"
    path.write_text("<?xml version='1.0'?>\n<osm version='0.6'>\n</osm>\n")
    assert P.parse_patch_provenance(str(path)) is None


def test_reader_missing_file_returns_none():
    assert P.parse_patch_provenance("/no/such/file.osm") is None


# ── git provenance ─────────────────────────────────────────────────────────────
def test_git_provenance_absent_outside_checkout(tmp_path):
    got = P.git_provenance(cwd=str(tmp_path))
    assert got == {"sha": None, "dirty": None}


def test_git_provenance_in_checkout():
    got = P.git_provenance()  # the auto_patch source lives in a git tree
    # In this repo the tree is a checkout; sha should be a short hash string
    # (or None if git is somehow unavailable -- then dirty must also be None).
    if got["sha"] is None:
        assert got["dirty"] is None
    else:
        assert isinstance(got["sha"], str) and len(got["sha"]) >= 7
        assert got["dirty"] in (True, False, None)


# ── baked-inset provenance recording (the DEM-side helper) ─────────────────────
def test_inset_bake_provenance_entry_reads_sidecar(tmp_path):
    import O4_Airport_Elevation_Insets as INSETS

    tif = tmp_path / "CYXY_hrdem.tif"
    tif.write_bytes(b"\x00")
    sidecar = tmp_path / "CYXY_hrdem.json"
    sidecar.write_text(json.dumps({
        "provider": "HRDEM",
        "source_ids": ["YT-Whitehorse_2019-1m"],
        "fetch_date": "2026-07-11",
        "native_resolution_m": 1.0,
    }))
    entry = INSETS._inset_bake_provenance_entry(str(tif))
    assert entry["icao"] == "CYXY"
    assert entry["provider"] == "HRDEM"
    assert entry["source_ids"] == ["YT-Whitehorse_2019-1m"]
    assert entry["fetch_date"] == "2026-07-11"


def test_inset_bake_provenance_entry_missing_sidecar(tmp_path):
    import O4_Airport_Elevation_Insets as INSETS

    tif = tmp_path / "KBNA_usgs3dep.tif"
    tif.write_bytes(b"\x00")
    entry = INSETS._inset_bake_provenance_entry(str(tif))
    assert entry["icao"] == "KBNA"
    assert entry["path"].endswith("KBNA_usgs3dep.tif")
    # No sidecar -> no provider key (degrades gracefully, no crash).
    assert "provider" not in entry


# ── the master gate ────────────────────────────────────────────────────────────
def test_provenance_enabled_default_on(monkeypatch):
    monkeypatch.delenv("O4_PATCH_PROVENANCE", raising=False)
    assert P.provenance_enabled() is True
    monkeypatch.setenv("O4_PATCH_PROVENANCE", "0")
    assert P.provenance_enabled() is False


def test_provenance_tags_are_quote_safe(tmp_path):
    prov = P.assemble_provenance("CYXY", {"insets": [], "raw": True})
    for value in P.provenance_tags(prov).values():
        assert "'" not in value  # never breaks the single-quoted attribute

"""Tests for Overpass server selection in ``src/O4_OSM_Utils.py``.

Covers the accumulating per-request failure exclusion added on
2026-07-16: within one ``get_overpass_data`` request, every server that
has already failed is excluded from the next attempt until each server
has failed once, after which the exclusion round resets.

All headless: HTTP posting, status probing, and back-off sleeping are
monkeypatched; no network access.
"""

import types

import pytest

import O4_OSM_Utils as OSM


@pytest.fixture()
def three_servers(monkeypatch):
    """Install a synthetic three-server pool and neutralise stickiness."""
    monkeypatch.setattr(
        OSM,
        "overpass_servers",
        {
            "alpha": "https://alpha.example/api/interpreter",
            "beta": "https://beta.example/api/interpreter",
            "gamma": "https://gamma.example/api/interpreter",
        },
    )
    monkeypatch.setattr(OSM, "overpass_server_choice", "random")
    if hasattr(OSM.get_overpass_data, "last_successful_server_key"):
        monkeypatch.delattr(
            OSM.get_overpass_data, "last_successful_server_key"
        )
    return ["alpha", "beta", "gamma"]


def _stub_probe_to_first_candidate(monkeypatch, probed_candidate_lists):
    """Record each candidate list the status probe sees; pick its head."""

    def fake_probe(candidate_keys):
        candidate_keys = list(candidate_keys)
        probed_candidate_lists.append(candidate_keys)
        return candidate_keys[0]

    monkeypatch.setattr(
        OSM, "_select_most_available_server_key", fake_probe
    )


def test_failed_servers_are_excluded_until_pool_exhausted(
    monkeypatch, three_servers
):
    probed = []
    _stub_probe_to_first_candidate(monkeypatch, probed)

    key_one = OSM._select_overpass_server_key(three_servers, set())
    key_two = OSM._select_overpass_server_key(three_servers, {key_one})
    key_three = OSM._select_overpass_server_key(
        three_servers, {key_one, key_two}
    )
    assert {key_one, key_two, key_three} == set(three_servers)
    # With two of three servers failed only one candidate remains, so the
    # last pick must not have gone through the status probe.
    assert probed[-1] != [key_three] or len(probed) == 2


def test_all_failed_falls_back_to_full_pool(monkeypatch, three_servers):
    probed = []
    _stub_probe_to_first_candidate(monkeypatch, probed)

    key = OSM._select_overpass_server_key(
        three_servers, set(three_servers)
    )
    assert key in three_servers
    assert probed[-1] == three_servers


def test_sticky_server_skipped_once_it_failed(monkeypatch, three_servers):
    probed = []
    _stub_probe_to_first_candidate(monkeypatch, probed)
    monkeypatch.setattr(
        OSM.get_overpass_data,
        "last_successful_server_key",
        "beta",
        raising=False,
    )

    assert OSM._select_overpass_server_key(three_servers, set()) == "beta"
    key_after_failure = OSM._select_overpass_server_key(
        three_servers, {"beta"}
    )
    assert key_after_failure != "beta"


def test_pinned_choice_wins_even_after_failure(monkeypatch, three_servers):
    monkeypatch.setattr(OSM, "overpass_server_choice", "gamma")
    key = OSM._select_overpass_server_key(three_servers, {"gamma"})
    assert key == "gamma"


def test_request_rotates_through_every_server_before_repeating(
    monkeypatch, three_servers
):
    """End-to-end through get_overpass_data with every attempt failing:
    each server must be tried once before any server is tried again."""
    monkeypatch.setattr(OSM, "max_osm_tentatives", 5)
    monkeypatch.setattr(OSM.time, "sleep", lambda seconds: None)
    probed = []
    _stub_probe_to_first_candidate(monkeypatch, probed)

    attempted_keys = []

    def failing_post(server_key, overpass_query, request_label):
        attempted_keys.append(server_key)
        raise OSM.requests.RequestException("synthetic outage")

    monkeypatch.setattr(
        OSM, "_post_overpass_query_reporting_progress", failing_post
    )

    result = OSM.get_overpass_data('way["highway"]', (0, 0, 1, 1))

    assert result == 0
    assert len(attempted_keys) == 5
    assert set(attempted_keys[:3]) == set(three_servers)
    # Round two starts over from the full pool.
    assert len(set(attempted_keys[3:])) == len(attempted_keys[3:])

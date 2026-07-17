"""Headless tests for the learned per-step tile build-time model.

Every test monkeypatches ``STORE_DIRECTORY`` to a ``tmp_path`` so no
real user history is read or written, and nothing here touches the
network or an X-Plane install.  The model is purely advisory, so the
tests assert on relative behavior (learned beats fallback, cached beats
cold, learned rate recovered within tolerance) rather than exact
seconds.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from o4_engine import tile_time_model as model  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the model's store at an isolated temporary directory."""
    directory = tmp_path / "tile_build_times"
    monkeypatch.setattr(model, "STORE_DIRECTORY", str(directory))
    return directory


def _features(**overrides):
    base = dict(zoomlevel=16, provider="BI", textures_total=0,
                textures_missing=0, airports=0)
    base.update(overrides)
    return model.features_for_record(**base)


def test_same_tile_round_trip_beats_fallback(store):
    """A rebuilt tile predicts from its own history, not the constant."""
    features = _features()
    for _ in range(3):
        model.record_build(10, -20, features,
                           {"mesh": 42.0, "masks": 12.0})
    predictions = model.predict_step_seconds(10, -20, features,
                                             ["mesh", "masks"])
    assert predictions["mesh"] == pytest.approx(42.0)
    assert predictions["masks"] == pytest.approx(12.0)
    # And these are not merely the fallback constants.
    assert predictions["mesh"] != model.DEFAULT_STEP_SECONDS["mesh"]


def test_history_ages_to_cap(store):
    """No tile file grows beyond RECORDS_KEPT_PER_TILE records."""
    features = _features()
    for index in range(model.RECORDS_KEPT_PER_TILE + 5):
        model.record_build(1, 2, features, {"mesh": float(index)})
    path = model._tile_record_path(1, 2)
    with open(path) as record_file:
        records = json.load(record_file)
    assert len(records) == model.RECORDS_KEPT_PER_TILE
    # Newest-last: the final record is the last one written.
    last_mesh = records[-1]["step_seconds"]["mesh"]
    assert last_mesh == pytest.approx(
        float(model.RECORDS_KEPT_PER_TILE + 5 - 1))


def test_imagery_cached_cheaper_than_cold(store):
    """A fully cached tile predicts materially less than a cold one."""
    # Populate one provider+zoom bucket with builds whose missing and
    # total counts vary independently, so least squares can separate the
    # fetch rate from the convert rate.
    samples = [(120, 120), (40, 150), (90, 90), (30, 200)]
    for index, (missing, total) in enumerate(samples):
        seconds = 2.0 * missing + 0.5 * total
        model.record_build(30 + index, 40, _features(
            provider="BI", zoomlevel=17,
            textures_total=total, textures_missing=missing),
            {"imagery": seconds})
    # Predict for a NEW tile (no own history) at the same size, cached
    # versus cold.
    cold = model.predict_step_seconds(0, 0, _features(
        provider="BI", zoomlevel=17,
        textures_total=200, textures_missing=200), ["imagery"])["imagery"]
    cached = model.predict_step_seconds(0, 0, _features(
        provider="BI", zoomlevel=17,
        textures_total=200, textures_missing=0), ["imagery"])["imagery"]
    assert cached < cold
    # Materially less: cached should be well under half the cold time
    # given the fetch cost dominates here.
    assert cached < 0.6 * cold


def test_imagery_recovers_known_fetch_rate(store):
    """A >=3-record bucket recovers the synthetic fetch rate within 20%."""
    fetch_rate = 2.0
    convert_rate = 0.5
    # Vary missing and total independently so the two rates are
    # identifiable by least squares.
    samples = [(100, 120), (60, 200), (150, 150), (40, 90), (200, 210)]
    for index, (missing, total) in enumerate(samples):
        seconds = fetch_rate * missing + convert_rate * total
        model.record_build(50 + index, 60, _features(
            provider="EOX", zoomlevel=18,
            textures_total=total, textures_missing=missing),
            {"imagery": seconds})
    # Recover the fetch rate as the difference two predictions make per
    # missing texture at fixed total.
    base = model.predict_step_seconds(0, 0, _features(
        provider="EOX", zoomlevel=18,
        textures_total=100, textures_missing=0), ["imagery"])["imagery"]
    plus = model.predict_step_seconds(0, 0, _features(
        provider="EOX", zoomlevel=18,
        textures_total=100, textures_missing=50), ["imagery"])["imagery"]
    recovered_fetch_rate = (plus - base) / 50.0
    assert recovered_fetch_rate == pytest.approx(fetch_rate, rel=0.20)


def test_vector_leverages_autopatch_prediction(store):
    """Vector prediction = learned overhead + the auto-patch prediction."""
    # Synthetic history where the vector step took overhead + airport
    # time, and the airport time is recorded as autopatch_seconds.
    overhead = 18.0
    for index in range(3):
        autopatch_seconds = 120.0 + 5.0 * index
        model.record_build(70, 80, _features(
            provider="BI", zoomlevel=16,
            autopatch_seconds=autopatch_seconds),
            {"vector": overhead + autopatch_seconds})
    # Now predict with a fresh auto-patch prediction for the next build.
    autopatch_prediction = 200.0
    predicted = model.predict_step_seconds(70, 80, _features(
        provider="BI", zoomlevel=16,
        autopatch_prediction_s=autopatch_prediction),
        ["vector"])["vector"]
    assert predicted == pytest.approx(overhead + autopatch_prediction, abs=1e-6)


def test_missing_store_yields_fallback(store):
    """An empty store returns the fallback constants, no exception."""
    # store fixture points at a directory that does not exist yet.
    predictions = model.predict_step_seconds(
        5, 5, _features(), list(model.STEP_KEYS))
    for step_key in model.STEP_KEYS:
        assert predictions[step_key] == model.DEFAULT_STEP_SECONDS[step_key]


def test_corrupt_json_is_ignored(store):
    """A corrupt tile file falls back cleanly instead of raising."""
    os.makedirs(str(store), exist_ok=True)
    path = model._tile_record_path(9, 9)
    with open(path, "w") as record_file:
        record_file.write("{ this is not valid json ]")
    predictions = model.predict_step_seconds(9, 9, _features(), ["mesh"])
    assert predictions["mesh"] == model.DEFAULT_STEP_SECONDS["mesh"]


def test_forward_compatible_old_records(store):
    """Records missing newer feature keys still predict without KeyError."""
    os.makedirs(str(store), exist_ok=True)
    path = model._tile_record_path(3, 3)
    # An "old" record with only the earliest feature keys present.
    old_records = [{
        "finished_at": 0.0,
        "features": {"zoomlevel": 16, "provider": "BI"},
        "step_seconds": {"mesh": 55.0, "imagery": 250.0},
    }]
    with open(path, "w") as record_file:
        json.dump(old_records, record_file)
    predictions = model.predict_step_seconds(3, 3, _features(
        zoomlevel=16, provider="BI"), ["mesh", "imagery", "vector"])
    # Same-tile history supplies mesh and imagery; vector has no basis
    # here and falls back, but nothing raises.
    assert predictions["mesh"] == pytest.approx(55.0)
    assert predictions["imagery"] == pytest.approx(250.0)
    assert predictions["vector"] == model.DEFAULT_STEP_SECONDS["vector"]


def test_features_for_record_tolerates_optional_absent():
    """The constructor fills defaults for every optional field."""
    features = model.features_for_record(zoomlevel=16, provider="BI")
    assert features["textures_total"] == 0
    assert features["textures_missing"] == 0
    assert features["airports"] == 0
    assert features["autopatch_prediction_s"] == 0.0
    assert features["autopatch_seconds"] == 0.0


# ---------------------------------------------------------------------------
# Pre-build texture-feature estimation (cold/warm cache signal, 2026-07-17)
# ---------------------------------------------------------------------------

def test_estimate_texture_features_warm_rebuild(store, tmp_path):
    """A tile with every texture on disk estimates zero missing."""
    features = _features(textures_total=40, textures_missing=40)
    model.record_build(10, -20, features, {"imagery": 400.0})
    textures = tmp_path / "textures"
    textures.mkdir()
    for index in range(40):
        (textures / ("%d_%d_BI16.dds" % (100 + index, 200))).touch()
    estimated = model.estimate_texture_features(
        10, -20, 16, "BI", str(textures))
    assert estimated == {"textures_total": 40, "textures_missing": 0}


def test_estimate_texture_features_cold_tile(store, tmp_path):
    """History total with an empty textures directory: all missing."""
    model.record_build(10, -20, _features(textures_total=40),
                       {"imagery": 400.0})
    textures = tmp_path / "textures"
    textures.mkdir()
    estimated = model.estimate_texture_features(
        10, -20, 16, "BI", str(textures))
    assert estimated == {"textures_total": 40, "textures_missing": 40}


def test_estimate_texture_features_ignores_other_bucket(store, tmp_path):
    """Files of another provider/zoom never count as present, and a
    record at a different zoom never supplies the total."""
    model.record_build(10, -20, _features(zoomlevel=17, textures_total=160),
                       {"imagery": 900.0})
    textures = tmp_path / "textures"
    textures.mkdir()
    (textures / "100_200_BI17.dds").touch()
    (textures / "100_200_GO216.dds").touch()
    estimated = model.estimate_texture_features(
        10, -20, 16, "BI", str(textures))
    assert estimated == {"textures_total": 0, "textures_missing": 0}


def test_estimate_texture_features_never_raises(store):
    """Unknown tile and a nonexistent directory degrade to zeros."""
    estimated = model.estimate_texture_features(
        55, 55, 16, "BI", "/nonexistent/anywhere")
    assert estimated == {"textures_total": 0, "textures_missing": 0}

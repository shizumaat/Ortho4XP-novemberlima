"""Learned per-step wall-clock estimates for whole-tile builds.

The Qt progress display wants to answer "about how long will this tile
take, step by step?" BEFORE any elapsed-time extrapolation is
meaningful (the early steps finish fast and a naive extrapolation locks
in a wrong total, exactly the failure the auto-patch build-time model
was written to cure).  This module is the tile-level sibling of
``auto_patch.build_time_model`` and follows the same philosophy:

* every finished tile build records its features (zoom level, provider,
  texture counts, airport counts, and whatever the auto-patch model
  already predicted for its airports) plus the measured wall seconds of
  each executed step — one small JSON file per tile under
  ``~/.ortho4xp/tile_build_times/`` named by ``short_latlon``;
* a REBUILD of the same tile predicts from that tile's own recent
  history first, because a user who rebuilds a tile is the strongest
  possible signal for how long the next rebuild takes;
* a first-time tile predicts from cross-tile rates fit over every other
  recorded tile — for imagery this is a genuine linear model in the
  texture counts (fetch cost scales with textures still missing,
  convert cost with textures total), because a fully cached tile is
  dramatically cheaper than a cold one at the same size;
* the airport/vector step deliberately does NOT re-model airports.  The
  auto-patch model already predicts airport build time; this module
  only learns the residual OSM/vector overhead and adds the auto-patch
  prediction on top, so the two models compose instead of competing.

Everything here is advisory and cosmetic.  Every function swallows its
own errors: ``record_build`` silently no-ops and ``predict_step_seconds``
always returns a usable dict (falling back to fixed constants), so a
corrupt store or a missing home directory can never take down a build.
"""

import json
import math
import os
import statistics
import time

# One JSON file per tile lives here.  Kept module-level and mutable so
# the tests can monkeypatch it at a ``tmp_path`` without touching a real
# user's history.
STORE_DIRECTORY = os.path.join(
    os.path.expanduser("~"), ".ortho4xp", "tile_build_times")

# Per-tile history kept (newest last).  Old records age out so that a
# one-off slow build (a cold cache, a throttled provider) stops skewing
# the estimate after a handful of rebuilds.
RECORDS_KEPT_PER_TILE = 8

# The build steps this model knows about, in pipeline order.  A given
# build executes a subset of these (a mesh-only rebuild records only
# "mesh", for example), and prediction is asked for whatever subset the
# caller plans to run.
STEP_KEYS = ("vector", "mesh", "masks", "imagery", "overlays")

# Last-resort per-step seconds when there is no history and no
# cross-tile basis at all (an empty store on a fresh install).  These
# are deliberately coarse ballparks, not precise figures — as soon as a
# single build is recorded the learned values take over.
DEFAULT_STEP_SECONDS = {
    "vector": 30.0,
    "mesh": 60.0,
    "masks": 30.0,
    "imagery": 300.0,
    "overlays": 60.0,
}

# When a running step outlives its prediction, the prediction is
# known-broken — but remaining time must NOT pin at "almost done" (the
# old zero floor showed "less than a minute" for however long the
# overrun lasted).  Instead the underestimate is assumed proportional
# to the overrun: remaining grows at this fraction of the time run
# past the estimate.  Continuous at the boundary (both sides reach 0).
OVERRUN_REMAINING_FRACTION = 0.5


def remaining_step_seconds(estimate_seconds, elapsed_seconds):
    """Remaining seconds for a step given its prediction and elapsed run.

    Under the estimate: the plain difference.  Past it (or with no
    estimate at all — ``None`` prices as zero, i.e. pure elapsed
    extrapolation), remaining is ``OVERRUN_REMAINING_FRACTION`` of the
    overrun, so an underestimated step reads as steadily receding
    rather than perpetually finished.
    """
    estimate = float(estimate_seconds) if estimate_seconds else 0.0
    elapsed = max(float(elapsed_seconds), 0.0)
    if elapsed < estimate:
        return estimate - elapsed
    return OVERRUN_REMAINING_FRACTION * (elapsed - estimate)


# Learned OSM/vector overhead is floored here.  The vector step always
# does some fixed parsing/serialization work even when the tile has no
# airports, so a learned overhead below this is treated as noise.
MINIMUM_VECTOR_OVERHEAD_S = 5.0

# How many of a tile's most recent records feed the same-tile median.
# A short window keeps the estimate responsive to recent perf changes,
# matching the auto-patch model's own [-3:] window.
_SAME_TILE_WINDOW = 3


def features_for_record(*, zoomlevel: int, provider: str,
                        textures_total: int = 0, textures_missing: int = 0,
                        airports: int = 0, autopatch_prediction_s: float = 0.0,
                        autopatch_seconds: float = 0.0) -> dict:
    """Build the canonical feature dictionary for one tile build.

    Callers (the build driver) and the store must agree on exact key
    names, so this constructor is the single place those names are
    spelled.  The output is a plain ``dict`` and readers tolerate extra
    keys, which lets the record format grow new features without
    breaking older stored records or older readers.

    ``zoomlevel`` and ``provider`` identify the imagery bucket a record
    belongs to; ``textures_total`` / ``textures_missing`` drive the
    linear imagery model; ``airports`` is informational; and the two
    ``autopatch_*`` fields let the vector step lean on the auto-patch
    model instead of re-predicting airport time (``autopatch_prediction_s``
    is that model's estimate for a build about to run, ``autopatch_seconds``
    is the measured airport time of a build that already finished).
    """
    return {
        "zoomlevel": int(zoomlevel),
        "provider": str(provider),
        "textures_total": int(textures_total),
        "textures_missing": int(textures_missing),
        "airports": int(airports),
        "autopatch_prediction_s": float(autopatch_prediction_s),
        "autopatch_seconds": float(autopatch_seconds),
    }


def estimate_texture_features(lat: int, lon: int, zoomlevel: int,
                              provider: str,
                              textures_directory: str) -> dict:
    """Pre-build ``textures_total`` / ``textures_missing`` estimate.

    The cold/warm cache signal BEFORE the build runs: ``textures_total``
    comes from this tile's newest record at the same provider and zoom
    level (the DSF references the same textures build after build), and
    ``textures_missing`` is that total minus the matching ``.dds`` files
    already present in the tile's textures directory — a fully warm
    rebuild estimates zero missing and collapses the imagery prediction
    to convert-only time.  Both fall back to zero when unknown (a tile
    never built at this provider/zoom), which the prediction model treats
    as "no texture basis".  Never raises.
    """
    total = 0
    try:
        for record in reversed(_load_tile_records(lat, lon)):
            record_features = record.get("features") or {}
            if (record_features.get("provider") == provider
                    and record_features.get("zoomlevel") == int(zoomlevel)):
                candidate = record_features.get("textures_total")
                if isinstance(candidate, (int, float)) and candidate > 0:
                    total = int(candidate)
                    break
    except Exception:
        total = 0
    present = 0
    try:
        # Standard texture names end with "_<provider><zoomlevel>.dds"
        # (O4_File_Names.dds_file_name_from_attributes).
        suffix = "_%s%d.dds" % (provider, int(zoomlevel))
        present = sum(
            1 for file_name in os.listdir(textures_directory)
            if file_name.endswith(suffix)
        )
    except Exception:
        present = 0
    missing = max(total - present, 0) if total else 0
    return {"textures_total": total, "textures_missing": missing}


def _tile_record_path(lat: int, lon: int) -> str:
    """Absolute path of the JSON history file for one tile.

    The file name is ``short_latlon`` (``+XX+YYY``) so the store is
    human-browsable and lines up with the tile naming used everywhere
    else in Ortho4XP.  ``short_latlon`` is imported defensively: if the
    heavier ``O4_File_Names`` module (which pulls in UI helpers) cannot
    be imported in a bare environment, a local reimplementation of the
    identical format is used so this module never hard-depends on it.
    """
    try:
        from O4_File_Names import short_latlon
        name = short_latlon(lat, lon)
    except Exception:
        name = "{:+03.0f}{:+04.0f}".format(lat, lon)
    return os.path.join(STORE_DIRECTORY, name + ".json")


def _load_tile_records(lat: int, lon: int) -> list:
    """This tile's build records, oldest first; ``[]`` on any problem.

    A corrupt or unreadable file is treated as no history rather than an
    error, so a single damaged file never blocks a prediction — the
    prediction simply falls through to the cross-tile or constant basis.
    """
    try:
        with open(_tile_record_path(lat, lon)) as record_file:
            records = json.load(record_file)
        return records if isinstance(records, list) else []
    except Exception:
        return []


def _load_all_records() -> list:
    """Every record from every tile file in the store (flat list).

    Cross-tile rate fitting wants as many data points as it can get, so
    unlike the same-tile path this pools all records rather than only
    the newest per tile.  Each unreadable file is skipped individually,
    so one corrupt file cannot poison the whole cross-tile basis.
    """
    all_records: list = []
    try:
        file_names = os.listdir(STORE_DIRECTORY)
    except Exception:
        return all_records
    for file_name in file_names:
        if not file_name.endswith(".json"):
            continue
        try:
            with open(os.path.join(STORE_DIRECTORY, file_name)) as record_file:
                records = json.load(record_file)
        except Exception:
            continue
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict):
                    all_records.append(record)
    return all_records


def record_build(lat: int, lon: int, features: dict, step_seconds: dict) -> None:
    """Append one finished tile build to that tile's history file.

    ``step_seconds`` maps a subset of :data:`STEP_KEYS` to measured wall
    seconds; only finite, non-negative values are kept.  The write is
    atomic (temp file then :func:`os.replace`) so a crash mid-write can
    never leave a half-written, unparseable history behind.  Being purely
    advisory data, any failure is swallowed rather than raised.
    """
    try:
        clean_steps = {}
        for step_key, seconds in (step_seconds or {}).items():
            if step_key not in STEP_KEYS:
                continue
            if not isinstance(seconds, (int, float)):
                continue
            if seconds < 0 or not math.isfinite(seconds):
                continue
            clean_steps[step_key] = round(float(seconds), 2)
        if not clean_steps:
            return
        records = _load_tile_records(lat, lon)
        records.append({
            "finished_at": time.time(),
            "features": dict(features or {}),
            "step_seconds": clean_steps,
        })
        records = records[-RECORDS_KEPT_PER_TILE:]
        os.makedirs(STORE_DIRECTORY, exist_ok=True)
        path = _tile_record_path(lat, lon)
        temporary_path = path + ".tmp"
        with open(temporary_path, "w") as record_file:
            json.dump(records, record_file, indent=1)
        os.replace(temporary_path, path)
    except Exception:
        pass


def _median(values):
    """Median of a sequence, or ``None`` when it is empty."""
    numbers = [value for value in values
               if isinstance(value, (int, float)) and math.isfinite(value)]
    if not numbers:
        return None
    return statistics.median(numbers)


def _record_step_seconds(record: dict, step_key: str):
    """Measured seconds of one step in a record, or ``None`` if absent."""
    seconds = (record.get("step_seconds") or {}).get(step_key)
    if isinstance(seconds, (int, float)) and math.isfinite(seconds) \
            and seconds >= 0:
        return float(seconds)
    return None


def _same_tile_compatible(record: dict, features: dict, step_key: str) -> bool:
    """Whether a past record is comparable to the planned build for a step.

    Only imagery is zoom-sensitive: a tile rebuilt at a different zoom
    level fetches and converts a completely different number of textures,
    so its imagery time is not a valid predictor.  Every other step is
    zoom-independent, so any record of the same tile is compatible.
    """
    if step_key != "imagery":
        return True
    record_zoom = (record.get("features") or {}).get("zoomlevel")
    planned_zoom = (features or {}).get("zoomlevel")
    return record_zoom == planned_zoom


def _fit_imagery_rates(records: list):
    """Least-squares (fetch_rate, convert_rate) for the imagery model.

    Fits ``seconds ≈ fetch_rate × textures_missing + convert_rate ×
    textures_total`` with no intercept (a zero-size tile costs nothing)
    via the 2×2 normal equations, then clamps both rates to be
    non-negative because negative fetch or convert costs are physically
    meaningless.  Returns ``None`` when the system is degenerate (too few
    points, or the two columns are collinear so the rates cannot be
    separated).
    """
    points = []
    for record in records:
        seconds = _record_step_seconds(record, "imagery")
        if seconds is None:
            continue
        record_features = record.get("features") or {}
        missing = record_features.get("textures_missing")
        total = record_features.get("textures_total")
        if not isinstance(missing, (int, float)) \
                or not isinstance(total, (int, float)):
            continue
        points.append((float(missing), float(total), seconds))
    if len(points) < 2:
        return None
    sum_mm = sum(m * m for m, _t, _s in points)
    sum_mt = sum(m * t for m, t, _s in points)
    sum_tt = sum(t * t for _m, t, _s in points)
    sum_ms = sum(m * s for m, _t, s in points)
    sum_ts = sum(t * s for _m, t, s in points)
    determinant = sum_mm * sum_tt - sum_mt * sum_mt
    if abs(determinant) < 1e-9:
        return None
    fetch_rate = (sum_ms * sum_tt - sum_ts * sum_mt) / determinant
    convert_rate = (sum_mm * sum_ts - sum_mt * sum_ms) / determinant
    return (max(0.0, fetch_rate), max(0.0, convert_rate))


def _global_median_imagery_rates(records: list):
    """Robust median (fetch_rate, convert_rate) across all imagery records.

    The fallback when no provider+zoom bucket is large enough to fit.  A
    fully cached record (``textures_missing == 0``) exposes the convert
    rate directly (``seconds / textures_total``); with the convert rate
    in hand, every other record's residual over its missing textures
    exposes the fetch rate.  Medians are used rather than a fit so a
    single outlier build cannot dominate this coarse global estimate.
    Returns ``None`` when there is not enough to learn either rate.
    """
    convert_samples = []
    fetch_candidates = []
    for record in records:
        seconds = _record_step_seconds(record, "imagery")
        if seconds is None:
            continue
        record_features = record.get("features") or {}
        missing = record_features.get("textures_missing")
        total = record_features.get("textures_total")
        if not isinstance(missing, (int, float)) \
                or not isinstance(total, (int, float)):
            continue
        missing = float(missing)
        total = float(total)
        if missing <= 0 and total > 0:
            convert_samples.append(seconds / total)
        elif missing > 0:
            fetch_candidates.append((missing, total, seconds))
    convert_rate = _median(convert_samples)
    if convert_rate is None:
        # No cached record to isolate the convert rate; fall back to a
        # single-variable fit that lumps all cost onto fetched textures.
        rates = _fit_imagery_rates(records)
        return rates
    fetch_samples = []
    for missing, total, seconds in fetch_candidates:
        residual = seconds - convert_rate * total
        fetch_samples.append(max(0.0, residual / missing))
    fetch_rate = _median(fetch_samples)
    if fetch_rate is None:
        fetch_rate = 0.0
    return (fetch_rate, convert_rate)


def _predict_imagery_cross_tile(features: dict, all_records: list):
    """Cross-tile imagery seconds from the linear texture-count model.

    Prefers rates fit within this build's own provider+zoom bucket once
    at least three records exist there (the tightest, most relevant
    sample), then global median rates, then ``None`` so the caller can
    apply the constant fallback.  Because the prediction is
    ``fetch_rate × missing + convert_rate × total``, a fully cached tile
    (missing zero) collapses to convert-only time — materially cheaper
    than a cold tile of the same size.
    """
    imagery_records = [r for r in all_records
                       if _record_step_seconds(r, "imagery") is not None]
    if not imagery_records:
        return None
    provider = (features or {}).get("provider")
    zoomlevel = (features or {}).get("zoomlevel")
    bucket = [r for r in imagery_records
              if (r.get("features") or {}).get("provider") == provider
              and (r.get("features") or {}).get("zoomlevel") == zoomlevel]
    rates = None
    if len(bucket) >= 3:
        rates = _fit_imagery_rates(bucket)
    if rates is None:
        rates = _global_median_imagery_rates(imagery_records)
    if rates is None:
        return None
    fetch_rate, convert_rate = rates
    missing = float((features or {}).get("textures_missing", 0) or 0)
    total = float((features or {}).get("textures_total", 0) or 0)
    seconds = fetch_rate * missing + convert_rate * total
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _learned_vector_overhead(same_tile_records: list, all_records: list):
    """Median residual vector seconds after removing airport build time.

    The vector step's time is airport build time (already predicted by
    the auto-patch model) plus a residual OSM/vector overhead.  Each
    past record exposes that residual as its recorded vector seconds
    minus its recorded ``autopatch_seconds`` (when that feature is
    present; otherwise the whole vector time is treated as overhead).
    Same-tile records are preferred; the cross-tile pool is the
    fallback.  The result is floored at :data:`MINIMUM_VECTOR_OVERHEAD_S`
    because the step never truly costs nothing.
    """
    for source_records in (same_tile_records, all_records):
        overhead_samples = []
        for record in source_records:
            vector_seconds = _record_step_seconds(record, "vector")
            if vector_seconds is None:
                continue
            autopatch_seconds = (record.get("features") or {}).get(
                "autopatch_seconds")
            if isinstance(autopatch_seconds, (int, float)) \
                    and math.isfinite(autopatch_seconds):
                overhead_samples.append(vector_seconds - float(autopatch_seconds))
            else:
                overhead_samples.append(vector_seconds)
        overhead = _median(overhead_samples)
        if overhead is not None:
            return max(MINIMUM_VECTOR_OVERHEAD_S, overhead)
    return None


def _predict_one_step(step_key: str, features: dict,
                      same_tile_records: list, all_records: list):
    """Predicted seconds for a single step, or ``None`` for no basis.

    Applies the shared prediction order — same-tile history, then a
    cross-tile model — leaving the constant fallback to the caller.  The
    two special-cased steps (vector leaning on the auto-patch model,
    imagery using the linear texture model) are handled here so
    :func:`predict_step_seconds` stays a thin loop.
    """
    compatible = [r for r in same_tile_records
                  if _same_tile_compatible(r, features, step_key)]

    if step_key == "vector":
        autopatch_prediction = (features or {}).get("autopatch_prediction_s")
        if isinstance(autopatch_prediction, (int, float)) \
                and math.isfinite(autopatch_prediction) \
                and autopatch_prediction > 0:
            overhead = _learned_vector_overhead(compatible, all_records)
            if overhead is None:
                overhead = MINIMUM_VECTOR_OVERHEAD_S
            return overhead + float(autopatch_prediction)

    # Source 1: this tile's own recent, compatible history for the step.
    same_tile_samples = [_record_step_seconds(r, step_key)
                         for r in compatible[-_SAME_TILE_WINDOW:]]
    same_tile_median = _median(
        [value for value in same_tile_samples if value is not None])
    if same_tile_median is not None:
        return same_tile_median

    # Source 2: cross-tile model.
    if step_key == "imagery":
        imagery_seconds = _predict_imagery_cross_tile(features, all_records)
        if imagery_seconds is not None:
            return imagery_seconds
    else:
        cross_tile_samples = [_record_step_seconds(r, step_key)
                              for r in all_records]
        cross_tile_median = _median(
            [value for value in cross_tile_samples if value is not None])
        if cross_tile_median is not None:
            return cross_tile_median

    return None


def predict_step_seconds(lat: int, lon: int, features: dict,
                         steps: list) -> dict:
    """Best-effort ``{step_key: seconds}`` for the planned steps.

    Never returns ``None`` and never raises: every requested step gets a
    value, falling back to :data:`DEFAULT_STEP_SECONDS` when neither the
    tile's own history nor any cross-tile model can supply one.  See the
    module docstring for the per-step prediction sources and the two
    special cases (vector composing with the auto-patch model, imagery
    fit linearly in the texture counts).
    """
    predictions = {}
    try:
        same_tile_records = _load_tile_records(lat, lon)
        all_records = _load_all_records()
    except Exception:
        same_tile_records = []
        all_records = []
    for step_key in (steps or ()):
        estimate = None
        try:
            estimate = _predict_one_step(
                step_key, features or {}, same_tile_records, all_records)
        except Exception:
            estimate = None
        if estimate is None or not math.isfinite(estimate) or estimate < 0:
            estimate = DEFAULT_STEP_SECONDS.get(step_key, 60.0)
        predictions[step_key] = float(estimate)
    return predictions

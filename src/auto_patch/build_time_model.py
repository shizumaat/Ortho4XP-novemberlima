"""Learned wall-clock estimates for full airport builds.

The progress window wants to answer "about how long will this airport
take?" BEFORE the build has run far enough for an elapsed-time
extrapolation to mean anything (user 2026-07-04: KDFW sat at
"About 0:06 remaining" for minutes because the early phases finish
fast and the naive extrapolation locked in a tiny total).

The answer here is a small persisted record of past builds:

* every finished full build records its airport's complexity features
  (apt.dat counts known seconds into the build), its per-phase wall
  times, and its total — one small JSON file per airport under
  ``~/.ortho4xp/auto_patch_build_times/``;
* a REBUILD of the same airport predicts from that airport's own
  recent history (the strongest predictor — users rebuild tiles
  constantly);
* a first-time airport predicts from every OTHER recorded airport via
  a per-complexity rate (least-squares seconds ≈ base + rate × score
  when several airports are recorded, a plain median rate otherwise) —
  "an airport with x route edges and y pavements lands in this
  ballpark".

Everything here is advisory and cosmetic: every function returns
``None`` (or silently does nothing) rather than raise, and nothing in
the build reads the predictions back — only the progress display does.
"""

import json
import math
import os
import time

STORE_DIRECTORY = os.path.join(
    os.path.expanduser("~"), ".ortho4xp", "auto_patch_build_times")

# Per-airport history kept (newest last).  Old records age out so a
# solver perf change stops haunting the estimate after a few rebuilds.
RECORDS_KEPT_PER_AIRPORT = 8

# A prediction below this is noise — no airport builds faster.
MINIMUM_PREDICTION_S = 10.0


def complexity_features(airport) -> dict:
    """Size features for one parsed apt.dat ``Airport`` block.

    Known immediately after the load phase (~seconds into a build) and
    strongly correlated with total build time: the route-network
    fragment count drives the downstream node counts roughly linearly
    (CYUL vs SPJC, 2026-07-03 perf round).
    """
    return {
        "runways": len(getattr(airport, "runways", ()) or ()),
        "pavements": len(getattr(airport, "pavements", ()) or ()),
        "taxi_nodes": len(getattr(airport, "taxi_nodes", {}) or {}),
        "taxi_edges": len(getattr(airport, "taxi_edges", ()) or ()),
        "truck_edges": len(getattr(airport, "truck_edges", ()) or ()),
    }


def complexity_score(features: dict) -> float:
    """Collapse the feature dict to one scalar build-size score.

    Route edges dominate (they set the spine/solver node counts);
    pavement polygons add geometry-phase work; runways carry a fixed
    per-runway profile cost.  The weights are coarse — only the RATIO
    between airports matters, and per-airport history takes over as
    soon as an airport has built once.
    """
    edges = (features.get("taxi_edges", 0) or 0) \
        + (features.get("truck_edges", 0) or 0)
    score = (float(edges)
             + float(features.get("pavements", 0) or 0)
             + 25.0 * float(features.get("runways", 0) or 0))
    return max(1.0, score)


def _airport_record_path(icao: str) -> str:
    safe_name = "".join(
        ch for ch in str(icao) if ch.isalnum() or ch in "-_") or "UNKNOWN"
    return os.path.join(STORE_DIRECTORY, safe_name + ".json")


def _load_airport_records(icao: str) -> list:
    """This airport's build records, oldest first.  [] on any problem."""
    try:
        with open(_airport_record_path(icao)) as record_file:
            records = json.load(record_file)
        return records if isinstance(records, list) else []
    except Exception:
        return []


def record_build(icao: str, features: dict, phase_seconds: dict,
                 total_seconds: float) -> None:
    """Persist one finished full build.  Never raises (cosmetic data)."""
    try:
        if not total_seconds or total_seconds <= 0 \
                or not math.isfinite(total_seconds):
            return
        records = _load_airport_records(icao)
        records.append({
            "finished_at": time.time(),
            "features": dict(features or {}),
            "phase_seconds": {str(label): round(float(seconds), 2)
                              for label, seconds in
                              (phase_seconds or {}).items()},
            "total_seconds": round(float(total_seconds), 2),
        })
        records = records[-RECORDS_KEPT_PER_AIRPORT:]
        os.makedirs(STORE_DIRECTORY, exist_ok=True)
        path = _airport_record_path(icao)
        temporary_path = path + ".tmp"
        with open(temporary_path, "w") as record_file:
            json.dump(records, record_file, indent=1)
        os.replace(temporary_path, path)
    except Exception:
        pass


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def _latest_record_per_airport() -> list:
    """Newest record of every airport in the store (any order)."""
    latest = []
    try:
        file_names = os.listdir(STORE_DIRECTORY)
    except Exception:
        return latest
    for file_name in file_names:
        if not file_name.endswith(".json"):
            continue
        try:
            with open(os.path.join(STORE_DIRECTORY, file_name)) as record_file:
                records = json.load(record_file)
            if isinstance(records, list) and records:
                latest.append(records[-1])
        except Exception:
            continue
    return latest


def predict_total_seconds(icao: str, features: dict):
    """Best prior for this airport's total build time, or ``None``.

    Same airport built before → median of its recent totals.  Otherwise
    a cross-airport per-complexity rate.  ``None`` when the store is
    empty (the display then falls back to pure elapsed extrapolation).
    """
    try:
        own_records = _load_airport_records(icao)
        own_totals = [r.get("total_seconds") for r in own_records[-3:]
                      if isinstance(r.get("total_seconds"), (int, float))]
        if own_totals:
            return max(MINIMUM_PREDICTION_S, _median(own_totals))

        score = complexity_score(features or {})
        points = []
        for record in _latest_record_per_airport():
            total = record.get("total_seconds")
            if not isinstance(total, (int, float)) or total <= 0:
                continue
            points.append(
                (complexity_score(record.get("features") or {}), total))
        if not points:
            return None
        if len(points) >= 2:
            # Least-squares seconds ≈ base + rate × score.  The intercept
            # absorbs the fixed per-build overhead (DEM load, imports).
            count = float(len(points))
            mean_x = sum(x for x, _ in points) / count
            mean_y = sum(y for _, y in points) / count
            variance_x = sum((x - mean_x) ** 2 for x, _ in points)
            if variance_x > 1e-9:
                rate = sum((x - mean_x) * (y - mean_y)
                           for x, y in points) / variance_x
                base = mean_y - rate * mean_x
                if rate > 0 and base >= 0:
                    return max(MINIMUM_PREDICTION_S, base + rate * score)
            # Degenerate fit (identical scores / inverted slope from a
            # small sample) → fall through to the plain rate.
        rate = _median([total / max(1.0, point_score)
                        for point_score, total in points])
        return max(MINIMUM_PREDICTION_S, rate * score)
    except Exception:
        return None


def predict_phase_seconds(icao: str, features: dict):
    """Per-phase-label predicted seconds, or ``None`` without data.

    Same-airport history → median per label over recent records;
    cross-airport → median per-complexity rate per label × this
    airport's score.  Feeds the mid-build refinement: once a phase
    finishes, its ACTUAL time replaces the prediction and the
    remaining phases are rescaled (progress.BuildProgress).
    """
    try:
        own_records = _load_airport_records(icao)
        source_records = own_records[-3:] if own_records else None
        if source_records:
            def phase_estimate(label, samples):
                return _median(samples)
        else:
            source_records = _latest_record_per_airport()
            score = complexity_score(features or {})

            def phase_estimate(label, samples):
                rate = _median(samples)
                return None if rate is None else rate * score
        samples_by_label: dict = {}
        for record in source_records or ():
            record_score = complexity_score(record.get("features") or {})
            for label, seconds in (record.get("phase_seconds") or {}).items():
                if not isinstance(seconds, (int, float)) or seconds < 0:
                    continue
                value = (seconds if own_records
                         else seconds / max(1.0, record_score))
                samples_by_label.setdefault(label, []).append(value)
        if not samples_by_label:
            return None
        predictions = {}
        for label, samples in samples_by_label.items():
            estimate = phase_estimate(label, samples)
            if estimate is not None:
                predictions[label] = float(estimate)
        return predictions or None
    except Exception:
        return None

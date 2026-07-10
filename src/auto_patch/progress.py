"""User-facing build progress for the auto_patch pavement builder.

A single airport build (:func:`pipeline.build_airport_pavement`) walks
through a handful of distinct components — load runways, assemble
pavement, build taxiways/junctions, solve elevations, emit terrain
features.  Each one can take a noticeable slice of the ~60-90 s build,
so a user watching a tile generate in the Ortho4XP window otherwise
sees a long quiet gap with no idea what is happening.

:class:`BuildProgress` prints a step-counted banner at the start of
each component::

    Auto-patch: CYXY [3/6] Building taxiways & terminals

so the window shows which component is running, how many steps the
build has, and how many remain.  Lines go through ``UI.lvprint(0, ...)``
so they are visible at the auto-patch log verbosity
(``config.LOG_VERBOSITY``, normally 0) and are recorded in
``Ortho4XP.log`` alongside the rest of the build chatter.

The reporter is output-only: it never touches geometry or elevations,
so the emitted OSM patch is byte-identical whether or not progress is
enabled.  ``config.BUILD_PROGRESS`` (env ``O4_BUILD_PROGRESS``, default
on) silences it without removing the call sites.
"""

import time as _time

import O4_UI_Utils as UI

from . import config


# When an airport build runs in a per-airport ProcessPool worker
# (driver._run_build_tasks), its UI output can't reach the main Ortho4XP window.
# The pool initializer sets this to a shared queue; ``step`` then PUSHES each
# phase transition onto it and the MAIN process drains + prints them (labelled by
# ICAO, so a watcher sees every airport advancing live).  None = normal in-process
# logging (serial builds / the test suite).
_worker_queue = None


def set_worker_queue(q) -> None:
    """Route phase-progress events to ``q`` (a shared queue) instead of the local
    UI — called once per worker by the pool initializer."""
    global _worker_queue
    _worker_queue = q


class BuildProgress:
    """Step-counted, TIME-WEIGHTED progress reporter for one airport build.

    Construct with the airport code and the ordered list of phase
    labels the build will run, then call :meth:`step` at the start of
    each phase.  The label list fixes the denominator up front so the
    very first banner can already announce the total (``[1/6]``), and
    ``compute_elevations=False`` builds (tests / tools, which skip the
    elevation + feature phases) get the correct smaller total.

    The GUI progress BAR is driven by per-phase WEIGHTS (measured
    typical time shares — the elevation solve is ~3/4 of a build, so
    six equal steps made the bar sprint to 4/6 and stall), reported as
    ``(percent, 100)``; the console banner keeps the step count.
    :meth:`substep` reports fractional progress WITHIN the current
    phase (the solve calls it at its internal boundaries), moving the
    bar smoothly through the long phase.

    TIME ESTIMATE (user 2026-07-04): once the apt.dat is parsed the
    pipeline hands the reporter a complexity-based prediction from
    past recorded builds (:meth:`set_time_model`,
    ``build_time_model``).  Every progress event then carries the
    current best TOTAL-time estimate: predicted phase times, with
    finished phases replaced by their actual times and the remaining
    phases rescaled by how far the build is running ahead of/behind
    the prediction ("refine as we go").  The GUI blends this with its
    own elapsed-time extrapolation.

    A reporter never raises out of :meth:`step`: progress is cosmetic,
    so a build must not fail because a banner could not be printed.
    """

    def __init__(self, icao, labels, weights=None, *, enabled=True):
        self.icao = icao
        self.labels = list(labels)
        self.total = len(self.labels)
        w = list(weights) if weights else [1.0] * self.total
        s = sum(w) or 1.0
        self.weights = [v / s for v in w]
        self.enabled = enabled and config.BUILD_PROGRESS
        self._done = 0
        self._pct = 0          # last reported percent (monotonic guard)
        self._started_at = _time.time()
        self._phase_started_at = None   # wall time the current phase began
        self._phase_seconds = {}        # finished phases: label → seconds
        self._predicted_total_s = None      # from build_time_model
        self._predicted_phase_s = None      # {label: seconds} or None
        self._estimate_total_s = None       # current best, sent with events

    def set_time_model(self, predicted_total_s, predicted_phase_s=None):
        """Attach the complexity-based prediction (both may be ``None``).

        When the prediction covers this build's phases it also REWEIGHTS
        THE BAR: the static PHASE_WEIGHTS split drifts as the pipeline
        evolves (2026-07-10: the elevation solve fell from ~70% to ~7%
        of a CYXY build while the emit phase grew to ~90% — the bar
        sprinted to the last phase in seconds and stalled there), while
        recorded per-phase times from past builds are ground truth for
        this airport's bar.  A small floor keeps every phase visible.
        """
        self._predicted_total_s = predicted_total_s
        self._predicted_phase_s = dict(predicted_phase_s or {}) or None
        if self._predicted_phase_s:
            w = [max(0.0, self._predicted_phase_s.get(label, 0.0))
                 for label in self.labels]
            s = sum(w)
            if s > 0:
                floor = 0.005
                w = [max(v / s, floor) for v in w]
                s = sum(w)
                self.weights = [v / s for v in w]
        self._refresh_estimate(0.0)

    def phase_seconds(self):
        """Measured per-phase wall times, including the phase still
        running (so the recorder called right before the build returns
        captures the final phase too)."""
        result = dict(self._phase_seconds)
        if self._done and self._phase_started_at is not None:
            label = self.labels[self._done - 1]
            if label not in result:
                result[label] = _time.time() - self._phase_started_at
        return result

    def _refresh_estimate(self, frac_in_phase):
        """Recompute the best current TOTAL-time estimate.

        Predicted per-phase times, with the finished prefix replaced by
        its ACTUAL elapsed time and the remaining phases rescaled by the
        observed ahead/behind ratio (clamped — one anomalous phase must
        not blow up the whole estimate).  Falls back to the flat
        predicted total, then to ``None`` (GUI extrapolates alone).

        The ahead/behind RATIO is computed from COMPLETED phases only
        (their actual wall time versus their prediction is ground
        truth).  Substep fractions are display hints with hard-coded
        positions — measured 2026-07-10: feeding them into the ratio
        let a substep that fires seconds into a long phase claim the
        phase mostly done, halving the estimate ("About 0:00
        remaining" for the final two minutes of a CYXY build).  The
        fraction still SPLITS the current phase's prediction between
        done and rest (arithmetic), but never inflates the evidence
        the ratio is judged on.
        """
        try:
            elapsed = _time.time() - self._started_at
            predicted = self._predicted_phase_s
            if predicted:
                done_labels = self.labels[:max(0, self._done - 1)]
                current_label = (self.labels[self._done - 1]
                                 if self._done else None)
                predicted_completed = sum(
                    predicted.get(label, 0.0) for label in done_labels)
                predicted_rest = sum(
                    predicted.get(label, 0.0)
                    for label in self.labels[max(0, self._done - 1):])
                if current_label is not None:
                    predicted_rest -= (frac_in_phase
                                       * predicted.get(current_label, 0.0))
                predicted_rest = max(0.0, predicted_rest)
                predicted_total = sum(
                    predicted.get(label, 0.0) for label in self.labels)
                elapsed_completed = (
                    (self._phase_started_at - self._started_at)
                    if self._phase_started_at is not None else elapsed)
                if predicted_completed > 3.0:
                    ratio = min(4.0, max(
                        0.5, elapsed_completed / predicted_completed))
                    # Trust the ahead/behind ratio in proportion to how
                    # much of the predicted build has actually run — one
                    # quick early phase must not halve the estimate.
                    confidence = min(
                        1.0,
                        predicted_completed / max(1.0, 0.3 * predicted_total))
                    ratio = 1.0 + (ratio - 1.0) * confidence
                else:
                    ratio = 1.0
                # A running phase never counts as finished: while any
                # phase is in progress the estimate keeps at least a
                # sliver of the predicted total ahead of the clock, so
                # the display cannot reach zero before the build does.
                floor = elapsed + (0.02 * predicted_total
                                   if self._done < self.total
                                   or frac_in_phase < 1.0 else 0.0)
                self._estimate_total_s = max(
                    elapsed + ratio * predicted_rest, floor)
            elif self._predicted_total_s:
                self._estimate_total_s = max(
                    self._predicted_total_s, elapsed)
        except Exception:
            pass

    def _report(self, pct, label, *, console=None):
        """Send ``pct`` (0-100) + ``label`` to the GUI bar (and optionally a
        console banner) through whichever channel this process uses."""
        pct = max(self._pct, min(100, int(round(pct))))
        self._pct = pct
        q = _worker_queue
        if q is not None:
            # In a pool worker: hand the event to the main process to print.
            try:
                q.put((self.icao, pct, 100, label, self._estimate_total_s))
            except Exception:
                pass
            return
        try:
            if console:
                UI.lvprint(0, console)
            # Serial builds run in the main process, so the phase event can go
            # straight to the second progress window (parallel builds route it
            # through the pool queue + driver._drain_progress instead).
            UI.auto_patch_progress(self.icao, pct, 100, label,
                                   eta_total_s=self._estimate_total_s)
        except Exception:
            # Never let a logging hiccup abort an airport build.
            pass

    def step(self):
        """Advance to the next phase and print its banner.

        Extra calls past the registered total are ignored (defensive —
        the call sites are fixed, but a refactor that adds one shouldn't
        print ``[7/6]``).
        """
        if self._done >= self.total:
            return
        label = self.labels[self._done]
        now = _time.time()
        if self._done and self._phase_started_at is not None:
            self._phase_seconds[self.labels[self._done - 1]] = (
                now - self._phase_started_at)
        self._phase_started_at = now
        self._done += 1
        if not self.enabled:
            return
        self._refresh_estimate(0.0)
        pct = 100.0 * sum(self.weights[: self._done - 1])
        self._report(
            pct, label,
            console="   Auto-patch: {} [{}/{}] {}".format(
                self.icao, self._done, self.total, label))

    def substep(self, frac, detail=None):
        """Report fractional progress WITHIN the current phase.

        ``frac`` in [0, 1] — how far through the current phase; the bar
        moves to ``done-weights + frac·current-weight``.  GUI-only (no
        console banner; sub-phases would spam the log).  ``detail``
        optionally replaces the bar's label line.  Monotonic and
        clamped, so a mis-ordered call can never move the bar backward.
        """
        if not self.enabled or self._done == 0:
            return
        frac = max(0.0, min(1.0, float(frac)))
        self._refresh_estimate(frac)
        base = sum(self.weights[: self._done - 1])
        pct = 100.0 * (base + frac * self.weights[self._done - 1])
        self._report(pct, detail or self.labels[self._done - 1])


# Ordered phase labels for a full build (``compute_elevations=True``).
# ``build_airport_pavement`` calls :meth:`BuildProgress.step` once per
# entry, in this order.  A geometry-only build uses just the first
# ``GEOMETRY_PHASES`` of these.
GEOMETRY_PHASES = 4
PHASE_LABELS = [
    "Loading apt.dat & runway geometry",
    "Assembling pavement & runway shoulders",
    "Building taxiways & terminals",
    "Building taxi rects, junctions & service roads",
    "Solving elevations (FAA grade compliance)",
    "Emitting terrain features & finalizing",
]
# Typical share of build TIME per phase (measured CYXY 2026-07-10:
# ~175 s total — geometry phases ~3 s, elevation solve ~12 s, emit
# ~160 s; the one-solve/node-diet work shrank the solve while the
# terrain-feature emitters — bands, gap fill, weld, decimation — grew
# the emit phase into the dominant cost).  This static split is only
# the FALLBACK for an airport with no recorded builds:
# ``set_time_model`` reweights the bar from recorded per-phase times
# (``build_time_model``) as soon as they exist.  Re-measure here if
# the pipeline's split drifts again (previous split 2026-07-03:
# [2, 5, 7, 5, 70, 11] — solve-dominated).
PHASE_WEIGHTS = [1, 1, 1, 1, 7, 89]


def for_build(icao, *, compute_elevations):
    """Return a :class:`BuildProgress` for one ``build_airport_pavement``.

    The elevation solve and the terrain-feature/finalize emit only run
    when ``compute_elevations`` is set, so a geometry-only build is
    reported as the first :data:`GEOMETRY_PHASES` steps and a full
    build as all of :data:`PHASE_LABELS`.  The reporter is also
    registered as the process-wide CURRENT one so deep phases (the
    elevation solve) can publish :meth:`BuildProgress.substep` without
    plumbing the object through every call layer.
    """
    labels = (PHASE_LABELS if compute_elevations
              else PHASE_LABELS[:GEOMETRY_PHASES])
    weights = (PHASE_WEIGHTS if compute_elevations
               else PHASE_WEIGHTS[:GEOMETRY_PHASES])
    bp = BuildProgress(icao, labels, weights)
    global _current
    _current = bp
    return bp


# Process-wide current reporter (one airport builds at a time per process;
# pool workers each hold their own module state).  ``substep`` is the
# fire-and-forget hook for deep build phases.
_current = None


def substep(frac, detail=None):
    """Report fractional progress within the CURRENT phase of the current
    build, if any — safe to call from anywhere (no-op when no build is
    running; never raises)."""
    bp = _current
    if bp is None:
        return
    try:
        bp.substep(frac, detail)
    except Exception:
        pass

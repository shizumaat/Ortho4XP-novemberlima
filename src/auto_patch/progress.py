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

    def _report(self, pct, label, *, console=None):
        """Send ``pct`` (0-100) + ``label`` to the GUI bar (and optionally a
        console banner) through whichever channel this process uses."""
        pct = max(self._pct, min(100, int(round(pct))))
        self._pct = pct
        q = _worker_queue
        if q is not None:
            # In a pool worker: hand the event to the main process to print.
            try:
                q.put((self.icao, pct, 100, label))
            except Exception:
                pass
            return
        try:
            if console:
                UI.lvprint(0, console)
            # Serial builds run in the main process, so the phase event can go
            # straight to the second progress window (parallel builds route it
            # through the pool queue + driver._drain_progress instead).
            UI.auto_patch_progress(self.icao, pct, 100, label)
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
        self._done += 1
        if not self.enabled:
            return
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
# Typical share of build TIME per phase (measured SPJC/CYXY, 2026-07-03:
# ~105 s total, elevation solve ~75-80 s).  Drives the GUI bar; only the
# RATIOS matter, and airports of any size follow roughly this split (the
# solve dominates because graph passes scale with the same node count
# the geometry passes produce).  Re-measure with scratchpad
# ``timed_build.py`` if the split drifts.
PHASE_WEIGHTS = [2, 5, 7, 5, 70, 11]


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

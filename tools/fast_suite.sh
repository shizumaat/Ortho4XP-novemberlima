#!/bin/sh
# FAST LANE for inner-loop development (user 2026-07-05): every full-suite
# cycle pays for building EVERY fixture airport (SPJC ~60s, HECA ~140s,
# MMOX/HEAZ on top — ~3.5 min wall even with xdist).  This lane runs the
# same tests but only the CHEAP airports (CYXY ~22s, SPLP ~8s) plus every
# non-build unit test — seconds-scale feedback for solver/law iterations.
#
# The FULL suite stays the merge gate; the fast lane is a development
# convenience only.  Anything airport-specific to SPJC/HECA/HEAZ/MMOX is
# invisible here by construction — run the full suite before committing.
#
# Usage: tools/fast_suite.sh [extra pytest args]
cd "$(dirname "$0")/.." || exit 1
exec venv/bin/python -m pytest tests/ -q \
    -k "not SPJC and not HECA and not HEAZ and not MMOX and not KCLT and not CYUL" \
    "$@"

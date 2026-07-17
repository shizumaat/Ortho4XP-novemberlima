# Activate Python virtual environment
source venv/bin/activate

# Reproducible builds: adjacent-ground band survival (and .osm node
# emission order) depend on Python's hash-ordered iteration — unpinned,
# the same tile build varies run to run (measured at KBNA 2026-07-16:
# 651-719 bands, junction coverage wandering).  The seed must be set
# BEFORE the interpreter starts; exporting it here covers the GUI and
# every in-process build it runs.
export PYTHONHASHSEED=${PYTHONHASHSEED:-0}

# Start Ortho4XP
venv/bin/python Ortho4XP_Qt.py
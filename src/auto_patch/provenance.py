"""Patch provenance — the single source of truth for "how was this patch built".

After a bake nobody should have to run forensics to learn whether a patch was
produced by the current code, with which gate configuration, and on which
elevation surface.  This module assembles that provenance and renders it three
ways from ONE computed record:

1.  As tags on the ``<osm>`` root element that ``PavementLayout.to_osm`` writes
    (``provenance_tags``).  They ride on the root exactly like the existing
    ``o4_apt_dat`` / ``o4_apt_dat_mtime`` stamp, so they perturb no geometry:
    the mesher's patch reader (``O4_Vector_Map.include_patches`` via
    ``O4_OSM_Utils.OSM_layer``) and the chain-divergence audit both parse only
    ``<node>`` / ``<way>`` elements and never look at root attributes.

2.  As a one-line human summary logged at default verbosity, one per airport,
    at patch completion (``format_log_line``).  The no-inset case reads as a
    warning — that is the silent-raw-DEM incident this feature exists to catch.

3.  Read back and printed by ``tools/patch_provenance.py`` (via the ``parse_*``
    helpers), which is a seconds-fast, no-build reader for humans and CI.

Three provenance facets are captured:

* **Source tree** — the git commit sha of the tree at build time and a
  dirty-tree flag (``git_provenance``).  Gracefully absent outside a checkout;
  provenance never crashes a build.
* **Gate configuration** — every ``O4_`` gate defined in :mod:`auto_patch.config`
  is enumerated by introspecting that module's source (``gate_provenance``), so
  the list cannot rot as gates are added or removed.  The record names which
  boolean gates are ON and, separately, every gate whose live value differs
  from its in-source default (the actionable "someone flipped a gate" signal).
* **Elevation provenance** — which airport-elevation insets actually baked into
  the DEM this patch was graded on, read from the inset provenance sidecars the
  ``O4_Airport_Elevation_Insets`` cache writes (provider, source_ids,
  fetch_date).  When no inset baked, the record says so LOUDLY.

The elevation facet is honest about the standalone-vs-production split: the
inset list is stamped onto the DEM object by ``bake_airport_insets_into_alt_dem``
at the moment it bakes, and read back here from that same object — so a
standalone build (which loads the raw base tile and never bakes) correctly
reports RAW even when an inset is sitting in the cache.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import urllib.parse


# Master gate for the whole feature.  ON by default (provenance should be on
# for every production build); set ``O4_PATCH_PROVENANCE=0`` to suppress the
# stamp + log line (used for the byte/audit A/B comparison).
def provenance_enabled() -> bool:
    """True when patch provenance stamping + logging is active (default ON)."""
    return os.environ.get("O4_PATCH_PROVENANCE", "1") == "1"


# ──────────────────────────────────────────────────────────────────────────────
# Facet 1 — source tree (git)
# ──────────────────────────────────────────────────────────────────────────────
def git_provenance(cwd: str | None = None) -> dict:
    """Return ``{"sha": str | None, "dirty": bool | None}`` for the source tree.

    ``sha`` is the short commit hash of ``HEAD``; ``dirty`` is True when the
    working tree has uncommitted changes (tracked or untracked).  Both are
    ``None`` outside a git checkout or when git is unavailable — provenance must
    never crash a build, so every failure mode is swallowed to a graceful
    absent value.
    """
    if cwd is None:
        # The auto_patch package lives inside the source checkout; resolve git
        # relative to it, not the process cwd (a build may run from anywhere).
        cwd = os.path.dirname(os.path.abspath(__file__))
    sha: str | None = None
    dirty: bool | None = None
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip() or None
    except Exception:
        return {"sha": None, "dirty": None}
    if sha is None:
        # Not a checkout (or detached with no commit): report absent, and do
        # not claim a dirty state we cannot determine.
        return {"sha": None, "dirty": None}
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = bool(status.stdout.strip())
    except Exception:
        dirty = None
    return {"sha": sha, "dirty": dirty}


# ──────────────────────────────────────────────────────────────────────────────
# Facet 2 — gate configuration (config.py introspection)
# ──────────────────────────────────────────────────────────────────────────────
# Matches ``environ.get("O4_NAME", "default")`` regardless of the os alias
# (``_os`` / ``_os_early``) or an enclosing ``int(...)`` / ``float(...)`` cast.
# Only two-argument calls with a STRING default are captured; a bare
# ``environ.get("O4_X")`` (default None) is intentionally skipped.
_ENV_GATE_RE = re.compile(
    r"""environ\.get\(\s*["'](O4_[A-Za-z0-9_]+)["']\s*,\s*["']([^"']*)["']"""
)


def _config_source_path() -> str | None:
    """Absolute path to the ``auto_patch.config`` source file, or None."""
    try:
        from . import config as _config

        return os.path.abspath(_config.__file__)
    except Exception:
        return None


def introspect_config_gates(source_path: str | None = None) -> dict:
    """Enumerate every ``O4_`` env gate declared in ``config.py``.

    Reads the config module's SOURCE (not a hand-maintained list) so the gate
    inventory tracks the code automatically.  Returns
    ``{name: {"default": str, "value": str, "is_boolean": bool}}`` where
    ``value`` is the live ``os.environ`` value falling back to the in-source
    default, and ``is_boolean`` marks gates whose default is ``"0"`` / ``"1"``.
    Returns an empty dict when the source cannot be read.
    """
    if source_path is None:
        source_path = _config_source_path()
    if not source_path or not os.path.isfile(source_path):
        return {}
    try:
        with open(source_path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {}
    gates: dict = {}
    for match in _ENV_GATE_RE.finditer(text):
        name = match.group(1)
        default = match.group(2)
        # A gate can be referenced more than once; keep the first default seen
        # (they agree in practice) but never overwrite with a later duplicate.
        if name in gates:
            continue
        value = os.environ.get(name, default)
        gates[name] = {
            "default": default,
            "value": value,
            "is_boolean": default in ("0", "1"),
        }
    return gates


def gate_provenance(source_path: str | None = None) -> dict:
    """Compute the active gate configuration from config introspection.

    Returns ``{"on": [names], "nondefault": [(name, value)], "total": int}``:

    * ``on`` — boolean gates whose live value is ``"1"`` (the active feature
      set), sorted.
    * ``nondefault`` — every gate (boolean or not) whose live value differs
      from its in-source default, as ``(name, value)`` pairs, sorted.  This is
      the actionable drift signal: a build that silently ran with a gate
      flipped shows here.
    * ``total`` — how many gates were enumerated (0 signals introspection
      failed, so a reader can distinguish "no gates on" from "could not read").
    """
    gates = introspect_config_gates(source_path)
    on = sorted(
        name
        for name, info in gates.items()
        if info["is_boolean"] and info["value"] == "1"
    )
    nondefault = sorted(
        (name, info["value"])
        for name, info in gates.items()
        if info["value"] != info["default"]
    )
    return {"on": on, "nondefault": nondefault, "total": len(gates)}


# ──────────────────────────────────────────────────────────────────────────────
# Facet 3 — elevation / inset provenance
# ──────────────────────────────────────────────────────────────────────────────
# Attribute name under which ``bake_airport_insets_into_alt_dem`` records the
# insets it baked onto the DEM object.  A list of per-inset dicts (possibly
# empty); ABSENT entirely when no bake step ran on this DEM (the standalone
# raw-load path), which reads as RAW.
DEM_INSET_PROVENANCE_ATTR = "airport_inset_provenance"


def dem_provenance_from_dem(dem_obj, icao: str | None = None) -> dict:
    """Read elevation provenance off the DEM object the solve graded against.

    Returns ``{"insets": [ {provider, source_ids, fetch_date, ...}, ... ],
    "raw": bool}``.  ``raw`` is True when no inset baked into this DEM — either
    the bake step never ran (standalone raw-load path: the attribute is absent)
    or it ran and baked nothing (empty list).  Either way the patch was graded
    on the base DEM, which the log/stamp must announce loudly.

    A production ``tile.dem`` is shared by every airport on the tile and carries
    all their baked insets; ``icao`` filters the record to the insets fetched
    for THIS airport (matched on the ICAO the bake parsed from the cache file
    name).  Entries with no ICAO field, or when ``icao`` is None, are kept.
    """
    insets = getattr(dem_obj, DEM_INSET_PROVENANCE_ATTR, None)
    if not insets:
        return {"insets": [], "raw": True}
    normalised = []
    for entry in insets:
        if not isinstance(entry, dict):
            continue
        if icao is not None and entry.get("icao"):
            if str(entry["icao"]).upper() != str(icao).upper():
                continue
        normalised.append(entry)
    return {"insets": normalised, "raw": len(normalised) == 0}


def _inset_label(entry: dict) -> str:
    """Render one baked inset as ``PROVIDER(source_id[,source_id])``."""
    provider = entry.get("provider") or "?"
    source_ids = entry.get("source_ids") or []
    if source_ids:
        return f"{provider}({','.join(str(s) for s in source_ids)})"
    return str(provider)


def dem_label(dem_meta: dict) -> str:
    """Human ``dem=`` value: ``base+PROVIDER(source)`` or the loud RAW form."""
    if not dem_meta or dem_meta.get("raw", True):
        return "base RAW (no inset baked)"
    labels = "+".join(_inset_label(e) for e in dem_meta.get("insets", []))
    return "base+" + labels


# ──────────────────────────────────────────────────────────────────────────────
# Assembly + tag rendering
# ──────────────────────────────────────────────────────────────────────────────
def assemble_provenance(icao: str, dem_meta: dict | None) -> dict:
    """Build the full provenance record for one airport patch.

    ``dem_meta`` is the ``dem_provenance_from_dem`` result (or None → RAW).
    """
    if dem_meta is None:
        dem_meta = {"insets": [], "raw": True}
    return {
        "icao": icao,
        "git": git_provenance(),
        "gates": gate_provenance(),
        "dem": dem_meta,
        "built": datetime.datetime.now().replace(microsecond=0).isoformat(),
    }


def _quote(value: str) -> str:
    """Percent-encode a tag value so it never contains a quote/space/newline."""
    return urllib.parse.quote(str(value), safe="")


def provenance_tags(prov: dict) -> dict:
    """Render the provenance record as ``o4_provenance_*`` root-attribute tags.

    Values are percent-encoded so the attribute string is always quote-safe,
    mirroring the ``o4_apt_dat`` convention.  Keys are stable so the reader can
    parse them back.  Absent facets are stamped explicitly (e.g. ``sha=absent``)
    so the reader can tell "no git" from "old unstamped patch".
    """
    git = prov.get("git") or {}
    gates = prov.get("gates") or {}
    dem = prov.get("dem") or {}
    tags: dict = {}
    tags["o4_provenance_sha"] = _quote(git.get("sha") or "absent")
    dirty = git.get("dirty")
    tags["o4_provenance_dirty"] = (
        "unknown" if dirty is None else ("true" if dirty else "false")
    )
    tags["o4_provenance_gates_on"] = _quote(",".join(gates.get("on", [])))
    tags["o4_provenance_gates_nondefault"] = _quote(
        ",".join(f"{n}={v}" for n, v in gates.get("nondefault", []))
    )
    tags["o4_provenance_gates_total"] = str(gates.get("total", 0))
    tags["o4_provenance_dem_raw"] = "true" if dem.get("raw", True) else "false"
    tags["o4_provenance_dem"] = _quote(dem_label(dem))
    tags["o4_provenance_built"] = _quote(prov.get("built") or "")
    tags["o4_provenance_icao"] = _quote(prov.get("icao") or "")
    return tags


def format_log_line(prov: dict) -> str:
    """One-line human summary logged per airport at patch completion.

    Compact by design: the full gate lists live in the tags/reader.  The line
    shows the sha (with a ``*`` dirty marker), the count of ON gates plus any
    non-default deviations (the actionable drift), and the DEM label.  The
    no-inset case is rendered as a WARNING so it stands out in the build log.
    """
    icao = prov.get("icao") or "????"
    git = prov.get("git") or {}
    gates = prov.get("gates") or {}
    dem = prov.get("dem") or {}
    sha = git.get("sha") or "absent"
    if git.get("dirty"):
        sha += "*"  # dirty-tree marker
    on_count = len(gates.get("on", []))
    nondefault = gates.get("nondefault", [])
    if nondefault:
        drift = ",".join(f"{n}={v}" for n, v in nondefault)
        gate_str = f"{on_count}on nondefault=[{drift}]"
    else:
        gate_str = f"{on_count}on"
    dem_str = dem_label(dem)
    line = f"  [provenance] {icao} patch: sha={sha} gates={gate_str} dem={dem_str}"
    if dem.get("raw", True):
        line += "  ← WARNING: graded on base DEM, no airport-elevation inset"
    return line


# ──────────────────────────────────────────────────────────────────────────────
# Reader side — parse the stamp back out of a written patch
# ──────────────────────────────────────────────────────────────────────────────
_ROOT_ATTR_RE = re.compile(r"o4_provenance_([a-z_]+)='([^']*)'")


def parse_patch_provenance(path: str) -> dict | None:
    """Read the provenance stamp back from a ``.patch.osm`` file's root.

    Returns a dict of the decoded facets, or ``None`` when the file is missing,
    unreadable, or carries no provenance stamp (an old/unstamped patch).  Only
    the first line or two are read — the ``<osm>`` root — so this is fast on any
    patch size.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            line = handle.readline()
            if "<osm " not in line:
                line = handle.readline()
    except OSError:
        return None
    if "<osm " not in line:
        return None
    raw: dict = {}
    for match in _ROOT_ATTR_RE.finditer(line):
        raw[match.group(1)] = match.group(2)
    if not raw:
        return None
    decoded: dict = {}
    decoded["sha"] = urllib.parse.unquote(raw.get("sha", ""))
    decoded["dirty"] = raw.get("dirty", "unknown")
    decoded["gates_on"] = [
        g
        for g in urllib.parse.unquote(raw.get("gates_on", "")).split(",")
        if g
    ]
    decoded["gates_nondefault"] = [
        g
        for g in urllib.parse.unquote(raw.get("gates_nondefault", "")).split(",")
        if g
    ]
    decoded["gates_total"] = raw.get("gates_total", "0")
    decoded["dem_raw"] = raw.get("dem_raw", "true") == "true"
    decoded["dem"] = urllib.parse.unquote(raw.get("dem", ""))
    decoded["built"] = urllib.parse.unquote(raw.get("built", ""))
    decoded["icao"] = urllib.parse.unquote(raw.get("icao", ""))
    return decoded

"""UI-independent link engine for installing Ortho4XP tiles into X-Plane.

This module owns the *only* place Ortho4XP touches the X-Plane Custom Scenery
folder to (un)install built tiles.  It creates and removes symlinks (falling
back to directory junctions on Windows) and derives every status directly from
disk, never from cached application state.  It contains no Tkinter/Qt imports
and no logging/printing: all functions either return a value or raise, and the
callers (Tk Track A, Qt Track B) are responsible for user feedback.

See ``docs/specs/installed-in-xplane-switch.md`` (sections 4 and 5).
"""

import os
import re
from enum import Enum

import O4_File_Names as FNAMES


class LinkStatus(Enum):
    """Disk-derived state of a tile's (or group's) X-Plane link."""

    INSTALLED = "installed"
    # The tile's build directory itself lives in the scenery folder (no link
    # involved) — X-Plane loads it, but there is nothing for Ortho4XP to
    # remove: uninstalling would mean deleting the user's tile data.
    PHYSICAL = "physical"
    NOT_INSTALLED = "not_installed"
    BROKEN = "broken"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


# Link name used for the shared overlays folder (Tools-menu 'o' command).
OVERLAY_LINK_NAME = "yOrtho4XP_Overlays"

# Windows reparse-point attribute (used to detect directory junctions, for
# which os.path.islink() returns False).
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Matches a per-tile link name, e.g. "zOrtho4XP_+48-006" or "zOrtho4XP_-34+012":
# sign + 2 digits for latitude, sign + 3 digits for longitude (see
# FNAMES.short_latlon).
_TILE_NAME_RE = re.compile(r"^zOrtho4XP_([+-]\d{2})([+-]\d{3})$")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
def link_name(lat: int, lon: int) -> str:
    """Return the per-tile link name for ``lat``/``lon`` (e.g. ``zOrtho4XP_+48-006``)."""
    return "zOrtho4XP_" + FNAMES.short_latlon(lat, lon)


def group_link_name(build_dir: str) -> str:
    """Return the grouped-build link name: ``zOrtho4XP_`` + basename of ``build_dir``."""
    return "zOrtho4XP_" + os.path.basename(os.path.normpath(build_dir))


def _parse_tile_name(name: str):
    """Invert :func:`link_name`.

    Return ``(lat, lon)`` for a per-tile link name or ``None`` if ``name`` does
    not match the ``zOrtho4XP_±XX±YYY`` pattern.
    """
    match = _TILE_NAME_RE.match(name)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


# ---------------------------------------------------------------------------
# Low-level link primitives (symlink / Windows junction aware)
# ---------------------------------------------------------------------------
def _is_reparse_point(path: str) -> bool:
    """Return True if ``path`` is a Windows reparse point (e.g. a junction).

    Safe on POSIX, where ``st_file_attributes`` is absent and this returns
    False.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not hasattr(st, "st_file_attributes"):
        return False
    return bool(st.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_link(path: str) -> bool:
    """Return True if ``path`` is a symlink or a Windows directory junction."""
    if os.path.islink(path):
        return True
    return _is_reparse_point(path)


def _create_link(target: str, link: str) -> None:
    """Create ``link`` pointing at ``target`` (a directory).

    Uses ``os.symlink`` on all platforms; on Windows this succeeds without
    elevation when Developer Mode is on.  If ``os.symlink`` raises ``OSError``
    on Windows (symlink privilege not held), falls back to a directory junction
    via ``_winapi.CreateJunction``.  Raises on any failure -- no silent pass.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            # POSIX: nothing to fall back to; propagate the real error.
            raise
    # Windows fallback: directory junction (needs no privileges).
    try:
        import _winapi
    except ImportError as exc:  # pragma: no cover - Windows-only path
        raise OSError(
            "symlink creation failed and _winapi is unavailable for the "
            "junction fallback"
        ) from exc
    if not hasattr(_winapi, "CreateJunction"):  # pragma: no cover - Windows-only
        raise OSError(
            "symlink creation failed and _winapi.CreateJunction is unavailable "
            "for the junction fallback"
        )
    _winapi.CreateJunction(target, link)  # pragma: no cover - Windows-only


def _remove_link(link: str) -> None:
    """Remove ``link`` (a symlink or Windows junction), never its target."""
    try:
        os.unlink(link)
    except OSError:
        # Windows junctions are directory reparse points; unlink may refuse
        # them, but rmdir removes the junction without touching the target.
        if os.name == "nt" and os.path.isdir(link):  # pragma: no cover - Windows-only
            os.rmdir(link)
        else:
            raise


# ---------------------------------------------------------------------------
# Status resolution
# ---------------------------------------------------------------------------
def _resolve_status(link_path: str, target_real: str) -> LinkStatus:
    """Classify ``link_path`` against the expected resolved target ``target_real``.

    Returns one of NOT_INSTALLED / INSTALLED / BROKEN / CONFLICT (never
    UNAVAILABLE -- availability is decided by the caller).
    """
    if not os.path.lexists(link_path):
        return LinkStatus.NOT_INSTALLED
    if _is_link(link_path):
        if not os.path.exists(link_path):
            # Link with the expected name whose target is gone.
            return LinkStatus.BROKEN
        if os.path.realpath(link_path) == target_real:
            return LinkStatus.INSTALLED
        # Link resolves somewhere else -- foreign, never overwritten.
        return LinkStatus.CONFLICT
    if os.path.realpath(link_path) == target_real:
        # Not a link: the build directory itself sits in the scenery folder.
        return LinkStatus.PHYSICAL
    # A foreign real directory or file occupies the name.
    return LinkStatus.CONFLICT


def _scenery_unavailable(build_dir: str, scenery_dir: str) -> bool:
    """Return True if ``scenery_dir`` is unusable for links against ``build_dir``.

    Unavailable when the scenery dir is unset/missing (not a directory) or when
    it resolves to the same path as the build dir.
    """
    if not scenery_dir or not os.path.isdir(scenery_dir):
        return True
    return os.path.realpath(scenery_dir) == os.path.realpath(build_dir)


def _names_and_target(lat: int, lon: int, build_dir: str, grouped: bool):
    """Return ``(name, target_real)`` for the per-tile or grouped link."""
    name = group_link_name(build_dir) if grouped else link_name(lat, lon)
    return name, os.path.realpath(build_dir)


# ---------------------------------------------------------------------------
# Public per-tile / grouped API
# ---------------------------------------------------------------------------
def link_status(
    lat: int,
    lon: int,
    build_dir: str,
    scenery_dir: str,
    grouped: bool = False,
) -> LinkStatus:
    """Return the disk-derived :class:`LinkStatus` for a tile (or its group).

    See spec section 4.3.  ``grouped=True`` uses :func:`group_link_name` and the
    build directory itself as the target.
    """
    if _scenery_unavailable(build_dir, scenery_dir):
        return LinkStatus.UNAVAILABLE
    name, target_real = _names_and_target(lat, lon, build_dir, grouped)
    return _resolve_status(os.path.join(scenery_dir, name), target_real)


def install(
    lat: int,
    lon: int,
    build_dir: str,
    scenery_dir: str,
    grouped: bool = False,
) -> None:
    """Install the tile (or group) link into ``scenery_dir``.

    No-op if already INSTALLED.  A BROKEN link with the expected name is
    silently replaced.  Raises ``ValueError`` on UNAVAILABLE or CONFLICT (a
    CONFLICT name is never overwritten).  Link-creation failures propagate as
    ``OSError``.
    """
    status = link_status(lat, lon, build_dir, scenery_dir, grouped)
    if status in (LinkStatus.INSTALLED, LinkStatus.PHYSICAL):
        return
    if status is LinkStatus.UNAVAILABLE:
        raise ValueError(
            "cannot install: X-Plane scenery folder is unset, missing, or "
            "equal to the build directory"
        )
    if status is LinkStatus.CONFLICT:
        name, _ = _names_and_target(lat, lon, build_dir, grouped)
        raise ValueError(
            "cannot install: a folder or foreign link named "
            f"{name!r} already exists in the scenery folder and is not "
            "managed by Ortho4XP"
        )
    name, target = _names_and_target(lat, lon, build_dir, grouped)
    link = os.path.join(scenery_dir, name)
    if status is LinkStatus.BROKEN:
        _remove_link(link)
    _create_link(target, link)


def uninstall(
    lat: int,
    lon: int,
    build_dir: str,
    scenery_dir: str,
    grouped: bool = False,
) -> None:
    """Remove the tile (or group) link, and only the link.

    Re-verifies from disk before deleting: only an INSTALLED or BROKEN link with
    the expected name is removed.  No-op if NOT_INSTALLED.  Raises ``ValueError``
    on CONFLICT (foreign name, never deleted) or UNAVAILABLE.
    """
    status = link_status(lat, lon, build_dir, scenery_dir, grouped)
    if status is LinkStatus.NOT_INSTALLED:
        return
    if status is LinkStatus.PHYSICAL:
        name, _ = _names_and_target(lat, lon, build_dir, grouped)
        raise ValueError(
            f"{name!r} is the tile's own folder inside Custom Scenery, not a "
            "link — Ortho4XP will not delete tile data. Move the folder out "
            "of Custom Scenery if you want it uninstalled."
        )
    if status is LinkStatus.CONFLICT:
        name, _ = _names_and_target(lat, lon, build_dir, grouped)
        raise ValueError(
            f"refusing to uninstall: {name!r} is a foreign folder or link not "
            "managed by Ortho4XP"
        )
    if status is LinkStatus.UNAVAILABLE:
        raise ValueError(
            "cannot uninstall: X-Plane scenery folder is unset, missing, or "
            "equal to the build directory"
        )
    # INSTALLED or BROKEN with the expected name -- safe to remove.
    name, _ = _names_and_target(lat, lon, build_dir, grouped)
    _remove_link(os.path.join(scenery_dir, name))


def iter_installed_tiles(scenery_dir: str):
    """Incremental form of :func:`installed_tiles` for live UIs.

    Yields ``(done, total, key, target_path)`` after EVERY directory entry
    examined — ``done``/``total`` drive a progress indicator over the whole
    Custom Scenery listing, and ``key``/``target_path`` carry the
    ``(lat, lon)`` and resolved path when the entry is an installed tile
    (``None``/``None`` otherwise).  Acceptance is identical to
    :func:`installed_tiles`, which is this generator drained.
    """
    if not scenery_dir or not os.path.isdir(scenery_dir):
        return
    entries = os.listdir(scenery_dir)
    total = len(entries)
    for done, entry in enumerate(entries, start=1):
        key = target = None
        parsed = _parse_tile_name(entry)
        if parsed is not None:
            path = os.path.join(scenery_dir, entry)
            # not isdir: broken link, or a plain file squatting on the name.
            if os.path.isdir(path):
                key, target = parsed, os.path.realpath(path)
        yield done, total, key, target


def installed_tiles(scenery_dir: str) -> dict:
    """Scan ``scenery_dir`` for per-tile entries X-Plane will load.

    Returns ``{(lat, lon): target_path}`` for every entry named
    ``zOrtho4XP_±XX±YYY`` that is either a resolving symlink/junction or a
    plain directory (a tile folder living directly in Custom Scenery is just
    as installed as a linked one).  Broken links, foreign names, and group
    links are skipped.
    """
    result = {}
    for _done, _total, key, target in iter_installed_tiles(scenery_dir):
        if key is not None:
            result[key] = target
    return result


# ---------------------------------------------------------------------------
# Overlay link (yOrtho4XP_Overlays) -- shares the same engine
# ---------------------------------------------------------------------------
def install_overlay_link(overlay_dir: str, scenery_dir: str) -> None:
    """Install the shared overlays link (``yOrtho4XP_Overlays``) into ``scenery_dir``.

    No-op if already installed; replaces a BROKEN link of that name; raises
    ``ValueError`` if the scenery folder is unavailable or a CONFLICT occupies
    the name.
    """
    if not scenery_dir or not os.path.isdir(scenery_dir):
        raise ValueError(
            "cannot install overlays link: scenery folder is unset or missing"
        )
    target = os.path.realpath(overlay_dir)
    link = os.path.join(scenery_dir, OVERLAY_LINK_NAME)
    status = _resolve_status(link, target)
    if status is LinkStatus.INSTALLED:
        return
    if status is LinkStatus.CONFLICT:
        raise ValueError(
            f"cannot install overlays link: {OVERLAY_LINK_NAME!r} already "
            "exists and is not managed by Ortho4XP"
        )
    if status is LinkStatus.BROKEN:
        _remove_link(link)
    _create_link(target, link)


def uninstall_overlay_link(overlay_dir: str, scenery_dir: str) -> None:
    """Remove the shared overlays link, and only the link.

    No-op if NOT_INSTALLED or the scenery folder is missing; raises
    ``ValueError`` on a CONFLICT name (never deleted).
    """
    if not scenery_dir or not os.path.isdir(scenery_dir):
        return
    target = os.path.realpath(overlay_dir)
    link = os.path.join(scenery_dir, OVERLAY_LINK_NAME)
    status = _resolve_status(link, target)
    if status is LinkStatus.NOT_INSTALLED:
        return
    if status is LinkStatus.CONFLICT:
        raise ValueError(
            f"refusing to uninstall overlays link: {OVERLAY_LINK_NAME!r} is a "
            "foreign folder or link not managed by Ortho4XP"
        )
    _remove_link(link)

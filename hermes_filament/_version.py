"""Plugin version helpers (stdlib-only, unit-testable).

The installed version is attached to every HTTP request the plugin makes to
the Filament server — as a ``User-Agent`` / ``X-Filament-Plugin-Version``
header pair plus the MCP ``clientInfo`` on ``initialize`` — so the server can
tell what version deployed agents are running. ``update_check.py`` builds the
update-available reminder on the same helpers.

Version resolution order (``plugin_version``):

1. The ``pyproject.toml`` shipped alongside this code (the plugin's own
   directory). This is the source of truth for a **directory install**
   (git-cloned into ~/.hermes/plugins/filament): ``hermes plugins update``
   git-pulls that tree, so reading the version from it means the reported
   version tracks the code that is actually running — unlike
   ``importlib.metadata``, which would report a stale/absent pip dist-info.
2. ``importlib.metadata`` — for a legacy pip install of the package.
3. ``"unknown"`` — a source checkout that was never installed; version
   comparison treats it as unparseable, so the update reminder stays quiet
   rather than nagging developers.
"""

import re
from importlib.metadata import version as _dist_version
from pathlib import Path

DIST_NAME = "hermes-filament"
REPO_URL = "https://github.com/filament-dm/filament-hermes"

PLUGIN_NAME = "filament"

# The name Hermes knows a *pre-0.8* install by: the plugin was called
# "filament-fcm" then, and `hermes plugins update` is a git pull in place — it
# never renames the directory. So an agent that upgrades that way keeps running
# from ~/.hermes/plugins/filament-fcm, and `hermes plugins update filament`
# would fail for it. Anything we hand an operator has to use the real one.
_KNOWN_PLUGIN_DIR_NAMES = (PLUGIN_NAME, "filament-fcm")


def plugin_name_from_dir(dir_name: str) -> str:
    """Map an enclosing directory name to the plugin name Hermes knows.

    Anything that isn't a plugin directory name — a dev checkout, site-packages —
    answers to the current name.
    """
    return dir_name if dir_name in _KNOWN_PLUGIN_DIR_NAMES else PLUGIN_NAME


def installed_plugin_name() -> str:
    """The name to use in ``hermes plugins update <name>`` on this host.

    Read from the directory this package sits in, which for a directory install
    *is* the plugin name.
    """
    try:
        return plugin_name_from_dir(Path(__file__).resolve().parent.parent.name)
    except Exception:
        return PLUGIN_NAME

# install.sh installs from git main, so the version on main IS the latest
# available version — no PyPI release to consult.
LATEST_PYPROJECT_URL = (
    "https://raw.githubusercontent.com/filament-dm/filament-hermes/main/pyproject.toml"
)

# First `version = "..."` line wins — in this repo's pyproject.toml that is
# the [project] version (ruff/hatch sections carry no version key).
_PYPROJECT_VERSION_RE = re.compile(
    r"^\s*version\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE
)


def version_from_pyproject(text: str) -> str | None:
    """Extract the [project] version from pyproject.toml text.

    A regex instead of a real TOML parse: tomllib is 3.11+ and this must
    stay stdlib-only for older interpreters.
    """
    match = _PYPROJECT_VERSION_RE.search(text)
    return match.group(1) if match else None


def _version_from_local_pyproject() -> str | None:
    """Read the version from the pyproject.toml next to this package.

    This file lives at ``<plugin_root>/hermes_filament/_version.py``, so the
    plugin's pyproject.toml is two levels up. Present in a git checkout and in a
    directory install; absent when only the package (no repo) was pip-installed.
    """
    try:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        return version_from_pyproject(pyproject.read_text())
    except Exception:
        return None


def plugin_version() -> str:
    """The running plugin's version, or "unknown" (see module docstring)."""
    local = _version_from_local_pyproject()
    if local:
        return local
    try:
        return _dist_version(DIST_NAME)
    except Exception:
        return "unknown"


PLUGIN_VERSION = plugin_version()
USER_AGENT = f"{DIST_NAME}/{PLUGIN_VERSION}"


def version_headers() -> dict:
    """Headers attached to every request to the Filament server.

    User-Agent lands in ordinary HTTP access logs;
    X-Filament-Plugin-Version is trivial for the server to pick up
    explicitly (e.g. into the MCP audit log).
    """
    return {
        "User-Agent": USER_AGENT,
        "X-Filament-Plugin-Version": PLUGIN_VERSION,
    }


def _version_tuple(version: str) -> tuple | None:
    """Parse "0.1.2" → (0, 1, 2); None when nothing numeric leads.

    Only leading numeric dot-components count; a suffix like "rc1" in
    "0.2.0rc1" is ignored (compared equal to its release).
    """
    parts = []
    for piece in version.strip().split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts) if parts else None


def is_newer(candidate: str, current: str) -> bool:
    """True when *candidate* is a strictly newer release than *current*.

    Fails quiet: if either side doesn't parse (e.g. "unknown"), the answer
    is False — never remind on garbage data.
    """
    a = _version_tuple(candidate)
    b = _version_tuple(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))

"""Directory-plugin entry point.

Hermes discovers this plugin from the repo root: ``plugin.yaml`` for the
manifest, this module for the code. Its loader requires an ``__init__.py`` here
(``hermes_cli/plugins.py:_load_directory_module``), execs it, and calls
``register()``. The implementation lives in the nested ``hermes_filament_fcm``
package, so this is a shim over it.

Committed, not generated at install time: ``hermes plugins install`` moves the
cloned tree into place verbatim, so anything not in the repo does not exist in
the installed plugin.

Two details of the shape below are load-bearing.

``vendor/`` goes on ``sys.path`` at module level, before anything can import the
package — importing it pulls in ``firebase_messaging`` through ``adapter``. The
plugin carries its own copy of that dependency because nothing in the install
path can provide one: ``hermes plugins install`` is a ``git clone`` that never
invokes pip or uv, ``plugin.yaml`` has no dependency field for it to read, and
by the time this module first executes — inside the gateway process, on the next
start — we are the unprivileged gateway uid while ``/opt/hermes/.venv`` on the
Docker and cloud images is root-owned. ``scripts/vendor-deps.sh`` rebuilds the
tree and documents what is in it and why.

The package import is deferred into ``register()`` so that importing this module
needs nothing but the standard library. That keeps the repo root harmless as a
package: pytest makes the rootdir a ``Package`` node once an ``__init__.py``
appears there and imports it before every test, which a module-level
``from .hermes_filament_fcm import register`` turns into a collection error in
an environment without Hermes. Failures stay just as loud — Hermes calls
``register()`` inside the same try/except that guards the module exec, so an
ImportError still surfaces as the plugin's load error.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Appended, not prepended: a properly installed ambient copy then wins package
# by package and the vendored tree only fills what the environment lacks, so a
# user who pip-installs these for real gets theirs and deps.py reports the
# version actually in force. A stock Hermes provides none of them.
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
    sys.path.append(str(_VENDOR_DIR))


def register(ctx) -> None:
    """Register the Filament platform and its tools (see the package's
    ``register``)."""
    # Deferred on purpose — see the module docstring. PLC0415 wants this at the
    # top, which is precisely what breaks importing this module standalone.
    from .hermes_filament_fcm import register as _register  # noqa: PLC0415

    return _register(ctx)


__all__ = ["register"]

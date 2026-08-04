"""FCM credential persistence.

Saves and loads Firebase Cloud Messaging registration credentials so the
plugin doesn't re-register with Google on every startup, and the
persistent ids of already-received pushes so Google MCS doesn't
redeliver them after a gateway restart.

Credentials are stored at ~/.hermes/filament/fcm_credentials.json
and received ids at ~/.hermes/filament/received_persistent_ids.json
(or the directory specified by FILAMENT_CREDENTIALS_DIR).

Note: The MCP token is NOT persisted here — it is provided by the user
via the FILAMENT_MCP_TOKEN environment variable and can be rotated
independently. See README.md for how to generate one.
"""

import json
import logging
import os
from pathlib import Path
from secrets import token_hex
from typing import Any

logger = logging.getLogger("gateway.filament")

_DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".hermes", "filament")

# The plugin was called "filament-fcm" through v0.7.0, and its state lived in
# ~/.hermes/filament-fcm/. That tree holds more than FCM credentials — standing
# instructions, the wake policy, the capability policy, engaged threads — so an
# agent that finds neither dir would silently revert to default instructions.
# The setup wizard moves the legacy dir on the next install (setup_cli.py's
# ``_migrate_state_dir``); this fallback covers agents that only ever run
# ``hermes plugins update``, which never runs the wizard.
#
# Duplicated in reactive.py's ``_default_dir`` on purpose: both modules must
# stay importable standalone (stdlib-only, no intra-package imports) so the
# tests can load them without Hermes — see CLAUDE.md. Keep the two in sync.
_LEGACY_DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".hermes", "filament-fcm")


def default_state_dir() -> str:
    """The plugin's state directory: the current path, or the legacy one when
    that is the only one present."""
    if not os.path.isdir(_DEFAULT_DIR) and os.path.isdir(_LEGACY_DEFAULT_DIR):
        return _LEGACY_DEFAULT_DIR
    return _DEFAULT_DIR

# Cap on how many received persistent ids we keep. MCS only redelivers
# recent unacked messages, so a bounded tail is plenty; this just keeps
# the file (and the login payload built from it) from growing forever.
MAX_RECEIVED_PERSISTENT_IDS = 1000


class CredentialStore:
    """Manages persisted FCM credentials for the filament plugin."""

    def __init__(self, base_dir: str | None = None) -> None:
        self._dir = Path(
            base_dir
            # FILAMENT_FCM_CREDENTIALS_DIR was the name through v0.7.0; still
            # honored so an existing .env keeps pointing at the same tree.
            or os.environ.get("FILAMENT_CREDENTIALS_DIR")
            or os.environ.get("FILAMENT_FCM_CREDENTIALS_DIR")
            or default_state_dir()
        )

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, filename: str) -> dict[str, Any] | None:
        path = self._dir / filename
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.warning("Failed to read %s", path, exc_info=True)
            return None

    def _write_json(self, filename: str, data: dict[str, Any]) -> None:
        self._ensure_dir()
        path = self._dir / filename
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug("Wrote %s", path)
        except Exception:
            logger.warning("Failed to write %s", path, exc_info=True)

    def load_fcm_credentials(self) -> dict[str, Any] | None:
        """Load saved FCM registration credentials."""
        return self._read_json("fcm_credentials.json")

    def save_fcm_credentials(self, creds: dict[str, Any]) -> None:
        """Persist FCM registration credentials."""
        self._write_json("fcm_credentials.json", creds)

    def load_update_notice(self) -> dict[str, Any] | None:
        """Load the update-reminder state (last version the principal was
        told about) — see update_check.py."""
        return self._read_json("update_notice.json")

    def save_update_notice(self, data: dict[str, Any]) -> None:
        """Persist the update-reminder state."""
        self._write_json("update_notice.json", data)

    def load_or_create_installation_id(self) -> str:
        """Return a stable, report-safe id for this Hermes plugin install."""
        data = self._read_json("installation.json")
        if isinstance(data, dict):
            installation_id = data.get("installation_id")
            if isinstance(installation_id, str) and installation_id:
                return installation_id
        installation_id = f"inst_{token_hex(5)}"
        self._write_json("installation.json", {"installation_id": installation_id})
        return installation_id

    def load_received_persistent_ids(self) -> list[str]:
        """Load the persistent ids of pushes we've already received."""
        data = self._read_json("received_persistent_ids.json")
        if not isinstance(data, dict):
            return []
        ids = data.get("ids")
        if not isinstance(ids, list):
            return []
        return [i for i in ids if isinstance(i, str)]

    def save_received_persistent_ids(self, ids: list[str]) -> None:
        """Persist the received-push persistent ids (bounded tail)."""
        self._write_json(
            "received_persistent_ids.json",
            {"ids": ids[-MAX_RECEIVED_PERSISTENT_IDS:]},
        )


class ReceivedPersistentIds:
    """Tracks which FCM pushes have already been received, across restarts.

    Google MCS redelivers any push it hasn't seen acknowledged. If the
    gateway exits before the ack flushes (e.g. a ``/restart`` command kills
    the process mid-handling), the same push arrives again on the next
    connect — and a redelivered ``/restart`` restarts the gateway in an
    infinite loop. Two defenses, both fed from this store:

    - ``ids`` is passed to ``FcmPushClient(received_persistent_ids=...)``
      so the MCS login tells Google not to redeliver them.
    - ``record()`` gates dispatch, dropping any redelivery that slips
      through anyway (the library does no callback-level dedup).

    ``record()`` persists *before* the message is dispatched, so the id is
    durable even when handling the message kills the process.
    """

    def __init__(
        self, store: CredentialStore, max_ids: int = MAX_RECEIVED_PERSISTENT_IDS
    ) -> None:
        self._store = store
        self._max = max_ids
        self._ids = store.load_received_persistent_ids()[-max_ids:]
        self._seen = set(self._ids)

    @property
    def ids(self) -> list[str]:
        """The received ids, oldest first."""
        return list(self._ids)

    def record(self, persistent_id: str | None) -> bool:
        """Record *persistent_id*; return True if it's new (safe to dispatch).

        Returns False for an already-seen id (a redelivery — skip it).
        Ids that are empty/None can't be deduped and are treated as new
        without being recorded.
        """
        if not persistent_id:
            return True
        if persistent_id in self._seen:
            return False
        self._ids.append(persistent_id)
        self._seen.add(persistent_id)
        if len(self._ids) > self._max:
            dropped = self._ids[: -self._max]
            self._ids = self._ids[-self._max :]
            self._seen.difference_update(dropped)
        self._store.save_received_persistent_ids(self._ids)
        return True

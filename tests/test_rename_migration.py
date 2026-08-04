"""Tests for the filament-fcm → filament rename migrations.

The plugin was called "filament-fcm" through v0.7.0 and keyed three things on
that name: the directory its state lives in, the ``plugins.enabled`` entry, and
the platform segment of the gateway's session-routing keys. What's pinned here is
that an upgrade keeps its state and its conversations — the failure mode is
silent (an agent comes back with default instructions and no history), so it
would not show up as an error anywhere.

Loaded standalone like the rest of the suite (see CLAUDE.md): ``credentials.py``,
``reactive.py`` and ``_version.py`` are stdlib-only, and ``setup_cli.py`` gets
``yaml`` / ``hermes_cli.setup`` / ``filament_api`` stubbed.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament"


def _load_standalone(name, filename):
    spec = importlib.util.spec_from_file_location(name, _PKG_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


credentials = _load_standalone("_credentials_undertest", "credentials.py")
reactive = _load_standalone("_reactive_rename_undertest", "reactive.py")
version_mod = _load_standalone("_version_rename_undertest", "_version.py")


def _load_setup_cli():
    """Load ``setup_cli.py`` with its non-stdlib imports stubbed.

    It needs ``yaml`` (real, if installed), ``hermes_cli.setup`` (Hermes — never
    present in a dev env) and its own ``.filament_api`` sibling, which drags in
    httpx. Only the config/state/session migrations are under test here, so all
    three are stand-ins.
    """
    pkg = types.ModuleType("hermes_filament")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hermes_filament"] = pkg

    hermes_cli = types.ModuleType("hermes_cli")
    setup_mod = types.ModuleType("hermes_cli.setup")
    for fn in (
        "get_env_value",
        "print_header",
        "print_info",
        "print_success",
        "print_warning",
        "prompt",
        "prompt_yes_no",
        "remove_env_value",
        "save_env_value",
    ):
        setattr(setup_mod, fn, lambda *a, **k: None)
    hermes_cli.setup = setup_mod
    sys.modules.setdefault("hermes_cli", hermes_cli)
    sys.modules.setdefault("hermes_cli.setup", setup_mod)

    api_mod = types.ModuleType("hermes_filament.filament_api")
    api_mod.FilamentAPI = object
    sys.modules["hermes_filament.filament_api"] = api_mod

    spec = importlib.util.spec_from_file_location(
        "hermes_filament.setup_cli", _PKG_DIR / "setup_cli.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_filament.setup_cli"] = module
    spec.loader.exec_module(module)
    return module


yaml = pytest.importorskip("yaml", reason="setup_cli needs PyYAML")
setup_cli = _load_setup_cli()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A throwaway ~/.hermes, with $HOME pointed at its parent.

    ``reactive._default_dir`` resolves against ``Path.home()`` and
    ``setup_cli._find_hermes_home`` against ``$HERMES_HOME``; both land here.
    """
    home = tmp_path / "home"
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.delenv("FILAMENT_CREDENTIALS_DIR", raising=False)
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    # credentials.py resolves ~ at import time, so its constants have to be
    # repointed rather than relying on $HOME.
    monkeypatch.setattr(credentials, "_DEFAULT_DIR", str(hermes / "filament"))
    monkeypatch.setattr(
        credentials, "_LEGACY_DEFAULT_DIR", str(hermes / "filament-fcm")
    )
    return hermes


# ── state dir resolution: the legacy tree stays readable ─────────────


def test_state_dir_prefers_current_name(hermes_home):
    (hermes_home / "filament").mkdir()
    assert credentials.default_state_dir() == str(hermes_home / "filament")
    assert reactive._default_dir() == hermes_home / "filament"


def test_state_dir_falls_back_to_legacy_when_only_it_exists(hermes_home):
    (hermes_home / "filament-fcm").mkdir()
    assert credentials.default_state_dir() == str(hermes_home / "filament-fcm")
    assert reactive._default_dir() == hermes_home / "filament-fcm"


def test_state_dir_ignores_legacy_once_migrated(hermes_home):
    """Both present (a stray legacy dir after a migration) → the new one wins."""
    (hermes_home / "filament").mkdir()
    (hermes_home / "filament-fcm").mkdir()
    assert credentials.default_state_dir() == str(hermes_home / "filament")
    assert reactive._default_dir() == hermes_home / "filament"


def test_state_dir_defaults_to_current_name_on_fresh_install(hermes_home):
    assert credentials.default_state_dir() == str(hermes_home / "filament")
    assert reactive._default_dir() == hermes_home / "filament"


def test_legacy_env_var_still_points_the_store(hermes_home, monkeypatch, tmp_path):
    """An existing .env with FILAMENT_FCM_CREDENTIALS_DIR must keep working."""
    pinned = tmp_path / "pinned"
    monkeypatch.setenv("FILAMENT_FCM_CREDENTIALS_DIR", str(pinned))
    assert credentials.CredentialStore()._dir == pinned
    assert reactive._default_dir() == pinned


def test_current_env_var_wins_over_legacy(hermes_home, monkeypatch, tmp_path):
    monkeypatch.setenv("FILAMENT_CREDENTIALS_DIR", str(tmp_path / "new"))
    monkeypatch.setenv("FILAMENT_FCM_CREDENTIALS_DIR", str(tmp_path / "old"))
    assert credentials.CredentialStore()._dir == tmp_path / "new"
    assert reactive._default_dir() == tmp_path / "new"


# ── plugins.enabled ──────────────────────────────────────────────────


def _enabled(hermes: Path):
    return (yaml.safe_load((hermes / "config.yaml").read_text()) or {})["plugins"][
        "enabled"
    ]


def test_enable_plugin_replaces_legacy_entry(hermes_home):
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n  - kanban\n  - filament-fcm\n"
    )
    setup_cli._enable_plugin()
    assert _enabled(hermes_home) == ["kanban", "filament"]


def test_enable_plugin_drops_legacy_entry_when_both_present(hermes_home):
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n  - filament-fcm\n  - filament\n"
    )
    setup_cli._enable_plugin()
    assert _enabled(hermes_home) == ["filament"]


def test_enable_plugin_appends_on_fresh_config(hermes_home):
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled:\n  - kanban\n")
    setup_cli._enable_plugin()
    assert _enabled(hermes_home) == ["kanban", "filament"]


def test_enable_plugin_leaves_already_enabled_config_alone(hermes_home):
    before = "plugins:\n  enabled:\n  - filament\n"
    (hermes_home / "config.yaml").write_text(before)
    setup_cli._enable_plugin()
    assert (hermes_home / "config.yaml").read_text() == before


# ── state dir migration ──────────────────────────────────────────────


def test_migrate_state_dir_moves_legacy_tree(hermes_home):
    legacy = hermes_home / "filament-fcm"
    legacy.mkdir()
    (legacy / "instructions.md").write_text("be helpful")
    setup_cli._migrate_state_dir()
    assert not legacy.exists()
    assert (hermes_home / "filament" / "instructions.md").read_text() == "be helpful"


def test_migrate_state_dir_never_clobbers_an_existing_tree(hermes_home):
    legacy = hermes_home / "filament-fcm"
    legacy.mkdir()
    (legacy / "instructions.md").write_text("old")
    current = hermes_home / "filament"
    current.mkdir()
    (current / "instructions.md").write_text("current")
    setup_cli._migrate_state_dir()
    assert (current / "instructions.md").read_text() == "current"
    assert legacy.exists()


def test_migrate_state_dir_respects_a_pinned_location(hermes_home, monkeypatch):
    monkeypatch.setenv("FILAMENT_CREDENTIALS_DIR", str(hermes_home / "elsewhere"))
    legacy = hermes_home / "filament-fcm"
    legacy.mkdir()
    setup_cli._migrate_state_dir()
    assert legacy.exists()
    assert not (hermes_home / "filament").exists()


def test_migrate_state_dir_is_a_noop_on_fresh_install(hermes_home):
    setup_cli._migrate_state_dir()
    assert not (hermes_home / "filament").exists()


# ── session re-keying: conversations survive the platform rename ──────


def _sessions_file(hermes: Path, data: dict) -> Path:
    path = hermes / "sessions" / "sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _entry(key: str, session_id: str = "sid1") -> dict:
    return {
        "session_key": key,
        "session_id": session_id,
        "platform": key.split(":")[2],
        "chat_type": "dm",
        "origin": {"platform": key.split(":")[2], "chat_id": "!room:host"},
    }


def test_migrate_session_keys_rewrites_the_platform_segment(hermes_home):
    old = "agent:main:filament-fcm:dm:!room:host"
    path = _sessions_file(hermes_home, {"_README": "notes", old: _entry(old)})

    setup_cli._migrate_session_keys()

    data = json.loads(path.read_text())
    new = "agent:main:filament:dm:!room:host"
    assert old not in data
    assert data["_README"] == "notes"
    entry = data[new]
    assert entry["session_key"] == new
    assert entry["session_id"] == "sid1"  # the conversation itself is preserved
    assert entry["platform"] == "filament"
    assert entry["origin"]["platform"] == "filament"


def test_migrate_session_keys_handles_a_named_profile_namespace(hermes_home):
    """Keys are ``agent:<ns>:<platform>:…`` — the namespace isn't always "main"."""
    old = "agent:coder:filament-fcm:group:!room:host"
    path = _sessions_file(hermes_home, {old: _entry(old)})
    setup_cli._migrate_session_keys()
    assert "agent:coder:filament:group:!room:host" in json.loads(path.read_text())


def test_migrate_session_keys_leaves_other_platforms_alone(hermes_home):
    key = "agent:main:telegram:dm:123"
    path = _sessions_file(hermes_home, {key: _entry(key)})
    setup_cli._migrate_session_keys()
    data = json.loads(path.read_text())
    assert key in data
    assert data[key]["platform"] == "telegram"


def test_migrate_session_keys_prefers_an_existing_new_key(hermes_home):
    """A turn already ran under the new name: that entry is current, keep it."""
    old = "agent:main:filament-fcm:dm:!room:host"
    new = "agent:main:filament:dm:!room:host"
    path = _sessions_file(
        hermes_home, {old: _entry(old, "stale"), new: _entry(new, "fresh")}
    )
    setup_cli._migrate_session_keys()
    data = json.loads(path.read_text())
    assert old not in data
    assert data[new]["session_id"] == "fresh"


def test_migrate_session_keys_leaves_the_file_untouched_when_nothing_matches(
    hermes_home,
):
    key = "agent:main:filament:dm:!room:host"
    path = _sessions_file(hermes_home, {key: _entry(key)})
    before = path.read_text()
    setup_cli._migrate_session_keys()
    assert path.read_text() == before


def test_migrate_session_keys_tolerates_a_missing_file(hermes_home):
    setup_cli._migrate_session_keys()  # no sessions/ dir at all
    assert not (hermes_home / "sessions" / "sessions.json").exists()


def test_migrate_session_keys_survives_a_corrupt_file(hermes_home):
    path = hermes_home / "sessions" / "sessions.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    setup_cli._migrate_session_keys()  # warns, doesn't raise
    assert path.read_text() == "{not json"


# ── the `hermes plugins update <name>` hint ──────────────────────────


def test_plugin_name_from_dir_keeps_a_legacy_install_addressable():
    """A pre-0.8 install still lives in plugins/filament-fcm — `plugins update`
    takes that name, not the new one."""
    assert version_mod.plugin_name_from_dir("filament-fcm") == "filament-fcm"


def test_plugin_name_from_dir_uses_the_current_name():
    assert version_mod.plugin_name_from_dir("filament") == "filament"


@pytest.mark.parametrize("dir_name", ["filament-hermes-dev", "site-packages", ""])
def test_plugin_name_from_dir_falls_back_for_non_plugin_dirs(dir_name):
    assert version_mod.plugin_name_from_dir(dir_name) == "filament"

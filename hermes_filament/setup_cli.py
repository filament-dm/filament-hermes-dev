#!/usr/bin/env python3
"""Setup CLI for hermes-filament.

Handles the chicken-and-egg problem where `hermes gateway setup` can't
see the plugin until it's in `plugins.enabled`, but the setup wizard is
supposed to handle enabling it.

This script:
  1. Adds 'filament' to plugins.enabled in config.yaml
  2. Migrates state left behind by the old 'filament-fcm' plugin name
  3. Runs the interactive setup (prompts for token, senders, URL)
  4. Restarts the gateway

Usage:
    filament-setup
"""

import asyncio
import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

import yaml
from hermes_cli.setup import (
    get_env_value,
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt,
    prompt_yes_no,
    remove_env_value,
    save_env_value,
)

from .filament_api import FilamentAPI

# The Firebase project the gateway registers with. It must be the same project
# the homeserver pushes from, or FCM rejects every token as cross-project and
# the agent is never woken — it connects, looks healthy, and silently answers
# nothing. The plugin defaults to production (see fcm_client), so only other
# homeservers export these; persist them like FILAMENT_MCP_URL below, because
# the gateway starts from .env and never sees the installer's environment.
_FIREBASE_ENV_KEYS = (
    "FILAMENT_FIREBASE_PROJECT_ID",
    "FILAMENT_FIREBASE_API_KEY",
    "FILAMENT_FIREBASE_APP_ID",
    "FILAMENT_FIREBASE_SENDER_ID",
)


def _find_hermes_home() -> Path:
    """Resolve the Hermes home directory."""
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home)
    return Path.home() / ".hermes"


# The plugin was named "filament-fcm" through v0.7.0. Everything it keyed on
# that name — the enabled-plugins entry, the state directory, the gateway's
# session-routing keys — is migrated by the helpers below, so an upgrade keeps
# working instead of coming back up as a stranger with no memory. Each one is
# idempotent and never overwrites an already-migrated target, so re-running the
# installer is safe. See also credentials.py / reactive.py, which fall back to
# the legacy state dir for agents that upgrade with `hermes plugins update`
# alone and therefore never reach this wizard.
_LEGACY_PLUGIN_NAME = "filament-fcm"
_PLUGIN_NAME = "filament"


def _enable_plugin() -> None:
    """Add 'filament' to plugins.enabled in config.yaml (dropping the legacy
    'filament-fcm' entry)."""
    config_path = _find_hermes_home() / "config.yaml"

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f"plugins:\n  enabled:\n  - {_PLUGIN_NAME}\n")
        print_info(f"Created {config_path} with {_PLUGIN_NAME} enabled")
        return

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    plugins = config.setdefault("plugins", {})
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []

    # Hermes matches plugins.enabled against the *directory* name first and the
    # manifest name second, so a stale "filament-fcm" entry is harmless on its
    # own — but leaving it would re-enable an old clone that survived somewhere,
    # and two copies both register the platform. Drop it.
    had_legacy = _LEGACY_PLUGIN_NAME in enabled
    already = _PLUGIN_NAME in enabled
    if already and not had_legacy:
        print_info(f"Plugin {_PLUGIN_NAME} is already enabled")
        return

    enabled = [e for e in enabled if e != _LEGACY_PLUGIN_NAME]
    if not already:
        enabled.append(_PLUGIN_NAME)
    plugins["enabled"] = enabled

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    if had_legacy:
        print_info(
            f"Renamed {_LEGACY_PLUGIN_NAME} → {_PLUGIN_NAME} in {config_path}"
        )
    else:
        print_info(f"Enabled {_PLUGIN_NAME} in {config_path}")


def _migrate_state_dir() -> None:
    """Move ~/.hermes/filament-fcm/ to ~/.hermes/filament/.

    The tree holds the agent's standing instructions, wake policy, capability
    policy, engaged threads and FCM registration — losing it would reset the
    agent's behaviour and force a re-registration with Google. Skipped when the
    user pinned the location by hand, and when the new dir already exists (the
    already-migrated case, and the only case where a move could destroy data).
    """
    if os.environ.get("FILAMENT_CREDENTIALS_DIR") or os.environ.get(
        "FILAMENT_FCM_CREDENTIALS_DIR"
    ):
        return

    # credentials.py resolves this against ~ rather than $HERMES_HOME, so match
    # that exactly — migrating a different tree would be a no-op at best.
    base = Path.home() / ".hermes"
    legacy, current = base / _LEGACY_PLUGIN_NAME, base / _PLUGIN_NAME
    if not legacy.is_dir() or current.exists():
        return

    try:
        os.replace(legacy, current)
    except OSError as exc:
        # Not fatal: both modules that read this tree fall back to the legacy
        # path when the new one is absent, so the agent keeps its state either
        # way. Say so rather than failing the install.
        print_warning(f"Could not move {legacy} to {current} ({exc}); still using it.")
        return
    print_info(f"Moved agent state {legacy} → {current}")


def _migrate_session_keys() -> None:
    """Re-key the gateway's session index from the old platform name.

    Session keys are ``agent:<ns>:<platform>:<chat_type>:...`` (``parts[2]`` is
    the platform), so renaming the platform would otherwise strand every
    channel's conversation and each one would start over with no context. Rewrite
    the routing index in place, keeping session ids intact.

    Called immediately before the gateway restart: the running gateway holds
    this index in memory and rewrites the whole file when it saves, so an
    earlier rewrite could be clobbered by a message arriving mid-install.
    """
    path = _find_hermes_home() / "sessions" / "sessions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        print_warning(f"Could not read {path}; channels will start fresh sessions.")
        return
    if not isinstance(data, dict):
        return

    def rekey(key: str) -> str:
        parts = key.split(":")
        if len(parts) > 2 and parts[2] == _LEGACY_PLUGIN_NAME:
            parts[2] = _PLUGIN_NAME
            return ":".join(parts)
        return key

    migrated = 0
    dropped = 0
    out: dict = {}
    for key, entry in data.items():
        # "_README" and any other underscore key is a comment, not an entry.
        if key.startswith("_") or not isinstance(entry, dict):
            out[key] = entry
            continue
        new_key = rekey(key)
        # A key under the new name already exists (a turn ran before the
        # migration): that one is current — keep it and drop the stale twin.
        # Counted separately, because dropping it is itself a change that has to
        # be written out; otherwise the stale key survives in the file.
        if new_key != key and new_key in data:
            dropped += 1
            continue
        if new_key != key:
            migrated += 1
        updated = dict(entry)
        updated["session_key"] = rekey(str(entry.get("session_key", new_key)))
        if updated.get("platform") == _LEGACY_PLUGIN_NAME:
            updated["platform"] = _PLUGIN_NAME
        origin = updated.get("origin")
        if isinstance(origin, dict) and origin.get("platform") == _LEGACY_PLUGIN_NAME:
            updated["origin"] = {**origin, "platform": _PLUGIN_NAME}
        out[new_key] = updated

    if not migrated and not dropped:
        return

    # Write via a temp file + replace so an interrupted install can't leave the
    # gateway with a truncated routing index.
    tmp = path.with_name(f"{path.name}.filament-rename.tmp")
    try:
        tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print_warning(f"Could not rewrite {path} ({exc}); channels start fresh.")
        with contextlib.suppress(OSError):
            tmp.unlink()
        return
    if migrated:
        print_info(f"Carried {migrated} conversation(s) over to the new platform name")


# JSON-RPC codes from the agents MCP. -32002: token valid but the account
# doesn't exist yet ("reserved" — the principal hasn't finished the connect
# flow). Anything else (e.g. -32001) means the token isn't usable.
_RESERVED_CODE = -32002


def _wait_for_finalization(token: str, url: str) -> tuple[bool, str | None]:
    """Block until the agent is finalized in the Filament app.

    Returns ``(ready, principal_id)``:

    - ``(True, "<@owner:server>")`` when the agent is finalized — the
      principal (owner) is extracted from the same ``get_self`` payload so
      the caller can seed the sender allowlist without prompting for a user
      ID. ``principal_id`` may be ``None`` if the payload lacked an owner.
    - ``(False, None)`` when the token is definitively rejected (auth
      error) or the user pressed Ctrl+C.

    While the agent is reserved, ``get_self`` returns -32002; we show a
    one-time nudge and keep polling, so the flow connects automatically
    once the user finishes naming the agent.  Transient errors (transport
    failures, HTTP 500, non-dict error responses) are retried — only a
    well-formed JSON-RPC auth rejection (-32001) aborts.
    """
    # Only this specific error code means the token itself is bad and
    # retrying won't help. Everything else is transient or reserved.
    _AUTH_REQUIRED_CODE = -32001

    async def _poll() -> tuple[bool, str | None]:
        api = FilamentAPI(url, token)
        nudged = False
        try:
            while True:
                try:
                    resp = await api.get_self()
                except Exception:
                    await asyncio.sleep(3)  # transient transport error — retry
                    continue
                err = (resp or {}).get("error")
                if err is None:
                    # Finalized. Pull the principal (owner) out of the
                    # get_self payload so setup can seed the sender allowlist
                    # without prompting for a user ID (mirrors the runtime
                    # extraction in adapter._initialize_api).
                    principal = None
                    data = api.parse_tool_result(resp)
                    if isinstance(data, dict):
                        owner = data.get("owner")
                        if isinstance(owner, dict) and owner.get("user_id"):
                            principal = owner["user_id"]
                        else:
                            principal = data.get("owner_id")
                    return True, principal

                # Only dict errors carry a JSON-RPC code. String errors
                # come from FilamentAPI._post() for HTTP-level failures
                # (e.g. "HTTP 401", "HTTP 500"). 401/403 are definitive
                # token rejections; everything else is transient.
                if not isinstance(err, dict):
                    if isinstance(err, str) and ("401" in err or "403" in err):
                        print_warning(
                            "The server rejected this token. Reconnect in "
                            "the Filament app to get a fresh one, then "
                            "re-run setup."
                        )
                        return False, None
                    await asyncio.sleep(3)
                    continue

                code = err.get("code")
                if code == _RESERVED_CODE:
                    if not nudged:
                        print_info(
                            "This agent isn't finished setting up yet — please "
                            "go back to the Filament app and finish the connect "
                            "flow (naming your agent creates it). This will "
                            "connect automatically once you're done."
                        )
                        nudged = True
                    await asyncio.sleep(3)
                    continue
                if code == _AUTH_REQUIRED_CODE:
                    print_warning(
                        "The server rejected this token. Reconnect in the "
                        "Filament app to get a fresh one, then re-run setup."
                    )
                    return False, None
                # Unknown JSON-RPC error — likely transient, retry.
                await asyncio.sleep(3)
        finally:
            await api.close()

    try:
        ready, principal = asyncio.run(_poll())
        if ready:
            print_success("Agent is finalized — ready to connect.")
            return True, principal
        return False, None
    except KeyboardInterrupt:
        print_info("Stopped waiting. Re-run setup once the agent is created.")
        return False, None


def _run_interactive_setup() -> bool:
    """Run the interactive setup prompts.

    Returns ``True`` when setup completed successfully (the agent is
    finalized and the gateway should be restarted), ``False`` when setup
    was skipped, aborted, or finalization failed.
    """
    print_header("Filament")

    # The app's one-line connect command exports the agent token as
    # CONNECT_TOKEN, so the whole flow is a single paste with no token prompt.
    connect_token = os.environ.get("CONNECT_TOKEN", "").strip()

    existing_token = get_env_value("FILAMENT_MCP_TOKEN")
    if existing_token and not connect_token:
        print_info(
            f"Filament: already configured (token: {existing_token[:12]}...)"
        )
        if not prompt_yes_no("Reconfigure?", False):
            return False

    print_info("Connect Hermes to Filament via FCM push notifications.")
    if not connect_token:
        print_info("You'll need an MCP agent token — see the README for how to")
        print_info("generate one using the token exchange endpoint.")
    print()

    # MCP token (required, secret). Prefer CONNECT_TOKEN from the environment
    # (set by the app's copy-paste command) so no interactive prompt is needed.
    if connect_token:
        token = connect_token
        print_info(f"Using MCP agent token from CONNECT_TOKEN ({token[:12]}...).")
    else:
        token = prompt("MCP agent token (fmcp_...)", password=True)
    if not token:
        print_warning("Token is required — skipping setup")
        return False
    token = token.strip()

    # MCP endpoint URL — never prompted. Use FILAMENT_MCP_URL when set (the
    # connect command exports it; local-dev users can export it or edit
    # ~/.hermes/.env), otherwise default to production.
    url = (
        (get_env_value("FILAMENT_MCP_URL") or "https://api.filament.dm/mcp/agents")
        .strip()
        .rstrip("/")
    )

    # Validate the token before persisting any configuration. If the token
    # is rejected or the user aborts, the previous working config in
    # ~/.hermes/.env is preserved rather than being overwritten with bad
    # credentials. _wait_for_finalization also handles the reserved window
    # (polls until the agent is finalized in the app) and returns the
    # principal (owner) it learned from get_self.
    ready, principal_id = _wait_for_finalization(token, url)
    if not ready:
        return False

    # Token validated — persist all configuration.
    save_env_value("FILAMENT_MCP_TOKEN", token)
    save_env_value("FILAMENT_MCP_URL", url)

    # Carry the Firebase project through to the gateway (see _FIREBASE_ENV_KEYS).
    for key in _FIREBASE_ENV_KEYS:
        value = (get_env_value(key) or "").strip()
        if value:
            save_env_value(key, value)

    # Seed FILAMENT_CONTROL_USERS with the principal we learned from get_self.
    # It is the platform's allowed_users_env, so the gateway admits these senders
    # (the owner reaches the agent with no manual `hermes pairing approve`), and
    # the adapter also reads it as its control-plane trusted set for trust-zone
    # framing. The adapter re-adds the principal at runtime too, but seeding here
    # trusts the owner from the very first message. We derive the ID from the
    # token, so the user is never prompted for it.
    senders: list[str] = []
    if principal_id:
        senders.append(principal_id)
    else:
        print_warning(
            "Could not determine the principal (owner) from the token — "
            "you may have to run `hermes pairing approve` once, or set "
            "FILAMENT_CONTROL_USERS manually."
        )

    if not connect_token:
        # Manual-token path: let operators add extra control-plane users beyond
        # the principal (e.g. teammates who should command the agent).
        print_info(
            "Your principal (owner) is added to the control-plane users "
            "automatically. You can grant additional commanders here."
        )
        # Default to the existing extra users (the previously-saved control set
        # minus the current principal) so pressing Enter on reconfigure
        # preserves teammates without re-pinning a stale principal: when
        # reconfiguring with a *different* owner's token, the old principal is
        # not silently carried over. The current principal is prepended fresh
        # below and the list de-duped.
        prior = get_env_value("FILAMENT_CONTROL_USERS") or ""
        prior_extras = ",".join(
            u for u in (s.strip() for s in prior.split(",")) if u and u != principal_id
        )
        extra = prompt(
            "Additional control-plane user IDs (optional, comma-separated)",
            default=prior_extras,
        )
        if extra:
            senders.extend(s for s in extra.replace(" ", "").split(",") if s)

    if senders:
        # De-dupe, preserving order (principal first).
        seen: set[str] = set()
        ordered = [s for s in senders if not (s in seen or seen.add(s))]
        save_env_value("FILAMENT_CONTROL_USERS", ",".join(ordered))
    else:
        # Nothing to allow — clear any stale value so it doesn't persist.
        remove_env_value("FILAMENT_CONTROL_USERS")

    print()
    print_success("Configuration saved to ~/.hermes/.env")

    return True


def _persist(token: str, url: str, principal_id: str | None) -> None:
    """Write the validated connection to the engine's .env.

    Seeds FILAMENT_CONTROL_USERS with the principal, which is the platform's
    allowed_users_env. The owner then reaches the agent from the first message,
    with no `hermes pairing approve` step.
    """
    save_env_value("FILAMENT_MCP_TOKEN", token)
    save_env_value("FILAMENT_MCP_URL", url)
    if principal_id:
        save_env_value("FILAMENT_CONTROL_USERS", principal_id)
    else:
        remove_env_value("FILAMENT_CONTROL_USERS")
        print_warning(
            "Could not determine the principal (owner) from the token. Run "
            "`hermes pairing approve` once, or set FILAMENT_CONTROL_USERS."
        )


def connect(token: str, url: str | None = None, restart: bool = True) -> int:
    """Connect this agent to Filament with *token*. Returns an exit code.

    The non-interactive path behind ``hermes filament connect <token>``. It
    replaces the token prompt that ``hermes plugins install`` raises from
    ``requires_env``, so the whole install is two commands with no prompt:

        hermes plugins install filament-dm/filament-hermes --enable
        hermes filament connect fmcp_...

    Unlike the prompt, this overwrites an existing token, so it is also the
    reconnect path. It blocks while the agent is reserved — the user may still
    be naming the agent in the app — and connects as soon as that finishes.
    """
    token = (token or "").strip()
    if not token:
        print_warning("A token is required. Copy it from Filament's connect flow.")
        return 2

    resolved = (
        url or get_env_value("FILAMENT_MCP_URL") or "https://api.filament.dm/mcp/agents"
    ).strip().rstrip("/")

    print_header("Filament")
    _enable_plugin()
    _migrate_state_dir()

    # Validate before writing anything, so a bad token leaves a working
    # configuration intact.
    ready, principal_id = _wait_for_finalization(token, resolved)
    if not ready:
        return 1

    _persist(token, resolved, principal_id)
    print_success("Connected. Configuration saved.")

    if restart:
        _migrate_session_keys()
        _restart_gateway()
    else:
        print_info("Restart the gateway to load it: hermes gateway restart")
    return 0


def _restart_gateway() -> None:
    """Restart the gateway immediately, launched DETACHED so setup can exit.

    When no service manager (systemd/launchd) is configured, ``hermes gateway
    restart`` runs the gateway in the FOREGROUND — it prints its banner and
    never returns. Waiting on it (``subprocess.run``) hangs the installer until
    a timeout, and killing it on timeout would tear down the gateway we just
    started. So launch it in its own session with stdio detached and do NOT
    wait: setup returns to the shell immediately while the gateway keeps running
    in the background (logs go to ~/.hermes/logs/gateway.log). Under a service
    manager the command simply exits on its own, which is equally fine.
    """
    print_info("Restarting the gateway...")

    try:
        subprocess.Popen(
            ["hermes", "gateway", "restart"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        print_warning("'hermes' command not found. Restart manually:")
        print_info("hermes gateway restart")
        return

    print_info("Gateway restarting in the background...")

    # Brief, bounded health check so the installer can give a thumbs-up without
    # blocking on the (possibly foreground) restart. Give the gateway a moment
    # to come up, then ask `hermes gateway status` once — status is a quick,
    # non-daemonizing command, so capturing it with a short timeout is safe.
    time.sleep(3)
    try:
        result = subprocess.run(
            ["hermes", "gateway", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        print_info("Verify it came up with: hermes gateway status")
        return

    # `hermes gateway status` exits 0 whether up or down, so parse its output:
    # "✓ ... running" vs "✗ ... not running" / "... stopped". Check the
    # negative markers first ("not running" contains "running").
    out = result.stdout or ""
    low = out.lower()
    if "not running" in low or "stopped" in low or "✗" in out:
        print_info("Gateway is still starting; verify with: hermes gateway status")
    elif "running" in low or "✓" in out:
        print_success("Gateway is running.")
    else:
        print_info("Verify the gateway came up with: hermes gateway status")


def main() -> None:
    """Entry point for the filament-setup command."""
    print()
    print_header("filament-setup")

    _enable_plugin()
    _migrate_state_dir()
    print()
    ready = _run_interactive_setup()
    print()

    if ready:
        _migrate_session_keys()
        _restart_gateway()

    print()
    print_info("Setup complete." if ready else "Setup incomplete.")
    print_info("Check status: hermes gateway status")
    print_info("View logs:    tail -f ~/.hermes/logs/gateway.log")
    print()


if __name__ == "__main__":
    main()

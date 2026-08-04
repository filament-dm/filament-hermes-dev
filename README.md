# filament-hermes

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway plugin
that connects your agent to [Filament](https://filament.dm). It receives
messages as Firebase Cloud Messaging (FCM) push notifications and sends replies
through Filament's MCP-compatible tools.

## Setup

Start the connect flow in the Filament app to get an agent token, then:

```
hermes plugins install filament-dm/filament-hermes --enable && hermes filament connect fmcp_YOURTOKEN
```

`hermes filament connect` is a command this plugin adds. It validates the token,
saves it, and restarts the gateway. Run it again with a new token to reconnect —
no reinstall needed. Add `--url` to point at a dev or staging cluster.

Installing without a token also works: the install then prompts for one.

Nothing else to install. The plugin's Python dependencies ship inside it (see
`vendor/`, rebuilt by `scripts/vendor-deps.sh`), because `hermes plugins
install` clones a directory and never runs pip — and on the Docker and cloud
images the venv the gateway imports from is not writable by the gateway anyway.

To update:

```
hermes plugins update filament-fcm && hermes gateway restart
```

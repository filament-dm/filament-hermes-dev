# Refactoring the turn path into six phases

## Why

The invariants in this plugin are carefully reasoned and unusually well
commented. The problem is not the reasoning — it is that **the decisions and
the effects are interleaved, and the sequence exists only in prose.** You
cannot look at `adapter.py` and see the order things happen in; you have to
read three near-parallel 150-line methods and diff them in your head.

Concretely, as of the start of this work:

**Four dispatch sites, each hand-rolling the same handoff.**
`_handle_control_message`, `_wake`, `_maybe_greet`, and (via `_wake`) the
reaction path each independently did: set four ContextVars → apply session
keying → begin the status turn → `handle_message`. They did not agree.
`_maybe_greet` set **zero** ContextVars, so a backchannel greet ran with
`current_zone` at its `"data"` default and no session keying applied — a
control-plane turn presenting as data plane. Benign today only because the
greet prompt says "do not call any tools."

**Triage duplicated and divergent.** `_is_new_event` and the own-sender check
were copy-pasted in `_handle_push_message_turn` and `_handle_reaction_turn`.
`is_system_sender` existed in **only** the message path, so a `filament_god`
reaction could wake the agent where the equivalent message could not. Nine
separate `turn.skipped` log sites carried hand-written reason strings.

**A sentinel where normalization belongs.** `_wake` told a message from a
reaction by `data is None`, with a three-line comment defending why an empty
string is not the same as `None`.

**Load-bearing ordering that only comments enforce.** "Skip before
wake-policy, media-note, and breadcrumb work so no turn or API call is spent."
"Order matters for cost and safety." "Applied synchronously right before
dispatch: no await can interleave." All true, all invisible in the structure,
all re-derivable only by reading every path.

## The shape

Every inbound event — message, reaction, backchannel command, slash command,
synthetic greet — becomes one `InboundEvent` at the FCM boundary and runs
through the *same* fixed sequence. Administrative traffic branches off before
the pipeline and never enters it.

```python
async def run_turn(self, event: InboundEvent) -> None:
    if drop := triage(event, seen=self._seen, self_id=self._user_id):
        return self._log_drop(event, drop)          # 1. worth looking at?
    await self._server_config.sync()                #    every fresh read is after this
    route = self.route(event)                       # 2. which mode / zone / session?
    if route.handler is Handler.SLASH:
        return await self._slash.run(event, route)  #    never reaches inference
    if drop := await self.admit(event, route):      # 3. should we spend a turn?
        return self._log_drop(event, drop)
    grant = self.grant(route)                       # 4. which tools may it use?
    prompt = await self.assemble(event, route, grant)   # 5. build the context
    await self.dispatch(TurnPlan(event, route, grant, prompt))   # 6. hand to Hermes
```

That function is the deliverable. Nine lines; each phase name greps to exactly
one module.

| Phase | I/O allowed | Returns |
|---|---|---|
| 1 `triage` | **none** (pure) | `None` \| `Drop(reason)` |
| 2 `route` | file reads | `Route(zone, mode, session_scope, reply_target, handler)` |
| 3 `admit` | ≤1 API call | `None` \| `Drop(reason)` |
| 4 `grant` | file reads | `Grant(tools, hint)` |
| 5 `assemble` | ≤2 API calls, concurrent | `Prompt(text, breadcrumb)` |
| 6 `dispatch` | Hermes | — |

The cheap-to-expensive ordering is now **structural**. "Don't spend an API call
on a message we are going to skip" holds because `assemble` is phase 5 and
every drop happens by phase 3. The comment can go.

## Worked traces

### A. A mention in a shared channel — the happy path

`@agent what's the deploy status?` from `@alice:filament.dm` in `#eng`
(`!eng:filament.dm`, `$evt1`, top-level, `is_mention=true`).

| Phase | Decision |
|---|---|
| 1 triage | `$evt1` unseen ✓ · sender ≠ self ✓ · sender ≠ `@filament_god` ✓ → continue |
| — | config sync (TTL-cached) — everything below reads fresh files |
| 2 route | room ≠ backchannel → `zone=DATA`; no thread + `reply_style=thread` → `mode=CHANNEL`, `session_scope=("channel","!eng:filament.dm")`, reply threads off `$evt1` |
| 3 admit | server `is_mention` → `is_agent_mention` true → wake policy admits → continue; record `$evt1` as an engaged thread |
| 4 grant | `advanced_tool_controls` OFF (default) → `capabilities=None` (ungated), `hint=""` |
| 5 assemble | strip mention → `what's the deploy status?` · `get_thread` for media → none · instructions + `#eng` guidance read · breadcrumb: 12 unseen → envelope |
| 6 dispatch | `TurnContext(zone=data, caps=None, cursor_channel=!eng…, anchor=None)` → session keying → `begin_turn` → `handle_message` |

### B. An `@everyone` broadcast — dropped at phase 3

`@everyone standup in 5` in `#eng`.

| Phase | Decision |
|---|---|
| 1 triage | passes |
| 2 route | `zone=DATA`, `mode=CHANNEL` |
| 3 admit | `is_everyone_mention` → `is_agent_mention` returns **false** (one broadcast must not wake every agent in the channel at once) → wake policy default `mention` → `Drop("wake_policy")` |

Nothing is fetched. No `get_thread`, no `get_recent_messages`, no instructions
file read. Today that property depends on a reviewer honoring a comment; after
the refactor it is a consequence of `assemble` being phase 5.

### C. The agent's own 👀 marker coming back — dropped at phase 1

The adapter added 👀 to `$evt1` while handling trace A; the reaction returns as
a push.

| Phase | Decision |
|---|---|
| 1 triage | new event id, so dedupe passes → sender == self → `Drop("own_reaction")` |

Two independent guards catch this, both in phase 1: self-authorship, and 👀
being in `_PROCESSING_REACTIONS` (which covers the case where a *different*
user adds 👀 and the principal has configured it as a wake trigger). Both fire
before any file read and before the config sync. If either were missing the
agent would re-wake itself forever.

Today this trace also shows the divergence being fixed: the reaction path has
its own copy of the dedupe and self checks but **no** `is_system_sender` check,
so a `filament_god` reaction reaches phase 3 where the equivalent message
would have been dropped.

### D. A slash command — diverted at phase 2

`/fil-config #eng wake all` in the backchannel.

| Phase | Decision |
|---|---|
| 1 triage | passes |
| 2 route | room == backchannel → `zone=CONTROL`; lead-stripped body starts with `/fil-` and `slash_commands` is ON → `handler=SLASH` |
| — | driver hands off to the slash runtime and returns |

Phases 3–6 never run. "A `/fil-` message must never reach inference" becomes
one visible line in the driver instead of an early return buried 40 lines into
`_handle_control_message`.

## Module layout

Flat modules, not a `turn/` package: the test loaders build a fake
`hermes_filament_fcm` module with `__path__` and pre-register submodules by
dotted name, so a subpackage would need every test file to register
`hermes_filament_fcm.turn` as well. Flat also matches the existing
`slash.py` / `timeline.py` / `reactive.py`.

```
adapter.py       ~450   BasePlatformAdapter: connect stages, send(), FCM callback bridge
turn.py           ~80   run_turn — the driver + drop logging
events.py        ~120   InboundEvent, Drop, Route, Grant, TurnPlan            (stdlib)
triage.py         ~90   phase 1                                              (pure)
route.py         ~140   phase 2 — absorbs keying_and_reply, conversation_key  (pure)
admit.py         ~160   phase 3 — wake policy, mentions, engaged threads  (1 injected fetch)
grant.py          ~90   phase 4                                              (pure)
assemble.py      ~220   phase 5 — orchestrates framing.py + the two fetches
dispatch.py      ~110   phase 6
framing.py       ~200   ✅ every string the model sees                        (pure)
turn_context.py  ~130   ✅ the per-turn authority value                       (pure)
admin.py         ~350   ping/pong, invite, vouch, heartbeat, update check, greet
slash_runtime.py ~180   the isinstance chain → a dispatch table
```

`reactive.py` (1462 lines) wants the same treatment afterwards: `stores.py`,
`capabilities.py` (~470 lines of bundle/resolve logic).

## The five structural moves

1. **One `TurnContext`, one ContextVar.** ✅ Done. A dispatch site can no
   longer configure three-quarters of a turn.
2. **`Drop` is a value, not a `return`.** One logging site, so the structured-log
   reason vocabulary is a closed set and "do all paths handle every reason" is a
   table you read rather than two functions you diff.
3. **Triage is total and shared.** Dedupe, self-authorship, system sender and
   the processing-reaction guard in one function over the normalized event.
4. **The control plane goes through the pipeline too.** Control becomes
   `Route(zone=CONTROL, mode=BACKCHANNEL)` and `assemble` branches on zone to
   pick the framing. Deletes a whole parallel implementation of media-note +
   breadcrumb + framing + dispatch.
5. **`dispatch()` owns the handoff invariant.** One place, one comment:
   `activate(context)` → `_apply_session_keying()` → `begin_turn` →
   `handle_message`, with no `await` between the keying and the handoff.

A bonus falls out of move 4: `assemble`'s two fetches (media note, breadcrumb)
become one `asyncio.gather` instead of two sequential awaits at different call
sites.

## What this buys in tests

Every adapter-level test today pays ~40 lines of gateway-stub boilerplate —
`test_thread_follow_up`, `test_slash_adapter`, `test_system_notice_skip`,
`test_media_notes`, `test_session_keying` all carry their own copy — because
`adapter.py` cannot be imported without Hermes. Phases 1, 2, 4 and the framing
half of 5 are pure: they test with **zero** stubs, like `test_slash.py` and
`test_timeline.py` already do.

`tests/test_framing.py` (19 tests, 10 ms, no stubs) and
`tests/test_turn_context.py` (11 tests, no stubs) are what that looks like.
This is the real reason to do the split — it is what makes the security-relevant
parts of this code cheap to change safely.

One gotcha for future phase modules: a standalone-loaded module that defines a
`@dataclass` must be registered in `sys.modules` **before** `exec_module`, because
`dataclasses` resolves annotations through `sys.modules[cls.__module__]`. See the
loader in `tests/test_turn_context.py`.

## Sequencing

Not every step is equally safe, and it is worth being explicit about which kind
each one is:

- **Type A — provably no behavior change.** Byte-identical output, verifiable
  by differential test against the old code.
- **Type B — mechanical, API shape changes, behavior unchanged.** Call sites
  move; what the model and the server see does not.
- **Type C — deliberate behavior change.** Ships alone, with its own test.

| # | Step | Kind | State |
|---|---|---|---|
| 1 | `framing.py` — extract every model-facing string | A | ✅ done |
| 2 | `turn_context.py` — collapse the four ContextVars | B | ✅ done |
| 3 | `turn.py` + `events.py` — the driver and `InboundEvent`, with each phase a shim delegating to the existing code | B | next |
| 4 | Move the shim bodies out into `triage.py` / `route.py` / `admit.py` / `grant.py` / `assemble.py` / `dispatch.py`, one at a time | B | |
| 5 | Route the control plane through the pipeline; delete `_handle_control_message` | B | |
| 6 | Reaction path gains the system-notice check | **C** | |
| 7 | Greet turn becomes a control-zone turn with session keying applied | **C** | |
| 8 | `admin.py`; then split `reactive.py` | B | |

### What landed in steps 1–2

`framing.py` (216 lines) — the wake envelope, the control sender line,
`sanitize_meta`, the attachment note. Verified byte-identical against the old
inline code across 3,984 input combinations (hostile display names, multi-line
bodies, every optional-block combination), 0 mismatches. `tests/test_framing.py`
pins the exact bytes, including the invariant that untrusted event data is
always last.

`turn_context.py` (130 lines) — eight scattered `.set()` calls became two
`activate()` calls; ten duplicated `current_zone.get() != "control"` guards in
`__init__.py` became `not turn_context.is_control()`. The four ContextVars are
gone from `reactive.py`.

Suite went 544 → 575 tests with no new stub boilerplate: 30 of the 31 new tests
load a single stdlib-only module in three lines. `adapter.py` 2531 → 2440,
`reactive.py` 1462 → 1414.

Not fixed, on purpose: `_maybe_greet` still activates no context, so a
backchannel greet runs `zone="data"` with no session keying. It is now one
visibly missing line rather than four. That is step 7.

Pre-existing and untouched: `test_plugin_manifest_version_matches_pyproject`
fails on clean `main` (`plugin.yaml` 0.10.5 vs `pyproject.toml` 0.10.6), and
ruff reports 10 findings in files this work did not touch.

**On frontloading the mechanical moves.** Only some of them are genuinely
self-contained. Steps 1 and 2 are, and they were worth doing first because
`framing.py` is the safety net for everything after it and `turn_context.py`
is what makes a forgotten dispatch decision impossible rather than merely
unlikely.

Normalizing the event into an `InboundEvent` is *not* in that category, even
though it looks like the same kind of change. Its field set is determined by
what the six phases consume, so doing it before the driver exists means
guessing the shape, then writing a destructure-back layer inside each of the
three old handlers — and deleting both when step 4 lands. Introduce the
skeleton first (step 3, phases as delegating shims), then move code into the
shims. That way the driver — the artifact that delivers "glance at it and
understand the process" — exists and is reviewable from step 3 onward, and no
code is written twice.

The two Type C changes are deferred deliberately. They are the bugs this
refactor surfaced, and each deserves a commit that says so rather than riding
along inside a large move.

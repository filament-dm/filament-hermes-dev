# Turn path refactor

## Overview

This document plans a refactor of the inbound turn path in `adapter.py`.
The goal is a fixed sequence of six named phases, so that reading one function
tells you the whole order of operations for any inbound event.

`adapter.py` is 2440 lines and holds five unrelated jobs: the staged connect,
administrative traffic, the turn path, `send()`, and the status lifecycle.
This plan covers the turn path only.
Steps 1 and 2 are complete; see [Progress](#progress).

Terms used throughout:

Term | Meaning
--- | ---
Control plane | The principal's private backchannel. Messages there are commands.
Data plane | Every shared channel. An event is a wake-up signal, and its content is data, not instructions.
Turn | One dispatch of an event to the Hermes agent loop.
Phase | One numbered step of the turn path, with a declared input, output, and I/O budget.

## Problems in the current code

**Four dispatch sites implement the same handoff separately.**
`_handle_control_message`, `_wake`, `_maybe_greet`, and the reaction path each
set four ContextVars, apply session keying, open a status turn, and call
`handle_message`.
They do not agree.
`_maybe_greet` sets no ContextVars at all, so a backchannel greeting runs with
the trust zone at its `"data"` default and no session keying.

**Triage is duplicated and the copies have drifted.**
Event deduplication and the own-sender check appear in both the message path
and the reaction path.
The check that suppresses Filament's system notices appears only in the message
path, so a system reaction can wake the agent where the equivalent message
cannot.
Nine separate log sites carry hand-written skip reasons.

**A sentinel stands in for normalization.**
`_wake` distinguishes a message from a reaction by testing whether its `data`
argument is `None`, which requires a comment explaining why an empty string is
not the same as `None`.

**Ordering rules exist only in comments.**
Three examples: drop an event before doing any paid work; check the local
engagement record before the API call that classifies a sender; apply session
keying with no `await` between it and the handoff.
Each rule is correct and each is invisible in the structure of the code.

## The refactored pipeline

Every inbound event becomes one `InboundEvent` at the FCM boundary and runs
through the same six phases.
Administrative traffic branches off before phase 1 and never enters the
pipeline.

```python
async def run_turn(self, event: InboundEvent) -> None:
    # 1. Is the event worth looking at?
    if drop := triage(event, seen=self._seen, self_id=self._user_id):
        return self._log_drop(event, drop)
    # Every fresh read below sees the server-held config.
    await self._server_config.sync()
    # 2. Which mode, zone, and session does it belong to?
    route = self.route(event)
    if route.handler is Handler.SLASH:
        return await self._slash.run(event, route)   # never reaches inference
    # 3. Is it worth spending a turn on?
    if drop := await self.admit(event, route):
        return self._log_drop(event, drop)
    grant = self.grant(route)                        # 4. which tools apply?
    prompt = await self.assemble(event, route, grant)  # 5. build context
    await self.dispatch(                             # 6. hand off to Hermes
        TurnPlan(event, route, grant, prompt)
    )
```

Each phase name resolves to one module.

Phase | Name | I/O budget | Returns
--- | --- | --- | ---
1 | `triage` | None | `None` or `Drop(reason)`
2 | `route` | File reads | `Route(zone, mode, session_scope, reply_target, handler)`
3 | `admit` | 1 API call at most | `None` or `Drop(reason)`
4 | `grant` | File reads | `Grant(tools, hint)`
5 | `assemble` | 2 API calls at most, run concurrently | `Prompt(text, breadcrumb)`
6 | `dispatch` | Hands off to Hermes | Nothing

The phases run cheapest first.
Because every drop happens by phase 3 and the only network reads are in phases
3 and 5, an event the agent ignores costs no API calls and no file reads.
That property currently depends on a reviewer preserving the order of
statements inside a 150-line method.

## Example traces

### Trace A: a mention in a shared channel

Input: `@agent what's the deploy status?` from `@alice:filament.dm` in `#eng`,
event `$evt1`, top level, with the server's mention flag set.

Phase | Decision
--- | ---
1 `triage` | `$evt1` is unseen, the sender is not the agent, and the sender is not Filament's system user. Continue.
— | Sync the server-held config, so every read below sees current policy.
2 `route` | The room is not the backchannel, so the zone is data. No thread and the channel's default reply style give mode `CHANNEL`, session scope `("channel", "!eng:filament.dm")`, and a reply threaded off `$evt1`.
3 `admit` | The mention flag admits the event under the channel's wake policy. Continue, and record `$evt1` as a thread the agent is engaged in.
4 `grant` | The `advanced_tool_controls` flag is off, which is the default, so the turn is ungated and gets no capability hint.
5 `assemble` | Strip the mention, look up attachments, read the standing instructions and the channel's guidance, and count unseen history. Build the envelope.
6 `dispatch` | Activate the turn context, apply session keying, open the status turn, and call `handle_message`.

### Trace B: an @everyone broadcast, dropped at phase 3

Input: `@everyone standup in 5` in `#eng`.

Phase | Decision
--- | ---
1 `triage` | Passes.
2 `route` | Zone is data, mode is `CHANNEL`.
3 `admit` | An `@everyone` mention is not a mention of the agent, because one broadcast must not wake every agent in a channel at once. The channel's default wake policy needs a mention, so the event drops with reason `wake_policy`.

Nothing is fetched: no attachment lookup, no history read, no instructions file
read.

### Trace C: the agent's own processing marker, dropped at phase 1

The adapter adds a 👀 reaction to `$evt1` while handling trace A, and that
reaction arrives back as a push.

Phase | Decision
--- | ---
1 `triage` | The reaction is a new event, so deduplication passes, but the sender is the agent itself. Drops with reason `own_reaction`.

Two independent guards catch this case, and both are in phase 1: the
own-sender check, and a list of reactions the adapter itself adds.
The second guard covers a different user adding 👀 to a channel where the
principal configured 👀 as a wake trigger.
If either guard were missing, the agent would wake itself in a loop.

This trace also shows the drift described above.
The reaction path has its own copy of the deduplication and own-sender checks
but no system-notice check, so a system reaction reaches phase 3 where the
equivalent message would already have dropped.

### Trace D: a slash command, diverted at phase 2

Input: `/fil-config #eng wake all` in the backchannel.

Phase | Decision
--- | ---
1 `triage` | Passes.
2 `route` | The room is the backchannel, so the zone is control. The body starts with `/fil-` and the `slash_commands` flag is on, so the handler is `SLASH`.
— | The pipeline hands off to the slash runtime and returns. Phases 3 through 6 do not run.

Slash commands must never reach the model, in success or in failure.
In the pipeline that rule is one line in `run_turn`, rather than an early
return part way through the control-plane handler.

## Module layout

Use flat modules rather than a `turn/` package.
The test loaders build a stand-in `hermes_filament_fcm` module and register
submodules by dotted name, so a subpackage would need extra registration in
every test file.
Flat modules also match the existing `slash.py`, `timeline.py`, and
`reactive.py`.

Module | Lines | Contents
--- | --- | ---
`adapter.py` | ~450 | Connect stages, `send()`, and the FCM callback bridge
`turn.py` | ~80 | `run_turn` and drop logging
`events.py` | ~120 | `InboundEvent`, `Drop`, `Route`, `Grant`, `TurnPlan`
`triage.py` | ~90 | Phase 1
`route.py` | ~140 | Phase 2
`admit.py` | ~160 | Phase 3
`grant.py` | ~90 | Phase 4
`assemble.py` | ~220 | Phase 5
`dispatch.py` | ~110 | Phase 6
`framing.py` | 216 | Every string the model sees. Complete.
`turn_context.py` | 137 | The per-turn authority value. Complete.
`admin.py` | ~350 | Ping, invite, vouch, heartbeat, update check, greeting
`slash_runtime.py` | ~180 | The slash result dispatch table

`reactive.py` is 1414 lines and needs the same treatment afterwards, split into
`stores.py` and `capabilities.py`.

## Design changes

1.  **One turn context behind one ContextVar.**
    Complete.
    A dispatch site can no longer configure part of a turn's authority.

2.  **A drop is a value, not an early return.**
    Each phase returns `Drop(reason)` and `run_turn` logs it in one place.
    The set of skip reasons becomes closed and readable as a list, instead of
    nine string literals spread across two methods.

3.  **One shared triage function.**
    Deduplication, the own-sender check, the system-notice check, and the
    processing-reaction guard all run against the normalized event, so the
    message and reaction paths cannot drift apart again.

4.  **The control plane runs through the pipeline.**
    Control becomes `Route(zone=CONTROL, mode=BACKCHANNEL)`, and `assemble`
    selects framing by zone.
    This deletes a parallel implementation of attachment lookup, history
    counting, framing, and dispatch.
    It also lets `assemble` run its two lookups concurrently, which the
    current code cannot do because they sit in different methods.

5.  **`dispatch` owns the handoff.**
    One function activates the turn context, applies session keying, opens the
    status turn, and calls `handle_message`, in that order and with no `await`
    between the keying and the handoff.

## Testing

Adapter-level tests currently carry about 40 lines of stub setup each, in five
separate files, because `adapter.py` cannot be imported without Hermes
installed.

Phases 1, 2, and 4, and the framing part of phase 5, are pure functions.
They test with no stubs at all, the way `slash.py` and `timeline.py` already
do.
`tests/test_framing.py` and `tests/test_turn_context.py` are the first two
examples: 30 tests that each load one stdlib-only module in three lines.

Caution: a module loaded standalone must be registered in `sys.modules` before
`exec_module` if it defines a dataclass, because `dataclasses` resolves
annotations through `sys.modules[cls.__module__]`.
See the loader in `tests/test_turn_context.py`.

## Sequencing

Steps fall into three kinds, and the kind determines how each one is verified.

Kind | Meaning | Verification
--- | --- | ---
A | No behavior change | Differential test against the replaced code
B | Call sites change, behavior does not | Existing suite
C | Deliberate behavior change | Ships alone with a new test

Step | Description | Kind | Status
--- | --- | --- | ---
1 | `framing.py`: extract every model-facing string | A | Complete
2 | `turn_context.py`: collapse the four ContextVars | B | Complete
3 | `turn.py` and `events.py`: the driver and `InboundEvent`, with each phase a shim delegating to existing code | B | Next
4 | Move the shim bodies into the phase modules, one at a time | B | |
5 | Route the control plane through the pipeline and delete `_handle_control_message` | B | |
6 | Add the system-notice check to the reaction path | C | |
7 | Make the greeting a control-zone turn with session keying applied | C | |
8 | `admin.py`, then split `reactive.py` | B | |

Step 3 introduces the pipeline before moving any logic into it.
`InboundEvent` cannot be built earlier: its fields are determined by what the
six phases consume, so defining it first means guessing the shape and then
writing a layer inside each of the three current handlers to unpack it again.
Both would be deleted in step 4.

Steps 6 and 7 fix the two bugs this plan identifies.
They are deferred so that each arrives in a commit that describes it, rather
than inside a large mechanical change.

## Progress

Steps 1 and 2 are complete.

-   `framing.py` holds the wake envelope, the control-plane sender line,
    metadata sanitization, and the attachment note.
    A differential test against the replaced code covered 3,984 input
    combinations with no difference in output.
-   `turn_context.py` replaces four ContextVars with one frozen value.
    Two `activate()` calls replace eight scattered assignments, and ten
    repeated zone guards in `__init__.py` now call one predicate.
-   The suite grew from 544 to 575 tests with no new stub setup.

Two known items are unchanged on purpose.
The greeting still activates no turn context, which is step 7.
`test_plugin_manifest_version_matches_pyproject` fails on `main` because
`plugin.yaml` and `pyproject.toml` disagree on the version, which is unrelated
to this work.

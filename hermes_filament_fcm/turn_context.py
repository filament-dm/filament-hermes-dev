"""The per-turn context: everything the dispatch site tells the rest of the
process about the turn it is about to hand to Hermes.

Four separate facts used to live in four separate ContextVars, each set
independently at each dispatch site. That made "did this dispatch path set
all four?" a question you answered by reading every path — and one path
(the first-contact greet) answered no. Here they are ONE frozen value
behind ONE ContextVar, so a dispatch site either sets the turn's context or
it doesn't; it cannot set three quarters of it.

Who reads what:

- ``zone`` — the control-plane tools (``set_instructions``,
  ``set_wake_policy``, ``set_capabilities``, ``set_feature``,
  ``set_agent_config`` and their getters) refuse unless it is ``"control"``,
  so a shared-channel participant can never reconfigure the agent.
- ``capabilities`` — the ``pre_tool_call`` capability gate denies any tool
  outside the set. This is the *hard* half of the trust boundary that the
  envelope framing states softly.
- ``cursor_channel`` — the ``get_recent_messages`` tool proxy records a read
  cursor only for this room.
- ``reply_anchor`` — ``adapter.send`` threads an unaddressed reply under it.

The two defaults are deliberately asymmetric, and the asymmetry is the
whole fail-closed story:

- ``zone`` defaults to ``"data"`` — the LOW-privilege value. A turn that
  never set a context cannot edit policy.
- ``capabilities`` defaults to ``None``, meaning *ungated*, so that turns
  from outside this plugin (a plain CLI session in the same Hermes process)
  are not gated by a Filament policy they have nothing to do with.

Data-plane fail-closure is therefore the dispatch site's job, and only
applies while the ``advanced_tool_controls`` feature flag is on: with the
flag on, a data turn always gets an explicit set (an unlisted channel
resolves to the minimal default profile, never to ``None``). With the flag
off — the default — a data turn is ungated, exactly like an install that
predates the feature.

ContextVars are task-local, so concurrent turns don't race and no reset is
needed: the turn's task ends and its context goes with it.

Stdlib-only and standalone-loadable.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class TurnContext:
    """One turn's trust zone, tool grant, cursor authority and reply anchor.

    Frozen on purpose: a turn's authority is decided once, at dispatch, and
    nothing downstream may widen it. Use :func:`activate` to install one.
    """

    zone: str = "data"
    capabilities: frozenset[str] | None = None
    cursor_channel: str | None = None
    reply_anchor: tuple[str, str] | None = None

    @property
    def is_control(self) -> bool:
        return self.zone == "control"

    def with_capabilities(
        self, capabilities: frozenset[str] | None
    ) -> TurnContext:
        """A copy carrying a different grant (for tests and for a future
        escalation path); never mutates the active context."""
        return replace(self, capabilities=capabilities)


# What a task that never dispatched a Filament turn sees: data zone (no
# policy edits), ungated tools (not our turn to restrict), no cursor
# authority, no reply anchor.
DEFAULT = TurnContext()

# Every control turn is this same value, which is the point: "the
# backchannel keeps full capability, asserts no channel's read cursor, and
# threads nothing by default" is one immutable fact rather than four
# statements that a new dispatch path could get three-quarters right.
CONTROL = TurnContext(
    zone="control", capabilities=None, cursor_channel=None, reply_anchor=None
)

_current: contextvars.ContextVar[TurnContext] = contextvars.ContextVar(
    "filament_turn_context", default=DEFAULT
)


def data_turn(
    *,
    capabilities: frozenset[str] | None,
    cursor_channel: str | None,
    reply_anchor: tuple[str, str] | None,
) -> TurnContext:
    """A data-plane turn's context. All three arguments are keyword-only and
    required — a data turn must decide each of them explicitly, because for
    each one the safe value depends on the channel, not on a default."""
    return TurnContext(
        zone="data",
        capabilities=capabilities,
        cursor_channel=cursor_channel,
        reply_anchor=reply_anchor,
    )


def activate(ctx: TurnContext) -> None:
    """Install *ctx* as the current turn's context.

    Call this at the dispatch site, as late as possible and with no ``await``
    between it and the handoff to Hermes.
    """
    _current.set(ctx)


def current() -> TurnContext:
    """The active turn's context (:data:`DEFAULT` outside a Filament turn)."""
    return _current.get()


def is_control() -> bool:
    """True in a control-plane turn. The guard the ``set_*`` tools use."""
    return _current.get().is_control


def zone() -> str:
    """The active zone, for log lines."""
    return _current.get().zone

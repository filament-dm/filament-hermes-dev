"""Per-turn authority, held as one value behind one ContextVar.

Four separate facts used to live in four separate ContextVars, each set
independently at each dispatch site. That made "did this dispatch path set all
four?" a question answerable only by reading every path, and one path, the
first-contact greeting, answered no. Here they are one frozen value, so a
dispatch site either sets a turn's whole authority or none of it.

Each field has one reader:

zone
    The control-plane tools (set_instructions, set_wake_policy,
    set_capabilities, set_feature, set_agent_config, and their getters) refuse
    unless it is "control", so a shared-channel participant can never
    reconfigure the agent.
capabilities
    The pre_tool_call capability gate denies any tool outside the set. This is
    the hard half of the trust boundary that the envelope framing states
    softly.
cursor_channel
    The get_recent_messages tool proxy records a read cursor only for this
    room.
reply_anchor
    adapter.send threads an unaddressed reply under it.

The two defaults are asymmetric, and the asymmetry is the fail-closed design:

- zone defaults to "data", the low-privilege value, so a turn that never set a
  context cannot edit policy.
- capabilities defaults to None, meaning ungated, so that turns originating
  outside this plugin, such as a plain CLI session in the same Hermes process,
  are not gated by a Filament policy unrelated to them.

Data-plane fail-closure is therefore the dispatch site's job, and applies only
while the advanced_tool_controls feature flag is on. With the flag on, a data
turn always gets an explicit set, and an unlisted channel resolves to the
minimal default profile rather than to None. With the flag off, which is the
default, a data turn is ungated, exactly like an install predating the feature.

ContextVars are task-local, so concurrent turns do not race and no reset is
needed: a turn's task ends and its context goes with it.

Docstrings in this module use the descriptive mood throughout.

This module is stdlib-only and loads standalone. See tests/test_turn_context.py.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class TurnContext:
    """The authority granted to one turn.

    Frozen on purpose: a turn's authority is decided once, at dispatch, and
    nothing downstream may widen it. Install one with activate.

    Attributes:
        zone: "control" for the principal's backchannel, "data" for a shared
            channel.
        capabilities: The tool names this turn may call, or None for an ungated
            turn.
        cursor_channel: The room id whose read cursor this turn may record, or
            None to record none.
        reply_anchor: The (room_id, event_id) pair an unaddressed reply threads
            under, or None to post at the top level.
    """

    zone: str = "data"
    capabilities: frozenset[str] | None = None
    cursor_channel: str | None = None
    reply_anchor: tuple[str, str] | None = None

    @property
    def is_control(self) -> bool:
        return self.zone == "control"

    def with_capabilities(self, capabilities: frozenset[str] | None) -> TurnContext:
        """Returns a copy carrying a different tool grant.

        Args:
            capabilities: The replacement grant, or None for an ungated turn.

        Returns:
            A new TurnContext with every other field unchanged. The receiver is
            never modified, so this cannot widen an active turn's authority.
        """
        return replace(self, capabilities=capabilities)


# What a task that never dispatched a Filament turn sees: the data zone, so no
# policy edits; ungated tools, since a non-Filament turn is not this plugin's
# to restrict; no cursor authority; no reply anchor.
DEFAULT = TurnContext()

# Every control turn is this same value, which is the point. "The backchannel
# keeps full capability, asserts no channel's read cursor, and threads nothing
# by default" is one immutable fact rather than four statements a new dispatch
# path could get three-quarters right.
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
    """Builds a data-plane turn's context.

    No argument has a default, because for each field the safe value depends on
    the channel. A caller that omits one gets a TypeError rather than a quiet
    wrong answer.

    Args:
        capabilities: The tool names this turn may call, or None for an ungated
            turn.
        cursor_channel: The room id whose read cursor this turn may record, or
            None to record none.
        reply_anchor: The (room_id, event_id) pair to thread a reply under, or
            None to post at the top level.

    Returns:
        A TurnContext in the data zone.
    """
    return TurnContext(
        zone="data",
        capabilities=capabilities,
        cursor_channel=cursor_channel,
        reply_anchor=reply_anchor,
    )


def activate(ctx: TurnContext) -> None:
    """Installs a context as the current turn's authority.

    Call this at the dispatch site, as late as possible and with no await
    between it and the handoff to Hermes.

    Args:
        ctx: The context to install.
    """
    _current.set(ctx)


def current() -> TurnContext:
    """Returns the active turn's context, or DEFAULT outside a Filament turn."""
    return _current.get()


def is_control() -> bool:
    """Returns True in a control-plane turn.

    This is the guard the control-plane set_* tools use.
    """
    return _current.get().is_control


def zone() -> str:
    """Returns the active turn's zone, for log lines."""
    return _current.get().zone

"""Per-turn authority, held as one value behind one ContextVar.

These four facts used to live in four separate ContextVars, each set
independently at every dispatch site, and one dispatch site (the first-contact
greeting) set none of them. As one frozen value, a dispatch site either sets a
turn's whole authority or none of it.

Each field has one reader:

zone
    The control-plane tools (set_instructions, set_wake_policy,
    set_capabilities, set_feature, set_agent_config, and their getters) refuse
    unless it is Zone.CONTROL, so a shared-channel participant can never
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

The two defaults are asymmetric on purpose:

- zone defaults to Zone.DATA, the low-privilege value, so a turn that never set
  a context cannot edit policy.
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
from enum import Enum
from typing import Final


class Zone(str, Enum):
    """The trust plane a turn runs in.

    Compare with ``is``, as in ``ctx.zone is Zone.CONTROL``. Identity is exact,
    so a raw string in the field reads as not-control and the control-plane
    guards fail closed.

    Attributes:
        CONTROL: The principal's private backchannel, where a message is a
            command. Required by the control-plane set_* tools.
        DATA: A shared channel, where an event is a wake-up signal and its
            content is data rather than instructions.
    """

    CONTROL = "control"
    DATA = "data"

    def __str__(self) -> str:
        """Returns the bare value, such as "control".

        Needed because a str-mixin enum's inherited __str__ renders
        "Zone.CONTROL" instead, and it does so inconsistently across Python
        versions: 3.10 and earlier disagree with 3.11 and later on whether an
        f-string yields the value or the member name. The zone appears in log
        lines interpolated with %s, so pin it here rather than depending on the
        interpreter's version.
        """
        return self.value


@dataclass(frozen=True)
class TurnContext:
    """The authority granted to one turn.

    Frozen on purpose: a turn's authority is decided once, at dispatch, and
    nothing downstream may widen it. Install one with activate.

    Attributes:
        zone: Which trust plane the turn runs in.
        capabilities: The tool names this turn may call, or None for an ungated
            turn.
        cursor_channel: The room id whose read cursor this turn may record, or
            None to record none.
        reply_anchor: The (room_id, event_id) pair an unaddressed reply threads
            under, or None to post at the top level.
    """

    zone: Zone = Zone.DATA
    capabilities: frozenset[str] | None = None
    cursor_channel: str | None = None
    reply_anchor: tuple[str, str] | None = None

    def with_capabilities(self, capabilities: frozenset[str] | None) -> TurnContext:
        """Returns a copy carrying a different tool grant.

        Args:
            capabilities: The replacement grant, or None for an ungated turn.

        Returns:
            A new TurnContext with every other field unchanged. The receiver is
            never modified, so this cannot widen an active turn's authority.
        """
        return replace(self, capabilities=capabilities)


# The context of a task no Filament turn claimed: the data zone, so no policy
# edits; ungated tools, since a non-Filament turn is not this plugin's to
# restrict; no cursor authority; no reply anchor.
#
# Reaching this is legitimate rather than an error, so it is not named INVALID.
# A plain CLI session or another platform's turn in the same Hermes process
# lands here, and every field is correct for it. A Filament dispatch path that
# forgot to call activate lands here too, where the ungated capabilities are
# too generous. The two cases are indistinguishable, since both are just
# "nobody called activate", so fix that at the dispatch site.
UNCLAIMED: Final = TurnContext()

# Every control turn uses this value: full capability, no read-cursor
# authority, and no reply anchor.
CONTROL: Final = TurnContext(
    zone=Zone.CONTROL, capabilities=None, cursor_channel=None, reply_anchor=None
)

_current: contextvars.ContextVar[TurnContext] = contextvars.ContextVar(
    "filament_turn_context", default=UNCLAIMED
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
        A TurnContext in Zone.DATA.
    """
    return TurnContext(
        zone=Zone.DATA,
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
    """Returns the active turn's context, or UNCLAIMED outside a Filament turn."""
    return _current.get()

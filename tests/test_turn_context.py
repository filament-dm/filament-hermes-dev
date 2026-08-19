"""Tests for turn_context.py, the per-turn authority value.

Two of these were loose assertions in test_reactive.py while a turn's authority
lived in four independent ContextVars. Collapsing them into one frozen value
made a new invariant testable, and it is the invariant that motivated the
change: a dispatch site cannot set part of a turn's authority. Previously a
path that set the zone and forgot the capabilities was a silent bug, and
_maybe_greet was exactly that. Now there is one value, so a turn either has a
context or has the fail-closed default.

turn_context is stdlib-only and loads standalone, so these tests need no stubs.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"

_spec = importlib.util.spec_from_file_location(
    "turn_context", _PKG_DIR / "turn_context.py"
)
turn_context = importlib.util.module_from_spec(_spec)
# Registered BEFORE exec: @dataclass resolves annotations through
# sys.modules[cls.__module__], so a standalone-loaded module that defines a
# dataclass must be findable there first. Every phase module of the pipeline
# refactor that carries a dataclass needs the same two lines.
sys.modules["turn_context"] = turn_context
_spec.loader.exec_module(turn_context)


# ── The fail-closed default ──────────────────────────────────────────


def test_default_is_fail_closed_for_policy_edits():
    """A task that never dispatched a Filament turn cannot edit policy."""
    ctx = turn_context.current()
    assert ctx.zone == "data"
    assert ctx.is_control is False
    assert turn_context.is_control() is False


def test_default_is_ungated_for_tools():
    """The capabilities default is ungated, deliberately not fail-closed.

    A plain CLI session in the same Hermes process must not be restricted by a
    Filament channel policy. Data-plane fail-closure is the dispatch site's
    job, and applies only while the advanced_tool_controls flag is on. With the
    flag off, which is the default, a data turn is ungated too.
    """
    assert turn_context.current().capabilities is None


def test_default_asserts_no_cursor_and_no_anchor():
    ctx = turn_context.current()
    assert ctx.cursor_channel is None
    assert ctx.reply_anchor is None


# ── The control turn is one immutable fact ───────────────────────────


def test_control_constant_carries_full_authority():
    assert turn_context.CONTROL.zone == "control"
    assert turn_context.CONTROL.is_control is True
    # Full capability (ungated), no channel's cursor to assert, nothing to
    # thread under by default.
    assert turn_context.CONTROL.capabilities is None
    assert turn_context.CONTROL.cursor_channel is None
    assert turn_context.CONTROL.reply_anchor is None


def test_activating_control_permits_policy_edits():
    async def main():
        turn_context.activate(turn_context.CONTROL)
        assert turn_context.is_control() is True
        assert turn_context.zone() == "control"

    asyncio.run(main())


# ── data_turn requires every decision to be explicit ─────────────────


def test_data_turn_requires_all_three_decisions():
    """Every field must be passed explicitly.

    For each field the safe value depends on the channel, so a caller that
    omits one gets a TypeError rather than a quiet wrong answer.
    """
    for missing in ("capabilities", "cursor_channel", "reply_anchor"):
        kwargs = {
            "capabilities": frozenset({"post"}),
            "cursor_channel": "!room:s",
            "reply_anchor": ("!room:s", "$e"),
        }
        del kwargs[missing]
        try:
            turn_context.data_turn(**kwargs)
        except TypeError:
            continue
        raise AssertionError(f"data_turn accepted a missing {missing}")


def test_data_turn_never_yields_the_control_zone():
    ctx = turn_context.data_turn(
        capabilities=None, cursor_channel=None, reply_anchor=None
    )
    assert ctx.zone == "data"
    assert ctx.is_control is False


# ── Atomicity: the invariant the collapse exists to enforce ──────────


def test_a_turn_cannot_be_partly_configured():
    """Activating a context replaces every field at once.

    No field can survive from a previous turn or from a dispatch path that
    forgot to set it. This is the invariant the collapse exists to enforce.
    """

    async def main():
        turn_context.activate(
            turn_context.data_turn(
                capabilities=frozenset({"post"}),
                cursor_channel="!a:s",
                reply_anchor=("!a:s", "$1"),
            )
        )
        turn_context.activate(turn_context.CONTROL)
        ctx = turn_context.current()
        # Not one field of the data turn survived.
        assert (ctx.zone, ctx.capabilities, ctx.cursor_channel, ctx.reply_anchor) == (
            "control",
            None,
            None,
            None,
        )

    asyncio.run(main())


def test_context_is_frozen_so_downstream_cannot_widen_authority():
    ctx = turn_context.data_turn(
        capabilities=frozenset({"post"}), cursor_channel=None, reply_anchor=None
    )
    for field, value in (
        ("zone", "control"),
        ("capabilities", None),
        ("cursor_channel", "!x:s"),
    ):
        try:
            setattr(ctx, field, value)
        except Exception:
            continue
        raise AssertionError(f"{field} was mutable")


def test_with_capabilities_copies_rather_than_mutating():
    ctx = turn_context.data_turn(
        capabilities=frozenset({"post"}), cursor_channel="!a:s", reply_anchor=None
    )
    narrowed = ctx.with_capabilities(frozenset())
    assert ctx.capabilities == frozenset({"post"})  # original untouched
    assert narrowed.capabilities == frozenset()
    assert narrowed.cursor_channel == "!a:s"  # everything else carried over


# ── Task-locality ────────────────────────────────────────────────────


def test_concurrent_turns_do_not_race():
    """Task-local storage lets two channels' turns run at once.

    Each turn keeps its own grant, which is what makes concurrent dispatch
    safe.
    """

    async def turn(name, caps, out):
        turn_context.activate(
            turn_context.data_turn(
                capabilities=frozenset(caps), cursor_channel=name, reply_anchor=None
            )
        )
        await asyncio.sleep(0)  # yield: the other turn activates here
        ctx = turn_context.current()
        out[name] = (ctx.capabilities, ctx.cursor_channel)

    async def main():
        out = {}
        await asyncio.gather(
            turn("!a:s", {"post"}, out), turn("!b:s", {"read_history"}, out)
        )
        assert out["!a:s"] == (frozenset({"post"}), "!a:s")
        assert out["!b:s"] == (frozenset({"read_history"}), "!b:s")

    asyncio.run(main())

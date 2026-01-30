"""Tests for session_card.py - session card data structures."""
from __future__ import annotations

import time

import pytest

from takopi.model import InputRequestEvent
from takopi.session_card import (
    ActivityItem,
    AgentBadge,
    BADGE_SYMBOLS,
    PendingInput,
    SessionCardBuilder,
    SessionCardState,
    STATUS_SYMBOLS,
    format_badge,
    format_activity_item,
)


class TestAgentBadge:
    def test_badge_creation(self) -> None:
        badge = AgentBadge(
            engine="codex",
            status="active",
            step_count=5,
            last_activity=time.time(),
        )

        assert badge.engine == "codex"
        assert badge.status == "active"
        assert badge.step_count == 5

    def test_badge_is_immutable(self) -> None:
        badge = AgentBadge(engine="claude", status="done")

        with pytest.raises(AttributeError):
            badge.status = "error"  # type: ignore[misc]


class TestActivityItem:
    def test_activity_item_creation(self) -> None:
        item = ActivityItem(
            timestamp=time.time(),
            engine="codex",
            kind="action",
            summary="reading file.py",
            detail={"path": "file.py"},
        )

        assert item.engine == "codex"
        assert item.kind == "action"
        assert item.summary == "reading file.py"
        assert item.detail == {"path": "file.py"}


class TestPendingInput:
    def test_pending_input_creation(self) -> None:
        pending = PendingInput(
            request_id="req-123",
            question="What should be the max file size?",
            source="codex",
            urgency="normal",
            options=("1MB", "5MB", "10MB"),
            context="Configuring file uploads",
        )

        assert pending.request_id == "req-123"
        assert pending.question == "What should be the max file size?"
        assert pending.source == "codex"
        assert pending.urgency == "normal"
        assert pending.options == ("1MB", "5MB", "10MB")


class TestSessionCardState:
    def test_state_creation(self) -> None:
        badge = AgentBadge(engine="codex", status="active")
        state = SessionCardState(
            session_id="session-123",
            started_at=time.time(),
            badges=(badge,),
            primary_engine="codex",
            activity_items=(),
        )

        assert state.session_id == "session-123"
        assert state.primary_engine == "codex"
        assert len(state.badges) == 1

    def test_is_multi_agent_single(self) -> None:
        badge = AgentBadge(engine="codex", status="active")
        state = SessionCardState(
            session_id="s1",
            started_at=time.time(),
            badges=(badge,),
            primary_engine="codex",
            activity_items=(),
        )

        assert not state.is_multi_agent

    def test_is_multi_agent_multiple(self) -> None:
        badges = (
            AgentBadge(engine="codex", status="active"),
            AgentBadge(engine="claude", status="waiting"),
        )
        state = SessionCardState(
            session_id="s1",
            started_at=time.time(),
            badges=badges,
            primary_engine="codex",
            activity_items=(),
        )

        assert state.is_multi_agent

    def test_has_pending_inputs(self) -> None:
        badge = AgentBadge(engine="codex", status="active")
        pending = PendingInput(
            request_id="r1",
            question="Q?",
            source="codex",
            urgency="normal",
        )
        state = SessionCardState(
            session_id="s1",
            started_at=time.time(),
            badges=(badge,),
            primary_engine="codex",
            activity_items=(),
            pending_inputs=(pending,),
        )

        assert state.has_pending_inputs

    def test_is_complete(self) -> None:
        badge = AgentBadge(engine="codex", status="done")
        state = SessionCardState(
            session_id="s1",
            started_at=time.time(),
            badges=(badge,),
            primary_engine="codex",
            activity_items=(),
            status="done",
        )

        assert state.is_complete

    def test_not_complete_when_working(self) -> None:
        badge = AgentBadge(engine="codex", status="active")
        state = SessionCardState(
            session_id="s1",
            started_at=time.time(),
            badges=(badge,),
            primary_engine="codex",
            activity_items=(),
            status="working",
        )

        assert not state.is_complete


class TestSessionCardBuilder:
    def test_builder_initialization(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )

        # Primary engine badge should be created automatically
        assert "codex" in builder._badges
        assert builder._badges["codex"].status == "active"

    def test_add_agent(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )

        builder.add_agent("claude", status="waiting")

        assert "claude" in builder._badges
        assert builder._badges["claude"].status == "waiting"

    def test_update_agent_status(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )
        builder.add_agent("claude")

        builder.update_agent_status("claude", "done")

        assert builder._badges["claude"].status == "done"

    def test_increment_step(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )

        builder.increment_step("codex")
        builder.increment_step("codex")

        assert builder._badges["codex"].step_count == 2

    def test_increment_step_new_engine(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )

        builder.increment_step("claude")

        assert "claude" in builder._badges
        assert builder._badges["claude"].step_count == 1

    def test_add_activity(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )

        builder.add_activity("codex", "action", "reading file")

        assert len(builder._activity) == 1
        assert builder._activity[0].summary == "reading file"

    def test_activity_truncation(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
            max_activity_items=3,
        )

        for i in range(5):
            builder.add_activity("codex", "action", f"step {i}")

        assert len(builder._activity) == 3
        assert builder._activity[0].summary == "step 2"
        assert builder._activity[-1].summary == "step 4"

    def test_add_pending_input(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )
        event = InputRequestEvent(
            engine="codex",
            request_id="r1",
            question="Q?",
            source="codex",
            urgency="normal",
        )

        builder.add_pending_input(event)

        assert "r1" in builder._pending_inputs
        assert builder._status == "waiting_input"

    def test_remove_pending_input(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )
        event = InputRequestEvent(
            engine="codex",
            request_id="r1",
            question="Q?",
            source="codex",
            urgency="normal",
        )
        builder.add_pending_input(event)

        builder.remove_pending_input("r1")

        assert "r1" not in builder._pending_inputs
        assert builder._status == "working"

    def test_set_complete_ok(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )

        builder.set_complete(ok=True)

        assert builder._status == "done"
        assert builder._badges["codex"].status == "done"

    def test_set_complete_with_error(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )

        builder.set_complete(error="Something went wrong")

        assert builder._status == "error"
        assert builder._error_message == "Something went wrong"

    def test_set_cancelled(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )

        builder.set_cancelled()

        assert builder._status == "cancelled"

    def test_build_creates_immutable_state(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )
        builder.add_agent("claude")
        builder.add_activity("codex", "action", "reading")
        builder.set_context("project: takopi")

        state = builder.build()

        assert isinstance(state, SessionCardState)
        assert state.session_id == "s1"
        assert len(state.badges) == 2
        assert state.context_line == "project: takopi"

    def test_build_truncates_activity(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )
        for i in range(10):
            builder.add_activity("codex", "action", f"step {i}")

        state = builder.build(max_visible_activity=3)

        assert len(state.activity_items) == 3
        assert state.activity_truncated
        assert state.activity_total == 10

    def test_build_sorts_badges_primary_first(self) -> None:
        builder = SessionCardBuilder(
            session_id="s1",
            started_at=time.time(),
            primary_engine="codex",
        )
        builder.add_agent("claude")
        builder.add_agent("pi")

        state = builder.build()

        # Primary engine should be first
        assert state.badges[0].engine == "codex"


class TestFormatBadge:
    def test_format_known_engine(self) -> None:
        badge = AgentBadge(engine="codex", status="active")
        formatted = format_badge(badge)

        assert BADGE_SYMBOLS["codex"] in formatted
        assert STATUS_SYMBOLS["active"] in formatted
        assert "codex" in formatted

    def test_format_unknown_engine(self) -> None:
        badge = AgentBadge(engine="unknown_engine", status="done")
        formatted = format_badge(badge)

        assert "unknown_engine" in formatted
        assert STATUS_SYMBOLS["done"] in formatted


class TestFormatActivityItem:
    def test_format_with_engine(self) -> None:
        item = ActivityItem(
            timestamp=time.time(),
            engine="codex",
            kind="action",
            summary="reading file",
        )
        formatted = format_activity_item(item, show_engine=True)

        assert "[codex]" in formatted
        assert "reading file" in formatted

    def test_format_without_engine(self) -> None:
        item = ActivityItem(
            timestamp=time.time(),
            engine="codex",
            kind="action",
            summary="reading file",
        )
        formatted = format_activity_item(item, show_engine=False)

        assert "[codex]" not in formatted
        assert "reading file" in formatted

"""Session card data structures for unified Telegram experience.

The session card is a unified UI component that works the same whether
there's 1 agent or N agents working on a task.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .model import EngineId, InputRequestEvent, ResumeToken


@dataclass(frozen=True, slots=True)
class AgentBadge:
    """Represents an agent's status in the session card.

    Badges are shown as colored indicators with status symbols.
    """

    engine: str
    status: Literal["active", "waiting", "done", "error"]
    step_count: int = 0
    last_activity: float | None = None


@dataclass(frozen=True, slots=True)
class ActivityItem:
    """A single item in the session activity feed.

    Activity items show what each agent is doing, with timestamps
    and color-coding by agent.
    """

    timestamp: float
    engine: str
    kind: str  # "action", "input_answered", "subagent_spawned", "pane_activity", etc.
    summary: str
    detail: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PendingInput:
    """An input request waiting for user response.

    Pending inputs are shown inline in the session card with
    response buttons.
    """

    request_id: str
    question: str
    source: str  # engine that asked
    urgency: Literal["low", "normal", "high", "critical"]
    options: tuple[str, ...] | None = None
    context: str | None = None
    received_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class SessionCardState:
    """Complete state for rendering a unified session card.

    The session card scales from 1 to N agents using the same
    structure. Simple tasks show minimal UI; complex tasks can
    expand to show more detail.
    """

    # Core identity
    session_id: str
    started_at: float

    # Multi-agent tracking
    badges: tuple[AgentBadge, ...]
    primary_engine: str

    # Activity feed
    activity_items: tuple[ActivityItem, ...]
    activity_truncated: bool = False
    activity_total: int = 0

    # Pending inputs (embedded in card)
    pending_inputs: tuple[PendingInput, ...] = ()

    # Context
    context_line: str | None = None
    resume_line: str | None = None

    # State
    status: Literal["working", "waiting_input", "done", "cancelled", "error"] = "working"
    error_message: str | None = None

    @property
    def is_multi_agent(self) -> bool:
        """True if multiple agents are involved."""
        return len(self.badges) > 1

    @property
    def has_pending_inputs(self) -> bool:
        """True if there are questions waiting for response."""
        return len(self.pending_inputs) > 0

    @property
    def is_complete(self) -> bool:
        """True if the session has finished."""
        return self.status in ("done", "cancelled", "error")


@dataclass(slots=True)
class SessionCardBuilder:
    """Mutable builder for constructing SessionCardState.

    Use this to incrementally build session state as events arrive,
    then call build() to get an immutable SessionCardState.
    """

    session_id: str
    started_at: float
    primary_engine: str

    _badges: dict[str, AgentBadge] = field(default_factory=dict)
    _activity: list[ActivityItem] = field(default_factory=list)
    _pending_inputs: dict[str, PendingInput] = field(default_factory=dict)
    _context_line: str | None = None
    _resume_line: str | None = None
    _status: Literal["working", "waiting_input", "done", "cancelled", "error"] = "working"
    _error_message: str | None = None

    max_activity_items: int = 50

    def __post_init__(self) -> None:
        # Initialize primary engine badge
        self._badges[self.primary_engine] = AgentBadge(
            engine=self.primary_engine,
            status="active",
            step_count=0,
            last_activity=time.time(),
        )

    def add_agent(
        self,
        engine: str,
        *,
        status: Literal["active", "waiting", "done", "error"] = "active",
    ) -> None:
        """Add or update an agent badge."""
        existing = self._badges.get(engine)
        step_count = existing.step_count if existing else 0
        self._badges[engine] = AgentBadge(
            engine=engine,
            status=status,
            step_count=step_count,
            last_activity=time.time(),
        )

    def update_agent_status(
        self,
        engine: str,
        status: Literal["active", "waiting", "done", "error"],
    ) -> None:
        """Update an agent's status."""
        if engine in self._badges:
            old = self._badges[engine]
            self._badges[engine] = AgentBadge(
                engine=engine,
                status=status,
                step_count=old.step_count,
                last_activity=time.time(),
            )

    def increment_step(self, engine: str) -> None:
        """Increment an agent's step count."""
        if engine in self._badges:
            old = self._badges[engine]
            self._badges[engine] = AgentBadge(
                engine=engine,
                status=old.status,
                step_count=old.step_count + 1,
                last_activity=time.time(),
            )
        else:
            self._badges[engine] = AgentBadge(
                engine=engine,
                status="active",
                step_count=1,
                last_activity=time.time(),
            )

    def add_activity(
        self,
        engine: str,
        kind: str,
        summary: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Add an activity item to the feed."""
        item = ActivityItem(
            timestamp=time.time(),
            engine=engine,
            kind=kind,
            summary=summary,
            detail=detail,
        )
        self._activity.append(item)

        # Trim old items if needed
        if len(self._activity) > self.max_activity_items:
            self._activity = self._activity[-self.max_activity_items :]

    def add_pending_input(self, event: InputRequestEvent) -> None:
        """Add a pending input request."""
        self._pending_inputs[event.request_id] = PendingInput(
            request_id=event.request_id,
            question=event.question,
            source=event.source,
            urgency=event.urgency,
            options=tuple(event.options) if event.options else None,
            context=event.context,
        )
        self._status = "waiting_input"

    def remove_pending_input(self, request_id: str) -> None:
        """Remove a pending input (answered or skipped)."""
        self._pending_inputs.pop(request_id, None)
        if not self._pending_inputs:
            self._status = "working"

    def set_context(self, context_line: str | None) -> None:
        """Set the context line."""
        self._context_line = context_line

    def set_resume(self, resume_line: str | None) -> None:
        """Set the resume line."""
        self._resume_line = resume_line

    def set_complete(
        self,
        *,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        """Mark the session as complete."""
        if error:
            self._status = "error"
            self._error_message = error
        elif ok:
            self._status = "done"
        else:
            self._status = "error"

        # Mark all agents as done
        for engine in list(self._badges.keys()):
            self.update_agent_status(engine, "done")

    def set_cancelled(self) -> None:
        """Mark the session as cancelled."""
        self._status = "cancelled"

    def build(self, *, max_visible_activity: int = 5) -> SessionCardState:
        """Build an immutable SessionCardState."""
        # Sort badges: primary first, then by last activity
        sorted_badges = sorted(
            self._badges.values(),
            key=lambda b: (b.engine != self.primary_engine, -(b.last_activity or 0)),
        )

        # Get recent activity
        visible_activity = self._activity[-max_visible_activity:]
        truncated = len(self._activity) > max_visible_activity

        return SessionCardState(
            session_id=self.session_id,
            started_at=self.started_at,
            badges=tuple(sorted_badges),
            primary_engine=self.primary_engine,
            activity_items=tuple(visible_activity),
            activity_truncated=truncated,
            activity_total=len(self._activity),
            pending_inputs=tuple(self._pending_inputs.values()),
            context_line=self._context_line,
            resume_line=self._resume_line,
            status=self._status,
            error_message=self._error_message,
        )


# Badge rendering constants
BADGE_SYMBOLS: dict[str, str] = {
    "claude": "\U0001F7E3",  # Purple circle
    "codex": "\U0001F7E2",  # Green circle
    "opencode": "\U0001F535",  # Blue circle
    "pi": "\U0001F7E0",  # Orange circle
    "liaison": "\U0001F7E1",  # Yellow circle
}

STATUS_SYMBOLS: dict[str, str] = {
    "active": "\u25B6",  # Play symbol ▶
    "waiting": "\u23F8",  # Pause symbol ⏸
    "done": "\u2713",  # Checkmark ✓
    "error": "\u2717",  # X mark ✗
}

ACTIVITY_SYMBOLS: dict[str, str] = {
    "action": "\u25B8",  # Right arrow ▸
    "input_answered": "\u2713",  # Checkmark ✓
    "subagent_spawned": "\u2795",  # Plus ➕
    "pane_activity": "\u25B8",  # Right arrow ▸
    "warning": "\u26A0",  # Warning ⚠
    "error": "\u2717",  # X mark ✗
    "complete": "\u2713",  # Checkmark ✓
}


def format_badge(badge: AgentBadge) -> str:
    """Format a badge for display in Telegram."""
    color = BADGE_SYMBOLS.get(badge.engine, "\u26AB")  # Default: black circle
    status = STATUS_SYMBOLS.get(badge.status, "")
    return f"{color}{status}{badge.engine}"


def format_activity_item(item: ActivityItem, *, show_engine: bool = True) -> str:
    """Format an activity item for display."""
    symbol = ACTIVITY_SYMBOLS.get(item.kind, "\u2022")  # Default: bullet
    engine_tag = f"[{item.engine}] " if show_engine else ""
    return f"{symbol} {engine_tag}{item.summary}"

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from .model import (
    Action,
    ActionEvent,
    InputRequestEvent,
    ResumeToken,
    StartedEvent,
    TakopiEvent,
)


@dataclass(frozen=True, slots=True)
class ActionState:
    action: Action
    phase: str
    ok: bool | None
    display_phase: str
    completed: bool
    first_seen: int
    last_update: int


@dataclass(frozen=True, slots=True)
class InputRequestState:
    """State of a pending input request from a liaison."""

    request_id: str
    question: str
    source: str
    urgency: str
    seen_at: int


@dataclass(frozen=True, slots=True)
class ProgressState:
    engine: str
    action_count: int
    actions: tuple[ActionState, ...]
    resume: ResumeToken | None
    resume_line: str | None
    context_line: str | None
    input_requests: tuple[InputRequestState, ...] = ()


class ProgressTracker:
    def __init__(self, *, engine: str) -> None:
        self.engine = engine
        self.resume: ResumeToken | None = None
        self.action_count = 0
        self._actions: dict[str, ActionState] = {}
        self._input_requests: dict[str, InputRequestState] = {}
        self._seq = 0

    def note_event(self, event: TakopiEvent) -> bool:
        match event:
            case StartedEvent(resume=resume):
                self.resume = resume
                return True
            case ActionEvent(action=action, phase=phase, ok=ok):
                if action.kind == "turn":
                    return False
                action_id = str(action.id or "")
                if not action_id:
                    return False
                completed = phase == "completed"
                existing = self._actions.get(action_id)
                has_open = existing is not None and not existing.completed
                is_update = phase == "updated" or (phase == "started" and has_open)
                display_phase = "updated" if is_update and not completed else phase

                self._seq += 1
                seq = self._seq

                if existing is None:
                    self.action_count += 1
                    first_seen = seq
                else:
                    first_seen = existing.first_seen
                self._actions[action_id] = ActionState(
                    action=action,
                    phase=phase,
                    ok=ok,
                    display_phase=display_phase,
                    completed=completed,
                    first_seen=first_seen,
                    last_update=seq,
                )
                return True
            case InputRequestEvent(
                request_id=request_id,
                question=question,
                source=source,
                urgency=urgency,
            ):
                self._seq += 1
                self._input_requests[request_id] = InputRequestState(
                    request_id=request_id,
                    question=question,
                    source=source,
                    urgency=urgency,
                    seen_at=self._seq,
                )
                return True
            case _:
                return False

    def clear_input_request(self, request_id: str) -> None:
        """Remove an input request after it has been answered."""
        self._input_requests.pop(request_id, None)

    def set_resume(self, resume: ResumeToken | None) -> None:
        if resume is not None:
            self.resume = resume

    def snapshot(
        self,
        *,
        resume_formatter: Callable[[ResumeToken], str] | None = None,
        context_line: str | None = None,
    ) -> ProgressState:
        resume_line: str | None = None
        if self.resume is not None and resume_formatter is not None:
            resume_line = resume_formatter(self.resume)
        actions = tuple(
            sorted(self._actions.values(), key=lambda item: item.first_seen)
        )
        input_requests = tuple(
            sorted(self._input_requests.values(), key=lambda item: item.seen_at)
        )
        return ProgressState(
            engine=self.engine,
            action_count=self.action_count,
            actions=actions,
            resume=self.resume,
            resume_line=resume_line,
            context_line=context_line,
            input_requests=input_requests,
        )

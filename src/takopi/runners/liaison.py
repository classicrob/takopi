"""Liaison runner that operates subagents via tmux.

The liaison uses Claude Code as a "brain" that interprets natural language
and orchestrates other Claude Code/Codex instances running in tmux panes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..backends import EngineBackend, EngineConfig
from ..events import EventFactory
from ..logging import get_logger
from ..model import (
    Action,
    EngineId,
    InputRequestEvent,
    InputResponseEvent,
    ResumeToken,
    TakopiEvent,
)
from ..runner import BaseRunner, ResumeTokenMixin, Runner
from .escalation import EscalationPolicy

ENGINE: EngineId = "liaison"

logger = get_logger(__name__)

_RESUME_RE = re.compile(
    r"(?im)^\s*`?liaison\s+--session\s+(?P<token>[^`\s]+)`?\s*$"
)

# Patterns for detecting questions in subagent output
_QUESTION_PATTERNS = [
    re.compile(r"(?:Do you want|Would you like|Should I|Can I|May I)\s+.+\?", re.I),
    re.compile(r"\?\s*$"),
    re.compile(r"(?:y/n|yes/no|Y/N)\s*[:>]?\s*$"),
    re.compile(r"(?:confirm|proceed|continue)\s*\?", re.I),
    re.compile(r"Press Enter to continue", re.I),
]


@dataclass(slots=True)
class TmuxPane:
    """Represents a tmux pane running a subagent."""

    session_name: str
    window_index: int
    pane_index: int
    engine: EngineId
    role: str  # "liaison" or "worker"
    subagent_resume: str | None = None
    last_capture_hash: str = ""
    pending_input_request: str | None = None


@dataclass(slots=True)
class LiaisonStreamState:
    """State maintained during a liaison run."""

    factory: EventFactory = field(default_factory=lambda: EventFactory(ENGINE))
    session_id: str | None = None
    tmux_session: str | None = None
    panes: dict[str, TmuxPane] = field(default_factory=dict)
    pending_requests: dict[str, InputRequestEvent] = field(default_factory=dict)
    note_seq: int = 0
    request_seq: int = 0
    coordination_folder: Path | None = None
    completed: bool = False
    final_answer: str = ""


@dataclass(slots=True)
class LiaisonRunner(ResumeTokenMixin, BaseRunner):
    """Runner that operates subagents via tmux.

    The liaison spawns a tmux session, runs Claude Code as the orchestrating
    agent, and can spawn additional panes for worker subagents. It monitors
    pane output via capture-pane and sends input via send-keys.
    """

    engine: EngineId = ENGINE
    resume_re: re.Pattern[str] = _RESUME_RE

    coordination_folder: Path = field(
        default_factory=lambda: Path.home() / ".takopi" / "liaison"
    )
    poll_interval_s: float = 0.5
    capture_lines: int = 50
    escalation_policy: EscalationPolicy = field(default_factory=EscalationPolicy)

    # Command to run the liaison Claude Code instance
    liaison_cmd: str = "claude"
    liaison_args: list[str] = field(default_factory=list)

    logger = logger

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != ENGINE:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`liaison --session {token.value}`"

    async def run_impl(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[TakopiEvent]:
        state = LiaisonStreamState()
        state.coordination_folder = self.coordination_folder
        self._ensure_folders(state)

        if resume is not None:
            restored = await self._restore_session(resume.value, state)
            if not restored:
                yield state.factory.completed_error(
                    error=f"Failed to restore liaison session {resume.value}",
                    resume=resume,
                )
                return
            state.session_id = resume.value
        else:
            session_id = self._generate_session_id()
            state.session_id = session_id
            tmux_session = await self._create_tmux_session(session_id)
            if tmux_session is None:
                yield state.factory.completed_error(
                    error="Failed to create tmux session",
                    resume=None,
                )
                return
            state.tmux_session = tmux_session

        token = ResumeToken(engine=ENGINE, value=state.session_id)
        yield state.factory.started(
            token,
            title="Liaison Agent",
            meta={
                "tmux_session": state.tmux_session,
                "coordination_folder": str(state.coordination_folder),
            },
        )

        # Spawn the liaison brain pane with Claude Code
        liaison_pane = await self._spawn_liaison_brain(prompt, state)
        if liaison_pane is None:
            yield state.factory.completed_error(
                error="Failed to spawn liaison brain",
                resume=token,
            )
            return

        # Save session state for resume
        await self._save_session(state)

        # Main polling loop
        async for event in self._poll_loop(state):
            yield event
            if state.completed:
                break

    async def handle_input_response(
        self, response: InputResponseEvent, state: LiaisonStreamState
    ) -> TakopiEvent | None:
        """Route an input response to the appropriate pane."""
        request_id = response.request_id
        if request_id not in state.pending_requests:
            self.logger.warning(
                "liaison.response.unknown_request", request_id=request_id
            )
            return None

        # Find the pane waiting for this response
        for pane_id, pane in state.panes.items():
            if pane.pending_input_request == request_id:
                success = await self._send_to_pane(pane, response.response)
                pane.pending_input_request = None
                del state.pending_requests[request_id]

                if success:
                    state.note_seq += 1
                    return state.factory.action_completed(
                        action_id=f"liaison.input.{state.note_seq}",
                        kind="note",
                        title=f"Sent response to {pane.engine}",
                        ok=True,
                        detail={"pane_id": pane_id, "response": response.response},
                    )
                else:
                    state.note_seq += 1
                    return state.factory.action_completed(
                        action_id=f"liaison.input.{state.note_seq}",
                        kind="warning",
                        title=f"Failed to send response to {pane.engine}",
                        ok=False,
                        detail={"pane_id": pane_id},
                    )

        return None

    def _ensure_folders(self, state: LiaisonStreamState) -> None:
        """Ensure coordination folder structure exists."""
        folder = state.coordination_folder
        if folder is None:
            return
        (folder / "sessions").mkdir(parents=True, exist_ok=True)
        (folder / "coordination" / "inbox").mkdir(parents=True, exist_ok=True)
        (folder / "coordination" / "broadcast").mkdir(parents=True, exist_ok=True)
        (folder / "state").mkdir(parents=True, exist_ok=True)
        (folder / "locks").mkdir(parents=True, exist_ok=True)

    def _generate_session_id(self) -> str:
        """Generate a unique session ID."""
        return f"liaison_{secrets.token_hex(8)}"

    async def _create_tmux_session(self, session_id: str) -> str | None:
        """Create a new tmux session."""
        tmux_name = f"takopi_{session_id}"
        cmd = ["tmux", "new-session", "-d", "-s", tmux_name]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            self.logger.error(
                "liaison.tmux.create_failed",
                session=tmux_name,
                error=stderr.decode("utf-8", errors="replace"),
            )
            return None

        self.logger.info("liaison.tmux.created", session=tmux_name)
        return tmux_name

    async def _spawn_liaison_brain(
        self, prompt: str, state: LiaisonStreamState
    ) -> TmuxPane | None:
        """Spawn Claude Code as the liaison brain in the tmux session."""
        if state.tmux_session is None:
            return None

        # Build the Claude Code command with the system prompt
        system_prompt = self._build_liaison_system_prompt()
        cmd_parts = [
            self.liaison_cmd,
            "-p",  # print mode
            "--system-prompt",
            system_prompt,
            "--",
            prompt,
        ]
        cmd_str = " ".join(self._shell_escape(p) for p in cmd_parts)

        # Send the command to the tmux pane
        send_cmd = [
            "tmux",
            "send-keys",
            "-t",
            f"{state.tmux_session}:0.0",
            cmd_str,
            "Enter",
        ]

        proc = await asyncio.create_subprocess_exec(
            *send_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        if proc.returncode != 0:
            return None

        pane = TmuxPane(
            session_name=state.tmux_session,
            window_index=0,
            pane_index=0,
            engine="claude",
            role="liaison",
        )
        state.panes["liaison_brain"] = pane

        self.logger.info(
            "liaison.brain.spawned",
            session=state.tmux_session,
            prompt_len=len(prompt),
        )
        return pane

    def _build_liaison_system_prompt(self) -> str:
        """Build the system prompt for the liaison Claude Code instance."""
        return """You are a liaison agent that interprets user requests and orchestrates work via Claude Code subagents.

IMPORTANT: You are an orchestrator, not an executor. Delegate work to Claude Code subagents running in tmux panes. Do NOT execute coding tasks yourself.

## Session Management

Track your active Claude Code sessions. Before starting a new session, check if you should resume an existing one:

**Resume an existing session when:**
- The user's request is a follow-up to a previous task
- The request relates to the same project/directory as an active session
- The user says "continue", "also", "and then", or references previous work

**Start a new session when:**
- The request is for a completely different project or task
- The user explicitly asks to start fresh
- No relevant active session exists

To check active sessions:
- List tmux panes: tmux list-panes -a -F "#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}"
- Capture pane to see what a session is doing: tmux capture-pane -t <pane> -p

## Directory Targeting

Claude Code sessions work in specific directories. To run a session in a particular directory:

1. Create pane and cd first:
   tmux split-window -h
   tmux send-keys -t <pane> 'cd /path/to/project && claude' Enter

2. Or start claude with a working directory context in the prompt:
   tmux send-keys -t <pane> 'cd /path/to/project && claude' Enter
   (then send task after claude starts)

To check what directory a session is in:
   tmux display -t <pane> -p '#{pane_current_path}'

## Spawning and Communicating with Subagents

To spawn a new Claude Code subagent:
1. Create pane: tmux split-window -h (or -v for vertical)
2. Get pane ID: tmux display -p '#{pane_id}'
3. Start claude: tmux send-keys -t <pane> 'cd <directory> && claude' Enter
4. Wait ~2 seconds for startup
5. Send task: tmux send-keys -t <pane> '<task description>' Enter

To send follow-up input to an existing session:
   tmux send-keys -t <pane> '<your message>' Enter

To monitor progress:
   tmux capture-pane -t <pane> -p -S -50

## Your Responsibilities

Do things yourself ONLY when:
- Reading files to understand context before delegating
- Coordinating between multiple subagents
- Planning or summarizing results

For all coding, file editing, running commands - use a subagent.

When a subagent asks a question:
- Safe/routine (mkdir, tests, format): answer automatically via send-keys
- Risky (delete, production, credentials): escalate to the user

When complete, summarize what was done across all sessions."""

    def _shell_escape(self, s: str) -> str:
        """Escape a string for shell use."""
        if not s:
            return "''"
        if re.match(r"^[a-zA-Z0-9_\-./=]+$", s):
            return s
        return "'" + s.replace("'", "'\"'\"'") + "'"

    async def _poll_loop(
        self, state: LiaisonStreamState
    ) -> AsyncIterator[TakopiEvent]:
        """Main loop that monitors panes and handles events."""
        iteration = 0
        max_idle_iterations = 600  # 5 minutes at 0.5s intervals

        while not state.completed:
            await asyncio.sleep(self.poll_interval_s)
            iteration += 1

            # Check tmux session health
            if not await self._check_tmux_health(state):
                yield state.factory.completed_error(
                    error="Tmux session crashed",
                    resume=ResumeToken(engine=ENGINE, value=state.session_id or ""),
                )
                return

            # Capture and process output from all panes
            had_activity = False
            for pane_id, pane in list(state.panes.items()):
                output = await self._capture_pane_output(pane)
                if output:
                    had_activity = True

                    # Emit pane activity event so user can see what's happening
                    state.note_seq += 1
                    pane_preview = self._truncate_output(output, max_lines=5)
                    yield state.factory.action_completed(
                        action_id=f"liaison.pane.{pane_id}.{state.note_seq}",
                        kind="pane_activity",
                        title=f"{pane.engine} ({pane.role})",
                        ok=True,
                        detail={
                            "pane_id": pane_id,
                            "engine": pane.engine,
                            "role": pane.role,
                            "output_preview": pane_preview,
                            "tmux_target": f"{pane.session_name}:{pane.window_index}.{pane.pane_index}",
                        },
                    )

                    events = self._parse_pane_output(output, pane, state)
                    for event in events:
                        yield event

            # Check for completion signals
            if self._check_completion(state):
                state.completed = True
                yield state.factory.completed(
                    ok=True,
                    answer=state.final_answer,
                    resume=ResumeToken(engine=ENGINE, value=state.session_id or ""),
                )
                return

            # Timeout if no activity
            if not had_activity and iteration > max_idle_iterations:
                yield state.factory.completed_error(
                    error="Liaison timed out waiting for activity",
                    resume=ResumeToken(engine=ENGINE, value=state.session_id or ""),
                )
                return

            # Save session periodically
            if iteration % 20 == 0:
                await self._save_session(state)

    async def _check_tmux_health(self, state: LiaisonStreamState) -> bool:
        """Check if the tmux session is still alive."""
        if state.tmux_session is None:
            return False

        cmd = ["tmux", "has-session", "-t", state.tmux_session]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def _capture_pane_output(self, pane: TmuxPane) -> str | None:
        """Capture new output from a tmux pane."""
        cmd = [
            "tmux",
            "capture-pane",
            "-t",
            f"{pane.session_name}:{pane.window_index}.{pane.pane_index}",
            "-p",
            "-S",
            str(-self.capture_lines),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return None

        output = stdout.decode("utf-8", errors="replace")

        # Check if output changed since last capture
        output_hash = str(hash(output))
        if output_hash == pane.last_capture_hash:
            return None

        pane.last_capture_hash = output_hash
        return output

    async def _send_to_pane(self, pane: TmuxPane, text: str) -> bool:
        """Send input to a tmux pane via send-keys."""
        # Escape for tmux send-keys
        escaped = text.replace("\\", "\\\\").replace(";", "\\;")

        cmd = [
            "tmux",
            "send-keys",
            "-t",
            f"{pane.session_name}:{pane.window_index}.{pane.pane_index}",
            escaped,
            "Enter",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    def _parse_pane_output(
        self, output: str, pane: TmuxPane, state: LiaisonStreamState
    ) -> list[TakopiEvent]:
        """Parse pane output for questions and completion signals."""
        events: list[TakopiEvent] = []

        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue

            # Check for question patterns
            for pattern in _QUESTION_PATTERNS:
                if pattern.search(line):
                    # Check escalation policy
                    if self.escalation_policy.should_escalate(line):
                        event = self._create_input_request(line, pane, state)
                        if event:
                            events.append(event)
                    else:
                        # Auto-respond
                        auto_response = self.escalation_policy.auto_response(line)
                        if auto_response is not None:
                            # Queue auto-response (will be sent in next iteration)
                            asyncio.create_task(
                                self._send_to_pane(pane, auto_response)
                            )
                            state.note_seq += 1
                            events.append(
                                state.factory.action_completed(
                                    action_id=f"liaison.auto.{state.note_seq}",
                                    kind="note",
                                    title=f"Auto-responded: {auto_response}",
                                    ok=True,
                                    detail={"question": line},
                                )
                            )
                    break

            # Check for completion markers
            if self._is_completion_marker(line):
                state.final_answer = output
                state.completed = True

        return events

    def _truncate_output(self, output: str, max_lines: int = 5) -> str:
        """Truncate output to show in progress updates."""
        lines = output.strip().splitlines()
        # Take the last N non-empty lines (most recent activity)
        non_empty = [line for line in lines if line.strip()]
        if len(non_empty) <= max_lines:
            return "\n".join(non_empty)
        return "\n".join(non_empty[-max_lines:])

    def _create_input_request(
        self, question: str, pane: TmuxPane, state: LiaisonStreamState
    ) -> InputRequestEvent | None:
        """Create an input request event for user escalation."""
        if pane.pending_input_request is not None:
            return None  # Already waiting for response

        state.request_seq += 1
        request_id = f"{state.session_id}_{state.request_seq}"

        urgency = self.escalation_policy.assess_urgency(question)

        event = InputRequestEvent(
            engine=ENGINE,
            request_id=request_id,
            question=question,
            source="subagent",
            context=f"From {pane.engine} in pane {pane.role}",
            urgency=urgency,
        )

        state.pending_requests[request_id] = event
        pane.pending_input_request = request_id

        return event

    def _is_completion_marker(self, line: str) -> bool:
        """Check if a line indicates task completion."""
        markers = [
            "Task completed",
            "Done.",
            "Finished.",
            "All tasks complete",
        ]
        return any(marker.lower() in line.lower() for marker in markers)

    def _check_completion(self, state: LiaisonStreamState) -> bool:
        """Check if the liaison task is complete."""
        return state.completed

    async def _save_session(self, state: LiaisonStreamState) -> None:
        """Save session state to disk for resume."""
        if state.coordination_folder is None or state.session_id is None:
            return

        session_file = (
            state.coordination_folder / "sessions" / f"{state.session_id}.json"
        )

        panes_data = []
        for pane_id, pane in state.panes.items():
            panes_data.append(
                {
                    "pane_id": pane_id,
                    "session_name": pane.session_name,
                    "window_index": pane.window_index,
                    "pane_index": pane.pane_index,
                    "engine": pane.engine,
                    "role": pane.role,
                    "subagent_resume": pane.subagent_resume,
                }
            )

        data = {
            "session_id": state.session_id,
            "tmux_session": state.tmux_session,
            "created_at": time.time(),
            "panes": panes_data,
            "coordination_folder": str(state.coordination_folder),
        }

        session_file.write_text(json.dumps(data, indent=2))

    async def _restore_session(
        self, session_id: str, state: LiaisonStreamState
    ) -> bool:
        """Restore session state from disk."""
        if state.coordination_folder is None:
            return False

        session_file = state.coordination_folder / "sessions" / f"{session_id}.json"
        if not session_file.exists():
            return False

        try:
            data = json.loads(session_file.read_text())
        except json.JSONDecodeError:
            return False

        state.tmux_session = data.get("tmux_session")
        if state.tmux_session is None:
            return False

        # Check if tmux session still exists
        if not await self._check_tmux_health(state):
            self.logger.warning(
                "liaison.restore.tmux_gone",
                session_id=session_id,
                tmux_session=state.tmux_session,
            )
            return False

        # Restore pane info
        for pane_data in data.get("panes", []):
            pane = TmuxPane(
                session_name=pane_data["session_name"],
                window_index=pane_data["window_index"],
                pane_index=pane_data["pane_index"],
                engine=pane_data["engine"],
                role=pane_data["role"],
                subagent_resume=pane_data.get("subagent_resume"),
            )
            state.panes[pane_data["pane_id"]] = pane

        self.logger.info(
            "liaison.restore.success",
            session_id=session_id,
            pane_count=len(state.panes),
        )
        return True


def build_runner(config: EngineConfig, _config_path: Path) -> Runner:
    """Build a LiaisonRunner from config."""
    coordination_folder = config.get("coordination_folder")
    if coordination_folder:
        coordination_folder = Path(coordination_folder)
    else:
        coordination_folder = Path.home() / ".takopi" / "liaison"

    poll_interval = config.get("poll_interval_s", 0.5)
    capture_lines = config.get("capture_lines", 50)

    # Build escalation policy from config
    policy = EscalationPolicy()
    if "escalation" in config:
        esc_config = config["escalation"]
        if "timeout_s" in esc_config:
            policy.default_timeout_s = esc_config["timeout_s"]

    return LiaisonRunner(
        coordination_folder=coordination_folder,
        poll_interval_s=poll_interval,
        capture_lines=capture_lines,
        escalation_policy=policy,
    )


BACKEND = EngineBackend(
    id="liaison",
    build_runner=build_runner,
    install_cmd=None,  # No separate install needed
)

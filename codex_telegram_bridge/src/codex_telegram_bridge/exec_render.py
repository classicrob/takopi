from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from textwrap import indent
from typing import Any, Optional

STATUS_RUNNING = "▸"
STATUS_DONE = "✓"
HEADER_SEP = " · "
HARD_BREAK = "  \n"

MAX_CMD_LEN = 40
MAX_QUERY_LEN = 60
MAX_PATH_LEN = 40
MAX_PROGRESS_CHARS = 300


def one_line(text: str) -> str:
    return " ".join(text.split())


def truncate(text: str, max_len: int) -> str:
    return one_line(text)[:max_len]


def format_elapsed(elapsed_s: float) -> str:
    total = max(0, int(elapsed_s))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def format_header(elapsed_s: float, turn: Optional[int], label: str) -> str:
    elapsed = format_elapsed(elapsed_s)
    if turn is not None:
        return f"{label}{HEADER_SEP}{elapsed}{HEADER_SEP}turn {turn}"
    return f"{label}{HEADER_SEP}{elapsed}"


def format_command(command: str) -> str:
    command = truncate(command, MAX_CMD_LEN)
    return f"`{command}`"


def format_query(query: str) -> str:
    return truncate(query, MAX_QUERY_LEN)


def format_paths(paths: list[str]) -> str:
    rendered = []
    for path in paths:
        rendered.append(f"`{truncate(path, MAX_PATH_LEN)}`")
    return ", ".join(rendered)


def format_file_change(changes: list[dict[str, Any]]) -> str:
    paths = [change.get("path") for change in changes if change.get("path")]
    if not paths:
        total = len(changes)
        return "updated files" if total == 0 else f"updated {total} files"
    if len(paths) <= 3:
        return f"updated {format_paths(paths)}"
    return f"updated {len(paths)} files"


def format_tool_call(server: str, tool: str) -> str:
    name = ".".join(part for part in (server, tool) if part)
    return name or "tool"

def is_command_log_line(line: str) -> bool:
    return f"{STATUS_RUNNING} running:" in line or f"{STATUS_DONE} ran:" in line


def extract_numeric_id(item_id: Optional[object], fallback: Optional[int] = None) -> Optional[int]:
    if isinstance(item_id, int):
        return item_id
    if isinstance(item_id, str):
        match = re.search(r"(?:item_)?(\d+)", item_id)
        if match:
            return int(match.group(1))
    return fallback


def attach_id(item_id: Optional[int], line: str) -> str:
    return f"[{item_id if item_id is not None else '?'}] {line}"

def format_reasoning(text: str) -> str:
    return f"_{text}_"


def format_item_action_line(etype: str, item: dict[str, Any]) -> str | None:
    match (item["type"], etype):
        case ("command_execution", "item.started"):
            command = format_command(item["command"])
            return f"{STATUS_RUNNING} running: {command}"
        case ("command_execution", "item.completed"):
            command = format_command(item["command"])
            exit_code = item["exit_code"]
            exit_part = f" (exit {exit_code})" if exit_code is not None else ""
            return f"{STATUS_DONE} ran: {command}{exit_part}"
        case ("mcp_tool_call", "item.started"):
            name = format_tool_call(item["server"], item["tool"])
            return f"{STATUS_RUNNING} tool: {name}"
        case ("mcp_tool_call", "item.completed"):
            name = format_tool_call(item["server"], item["tool"])
            return f"{STATUS_DONE} tool: {name}"
        case _:
            return None


def format_item_completed_line(item: dict[str, Any]) -> str | None:
    match item["type"]:
        case "reasoning":
            return format_reasoning(item["text"])
        case "web_search":
            query = format_query(item["query"])
            return f"{STATUS_DONE} searched: {query}"
        case "file_change":
            return f"{STATUS_DONE} {format_file_change(item['changes'])}"
        case "error":
            warning = truncate(item["message"], 120)
            return f"{STATUS_DONE} warning: {warning}"
        case _:
            return None


@dataclass
class ExecRenderState:
    recent_actions: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    last_turn: Optional[int] = None


def record_item(state: ExecRenderState, item: dict[str, Any]) -> None:
    numeric_id = extract_numeric_id(item["id"])
    if numeric_id is not None:
        state.last_turn = numeric_id


def render_event_cli(
    event: dict[str, Any],
    state: ExecRenderState,
) -> list[str]:
    lines: list[str] = []

    etype = event["type"]
    match etype:
        case "thread.started":
            return ["thread started"]
        case "turn.started":
            return ["turn started"]
        case "turn.completed":
            return ["turn completed"]
        case "turn.failed":
            return [f"turn failed: {event['error']['message']}"]
        case "error":
            return [f"stream error: {event['message']}"]
        case "item.started" | "item.updated" | "item.completed":
            item = event["item"]
            record_item(state, item)

            item_num = extract_numeric_id(item["id"], state.last_turn)
            match (item["type"], etype):
                case ("agent_message", "item.completed"):
                    lines.append("assistant:")
                    lines.extend(indent(item["text"], "  ").splitlines())
                case _:
                    action_line = format_item_action_line(etype, item)
                    if action_line is not None:
                        lines.append(attach_id(item_num, action_line))
                    elif etype == "item.completed":
                        completed_line = format_item_completed_line(item)
                        if completed_line is not None:
                            lines.append(attach_id(item_num, completed_line))
            return lines
        case _:
            return lines


class ExecProgressRenderer:
    def __init__(self, max_actions: int = 5, max_chars: int = MAX_PROGRESS_CHARS) -> None:
        self.max_actions = max_actions
        self.state = ExecRenderState(recent_actions=deque(maxlen=max_actions))
        self.max_chars = max_chars

    def note_event(self, event: dict[str, Any]) -> bool:
        etype = event["type"]
        match etype:
            case "thread.started" | "turn.started":
                return True
            case "item.started" | "item.updated" | "item.completed":
                item = event["item"]
                record_item(self.state, item)
                item_id = extract_numeric_id(item["id"], self.state.last_turn)
                match item["type"]:
                    case "agent_message":
                        return False
                    case _:
                        action_line = format_item_action_line(etype, item)
                        if action_line is not None:
                            full = attach_id(item_id, action_line)
                            if etype == "item.completed" and self.state.recent_actions:
                                last = self.state.recent_actions[-1]
                                if last.startswith(f"[{item_id}] {STATUS_RUNNING} "):
                                    self.state.recent_actions.pop()
                            self.state.recent_actions.append(full)
                            return True
                        if etype == "item.completed":
                            completed_line = format_item_completed_line(item)
                            if completed_line is not None:
                                self.state.recent_actions.append(attach_id(item_id, completed_line))
                                return True
                        return False
            case _:
                return False

    def render_progress(self, elapsed_s: float) -> str:
        header = format_header(elapsed_s, self.state.last_turn, label="working")
        message = self._assemble(header, list(self.state.recent_actions))
        if len(message) <= self.max_chars:
            return message
        return header

    def render_final(self, elapsed_s: float, answer: str, status: str = "done") -> str:
        header = format_header(elapsed_s, self.state.last_turn, label=status)
        lines = list(self.state.recent_actions)
        if status == "done":
            lines = [line for line in lines if not is_command_log_line(line)]
        body = self._assemble(header, lines)
        answer = (answer or "").strip()
        if answer:
            body = body + "\n\n" + answer
        return body

    @staticmethod
    def _assemble(header: str, lines: list[str]) -> str:
        if not lines:
            return header
        return header + "\n\n" + HARD_BREAK.join(lines)

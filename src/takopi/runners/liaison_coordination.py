"""Coordination layer for multiple liaison agents (swarm pattern).

Liaisons share information via files in a coordination folder, allowing
them to collaborate on tasks and avoid duplicating work.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class CoordinationMessage:
    """A message between liaison agents."""

    message_id: str
    from_liaison: str
    to_liaison: str | None  # None = broadcast to all
    timestamp: float
    type: str  # "info_share", "question", "task_claim", "task_complete"
    payload: dict[str, Any]
    expires_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "from_liaison": self.from_liaison,
            "to_liaison": self.to_liaison,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoordinationMessage:
        return cls(
            message_id=data["message_id"],
            from_liaison=data["from_liaison"],
            to_liaison=data.get("to_liaison"),
            timestamp=data["timestamp"],
            type=data["type"],
            payload=data.get("payload", {}),
            expires_at=data.get("expires_at"),
        )


@dataclass(slots=True)
class LiaisonCoordinator:
    """Handles inter-liaison communication via shared folder."""

    folder: Path
    liaison_id: str
    _read_broadcast_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._ensure_folders()

    def _ensure_folders(self) -> None:
        """Create the coordination folder structure."""
        (self.folder / "coordination" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.folder / "coordination" / "broadcast").mkdir(parents=True, exist_ok=True)
        (self.folder / "inbox" / self.liaison_id).mkdir(parents=True, exist_ok=True)
        (self.folder / "state").mkdir(parents=True, exist_ok=True)
        (self.folder / "locks").mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        """Acquire an exclusive lock on a file."""
        lock_path = self.folder / "locks" / f"{path.stem}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_json(self, path: Path, default: Any = None) -> Any:
        """Load JSON from a file, returning default if not found."""
        if not path.exists():
            return default if default is not None else {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return default if default is not None else {}

    def _save_json(self, path: Path, data: Any) -> None:
        """Save data as JSON to a file."""
        path.write_text(json.dumps(data, indent=2))

    def send_message(self, message: CoordinationMessage) -> None:
        """Send a message to another liaison or broadcast to all."""
        if message.to_liaison is None:
            # Broadcast
            dest = self.folder / "coordination" / "broadcast"
            filename = f"{int(message.timestamp * 1000)}_{self.liaison_id}.json"
        else:
            # Direct message
            dest = self.folder / "coordination" / "inbox" / message.to_liaison
            dest.mkdir(parents=True, exist_ok=True)
            filename = f"{int(message.timestamp * 1000)}_{self.liaison_id}.json"

        filepath = dest / filename
        self._save_json(filepath, message.to_dict())

    def receive_messages(self) -> list[CoordinationMessage]:
        """Check inbox and broadcast for new messages."""
        messages: list[CoordinationMessage] = []
        now = time.time()

        # Check direct inbox
        inbox = self.folder / "coordination" / "inbox" / self.liaison_id
        if inbox.exists():
            for filepath in inbox.glob("*.json"):
                msg = self._read_message(filepath, now)
                if msg is not None:
                    messages.append(msg)
                    filepath.unlink()  # Remove after reading

        # Check broadcast (don't delete, just track read IDs)
        broadcast = self.folder / "coordination" / "broadcast"
        if broadcast.exists():
            for filepath in broadcast.glob("*.json"):
                msg = self._read_message(filepath, now)
                if msg is not None and msg.message_id not in self._read_broadcast_ids:
                    messages.append(msg)
                    self._read_broadcast_ids.add(msg.message_id)

        return messages

    def _read_message(
        self, filepath: Path, now: float
    ) -> CoordinationMessage | None:
        """Read and validate a message file."""
        try:
            data = json.loads(filepath.read_text())
            msg = CoordinationMessage.from_dict(data)

            # Check expiration
            if msg.expires_at is not None and msg.expires_at < now:
                return None

            # Don't return our own messages
            if msg.from_liaison == self.liaison_id:
                return None

            return msg
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def register_liaison(self, task: str) -> None:
        """Register this liaison as active."""
        active_file = self.folder / "state" / "active_liaisons.json"

        with self._file_lock(active_file):
            data = self._load_json(active_file, {"liaisons": {}, "version": 1})
            data["liaisons"][self.liaison_id] = {
                "started_at": time.time(),
                "pid": os.getpid(),
                "task": task,
                "status": "running",
                "last_heartbeat": time.time(),
            }
            self._save_json(active_file, data)

    def heartbeat(self, status: str = "running") -> None:
        """Update heartbeat timestamp."""
        active_file = self.folder / "state" / "active_liaisons.json"

        with self._file_lock(active_file):
            data = self._load_json(active_file, {"liaisons": {}, "version": 1})
            if self.liaison_id in data.get("liaisons", {}):
                data["liaisons"][self.liaison_id]["last_heartbeat"] = time.time()
                data["liaisons"][self.liaison_id]["status"] = status
                self._save_json(active_file, data)

    def deregister_liaison(self) -> None:
        """Remove this liaison from active list."""
        active_file = self.folder / "state" / "active_liaisons.json"

        with self._file_lock(active_file):
            data = self._load_json(active_file, {"liaisons": {}, "version": 1})
            data.get("liaisons", {}).pop(self.liaison_id, None)
            self._save_json(active_file, data)

    def get_active_liaisons(self) -> dict[str, dict[str, Any]]:
        """Get information about all active liaisons."""
        active_file = self.folder / "state" / "active_liaisons.json"
        data = self._load_json(active_file, {"liaisons": {}, "version": 1})

        # Filter out stale liaisons (no heartbeat in 60 seconds)
        now = time.time()
        active = {}
        for lid, info in data.get("liaisons", {}).items():
            last_heartbeat = info.get("last_heartbeat", 0)
            if now - last_heartbeat < 60:
                active[lid] = info

        return active

    def claim_task(self, task_id: str, description: str) -> bool:
        """Attempt to claim a task (returns False if already claimed)."""
        tasks_file = self.folder / "state" / "task_registry.json"

        with self._file_lock(tasks_file):
            data = self._load_json(tasks_file, {"tasks": {}, "version": 1})
            tasks = data.get("tasks", {})

            if task_id in tasks:
                existing = tasks[task_id]
                if existing.get("status") == "in_progress":
                    return False  # Already claimed

            tasks[task_id] = {
                "claimed_by": self.liaison_id,
                "claimed_at": time.time(),
                "description": description,
                "status": "in_progress",
            }
            data["tasks"] = tasks
            self._save_json(tasks_file, data)
            return True

    def complete_task(self, task_id: str, result: str | None = None) -> None:
        """Mark a task as complete."""
        tasks_file = self.folder / "state" / "task_registry.json"

        with self._file_lock(tasks_file):
            data = self._load_json(tasks_file, {"tasks": {}, "version": 1})
            tasks = data.get("tasks", {})

            if task_id in tasks:
                tasks[task_id]["status"] = "completed"
                tasks[task_id]["completed_at"] = time.time()
                if result is not None:
                    tasks[task_id]["result"] = result
                self._save_json(tasks_file, data)

    def share_context(self, key: str, value: Any) -> None:
        """Share context information with other liaisons."""
        context_file = self.folder / "state" / "shared_context.json"

        with self._file_lock(context_file):
            data = self._load_json(context_file, {"context": {}, "version": 1})
            context = data.get("context", {})
            context[key] = {
                "value": value,
                "from_liaison": self.liaison_id,
                "updated_at": time.time(),
            }
            data["context"] = context
            self._save_json(context_file, data)

    def get_shared_context(self) -> dict[str, Any]:
        """Get all shared context."""
        context_file = self.folder / "state" / "shared_context.json"
        data = self._load_json(context_file, {"context": {}, "version": 1})
        return data.get("context", {})

    def broadcast_discovery(self, topic: str, data: dict[str, Any]) -> None:
        """Broadcast a discovery to all liaisons."""
        msg = CoordinationMessage(
            message_id=f"discovery_{secrets.token_hex(8)}",
            from_liaison=self.liaison_id,
            to_liaison=None,  # Broadcast
            timestamp=time.time(),
            type="info_share",
            payload={"topic": topic, "data": data},
            expires_at=time.time() + 3600,  # Expire in 1 hour
        )
        self.send_message(msg)

    def ask_liaison(
        self, to_liaison: str, question: str, context: dict[str, Any] | None = None
    ) -> str:
        """Send a question to a specific liaison."""
        msg_id = f"question_{secrets.token_hex(8)}"
        msg = CoordinationMessage(
            message_id=msg_id,
            from_liaison=self.liaison_id,
            to_liaison=to_liaison,
            timestamp=time.time(),
            type="question",
            payload={"question": question, "context": context or {}},
            expires_at=time.time() + 300,  # Expire in 5 minutes
        )
        self.send_message(msg)
        return msg_id

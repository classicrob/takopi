"""Tests for the liaison coordination module."""

import json
import pytest
import time
from pathlib import Path

from takopi.runners.liaison_coordination import (
    CoordinationMessage,
    LiaisonCoordinator,
)


class TestCoordinationMessage:
    """Tests for CoordinationMessage dataclass."""

    def test_message_creation(self) -> None:
        """Message should be created with correct attributes."""
        now = time.time()
        msg = CoordinationMessage(
            message_id="msg_123",
            from_liaison="liaison_a",
            to_liaison="liaison_b",
            timestamp=now,
            type="info_share",
            payload={"key": "value"},
        )
        assert msg.message_id == "msg_123"
        assert msg.from_liaison == "liaison_a"
        assert msg.to_liaison == "liaison_b"
        assert msg.timestamp == now
        assert msg.type == "info_share"
        assert msg.payload == {"key": "value"}
        assert msg.expires_at is None

    def test_message_with_expiration(self) -> None:
        """Message can have expiration time."""
        now = time.time()
        msg = CoordinationMessage(
            message_id="msg_123",
            from_liaison="liaison_a",
            to_liaison=None,  # broadcast
            timestamp=now,
            type="info_share",
            payload={},
            expires_at=now + 3600,
        )
        assert msg.to_liaison is None
        assert msg.expires_at == now + 3600

    def test_message_to_dict(self) -> None:
        """Message should serialize to dict correctly."""
        now = time.time()
        msg = CoordinationMessage(
            message_id="msg_123",
            from_liaison="liaison_a",
            to_liaison="liaison_b",
            timestamp=now,
            type="question",
            payload={"question": "What should I do?"},
            expires_at=now + 300,
        )
        data = msg.to_dict()
        assert data["message_id"] == "msg_123"
        assert data["from_liaison"] == "liaison_a"
        assert data["to_liaison"] == "liaison_b"
        assert data["timestamp"] == now
        assert data["type"] == "question"
        assert data["payload"] == {"question": "What should I do?"}
        assert data["expires_at"] == now + 300

    def test_message_from_dict(self) -> None:
        """Message should deserialize from dict correctly."""
        now = time.time()
        data = {
            "message_id": "msg_456",
            "from_liaison": "liaison_x",
            "to_liaison": "liaison_y",
            "timestamp": now,
            "type": "task_claim",
            "payload": {"task_id": "task_1"},
            "expires_at": now + 600,
        }
        msg = CoordinationMessage.from_dict(data)
        assert msg.message_id == "msg_456"
        assert msg.from_liaison == "liaison_x"
        assert msg.to_liaison == "liaison_y"
        assert msg.timestamp == now
        assert msg.type == "task_claim"
        assert msg.payload == {"task_id": "task_1"}
        assert msg.expires_at == now + 600

    def test_message_from_dict_optional_fields(self) -> None:
        """Message should handle missing optional fields."""
        now = time.time()
        data = {
            "message_id": "msg_789",
            "from_liaison": "liaison_z",
            "timestamp": now,
            "type": "info_share",
        }
        msg = CoordinationMessage.from_dict(data)
        assert msg.to_liaison is None
        assert msg.payload == {}
        assert msg.expires_at is None

    def test_message_roundtrip(self) -> None:
        """Message should survive serialization roundtrip."""
        now = time.time()
        original = CoordinationMessage(
            message_id="roundtrip_test",
            from_liaison="liaison_a",
            to_liaison="liaison_b",
            timestamp=now,
            type="info_share",
            payload={"nested": {"data": [1, 2, 3]}},
            expires_at=now + 3600,
        )
        data = original.to_dict()
        restored = CoordinationMessage.from_dict(data)
        assert restored == original


class TestLiaisonCoordinator:
    """Tests for LiaisonCoordinator."""

    @pytest.fixture
    def coord_folder(self, tmp_path: Path) -> Path:
        """Create a temporary coordination folder."""
        return tmp_path / "coordination"

    @pytest.fixture
    def coordinator(self, coord_folder: Path) -> LiaisonCoordinator:
        """Create a coordinator instance."""
        return LiaisonCoordinator(folder=coord_folder, liaison_id="test_liaison")

    def test_folder_structure_created(self, coordinator: LiaisonCoordinator) -> None:
        """Coordinator should create folder structure on init."""
        folder = coordinator.folder
        assert (folder / "coordination" / "inbox").exists()
        assert (folder / "coordination" / "broadcast").exists()
        assert (folder / "inbox" / "test_liaison").exists()
        assert (folder / "state").exists()
        assert (folder / "locks").exists()

    def test_send_direct_message(self, coord_folder: Path) -> None:
        """Direct message should be sent to recipient's inbox."""
        sender = LiaisonCoordinator(folder=coord_folder, liaison_id="sender")
        receiver = LiaisonCoordinator(folder=coord_folder, liaison_id="receiver")

        msg = CoordinationMessage(
            message_id="dm_test",
            from_liaison="sender",
            to_liaison="receiver",
            timestamp=time.time(),
            type="info_share",
            payload={"info": "test data"},
        )
        sender.send_message(msg)

        # Check file was created in receiver's inbox
        inbox = coord_folder / "coordination" / "inbox" / "receiver"
        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["message_id"] == "dm_test"

    def test_send_broadcast_message(self, coord_folder: Path) -> None:
        """Broadcast message should be sent to broadcast folder."""
        sender = LiaisonCoordinator(folder=coord_folder, liaison_id="sender")

        msg = CoordinationMessage(
            message_id="broadcast_test",
            from_liaison="sender",
            to_liaison=None,
            timestamp=time.time(),
            type="info_share",
            payload={"info": "broadcast data"},
        )
        sender.send_message(msg)

        # Check file was created in broadcast folder
        broadcast = coord_folder / "coordination" / "broadcast"
        files = list(broadcast.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["message_id"] == "broadcast_test"
        assert data["to_liaison"] is None

    def test_receive_direct_messages(self, coord_folder: Path) -> None:
        """Receiver should get direct messages."""
        sender = LiaisonCoordinator(folder=coord_folder, liaison_id="sender")
        receiver = LiaisonCoordinator(folder=coord_folder, liaison_id="receiver")

        msg = CoordinationMessage(
            message_id="receive_test",
            from_liaison="sender",
            to_liaison="receiver",
            timestamp=time.time(),
            type="question",
            payload={"question": "Hello?"},
        )
        sender.send_message(msg)

        messages = receiver.receive_messages()
        assert len(messages) == 1
        assert messages[0].message_id == "receive_test"
        assert messages[0].payload == {"question": "Hello?"}

        # Message should be deleted after reading
        messages2 = receiver.receive_messages()
        assert len(messages2) == 0

    def test_receive_broadcast_messages(self, coord_folder: Path) -> None:
        """All liaisons should receive broadcast messages."""
        sender = LiaisonCoordinator(folder=coord_folder, liaison_id="sender")
        receiver1 = LiaisonCoordinator(folder=coord_folder, liaison_id="receiver1")
        receiver2 = LiaisonCoordinator(folder=coord_folder, liaison_id="receiver2")

        msg = CoordinationMessage(
            message_id="broadcast_recv_test",
            from_liaison="sender",
            to_liaison=None,
            timestamp=time.time(),
            type="info_share",
            payload={"broadcast": True},
        )
        sender.send_message(msg)

        messages1 = receiver1.receive_messages()
        assert len(messages1) == 1
        assert messages1[0].message_id == "broadcast_recv_test"

        messages2 = receiver2.receive_messages()
        assert len(messages2) == 1
        assert messages2[0].message_id == "broadcast_recv_test"

        # Broadcast should not be re-read
        messages1_again = receiver1.receive_messages()
        assert len(messages1_again) == 0

    def test_ignore_own_messages(self, coord_folder: Path) -> None:
        """Liaison should not receive own messages."""
        coordinator = LiaisonCoordinator(folder=coord_folder, liaison_id="self")

        msg = CoordinationMessage(
            message_id="self_msg",
            from_liaison="self",
            to_liaison=None,
            timestamp=time.time(),
            type="info_share",
            payload={},
        )
        coordinator.send_message(msg)

        messages = coordinator.receive_messages()
        assert len(messages) == 0

    def test_expired_messages_ignored(self, coord_folder: Path) -> None:
        """Expired messages should be ignored."""
        sender = LiaisonCoordinator(folder=coord_folder, liaison_id="sender")
        receiver = LiaisonCoordinator(folder=coord_folder, liaison_id="receiver")

        expired_msg = CoordinationMessage(
            message_id="expired",
            from_liaison="sender",
            to_liaison="receiver",
            timestamp=time.time() - 3600,  # 1 hour ago
            type="info_share",
            payload={},
            expires_at=time.time() - 1800,  # expired 30 min ago
        )
        sender.send_message(expired_msg)

        messages = receiver.receive_messages()
        assert len(messages) == 0

    def test_register_liaison(self, coordinator: LiaisonCoordinator) -> None:
        """Liaison should be registered in active list."""
        coordinator.register_liaison(task="test task")

        active = coordinator.get_active_liaisons()
        assert "test_liaison" in active
        assert active["test_liaison"]["task"] == "test task"
        assert active["test_liaison"]["status"] == "running"

    def test_heartbeat(self, coordinator: LiaisonCoordinator) -> None:
        """Heartbeat should update timestamp."""
        coordinator.register_liaison(task="test")
        time.sleep(0.01)  # Small delay
        coordinator.heartbeat(status="busy")

        active = coordinator.get_active_liaisons()
        assert active["test_liaison"]["status"] == "busy"

    def test_deregister_liaison(self, coordinator: LiaisonCoordinator) -> None:
        """Liaison should be removed from active list."""
        coordinator.register_liaison(task="test")
        coordinator.deregister_liaison()

        active = coordinator.get_active_liaisons()
        assert "test_liaison" not in active

    def test_stale_liaisons_filtered(self, coord_folder: Path) -> None:
        """Stale liaisons should be filtered from active list."""
        coordinator = LiaisonCoordinator(folder=coord_folder, liaison_id="stale")

        # Manually write stale entry
        active_file = coord_folder / "state" / "active_liaisons.json"
        active_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "liaisons": {
                "stale_liaison": {
                    "started_at": time.time() - 3600,
                    "pid": 12345,
                    "task": "old task",
                    "status": "running",
                    "last_heartbeat": time.time() - 120,  # 2 min ago
                }
            },
            "version": 1,
        }
        active_file.write_text(json.dumps(data))

        active = coordinator.get_active_liaisons()
        assert "stale_liaison" not in active

    def test_claim_task(self, coordinator: LiaisonCoordinator) -> None:
        """Task should be claimed successfully."""
        result = coordinator.claim_task("task_1", "Build the feature")
        assert result is True

    def test_claim_task_already_claimed(self, coord_folder: Path) -> None:
        """Claimed task should not be re-claimable."""
        coord1 = LiaisonCoordinator(folder=coord_folder, liaison_id="liaison_1")
        coord2 = LiaisonCoordinator(folder=coord_folder, liaison_id="liaison_2")

        result1 = coord1.claim_task("task_1", "Build the feature")
        assert result1 is True

        result2 = coord2.claim_task("task_1", "Build the feature")
        assert result2 is False

    def test_complete_task(self, coordinator: LiaisonCoordinator) -> None:
        """Task should be marked complete."""
        coordinator.claim_task("task_1", "Test task")
        coordinator.complete_task("task_1", result="Done!")

        # Task should now be claimable again (completed)
        result = coordinator.claim_task("task_1", "Redo task")
        assert result is True

    def test_share_context(self, coord_folder: Path) -> None:
        """Context should be shared between liaisons."""
        coord1 = LiaisonCoordinator(folder=coord_folder, liaison_id="liaison_1")
        coord2 = LiaisonCoordinator(folder=coord_folder, liaison_id="liaison_2")

        coord1.share_context("api_endpoint", "https://example.com/api")

        context = coord2.get_shared_context()
        assert "api_endpoint" in context
        assert context["api_endpoint"]["value"] == "https://example.com/api"
        assert context["api_endpoint"]["from_liaison"] == "liaison_1"

    def test_broadcast_discovery(self, coord_folder: Path) -> None:
        """Discovery should be broadcast to all."""
        sender = LiaisonCoordinator(folder=coord_folder, liaison_id="discoverer")
        receiver = LiaisonCoordinator(folder=coord_folder, liaison_id="listener")

        sender.broadcast_discovery("api_found", {"url": "http://api.test"})

        messages = receiver.receive_messages()
        assert len(messages) == 1
        assert messages[0].type == "info_share"
        assert messages[0].payload["topic"] == "api_found"
        assert messages[0].payload["data"] == {"url": "http://api.test"}

    def test_ask_liaison(self, coord_folder: Path) -> None:
        """Question should be sent to specific liaison."""
        asker = LiaisonCoordinator(folder=coord_folder, liaison_id="asker")
        answerer = LiaisonCoordinator(folder=coord_folder, liaison_id="answerer")

        msg_id = asker.ask_liaison("answerer", "What is the API key?")
        assert msg_id.startswith("question_")

        messages = answerer.receive_messages()
        assert len(messages) == 1
        assert messages[0].type == "question"
        assert messages[0].payload["question"] == "What is the API key?"

    def test_concurrent_access(self, coord_folder: Path) -> None:
        """Multiple coordinators should handle concurrent access."""
        coords = [
            LiaisonCoordinator(folder=coord_folder, liaison_id=f"liaison_{i}")
            for i in range(5)
        ]

        # All register
        for i, coord in enumerate(coords):
            coord.register_liaison(task=f"task_{i}")

        # Check all are active
        active = coords[0].get_active_liaisons()
        assert len(active) == 5

        # All send broadcasts
        for i, coord in enumerate(coords):
            coord.broadcast_discovery(f"topic_{i}", {"from": i})

        # Each should receive 4 messages (not own)
        for coord in coords:
            messages = coord.receive_messages()
            assert len(messages) == 4

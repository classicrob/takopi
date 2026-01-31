"""Tests for the liaison runner module."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from takopi.model import (
    InputRequestEvent,
    InputResponseEvent,
    ResumeToken,
)
from takopi.runners.liaison import (
    ENGINE,
    LiaisonRunner,
    LiaisonStreamState,
    TmuxPane,
    _QUESTION_PATTERNS,
    _RESUME_RE,
)
from takopi.runners.escalation import EscalationPolicy


class TestResumeTokenParsing:
    """Tests for resume token parsing and formatting."""

    def test_format_resume_token(self) -> None:
        """Resume token should format correctly."""
        runner = LiaisonRunner()
        token = ResumeToken(engine=ENGINE, value="session_abc123")
        assert runner.format_resume(token) == "`liaison --session session_abc123`"

    def test_format_resume_wrong_engine_raises(self) -> None:
        """Format should raise for wrong engine."""
        runner = LiaisonRunner()
        token = ResumeToken(engine="claude", value="session_abc123")
        with pytest.raises(RuntimeError, match="resume token is for engine"):
            runner.format_resume(token)

    def test_extract_resume_valid(self) -> None:
        """Valid resume lines should be extracted."""
        runner = LiaisonRunner()
        # With backticks
        assert runner.extract_resume("`liaison --session abc123`") == ResumeToken(
            engine=ENGINE, value="abc123"
        )
        # Without backticks
        assert runner.extract_resume("liaison --session xyz789") == ResumeToken(
            engine=ENGINE, value="xyz789"
        )
        # With whitespace
        assert runner.extract_resume("  liaison --session test  ") == ResumeToken(
            engine=ENGINE, value="test"
        )

    def test_extract_resume_invalid(self) -> None:
        """Invalid resume lines should return None."""
        runner = LiaisonRunner()
        assert runner.extract_resume("claude --resume abc") is None
        assert runner.extract_resume("random text") is None
        assert runner.extract_resume("") is None

    def test_resume_regex_pattern(self) -> None:
        """Resume regex should match expected patterns."""
        # Case insensitive
        assert _RESUME_RE.search("Liaison --session test") is not None
        assert _RESUME_RE.search("LIAISON --session TEST") is not None

        # With backticks
        match = _RESUME_RE.search("`liaison --session abc123`")
        assert match is not None
        assert match.group("token") == "abc123"

        # At start of line
        match = _RESUME_RE.search("liaison --session xyz")
        assert match is not None
        assert match.group("token") == "xyz"


class TestQuestionPatterns:
    """Tests for question detection patterns."""

    def test_detect_do_you_want(self) -> None:
        """'Do you want' questions should be detected."""
        for pattern in _QUESTION_PATTERNS:
            match = pattern.search("Do you want to continue?")
            if match:
                break
        else:
            pytest.fail("No pattern matched 'Do you want to continue?'")

    def test_detect_should_i(self) -> None:
        """'Should I' questions should be detected."""
        for pattern in _QUESTION_PATTERNS:
            match = pattern.search("Should I delete this file?")
            if match:
                break
        else:
            pytest.fail("No pattern matched 'Should I delete this file?'")

    def test_detect_yes_no_prompt(self) -> None:
        """y/n prompts should be detected."""
        for pattern in _QUESTION_PATTERNS:
            match = pattern.search("Continue? (y/n)")
            if match:
                break
        else:
            pytest.fail("No pattern matched 'Continue? (y/n)'")

    def test_detect_confirm(self) -> None:
        """Confirm prompts should be detected."""
        for pattern in _QUESTION_PATTERNS:
            match = pattern.search("Please confirm?")
            if match:
                break
        else:
            pytest.fail("No pattern matched 'Please confirm?'")

    def test_detect_press_enter(self) -> None:
        """Press Enter prompts should be detected."""
        for pattern in _QUESTION_PATTERNS:
            match = pattern.search("Press Enter to continue")
            if match:
                break
        else:
            pytest.fail("No pattern matched 'Press Enter to continue'")


class TestTmuxPane:
    """Tests for TmuxPane dataclass."""

    def test_pane_creation(self) -> None:
        """Pane should be created with correct attributes."""
        pane = TmuxPane(
            session_name="takopi_liaison_abc",
            window_index=0,
            pane_index=1,
            engine="codex",
            role="worker",
        )
        assert pane.session_name == "takopi_liaison_abc"
        assert pane.window_index == 0
        assert pane.pane_index == 1
        assert pane.engine == "codex"
        assert pane.role == "worker"
        assert pane.subagent_resume is None
        assert pane.last_capture_hash == ""
        assert pane.pending_input_request is None

    def test_pane_with_optional_fields(self) -> None:
        """Pane should accept optional fields."""
        pane = TmuxPane(
            session_name="test",
            window_index=0,
            pane_index=0,
            engine="claude",
            role="liaison",
            subagent_resume="resume_token",
        )
        assert pane.subagent_resume == "resume_token"


class TestLiaisonStreamState:
    """Tests for LiaisonStreamState."""

    def test_state_initialization(self) -> None:
        """State should initialize with defaults."""
        state = LiaisonStreamState()
        assert state.session_id is None
        assert state.tmux_session is None
        assert state.panes == {}
        assert state.pending_requests == {}
        assert state.note_seq == 0
        assert state.request_seq == 0
        assert state.coordination_folder is None
        assert state.completed is False
        assert state.final_answer == ""

    def test_state_factory_creates_events(self) -> None:
        """State factory should create events correctly."""
        state = LiaisonStreamState()
        started = state.factory.started(
            ResumeToken(engine=ENGINE, value="test"),
            title="Test",
        )
        assert started.type == "started"
        assert started.engine == ENGINE


class TestLiaisonRunnerHelpers:
    """Tests for LiaisonRunner helper methods."""

    def test_shell_escape_simple(self) -> None:
        """Simple strings should not be quoted."""
        runner = LiaisonRunner()
        assert runner._shell_escape("hello") == "hello"
        assert runner._shell_escape("test_file.py") == "test_file.py"
        assert runner._shell_escape("./path/to/file") == "./path/to/file"

    def test_shell_escape_special_chars(self) -> None:
        """Strings with special chars should be quoted."""
        runner = LiaisonRunner()
        assert runner._shell_escape("hello world") == "'hello world'"
        assert runner._shell_escape("test'file") == "'test'\"'\"'file'"

    def test_shell_escape_empty(self) -> None:
        """Empty string should return empty quotes."""
        runner = LiaisonRunner()
        assert runner._shell_escape("") == "''"

    def test_truncate_output_short(self) -> None:
        """Short output should not be truncated."""
        runner = LiaisonRunner()
        output = "line1\nline2\nline3"
        assert runner._truncate_output(output, max_lines=5) == "line1\nline2\nline3"

    def test_truncate_output_long(self) -> None:
        """Long output should be truncated to last N lines."""
        runner = LiaisonRunner()
        output = "line1\nline2\nline3\nline4\nline5\nline6\nline7"
        result = runner._truncate_output(output, max_lines=3)
        assert result == "line5\nline6\nline7"

    def test_truncate_output_empty_lines(self) -> None:
        """Empty lines should be filtered out."""
        runner = LiaisonRunner()
        output = "line1\n\n\nline2\n  \nline3"
        result = runner._truncate_output(output, max_lines=5)
        assert result == "line1\nline2\nline3"

    def test_is_completion_marker(self) -> None:
        """Completion markers should be detected."""
        runner = LiaisonRunner()
        assert runner._is_completion_marker("Task completed") is True
        assert runner._is_completion_marker("Done.") is True
        assert runner._is_completion_marker("Finished.") is True
        assert runner._is_completion_marker("All tasks complete") is True
        assert runner._is_completion_marker("Still working...") is False

    def test_is_completion_marker_case_insensitive(self) -> None:
        """Completion markers should be case insensitive."""
        runner = LiaisonRunner()
        assert runner._is_completion_marker("TASK COMPLETED") is True
        assert runner._is_completion_marker("done.") is True

    def test_generate_session_id(self) -> None:
        """Session ID should be generated with prefix."""
        runner = LiaisonRunner()
        sid = runner._generate_session_id()
        assert sid.startswith("liaison_")
        assert len(sid) > len("liaison_")

    def test_generate_session_id_unique(self) -> None:
        """Each session ID should be unique."""
        runner = LiaisonRunner()
        ids = [runner._generate_session_id() for _ in range(100)]
        assert len(ids) == len(set(ids))


class TestLiaisonRunnerConfig:
    """Tests for LiaisonRunner configuration."""

    def test_default_config(self) -> None:
        """Default config should have sensible values."""
        runner = LiaisonRunner()
        assert runner.engine == ENGINE
        assert runner.poll_interval_s == 0.5
        assert runner.capture_lines == 50
        assert runner.liaison_cmd == "claude"
        assert isinstance(runner.escalation_policy, EscalationPolicy)

    def test_custom_config(self) -> None:
        """Custom config should override defaults."""
        policy = EscalationPolicy(default_timeout_s=60.0)
        runner = LiaisonRunner(
            coordination_folder=Path("/tmp/liaison"),
            poll_interval_s=1.0,
            capture_lines=100,
            escalation_policy=policy,
        )
        assert runner.coordination_folder == Path("/tmp/liaison")
        assert runner.poll_interval_s == 1.0
        assert runner.capture_lines == 100
        assert runner.escalation_policy.default_timeout_s == 60.0


class TestBuildRunner:
    """Tests for build_runner function."""

    def test_build_runner_default(self) -> None:
        """Build runner with empty config."""
        from takopi.runners.liaison import build_runner

        runner = build_runner({}, Path("takopi.toml"))
        assert isinstance(runner, LiaisonRunner)
        assert runner.coordination_folder == Path.home() / ".takopi" / "liaison"

    def test_build_runner_custom_folder(self) -> None:
        """Build runner with custom coordination folder."""
        from takopi.runners.liaison import build_runner

        runner = build_runner(
            {"coordination_folder": "/custom/path"}, Path("takopi.toml")
        )
        assert isinstance(runner, LiaisonRunner)
        assert runner.coordination_folder == Path("/custom/path")

    def test_build_runner_poll_interval(self) -> None:
        """Build runner with custom poll interval."""
        from takopi.runners.liaison import build_runner

        runner = build_runner({"poll_interval_s": 2.0}, Path("takopi.toml"))
        assert isinstance(runner, LiaisonRunner)
        assert runner.poll_interval_s == 2.0

    def test_build_runner_escalation_config(self) -> None:
        """Build runner with escalation config."""
        from takopi.runners.liaison import build_runner

        runner = build_runner(
            {"escalation": {"timeout_s": 120.0}}, Path("takopi.toml")
        )
        assert isinstance(runner, LiaisonRunner)
        assert runner.escalation_policy.default_timeout_s == 120.0


class TestBackend:
    """Tests for the BACKEND export."""

    def test_backend_id(self) -> None:
        """Backend should have correct id."""
        from takopi.runners.liaison import BACKEND

        assert BACKEND.id == "liaison"

    def test_backend_build_runner(self) -> None:
        """Backend build_runner should work."""
        from takopi.runners.liaison import BACKEND

        runner = BACKEND.build_runner({}, Path("takopi.toml"))
        assert isinstance(runner, LiaisonRunner)

    def test_backend_install_cmd(self) -> None:
        """Backend should have tmux install hint."""
        from takopi.runners.liaison import BACKEND

        # Liaison requires tmux, provides install hint for macOS
        assert BACKEND.install_cmd == "brew install tmux"

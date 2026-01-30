"""Tests for smart_router.py - heuristic-based engine routing."""
from __future__ import annotations

import pytest

from takopi.router import AutoRouter, RunnerEntry
from takopi.runners.codex import CodexRunner
from takopi.smart_router import (
    RoutingDecision,
    SmartRouter,
    create_smart_router,
    _LIAISON_PATTERNS,
    _SIMPLE_PATTERNS,
)


def _make_router(*, has_liaison: bool = False) -> AutoRouter:
    """Create a minimal AutoRouter for testing."""
    codex = CodexRunner(codex_cmd="codex", extra_args=[])
    entries = [RunnerEntry(engine=codex.engine, runner=codex)]
    if has_liaison:
        # Mock a liaison entry
        entries.append(RunnerEntry(engine="liaison", runner=codex))
    return AutoRouter(entries=entries, default_engine=codex.engine)


class TestRoutingDecision:
    def test_explicit_engine_takes_priority(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze(
            "refactor all files across the codebase",
            explicit_engine="codex",
        )

        assert decision.engine == "codex"
        assert decision.reason == "explicit"
        assert decision.confidence == 1.0
        assert not decision.suggested_multi_agent

    def test_resume_engine_takes_priority(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze(
            "refactor all files across the codebase",
            resume_engine="claude",
        )

        assert decision.engine == "claude"
        assert decision.reason == "resume"
        assert decision.confidence == 1.0
        assert not decision.suggested_multi_agent


class TestLiaisonPatterns:
    def test_refactor_across_suggests_liaison(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("refactor all the files across the codebase")

        assert decision.suggested_multi_agent

    def test_update_all_files_suggests_liaison(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("update all config files to use the new format")

        assert decision.suggested_multi_agent

    def test_migrate_suggests_liaison(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("migrate from REST to GraphQL")

        assert decision.suggested_multi_agent

    def test_coordinate_suggests_liaison(self) -> None:
        router = _make_router(has_liaison=True)
        # Use suggest_only=False to get the actual liaison score in confidence
        smart = SmartRouter(router=router, suggest_only=False)

        decision = smart.analyze("coordinate the changes between frontend and backend")

        assert decision.engine == "liaison"
        assert decision.reason == "heuristic"
        assert decision.confidence >= 0.9

    def test_in_parallel_suggests_liaison(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("update the tests in parallel")

        assert decision.suggested_multi_agent

    def test_entire_codebase_suggests_liaison(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("search the entire codebase for deprecated functions")

        assert decision.suggested_multi_agent


class TestSimplePatterns:
    def test_fix_typo_is_simple(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("fix the typo in README")

        assert not decision.suggested_multi_agent
        assert decision.reason == "default"

    def test_what_is_question_is_simple(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("what is the purpose of this function?")

        assert not decision.suggested_multi_agent

    def test_explain_is_simple(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("explain how the auth flow works")

        assert not decision.suggested_multi_agent

    def test_how_to_question_is_simple(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router)

        decision = smart.analyze("how do I run the tests?")

        assert not decision.suggested_multi_agent


class TestSuggestOnlyMode:
    def test_suggest_only_does_not_switch_engine(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router, suggest_only=True)

        decision = smart.analyze("refactor all modules across the codebase")

        assert decision.engine == "codex"  # Default, not liaison
        assert decision.suggested_multi_agent
        assert decision.reason == "default"

    def test_auto_switch_when_not_suggest_only(self) -> None:
        router = _make_router(has_liaison=True)
        smart = SmartRouter(router=router, suggest_only=False)

        decision = smart.analyze("orchestrate the deployment")

        assert decision.engine == "liaison"
        assert decision.reason == "heuristic"
        assert decision.suggested_multi_agent


class TestLiaisonAvailability:
    def test_no_suggestion_without_liaison_engine(self) -> None:
        router = _make_router(has_liaison=False)
        smart = SmartRouter(router=router)

        decision = smart.analyze("refactor all modules across the codebase")

        assert not decision.suggested_multi_agent
        assert decision.engine == "codex"


class TestCreateSmartRouter:
    def test_returns_none_when_disabled(self) -> None:
        router = _make_router()
        result = create_smart_router(router, enabled=False)
        assert result is None

    def test_returns_router_when_enabled(self) -> None:
        router = _make_router()
        result = create_smart_router(router, enabled=True)
        assert result is not None
        assert isinstance(result, SmartRouter)

    def test_respects_suggest_only_setting(self) -> None:
        router = _make_router()
        result = create_smart_router(router, enabled=True, suggest_only=False)
        assert result is not None
        assert not result.suggest_only

    def test_respects_threshold_setting(self) -> None:
        router = _make_router()
        result = create_smart_router(router, enabled=True, liaison_threshold=0.5)
        assert result is not None
        assert result.liaison_threshold == 0.5


class TestPatternCompilation:
    def test_liaison_patterns_are_compiled(self) -> None:
        """Verify all liaison patterns compile without error."""
        for pattern, weight in _LIAISON_PATTERNS:
            assert pattern.pattern  # Access compiled pattern
            assert 0.0 <= weight <= 1.0

    def test_simple_patterns_are_compiled(self) -> None:
        """Verify all simple patterns compile without error."""
        for pattern, weight in _SIMPLE_PATTERNS:
            assert pattern.pattern
            assert 0.0 <= weight <= 1.0

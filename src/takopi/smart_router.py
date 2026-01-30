"""Smart router for automatic engine selection based on request analysis.

Analyzes request content to decide whether to use a single agent (claude/codex)
or multi-agent orchestration (liaison) without requiring explicit commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .model import EngineId
    from .router import AutoRouter


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Result of analyzing a request for routing."""

    engine: str  # The selected engine
    reason: Literal["explicit", "resume", "heuristic", "default"]
    confidence: float  # 0.0 - 1.0
    suggested_multi_agent: bool  # Whether liaison might be better


# Patterns suggesting multi-agent orchestration (liaison)
_LIAISON_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    # Explicit multi-step/multi-file patterns
    (re.compile(r"refactor\s+(?:all|multiple|across|the\s+entire)\b", re.I), 0.85),
    (re.compile(r"update\s+(?:all|every|each)\s+\w+\s+files?\b", re.I), 0.80),
    (re.compile(r"migrate\s+(?:from|to|the)\b", re.I), 0.75),
    (re.compile(r"coordinate\b", re.I), 0.90),
    (re.compile(r"orchestrate\b", re.I), 0.90),
    (re.compile(r"in\s+parallel\b", re.I), 0.85),
    # Multi-component work
    (re.compile(r"(?:and|then)\s+(?:run|execute)\s+(?:tests?|lint)", re.I), 0.70),
    (re.compile(r"(?:build|implement)\s+.+\s+(?:and|with)\s+tests?", re.I), 0.65),
    (re.compile(r"full\s+(?:stack|feature|implementation)", re.I), 0.70),
    # Large scope indicators
    (re.compile(r"entire\s+(?:codebase|project|application)", re.I), 0.80),
    (re.compile(r"(?:multiple|several|many)\s+(?:files?|components?|modules?)", re.I), 0.75),
    (re.compile(r"across\s+(?:the\s+)?(?:codebase|project)", re.I), 0.80),
]

# Patterns suggesting simple single-agent tasks
_SIMPLE_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    # Quick fixes
    (re.compile(r"^fix\s+(?:the|this|a)\s+\w+", re.I), 0.85),
    (re.compile(r"^(?:add|update|change|remove)\s+(?:the|this|a)\s+\w+", re.I), 0.75),
    (re.compile(r"typo\b", re.I), 0.90),
    # Questions/explanations
    (re.compile(r"^what\s+(?:is|does|are)\b", re.I), 0.90),
    (re.compile(r"^explain\b", re.I), 0.90),
    (re.compile(r"^how\s+(?:do|does|to)\b", re.I), 0.85),
    (re.compile(r"^why\s+(?:is|does|do)\b", re.I), 0.85),
    # Single file operations
    (re.compile(r"^read\s+(?:the\s+)?(?:file|code)", re.I), 0.90),
    (re.compile(r"in\s+(?:this|the)\s+file\b", re.I), 0.80),
]


@dataclass(slots=True)
class SmartRouter:
    """Analyzes requests to suggest optimal engine routing.

    This router uses heuristics to determine whether a request would benefit
    from multi-agent orchestration (liaison) or can be handled by a single agent.
    """

    router: AutoRouter
    liaison_threshold: float = 0.70
    suggest_only: bool = True  # If True, only suggest, don't auto-switch

    def analyze(
        self,
        prompt: str,
        *,
        explicit_engine: str | None = None,
        resume_engine: str | None = None,
    ) -> RoutingDecision:
        """Analyze a prompt and return routing decision.

        Args:
            prompt: The user's request text
            explicit_engine: Engine specified via directive (/claude, etc.)
            resume_engine: Engine from a resume token

        Returns:
            RoutingDecision with selected engine and reasoning
        """
        # Explicit directive takes priority
        if explicit_engine is not None:
            return RoutingDecision(
                engine=explicit_engine,
                reason="explicit",
                confidence=1.0,
                suggested_multi_agent=False,
            )

        # Resume token takes priority
        if resume_engine is not None:
            return RoutingDecision(
                engine=resume_engine,
                reason="resume",
                confidence=1.0,
                suggested_multi_agent=False,
            )

        # Analyze prompt content
        liaison_score = self._score_liaison_patterns(prompt)
        simple_score = self._score_simple_patterns(prompt)

        # Determine if liaison is suggested
        suggested_multi_agent = (
            liaison_score >= self.liaison_threshold
            and liaison_score > simple_score
            and self._has_liaison_engine()
        )

        # Pick engine based on analysis
        if suggested_multi_agent and not self.suggest_only:
            return RoutingDecision(
                engine="liaison",
                reason="heuristic",
                confidence=liaison_score,
                suggested_multi_agent=True,
            )

        # Default to router's default engine
        return RoutingDecision(
            engine=self.router.default_engine,
            reason="default",
            confidence=max(simple_score, 0.5),
            suggested_multi_agent=suggested_multi_agent,
        )

    def _score_liaison_patterns(self, prompt: str) -> float:
        """Score how much a prompt suggests multi-agent work."""
        max_score = 0.0
        for pattern, weight in _LIAISON_PATTERNS:
            if pattern.search(prompt):
                max_score = max(max_score, weight)
        return max_score

    def _score_simple_patterns(self, prompt: str) -> float:
        """Score how much a prompt suggests simple single-agent work."""
        max_score = 0.0
        for pattern, weight in _SIMPLE_PATTERNS:
            if pattern.search(prompt):
                max_score = max(max_score, weight)
        return max_score

    def _has_liaison_engine(self) -> bool:
        """Check if liaison engine is available."""
        return "liaison" in self.router.engine_ids


def create_smart_router(
    router: AutoRouter,
    *,
    enabled: bool = False,
    suggest_only: bool = True,
    liaison_threshold: float = 0.70,
) -> SmartRouter | None:
    """Create a SmartRouter if enabled.

    Args:
        router: The AutoRouter instance
        enabled: Whether smart routing is enabled
        suggest_only: If True, only suggest multi-agent, don't auto-switch
        liaison_threshold: Confidence threshold for suggesting liaison

    Returns:
        SmartRouter instance if enabled, None otherwise
    """
    if not enabled:
        return None
    return SmartRouter(
        router=router,
        liaison_threshold=liaison_threshold,
        suggest_only=suggest_only,
    )

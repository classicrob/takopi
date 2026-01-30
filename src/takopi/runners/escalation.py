"""Escalation policy for liaison agents.

Determines when a liaison should escalate questions to the user versus
answering them autonomously.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class EscalationPolicy:
    """Configures when the liaison should escalate to the user.

    The policy uses pattern matching to decide whether to escalate a question
    or handle it automatically. Patterns are checked in order:
    1. always_escalate_patterns - if matched, always ask the user
    2. auto_approve_patterns - if matched, auto-approve without asking
    3. custom_decider - if provided, use for remaining cases
    4. default - escalate (safer default)
    """

    # Always escalate these patterns (destructive, sensitive operations)
    always_escalate_patterns: list[re.Pattern[str]] = field(
        default_factory=lambda: [
            re.compile(r"(?i)delete|remove|destroy|drop|truncate"),
            re.compile(r"(?i)production|prod|live"),
            re.compile(r"(?i)api[- ]?key|secret|password|credential|token"),
            re.compile(r"(?i)billing|payment|cost|charge"),
            re.compile(r"(?i)force|--force|-f\b"),
            re.compile(r"(?i)push.*main|push.*master|merge.*main|merge.*master"),
        ]
    )

    # Never escalate (auto-answer yes) for these safe operations
    auto_approve_patterns: list[re.Pattern[str]] = field(
        default_factory=lambda: [
            re.compile(r"(?i)create.*directory|mkdir"),
            re.compile(r"(?i)install.*dev.*depend"),
            re.compile(r"(?i)run.*test|npm test|pytest|cargo test"),
            re.compile(r"(?i)format.*code|prettier|black|ruff"),
            re.compile(r"(?i)lint|eslint|flake8"),
            re.compile(r"(?i)build|compile"),
            re.compile(r"(?i)read|view|show|list|ls\b|cat\b"),
        ]
    )

    # Timeout before auto-handling (None = always wait for user)
    default_timeout_s: float | None = 300.0  # 5 minutes

    # Custom decision function: (question, context) -> "escalate" | "auto" | None
    custom_decider: Callable[[str, str], str | None] | None = None

    def should_escalate(self, question: str, context: str | None = None) -> bool:
        """Determine if a question should be escalated to the user.

        Args:
            question: The question text from the subagent
            context: Optional additional context about the question

        Returns:
            True if the question should be escalated to the user,
            False if the liaison should handle it automatically
        """
        full_text = f"{question} {context or ''}"

        # Check always-escalate patterns first (safety critical)
        for pattern in self.always_escalate_patterns:
            if pattern.search(full_text):
                return True

        # Check auto-approve patterns (known safe operations)
        for pattern in self.auto_approve_patterns:
            if pattern.search(full_text):
                return False

        # Use custom decider if available
        if self.custom_decider is not None:
            decision = self.custom_decider(question, context or "")
            if decision == "escalate":
                return True
            if decision == "auto":
                return False

        # Default: escalate (safer to ask than assume)
        return True

    def auto_response(self, question: str, context: str | None = None) -> str | None:
        """Get automatic response for non-escalated questions.

        Args:
            question: The question text from the subagent
            context: Optional additional context

        Returns:
            The automatic response string, or None if escalation is needed
        """
        if self.should_escalate(question, context):
            return None

        full_text = f"{question} {context or ''}"

        # Check if it's a yes/no question
        if re.search(r"(?i)y/n|yes.*no|\(y\)|\[y\]", question):
            return "y"

        # Check if it's asking for confirmation
        if re.search(r"(?i)confirm|proceed|continue|ok\?|okay\?", question):
            return "yes"

        # Check if it's asking to press enter
        if re.search(r"(?i)press enter|hit enter|<enter>", question):
            return ""  # Empty string triggers Enter key

        # Default affirmative
        return "yes"

    def assess_urgency(self, question: str, context: str | None = None) -> str:
        """Assess the urgency level of a question.

        Args:
            question: The question text
            context: Optional additional context

        Returns:
            Urgency level: "low", "normal", "high", or "critical"
        """
        full_text = f"{question} {context or ''}"

        # Critical: production, billing, credentials
        critical_patterns = [
            r"(?i)production|prod\s|live\s",
            r"(?i)billing|payment|charge",
            r"(?i)api[- ]?key|secret|password|credential",
        ]
        for pattern in critical_patterns:
            if re.search(pattern, full_text):
                return "critical"

        # High: destructive operations
        high_patterns = [
            r"(?i)delete|remove|destroy|drop|truncate",
            r"(?i)force|--force",
            r"(?i)overwrite|replace.*all",
        ]
        for pattern in high_patterns:
            if re.search(pattern, full_text):
                return "high"

        # Low: routine confirmations
        low_patterns = [
            r"(?i)create.*directory|mkdir",
            r"(?i)install.*depend",
            r"(?i)format|lint",
        ]
        for pattern in low_patterns:
            if re.search(pattern, full_text):
                return "low"

        return "normal"

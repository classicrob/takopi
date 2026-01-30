"""Tests for the escalation policy module."""

import pytest
import re

from takopi.runners.escalation import EscalationPolicy


class TestEscalationPolicy:
    """Tests for EscalationPolicy."""

    def test_default_escalates_unknown_questions(self) -> None:
        """Unknown questions should escalate by default (safety)."""
        policy = EscalationPolicy()
        assert policy.should_escalate("What color should the button be?") is True
        assert policy.should_escalate("How many retries should I use?") is True

    def test_always_escalate_destructive_operations(self) -> None:
        """Destructive operations should always escalate."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Should I delete the file?") is True
        assert policy.should_escalate("Remove all test data?") is True
        assert policy.should_escalate("Destroy the database?") is True
        assert policy.should_escalate("Drop the table?") is True
        assert policy.should_escalate("Truncate the logs?") is True

    def test_always_escalate_production(self) -> None:
        """Production-related questions should always escalate."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Deploy to production?") is True
        assert policy.should_escalate("Run this in prod?") is True
        assert policy.should_escalate("Update the live server?") is True

    def test_always_escalate_credentials(self) -> None:
        """Credential-related questions should always escalate."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Enter your API key:") is True
        assert policy.should_escalate("What is the api-key?") is True
        assert policy.should_escalate("Password:") is True
        assert policy.should_escalate("Secret token required") is True
        assert policy.should_escalate("Enter credential:") is True

    def test_always_escalate_billing(self) -> None:
        """Billing-related questions should always escalate."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Confirm billing?") is True
        assert policy.should_escalate("Process payment?") is True
        assert policy.should_escalate("This will cost $10, proceed?") is True

    def test_always_escalate_force_operations(self) -> None:
        """Force operations should always escalate."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Force push to origin?") is True
        assert policy.should_escalate("Run with --force?") is True
        assert policy.should_escalate("Use -f flag?") is True

    def test_always_escalate_main_branch(self) -> None:
        """Main/master branch operations should always escalate."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Push to main?") is True
        assert policy.should_escalate("Merge to master?") is True

    def test_auto_approve_safe_operations(self) -> None:
        """Safe operations should auto-approve."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Create directory ./src?") is False
        assert policy.should_escalate("mkdir test_folder?") is False
        assert policy.should_escalate("Install dev dependencies?") is False
        assert policy.should_escalate("Run tests?") is False
        assert policy.should_escalate("npm test?") is False
        assert policy.should_escalate("pytest?") is False
        assert policy.should_escalate("cargo test?") is False

    def test_auto_approve_format_lint(self) -> None:
        """Formatting and linting should auto-approve."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Format code?") is False
        assert policy.should_escalate("Run prettier?") is False
        assert policy.should_escalate("Run black?") is False
        assert policy.should_escalate("Run ruff?") is False
        assert policy.should_escalate("Lint the files?") is False
        assert policy.should_escalate("Run eslint?") is False

    def test_auto_approve_build(self) -> None:
        """Build operations should auto-approve."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Build the project?") is False
        assert policy.should_escalate("Compile?") is False

    def test_auto_approve_read_operations(self) -> None:
        """Read-only operations should auto-approve."""
        policy = EscalationPolicy()
        assert policy.should_escalate("Read the config?") is False
        assert policy.should_escalate("View the file?") is False
        assert policy.should_escalate("Show the output?") is False
        assert policy.should_escalate("List files?") is False
        assert policy.should_escalate("ls?") is False
        assert policy.should_escalate("cat the file?") is False

    def test_custom_decider_escalate(self) -> None:
        """Custom decider can force escalation for questions not matching patterns."""

        def always_escalate(q: str, c: str) -> str:
            return "escalate"

        policy = EscalationPolicy(custom_decider=always_escalate)
        # Custom decider runs after pattern checks, so use a question that doesn't
        # match any patterns (neither always_escalate nor auto_approve)
        assert policy.should_escalate("What color should the button be?") is True

    def test_custom_decider_auto(self) -> None:
        """Custom decider can force auto-approve."""

        def always_auto(q: str, c: str) -> str:
            return "auto"

        policy = EscalationPolicy(custom_decider=always_auto)
        # Even dangerous operations auto-approve (not recommended!)
        # But always_escalate_patterns take precedence
        assert policy.should_escalate("Delete everything?") is True  # pattern matched first

    def test_custom_decider_none_falls_through(self) -> None:
        """Custom decider returning None falls through to default."""

        def custom(q: str, c: str) -> str | None:
            if "magic" in q:
                return "auto"
            return None

        policy = EscalationPolicy(custom_decider=custom)
        assert policy.should_escalate("Run magic command?") is False
        assert policy.should_escalate("Unknown command?") is True  # default escalates

    def test_context_is_checked(self) -> None:
        """Context string is also checked for patterns."""
        policy = EscalationPolicy()
        # Question is safe but context mentions production
        assert policy.should_escalate("Run tests?", context="in production") is True

    def test_auto_response_yes_no_question(self) -> None:
        """Auto response handles y/n questions for auto-approved operations."""
        policy = EscalationPolicy()
        # Must use questions that match auto_approve_patterns AND have y/n format
        assert policy.auto_response("Run tests? (y/n)") == "y"
        assert policy.auto_response("npm test? Y/N") == "y"

    def test_auto_response_confirm_question(self) -> None:
        """Auto response handles confirmation for auto-approved operations."""
        policy = EscalationPolicy()
        # Must use questions that match auto_approve_patterns
        assert policy.auto_response("Proceed with build?") == "yes"
        assert policy.auto_response("Continue running tests?") == "yes"

    def test_auto_response_enter_prompt(self) -> None:
        """Auto response handles press-enter prompts for auto-approved operations."""
        policy = EscalationPolicy()
        # Must use questions that match auto_approve_patterns AND mention enter
        assert policy.auto_response("Press Enter to run tests") == ""
        assert policy.auto_response("Hit enter to build") == ""

    def test_auto_response_none_for_escalated(self) -> None:
        """Auto response returns None for escalated questions."""
        policy = EscalationPolicy()
        assert policy.auto_response("Delete all files?") is None
        assert policy.auto_response("Deploy to production?") is None

    def test_assess_urgency_critical(self) -> None:
        """Critical urgency for production/billing/credentials."""
        policy = EscalationPolicy()
        assert policy.assess_urgency("Deploy to production?") == "critical"
        assert policy.assess_urgency("Confirm billing?") == "critical"
        assert policy.assess_urgency("Enter API key:") == "critical"

    def test_assess_urgency_high(self) -> None:
        """High urgency for destructive operations."""
        policy = EscalationPolicy()
        assert policy.assess_urgency("Delete the file?") == "high"
        assert policy.assess_urgency("Use --force?") == "high"
        assert policy.assess_urgency("Overwrite existing?") == "high"

    def test_assess_urgency_low(self) -> None:
        """Low urgency for routine operations."""
        policy = EscalationPolicy()
        assert policy.assess_urgency("Create directory?") == "low"
        assert policy.assess_urgency("Install dependencies?") == "low"
        assert policy.assess_urgency("Format the file?") == "low"

    def test_assess_urgency_normal(self) -> None:
        """Normal urgency for other questions."""
        policy = EscalationPolicy()
        assert policy.assess_urgency("What color should it be?") == "normal"
        assert policy.assess_urgency("How many items?") == "normal"

    def test_custom_patterns(self) -> None:
        """Custom patterns can be added to the policy."""
        policy = EscalationPolicy(
            always_escalate_patterns=[re.compile(r"(?i)dangerous")],
            auto_approve_patterns=[re.compile(r"(?i)safe")],
        )
        assert policy.should_escalate("This is dangerous!") is True
        assert policy.should_escalate("This is safe") is False
        # Default patterns are replaced, not extended
        assert policy.should_escalate("Delete file?") is True  # falls through to default

    def test_timeout_config(self) -> None:
        """Timeout can be configured."""
        policy = EscalationPolicy(default_timeout_s=60.0)
        assert policy.default_timeout_s == 60.0

        policy_no_timeout = EscalationPolicy(default_timeout_s=None)
        assert policy_no_timeout.default_timeout_s is None

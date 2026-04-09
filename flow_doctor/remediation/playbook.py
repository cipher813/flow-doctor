"""Remediation playbook: known failure patterns and auto-fix actions.

Each pattern maps a failure signature to a remediation action. Patterns are
matched by error_type, flow_name, and keyword matching against the error message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RemediationType(str, Enum):
    RESTART_SERVICE = "restart_service"
    RERUN_STEP = "rerun_step"
    UPDATE_CONFIG = "update_config"
    CODE_FIX = "code_fix"
    ESCALATE = "escalate"


@dataclass
class RemediationAction:
    """A specific remediation action to take."""
    action_type: RemediationType
    description: str
    commands: List[str] = field(default_factory=list)
    ssm_target: Optional[str] = None  # EC2 instance tag for SSM
    step_function_arn: Optional[str] = None
    step_function_input: Optional[Dict[str, Any]] = None
    safe_during_market_hours: bool = False
    max_retries: int = 2


@dataclass
class PlaybookPattern:
    """A known failure pattern with its remediation."""
    name: str
    description: str
    category: str  # TRANSIENT, DATA, CODE, CONFIG, EXTERNAL, INFRA
    error_type_pattern: Optional[str] = None  # regex match on error type
    message_pattern: Optional[str] = None  # regex match on error message
    flow_names: Optional[List[str]] = None  # restrict to specific flows
    min_confidence: float = 0.9
    action: RemediationAction = field(default_factory=lambda: RemediationAction(
        action_type=RemediationType.ESCALATE,
        description="Escalate to human",
    ))

    def matches(self, error_type: Optional[str], error_message: str,
                flow_name: Optional[str]) -> bool:
        """Check if this pattern matches the given error."""
        # If pattern requires specific flows and we have a flow_name,
        # reject if it doesn't match. If no flow_name given but pattern
        # requires specific flows, also reject (be strict).
        if self.flow_names:
            if not flow_name or flow_name not in self.flow_names:
                return False

        if self.error_type_pattern and error_type:
            if not re.search(self.error_type_pattern, error_type, re.IGNORECASE):
                return False

        if self.message_pattern:
            if not re.search(self.message_pattern, error_message, re.IGNORECASE):
                return False

        return True


class Playbook:
    """Manages the remediation playbook and matches failures to actions."""

    def __init__(self, patterns: Optional[List[PlaybookPattern]] = None):
        self.patterns = patterns or []

    def match(
        self,
        error_type: Optional[str],
        error_message: str,
        flow_name: Optional[str] = None,
    ) -> Optional[PlaybookPattern]:
        """Find the first matching pattern for the given error."""
        for pattern in self.patterns:
            if pattern.matches(error_type, error_message, flow_name):
                return pattern
        return None

    def match_all(
        self,
        error_type: Optional[str],
        error_message: str,
        flow_name: Optional[str] = None,
    ) -> List[PlaybookPattern]:
        """Find all matching patterns for the given error."""
        return [
            p for p in self.patterns
            if p.matches(error_type, error_message, flow_name)
        ]

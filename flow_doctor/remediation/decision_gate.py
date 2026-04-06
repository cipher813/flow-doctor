"""Decision gate: routes diagnosis to auto-remediate, generate PR, or escalate.

The gate uses diagnosis confidence and category to determine the appropriate
action, with safety rails for market hours and retry limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from flow_doctor.core.models import Diagnosis
from flow_doctor.remediation.playbook import (
    Playbook,
    PlaybookPattern,
    RemediationAction,
    RemediationType,
)

logger = logging.getLogger("flow_doctor.decision_gate")


class DecisionType(str, Enum):
    AUTO_REMEDIATE = "auto_remediate"
    GENERATE_FIX_PR = "generate_fix_pr"
    ESCALATE = "escalate"
    LOG_ONLY = "log_only"


@dataclass
class Decision:
    """The gate's routing decision for a diagnosed failure."""
    decision_type: DecisionType
    reason: str
    diagnosis: Diagnosis
    playbook_match: Optional[PlaybookPattern] = None
    action: Optional[RemediationAction] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GateConfig:
    """Configuration for the decision gate."""
    # Confidence thresholds
    auto_remediate_min_confidence: float = 0.9
    fix_pr_min_confidence: float = 0.8
    # Market hours lockout (ET)
    market_open_hour: int = 9   # 9:30 AM ET
    market_close_hour: int = 16  # 4:00 PM ET
    # Retry limits
    max_auto_remediations_per_day: int = 5
    max_auto_remediations_per_failure: int = 2
    # Categories eligible for auto-remediation
    auto_remediable_categories: List[str] = field(
        default_factory=lambda: ["INFRA", "CONFIG", "TRANSIENT", "DATA"]
    )
    # Categories eligible for PR generation
    pr_eligible_categories: List[str] = field(
        default_factory=lambda: ["CODE", "CONFIG"]
    )


class DecisionGate:
    """Routes diagnosed failures to the appropriate remediation path."""

    def __init__(
        self,
        playbook: Optional[Playbook] = None,
        config: Optional[GateConfig] = None,
    ):
        self.playbook = playbook or Playbook()
        self.config = config or GateConfig()
        self._daily_remediation_count = 0
        self._failure_remediation_counts: Dict[str, int] = {}
        self._last_reset_date: Optional[str] = None

    def decide(
        self,
        diagnosis: Diagnosis,
        error_type: Optional[str] = None,
        error_message: str = "",
        flow_name: Optional[str] = None,
    ) -> Decision:
        """Determine the appropriate action for a diagnosed failure."""
        self._maybe_reset_daily_counts()

        # 1. Check for playbook match
        playbook_match = self.playbook.match(error_type, error_message, flow_name)

        # 2. Route based on confidence and category
        if diagnosis.confidence >= self.config.auto_remediate_min_confidence:
            if diagnosis.category in self.config.auto_remediable_categories:
                return self._try_auto_remediate(diagnosis, playbook_match)

        if diagnosis.confidence >= self.config.fix_pr_min_confidence:
            if diagnosis.category in self.config.pr_eligible_categories:
                return Decision(
                    decision_type=DecisionType.GENERATE_FIX_PR,
                    reason=f"High-confidence {diagnosis.category} issue — generating fix PR",
                    diagnosis=diagnosis,
                    playbook_match=playbook_match,
                )

        if diagnosis.category == "TRANSIENT":
            return Decision(
                decision_type=DecisionType.LOG_ONLY,
                reason="Transient error — log and monitor",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        return Decision(
            decision_type=DecisionType.ESCALATE,
            reason=f"Confidence {diagnosis.confidence:.2f} below threshold — escalating to human",
            diagnosis=diagnosis,
            playbook_match=playbook_match,
        )

    def _try_auto_remediate(
        self,
        diagnosis: Diagnosis,
        playbook_match: Optional[PlaybookPattern],
    ) -> Decision:
        """Attempt auto-remediation with safety checks."""
        # Safety: daily limit
        if self._daily_remediation_count >= self.config.max_auto_remediations_per_day:
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason=f"Daily auto-remediation limit ({self.config.max_auto_remediations_per_day}) reached",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # Safety: per-failure limit
        failure_key = f"{diagnosis.flow_name}:{diagnosis.category}:{diagnosis.root_cause[:50]}"
        failure_count = self._failure_remediation_counts.get(failure_key, 0)
        if failure_count >= self.config.max_auto_remediations_per_failure:
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason=f"Max retries ({self.config.max_auto_remediations_per_failure}) reached for this failure",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # Safety: market hours lockout for trading-path components
        if not self._is_safe_for_auto_remediation(playbook_match):
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason="Market hours lockout — auto-remediation blocked",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # Must have a playbook match with action to auto-remediate
        if not playbook_match:
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason="No playbook pattern — cannot auto-remediate without known fix",
                diagnosis=diagnosis,
            )

        action = playbook_match.action
        if action.action_type == RemediationType.ESCALATE:
            return Decision(
                decision_type=DecisionType.ESCALATE,
                reason=f"Playbook pattern '{playbook_match.name}' requires human intervention",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        if action.action_type == RemediationType.CODE_FIX:
            return Decision(
                decision_type=DecisionType.GENERATE_FIX_PR,
                reason=f"Playbook pattern '{playbook_match.name}' requires code change",
                diagnosis=diagnosis,
                playbook_match=playbook_match,
            )

        # Auto-remediate
        self._daily_remediation_count += 1
        self._failure_remediation_counts[failure_key] = failure_count + 1

        return Decision(
            decision_type=DecisionType.AUTO_REMEDIATE,
            reason=f"Matched playbook pattern '{playbook_match.name}' — auto-remediating",
            diagnosis=diagnosis,
            playbook_match=playbook_match,
            action=action,
        )

    def _is_safe_for_auto_remediation(
        self, playbook_match: Optional[PlaybookPattern],
    ) -> bool:
        """Check if auto-remediation is safe given current market hours."""
        if playbook_match and playbook_match.action.safe_during_market_hours:
            return True

        now = datetime.now(timezone.utc)
        # Convert to ET (UTC-4 EDT / UTC-5 EST — approximate)
        et_hour = (now.hour - 4) % 24
        is_market_hours = self.config.market_open_hour <= et_hour < self.config.market_close_hour
        is_weekday = now.weekday() < 5

        if is_market_hours and is_weekday:
            return False
        return True

    def _maybe_reset_daily_counts(self) -> None:
        """Reset daily counters at midnight."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_remediation_count = 0
            self._failure_remediation_counts.clear()
            self._last_reset_date = today

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


# ── Alpha Engine Playbook ────────────────────────────────────────────────────

ALPHA_ENGINE_PLAYBOOK: List[PlaybookPattern] = [
    # IB Gateway stale session
    PlaybookPattern(
        name="ib_gateway_stale_session",
        description="IB Gateway competing live session (error 10197)",
        category="INFRA",
        message_pattern=r"(10197|competing live session|No market data)",
        flow_names=["executor-planner", "executor-daemon"],
        action=RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="Restart IB Gateway and rerun planner",
            commands=[
                "sudo systemctl restart ibgateway",
                "sleep 30",
                "python executor/main.py",
            ],
            ssm_target="ae-trading",
            safe_during_market_hours=False,
        ),
    ),

    # Daemon not running
    PlaybookPattern(
        name="daemon_not_running",
        description="Alpha Engine daemon not running on trading instance",
        category="INFRA",
        message_pattern=r"(daemon.*not running|systemctl.*inactive|alpha-engine-daemon)",
        flow_names=["executor-daemon"],
        action=RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="Restart the intraday daemon",
            commands=["sudo systemctl restart alpha-engine-daemon"],
            ssm_target="ae-trading",
            safe_during_market_hours=True,
        ),
    ),

    # EC2 instance not started
    PlaybookPattern(
        name="ec2_not_started",
        description="Trading EC2 instance not running",
        category="INFRA",
        message_pattern=r"(instance.*not.*running|ec2.*stopped|connection refused.*4002)",
        action=RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="Start the trading EC2 instance",
            commands=["aws ec2 start-instances --instance-ids $TRADING_INSTANCE_ID --query 'StartingInstances[0].CurrentState.Name' --output text"],
            safe_during_market_hours=True,
        ),
    ),

    # Step Function didn't trigger
    PlaybookPattern(
        name="step_function_not_triggered",
        description="Step Function execution failed to start or was denied",
        category="CONFIG",
        message_pattern=r"(AccessDenied|states:StartExecution|not authorized)",
        flow_names=["weekday-pipeline", "saturday-pipeline"],
        action=RemediationAction(
            action_type=RemediationType.RERUN_STEP,
            description="Manually trigger Step Function execution",
        ),
    ),

    # Lambda timeout
    PlaybookPattern(
        name="lambda_timeout",
        description="Lambda function timed out",
        category="INFRA",
        message_pattern=r"(Task timed out|timeout|TimeoutError)",
        flow_names=["research-lambda", "predictor-inference", "predictor-training"],
        action=RemediationAction(
            action_type=RemediationType.RERUN_STEP,
            description="Retry Lambda execution (may need timeout increase)",
            max_retries=1,
        ),
    ),

    # Data staleness
    PlaybookPattern(
        name="data_staleness",
        description="Stale or missing data in S3",
        category="DATA",
        message_pattern=r"(stale.*data|missing.*signals|no.*daily_closes|NoSuchKey)",
        action=RemediationAction(
            action_type=RemediationType.RERUN_STEP,
            description="Rerun upstream data collection step",
        ),
    ),

    # Holiday false positive
    PlaybookPattern(
        name="holiday_false_positive",
        description="Trading day incorrectly classified as holiday",
        category="CODE",
        message_pattern=r"(holiday.*skip|not.*trading.*day|market.*closed)",
        flow_names=["weekday-pipeline"],
        action=RemediationAction(
            action_type=RemediationType.CODE_FIX,
            description="Fix trading day detection logic",
        ),
    ),

    # yfinance / market data API failure
    PlaybookPattern(
        name="market_data_api_failure",
        description="Market data API (yfinance/Polygon) failure",
        category="EXTERNAL",
        message_pattern=r"(yfinance|polygon|HTTPError.*429|rate.limit|No data found)",
        action=RemediationAction(
            action_type=RemediationType.RERUN_STEP,
            description="Retry after rate limit cooldown",
            max_retries=2,
        ),
    ),

    # S3 access denied
    PlaybookPattern(
        name="s3_access_denied",
        description="S3 access denied — likely IAM policy issue",
        category="CONFIG",
        error_type_pattern=r"(ClientError|AccessDenied|NoCredentialsError)",
        message_pattern=r"(AccessDenied|Access Denied|s3.*forbidden)",
        action=RemediationAction(
            action_type=RemediationType.UPDATE_CONFIG,
            description="Check and update IAM policy for S3 access",
        ),
    ),

    # OOM / memory exhaustion
    PlaybookPattern(
        name="oom_kill",
        description="Process killed by OOM (out of memory)",
        category="INFRA",
        message_pattern=r"(MemoryError|OOM|Killed|Cannot allocate memory|memory.*exceeded)",
        action=RemediationAction(
            action_type=RemediationType.ESCALATE,
            description="Investigate memory usage; may need instance type upgrade",
        ),
    ),
]


class Playbook:
    """Manages the remediation playbook and matches failures to actions."""

    def __init__(self, patterns: Optional[List[PlaybookPattern]] = None):
        self.patterns = patterns or ALPHA_ENGINE_PLAYBOOK

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

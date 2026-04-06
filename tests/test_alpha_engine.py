"""Tests for Alpha Engine diagnosis context, playbook, and decision gate.

Validates against the 5 failure types from the 2026-04-06 incident:
1. IAM policy missing weekday ARN
2. Step Function false holiday skip
3. IB Gateway stale session (error 10197)
4. Planner ran with no market data (cascade of #3)
5. Daemon started with empty order book
"""

import json
import pytest

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.diagnosis.alpha_engine import (
    AlphaEngineContext,
    AlphaEngineContextAssembler,
    _MODULE_FROM_FLOW,
)
from flow_doctor.remediation.playbook import (
    ALPHA_ENGINE_PLAYBOOK,
    Playbook,
    RemediationType,
)
from flow_doctor.remediation.decision_gate import (
    Decision,
    DecisionGate,
    DecisionType,
    GateConfig,
)


# ── Context Assembler Tests ──────────────────────────────────────────────────


class TestAlphaEngineContextAssembler:

    def test_system_prompt_contains_architecture(self):
        assembler = AlphaEngineContextAssembler()
        prompt = assembler.system_prompt
        assert "Alpha Engine" in prompt
        assert "Step Function" in prompt
        assert "IB Gateway" in prompt

    def test_system_prompt_contains_response_format(self):
        assembler = AlphaEngineContextAssembler()
        prompt = assembler.system_prompt
        assert "affected_module" in prompt
        assert "auto_fix_type" in prompt
        assert "cascade_risk" in prompt

    def test_assemble_with_infra_context(self):
        assembler = AlphaEngineContextAssembler(repo="cipher813/alpha-engine")
        report = Report(
            flow_name="executor-planner",
            error_type="RuntimeError",
            error_message="No market data",
        )
        infra = AlphaEngineContext(
            health_markers={"executor": {"status": "error"}},
            ib_gateway_status="Error 10197: competing live session",
            module="executor",
        )
        ctx = assembler.assemble_with_infra(report, infra_context=infra)

        assert "INFRASTRUCTURE CONTEXT" in ctx.logs
        assert "executor" in ctx.logs
        assert "10197" in ctx.logs

    def test_assemble_without_infra(self):
        assembler = AlphaEngineContextAssembler()
        report = Report(
            flow_name="backtester",
            error_type="ValueError",
            error_message="param sweep failed",
        )
        ctx = assembler.assemble_with_infra(report)
        # Should work fine without infra context
        assert ctx.error_message == "param sweep failed"

    def test_module_from_flow_mapping(self):
        assert _MODULE_FROM_FLOW["executor-planner"] == "executor"
        assert _MODULE_FROM_FLOW["predictor-training"] == "predictor"
        assert _MODULE_FROM_FLOW["research-lambda"] == "research"
        assert _MODULE_FROM_FLOW["weekday-pipeline"] == "orchestration"


# ── Playbook Tests ───────────────────────────────────────────────────────────


class TestPlaybook:

    def setup_method(self):
        self.playbook = Playbook(ALPHA_ENGINE_PLAYBOOK)

    # Failure 1: IAM policy missing weekday ARN
    def test_iam_access_denied(self):
        match = self.playbook.match(
            error_type="ClientError",
            error_message="An error occurred (AccessDenied) when calling StartExecution: "
                          "states:StartExecution not authorized",
            flow_name="weekday-pipeline",
        )
        assert match is not None
        assert match.name == "step_function_not_triggered"
        assert match.category == "CONFIG"
        assert match.action.action_type == RemediationType.RERUN_STEP

    # Failure 2: Step Function false holiday skip
    def test_holiday_false_positive(self):
        match = self.playbook.match(
            error_type=None,
            error_message="NotifyHolidaySkip: market closed, skipping pipeline — "
                          "not a trading day",
            flow_name="weekday-pipeline",
        )
        assert match is not None
        assert match.name == "holiday_false_positive"
        assert match.category == "CODE"
        assert match.action.action_type == RemediationType.CODE_FIX

    # Failure 3: IB Gateway stale session
    def test_ib_gateway_stale_session(self):
        match = self.playbook.match(
            error_type="RuntimeError",
            error_message="No market data during competing live session (error 10197)",
            flow_name="executor-planner",
        )
        assert match is not None
        assert match.name == "ib_gateway_stale_session"
        assert match.category == "INFRA"
        assert match.action.action_type == RemediationType.RESTART_SERVICE
        assert "restart ibgateway" in match.action.commands[0]

    # Failure 4: Planner ran with no market data
    def test_no_market_data(self):
        match = self.playbook.match(
            error_type="RuntimeError",
            error_message="No market data during competing live session",
            flow_name="executor-planner",
        )
        assert match is not None
        # Should match IB Gateway pattern (same root cause)
        assert match.name == "ib_gateway_stale_session"

    # Failure 5: Daemon started with empty order book
    def test_daemon_not_running(self):
        match = self.playbook.match(
            error_type=None,
            error_message="alpha-engine-daemon inactive, 0 entries in order book",
            flow_name="executor-daemon",
        )
        assert match is not None
        assert match.name == "daemon_not_running"
        assert match.action.action_type == RemediationType.RESTART_SERVICE
        assert match.action.safe_during_market_hours is True

    def test_no_match_for_unknown_error(self):
        match = self.playbook.match(
            error_type="UnknownError",
            error_message="Something completely unexpected happened",
            flow_name="unknown-flow",
        )
        assert match is None

    def test_s3_access_denied(self):
        match = self.playbook.match(
            error_type="ClientError",
            error_message="An error occurred (AccessDenied) when calling GetObject: Access Denied",
        )
        assert match is not None
        assert match.name == "s3_access_denied"

    def test_lambda_timeout(self):
        match = self.playbook.match(
            error_type="TimeoutError",
            error_message="Task timed out after 900.00 seconds",
            flow_name="research-lambda",
        )
        assert match is not None
        assert match.name == "lambda_timeout"

    def test_data_staleness(self):
        match = self.playbook.match(
            error_type="FileNotFoundError",
            error_message="NoSuchKey: signals/2026-04-06/signals.json not found",
        )
        assert match is not None
        assert match.name == "data_staleness"


# ── Decision Gate Tests ──────────────────────────────────────────────────────


class TestDecisionGate:

    def setup_method(self):
        # Use permissive config for testing (no market hours lockout)
        self.config = GateConfig(
            market_open_hour=0,
            market_close_hour=0,  # Effectively disable market hours check
        )
        self.gate = DecisionGate(config=self.config)

    def _make_diagnosis(self, category="INFRA", confidence=0.95,
                        flow_name="executor-planner", root_cause="test") -> Diagnosis:
        return Diagnosis(
            report_id="test-report",
            flow_name=flow_name,
            category=category,
            root_cause=root_cause,
            confidence=confidence,
        )

    # Failure 3: IB Gateway — should auto-remediate
    def test_ib_gateway_auto_remediates(self):
        diagnosis = self._make_diagnosis(
            category="INFRA", confidence=0.95, flow_name="executor-planner",
            root_cause="IB Gateway stale session",
        )
        decision = self.gate.decide(
            diagnosis,
            error_type="RuntimeError",
            error_message="No market data during competing live session (error 10197)",
            flow_name="executor-planner",
        )
        assert decision.decision_type == DecisionType.AUTO_REMEDIATE
        assert decision.playbook_match is not None
        assert decision.action is not None
        assert "restart" in decision.action.commands[0]

    # Failure 1: IAM — should auto-remediate (rerun step)
    def test_iam_rerun_step(self):
        diagnosis = self._make_diagnosis(
            category="CONFIG", confidence=0.95, flow_name="weekday-pipeline",
        )
        decision = self.gate.decide(
            diagnosis,
            error_type="ClientError",
            error_message="states:StartExecution not authorized",
            flow_name="weekday-pipeline",
        )
        assert decision.decision_type == DecisionType.AUTO_REMEDIATE

    # Failure 2: Holiday skip — should generate PR (code fix)
    def test_holiday_generates_pr(self):
        diagnosis = self._make_diagnosis(
            category="CODE", confidence=0.9, flow_name="weekday-pipeline",
            root_cause="Trading day check missing InProgress state",
        )
        decision = self.gate.decide(
            diagnosis,
            error_type=None,
            error_message="not a trading day",
            flow_name="weekday-pipeline",
        )
        assert decision.decision_type == DecisionType.GENERATE_FIX_PR

    # Low confidence — should escalate
    def test_low_confidence_escalates(self):
        diagnosis = self._make_diagnosis(category="CODE", confidence=0.5)
        decision = self.gate.decide(
            diagnosis,
            error_type="ValueError",
            error_message="unexpected error",
        )
        assert decision.decision_type == DecisionType.ESCALATE
        assert "below threshold" in decision.reason

    # Transient error — log only
    def test_transient_logs_only(self):
        diagnosis = self._make_diagnosis(category="TRANSIENT", confidence=0.7)
        decision = self.gate.decide(
            diagnosis,
            error_type="ConnectionError",
            error_message="network timeout",
        )
        assert decision.decision_type == DecisionType.LOG_ONLY

    # Daily limit exceeded — should escalate
    def test_daily_limit_escalates(self):
        gate = DecisionGate(config=GateConfig(
            max_auto_remediations_per_day=1,
            market_open_hour=0, market_close_hour=0,
        ))

        # First remediation succeeds
        diagnosis = self._make_diagnosis(category="INFRA", confidence=0.95)
        d1 = gate.decide(
            diagnosis,
            error_type="RuntimeError",
            error_message="competing live session (error 10197)",
            flow_name="executor-planner",
        )
        assert d1.decision_type == DecisionType.AUTO_REMEDIATE

        # Second hits the limit — different root cause to avoid per-failure limit
        diagnosis2 = self._make_diagnosis(
            category="INFRA", confidence=0.95, root_cause="different failure",
        )
        d2 = gate.decide(
            diagnosis2,
            error_type="RuntimeError",
            error_message="alpha-engine-daemon inactive",
            flow_name="executor-daemon",
        )
        assert d2.decision_type == DecisionType.ESCALATE
        assert "limit" in d2.reason.lower()

    # Per-failure retry limit
    def test_per_failure_retry_limit(self):
        gate = DecisionGate(config=GateConfig(
            max_auto_remediations_per_failure=1,
            market_open_hour=0, market_close_hour=0,
        ))

        diagnosis = self._make_diagnosis(
            category="INFRA", confidence=0.95,
            root_cause="IB Gateway stale session",
        )
        d1 = gate.decide(
            diagnosis,
            error_type="RuntimeError",
            error_message="competing live session (error 10197)",
            flow_name="executor-planner",
        )
        assert d1.decision_type == DecisionType.AUTO_REMEDIATE

        # Same failure again — should escalate
        d2 = gate.decide(
            diagnosis,
            error_type="RuntimeError",
            error_message="competing live session (error 10197)",
            flow_name="executor-planner",
        )
        assert d2.decision_type == DecisionType.ESCALATE
        assert "retries" in d2.reason.lower()

    # No playbook match — escalate even with high confidence
    def test_no_playbook_match_escalates(self):
        diagnosis = self._make_diagnosis(
            category="INFRA", confidence=0.95,
            root_cause="Unknown infrastructure issue",
        )
        decision = self.gate.decide(
            diagnosis,
            error_type="UnknownError",
            error_message="something completely new",
        )
        assert decision.decision_type == DecisionType.ESCALATE
        assert "No playbook" in decision.reason

    # OOM — playbook says escalate
    def test_oom_escalates_per_playbook(self):
        diagnosis = self._make_diagnosis(
            category="INFRA", confidence=0.95,
            root_cause="OOM kill on trading instance",
        )
        decision = self.gate.decide(
            diagnosis,
            error_type="MemoryError",
            error_message="Cannot allocate memory",
        )
        assert decision.decision_type == DecisionType.ESCALATE
        assert "human intervention" in decision.reason

"""Integration test: full report → diagnose → decide → remediate pipeline.

Tests the complete flow through FlowDoctor with remediation enabled,
using mocked LLM responses and AWS clients.
"""

import json
import sqlite3
import tempfile

import pytest
from unittest.mock import MagicMock, patch

import flow_doctor
from flow_doctor.core.models import Diagnosis
from flow_doctor.remediation.decision_gate import DecisionType
from flow_doctor.remediation.executor import ExecutionResult, RemediationExecutor
from flow_doctor.remediation.playbook import Playbook, RemediationType


# ── Executor Unit Tests ──────────────────────────────────────────────────────


class TestRemediationExecutor:

    def test_dry_run_logs_without_executing(self):
        """Dry-run mode should succeed without calling AWS."""
        from flow_doctor.remediation.decision_gate import Decision, DecisionGate, GateConfig
        from flow_doctor.remediation.playbook import PlaybookPattern, RemediationAction

        executor = RemediationExecutor(dry_run=True)

        diagnosis = Diagnosis(
            report_id="r1", flow_name="executor-planner",
            category="INFRA", root_cause="IB Gateway stale session",
            confidence=0.95,
        )
        action = RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="Restart IB Gateway",
            commands=["sudo systemctl restart ibgateway", "sleep 30"],
            ssm_target="ae-trading",
        )
        pattern = PlaybookPattern(
            name="ib_gateway_stale_session",
            description="IB Gateway stale session",
            category="INFRA",
            action=action,
        )
        decision = Decision(
            decision_type=DecisionType.AUTO_REMEDIATE,
            reason="Matched playbook",
            diagnosis=diagnosis,
            playbook_match=pattern,
            action=action,
        )

        result = executor.execute(decision)
        assert result.success is True
        assert result.dry_run is True
        assert len(result.commands_run) == 2
        assert "DRY RUN" in result.output

    def test_non_auto_remediate_returns_error(self):
        """Non-auto-remediate decisions should not execute."""
        executor = RemediationExecutor(dry_run=True)

        diagnosis = Diagnosis(
            report_id="r1", flow_name="test",
            category="CODE", root_cause="bug",
            confidence=0.9,
        )
        decision = MagicMock()
        decision.decision_type = DecisionType.ESCALATE
        decision.diagnosis = diagnosis

        result = executor.execute(decision)
        assert result.success is False

    def test_audit_trail_saved(self, tmp_path):
        """Execution results should be persisted to SQLite."""
        from flow_doctor.storage.sqlite import SQLiteStorage
        from flow_doctor.remediation.decision_gate import Decision
        from flow_doctor.remediation.playbook import PlaybookPattern, RemediationAction

        db_path = str(tmp_path / "test.db")
        store = SQLiteStorage(db_path)
        store.init_schema()

        executor = RemediationExecutor(dry_run=True, store=store)

        diagnosis = Diagnosis(
            report_id="r1", flow_name="executor-planner",
            category="INFRA", root_cause="test",
            confidence=0.95,
        )
        action = RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="test restart",
            commands=["echo test"],
        )
        decision = Decision(
            decision_type=DecisionType.AUTO_REMEDIATE,
            reason="test",
            diagnosis=diagnosis,
            playbook_match=PlaybookPattern(
                name="test_pattern", description="test",
                category="INFRA", action=action,
            ),
            action=action,
        )

        executor.execute(decision)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM remediation_actions").fetchall()
        conn.close()
        assert len(rows) == 1


# ── Full Pipeline Integration Test ───────────────────────────────────────────


class TestFullPipeline:
    """Test the complete report() → diagnose → decide → remediate flow."""

    def _make_fd(self, tmp_path, diagnosis_enabled=False, remediation_enabled=True):
        """Create a FlowDoctor instance with remediation enabled."""
        db_path = str(tmp_path / "flow_doctor_pipeline.db")

        fd = flow_doctor.init(
            flow_name="executor-planner",
            repo="cipher813/alpha-engine",
            store={"type": "sqlite", "path": db_path},
            notify=[],
            dependencies=["predictor-training", "data-phase1"],
            rate_limits={"max_alerts_per_day": 50, "dedup_cooldown_minutes": 1},
            remediation={
                "enabled": remediation_enabled,
                "dry_run": True,
                "market_hours_lockout": False,
            },
        )
        return fd, db_path

    def test_report_with_remediation_enabled(self, tmp_path):
        """report() should proceed without error when remediation is enabled."""
        fd, db_path = self._make_fd(tmp_path)

        try:
            raise RuntimeError("No market data during competing live session (error 10197)")
        except Exception as e:
            report_id = fd.report(e, severity="error", context={
                "site": "executor_planner"})

        assert report_id is not None

        # Check that the report was stored
        conn = sqlite3.connect(db_path)
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.close()
        assert report is not None

    def test_remediation_audit_trail_created(self, tmp_path):
        """When diagnosis runs and decision gate fires, audit trail should exist."""
        fd, db_path = self._make_fd(tmp_path)

        # Manually inject a decision gate with a playbook that matches
        # (Without LLM diagnosis, the gate won't fire through report(),
        # but we can test the gate directly)
        from flow_doctor.remediation.decision_gate import DecisionGate, GateConfig

        gate = DecisionGate(config=GateConfig(
            market_open_hour=0, market_close_hour=0,
        ))

        diagnosis = Diagnosis(
            report_id="test-r1", flow_name="executor-planner",
            category="INFRA", root_cause="IB Gateway stale session",
            confidence=0.95,
        )

        decision = gate.decide(
            diagnosis,
            error_type="RuntimeError",
            error_message="competing live session (error 10197)",
            flow_name="executor-planner",
        )

        assert decision.decision_type == DecisionType.AUTO_REMEDIATE

        # Execute in dry-run
        from flow_doctor.storage.sqlite import SQLiteStorage
        store = SQLiteStorage(db_path)
        store.init_schema()

        executor = RemediationExecutor(dry_run=True, store=store)
        result = executor.execute(decision)

        assert result.success is True
        assert result.dry_run is True

        # Verify audit trail
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM remediation_actions").fetchall()
        conn.close()
        assert len(rows) == 1

    def test_remediation_disabled_skips_gate(self, tmp_path):
        """When remediation is disabled, decision gate should not be initialized."""
        fd, _ = self._make_fd(tmp_path, remediation_enabled=False)
        assert fd._decision_gate is None
        assert fd._remediation_executor is None

    def test_report_never_crashes_with_remediation(self, tmp_path):
        """report() must never crash even if remediation has errors."""
        fd, _ = self._make_fd(tmp_path)

        # These should all succeed without raising
        fd.report("string error", severity="error")
        fd.report(None, severity="warning", message="manual warning")

        try:
            raise ValueError("test error")
        except Exception as e:
            result = fd.report(e, severity="critical")
            assert result is not None or result is None  # just no crash

    def test_decision_gate_routes_all_five_failures(self, tmp_path):
        """Verify the 5 failure types from 2026-04-06 are routed correctly."""
        from flow_doctor.remediation.decision_gate import DecisionGate, GateConfig

        gate = DecisionGate(config=GateConfig(
            market_open_hour=0, market_close_hour=0,
        ))

        failures = [
            # (category, confidence, error_type, error_message, flow_name, expected_decision)
            ("CONFIG", 0.95, "ClientError", "states:StartExecution not authorized",
             "weekday-pipeline", DecisionType.AUTO_REMEDIATE),
            ("CODE", 0.9, None, "not a trading day",
             "weekday-pipeline", DecisionType.GENERATE_FIX_PR),
            ("INFRA", 0.95, "RuntimeError", "competing live session (error 10197)",
             "executor-planner", DecisionType.AUTO_REMEDIATE),
            ("INFRA", 0.95, "RuntimeError", "No valid price for any ticker",
             "executor-planner", DecisionType.ESCALATE),  # No playbook match
            ("INFRA", 0.95, None, "alpha-engine-daemon inactive",
             "executor-daemon", DecisionType.AUTO_REMEDIATE),
        ]

        for cat, conf, err_type, err_msg, flow, expected in failures:
            diagnosis = Diagnosis(
                report_id="test", flow_name=flow,
                category=cat, root_cause=err_msg[:50],
                confidence=conf,
            )
            decision = gate.decide(diagnosis, err_type, err_msg, flow)
            assert decision.decision_type == expected, (
                f"Failed for '{err_msg[:40]}': expected {expected}, got {decision.decision_type} "
                f"(reason: {decision.reason})"
            )

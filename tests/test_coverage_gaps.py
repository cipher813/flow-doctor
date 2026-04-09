"""Tests to cover notification formatters and remediation executor dispatch."""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from flow_doctor.core.models import Diagnosis, Report, Severity
from flow_doctor.notify.email import EmailNotifier
from flow_doctor.notify.slack import SlackNotifier
from flow_doctor.remediation.executor import ExecutionResult, RemediationExecutor
from flow_doctor.remediation.decision_gate import Decision, DecisionType
from flow_doctor.remediation.playbook import (
    PlaybookPattern, RemediationAction, RemediationType,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_report(
    severity="error",
    error_type="RuntimeError",
    error_message="something broke",
    traceback="Traceback:\n  File x.py\n    raise RuntimeError",
    logs=None,
    cascade_source=None,
):
    return Report(
        flow_name="test-flow",
        severity=severity,
        error_type=error_type,
        error_message=error_message,
        traceback=traceback,
        logs=logs,
        cascade_source=cascade_source,
    )


def _make_diagnosis(category="CODE", confidence=0.92):
    return Diagnosis(
        report_id="r1",
        flow_name="test-flow",
        category=category,
        root_cause="Missing null check in parser",
        confidence=confidence,
        remediation="Add a null guard before accessing .data",
        auto_fixable=True,
        affected_files=["src/parser.py"],
        alternative_hypotheses=["Could be a data format change"],
        source="anthropic",
        tokens_used=500,
        cost_usd=0.003,
    )


def _make_decision(action_type=RemediationType.RESTART_SERVICE, commands=None):
    action = RemediationAction(
        action_type=action_type,
        description="Restart the service",
        commands=commands or ["sudo systemctl restart app"],
        ssm_target="app-server",
    )
    pattern = PlaybookPattern(
        name="test_pattern",
        description="Test",
        category="INFRA",
        action=action,
    )
    return Decision(
        decision_type=DecisionType.AUTO_REMEDIATE,
        reason="High confidence",
        diagnosis=_make_diagnosis("INFRA"),
        playbook_match=pattern,
        action=action,
    )


# ── Email Notifier Tests ───────────────────────────────────────────────────


class TestEmailFormatter:
    def test_format_body_basic(self):
        report = _make_report()
        body = EmailNotifier._format_body(report, "my-flow")
        assert "Flow Doctor Alert: my-flow" in body
        assert "ERROR" in body
        assert "RuntimeError: something broke" in body

    def test_format_body_with_diagnosis(self):
        report = _make_report()
        diag = _make_diagnosis()
        body = EmailNotifier._format_body(report, "my-flow", diag)
        assert "DIAGNOSIS" in body
        assert "CODE" in body
        assert "92%" in body
        assert "Missing null check" in body
        assert "src/parser.py" in body
        assert "Auto-fixable: Yes" in body
        assert "Alternative Hypotheses" in body

    def test_format_body_message_only(self):
        report = _make_report(error_type=None, error_message="no candidates")
        body = EmailNotifier._format_body(report, "scanner")
        assert "Message: no candidates" in body

    def test_format_body_cascade(self):
        report = _make_report(cascade_source="upstream-data")
        body = EmailNotifier._format_body(report, "my-flow")
        assert "upstream" in body
        assert "upstream-data" in body

    def test_format_body_with_logs(self):
        report = _make_report(logs="line1\nline2\nline3")
        body = EmailNotifier._format_body(report, "my-flow")
        assert "Captured Logs" in body
        assert "line1" in body

    def test_send_returns_false_on_connection_error(self):
        notifier = EmailNotifier(
            sender="test@example.com",
            recipients="admin@example.com",
            smtp_host="invalid.host.example.com",
            smtp_port=9999,
        )
        report = _make_report()
        result = notifier.send(report, "test-flow")
        assert result is False

    def test_init_splits_recipients(self):
        notifier = EmailNotifier(
            sender="test@example.com",
            recipients="a@b.com, c@d.com, e@f.com",
        )
        assert notifier.recipients == ["a@b.com", "c@d.com", "e@f.com"]


# ── Slack Notifier Tests ──────────────────────────────────────────────────


class TestSlackFormatter:
    def test_format_message_error(self):
        report = _make_report(severity="error")
        msg = SlackNotifier._format_message(report, "my-flow")
        assert "*[ERROR] my-flow*" in msg
        assert "RuntimeError" in msg

    def test_format_message_critical(self):
        report = _make_report(severity="critical")
        msg = SlackNotifier._format_message(report, "my-flow")
        assert "*[CRITICAL] my-flow*" in msg

    def test_format_message_warning(self):
        report = _make_report(severity="warning")
        msg = SlackNotifier._format_message(report, "my-flow")
        assert "*[WARNING] my-flow*" in msg

    def test_format_message_with_diagnosis(self):
        report = _make_report()
        diag = _make_diagnosis(category="INFRA", confidence=0.95)
        msg = SlackNotifier._format_message(report, "my-flow", diag)
        assert "INFRA" in msg
        assert "95%" in msg
        assert "Missing null check" in msg

    def test_format_message_no_error_type(self):
        report = _make_report(error_type=None, error_message="scanner empty")
        msg = SlackNotifier._format_message(report, "my-flow")
        assert "*Message:*" in msg
        assert "scanner empty" in msg

    def test_format_message_cascade(self):
        report = _make_report(cascade_source="data-pipeline")
        msg = SlackNotifier._format_message(report, "my-flow")
        assert "data-pipeline" in msg

    def test_format_message_traceback(self):
        report = _make_report(traceback="line1\nline2\nline3\nline4\nline5\nline6")
        msg = SlackNotifier._format_message(report, "my-flow")
        assert "```" in msg
        # Only last 5 lines
        assert "line2" in msg
        assert "line6" in msg

    def test_send_returns_false_on_error(self):
        notifier = SlackNotifier(
            webhook_url="http://invalid.host.example.com/webhook",
            channel="#test",
        )
        report = _make_report()
        result = notifier.send(report, "test-flow")
        assert result is False


# ── Remediation Executor Tests ────────────────────────────────────────────


class TestRemediationExecutor:
    def test_non_auto_remediate_decision_returns_failure(self):
        executor = RemediationExecutor(dry_run=True)
        decision = Decision(
            decision_type=DecisionType.ESCALATE,
            reason="Low confidence",
            diagnosis=_make_diagnosis(),
        )
        result = executor.execute(decision)
        assert result.success is False
        assert "not auto-remediate" in result.error

    def test_no_action_returns_failure(self):
        executor = RemediationExecutor(dry_run=True)
        decision = Decision(
            decision_type=DecisionType.AUTO_REMEDIATE,
            reason="Test",
            diagnosis=_make_diagnosis(),
            action=None,
        )
        result = executor.execute(decision)
        assert result.success is False
        assert "No remediation action" in result.error

    def test_dry_run_ssm_succeeds(self):
        executor = RemediationExecutor(dry_run=True)
        decision = _make_decision()
        result = executor.execute(decision)
        assert result.success is True
        assert result.dry_run is True
        assert "DRY RUN" in result.output

    def test_no_commands_dry_run_still_succeeds(self):
        executor = RemediationExecutor(dry_run=True)
        decision = _make_decision(commands=[])
        result = executor.execute(decision)
        # Dry run with empty commands still reports success
        assert result.dry_run is True

    def test_no_ssm_client_returns_failure(self):
        executor = RemediationExecutor(dry_run=False)
        decision = _make_decision()
        result = executor.execute(decision)
        assert result.success is False
        assert "SSM client not configured" in result.error

    def test_unsupported_action_type(self):
        executor = RemediationExecutor(dry_run=False)
        decision = _make_decision(action_type=RemediationType.CODE_FIX)
        result = executor.execute(decision)
        assert result.success is False
        assert "Unsupported" in result.error

    def test_config_update_escalates(self):
        executor = RemediationExecutor(dry_run=False)
        decision = _make_decision(action_type=RemediationType.UPDATE_CONFIG)
        result = executor.execute(decision)
        assert result.success is False

    def test_rerun_step_dry_run(self):
        executor = RemediationExecutor(dry_run=True)
        action = RemediationAction(
            action_type=RemediationType.RERUN_STEP,
            description="Rerun step",
            step_function_arn="arn:aws:states:us-east-1:123:stateMachine:test",
        )
        pattern = PlaybookPattern(
            name="test", description="Test", category="INFRA", action=action,
        )
        decision = Decision(
            decision_type=DecisionType.AUTO_REMEDIATE,
            reason="Test",
            diagnosis=_make_diagnosis("INFRA"),
            playbook_match=pattern,
            action=action,
        )
        result = executor.execute(decision)
        assert result.success is True
        assert result.dry_run is True


# ── Digest Generator Tests ────────────────────────────────────────────────


class TestDigestGenerator:
    def test_generate_returns_none_when_empty(self, tmp_path):
        from flow_doctor.storage.sqlite import SQLiteStorage
        from flow_doctor.digest.generator import DigestGenerator

        store = SQLiteStorage(str(tmp_path / "digest.db"))
        store.init_schema()
        gen = DigestGenerator(store)
        result = gen.generate()
        assert result is None

    def test_generate_with_degraded_actions(self, tmp_path):
        from flow_doctor.storage.sqlite import SQLiteStorage
        from flow_doctor.digest.generator import DigestGenerator
        from flow_doctor.core.models import Report, Action, ActionType, ActionStatus

        store = SQLiteStorage(str(tmp_path / "digest.db"))
        store.init_schema()

        report = _make_report()
        store.save_report(report)

        action = Action(
            report_id=report.id,
            action_type=ActionType.EMAIL_ALERT,
            target="test@example.com",
            status=ActionStatus.DEGRADED,
        )
        store.save_action(action)

        gen = DigestGenerator(store)
        result = gen.generate()
        assert result is not None
        assert "test-flow" in result

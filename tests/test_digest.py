"""Tests for daily digest generation."""

import tempfile
from datetime import datetime, timedelta

from flow_doctor.core.models import Action, ActionStatus, ActionType, Diagnosis, Report
from flow_doctor.digest.generator import DigestGenerator
from flow_doctor.storage.sqlite import SQLiteStorage


def _make_store():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = SQLiteStorage(f.name)
    store.init_schema()
    return store


def test_generate_empty():
    store = _make_store()
    gen = DigestGenerator(store)
    result = gen.generate()
    assert result is None


def test_generate_with_degraded_actions():
    store = _make_store()

    # Create a report
    report = Report(
        flow_name="research-lambda",
        error_message="Scanner returned 0 candidates",
        error_type="ValueError",
        severity="error",
    )
    store.save_report(report)

    # Create degraded actions
    action1 = Action(
        report_id=report.id,
        action_type=ActionType.SLACK_ALERT.value,
        status=ActionStatus.DEGRADED.value,
        target="degraded - queued for digest",
    )
    action2 = Action(
        report_id=report.id,
        action_type=ActionType.EMAIL_ALERT.value,
        status=ActionStatus.DEGRADED.value,
        target="degraded - queued for digest",
    )
    store.save_action(action1)
    store.save_action(action2)

    gen = DigestGenerator(store)
    result = gen.generate()

    assert result is not None
    assert "Daily Digest" in result
    assert "research-lambda" in result
    assert "Scanner returned 0 candidates" in result
    assert "Suppressed alerts:" in result
    assert "slack_alert" in result
    assert "email_alert" in result


def test_generate_with_diagnosis():
    store = _make_store()

    report = Report(
        flow_name="predictor-inference",
        error_message="KeyError: 'AAPL'",
        error_type="KeyError",
        severity="error",
    )
    store.save_report(report)

    diag = Diagnosis(
        report_id=report.id,
        flow_name="predictor-inference",
        category="CODE",
        root_cause="New ticker missing expected columns",
        confidence=0.85,
    )
    store.save_diagnosis(diag)

    action = Action(
        report_id=report.id,
        action_type=ActionType.SLACK_ALERT.value,
        status=ActionStatus.DEGRADED.value,
    )
    store.save_action(action)

    gen = DigestGenerator(store)
    result = gen.generate()

    assert result is not None
    assert "CODE" in result
    assert "New ticker missing expected columns" in result


def test_generate_multiple_reports():
    store = _make_store()

    for i in range(3):
        report = Report(
            flow_name=f"flow-{i}",
            error_message=f"Error {i}",
            severity="error",
        )
        store.save_report(report)
        action = Action(
            report_id=report.id,
            action_type=ActionType.SLACK_ALERT.value,
            status=ActionStatus.DEGRADED.value,
        )
        store.save_action(action)

    gen = DigestGenerator(store)
    result = gen.generate()

    assert result is not None
    assert "Unique reports:** 3" in result
    assert "flow-0" in result
    assert "flow-1" in result
    assert "flow-2" in result


def test_generate_respects_since():
    store = _make_store()

    report = Report(
        flow_name="old-flow",
        error_message="Old error",
        severity="error",
    )
    store.save_report(report)
    action = Action(
        report_id=report.id,
        action_type=ActionType.SLACK_ALERT.value,
        status=ActionStatus.DEGRADED.value,
    )
    store.save_action(action)

    # Use a future since time — should find nothing
    gen = DigestGenerator(store)
    result = gen.generate(since=datetime.utcnow() + timedelta(hours=1))
    assert result is None


def test_generate_dedup_count():
    store = _make_store()

    report = Report(
        flow_name="test-flow",
        error_message="Repeated error",
        severity="error",
        dedup_count=5,
    )
    store.save_report(report)
    action = Action(
        report_id=report.id,
        action_type=ActionType.SLACK_ALERT.value,
        status=ActionStatus.DEGRADED.value,
    )
    store.save_action(action)

    gen = DigestGenerator(store)
    result = gen.generate()

    assert "Occurrences:** 5" in result

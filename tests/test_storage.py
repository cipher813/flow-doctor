"""Tests for SQLite storage backend."""

import tempfile
from datetime import datetime, timedelta

from flow_doctor.core.models import Action, ActionStatus, ActionType, Report
from flow_doctor.storage.sqlite import SQLiteStorage


def _make_store():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = SQLiteStorage(f.name)
    store.init_schema()
    return store, f.name


def test_init_schema():
    store, _ = _make_store()
    # Should not raise on double init
    store.init_schema()


def test_save_and_get_report():
    store, _ = _make_store()
    report = Report(
        flow_name="test",
        error_message="boom",
        severity="error",
        error_type="ValueError",
        error_signature="sig123",
        context={"key": "value"},
    )
    store.save_report(report)

    reports = store.get_reports(flow_name="test")
    assert len(reports) == 1
    assert reports[0].id == report.id
    assert reports[0].error_message == "boom"
    assert reports[0].context == {"key": "value"}


def test_get_reports_limit():
    store, _ = _make_store()
    for i in range(5):
        store.save_report(Report(flow_name="test", error_message=f"error {i}"))

    reports = store.get_reports(limit=3)
    assert len(reports) == 3


def test_get_reports_filter_by_flow():
    store, _ = _make_store()
    store.save_report(Report(flow_name="flow-a", error_message="a"))
    store.save_report(Report(flow_name="flow-b", error_message="b"))

    a_reports = store.get_reports(flow_name="flow-a")
    assert len(a_reports) == 1
    assert a_reports[0].flow_name == "flow-a"


def test_find_report_by_signature():
    store, _ = _make_store()
    report = Report(
        flow_name="test",
        error_message="boom",
        error_signature="sig456",
    )
    store.save_report(report)

    found = store.find_report_by_signature(
        "sig456",
        since=datetime.utcnow() - timedelta(hours=1),
    )
    assert found is not None
    assert found.id == report.id


def test_find_report_by_signature_expired():
    store, _ = _make_store()
    report = Report(
        flow_name="test",
        error_message="boom",
        error_signature="sig789",
    )
    store.save_report(report)

    # Looking for reports from the future
    found = store.find_report_by_signature(
        "sig789",
        since=datetime.utcnow() + timedelta(hours=1),
    )
    assert found is None


def test_increment_dedup_count():
    store, _ = _make_store()
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)

    store.increment_dedup_count(report.id)
    store.increment_dedup_count(report.id)

    reports = store.get_reports(limit=1)
    assert reports[0].dedup_count == 3


def test_save_and_count_actions():
    store, _ = _make_store()
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)

    action = Action(
        report_id=report.id,
        action_type=ActionType.SLACK_ALERT.value,
        status=ActionStatus.SENT.value,
    )
    store.save_action(action)

    count = store.count_actions_today(ActionType.SLACK_ALERT.value)
    assert count == 1

    count_email = store.count_actions_today(ActionType.EMAIL_ALERT.value)
    assert count_email == 0


def test_has_recent_failure():
    store, _ = _make_store()
    store.save_report(Report(
        flow_name="upstream",
        error_message="upstream broke",
        severity="error",
    ))

    assert store.has_recent_failure("upstream", since=datetime.utcnow() - timedelta(hours=1))
    assert not store.has_recent_failure("other-flow", since=datetime.utcnow() - timedelta(hours=1))


def test_has_recent_failure_warning_excluded():
    """Warnings should not count as failures for cascade detection."""
    store, _ = _make_store()
    store.save_report(Report(
        flow_name="upstream",
        error_message="just a warning",
        severity="warning",
    ))

    assert not store.has_recent_failure("upstream", since=datetime.utcnow() - timedelta(hours=1))

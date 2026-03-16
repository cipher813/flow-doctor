"""Tests for rate limiting and cascade detection."""

import tempfile
from datetime import datetime, timedelta

from flow_doctor.core.config import RateLimitConfig
from flow_doctor.core.models import Action, ActionStatus, ActionType, Report
from flow_doctor.core.rate_limiter import CascadeDetector, RateLimiter
from flow_doctor.storage.sqlite import SQLiteStorage


def _make_store():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = SQLiteStorage(f.name)
    store.init_schema()
    return store


def test_rate_limiter_allow():
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=5)
    rl = RateLimiter(store, config)

    assert rl.check("slack_alert") == "allow"


def test_rate_limiter_degrade():
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=2)
    rl = RateLimiter(store, config)

    # Create a report to reference
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)

    # Add 2 actions to hit the limit
    for _ in range(2):
        store.save_action(Action(
            report_id=report.id,
            action_type="slack_alert",
            status=ActionStatus.SENT.value,
        ))

    assert rl.check("slack_alert") == "degrade"


def test_rate_limiter_different_action_types():
    store = _make_store()
    config = RateLimitConfig(max_alerts_per_day=1, max_diagnosed_per_day=3)
    rl = RateLimiter(store, config)

    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)

    store.save_action(Action(
        report_id=report.id,
        action_type="slack_alert",
        status=ActionStatus.SENT.value,
    ))

    assert rl.check("slack_alert") == "degrade"
    assert rl.check("diagnosis") == "allow"


def test_cascade_detector_no_dependencies():
    store = _make_store()
    detector = CascadeDetector(store)
    result = detector.check_cascade([], "my-flow")
    assert result is None


def test_cascade_detector_no_upstream_failure():
    store = _make_store()
    detector = CascadeDetector(store)
    result = detector.check_cascade(["upstream-flow"], "my-flow")
    assert result is None


def test_cascade_detector_upstream_failure():
    store = _make_store()
    # Record an upstream failure
    store.save_report(Report(
        flow_name="research-lambda",
        error_message="research failed",
        severity="error",
    ))

    detector = CascadeDetector(store)
    result = detector.check_cascade(["research-lambda"], "predictor-training")
    assert result == "research-lambda"


def test_cascade_detector_upstream_warning_not_cascade():
    store = _make_store()
    # Record an upstream warning (not a failure)
    store.save_report(Report(
        flow_name="research-lambda",
        error_message="just a warning",
        severity="warning",
    ))

    detector = CascadeDetector(store)
    result = detector.check_cascade(["research-lambda"], "predictor-training")
    assert result is None

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


# ── telegram_alert budget regression (2026-07-28 fleet alert blackout) ────────
#
# telegram_alert and s3_alert shipped as notifiers but were never added to
# RateLimiter.limits, so `limits.get(action, 10)` silently gave them a
# hardcoded 10/day while the configured max_alerts_per_day reached only
# slack/email. Telegram was the ONLY channel that fleet read. The shared budget
# burned out early each day, so terminal pipeline notifications (late in the
# cycle) were systematically dropped while start-of-run pings got through —
# 12 of 13 terminal notifications suppressed across two days, including two
# consecutive production trading-pipeline failures that paged and were never
# seen.


def test_every_action_type_has_a_configured_budget():
    """The generative defect: a notifier ships, its ActionType is never added
    to the budget map, and it silently inherits a small hardcoded default.
    Asserting over the enum means a NEW ActionType cannot regress this."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig())
    missing = {a.value for a in ActionType} - set(rl.limits)
    assert not missing, (
        f"ActionType(s) {sorted(missing)} have no rate-limit budget and would "
        f"fall through to the unmapped path. Add them to _ALERT_ACTIONS or "
        f"_ISSUE_ACTIONS in flow_doctor/core/rate_limiter.py."
    )


def test_telegram_and_s3_alerts_use_the_configured_alert_budget():
    """Not a hardcoded default — the value the operator actually configured."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=100))
    assert rl.limits[ActionType.TELEGRAM_ALERT.value] == 100
    assert rl.limits[ActionType.S3_ALERT.value] == 100
    # Regression pin: the old code produced 10 here regardless of config.
    assert rl.limits[ActionType.TELEGRAM_ALERT.value] != 10


def test_unmapped_action_fails_open_not_to_a_silent_small_default():
    """An extra alert costs noise; a dropped one costs an outage. An unknown
    action must never be silently capped."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=1))
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)
    for _ in range(50):
        store.save_action(Action(
            report_id=report.id,
            action_type="brand_new_notifier",
            status=ActionStatus.SENT.value,
        ))
    assert rl.check("brand_new_notifier") == "allow"


def test_failure_severities_are_exempt_from_the_daily_cap():
    """A rate limiter that can drop a failure page is an outage amplifier.
    Repeats of the SAME failure are suppressed by signature dedup upstream, so
    anything reaching the limiter at error/critical is a DISTINCT failure."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(max_alerts_per_day=1))
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)
    for _ in range(5):
        store.save_action(Action(
            report_id=report.id,
            action_type=ActionType.TELEGRAM_ALERT.value,
            status=ActionStatus.SENT.value,
        ))

    # Budget is blown...
    assert rl.check(ActionType.TELEGRAM_ALERT.value) == "degrade"
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="info") == "degrade"
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="warning") == "degrade"
    # ...but failures still get through.
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="error") == "allow"
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="critical") == "allow"


def test_exemption_is_configurable_and_can_be_disabled():
    """Setting it to [] restores the pre-0.8.8 cap-everything behaviour."""
    store = _make_store()
    rl = RateLimiter(store, RateLimitConfig(
        max_alerts_per_day=1, rate_limit_exempt_severities=[]
    ))
    report = Report(flow_name="test", error_message="boom")
    store.save_report(report)
    for _ in range(5):
        store.save_action(Action(
            report_id=report.id,
            action_type=ActionType.TELEGRAM_ALERT.value,
            status=ActionStatus.SENT.value,
        ))
    assert rl.check(ActionType.TELEGRAM_ALERT.value, severity="error") == "degrade"

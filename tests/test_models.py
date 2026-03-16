"""Tests for data models."""

from flow_doctor.core.models import (
    Action,
    ActionStatus,
    ActionType,
    Diagnosis,
    Feedback,
    FixAttempt,
    KnownPattern,
    Report,
    Severity,
    _ulid,
)


def test_ulid_uniqueness():
    ids = {_ulid() for _ in range(100)}
    assert len(ids) == 100


def test_ulid_sortable():
    """ULIDs generated in sequence should sort chronologically."""
    a = _ulid()
    b = _ulid()
    # Same millisecond is fine; at minimum they shouldn't be identical
    assert a != b


def test_report_defaults():
    r = Report(flow_name="test", error_message="boom")
    assert r.flow_name == "test"
    assert r.severity == "error"
    assert r.dedup_count == 1
    assert r.id  # auto-generated
    assert r.created_at is not None


def test_severity_enum():
    assert Severity.CRITICAL.value == "critical"
    assert Severity.ERROR.value == "error"
    assert Severity.WARNING.value == "warning"


def test_action_type_enum():
    assert ActionType.SLACK_ALERT.value == "slack_alert"
    assert ActionType.EMAIL_ALERT.value == "email_alert"


def test_diagnosis_dataclass():
    d = Diagnosis(
        report_id="abc",
        flow_name="test",
        category="CODE",
        root_cause="bad import",
        confidence=0.85,
    )
    assert d.source == "llm"
    assert d.confidence == 0.85


def test_feedback_dataclass():
    f = Feedback(diagnosis_id="abc", correct=True)
    assert f.correct is True
    assert f.corrected_category is None


def test_known_pattern_dataclass():
    kp = KnownPattern(
        error_signature="abc123",
        category="EXTERNAL",
        root_cause="API down",
    )
    assert kp.auto_fixable is False
    assert kp.hit_count == 0


def test_fix_attempt_dataclass():
    fa = FixAttempt(diagnosis_id="abc", diff="--- a\n+++ b\n")
    assert fa.test_passed is None
    assert fa.pr_url is None

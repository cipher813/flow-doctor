"""Tests for GitHub issue notifier."""

import json
from unittest.mock import MagicMock, patch

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.github import GitHubNotifier


def _make_report(**kwargs):
    defaults = dict(
        flow_name="test-flow",
        error_message="Something failed",
        error_type="RuntimeError",
        severity="error",
        traceback="Traceback (most recent call last):\n  File 'main.py', line 5\nRuntimeError: fail",
    )
    defaults.update(kwargs)
    return Report(**defaults)


def _make_diagnosis(**kwargs):
    defaults = dict(
        report_id="r1",
        flow_name="test-flow",
        category="CODE",
        root_cause="Logic error in main loop",
        confidence=0.85,
        remediation="Fix the loop condition",
        affected_files=["main.py:5"],
        auto_fixable=True,
        alternative_hypotheses=["Could be race condition"],
    )
    defaults.update(kwargs)
    return Diagnosis(**defaults)


def test_format_title_with_diagnosis():
    report = _make_report()
    diagnosis = _make_diagnosis()
    title = GitHubNotifier._format_title(report, "test-flow", diagnosis)
    assert title == "[CODE] test-flow: RuntimeError"


def test_format_title_without_diagnosis():
    report = _make_report()
    title = GitHubNotifier._format_title(report, "test-flow")
    assert title == "[ERROR] test-flow: RuntimeError"


def test_format_title_no_error_type():
    report = _make_report(error_type=None)
    title = GitHubNotifier._format_title(report, "test-flow")
    assert "Something failed" in title


def test_format_body_with_diagnosis():
    report = _make_report()
    diagnosis = _make_diagnosis()
    body = GitHubNotifier._format_body(report, "test-flow", diagnosis)

    assert "## Diagnosis" in body
    assert "CODE" in body
    assert "85%" in body
    assert "Logic error in main loop" in body
    assert "Fix the loop condition" in body
    assert "`main.py:5`" in body
    assert "race condition" in body
    assert "Auto-fixable:** Yes" in body


def test_format_body_without_diagnosis():
    report = _make_report()
    body = GitHubNotifier._format_body(report, "test-flow")

    assert "## Error" in body
    assert "RuntimeError" in body
    assert "## Traceback" in body
    assert "## Diagnosis" not in body


def test_format_body_cascade():
    report = _make_report(cascade_source="upstream-flow")
    body = GitHubNotifier._format_body(report, "test-flow")
    assert "upstream-flow" in body


def test_format_body_with_logs():
    report = _make_report(logs="INFO: Starting\nERROR: Crashed\nDEBUG: cleanup")
    body = GitHubNotifier._format_body(report, "test-flow")
    assert "Captured Logs" in body
    assert "Crashed" in body


def test_send_success():
    notifier = GitHubNotifier(repo="owner/repo", token="test-token")
    report = _make_report()

    mock_resp = MagicMock()
    mock_resp.status = 201
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("flow_doctor.notify.github.urlopen", return_value=mock_resp) as mock_url:
        result = notifier.send(report, "test-flow")

    assert result is True
    call_args = mock_url.call_args
    req = call_args[0][0]
    assert "owner/repo" in req.full_url
    payload = json.loads(req.data)
    assert "flow-doctor" in payload["labels"]


def test_send_failure():
    notifier = GitHubNotifier(repo="owner/repo", token="test-token")
    report = _make_report()

    with patch("flow_doctor.notify.github.urlopen", side_effect=Exception("API error")):
        result = notifier.send(report, "test-flow")

    assert result is False

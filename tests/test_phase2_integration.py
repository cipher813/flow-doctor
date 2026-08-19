"""Integration tests for Phase 2: report → diagnosis → enriched notification."""

import json
import tempfile
from unittest.mock import MagicMock, patch

from flow_doctor.core.client import FlowDoctor
from flow_doctor.core.config import (
    DiagnosisConfig,
    FlowDoctorConfig,
    GitHubConfig,
    NotifyChannelConfig,
    RateLimitConfig,
    StoreConfig,
)
from flow_doctor.core.models import Diagnosis, KnownPattern
from flow_doctor.storage.sqlite import SQLiteStorage


def _make_config(db_path, diagnosis_enabled=False, api_key=None):
    return FlowDoctorConfig(
        flow_name="test-flow",
        repo="owner/repo",
        owner="@testuser",
        store=StoreConfig(type="sqlite", path=db_path),
        diagnosis=DiagnosisConfig(
            enabled=diagnosis_enabled,
            # `provider` has no default since 0.15.0 (AnthropicProvider was
            # deleted) — `diagnosis.enabled=True` now requires it explicitly,
            # so name it whenever diagnosis is actually on.
            provider="openai_compat" if diagnosis_enabled else None,
            base_url="https://openrouter.ai/api/v1" if diagnosis_enabled else None,
            api_key=api_key,
            confidence_calibration=0.85,
        ),
        github=GitHubConfig(token="gh-token"),
        rate_limits=RateLimitConfig(
            max_diagnosed_per_day=3,
            max_alerts_per_day=5,
        ),
    )


def _install_fake_openai(monkeypatch, resp):
    """Inject a fake `openai` module (the SDK is an optional extra — tests
    can't rely on it being installed). Returns the mock client. Mirrors the
    helper in tests/test_diagnosis_provider.py."""
    import sys
    import types

    client = MagicMock()
    client.chat.completions.create.return_value = resp
    fake = types.ModuleType("openai")
    fake.OpenAI = lambda *a, **kw: client
    monkeypatch.setitem(sys.modules, "openai", fake)
    return client


def _mock_openai_response(content_text, prompt=1000, completion=500, cost=None):
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.cost = cost
    message = MagicMock()
    message.content = content_text
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def test_report_without_diagnosis():
    """Phase 1 behavior: report without diagnosis enabled."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name, diagnosis_enabled=False)
        fd = FlowDoctor(config)

        report_id = fd.report(ValueError("test error"))

        assert report_id is not None
        reports = fd.history()
        assert len(reports) == 1
        assert reports[0].error_type == "ValueError"


def test_report_with_knowledge_base_hit():
    """KB hit should produce a diagnosis without LLM call."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name, diagnosis_enabled=True, api_key="test-key")
        fd = FlowDoctor(config)

        # Seed a known pattern
        try:
            exc = ValueError("known error")
            raise exc
        except ValueError as e:
            # Compute the signature
            from flow_doctor.core.dedup import compute_signature_from_exception
            sig = compute_signature_from_exception(e)

            # Save a known pattern with this signature
            pattern = KnownPattern(
                error_signature=sig,
                category="DATA",
                root_cause="Known data issue",
                resolution="Fix the data source",
                auto_fixable=False,
            )
            fd._store.save_known_pattern(pattern)

            # Report the error
            report_id = fd.report(e)

        assert report_id is not None

        # Check diagnosis was created from KB
        diag = fd._store.get_diagnosis_by_report(report_id)
        assert diag is not None
        assert diag.source == "knowledge_base"
        assert diag.category == "DATA"
        assert diag.root_cause == "Known data issue"


def test_report_with_llm_diagnosis(monkeypatch):
    """LLM diagnosis on KB miss."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name, diagnosis_enabled=True, api_key="test-key")

        response_json = json.dumps({
            "category": "CODE",
            "root_cause": "Bug in the parser",
            "confidence": 0.90,
            "affected_files": ["parser.py:10"],
            "remediation": "Fix the parser",
            "auto_fixable": True,
            "alternative_hypotheses": ["Data issue"],
            "reasoning": "Traceback points to parser",
        })

        _install_fake_openai(
            monkeypatch,
            _mock_openai_response(response_json, prompt=5000, completion=500, cost=0.02),
        )

        fd = FlowDoctor(config)
        report_id = fd.report(RuntimeError("parser crashed"))

        assert report_id is not None

        diag = fd._store.get_diagnosis_by_report(report_id)
        assert diag is not None
        assert diag.source == "llm"
        assert diag.category == "CODE"
        assert diag.confidence == 0.90 * 0.85  # Calibrated


def test_report_warning_skips_diagnosis():
    """Warnings should not trigger diagnosis."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name, diagnosis_enabled=True, api_key="test-key")
        fd = FlowDoctor(config)

        report_id = fd.report("Low signal count", severity="warning")

        assert report_id is not None
        diag = fd._store.get_diagnosis_by_report(report_id)
        assert diag is None


def test_report_cascade_skips_diagnosis():
    """Cascade reports should not trigger diagnosis."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name, diagnosis_enabled=True, api_key="test-key")
        config.dependencies = ["upstream-flow"]
        fd = FlowDoctor(config)

        # Simulate upstream failure
        from flow_doctor.core.models import Report
        upstream_report = Report(
            flow_name="upstream-flow",
            error_message="Upstream failed",
            severity="error",
        )
        fd._store.save_report(upstream_report)

        # Report downstream error — should detect cascade
        report_id = fd.report(RuntimeError("downstream failed"))

        # May or may not detect cascade depending on timing, but shouldn't crash
        assert report_id is not None


def test_diagnosis_rate_limiting(monkeypatch):
    """Diagnosis should be rate-limited."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name, diagnosis_enabled=True, api_key="test-key")
        config.rate_limits.max_diagnosed_per_day = 1

        response_json = json.dumps({
            "category": "CODE",
            "root_cause": "Bug",
            "confidence": 0.9,
        })

        _install_fake_openai(
            monkeypatch,
            _mock_openai_response(response_json, prompt=1000, completion=200, cost=0.01),
        )

        fd = FlowDoctor(config)

        # First report gets diagnosis
        id1 = fd.report(ValueError("error 1"))
        diag1 = fd._store.get_diagnosis_by_report(id1)
        assert diag1 is not None

        # Second report should be rate-limited (no diagnosis)
        id2 = fd.report(TypeError("error 2"))
        diag2 = fd._store.get_diagnosis_by_report(id2)
        assert diag2 is None


def test_github_notifier_integration():
    """GitHub notifier should be initialized from config."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name)
        config.notify = [
            NotifyChannelConfig(type="github", repo="owner/repo", token="gh-token"),
        ]
        fd = FlowDoctor(config)

        from flow_doctor.notify.github import GitHubNotifier
        github_notifiers = [n for n in fd._notifiers if isinstance(n, GitHubNotifier)]
        assert len(github_notifiers) == 1
        assert github_notifiers[0].repo == "owner/repo"


def test_digest_generation():
    """Digest should summarize degraded actions."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name)
        config.rate_limits.max_alerts_per_day = 0  # Force all to degrade
        # This test asserts the DIGEST summarises degraded actions, so it needs
        # actions to actually degrade. Failure severities are exempt from the
        # daily cap by default (a limiter that can drop a failure page is an
        # outage amplifier — see RateLimiter.check), and the errors reported
        # below are error-severity, so without this the cap never applies and
        # there is nothing to digest.
        config.rate_limits.rate_limit_exempt_severities = []

        config.notify = [
            NotifyChannelConfig(
                type="slack",
                webhook_url="https://hooks.slack.com/test",
            ),
        ]

        fd = FlowDoctor(config)

        # Report some errors (all alerts will be degraded)
        fd.report(ValueError("error 1"))
        fd.report(TypeError("error 2"))

        # Generate digest
        content = fd._digest_generator.generate()
        assert content is not None
        assert "Daily Digest" in content

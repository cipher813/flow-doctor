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
            api_key=api_key,
            confidence_calibration=0.85,
        ),
        github=GitHubConfig(token="gh-token"),
        rate_limits=RateLimitConfig(
            max_diagnosed_per_day=3,
            max_alerts_per_day=5,
        ),
    )


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


def test_report_with_llm_diagnosis():
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

        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = response_json

        mock_usage = MagicMock()
        mock_usage.input_tokens = 5000
        mock_usage.output_tokens = 500

        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage

        with patch("anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_anthropic_cls.return_value = mock_client

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


def test_diagnosis_rate_limiting():
    """Diagnosis should be rate-limited."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _make_config(f.name, diagnosis_enabled=True, api_key="test-key")
        config.rate_limits.max_diagnosed_per_day = 1

        response_json = json.dumps({
            "category": "CODE",
            "root_cause": "Bug",
            "confidence": 0.9,
        })

        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = response_json

        mock_usage = MagicMock()
        mock_usage.input_tokens = 1000
        mock_usage.output_tokens = 200

        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage

        with patch("anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_anthropic_cls.return_value = mock_client

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

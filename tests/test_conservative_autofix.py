"""Tests for conservative auto-fix defaults and the deny_repos feature.

Pins the conservative-defaults revision of RemediationConfig and GateConfig:

- ``max_auto_remediations_per_day`` default 5 → 2
- ``fix_pr_min_confidence`` default 0.8 → 0.85
- new ``deny_repos`` field on both RemediationConfig and GateConfig
  that blocks auto-fix on the listed repos even when auto-fix is
  otherwise enabled.
"""

import tempfile
from typing import Optional

import pytest

from flow_doctor.core.config import RemediationConfig, load_config
from flow_doctor.core.models import Diagnosis
from flow_doctor.remediation.decision_gate import (
    DecisionGate,
    DecisionType,
    GateConfig,
)


# ── Default values ────────────────────────────────────────────────────

class TestConservativeDefaults:
    """Pin the lowered defaults so a future revert is caught by CI."""

    def test_remediation_config_max_per_day_default(self):
        cfg = RemediationConfig()
        assert cfg.max_auto_remediations_per_day == 2, (
            "Default lowered 5 → 2 in the conservative-defaults revision. "
            "If you need a higher value, override per-install in "
            "flow-doctor.yaml — do not raise the package default."
        )

    def test_remediation_config_fix_pr_confidence_default(self):
        cfg = RemediationConfig()
        assert cfg.fix_pr_min_confidence == 0.85, (
            "Default raised 0.8 → 0.85 in the conservative-defaults revision."
        )

    def test_remediation_config_auto_remediate_confidence_unchanged(self):
        """auto_remediate is already high; should stay at 0.9."""
        cfg = RemediationConfig()
        assert cfg.auto_remediate_min_confidence == 0.9

    def test_remediation_config_deny_repos_default_empty(self):
        cfg = RemediationConfig()
        assert cfg.deny_repos == []

    def test_gate_config_matches_remediation_config_defaults(self):
        """GateConfig defaults must match RemediationConfig so there's
        no silent drift if the user constructs GateConfig directly."""
        rem = RemediationConfig()
        gate = GateConfig()
        assert (
            gate.max_auto_remediations_per_day == rem.max_auto_remediations_per_day
        )
        assert gate.fix_pr_min_confidence == rem.fix_pr_min_confidence


# ── YAML loading ──────────────────────────────────────────────────────

class TestYAMLLoading:
    def test_yaml_deny_repos_list(self):
        """YAML list form should populate deny_repos."""
        yaml_content = """
flow_name: test
remediation:
  enabled: true
  deny_repos:
    - cipher813/alpha-engine
    - cipher813/alpha-engine-data
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = load_config(config_path=f.name)

        assert config.remediation.deny_repos == [
            "cipher813/alpha-engine",
            "cipher813/alpha-engine-data",
        ]

    def test_yaml_deny_repos_single_string(self):
        """YAML scalar form (a single string) should be normalized to a list."""
        yaml_content = """
flow_name: test
remediation:
  enabled: true
  deny_repos: cipher813/alpha-engine
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = load_config(config_path=f.name)

        assert config.remediation.deny_repos == ["cipher813/alpha-engine"]

    def test_yaml_missing_deny_repos_defaults_empty(self):
        yaml_content = """
flow_name: test
remediation:
  enabled: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = load_config(config_path=f.name)

        assert config.remediation.deny_repos == []

    def test_yaml_override_defaults(self):
        """A consumer overriding to looser values should work (opt-in)."""
        yaml_content = """
flow_name: test
remediation:
  enabled: true
  max_auto_remediations_per_day: 10
  fix_pr_min_confidence: 0.75
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = load_config(config_path=f.name)

        assert config.remediation.max_auto_remediations_per_day == 10
        assert config.remediation.fix_pr_min_confidence == 0.75


# ── deny_repos enforcement in DecisionGate ────────────────────────────

def _make_diagnosis(
    category: str = "CONFIG",
    confidence: float = 0.95,
    flow_name: str = "test-flow",
    context: Optional[dict] = None,
) -> Diagnosis:
    d = Diagnosis(
        report_id="test",
        flow_name=flow_name,
        category=category,
        root_cause="test root cause",
        confidence=confidence,
    )
    if context is not None:
        d.context = context
    return d


class TestDenyRepoEnforcement:
    def test_deny_repo_blocks_auto_remediate_via_flow_name(self):
        """A denied repo should escalate instead of auto-remediating."""
        gate = DecisionGate(
            config=GateConfig(
                market_open_hour=0, market_close_hour=0,
                deny_repos=["cipher813/alpha-engine"],
                max_auto_remediations_per_day=10,
            )
        )
        # INFRA + confidence 0.95 would normally AUTO_REMEDIATE
        diagnosis = _make_diagnosis(
            category="INFRA", confidence=0.95,
            flow_name="cipher813/alpha-engine-executor",
        )
        decision = gate.decide(
            diagnosis,
            error_type="RuntimeError",
            error_message="boom",
            flow_name="cipher813/alpha-engine-executor",
        )
        assert decision.decision_type == DecisionType.ESCALATE
        assert "deny list" in decision.reason.lower()
        assert "cipher813/alpha-engine" in decision.reason

    def test_deny_repo_blocks_fix_pr_generation(self):
        """A denied repo should also block fix-PR generation, not just remediate."""
        gate = DecisionGate(
            config=GateConfig(
                market_open_hour=0, market_close_hour=0,
                deny_repos=["alpha-engine"],
                max_auto_remediations_per_day=10,
            )
        )
        # CODE + confidence 0.9 would normally GENERATE_FIX_PR
        diagnosis = _make_diagnosis(
            category="CODE", confidence=0.9,
            flow_name="cipher813/alpha-engine-executor",
        )
        decision = gate.decide(
            diagnosis,
            error_type="ValueError",
            error_message="bad data",
            flow_name="cipher813/alpha-engine-executor",
        )
        assert decision.decision_type == DecisionType.ESCALATE

    def test_deny_repo_allows_non_matching_repos(self):
        """A repo NOT on the deny list should proceed normally."""
        gate = DecisionGate(
            config=GateConfig(
                market_open_hour=0, market_close_hour=0,
                deny_repos=["cipher813/alpha-engine"],
                max_auto_remediations_per_day=10,
            )
        )
        diagnosis = _make_diagnosis(
            category="CODE", confidence=0.9,
            flow_name="cipher813/mnemon",
        )
        decision = gate.decide(
            diagnosis,
            error_type="ValueError",
            error_message="bad data",
            flow_name="cipher813/mnemon",
        )
        # Should route to FIX_PR, not escalate-due-to-deny
        assert decision.decision_type == DecisionType.GENERATE_FIX_PR

    def test_deny_repo_case_insensitive_match(self):
        gate = DecisionGate(
            config=GateConfig(
                market_open_hour=0, market_close_hour=0,
                deny_repos=["ALPHA-ENGINE"],
                max_auto_remediations_per_day=10,
            )
        )
        diagnosis = _make_diagnosis(
            category="INFRA", confidence=0.95,
            flow_name="cipher813/alpha-engine-executor",
        )
        decision = gate.decide(
            diagnosis, "RuntimeError", "boom", "cipher813/alpha-engine-executor"
        )
        assert decision.decision_type == DecisionType.ESCALATE

    def test_empty_deny_list_is_noop(self):
        """An empty deny list should behave like the list doesn't exist."""
        gate = DecisionGate(
            config=GateConfig(
                market_open_hour=0, market_close_hour=0,
                deny_repos=[],
                max_auto_remediations_per_day=10,
            )
        )
        diagnosis = _make_diagnosis(category="INFRA", confidence=0.95)
        decision = gate.decide(
            diagnosis, "RuntimeError", "boom", "any-flow"
        )
        # Should route normally (auto_remediate requires playbook match, so
        # without one it escalates — but the reason should NOT be about deny list)
        assert "deny list" not in decision.reason.lower()

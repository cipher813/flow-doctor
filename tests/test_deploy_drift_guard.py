"""Tests for deploy-drift remediation guard (alpha-engine-config#5291)."""

from flow_doctor.core.client import FlowDoctor
from flow_doctor.core.models import Diagnosis


def _make_diagnosis(remediation=None, category="INFRA", confidence=0.81):
    """Build a minimal Diagnosis for guard testing."""
    return Diagnosis(
        report_id="test-report-id",
        flow_name="executor",
        category=category,
        root_cause="Deploy drift detected",
        confidence=confidence,
        remediation=remediation,
        source="llm",
    )


class TestGuardDeployDriftRemediation:
    """Tests for FlowDoctor._guard_deploy_drift_remediation()."""

    # --- No-op cases: guard should not modify remediation ---

    def test_no_remediation_returns_unchanged(self):
        """Guard does nothing when diagnosis has no remediation text."""
        diag = _make_diagnosis(remediation=None)
        result = FlowDoctor._guard_deploy_drift_remediation(
            diag, "Some error without SHAs"
        )
        assert result.remediation is None

    def test_non_drift_error_returns_unchanged(self):
        """Guard does nothing when error message has fewer than 2 SHAs."""
        diag = _make_diagnosis(
            remediation="Run git checkout abc1234 to fix"
        )
        result = FlowDoctor._guard_deploy_drift_remediation(
            diag, "A generic error: invalid literal for int()"
        )
        assert "FLOW-DOCTOR POST-HOC GUARD" not in (result.remediation or "")

    def test_no_checkout_in_remediation_passes_through(self):
        """Guard does not annotate when remediation has no git checkout."""
        diag = _make_diagnosis(
            remediation="Restart the executor service and re-run the pipeline."
        )
        result = FlowDoctor._guard_deploy_drift_remediation(
            diag,
            "Deploy drift: checkout at f579756 but pinned EXPECTED_EXECUTOR_SHA=b39fa05",
        )
        assert "FLOW-DOCTOR POST-HOC GUARD" not in (result.remediation or "")

    # --- Guard triggers: remediation with git checkout in drift error ---

    def test_checkout_in_drift_error_annotated(self):
        """Guard annotates remediation when it contains git checkout."""
        diag = _make_diagnosis(
            remediation=(
                "1. SSH into the EC2 instance.\n"
                "2. Run: git checkout b39fa05d50c0\n"
                "3. Verify with git rev-parse HEAD."
            )
        )
        result = FlowDoctor._guard_deploy_drift_remediation(
            diag,
            "Deploy drift: checkout at f579756c but pinned "
            "EXPECTED_EXECUTOR_SHA=b39fa05d50c0",
        )
        assert "FLOW-DOCTOR POST-HOC GUARD" in (result.remediation or "")

    def test_checkout_sha_not_in_error_message_not_annotated(self):
        """Guard does not annotate when the checkout SHA isn't from the error."""
        diag = _make_diagnosis(
            remediation="Run: git checkout deadbeef to deploy the fix."
        )
        result = FlowDoctor._guard_deploy_drift_remediation(
            diag,
            "Deploy drift: checkout at f579756c but pinned "
            "EXPECTED_EXECUTOR_SHA=b39fa05d50c0",
        )
        # deadbeef is not in the error message, so no SHA cited by the
        # remediation matches the error's SHAs
        assert "FLOW-DOCTOR POST-HOC GUARD" not in (result.remediation or "")

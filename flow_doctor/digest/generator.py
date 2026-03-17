"""Daily digest: summarizes rate-limited (degraded) reports."""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from flow_doctor.core.models import Action, Diagnosis, Report
from flow_doctor.notify.base import Notifier
from flow_doctor.storage.base import StorageBackend


class DigestGenerator:
    """Generates and sends daily digest of suppressed/degraded reports."""

    def __init__(self, store: StorageBackend):
        self.store = store

    def generate(self, since: Optional[datetime] = None) -> Optional[str]:
        """Generate a digest of degraded actions since the given time.

        Args:
            since: Cutoff time. Defaults to 24 hours ago.

        Returns:
            Markdown-formatted digest string, or None if nothing to report.
        """
        if since is None:
            since = datetime.utcnow() - timedelta(hours=24)

        degraded = self.store.get_degraded_actions(since)
        if not degraded:
            return None

        # Group by report_id to deduplicate
        by_report: Dict[str, List[Action]] = defaultdict(list)
        for action in degraded:
            by_report[action.report_id].append(action)

        # Build digest
        lines = [
            "# Flow Doctor Daily Digest",
            f"**Period:** {since.strftime('%Y-%m-%d %H:%M UTC')} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            f"**Suppressed alerts:** {len(degraded)}",
            f"**Unique reports:** {len(by_report)}",
            "",
        ]

        for report_id, actions in by_report.items():
            report = self.store.get_report(report_id)
            if report is None:
                continue

            severity_badge = {"critical": "🔴", "error": "🟠", "warning": "🟡"}.get(
                report.severity, "⚪"
            )

            lines.append(f"---")
            lines.append(f"### {severity_badge} {report.flow_name}")
            lines.append(f"**Time:** {report.created_at.strftime('%H:%M UTC')}")

            if report.error_type:
                lines.append(f"**Error:** `{report.error_type}: {report.error_message}`")
            else:
                lines.append(f"**Message:** {report.error_message}")

            if report.cascade_source:
                lines.append(f"_Cascade from: {report.cascade_source}_")

            # Check if there's a diagnosis
            diagnosis = self.store.get_diagnosis_by_report(report_id)
            if diagnosis:
                lines.append(f"**Diagnosis:** [{diagnosis.category}] {diagnosis.root_cause[:200]}")

            suppressed_types = [a.action_type for a in actions]
            lines.append(f"**Suppressed:** {', '.join(suppressed_types)}")

            if report.dedup_count > 1:
                lines.append(f"**Occurrences:** {report.dedup_count}")

            lines.append("")

        lines.append("---")
        lines.append("*This digest summarizes alerts that were rate-limited during the reporting period.*")

        return "\n".join(lines)

    def send(
        self,
        notifiers: List[Notifier],
        flow_name: str,
        since: Optional[datetime] = None,
    ) -> bool:
        """Generate and send digest via configured notifiers.

        Bypasses rate limiting — digests always send.
        Returns True if at least one notifier succeeded.
        """
        content = self.generate(since)
        if content is None:
            return True  # Nothing to send is not a failure

        # Create a synthetic report for the digest
        digest_report = Report(
            flow_name=flow_name,
            error_message=content,
            severity="warning",
            error_type="DailyDigest",
        )

        any_success = False
        for notifier in notifiers:
            try:
                success = notifier.send(digest_report, f"{flow_name} (Daily Digest)")
                if success:
                    any_success = True
            except Exception as e:
                print(f"[flow-doctor] Digest send failed: {e}", file=sys.stderr)

        return any_success

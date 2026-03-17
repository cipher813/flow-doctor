"""Email (SMTP) notification backend."""

from __future__ import annotations

import smtplib
import sys
from email.mime.text import MIMEText
from typing import Optional

from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import Notifier


class EmailNotifier(Notifier):
    """Send alerts via SMTP email."""

    def __init__(
        self,
        sender: str,
        recipients: str,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 587,
        smtp_password: Optional[str] = None,
    ):
        self.sender = sender
        self.recipients = [r.strip() for r in recipients.split(",")]
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_password = smtp_password

    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> bool:
        try:
            subject = f"[Flow Doctor] [{report.severity.upper()}] {flow_name}"
            if report.error_type:
                subject += f" - {report.error_type}"
            if diagnosis:
                subject += f" [{diagnosis.category}]"

            body = self._format_body(report, flow_name, diagnosis)
            msg = MIMEText(body, "plain")
            msg["Subject"] = subject
            msg["From"] = self.sender
            msg["To"] = ", ".join(self.recipients)

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls()
                if self.smtp_password:
                    server.login(self.sender, self.smtp_password)
                server.sendmail(self.sender, self.recipients, msg.as_string())
            return True
        except Exception as e:
            print(f"[flow-doctor] Email notification failed: {e}", file=sys.stderr)
            return False

    @staticmethod
    def _format_body(
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> str:
        lines = [
            f"Flow Doctor Alert: {flow_name}",
            f"Severity: {report.severity.upper()}",
            f"Report ID: {report.id}",
            f"Time: {report.created_at.isoformat()}",
            "",
        ]
        if report.error_type:
            lines.append(f"Error: {report.error_type}: {report.error_message}")
        else:
            lines.append(f"Message: {report.error_message}")

        if report.cascade_source:
            lines.append(f"\nNote: Likely caused by upstream '{report.cascade_source}' failure")

        # Diagnosis section
        if diagnosis:
            lines.append("")
            lines.append("=" * 50)
            lines.append("DIAGNOSIS")
            lines.append("=" * 50)
            lines.append(f"Category: {diagnosis.category}")
            lines.append(f"Confidence: {diagnosis.confidence:.0%}")
            lines.append(f"Source: {diagnosis.source}")
            lines.append(f"\nRoot Cause:\n{diagnosis.root_cause}")
            if diagnosis.remediation:
                lines.append(f"\nRemediation:\n{diagnosis.remediation}")
            if diagnosis.affected_files:
                lines.append(f"\nAffected Files:")
                for f in diagnosis.affected_files:
                    lines.append(f"  - {f}")
            if diagnosis.alternative_hypotheses:
                lines.append(f"\nAlternative Hypotheses:")
                for h in diagnosis.alternative_hypotheses:
                    lines.append(f"  - {h}")
            if diagnosis.auto_fixable is not None:
                lines.append(f"\nAuto-fixable: {'Yes' if diagnosis.auto_fixable else 'No'}")
            lines.append("=" * 50)

        if report.traceback:
            lines.append("\nTraceback:")
            lines.append(report.traceback)

        if report.logs:
            lines.append("\nCaptured Logs (last 50 lines):")
            log_lines = report.logs.strip().splitlines()[-50:]
            lines.extend(log_lines)

        return "\n".join(lines)

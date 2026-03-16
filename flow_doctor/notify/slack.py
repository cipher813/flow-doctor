"""Slack webhook notification backend."""

from __future__ import annotations

import json
import sys
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from flow_doctor.core.models import Report
from flow_doctor.notify.base import Notifier


class SlackNotifier(Notifier):
    """Send alerts via Slack incoming webhook."""

    def __init__(self, webhook_url: str, channel: Optional[str] = None):
        self.webhook_url = webhook_url
        self.channel = channel

    def send(self, report: Report, flow_name: str) -> bool:
        try:
            text = self._format_message(report, flow_name)
            payload = {"text": text}
            if self.channel:
                payload["channel"] = self.channel

            data = json.dumps(payload).encode("utf-8")
            req = Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            print(f"[flow-doctor] Slack notification failed: {e}", file=sys.stderr)
            return False

    @staticmethod
    def _format_message(report: Report, flow_name: str) -> str:
        severity_emoji = {
            "critical": "🔴",
            "error": "🟠",
            "warning": "🟡",
        }
        emoji = severity_emoji.get(report.severity, "⚪")
        lines = [
            f"{emoji} *[{report.severity.upper()}] {flow_name}*",
            "",
        ]
        if report.error_type:
            lines.append(f"*Error:* `{report.error_type}: {report.error_message}`")
        else:
            lines.append(f"*Message:* {report.error_message}")

        if report.cascade_source:
            lines.append(f"_Likely caused by upstream `{report.cascade_source}` failure_")

        if report.traceback:
            # Show last 5 lines of traceback
            tb_lines = report.traceback.strip().splitlines()[-5:]
            lines.append("")
            lines.append("```")
            lines.extend(tb_lines)
            lines.append("```")

        lines.append(f"\n_Report ID: {report.id}_")
        return "\n".join(lines)

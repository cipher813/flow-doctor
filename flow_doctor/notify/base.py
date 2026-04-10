"""Abstract base class for notification backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from flow_doctor.core.models import Diagnosis, Report


class Notifier(ABC):
    """Pluggable notification interface."""

    @abstractmethod
    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        """Send a notification for the given report.

        Args:
            report: The error report.
            flow_name: Name of the flow that failed.
            diagnosis: Optional diagnosis to enrich the notification.

        Returns:
            On success, a target identifier string that will be stored in
            the action record's ``target`` field — typically a user-facing
            URL (GitHub issue URL, Slack webhook endpoint) or address
            (email recipients). On failure, ``None``.

            Callers should use truthiness (``if send(...)``) to distinguish
            success from failure, and use the value to construct follow-up
            links when it is non-empty.
        """

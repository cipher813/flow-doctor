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
    ) -> bool:
        """Send a notification for the given report.

        Args:
            report: The error report.
            flow_name: Name of the flow that failed.
            diagnosis: Optional diagnosis to enrich the notification.

        Returns True on success, False on failure.
        """

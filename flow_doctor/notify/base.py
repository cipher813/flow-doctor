"""Abstract base class for notification backends."""

from __future__ import annotations

from abc import ABC, abstractmethod

from flow_doctor.core.models import Report


class Notifier(ABC):
    """Pluggable notification interface."""

    @abstractmethod
    def send(self, report: Report, flow_name: str) -> bool:
        """Send a notification for the given report.

        Returns True on success, False on failure.
        """

"""Abstract base class for storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from flow_doctor.core.models import Action, Report


class StorageBackend(ABC):
    """Pluggable storage interface for Flow Doctor."""

    @abstractmethod
    def init_schema(self) -> None:
        """Create tables/schema if they don't exist."""

    @abstractmethod
    def save_report(self, report: Report) -> None:
        """Persist a report."""

    @abstractmethod
    def save_action(self, action: Action) -> None:
        """Persist an action record."""

    @abstractmethod
    def find_report_by_signature(
        self,
        error_signature: str,
        since: datetime,
    ) -> Optional[Report]:
        """Find the most recent report with this signature since cutoff."""

    @abstractmethod
    def increment_dedup_count(self, report_id: str) -> None:
        """Increment the dedup_count for a report."""

    @abstractmethod
    def count_actions_today(self, action_type: str) -> int:
        """Count actions of the given type created today (UTC)."""

    @abstractmethod
    def has_recent_failure(self, flow_name: str, since: datetime) -> bool:
        """Check if a flow reported a failure since the given time."""

    @abstractmethod
    def get_reports(
        self,
        flow_name: Optional[str] = None,
        limit: int = 10,
    ) -> List[Report]:
        """Get recent reports, optionally filtered by flow name."""

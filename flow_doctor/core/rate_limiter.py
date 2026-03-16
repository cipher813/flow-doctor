"""Rate limiting: tiered degradation and cascade-aware budget."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from flow_doctor.storage.base import StorageBackend
    from flow_doctor.core.config import RateLimitConfig


class RateLimiter:
    """Tiered rate limiter that returns 'allow' or 'degrade' for each action."""

    def __init__(self, store: StorageBackend, config: RateLimitConfig):
        self.store = store
        self.limits = {
            "diagnosis": config.max_diagnosed_per_day,
            "github_issue": config.max_issues_per_day,
            "github_pr": config.max_issues_per_day,
            "slack_alert": config.max_alerts_per_day,
            "email_alert": config.max_alerts_per_day,
        }

    def check(self, action: str) -> str:
        """Returns 'allow' or 'degrade'."""
        limit = self.limits.get(action, 10)
        today_count = self.store.count_actions_today(action)
        if today_count < limit:
            return "allow"
        return "degrade"


class CascadeDetector:
    """Detect if a failure is caused by an upstream dependency failure."""

    def __init__(self, store: StorageBackend, window_hours: int = 4):
        self.store = store
        self.window_hours = window_hours

    def check_cascade(
        self,
        dependencies: list[str],
        flow_name: str,
    ) -> Optional[str]:
        """Check if any dependency reported a failure within the cascade window.

        Returns the dependency flow_name that failed, or None.
        """
        if not dependencies:
            return None
        cutoff = datetime.utcnow() - timedelta(hours=self.window_hours)
        for dep in dependencies:
            if self.store.has_recent_failure(dep, since=cutoff):
                return dep
        return None

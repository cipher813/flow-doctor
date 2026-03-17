"""Replay store: tracks prior rejected fixes to avoid repeating mistakes."""

from __future__ import annotations

from typing import List, Optional

from flow_doctor.storage.base import StorageBackend


class ReplayStore:
    """Queries storage for prior fix rejections to inform new attempts."""

    def __init__(self, storage: StorageBackend):
        self._storage = storage

    def get_rejections(self, diagnosis_id: str) -> List[str]:
        """Get rejection reasons for prior fix attempts on this diagnosis.

        Returns:
            List of rejection reason strings.
        """
        attempts = self._storage.get_fix_attempts_for_diagnosis(diagnosis_id)
        return [
            a.rejection_reason
            for a in attempts
            if a.rejection_reason
        ]

    def get_rejections_for_flow(
        self,
        flow_name: str,
        error_signature: str,
    ) -> List[str]:
        """Get rejection reasons for a flow + error signature combo.

        This is a best-effort lookup — requires the storage backend
        to support cross-table queries. Falls back to empty list.
        """
        # For now, the CLI passes diagnosis_id directly, so this is unused.
        # Placeholder for future enhancement.
        return []

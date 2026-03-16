"""Deduplication: error signature hashing and cooldown logic."""

from __future__ import annotations

import hashlib
import traceback as tb_module
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from flow_doctor.storage.base import StorageBackend


def compute_error_signature(
    error_type: Optional[str],
    traceback_str: Optional[str],
) -> str:
    """Compute a dedup signature from exception type + top 3 stack frames.

    The signature is a hex digest of: error_type + normalized top 3 frames.
    """
    parts: List[str] = []
    if error_type:
        parts.append(error_type)

    if traceback_str:
        # Extract file/line/function from traceback lines
        frames = _extract_frames(traceback_str)
        for frame in frames[:3]:
            parts.append(frame)

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_signature_from_exception(exc: BaseException) -> str:
    """Compute error signature from a live exception object."""
    error_type = type(exc).__qualname__
    tb = exc.__traceback__
    if tb is not None:
        formatted = "".join(tb_module.format_tb(tb))
    else:
        formatted = ""
    return compute_error_signature(error_type, formatted)


def _extract_frames(traceback_str: str) -> List[str]:
    """Extract normalized frame identifiers from a traceback string.

    Returns list of 'filename:lineno:funcname' strings, innermost first.
    """
    frames = []
    lines = traceback_str.strip().splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("File "):
            # Parse: File "path", line N, in func
            parts = stripped.split(",")
            if len(parts) >= 3:
                filename = parts[0].split('"')[1] if '"' in parts[0] else parts[0]
                # Normalize: use just the basename
                if "/" in filename:
                    filename = filename.rsplit("/", 1)[1]
                if "\\" in filename:
                    filename = filename.rsplit("\\", 1)[1]
                lineno = parts[1].strip()
                func = parts[2].strip()
                frames.append(f"{filename}:{lineno}:{func}")
    # Return innermost frames first (they're most specific)
    frames.reverse()
    return frames


class DedupChecker:
    """Check whether a report is a duplicate within the cooldown window."""

    def __init__(self, store: StorageBackend, cooldown_minutes: int = 60):
        self.store = store
        self.cooldown_minutes = cooldown_minutes

    def is_duplicate(self, error_signature: str) -> Tuple[bool, Optional[str]]:
        """Check if this signature was seen within the cooldown window.

        Returns (is_dup, existing_report_id).
        """
        cutoff = datetime.utcnow() - timedelta(minutes=self.cooldown_minutes)
        existing = self.store.find_report_by_signature(error_signature, since=cutoff)
        if existing:
            return True, existing.id
        return False, None

    def record_dedup_hit(self, report_id: str) -> None:
        """Increment the dedup counter on an existing report."""
        self.store.increment_dedup_count(report_id)

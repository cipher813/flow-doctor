"""Scope guard: validates that diff files are within allowed paths."""

from __future__ import annotations

import fnmatch
from typing import List, Tuple


class ScopeGuard:
    """Validates that every file in a diff is within allow and not in deny."""

    def __init__(self, allow: List[str], deny: List[str]):
        self.allow = allow
        self.deny = deny

    def check(self, diff_files: List[str]) -> Tuple[bool, List[str]]:
        """Check if all diff files are in scope.

        Returns:
            (passed, violations) — passed is True if all files are in scope,
            violations lists any out-of-scope files.
        """
        violations: List[str] = []

        for path in diff_files:
            if not self._is_allowed(path):
                violations.append(f"not in allow list: {path}")
            elif self._is_denied(path):
                violations.append(f"in deny list: {path}")

        return (len(violations) == 0, violations)

    def _is_allowed(self, path: str) -> bool:
        if not self.allow:
            return True
        return any(self._matches(pattern, path) for pattern in self.allow)

    def _is_denied(self, path: str) -> bool:
        if not self.deny:
            return False
        return any(self._matches(pattern, path) for pattern in self.deny)

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        """Match a pattern against a path. Supports glob and prefix matching."""
        # Direct glob match
        if fnmatch.fnmatch(path, pattern):
            return True
        # Prefix match (e.g., "data/" matches "data/scanner.py")
        if pattern.endswith("/") and path.startswith(pattern):
            return True
        # Prefix match without trailing slash
        if not pattern.endswith("/") and "/" not in pattern and "*" not in pattern:
            if path.startswith(pattern + "/") or path == pattern:
                return True
        return False

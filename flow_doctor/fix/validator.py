"""Test validator: runs test command and reports results."""

from __future__ import annotations

import subprocess
from typing import Tuple


class TestValidator:
    """Runs a test command and returns pass/fail with output."""

    def run(self, test_command: str, repo_path: str, timeout: int = 300) -> Tuple[bool, str]:
        """Run the test command in the given repo path.

        Args:
            test_command: Shell command to run (e.g., "python -m pytest tests/ -x -q").
            repo_path: Working directory for the command.
            timeout: Max seconds to wait.

        Returns:
            (passed, output) — passed is True if exit code is 0.
        """
        try:
            result = subprocess.run(
                test_command,
                shell=True,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout
            if result.stderr:
                output += "\n" + result.stderr
            return (result.returncode == 0, output.strip())
        except subprocess.TimeoutExpired:
            return (False, f"Test command timed out after {timeout}s")
        except Exception as e:
            return (False, f"Test command failed to execute: {e}")

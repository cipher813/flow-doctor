"""Tests for SHA-range commit context loading (deploy-drift error support)."""

import subprocess
from unittest.mock import MagicMock, patch

from flow_doctor.diagnosis.git_context import GitContextLoader


def _get_two_shas(repo_path="."):
    """Return (newer_full_sha, older_full_sha) from the repo history."""
    result = subprocess.run(
        ["git", "log", "--format=%H", "-2"],
        capture_output=True, text=True, cwd=repo_path, timeout=10,
    )
    lines = result.stdout.strip().split("\n")
    if len(lines) < 2:
        return None, None
    return lines[0], lines[1]


def test_load_sha_range_identifies_ordering():
    """load_sha_range correctly identifies which SHA is newer."""
    newer, older = _get_two_shas()
    if newer is None or older is None:
        return  # not enough history; skip

    result = GitContextLoader.load_sha_range(".", older, newer)
    assert result, "Should return non-empty dict for valid SHA range"
    assert result["newer_sha"] == newer
    assert result["older_sha"] == older
    assert "sha_range_log" in result
    assert len(result["sha_range_log"]) > 0


def test_load_sha_range_reversed_arguments():
    """load_sha_range handles reversed argument order correctly."""
    newer, older = _get_two_shas()
    if newer is None or older is None:
        return

    # Passing newer first, older second — should still detect ordering
    result = GitContextLoader.load_sha_range(".", newer, older)
    assert result, "Should still work with reversed args"
    assert result["newer_sha"] == newer
    assert result["older_sha"] == older


def test_load_sha_range_same_sha():
    """load_sha_range returns empty dict when both SHAs are the same."""
    newer, _ = _get_two_shas()
    if newer is None:
        return

    result = GitContextLoader.load_sha_range(".", newer, newer)
    assert result == {}, "Same SHA should return empty dict (no range)"


def test_load_sha_range_bad_path():
    """load_sha_range returns empty dict for a non-git directory."""
    result = GitContextLoader.load_sha_range("/tmp", "abc1234", "def5678")
    assert result == {}


def test_detect_and_load_sha_range_with_drift_error():
    """detect_and_load_sha_range extracts SHAs from deploy-drift error text."""
    newer, older = _get_two_shas()
    if newer is None or older is None:
        return

    error_msg = (
        f"RuntimeError: Deploy drift: executor checkout at "
        f"/home/ec2-user/alpha-engine is on {newer[:7]} but this run "
        f"pinned EXPECTED_EXECUTOR_SHA={older[:7]} at its freshness gate."
    )
    result = GitContextLoader.detect_and_load_sha_range(error_msg, ".")
    assert result is not None
    # The error message only contains abbreviated SHAs (7 chars),
    # so load_sha_range returns the abbreviated versions it was given.
    assert result["newer_sha"].startswith(newer[:7])
    assert result["older_sha"].startswith(older[:7])


def test_detect_and_load_with_non_drift_error():
    """detect_and_load_sha_range returns None for errors without two SHAs."""
    result = GitContextLoader.detect_and_load_sha_range(
        "Some generic error without any commit hashes",
        ".",
    )
    assert result is None


def test_detect_and_load_with_one_sha():
    """detect_and_load_sha_range returns None when only one SHA is present."""
    result = GitContextLoader.detect_and_load_sha_range(
        "Error on commit abc1234, please investigate",
        ".",
    )
    assert result is None

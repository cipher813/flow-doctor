"""Tests for git context loader."""

import json
from unittest.mock import MagicMock, patch

from flow_doctor.diagnosis.git_context import GitContextLoader


def test_load_local_in_git_repo():
    """Test loading local git context (runs in this repo)."""
    result = GitContextLoader.load_local(".")
    # We're in a git repo, so this should return something
    assert isinstance(result, dict)
    if result:  # May be empty if no recent commits
        assert "git_log" in result or "changed_files" in result


def test_load_local_bad_path():
    """Test loading from a non-git directory."""
    result = GitContextLoader.load_local("/tmp")
    # Should return empty dict, not crash
    assert isinstance(result, dict)


def test_load_github_success():
    """Test GitHub API loading with mocked responses."""
    commits_data = [
        {
            "sha": "abc1234567890",
            "commit": {"message": "Fix parser bug\n\nDetails here"},
        },
        {
            "sha": "def4567890123",
            "commit": {"message": "Add new feature"},
        },
    ]

    detail_data = {
        "files": [
            {"filename": "src/parser.py"},
            {"filename": "tests/test_parser.py"},
        ]
    }

    def mock_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        resp = MagicMock()
        if "/commits?" in url:
            resp.read.return_value = json.dumps(commits_data).encode()
        else:
            resp.read.return_value = json.dumps(detail_data).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    with patch("flow_doctor.diagnosis.git_context.urlopen", side_effect=mock_urlopen):
        result = GitContextLoader.load_github("owner/repo", "test-token")

    assert "git_log" in result
    assert "abc1234" in result["git_log"]
    assert "Fix parser bug" in result["git_log"]
    assert "changed_files" in result
    assert "src/parser.py" in result["changed_files"]


def test_load_github_failure():
    """Test graceful failure on API error."""
    with patch("flow_doctor.diagnosis.git_context.urlopen", side_effect=Exception("API error")):
        result = GitContextLoader.load_github("owner/repo", "bad-token")

    assert result == {}

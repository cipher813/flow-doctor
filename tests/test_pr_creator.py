"""Tests for PR creator (mocked git + GitHub API)."""

import json
from unittest.mock import MagicMock, patch, call

from flow_doctor.fix.pr_creator import PRCreator


def test_create_branch():
    with patch("flow_doctor.fix.pr_creator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        branch = PRCreator.create_branch("/tmp/repo", "research-lambda")

    assert branch.startswith("flow-doctor/research-lambda/")
    assert len(branch.split("/")) == 3
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "git"
    assert cmd[1] == "checkout"
    assert cmd[2] == "-b"


def test_apply_diff_success():
    with patch("flow_doctor.fix.pr_creator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = PRCreator.apply_diff("/tmp/repo", "--- a/f\n+++ b/f\n")

    assert result is True
    assert mock_run.call_count == 2  # check + apply


def test_apply_diff_check_fails():
    with patch("flow_doctor.fix.pr_creator.subprocess.run") as mock_run:
        fail_result = MagicMock(returncode=1, stderr="patch does not apply")
        mock_run.return_value = fail_result
        result = PRCreator.apply_diff("/tmp/repo", "bad diff")

    assert result is False


def test_commit_and_push():
    with patch("flow_doctor.fix.pr_creator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = PRCreator.commit_and_push("/tmp/repo", "flow-doctor/test/123", "fix: test")

    assert result is True
    assert mock_run.call_count == 3  # add, commit, push


def test_create_pr_success():
    mock_resp = MagicMock()
    mock_resp.status = 201
    mock_resp.read.return_value = json.dumps({
        "html_url": "https://github.com/owner/repo/pull/1",
        "number": 1,
    }).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("flow_doctor.fix.pr_creator.urlopen", return_value=mock_resp):
        url = PRCreator.create_pr(
            repo="owner/repo",
            head="flow-doctor/test/123",
            base="main",
            title="Fix bug",
            body="Description",
            token="test-token",
        )

    assert url == "https://github.com/owner/repo/pull/1"


def test_create_pr_failure():
    with patch("flow_doctor.fix.pr_creator.urlopen", side_effect=Exception("API error")):
        url = PRCreator.create_pr(
            repo="owner/repo",
            head="branch",
            base="main",
            title="Fix",
            body="Body",
            token="token",
        )

    assert url is None


def test_create_pr_with_labels():
    pr_resp = MagicMock()
    pr_resp.read.return_value = json.dumps({
        "html_url": "https://github.com/owner/repo/pull/1",
        "number": 1,
    }).encode()
    pr_resp.__enter__ = lambda s: s
    pr_resp.__exit__ = lambda s, *a: None

    label_resp = MagicMock()
    label_resp.__enter__ = lambda s: s
    label_resp.__exit__ = lambda s, *a: None

    with patch("flow_doctor.fix.pr_creator.urlopen", side_effect=[pr_resp, label_resp]) as mock_url:
        url = PRCreator.create_pr(
            repo="owner/repo",
            head="branch",
            base="main",
            title="Fix",
            body="Body",
            token="token",
            labels=["auto-fix"],
        )

    assert url == "https://github.com/owner/repo/pull/1"
    # Second call is for labels
    assert mock_url.call_count == 2

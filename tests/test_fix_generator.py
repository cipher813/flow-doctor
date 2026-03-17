"""Tests for fix generator with mocked LLM API."""

import sys
from unittest.mock import MagicMock, patch

from flow_doctor.fix.generator import FixGenerator


def _mock_response(text: str):
    """Create a mock Anthropic response."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _patch_anthropic():
    """Create a mock anthropic module and patch it into sys.modules."""
    mock_anthropic = MagicMock()
    return patch.dict(sys.modules, {"anthropic": mock_anthropic}), mock_anthropic


def test_generate_returns_diff():
    gen = FixGenerator(api_key="test-key")

    diff_text = (
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def run():\n"
        "-    return 1 / 0\n"
        "+    return 1\n"
    )

    patcher, mock_anthropic = _patch_anthropic()
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client
    mock_client.messages.create.return_value = _mock_response(diff_text)

    with patcher:
        result = gen.generate(
            category="CODE",
            root_cause="Division by zero",
            confidence=0.90,
            remediation="Remove the division",
            affected_files=["main.py"],
            file_contents={"main.py": "def run():\n    return 1 / 0\n"},
            test_contents={},
        )

    assert result is not None
    assert "+++ b/main.py" in result
    assert "return 1" in result


def test_generate_no_fix():
    gen = FixGenerator(api_key="test-key")

    patcher, mock_anthropic = _patch_anthropic()
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client
    mock_client.messages.create.return_value = _mock_response("NO_FIX")

    with patcher:
        result = gen.generate(
            category="EXTERNAL",
            root_cause="Third-party API down",
            confidence=0.50,
            remediation=None,
            affected_files=["client.py"],
            file_contents={"client.py": "import requests\n"},
            test_contents={},
        )

    assert result is None


def test_generate_strips_markdown_fences():
    gen = FixGenerator(api_key="test-key")

    fenced = (
        "```diff\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "```"
    )

    patcher, mock_anthropic = _patch_anthropic()
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client
    mock_client.messages.create.return_value = _mock_response(fenced)

    with patcher:
        result = gen.generate(
            category="CODE",
            root_cause="Bug",
            confidence=0.95,
            remediation="Fix it",
            affected_files=["main.py"],
            file_contents={"main.py": "old\n"},
            test_contents={},
        )

    assert result is not None
    assert not result.startswith("```")
    assert "--- a/main.py" in result


def test_generate_with_prior_rejections():
    gen = FixGenerator(api_key="test-key")

    patcher, mock_anthropic = _patch_anthropic()
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client
    mock_client.messages.create.return_value = _mock_response("NO_FIX")

    with patcher:
        gen.generate(
            category="CODE",
            root_cause="Bug",
            confidence=0.95,
            remediation="Fix it",
            affected_files=["main.py"],
            file_contents={"main.py": "old\n"},
            test_contents={},
            prior_rejections=["Tests failed: assertion error in test_main"],
        )

        # Verify rejection context was included in the prompt
        call_args = mock_client.messages.create.call_args
        user_msg = call_args[1]["messages"][0]["content"]
        assert "Prior Rejected Fix Attempts" in user_msg
        assert "assertion error" in user_msg


def test_extract_files_from_diff():
    diff = (
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "--- a/utils.py\n"
        "+++ b/utils.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    files = FixGenerator.extract_files_from_diff(diff)
    assert files == ["main.py", "utils.py"]

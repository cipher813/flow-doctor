"""Tests for fix generator with mocked LLM transports.

``FixGenerator`` no longer accepts ``provider="anthropic"`` (removed 0.15.0
— alpha-engine-config-I7460, see ``tests/test_fix_generator_provider.py`` for
the removal/rejection tests and ``tests/test_fix_generator_router.py`` for
the krepis-router path). These tests exercise the surviving
``openai_compat`` transport, which is now the default.
"""

import sys
import types
from unittest.mock import MagicMock

from flow_doctor.fix.generator import FixGenerator


def _mock_openai_response(text: str):
    """Create a mock OpenAI chat-completions response."""
    message = MagicMock()
    message.content = text
    choice = MagicMock()
    choice.message = message
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _patch_openai():
    """Create a mock openai module and patch it into sys.modules."""
    mock_openai = types.ModuleType("openai")
    mock_openai.OpenAI = MagicMock()
    return __import__("unittest.mock", fromlist=["patch"]).patch.dict(
        sys.modules, {"openai": mock_openai}
    ), mock_openai


def _gen(**kw):
    defaults = dict(api_key="test-key", provider="openai_compat", base_url="http://router.internal/v1")
    defaults.update(kw)
    return FixGenerator(**defaults)


def test_generate_returns_diff():
    gen = _gen()

    diff_text = (
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def run():\n"
        "-    return 1 / 0\n"
        "+    return 1\n"
    )

    patcher, mock_openai = _patch_openai()
    mock_client = MagicMock()
    mock_openai.OpenAI.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response(diff_text)

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
    gen = _gen()

    patcher, mock_openai = _patch_openai()
    mock_client = MagicMock()
    mock_openai.OpenAI.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response("NO_FIX")

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
    gen = _gen()

    fenced = (
        "```diff\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "```"
    )

    patcher, mock_openai = _patch_openai()
    mock_client = MagicMock()
    mock_openai.OpenAI.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response(fenced)

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
    gen = _gen()

    patcher, mock_openai = _patch_openai()
    mock_client = MagicMock()
    mock_openai.OpenAI.return_value = mock_client
    mock_client.chat.completions.create.return_value = _mock_openai_response("NO_FIX")

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
        call_args = mock_client.chat.completions.create.call_args
        user_msg = call_args[1]["messages"][1]["content"]
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

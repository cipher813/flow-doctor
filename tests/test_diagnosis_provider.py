"""Tests for the diagnosis provider (with mocked Anthropic API)."""

import json
from unittest.mock import MagicMock, patch

from flow_doctor.core.models import Diagnosis
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext
from flow_doctor.diagnosis.provider import AnthropicProvider


def _make_context(**kwargs):
    defaults = dict(
        error_type="ValueError",
        error_message="invalid literal",
        traceback="Traceback...\nValueError: invalid literal",
        flow_name="test-flow",
    )
    defaults.update(kwargs)
    return DiagnosisContext(**defaults)


def _mock_response(content_text, input_tokens=1000, output_tokens=500):
    """Create a mock Anthropic API response."""
    block = MagicMock()
    block.type = "text"
    block.text = content_text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def test_diagnose_parses_json():
    provider = AnthropicProvider(api_key="test-key", confidence_calibration=1.0)
    ctx = _make_context()
    assembler = ContextAssembler()

    response_json = json.dumps({
        "category": "CODE",
        "root_cause": "Invalid type conversion in parser",
        "affected_files": ["parser.py:42"],
        "confidence": 0.90,
        "remediation": "Fix the type conversion",
        "auto_fixable": True,
        "alternative_hypotheses": ["Could be input data issue"],
        "reasoning": "The traceback points to a ValueError in the parser",
    })

    mock_resp = _mock_response(response_json)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = provider.diagnose(ctx, assembler)

    assert isinstance(result, Diagnosis)
    assert result.category == "CODE"
    assert result.root_cause == "Invalid type conversion in parser"
    assert result.confidence == 0.90
    assert result.affected_files == ["parser.py:42"]
    assert result.remediation == "Fix the type conversion"
    assert result.auto_fixable is True
    assert result.alternative_hypotheses == ["Could be input data issue"]
    assert result.source == "llm"
    assert result.tokens_used == 1500
    assert result.cost_usd is not None


def test_confidence_calibration():
    provider = AnthropicProvider(api_key="test-key", confidence_calibration=0.85)
    ctx = _make_context()
    assembler = ContextAssembler()

    response_json = json.dumps({
        "category": "DATA",
        "root_cause": "Missing input file",
        "confidence": 1.0,
    })

    mock_resp = _mock_response(response_json)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = provider.diagnose(ctx, assembler)

    assert result.confidence == 0.85  # 1.0 * 0.85


def test_parse_json_from_code_fence():
    data = {"category": "TRANSIENT", "root_cause": "timeout", "confidence": 0.7}
    text = f"Here's my analysis:\n```json\n{json.dumps(data)}\n```"
    result = AnthropicProvider._parse_json(text)
    assert result["category"] == "TRANSIENT"


def test_parse_json_from_braces():
    data = {"category": "INFRA", "root_cause": "OOM", "confidence": 0.8}
    text = f"The diagnosis is: {json.dumps(data)} and that's it."
    result = AnthropicProvider._parse_json(text)
    assert result["category"] == "INFRA"


def test_parse_json_fallback():
    result = AnthropicProvider._parse_json("This is not JSON at all")
    assert result["category"] == "CODE"
    assert result["confidence"] == 0.3


def test_invalid_category_normalized():
    provider = AnthropicProvider(api_key="test-key", confidence_calibration=1.0)
    ctx = _make_context()
    assembler = ContextAssembler()

    response_json = json.dumps({
        "category": "UNKNOWN_CATEGORY",
        "root_cause": "something",
        "confidence": 0.5,
    })

    mock_resp = _mock_response(response_json)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = provider.diagnose(ctx, assembler)

    assert result.category == "CODE"  # Falls back to CODE


def test_cost_calculation():
    provider = AnthropicProvider(api_key="test-key", confidence_calibration=1.0)
    ctx = _make_context()
    assembler = ContextAssembler()

    response_json = json.dumps({
        "category": "CODE",
        "root_cause": "bug",
        "confidence": 0.9,
    })

    # 10K input, 1K output
    mock_resp = _mock_response(response_json, input_tokens=10000, output_tokens=1000)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = provider.diagnose(ctx, assembler)

    # $3/M input + $15/M output = 10000*3/1M + 1000*15/1M = 0.03 + 0.015 = 0.045
    assert abs(result.cost_usd - 0.045) < 0.001
    assert result.tokens_used == 11000

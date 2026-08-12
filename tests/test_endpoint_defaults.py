"""flow-doctor ships no provider endpoint of its own.

The defect these guard against shipped for real, in three places at once:
``DiagnosisConfig.base_url``, the YAML parser's ``base_url`` fallback, and
``FixGenerator.__init__`` each carried the literal ``https://openrouter.ai/api/v1``.
A deployment that set ``provider: openai_compat`` without naming an endpoint
sent its diagnosis context — tracebacks, log tails, source excerpts, and for
auto-fix the full contents of the affected files — to a third party nobody
configured, from inside the error path of an unattended pipeline.

Two kinds of test here, and the second matters more:

* behavioural tests, that each shape resolves or fails closed;
* a **source-level** test (:func:`test_no_provider_endpoint_literal_in_package`)
  asserting no packaged module contains a provider endpoint URL at all. The
  behavioural tests only cover paths someone thought to write; the literal scan
  covers the ones they did not — which is where this lived. The suite was fully
  green the whole time the literals were present, because nothing asserted on
  what the *default* was.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from flow_doctor.core.config import DiagnosisConfig, load_config


# ── No default may creep back ────────────────────────────────────────────────


def test_base_url_has_no_default_on_the_model():
    assert DiagnosisConfig().base_url is None


def test_yaml_parser_substitutes_no_default_base_url():
    """The parser had its own copy of the literal.

    A defect fixed on the model but not on the parser is not fixed — the parser
    is the path every file-configured deployment actually takes.
    """
    cfg = load_config(
        flow_name="t",
        diagnosis={"enabled": True, "provider": "openai_compat", "api_key": "k"},
    )
    assert cfg.diagnosis.base_url is None


def test_explicit_base_url_is_preserved():
    cfg = load_config(
        flow_name="t",
        diagnosis={
            "enabled": True,
            "provider": "openai_compat",
            "api_key": "k",
            "base_url": "http://router.internal:8080/v1",
        },
    )
    assert cfg.diagnosis.base_url == "http://router.internal:8080/v1"


# ── The class-level guard ────────────────────────────────────────────────────


#: Hosts that are somebody's inference API. A packaged module naming one in a
#: reachable expression is holding a route, which is the whole defect.
_PROVIDER_HOST_RE = re.compile(
    r"https?://[^\s\"']*"
    r"(openrouter\.ai|api\.openai\.com|api\.anthropic\.com|api\.deepseek\.com"
    r"|generativelanguage\.googleapis\.com|api\.x\.ai|api\.mistral\.ai)",
    re.IGNORECASE,
)


def test_no_provider_endpoint_literal_in_package():
    """No packaged module may contain a provider endpoint URL.

    This is the test that would have caught the original defect. Comment lines
    are skipped so the history can be explained where it is relevant; anything
    the code can actually reach is a failure.
    """
    pkg = pathlib.Path(__file__).resolve().parent.parent / "flow_doctor"
    offenders = []

    for path in sorted(pkg.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _PROVIDER_HOST_RE.search(line):
                offenders.append(f"{path.relative_to(pkg.parent)}:{lineno}: {stripped}")

    assert not offenders, (
        "provider endpoint literal(s) found in packaged code — flow-doctor "
        "must take every endpoint from operator config or resolve it through "
        "the router, never hold one:\n  " + "\n  ".join(offenders)
    )


# ── Consumers fail closed rather than picking a destination ──────────────────


def test_client_disables_diagnosis_when_openai_compat_has_no_base_url(capsys):
    """flow-doctor runs in its consumers' error path.

    An unusable route must not raise into a pipeline that is already handling a
    failure — but it must say so loudly enough to be found, and it must not
    substitute an endpoint.
    """
    from flow_doctor import FlowDoctor

    cfg = load_config(
        flow_name="t",
        diagnosis={"enabled": True, "provider": "openai_compat", "api_key": "k"},
    )
    fd = FlowDoctor(cfg)

    assert fd._diagnosis_provider is None
    err = capsys.readouterr().err
    assert "diagnosis disabled" in err
    assert "base_url" in err
    # Must not suggest a vendor to send to — that reintroduces the defect as a
    # copy-paste recommendation.
    assert "openrouter.ai" not in err


def test_fix_generator_openai_compat_without_base_url_fails_closed():
    """The openai SDK defaults to OpenAI's API, so unset is not inert."""
    from flow_doctor.fix.generator import FixGenerator

    gen = FixGenerator(api_key="k", model="m", provider="openai_compat")
    assert gen.base_url is None

    with pytest.raises(ValueError) as exc:
        gen._complete_openai_compat("prompt")
    assert "base_url" in str(exc.value)


def test_fix_generator_base_url_default_is_none():
    from flow_doctor.fix.generator import FixGenerator

    assert FixGenerator(api_key="k").base_url is None

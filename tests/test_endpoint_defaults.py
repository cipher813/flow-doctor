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

    assert FixGenerator(api_key="k", provider="openai_compat").base_url is None


# ── No vendor default may creep back in (0.15.0) ─────────────────────────────
#
# `AnthropicProvider` was deleted in 0.15.0 (Brian ruling: flow-doctor must
# not depend on the `anthropic` distribution or construct a direct-Anthropic
# client anywhere). The specific defect that made this necessary:
# `DiagnosisConfig.provider` used to default to `"anthropic"`, so every fleet
# config that simply omitted `provider:` silently took a direct, unscanned
# connection to one vendor with nothing in its own configuration saying so.
# These guards are the sibling of `test_no_provider_endpoint_literal_in_package`
# above — that one catches a hardcoded endpoint URL, these catch the vendor
# SDK and the quiet default coming back.


def test_anthropic_distribution_not_a_dependency_anywhere():
    """No `pyproject.toml` dependency list — base or any extra — may name the
    `anthropic` distribution. It was declared in three places at once
    (`diagnosis`, `agent`, `all` extras) before 0.15.0; `krepis`'s own
    `flow_doctor` extra floors `flow-doctor[diagnosis,s3]`, so the `diagnosis`
    extra alone forced the Anthropic SDK onto every `krepis[flow_doctor]`
    consumer transitively — including repos that had deliberately removed
    direct LLM exposure (crucible-executor, 2026-05-25).
    """
    try:
        import tomllib
    except ImportError:  # Python < 3.11 (repo supports 3.9+); optional here.
        tomllib = pytest.importorskip("tomli")

    pyproject_path = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    def _names(specs):
        # A PEP 508 requirement string: name, optional extras in [...], then
        # a version specifier / other trailer. The distribution name is
        # everything before the first non-name character.
        for spec in specs:
            name = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
            if name:
                yield name.group(1).lower()

    offenders = []
    base_deps = data.get("project", {}).get("dependencies", [])
    if "anthropic" in _names(base_deps):
        offenders.append("project.dependencies")
    for extra, specs in data.get("project", {}).get("optional-dependencies", {}).items():
        if "anthropic" in _names(specs):
            offenders.append(f"project.optional-dependencies.{extra}")

    assert not offenders, (
        "the 'anthropic' distribution is declared as a dependency in: "
        + ", ".join(offenders)
    )


def test_no_anthropic_import_in_package():
    """No packaged module may import the `anthropic` SDK. Comment lines are
    skipped (history may be explained where relevant); anything the code can
    actually reach is a failure."""
    pkg = pathlib.Path(__file__).resolve().parent.parent / "flow_doctor"
    offenders = []

    import_re = re.compile(r"^\s*(import\s+anthropic\b|from\s+anthropic\b)")

    for path in sorted(pkg.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if import_re.match(stripped):
                offenders.append(f"{path.relative_to(pkg.parent)}:{lineno}: {stripped}")

    assert not offenders, (
        "packaged code imports 'anthropic' — flow-doctor must not construct "
        "a direct-Anthropic client anywhere:\n  " + "\n  ".join(offenders)
    )


def test_diagnosis_provider_has_no_default_value():
    """`DiagnosisConfig.provider` must have no vendor default. It used to
    default to `"anthropic"` — the actual defect: every config that omitted
    `provider:` silently chose a vendor. `None` (not a vendor) is the only
    legal default; an explicit choice or a loud ConfigError, never a
    picked-for-you vendor.
    """
    field = DiagnosisConfig.model_fields["provider"]
    assert field.default is None
    assert DiagnosisConfig().provider is None


def test_fix_generator_provider_parameter_has_no_default():
    """`FixGenerator.__init__`'s `provider` parameter must be required — no
    default value at all — mirroring `DiagnosisConfig.provider` above."""
    import inspect

    from flow_doctor.fix.generator import FixGenerator

    sig = inspect.signature(FixGenerator.__init__)
    assert sig.parameters["provider"].default is inspect.Parameter.empty

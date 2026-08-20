"""Every raised exception message in the packaged library must be
Latin-1-encodable.

Root incident (alpha-engine-config-I7855): a ConfigError message containing
an em-dash ("—") crashed init on alpha-engine-data-collector's Lambda
v341. AWS's `awslambdaric` (the Lambda Runtime Interface Client) encodes its
`post_init_error` response as Latin-1 — not something flow-doctor controls,
and confirmed upstream/out of scope for this repo (alpha-engine-config-I<TBD
followup>, filed rather than patched around). The em-dash's UnicodeEncodeError
inside `post_init_error` itself degraded a clean, readable init-error report
(which WOULD have named the real ConfigError) into an opaque
`Runtime.ExitError` with no parseable error key — the actual root cause
(diagnosis.provider unset) never reached CloudWatch's INIT_REPORT at all.

flow-doctor's own error messages are the one thing in this chain we DO
control, and they are also the only strings the strict-mode caller sees
before that Lambda-runtime boundary. Keeping them Latin-1-safe means a
future config/validation error still reports cleanly through
`post_init_error` even though the underlying encoding bug is not ours to
fix. This is a source-level literal scan (mirrors
test_endpoint_defaults.py::test_no_provider_endpoint_literal_in_package) so
it catches every raise site, not just the ones a behavioural test thought
to exercise.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "flow_doctor"

_RAISE_CALL_NAMES = {
    "ConfigError",
    "ValueError",
    "RuntimeError",
    "KeyError",
    "TypeError",
    "RouterUnresolvable",
    "StorageBackendError",
}


def _iter_python_files():
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _non_latin1_chars(s: str) -> set[str]:
    return {c for c in s if ord(c) > 255}


def _collect_string_literals(node: ast.AST) -> list[str]:
    """Collect every string literal (incl. f-string static parts) under node."""
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


@pytest.mark.parametrize("path", _iter_python_files(), ids=lambda p: str(p.relative_to(PACKAGE_ROOT)))
def test_raised_exception_messages_are_latin1_safe(path: pathlib.Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        call = node.exc
        if isinstance(call, ast.Call):
            func = call.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name not in _RAISE_CALL_NAMES:
                continue
            for arg in call.args:
                for literal in _collect_string_literals(arg):
                    bad = _non_latin1_chars(literal)
                    if bad:
                        offenders.append(f"line {node.lineno}: {bad!r} in {literal!r}")
    assert not offenders, (
        f"{path}: raised exception message(s) contain non-Latin-1 characters "
        f"(awslambdaric's post_init_error is Latin-1-only; a non-Latin-1 "
        f"character here degrades a clean init-error report into an opaque "
        f"Runtime.ExitError — alpha-engine-config-I7855): {offenders}"
    )

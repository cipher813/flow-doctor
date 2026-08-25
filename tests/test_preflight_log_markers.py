"""Every preflight-unreachable path emits the same machine-readable marker.

alpha-engine-config-I8298 deliverable 5. A CloudWatch metric filter is how a
signal LEAVES a process by a path the failing process does not control — on
2026-08-24 the only alerting transport `alpha-engine-predictor-inference` had
was Telegram, which was the thing that was unreachable.

The first filter written for that matched the PROSE `"preflight unreachable"`,
which is `TelegramNotifier`'s wording alone. It would have caught the incident
that prompted it and nothing else like it: not the class-level guard in
`FlowDoctor._run_notifier_preflights` that covers every notifier including
third-party ones, and not `GitHubNotifier`, which carried the identical defect.

These tests are the contract that keeps the marker on every path, so the filter
matches the CONDITION and the prose stays free to change.
"""

from __future__ import annotations

import logging
import socket

import pytest

from flow_doctor import FlowDoctor
from flow_doctor.notify.base import (
    PREFLIGHT_UNREACHABLE_MARKER,
    PREFLIGHT_UNVALIDATED_MARKER,
    Notifier,
)
from flow_doctor.notify.github import GitHubNotifier
from flow_doctor.notify.telegram import TelegramNotifier


class _Recorder(Notifier):
    """Minimal notifier whose preflight outcome the test dictates."""

    def __init__(self, exc: BaseException | None = None, delay: float = 0.0):
        self.exc = exc
        self.delay = delay
        self.validated = False
        self.last_error = None

    def send(self, report, flow_name, diagnosis=None):  # pragma: no cover - unused
        return None

    def validate(self) -> None:
        self.validated = True
        if self.delay:
            import time as _t

            _t.sleep(self.delay)
        if self.exc is not None:
            raise self.exc


def test_telegram_transport_warning_carries_the_marker(monkeypatch, caplog):
    monkeypatch.delenv("FLOW_DOCTOR_SKIP_PREFLIGHT", raising=False)
    notifier = TelegramNotifier(bot_token="t", chat_id=1)
    monkeypatch.setattr(
        "flow_doctor.notify.telegram.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("The read operation timed out")),
    )
    with caplog.at_level(logging.WARNING, logger="flow_doctor"):
        notifier.validate()
    assert PREFLIGHT_UNREACHABLE_MARKER in caplog.text


def test_github_transport_warning_carries_the_marker(monkeypatch, caplog):
    monkeypatch.delenv("FLOW_DOCTOR_SKIP_PREFLIGHT", raising=False)
    notifier = GitHubNotifier(token="t", repo="owner/repo")
    monkeypatch.setattr(
        "flow_doctor.notify.github.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(socket.timeout("timed out")),
    )
    with caplog.at_level(logging.WARNING, logger="flow_doctor"):
        notifier.validate()
    assert PREFLIGHT_UNREACHABLE_MARKER in caplog.text


def test_class_level_guard_carries_the_marker(capsys):
    """The path that covers notifiers this package has never heard of."""
    fd = object.__new__(FlowDoctor)
    fd._run_notifier_preflights([_Recorder(exc=TimeoutError("timed out"))])
    assert PREFLIGHT_UNREACHABLE_MARKER in capsys.readouterr().err


def test_budget_exhaustion_carries_its_own_marker(monkeypatch, capsys):
    """UNVALIDATED is a different condition from UNREACHABLE and gets its own token."""
    monkeypatch.setenv("FLOW_DOCTOR_PREFLIGHT_BUDGET_S", "0.05")
    fd = object.__new__(FlowDoctor)
    fd._run_notifier_preflights([_Recorder(delay=0.1), _Recorder()])
    err = capsys.readouterr().err
    assert PREFLIGHT_UNVALIDATED_MARKER in err
    assert "UNVALIDATED" in err


@pytest.mark.parametrize(
    "marker", [PREFLIGHT_UNREACHABLE_MARKER, PREFLIGHT_UNVALIDATED_MARKER]
)
def test_markers_are_filter_safe(marker):
    """A CloudWatch metric filter pattern quotes these verbatim.

    Bracketed, upper-snake, no spaces, no characters that need escaping in a
    quoted filter pattern. If a rename breaks this, the detector silently
    matches nothing.
    """
    assert marker.startswith("[FLOW_DOCTOR_") and marker.endswith("]")
    assert " " not in marker
    assert marker.replace("[", "").replace("]", "").replace("_", "").isalnum()


def test_the_two_markers_are_distinct():
    assert PREFLIGHT_UNREACHABLE_MARKER != PREFLIGHT_UNVALIDATED_MARKER

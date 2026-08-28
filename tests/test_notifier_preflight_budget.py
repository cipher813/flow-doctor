"""Notifier preflight is bounded and never crashes the instrumented process.

alpha-engine-config-I8298. On 2026-08-24 `api.telegram.org` stopped
answering for roughly 90 seconds. `TelegramNotifier.validate()` raised the
resulting read timeout, `FlowDoctor.__init__` re-raised it under `strict`,
and because `krepis.setup_logging` is called at MODULE level in
`inference/handler.py`, the import of `alpha-engine-predictor-inference`
failed outright. The postclose trading pipeline's MarketHoursGate and
DeployDriftCheck both degraded on that one unreachable host.

The rule these tests hold: a monitoring channel that cannot be reached is a
reason to WARN, never a reason to stop the workload it was only watching. A
credential the endpoint actively REJECTED is still a hard failure under
`strict` — that distinction is the whole point.
"""

from __future__ import annotations

import socket

import pytest

from flow_doctor import ConfigError, FlowDoctor
from flow_doctor.notify.base import Notifier


class _Recorder(Notifier):
    """Minimal notifier whose preflight outcome the test dictates."""

    def __init__(self, name: str, exc: BaseException | None = None, delay: float = 0.0):
        self.name = name
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


def _fd() -> FlowDoctor:
    """A FlowDoctor shell — these tests exercise the preflight loop only."""
    return object.__new__(FlowDoctor)


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("The read operation timed out"),
        socket.timeout("timed out"),
        ConnectionResetError("reset by peer"),
        OSError("Network is unreachable"),
    ],
    ids=["read-timeout", "socket-timeout", "conn-reset", "unreachable"],
)
def test_transport_failure_does_not_propagate(exc, capsys):
    n = _Recorder("telegram", exc=exc)
    _fd()._run_notifier_preflights([n])
    err = capsys.readouterr().err
    assert "preflight could not reach" in err
    assert "_Recorder" in err


def test_transport_failure_does_not_stop_later_notifiers():
    """One unreachable endpoint must not blind the notifiers behind it."""
    first = _Recorder("telegram", exc=TimeoutError("timed out"))
    second = _Recorder("s3")
    _fd()._run_notifier_preflights([first, second])
    assert second.validated is True


def test_credential_verdict_still_raises():
    """A REJECTED credential is not a transport failure and still fails loud."""
    n = _Recorder("telegram", exc=ConfigError("bot token rejected"))
    with pytest.raises(ConfigError):
        _fd()._run_notifier_preflights([n])


def test_budget_exhaustion_names_the_unvalidated(monkeypatch, capsys):
    """No silent caps: a skipped preflight is reported, not assumed fine."""
    monkeypatch.setenv("FLOW_DOCTOR_PREFLIGHT_BUDGET_S", "0.05")
    slow = _Recorder("slow", delay=0.1)
    never = _Recorder("never")
    _fd()._run_notifier_preflights([slow, never])
    err = capsys.readouterr().err
    assert slow.validated is True
    assert never.validated is False
    assert "UNVALIDATED" in err
    assert "_Recorder" in err


@pytest.mark.parametrize("raw", ["nonsense", "0", "-1"])
def test_budget_falls_back_rather_than_unbounding(monkeypatch, raw):
    monkeypatch.setenv("FLOW_DOCTOR_PREFLIGHT_BUDGET_S", raw)
    assert FlowDoctor._preflight_budget() == FlowDoctor._DEFAULT_PREFLIGHT_BUDGET_S


def test_slow_call_in_flight_does_not_block_past_the_budget(monkeypatch, capsys):
    """alpha-engine-config-I9102: the budget must bound a call ALREADY IN
    FLIGHT, not just gate whether the NEXT notifier gets to start.

    Before this fix, ``_run_notifier_preflights`` called ``validate()``
    synchronously on the main thread — the pre-loop budget check only
    decided whether to START a notifier's preflight, and did nothing once
    a call was already running. A notifier whose own transport has no
    internal timeout (a bare ``boto3.client()`` defaults to 60s
    connect + 60s read + up to 5 retries) could single-handedly consume
    the entire budget and then some, which is exactly what turned a
    Lambda's 1.6s of real handler work into a 300s `States.Timeout` on
    the live `eval_rolling_mean` weekly run. This asserts the bound is
    now HARD: a notifier's own call cannot make the whole preflight step
    run any longer than the configured budget, even mid-call.
    """
    import time as _t

    monkeypatch.setenv("FLOW_DOCTOR_PREFLIGHT_BUDGET_S", "0.1")
    # Far longer than the budget — stands in for an unbounded transport
    # call (e.g. S3's default botocore timeouts) that never returns on its
    # own inside the budget window.
    stuck = _Recorder("stuck", delay=5.0)

    started = _t.monotonic()
    _fd()._run_notifier_preflights([stuck])
    elapsed = _t.monotonic() - started

    # The call is still sleeping on its abandoned daemon thread — this
    # process must not have waited for it.
    assert elapsed < 1.0, (
        f"_run_notifier_preflights blocked for {elapsed:.2f}s against a 0.1s "
        f"budget — a slow notifier call is still able to hold up the caller"
    )
    err = capsys.readouterr().err
    assert "UNVALIDATED" in err
    assert "timed out mid-call" in err


def test_unreachable_telegram_does_not_break_strict_init(tmp_path, monkeypatch):
    """The end-to-end invariant, at the layer the predictor Lambda hits."""
    monkeypatch.delenv("FLOW_DOCTOR_SKIP_PREFLIGHT", raising=False)

    def _timeout(*_a, **_kw):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("flow_doctor.notify.telegram.urlopen", _timeout)
    fd = FlowDoctor.from_config(
        flow_name="test",
        store={"type": "sqlite", "path": str(tmp_path / "fd.db")},
        notify=[{"type": "telegram", "bot_token": "t", "chat_id": 1}],
        strict=True,
    )
    assert fd._healthy is True
    assert len(fd._notifiers) == 1

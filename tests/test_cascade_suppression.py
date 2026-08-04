"""A cascade report is recorded and digested, never paged.

`cascade_source` is computed by the cascade detector, persisted on the report,
and rendered into every notifier's message body ("Likely caused by upstream
`X` failure"). Until this gate existed, `FlowDoctor._send_notifications` took
`is_cascade` as a parameter and never read it — so a failure the system had
already attributed to an upstream root cause was pushed at full severity
anyway. On 2026-08-04 that turned one `predictor-training` failure into five
separate ERROR pages, every one of them carrying its own cascade line.

Suppression is not a drop: the report is persisted, a DEGRADED action is
recorded so it reaches the digest, and the decision trace records
CASCADE_SUPPRESSED so "saw N, alerted M" stays honest.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from flow_doctor import FlowDoctor
from flow_doctor.core.models import Diagnosis, DecisionReason, Report
from flow_doctor.notify.base import Notifier


class _RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: List[Report] = []

    def send(
        self, report: Report, flow_name: str, diagnosis: Optional[Diagnosis] = None
    ) -> Optional[str]:
        self.sent.append(report)
        return "recording:ok"


@pytest.fixture
def fd():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        yield FlowDoctor.builder("cascade-test").with_store(path=f.name).build()


def _force_cascade(fd, source: str = "predictor-training") -> None:
    """Make the cascade detector report `source` for every check."""
    fd._cascade_detector.check_cascade = lambda *a, **k: source  # type: ignore[assignment]


def test_cascade_report_is_not_pushed(fd):
    n = _RecordingNotifier()
    fd._notifiers = [n]
    _force_cascade(fd)
    fd.report(RuntimeError("pit_parity walkforward timed out"))
    assert n.sent == [], (
        "a report attributed to an upstream failure was pushed — the cascade "
        "detector's own verdict was computed and then ignored at dispatch"
    )


def test_cascade_report_is_still_persisted_and_traced(fd):
    fd._notifiers = [_RecordingNotifier()]
    _force_cascade(fd)
    report_id = fd.report(RuntimeError("derived failure"))
    assert report_id is not None, "a suppressed cascade must still be recorded"
    breakdown = fd._store.decision_breakdown_today("cascade-test")
    assert breakdown.get(DecisionReason.CASCADE_SUPPRESSED.value) == 1, (
        f"cascade suppression must be countable, got {breakdown}"
    )


def test_cascade_report_is_queued_for_the_digest(fd):
    fd._notifiers = [_RecordingNotifier()]
    _force_cascade(fd)
    fd.report(RuntimeError("derived failure"))
    # get_degraded_actions is exactly what the digest generator reads.
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    actions = fd._store.get_degraded_actions(since)
    assert actions, "a suppressed cascade must leave a degraded action row"
    assert any("cascade" in (a.target or "") for a in actions), (
        f"suppressed cascade must be queued for the digest, got "
        f"{[(a.status, a.target) for a in actions]}"
    )


def test_notifier_can_opt_in_to_cascades(fd):
    n = _RecordingNotifier()
    n.notify_on_cascade = True
    fd._notifiers = [n]
    _force_cascade(fd)
    fd.report(RuntimeError("derived failure"))
    assert len(n.sent) == 1, "notify_on_cascade=True must still receive cascades"


def test_non_cascade_report_is_unaffected(fd):
    n = _RecordingNotifier()
    fd._notifiers = [n]
    fd._cascade_detector.check_cascade = lambda *a, **k: None  # type: ignore[assignment]
    fd.report(RuntimeError("root cause"))
    assert len(n.sent) == 1, "a root-cause failure must still page"

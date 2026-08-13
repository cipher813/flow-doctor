"""alpha-engine-config-I7276 defect 2: the alert must carry its own cause.

Before this fix, ``S3Notifier.send`` (and its siblings' internal HTTP-status
failure branches) caught the real exception, logged it below the handler's
capture threshold (WARNING) or in a separate per-notifier CRITICAL, and
returned ``None``. The dispatcher's own CRITICAL — the one message an
operator's root-logger-attached handler actually turns into an alert —
said only "(notifier-specific reason logged separately)", so the alert
that fired was structurally guaranteed not to contain the reason.

Every ``Notifier.send()`` now sets ``self.last_error`` on every failure
path; the dispatcher reads it and both logs and persists the real reason.
"""

import json
import logging
import sqlite3
from typing import Optional
from unittest.mock import patch

import pytest

from flow_doctor import FlowDoctor
from flow_doctor.core.models import Diagnosis, Report
from flow_doctor.notify.base import Notifier


@pytest.fixture
def sqlite_store(tmp_path):
    return {"type": "sqlite", "path": str(tmp_path / "flow_doctor.db")}


class _FailsWithReason(Notifier):
    """A notifier that fails without raising — mirrors S3Notifier.send's
    catch-log-return-None shape, minus the network call."""

    def send(
        self, report: Report, flow_name: str, diagnosis: Optional[Diagnosis] = None
    ) -> Optional[str]:
        self.last_error = None
        self.last_error = "ClientError: NoSuchBucket: bucket 'x' does not exist"
        return None


def test_action_metadata_carries_failure_reason(sqlite_store, monkeypatch):
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "ghp_test")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "cipher813/test"}],
    )
    fd._notifiers = [_FailsWithReason()]

    fd.report(RuntimeError("boom"), severity="error")

    conn = sqlite3.connect(sqlite_store["path"])
    rows = list(
        conn.execute("SELECT status, metadata FROM actions ORDER BY id DESC LIMIT 1")
    )
    assert len(rows) == 1
    status, metadata_json = rows[0]
    assert status == "failed"
    metadata = json.loads(metadata_json)
    assert metadata["failure_reason"] == (
        "ClientError: NoSuchBucket: bucket 'x' does not exist"
    )


def test_critical_log_names_the_actual_reason(sqlite_store, monkeypatch, caplog):
    """The single dispatcher CRITICAL must name the reason, not defer to
    'notifier-specific reason logged separately'."""
    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "ghp_test")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "cipher813/test"}],
    )
    fd._notifiers = [_FailsWithReason()]

    with caplog.at_level(logging.CRITICAL, logger="flow_doctor"):
        fd.report(RuntimeError("boom"), severity="error")

    critical_messages = [r.getMessage() for r in caplog.records]
    assert any(
        "NoSuchBucket" in m for m in critical_messages
    ), f"no CRITICAL log named the real reason: {critical_messages}"
    assert not any("reason logged separately" in m for m in critical_messages)


def test_exception_raised_by_notifier_carries_message(sqlite_store, monkeypatch):
    """The raise path (notifier.send() throwing) already carried the
    exception text in the dispatcher's CRITICAL; pin that it also now
    lands in Action.metadata."""

    class _Raises(Notifier):
        def send(self, report, flow_name, diagnosis=None):
            raise ConnectionError("s3 bucket unreachable: connection refused")

    monkeypatch.setenv("FLOW_DOCTOR_GITHUB_TOKEN", "ghp_test")
    fd = FlowDoctor.from_config(
        flow_name="test",
        store=sqlite_store,
        notify=[{"type": "github", "repo": "cipher813/test"}],
    )
    fd._notifiers = [_Raises()]

    fd.report(RuntimeError("boom"), severity="error")

    conn = sqlite3.connect(sqlite_store["path"])
    rows = list(
        conn.execute("SELECT status, metadata FROM actions ORDER BY id DESC LIMIT 1")
    )
    status, metadata_json = rows[0]
    assert status == "failed"
    metadata = json.loads(metadata_json)
    assert "connection refused" in metadata["failure_reason"]

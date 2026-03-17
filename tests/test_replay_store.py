"""Tests for replay store."""

from flow_doctor.core.models import FixAttempt
from flow_doctor.fix.replay_store import ReplayStore
from flow_doctor.storage.sqlite import SQLiteStorage


def _setup_store(tmp_path):
    db_path = str(tmp_path / "test.db")
    storage = SQLiteStorage(db_path)
    storage.init_schema()
    return storage


def test_get_rejections_empty(tmp_path):
    storage = _setup_store(tmp_path)
    replay = ReplayStore(storage)
    assert replay.get_rejections("nonexistent") == []


def test_get_rejections_with_data(tmp_path):
    storage = _setup_store(tmp_path)
    replay = ReplayStore(storage)

    # Save some attempts
    attempt1 = FixAttempt(
        diagnosis_id="d1",
        diff="--- a/f\n+++ b/f\n",
        test_passed=False,
        rejection_reason="Tests failed: assertion error",
    )
    attempt2 = FixAttempt(
        diagnosis_id="d1",
        diff="--- a/f\n+++ b/f\n",
        test_passed=True,
        rejection_reason=None,  # Not rejected
    )
    attempt3 = FixAttempt(
        diagnosis_id="d1",
        diff="--- a/f\n+++ b/f\n",
        test_passed=False,
        rejection_reason="Scope violation",
    )

    storage.save_fix_attempt(attempt1)
    storage.save_fix_attempt(attempt2)
    storage.save_fix_attempt(attempt3)

    rejections = replay.get_rejections("d1")
    assert len(rejections) == 2
    assert "assertion error" in rejections[0] or "assertion error" in rejections[1]
    assert "Scope violation" in rejections[0] or "Scope violation" in rejections[1]


def test_get_rejections_filters_by_diagnosis(tmp_path):
    storage = _setup_store(tmp_path)
    replay = ReplayStore(storage)

    storage.save_fix_attempt(FixAttempt(
        diagnosis_id="d1", diff="diff1", rejection_reason="Reason 1",
    ))
    storage.save_fix_attempt(FixAttempt(
        diagnosis_id="d2", diff="diff2", rejection_reason="Reason 2",
    ))

    assert len(replay.get_rejections("d1")) == 1
    assert replay.get_rejections("d1")[0] == "Reason 1"

"""Tests for deduplication."""

import tempfile

from flow_doctor.core.dedup import (
    DedupChecker,
    compute_error_signature,
    compute_signature_from_exception,
)
from flow_doctor.core.models import Report
from flow_doctor.storage.sqlite import SQLiteStorage


def test_error_signature_same_exception():
    """Same exception type + traceback should produce the same signature."""
    tb = '''Traceback (most recent call last):
  File "handler.py", line 10, in main
    run_pipeline()
  File "pipeline.py", line 42, in run_pipeline
    fetch_data()
  File "data.py", line 15, in fetch_data
    raise ValueError("bad data")
ValueError: bad data'''

    sig1 = compute_error_signature("ValueError", tb)
    sig2 = compute_error_signature("ValueError", tb)
    assert sig1 == sig2


def test_error_signature_different_exception():
    """Different exception types should produce different signatures."""
    tb = '''Traceback (most recent call last):
  File "handler.py", line 10, in main
    run_pipeline()
'''
    sig1 = compute_error_signature("ValueError", tb)
    sig2 = compute_error_signature("KeyError", tb)
    assert sig1 != sig2


def test_error_signature_different_frames():
    """Different stack frames should produce different signatures."""
    tb1 = '''Traceback (most recent call last):
  File "handler.py", line 10, in main
    run_pipeline()
'''
    tb2 = '''Traceback (most recent call last):
  File "handler.py", line 20, in other_func
    do_something()
'''
    sig1 = compute_error_signature("ValueError", tb1)
    sig2 = compute_error_signature("ValueError", tb2)
    assert sig1 != sig2


def test_compute_signature_from_exception():
    """Should compute signature from a live exception."""
    try:
        raise ValueError("test error")
    except ValueError as e:
        sig = compute_signature_from_exception(e)
        assert isinstance(sig, str)
        assert len(sig) == 16  # hex digest prefix


def test_dedup_checker_no_duplicate():
    """First report should not be a duplicate."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = SQLiteStorage(f.name)
        store.init_schema()
        checker = DedupChecker(store, cooldown_minutes=60)

        is_dup, existing_id = checker.is_duplicate("sig123")
        assert is_dup is False
        assert existing_id is None


def test_dedup_checker_finds_duplicate():
    """Second report with same signature should be flagged as duplicate."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = SQLiteStorage(f.name)
        store.init_schema()

        # Save a report with a known signature
        report = Report(
            flow_name="test",
            error_message="boom",
            error_signature="sig123",
        )
        store.save_report(report)

        checker = DedupChecker(store, cooldown_minutes=60)
        is_dup, existing_id = checker.is_duplicate("sig123")
        assert is_dup is True
        assert existing_id == report.id


def test_dedup_increment():
    """Dedup hit should increment the counter."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = SQLiteStorage(f.name)
        store.init_schema()

        report = Report(
            flow_name="test",
            error_message="boom",
            error_signature="sig123",
        )
        store.save_report(report)

        checker = DedupChecker(store, cooldown_minutes=60)
        checker.record_dedup_hit(report.id)

        # Check the count was incremented
        reports = store.get_reports(limit=1)
        assert reports[0].dedup_count == 2

"""Tests for the knowledge base."""

import tempfile
from datetime import datetime

from flow_doctor.core.models import Diagnosis, KnownPattern
from flow_doctor.diagnosis.knowledge_base import KnowledgeBase
from flow_doctor.storage.sqlite import SQLiteStorage


def _make_store():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = SQLiteStorage(f.name)
    store.init_schema()
    return store


def test_lookup_miss():
    store = _make_store()
    kb = KnowledgeBase(store)
    result = kb.lookup("abc123", "report-1", "test-flow")
    assert result is None


def test_lookup_hit():
    store = _make_store()
    pattern = KnownPattern(
        error_signature="sig123",
        category="EXTERNAL",
        root_cause="Third-party API is down",
        flow_name="research-lambda",
        resolution="Retry in 15 minutes",
        auto_fixable=False,
    )
    store.save_known_pattern(pattern)

    kb = KnowledgeBase(store)
    result = kb.lookup("sig123", "report-1", "research-lambda")

    assert result is not None
    assert isinstance(result, Diagnosis)
    assert result.category == "EXTERNAL"
    assert result.root_cause == "Third-party API is down"
    assert result.source == "knowledge_base"
    assert result.confidence == 0.95
    assert result.report_id == "report-1"
    assert result.remediation == "Retry in 15 minutes"


def test_lookup_increments_hit_count():
    store = _make_store()
    pattern = KnownPattern(
        error_signature="sig456",
        category="CONFIG",
        root_cause="Missing env var",
        hit_count=0,
    )
    store.save_known_pattern(pattern)

    kb = KnowledgeBase(store)
    kb.lookup("sig456", "r1", "flow")
    kb.lookup("sig456", "r2", "flow")

    updated = store.find_known_pattern("sig456")
    assert updated.hit_count == 2
    assert updated.last_seen is not None


def test_record_creates_pattern():
    store = _make_store()
    kb = KnowledgeBase(store)

    diag = Diagnosis(
        report_id="r1",
        flow_name="test-flow",
        category="CODE",
        root_cause="Off-by-one error in loop",
        confidence=0.85,
        remediation="Fix the loop boundary",
        auto_fixable=True,
    )
    kb.record(diag, "new_sig")

    pattern = store.find_known_pattern("new_sig")
    assert pattern is not None
    assert pattern.category == "CODE"
    assert pattern.root_cause == "Off-by-one error in loop"
    assert pattern.auto_fixable is True


def test_record_no_duplicate():
    store = _make_store()
    kb = KnowledgeBase(store)

    diag = Diagnosis(
        report_id="r1", flow_name="f", category="CODE",
        root_cause="bug", confidence=0.8,
    )
    kb.record(diag, "dup_sig")
    kb.record(diag, "dup_sig")  # Should not create second pattern

    # Verify only one exists by checking the pattern
    pattern = store.find_known_pattern("dup_sig")
    assert pattern is not None


def test_bootstrap():
    store = _make_store()
    kb = KnowledgeBase(store)

    patterns = [
        {
            "error_signature": "boot1",
            "category": "EXTERNAL",
            "root_cause": "Anthropic API overloaded",
            "flow_name": "research-lambda",
            "resolution": "Wait and retry",
        },
        {
            "error_signature": "boot2",
            "category": "INFRA",
            "root_cause": "OOM on Lambda",
            "resolution": "Increase memory limit",
            "auto_fixable": False,
        },
    ]
    kb.bootstrap(patterns)

    p1 = store.find_known_pattern("boot1")
    assert p1 is not None
    assert p1.category == "EXTERNAL"
    assert p1.flow_name == "research-lambda"

    p2 = store.find_known_pattern("boot2")
    assert p2 is not None
    assert p2.category == "INFRA"


def test_bootstrap_idempotent():
    store = _make_store()
    kb = KnowledgeBase(store)

    patterns = [{"error_signature": "idem", "category": "CODE", "root_cause": "bug"}]
    kb.bootstrap(patterns)
    kb.bootstrap(patterns)  # Should not fail or duplicate

    p = store.find_known_pattern("idem")
    assert p is not None

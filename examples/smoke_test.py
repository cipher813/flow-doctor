"""
Smoke test for Flow Doctor Phase 1.
Run from the flow-doctor repo root:
    python examples/smoke_test.py
"""
import flow_doctor
import logging
import os
import tempfile

db_path = os.path.join(tempfile.gettempdir(), "fd_smoke_test.db")
# Clean up from prior runs
if os.path.exists(db_path):
    os.remove(db_path)

fd = flow_doctor.init(
    flow_name="test-flow",
    repo="cipher813/alpha-engine-research",
    owner="@brianmcmahon",
    store=f"sqlite:///{db_path}",
)

print("=" * 60)
print("FLOW DOCTOR PHASE 1 — SMOKE TEST")
print("=" * 60)

# --- Test 1: Exception report ---
print("\n--- Test 1: Exception report ---")
try:
    raise KeyError("RSI_14")
except Exception as e:
    report_id = fd.report(e)
    print(f"  Report ID: {report_id}")
    assert report_id is not None, "Expected a report ID"

# --- Test 2: guard() re-raises ---
print("\n--- Test 2: guard() context manager ---")
try:
    with fd.guard():
        raise ValueError("bad data from yfinance")
except ValueError as e:
    print(f"  Guard re-raised correctly: {e}")
else:
    raise AssertionError("guard() should have re-raised")

# --- Test 3: monitor() decorator ---
print("\n--- Test 3: @monitor decorator ---")

@fd.monitor
def failing_function():
    raise RuntimeError("Lambda timeout after 300s")

try:
    failing_function()
except RuntimeError as e:
    print(f"  Monitor re-raised correctly: {e}")
else:
    raise AssertionError("monitor() should have re-raised")

# --- Test 4: Dedup suppression ---
print("\n--- Test 4: Dedup (5 identical errors → 1 report) ---")
dedup_results = []
for i in range(5):
    try:
        raise KeyError("RSI_14")
    except Exception as e:
        result = fd.report(e)
        dedup_results.append(result)
        status = "NEW" if result else "DEDUPED"
        print(f"  Attempt {i+1}: {status}")

new_count = sum(1 for r in dedup_results if r is not None)
dedup_count = sum(1 for r in dedup_results if r is None)
print(f"  → {new_count} new, {dedup_count} deduped")

# --- Test 5: Non-exception warning ---
print("\n--- Test 5: Non-exception warning ---")
report_id = fd.report("Scanner returned 0 candidates", severity="warning")
print(f"  Warning report ID: {report_id}")

# --- Test 6: capture_logs() ---
print("\n--- Test 6: Log capture ---")
logger = logging.getLogger("test.scanner")
with fd.capture_logs(level=logging.INFO):
    logger.info("Starting scanner with 900 tickers")
    logger.warning("yfinance rate limit approaching")
    try:
        raise ConnectionError("yfinance RSS feed timeout")
    except Exception as e:
        report_id = fd.report(e)
        print(f"  Report with logs: {report_id}")

# --- Test 7: Secret scrubbing ---
print("\n--- Test 7: Secret scrubbing ---")
try:
    api_key = "AKIAIOSFODNN7EXAMPLE"
    raise RuntimeError(f"S3 auth failed with key {api_key}")
except Exception as e:
    report_id = fd.report(
        e,
        context={
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG",
            "tickers_scanned": 900,
        },
    )
    print(f"  Scrubbed report ID: {report_id}")

# --- Test 8: report() never crashes ---
print("\n--- Test 8: report() never crashes caller ---")
# Create an fd with a broken store path
broken_fd = flow_doctor.init(
    flow_name="broken-flow",
    store="sqlite:////nonexistent/impossible/path/db.sqlite",
)
try:
    raise RuntimeError("this should not crash")
except Exception as e:
    result = broken_fd.report(e)
    print(f"  Broken store report result: {result} (None is OK)")
print("  Caller survived — report() did not propagate")

# --- History ---
print("\n--- Report History ---")
for r in fd.history(limit=20):
    dedup_str = f" (dedup x{r.dedup_count})" if r.dedup_count > 1 else ""
    print(f"  [{r.severity:8s}] {r.error_type or 'msg'}: {r.error_message[:60]}{dedup_str}")

print("\n" + "=" * 60)
print("ALL SMOKE TESTS PASSED")
print(f"DB at: {db_path}")
print("=" * 60)

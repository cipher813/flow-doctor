#!/usr/bin/env python3
"""Secret scanning must scan the same way locally and in CI.

alpha-engine-config-I6622, sweep finding. `Gitleaks scan` failed on FOURTEEN
consecutive runs on main — 2026-07-31 to 2026-08-06 — and nobody read any of
them. The cause was not a leak. It was a configuration split:

  * `.gitleaks.toml` was TRACKED, as a symlink whose target is the absolute
    path `/Users/brianmcmahon/Development/.gitleaks.toml`. That path exists on
    one laptop and in no CI checkout, where the symlink simply dangles.
  * So a local scan ran WITH the shared config and CI ran WITHOUT it, against
    gitleaks' bare default ruleset. The two disagree: locally 2 findings, in
    CI 4.
  * `.gitleaks-baseline.json` was generated locally, under the config. It
    therefore accepted the 2 findings a local scan sees and none of the 2 extra
    ones CI sees, so CI could never go green — and the failure looked like a
    finding rather than a config split, which is why it survived 14 runs.

The two CI-only findings are scrubber test fixtures (`sk-` toys sitting beside
`secret123` and `user:pass123@host.com`), not credentials. They are accepted in
the baseline as individual records.

These tests pin the property that made this invisible: the scanner must not be
able to behave differently for the person generating the baseline than for the
CI job checking it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / ".gitleaks-baseline.json"


@pytest.fixture(scope="module")
def baseline() -> list[dict]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_gitleaks_config_is_not_tracked():
    """A tracked config that CI cannot resolve is worse than none at all."""
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".gitleaks.toml"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert tracked.returncode != 0, (
        ".gitleaks.toml is tracked again. If it is a symlink to a machine-local "
        "path it dangles in CI, and local and CI scans diverge silently — the "
        "exact split that cost 14 unread failures. If a repo config is genuinely "
        "wanted, commit a REAL file whose contents CI can read, and update this test."
    )


def test_gitleaks_config_is_gitignored():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".gitleaks.toml"], cwd=REPO_ROOT, capture_output=True
    )
    assert ignored.returncode == 0, (
        ".gitleaks.toml is not gitignored, so the local symlink can be "
        "re-committed by an unrelated `git add -A`"
    )


def test_no_baseline_entry_carries_an_unredacted_secret(baseline):
    """The baseline is committed. A real value in it is a leak by definition."""
    for entry in baseline:
        assert entry.get("Secret") == "REDACTED", (
            f"{entry.get('File')}:{entry.get('StartLine')} has an unredacted "
            "Secret. Generate the baseline with --redact; committing the value "
            "turns the accept-list into the leak."
        )


def test_every_baseline_entry_is_traceable(baseline):
    for entry in baseline:
        for field in ("RuleID", "File", "Commit", "Fingerprint"):
            assert entry.get(field), f"baseline entry missing {field}"


def test_baseline_fingerprints_are_unique(baseline):
    fps = [e["Fingerprint"] for e in baseline]
    dupes = {f for f in fps if fps.count(f) > 1}
    assert not dupes, f"duplicate fingerprints {sorted(dupes)}"


def test_baseline_covers_the_scrubber_fixtures(baseline):
    """The two CI-only findings. If these vanish, the config split is back."""
    covered = {(e["File"], e["StartLine"]) for e in baseline}
    for line in (36, 51):
        assert ("tests/test_scrubber.py", line) in covered, (
            f"tests/test_scrubber.py:{line} is no longer accepted in the "
            "baseline. Either the fixture moved — regenerate the record — or "
            "the baseline was generated under a local config again, which is "
            "how CI went red for 14 runs."
        )

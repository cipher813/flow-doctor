"""Test suite-wide fixtures and environment setup."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _skip_flow_doctor_preflight(monkeypatch):
    """Skip notifier preflight network calls in tests.

    Tests construct GitHubNotifier with fake tokens ("test-token") and
    FlowDoctor.__init__ now invokes ``validate()`` on each notifier. That
    hits api.github.com with the fake token and returns 401, which the
    preflight treats as a hard auth failure. Setting
    FLOW_DOCTOR_SKIP_PREFLIGHT=1 bypasses the network call so tests
    exercise construction logic without external dependencies.

    Tests that want to exercise the preflight itself can unset or
    override the env var explicitly.
    """
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")

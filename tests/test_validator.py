"""Tests for test validator."""

import subprocess
from unittest.mock import patch, MagicMock

from flow_doctor.fix.validator import TestValidator


def test_run_passing_tests():
    validator = TestValidator()

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "3 passed"
    mock_result.stderr = ""

    with patch("flow_doctor.fix.validator.subprocess.run", return_value=mock_result):
        passed, output = validator.run("pytest", "/tmp/repo")

    assert passed is True
    assert "3 passed" in output


def test_run_failing_tests():
    validator = TestValidator()

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "1 failed, 2 passed"
    mock_result.stderr = "FAILED test_main.py::test_foo"

    with patch("flow_doctor.fix.validator.subprocess.run", return_value=mock_result):
        passed, output = validator.run("pytest", "/tmp/repo")

    assert passed is False
    assert "failed" in output


def test_run_timeout():
    validator = TestValidator()

    with patch(
        "flow_doctor.fix.validator.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=60),
    ):
        passed, output = validator.run("pytest", "/tmp/repo", timeout=60)

    assert passed is False
    assert "timed out" in output


def test_run_execution_error():
    validator = TestValidator()

    with patch(
        "flow_doctor.fix.validator.subprocess.run",
        side_effect=FileNotFoundError("command not found"),
    ):
        passed, output = validator.run("nonexistent", "/tmp/repo")

    assert passed is False
    assert "failed to execute" in output.lower()


def test_run_combines_stdout_stderr():
    validator = TestValidator()

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "output"
    mock_result.stderr = "warnings"

    with patch("flow_doctor.fix.validator.subprocess.run", return_value=mock_result):
        passed, output = validator.run("pytest", "/tmp/repo")

    assert passed is True
    assert "output" in output
    assert "warnings" in output

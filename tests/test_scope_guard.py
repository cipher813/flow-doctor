"""Tests for scope guard."""

from flow_doctor.fix.scope_guard import ScopeGuard


def test_empty_allow_permits_all():
    guard = ScopeGuard(allow=[], deny=[])
    passed, violations = guard.check(["any/file.py", "other.py"])
    assert passed is True
    assert violations == []


def test_allow_list_permits_matching():
    guard = ScopeGuard(allow=["src/*.py", "lib/*.py"], deny=[])
    passed, violations = guard.check(["src/main.py", "lib/utils.py"])
    assert passed is True


def test_allow_list_blocks_non_matching():
    guard = ScopeGuard(allow=["src/*.py"], deny=[])
    passed, violations = guard.check(["src/main.py", "other/file.py"])
    assert passed is False
    assert len(violations) == 1
    assert "other/file.py" in violations[0]


def test_deny_list_blocks_matching():
    guard = ScopeGuard(allow=[], deny=["config/*"])
    passed, violations = guard.check(["config/settings.py"])
    assert passed is False
    assert "deny list" in violations[0]


def test_deny_overrides_allow():
    guard = ScopeGuard(allow=["src/*"], deny=["src/secrets.py"])
    passed, violations = guard.check(["src/secrets.py"])
    assert passed is False


def test_prefix_matching_with_slash():
    guard = ScopeGuard(allow=["data/"], deny=[])
    passed, violations = guard.check(["data/scanner.py"])
    assert passed is True


def test_prefix_matching_without_slash():
    guard = ScopeGuard(allow=["data"], deny=[])
    passed, violations = guard.check(["data/scanner.py"])
    assert passed is True


def test_multiple_violations():
    guard = ScopeGuard(allow=["src/*.py"], deny=[])
    passed, violations = guard.check(["other/a.py", "other/b.py"])
    assert passed is False
    assert len(violations) == 2


def test_deny_prefix_matching():
    guard = ScopeGuard(allow=[], deny=["tests/"])
    passed, violations = guard.check(["tests/test_main.py"])
    assert passed is False

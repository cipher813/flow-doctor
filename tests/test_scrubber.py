"""Tests for secret scrubbing."""

from flow_doctor.core.scrubber import REDACTED, Scrubber


def test_scrub_aws_key():
    s = Scrubber()
    text = "key=AKIAIOSFODNN7EXAMPLE"
    result = s.scrub_string(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert REDACTED in result


def test_scrub_bearer_token():
    s = Scrubber()
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc"
    result = s.scrub_string(text)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
    assert f"Bearer {REDACTED}" in result


def test_scrub_password_in_url():
    s = Scrubber()
    text = "postgresql://admin:s3cretP4ss@localhost:5432/db"
    result = s.scrub_string(text)
    assert "s3cretP4ss" not in result


def test_scrub_env_vars():
    s = Scrubber()
    env = {
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "GMAIL_APP_PASSWORD": "my-secret-pass",
        "HOME": "/home/user",
        "MY_API_KEY": "sk-1234567890",
        "DATABASE_TOKEN": "tok-abc",
    }
    result = s.scrub_env_vars(env)
    assert result["AWS_ACCESS_KEY_ID"] == REDACTED
    assert result["AWS_SECRET_ACCESS_KEY"] == REDACTED
    assert result["GMAIL_APP_PASSWORD"] == REDACTED
    assert result["MY_API_KEY"] == REDACTED
    assert result["DATABASE_TOKEN"] == REDACTED
    assert result["HOME"] == "/home/user"  # not scrubbed


def test_scrub_dict():
    s = Scrubber()
    d = {
        "api_key": "sk-1234567890abcdef",
        "name": "test",
        "nested": {
            "password": "secret123",
            "url": "https://user:pass123@host.com/path",
        },
    }
    result = s.scrub_dict(d)
    assert result["api_key"] == REDACTED
    assert result["name"] == "test"
    assert result["nested"]["password"] == REDACTED


def test_scrub_custom_patterns():
    s = Scrubber(extra_patterns=[r"custom-secret-\d+"])
    text = "token=custom-secret-12345 and more text"
    result = s.scrub_string(text)
    assert "custom-secret-12345" not in result


def test_scrub_preserves_normal_text():
    s = Scrubber()
    text = "This is a normal error message with no secrets"
    result = s.scrub_string(text)
    assert result == text


def test_scrub_empty_string():
    s = Scrubber()
    assert s.scrub_string("") == ""
    assert s.scrub_string(None) is None

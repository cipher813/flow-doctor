"""Tests for configuration loading."""

import os
import tempfile

from flow_doctor.core.config import FlowDoctorConfig, load_config


def test_load_inline_config():
    config = load_config(
        flow_name="test-flow",
        repo="user/repo",
        owner="@user",
    )
    assert config.flow_name == "test-flow"
    assert config.repo == "user/repo"
    assert config.owner == "@user"


def test_load_yaml_config():
    yaml_content = """
flow_name: research-lambda
repo: user/alpha-engine-research
owner: "@user"
dedup_cooldown_minutes: 30
dependencies:
  - upstream-flow
store:
  type: sqlite
  path: /tmp/test_fd.db
rate_limits:
  max_alerts_per_day: 10
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(config_path=f.name)

    os.unlink(f.name)

    assert config.flow_name == "research-lambda"
    assert config.repo == "user/alpha-engine-research"
    assert config.dedup_cooldown_minutes == 30
    assert config.dependencies == ["upstream-flow"]
    assert config.store.type == "sqlite"
    assert config.store.path == "/tmp/test_fd.db"
    assert config.rate_limits.max_alerts_per_day == 10


def test_load_yaml_with_env_vars():
    os.environ["TEST_WEBHOOK_URL"] = "https://hooks.slack.com/test"
    yaml_content = """
flow_name: test
notify:
  - type: slack
    webhook_url: ${TEST_WEBHOOK_URL}
    channel: "#alerts"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(config_path=f.name)

    os.unlink(f.name)
    del os.environ["TEST_WEBHOOK_URL"]

    assert len(config.notify) == 1
    assert config.notify[0].webhook_url == "https://hooks.slack.com/test"
    assert config.notify[0].channel == "#alerts"


def test_inline_notify_shorthand():
    os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.com/test"
    config = load_config(
        flow_name="test",
        notify=["slack:#alpha-alerts", "email:user@example.com"],
    )
    del os.environ["SLACK_WEBHOOK_URL"]

    assert len(config.notify) == 2
    assert config.notify[0].type == "slack"
    assert config.notify[0].channel == "#alpha-alerts"
    assert config.notify[1].type == "email"
    assert config.notify[1].recipients == "user@example.com"


def test_store_string_sqlite():
    config = load_config(
        flow_name="test",
        store="sqlite:///tmp/test.db",
    )
    assert config.store.type == "sqlite"
    assert config.store.path == "/tmp/test.db"


def test_store_string_s3():
    config = load_config(
        flow_name="test",
        store="s3://my-bucket/flow-doctor/",
    )
    assert config.store.type == "s3"
    assert config.store.bucket == "my-bucket"
    assert config.store.prefix == "flow-doctor/"


def test_kwargs_override_yaml():
    yaml_content = """
flow_name: from-yaml
repo: yaml/repo
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_config(config_path=f.name, flow_name="from-kwargs")

    os.unlink(f.name)
    assert config.flow_name == "from-kwargs"


def test_default_config():
    config = load_config()
    assert config.flow_name == "default"
    assert config.store.type == "sqlite"
    assert config.rate_limits.max_diagnosed_per_day == 3
    assert config.dedup_cooldown_minutes == 60

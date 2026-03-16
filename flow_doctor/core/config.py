"""Configuration: YAML file + inline Python kwargs."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class NotifyChannelConfig:
    type: str  # "slack" or "email"
    # Slack fields
    webhook_url: Optional[str] = None
    channel: Optional[str] = None
    # Email fields
    sender: Optional[str] = None
    recipients: Optional[str] = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_password: Optional[str] = None


@dataclass
class StoreConfig:
    type: str = "sqlite"
    path: str = "/tmp/flow_doctor.db"
    bucket: Optional[str] = None
    prefix: Optional[str] = None


@dataclass
class RateLimitConfig:
    max_diagnosed_per_day: int = 3
    max_issues_per_day: int = 3
    max_alerts_per_day: int = 5
    daily_digest: bool = True
    digest_time: str = "17:00"
    dedup_cooldown_minutes: int = 60


@dataclass
class FlowDoctorConfig:
    flow_name: str = "default"
    repo: Optional[str] = None
    owner: Optional[str] = None
    notify: List[NotifyChannelConfig] = field(default_factory=list)
    store: StoreConfig = field(default_factory=StoreConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    dependencies: List[str] = field(default_factory=list)
    dedup_cooldown_minutes: int = 60
    # Phase 2+ placeholders in config are ignored
    extra: Dict[str, Any] = field(default_factory=dict)


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} with the environment variable value."""
    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))
    return _ENV_VAR_RE.sub(_replacer, value)


def _resolve_dict(d: Any) -> Any:
    """Recursively resolve env vars in a dict/list/string."""
    if isinstance(d, str):
        return _resolve_env_vars(d)
    if isinstance(d, dict):
        return {k: _resolve_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_resolve_dict(item) for item in d]
    return d


def _parse_notify_shorthand(items: List[str]) -> List[NotifyChannelConfig]:
    """Parse shorthand notify list like ['slack:#channel', 'email:addr']."""
    configs = []
    for item in items:
        if item.startswith("slack:"):
            channel = item[len("slack:"):]
            configs.append(NotifyChannelConfig(
                type="slack",
                channel=channel,
                webhook_url=os.environ.get("SLACK_WEBHOOK_URL"),
            ))
        elif item.startswith("email:"):
            addr = item[len("email:"):]
            configs.append(NotifyChannelConfig(
                type="email",
                sender=os.environ.get("EMAIL_SENDER", addr),
                recipients=addr,
                smtp_host="smtp.gmail.com",
                smtp_password=os.environ.get("GMAIL_APP_PASSWORD"),
            ))
        else:
            configs.append(NotifyChannelConfig(type=item))
    return configs


def _parse_notify_dicts(items: List[Dict]) -> List[NotifyChannelConfig]:
    """Parse YAML notify list of dicts."""
    configs = []
    for item in items:
        item = _resolve_dict(item)
        configs.append(NotifyChannelConfig(
            type=item.get("type", "slack"),
            webhook_url=item.get("webhook_url"),
            channel=item.get("channel"),
            sender=item.get("sender"),
            recipients=item.get("recipients"),
            smtp_host=item.get("smtp_host", "smtp.gmail.com"),
            smtp_port=item.get("smtp_port", 587),
            smtp_password=item.get("smtp_password"),
        ))
    return configs


def _parse_store(raw: Any) -> StoreConfig:
    """Parse store config from string or dict."""
    if raw is None:
        return StoreConfig()
    if isinstance(raw, str):
        raw = _resolve_env_vars(raw)
        if raw.startswith("sqlite://"):
            return StoreConfig(type="sqlite", path=raw[len("sqlite://"):])
        if raw.startswith("s3://"):
            parts = raw[len("s3://"):].split("/", 1)
            return StoreConfig(type="s3", bucket=parts[0], prefix=parts[1] if len(parts) > 1 else "")
        return StoreConfig(type="sqlite", path=raw)
    if isinstance(raw, dict):
        raw = _resolve_dict(raw)
        return StoreConfig(
            type=raw.get("type", "sqlite"),
            path=raw.get("path", "/tmp/flow_doctor.db"),
            bucket=raw.get("bucket"),
            prefix=raw.get("prefix"),
        )
    return StoreConfig()


def load_config(
    config_path: Optional[str] = None,
    **kwargs: Any,
) -> FlowDoctorConfig:
    """Load config from YAML file, inline kwargs, or both (kwargs override YAML)."""
    raw: Dict[str, Any] = {}

    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            raw = _resolve_dict(raw)

    # Merge inline kwargs (they override YAML values)
    for k, v in kwargs.items():
        if v is not None:
            raw[k] = v

    # Parse notify
    notify_raw = raw.get("notify", [])
    if isinstance(notify_raw, list):
        if notify_raw and isinstance(notify_raw[0], str):
            notify = _parse_notify_shorthand(notify_raw)
        elif notify_raw and isinstance(notify_raw[0], dict):
            notify = _parse_notify_dicts(notify_raw)
        else:
            notify = []
    else:
        notify = []

    # Parse store
    store = _parse_store(raw.get("store"))

    # Parse rate limits
    rl_raw = raw.get("rate_limits", {})
    rate_limits = RateLimitConfig(
        max_diagnosed_per_day=rl_raw.get("max_diagnosed_per_day", 3),
        max_issues_per_day=rl_raw.get("max_issues_per_day", 3),
        max_alerts_per_day=rl_raw.get("max_alerts_per_day", 5),
        daily_digest=rl_raw.get("daily_digest", True),
        digest_time=rl_raw.get("digest_time", "17:00"),
        dedup_cooldown_minutes=rl_raw.get("dedup_cooldown_minutes",
                                           raw.get("dedup_cooldown_minutes", 60)),
    )

    dedup_cooldown = raw.get("dedup_cooldown_minutes", rate_limits.dedup_cooldown_minutes)

    return FlowDoctorConfig(
        flow_name=raw.get("flow_name", "default"),
        repo=raw.get("repo"),
        owner=raw.get("owner"),
        notify=notify,
        store=store,
        rate_limits=rate_limits,
        dependencies=raw.get("dependencies", []),
        dedup_cooldown_minutes=dedup_cooldown,
    )

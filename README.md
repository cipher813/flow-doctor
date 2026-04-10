# Flow Doctor

[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-243_passing-brightgreen.svg)]()
[![Coverage](https://img.shields.io/badge/coverage-81%25-brightgreen.svg)]()
[![PyPI](https://img.shields.io/badge/PyPI-v0.2.0-blue.svg)](https://pypi.org/project/flow-doctor/)

Pipeline error handler for Python. Captures exceptions, diagnoses root causes with LLMs, files GitHub issues, and generates fix PRs.

**Fail-loud by default (v0.2.0+).** Configuration errors — missing tokens, unresolved `${VAR}` references, misconfigured notifiers — raise `ConfigError` at `init()` time instead of silently degrading. Silent degradation means users discover broken error monitoring only during an actual incident, which defeats the purpose.

```python
import logging
import flow_doctor

# Zero config file: read everything from FLOW_DOCTOR_* env vars
fd = flow_doctor.init()

handler = flow_doctor.FlowDoctorHandler(fd, level=logging.WARNING)
logging.getLogger().addHandler(handler)

# Every WARNING+ log is now captured, deduplicated, diagnosed, and routed.
```

## How It Works

```
Exception → Capture → Dedup → Diagnose (LLM) → GitHub Issue → Fix PR
```

1. **Capture** — exception, traceback, logs, and runtime context
2. **Dedup** — same error signature within cooldown window is suppressed
3. **Cascade** — if a declared upstream dependency also failed, tag it and skip diagnosis
4. **Diagnose** — check the knowledge base (free), then call Claude if rate limit allows
5. **Notify** — file a GitHub issue, send Slack/email (rate-limited with daily digest fallback)
6. **Fix** — human adds `flow-doctor:fix` label on the issue, triggering automated fix PR generation

## Installation

```bash
pip install flow-doctor                          # core only
pip install "flow-doctor[diagnosis]"             # + LLM diagnosis (anthropic SDK)
pip install "flow-doctor[diagnosis,remediation]" # + auto-remediation (boto3 for SSM/Step Functions)
pip install "flow-doctor[all]"                   # everything
```

## Quick Start

### Option 1: Logging handler (recommended)

Attach to Python's logging system. Zero changes at call sites — any `WARNING+` log triggers the full pipeline.

```python
import logging
import flow_doctor

fd = flow_doctor.init(config_path="flow-doctor.yaml")
handler = flow_doctor.FlowDoctorHandler(fd, level=logging.WARNING)
logging.getLogger().addHandler(handler)

# These now trigger dedup, diagnosis, and notifications automatically:
logger.warning("Upstream data is 48h stale")
logger.error("S3 backup failed: AccessDenied")
logger.exception("Pipeline crashed")
```

The handler is **non-blocking** — `emit()` enqueues work and returns immediately. A background thread calls `fd.report()` asynchronously.

### Option 2: Direct reporting

```python
fd = flow_doctor.init(config_path="flow-doctor.yaml")

try:
    run_pipeline()
except Exception as e:
    fd.report(e)  # never crashes the caller
```

### Option 3: Context manager / decorator

```python
with fd.guard():
    run_pipeline()  # exceptions are reported and re-raised

@fd.monitor
def handler(event, context):
    run_pipeline()
```

### Log capture

Attach recent logs to the next error report for richer diagnosis context:

```python
with fd.capture_logs(level=logging.INFO):
    logger.info("Starting scan with 900 tickers...")
    run_pipeline()
    # All captured logs are attached to the next fd.report() call
```

## Configuration

Create a `flow-doctor.yaml` in your project root:

```yaml
flow_name: my-pipeline
repo: owner/repo

notify:
  - type: github
    repo: owner/repo
  - type: email
    sender: alerts@example.com
    recipients: oncall@example.com

store:
  type: sqlite
  path: flow_doctor.db

diagnosis:
  enabled: true
  model: claude-sonnet-4-6-20250514
  api_key: ${ANTHROPIC_API_KEY}
  timeout_seconds: 30
  max_daily_cost_usd: 1.00

github:
  token: ${GITHUB_TOKEN}
  labels: [flow-doctor]

rate_limits:
  max_diagnosed_per_day: 3
  max_issues_per_day: 3
  dedup_cooldown_minutes: 60

dependencies:
  - upstream-pipeline

remediation:
  enabled: true
  dry_run: true
  auto_remediate_min_confidence: 0.9
  market_hours_lockout: false

auto_fix:
  enabled: true
  confidence_threshold: 0.90
  test_command: "python -m pytest tests/ -x -q"
  scope:
    allow: ["src/", "lib/"]
    deny: ["*.yaml", "*.yml"]
```

Environment variables in `${VAR}` syntax are resolved at load time. **Unresolved references raise `ConfigError`** — no silent passthrough where `${MISSING_VAR}` ends up being used as a literal token.

Inline configuration (no YAML file):

```python
fd = flow_doctor.init(
    flow_name="my-pipeline",
    repo="owner/repo",
    store={"type": "sqlite", "path": "flow_doctor.db"},
    notify=["github:owner/repo"],
)
```

## Environment Variables

flow-doctor reads credentials from environment variables as its primary configuration mechanism. Every notifier has a documented fallback chain: config → `FLOW_DOCTOR_*` canonical name → common conventions. This lets the same code work across `export`-in-shell, systemd `EnvironmentFile=`, Docker `--env`, Kubernetes Secrets, CI runners, Render/Fly.io/Heroku, and everything else, without touching a file.

### Canonical contract

| Variable | Used by | Fallback chain | Required when |
|---|---|---|---|
| `FLOW_DOCTOR_GITHUB_TOKEN` | GitHub notifier, auto-fix PR creator | `FLOW_DOCTOR_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN` | Any GitHub notifier or auto-fix is configured |
| `FLOW_DOCTOR_GITHUB_REPO` | GitHub notifier | `FLOW_DOCTOR_GITHUB_REPO` | GitHub notifier config has no explicit `repo` field |
| `FLOW_DOCTOR_SMTP_PASSWORD` | Email notifier | `FLOW_DOCTOR_SMTP_PASSWORD` → `GMAIL_APP_PASSWORD` | SMTP requires auth |
| `FLOW_DOCTOR_SMTP_SENDER` | Email notifier | `FLOW_DOCTOR_SMTP_SENDER` → `EMAIL_SENDER` | Email notifier config has no explicit `sender` field |
| `FLOW_DOCTOR_SMTP_RECIPIENTS` | Email notifier | `FLOW_DOCTOR_SMTP_RECIPIENTS` → `EMAIL_RECIPIENTS` | Email notifier config has no explicit `recipients` field |
| `FLOW_DOCTOR_SLACK_WEBHOOK` | Slack notifier | `FLOW_DOCTOR_SLACK_WEBHOOK` → `SLACK_WEBHOOK_URL` | Slack notifier config has no explicit `webhook_url` field |
| `FLOW_DOCTOR_ANTHROPIC_API_KEY` | LLM diagnosis, auto-fix generator | `FLOW_DOCTOR_ANTHROPIC_API_KEY` → `ANTHROPIC_API_KEY` | `diagnosis.enabled: true` or auto-fix is on |

**Precedence** for every field is: explicit value in YAML/kwargs → canonical `FLOW_DOCTOR_*` env var → convention fallbacks in the order listed. The first non-empty value wins. Missing values raise `ConfigError` at `init()` time naming the specific field and the env vars that would satisfy it.

### Env-var-only quickstart

For the minimum possible setup, create a GitHub PAT with `Issues: Read and write`, then:

```bash
export FLOW_DOCTOR_GITHUB_REPO=myorg/myrepo
export FLOW_DOCTOR_GITHUB_TOKEN=github_pat_...
```

```python
import flow_doctor

fd = flow_doctor.init(
    flow_name="my-pipeline",
    notify=[{"type": "github"}],
)

try:
    risky_thing()
except Exception as e:
    fd.report(e)
```

Two env vars, four lines of Python, working GitHub issues on the next exception. No YAML file required. The GitHub notifier's `repo` and `token` both resolve from the env.

### Strict mode and degraded mode

`flow_doctor.init()` defaults to `strict=True`. Any configuration error (missing required field, unresolved `${VAR}`, unknown notifier type) raises `ConfigError` and prevents startup. This is the recommended default — a non-running flow-doctor is a loud failure; a silently-degraded flow-doctor is a silent one.

If you genuinely want best-effort init that logs errors but keeps running with no notifiers, opt in explicitly:

```python
fd = flow_doctor.init(strict=False)  # degraded mode — use with caution
```

## Features

### Error Capture and Dedup

- Traceback extraction with frame-based signature hashing
- Configurable cooldown window (default 60 min) — same error is captured once, not spammed
- Cascade detection tags downstream failures caused by upstream dependency outages
- Automatic secret scrubbing (AWS keys, Bearer tokens, passwords in URLs)

### LLM Diagnosis

- Structured root cause analysis via Claude: category, confidence, affected files, remediation
- Six categories: `TRANSIENT`, `DATA`, `CODE`, `CONFIG`, `EXTERNAL`, `INFRA`
- Knowledge base caching — known patterns are matched for free before calling the LLM
- Git context assembly (recent commits, changed files) for better diagnosis accuracy
- Daily cost cap (default $1.00) and rate limiting (default 3 diagnoses/day)

### GitHub Issues

- Auto-filed with diagnosis, traceback, and captured logs
- Machine-readable metadata embedded in HTML comments for downstream automation
- Rate-limited with graceful degradation to daily digest

### Auto-Fix PRs

Human-in-the-loop: a human reviews the diagnosis, adds a `flow-doctor:fix` label, and a GitHub Actions workflow generates a validated fix PR.

1. An error occurs and Flow Doctor creates a GitHub issue with structured diagnosis
2. A human reviews the diagnosis and adds the `flow-doctor:fix` label
3. GitHub Actions triggers `flow-doctor generate-fix`
4. The CLI generates a diff via LLM, validates against scope rules, runs tests
5. If tests pass, a PR is opened. If tests fail, a comment explains what went wrong.

**Safety gates** — fix generation is skipped when:
- Confidence below threshold (default 90%)
- Category is `EXTERNAL` or `INFRA` (nothing to fix in code)
- Config issue involves credentials/secrets
- Generated diff touches files outside configured scope
- Tests fail after applying the fix

### Remediation Playbooks

Define patterns that map failure signatures to automated actions:

```python
from flow_doctor.remediation.playbook import Playbook, PlaybookPattern, RemediationAction, RemediationType

my_playbook = Playbook(patterns=[
    PlaybookPattern(
        name="service_down",
        description="App service not responding",
        category="INFRA",
        message_pattern=r"(connection refused|service unavailable)",
        action=RemediationAction(
            action_type=RemediationType.RESTART_SERVICE,
            description="Restart the app service",
            commands=["sudo systemctl restart myapp"],
            ssm_target="app-server",
        ),
    ),
])
```

### Notifications

- **GitHub issues** — primary notification with full diagnosis
- **Slack** — webhook-based alerts with severity emoji and diagnosis snippet
- **Email** — SMTP with detailed body (traceback, diagnosis, affected files)
- **Daily digest** — summarizes rate-limited/suppressed errors at end of day

## Auto-Fix CLI

```bash
flow-doctor generate-fix \
  --issue-number 42 \
  --repo owner/repo \
  --token $GITHUB_TOKEN \
  --config flow-doctor.yaml \
  --dry-run
```

GitHub Actions workflow (copy to your repo at `.github/workflows/flow-doctor-fix.yml`):

```yaml
name: Flow Doctor Fix
on:
  issues:
    types: [labeled]
jobs:
  generate-fix:
    if: github.event.label.name == 'flow-doctor:fix'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install flow-doctor[diagnosis]
      - run: |
          python -m flow_doctor.fix.cli generate-fix \
            --issue-number ${{ github.event.issue.number }} \
            --repo ${{ github.repository }} \
            --token $GITHUB_TOKEN
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Architecture

```
flow_doctor/
  core/           # Client, config, models, dedup, rate limiting, scrubber, logging handler
  diagnosis/      # LLM provider, context assembly, knowledge base, git context
  digest/         # Daily digest generator
  fix/            # Auto-fix: LLM generator, scope guard, test validator, PR creator, CLI
  notify/         # Slack, email, GitHub issue backends
  remediation/    # Decision gate, executor, playbook patterns
  storage/        # SQLite backend (thread-safe, per-thread connections)
```

## Development

```bash
git clone https://github.com/cipher813/flow-doctor.git
cd flow-doctor
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m pytest tests/ -x -q        # 212 tests
python -m pytest tests/ --cov=flow_doctor  # coverage report
python examples/smoke_test.py         # end-to-end smoke test
```

## License

[MIT](LICENSE)

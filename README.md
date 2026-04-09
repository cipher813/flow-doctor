# Flow Doctor

Call-site error handler for Python pipelines. Catch exceptions, diagnose root causes with LLMs, and open fix PRs — all from a single `report()` call.

```python
import flow_doctor

fd = flow_doctor.init(config_path="flow-doctor.yaml")

try:
    run_pipeline()
except Exception as e:
    fd.report(e)
```

Flow Doctor captures the error, deduplicates it, runs LLM diagnosis, files a GitHub issue, and (with human approval) generates a fix PR. Your code never crashes — `report()` swallows its own failures silently.

## Features

- **Error capture** with traceback, logs, and runtime context
- **Deduplication** — same error signature within a cooldown window is suppressed
- **Cascade detection** — downstream failures linked to upstream are tagged, not re-diagnosed
- **Rate limiting** with graceful degradation (full diagnosis > log-only > daily digest)
- **Secret scrubbing** — AWS keys, tokens, and passwords are redacted automatically
- **LLM diagnosis** — structured root cause analysis via Claude (category, confidence, affected files, remediation)
- **GitHub issues** — auto-filed with diagnosis, traceback, and machine-readable metadata
- **Auto-fix PRs** — human-in-the-loop: add a `flow-doctor:fix` label to an issue, and a GitHub Actions workflow generates a validated fix PR
- **Logging handler** — `FlowDoctorHandler` integrates with Python's `logging` module for zero-change-at-call-site adoption
- **Notifications** — Slack, email, and GitHub issue backends
- **Daily digest** — summarizes suppressed errors at end of day

## Installation

```bash
pip install -e .
```

With optional dependencies:

```bash
pip install -e ".[diagnosis]"   # LLM diagnosis (anthropic SDK)
pip install -e ".[slack]"       # Slack notifications (requests)
pip install -e ".[dev]"         # Development (pytest + all optional deps)
```

## Configuration

Create a `flow-doctor.yaml` in your project root:

```yaml
flow_name: research-lambda
repo: owner/repo

notify:
  - type: github
    repo: owner/repo

store:
  type: sqlite
  path: flow_doctor.db

diagnosis:
  enabled: true
  model: claude-sonnet-4-6-20250514
  api_key: ${ANTHROPIC_API_KEY}

github:
  token: ${GITHUB_TOKEN}

rate_limits:
  max_diagnosed_per_day: 3
  max_issues_per_day: 3
  max_alerts_per_day: 5
  dedup_cooldown_minutes: 60

dependencies:
  - upstream-flow-name

auto_fix:
  enabled: true
  confidence_threshold: 0.90
  test_command: "python -m pytest tests/ -x -q"
  dry_run: false
  scope:
    allow:
      - "flow_doctor/"
      - "src/"
    deny:
      - "*.yaml"
      - "*.yml"
```

Environment variables in `${VAR}` syntax are resolved at load time.

You can also configure inline without a YAML file:

```python
fd = flow_doctor.init(
    flow_name="my-pipeline",
    repo="owner/repo",
    store="sqlite://flow_doctor.db",
    notify=["slack:#alerts", "email:oncall@example.com"],
)
```

## Usage

### Basic error reporting

```python
fd = flow_doctor.init(config_path="flow-doctor.yaml")

# Report an exception
try:
    run_pipeline()
except Exception as e:
    fd.report(e)

# Report with extra context
fd.report(error, context={"ticker": "AAPL", "stage": "scanner"})

# Report a warning (no alerts sent)
fd.report("Scanner returned 0 candidates", severity="warning")
```

### Context manager

```python
with fd.guard():
    run_pipeline()  # exceptions are reported and re-raised
```

### Decorator

```python
@fd.monitor
def handler(event, context):
    run_pipeline()
```

### Log capture

```python
with fd.capture_logs(level=logging.INFO):
    logger.info("Starting scan...")
    run_pipeline()
    # All logs are attached to the next report
```

### Logging handler

Automatically route `logger.error()` and `logger.exception()` calls through Flow Doctor's full pipeline — no `fd.report()` calls needed at each site:

```python
import logging

fd = flow_doctor.init(config_path="flow-doctor.yaml")

# Attach to any logger
handler = fd.get_handler(level=logging.ERROR)
logging.getLogger().addHandler(handler)

# Now any ERROR+ log triggers dedup, diagnosis, and notifications
logger = logging.getLogger("my_pipeline")
logger.error("Scanner returned 0 candidates")  # → Flow Doctor report

try:
    run_pipeline()
except Exception:
    logger.exception("Pipeline failed")  # → report with full exception info
```

You can also construct the handler directly for more control:

```python
from flow_doctor import FlowDoctorHandler

handler = FlowDoctorHandler(
    fd,
    level=logging.ERROR,
    exclude_patterns=[r"^Connection reset"],  # skip noisy errors
    include_patterns=[r"CRITICAL"],           # allowlist (if set, only matching pass)
    queue_size=200,
)
```

The handler is **non-blocking** — `emit()` enqueues work and returns immediately. A background daemon thread calls `fd.report()` asynchronously. If the queue fills up, messages are dropped silently rather than blocking the caller.

Configure defaults via YAML:

```yaml
handler:
  level: ERROR
  exclude_patterns:
    - "^Connection reset"
  queue_size: 100
```

Call `handler.shutdown()` or `handler.close()` at process exit for graceful drain.

### History and digest

```python
# Recent reports
for report in fd.history(limit=10):
    print(f"[{report.severity}] {report.error_message}")

# Daily digest (generates + sends via configured notifiers)
fd.digest()
```

## Auto-Fix PR Generation

Flow Doctor can generate fix PRs for diagnosed issues, gated by human approval.

### How it works

1. An error occurs and Flow Doctor creates a GitHub issue with a structured diagnosis
2. A human reviews the diagnosis on the issue
3. If the diagnosis looks right, the human adds a `flow-doctor:fix` label
4. A GitHub Actions workflow triggers and runs the fix CLI
5. The CLI generates a diff via LLM, validates it against scope rules, runs tests, and opens a PR

### Setup

Copy the example workflow to your repo:

```bash
cp .github/workflows/flow-doctor-fix.yml <your-repo>/.github/workflows/
```

Required secrets in your GitHub repo:
- `ANTHROPIC_API_KEY` — for LLM fix generation
- `GITHUB_TOKEN` — automatically available in GitHub Actions

### CLI

The fix CLI can also be run manually:

```bash
flow-doctor generate-fix \
  --issue-number 42 \
  --repo owner/repo \
  --token $GITHUB_TOKEN \
  --config flow-doctor.yaml \
  --dry-run
```

### Safety gates

Fix generation is skipped when:
- Diagnosis confidence is below the configured threshold (default: 90%)
- Category is `EXTERNAL` or `INFRA` (nothing to fix in code)
- Category is `CONFIG` with credential/secret-related root cause
- No affected files are specified in the diagnosis
- Generated diff touches files outside the configured scope (allow/deny lists)
- Tests fail after applying the fix

## Architecture

```
flow_doctor/
  core/         # Client, config, models, dedup, rate limiting, scrubber
  diagnosis/    # LLM diagnosis provider, context assembly, knowledge base
  digest/       # Daily digest generator
  fix/          # Auto-fix: generator, scope guard, validator, PR creator, CLI
  notify/       # Slack, email, GitHub issue backends
  storage/      # SQLite storage backend
```

### Core loop

```
Catch -> Dedup -> Cascade check -> Diagnose -> Notify -> (human) -> Fix
```

1. **Catch** — `report()` captures the exception, traceback, and context
2. **Dedup** — Same error signature within the cooldown window is suppressed
3. **Cascade** — If a declared dependency also failed recently, tag it and skip diagnosis
4. **Diagnose** — Check the knowledge base first (free), then call the LLM if rate limit allows
5. **Notify** — File a GitHub issue, send Slack/email alerts (rate-limited with digest fallback)
6. **Fix** — Human adds `flow-doctor:fix` label, triggering automated fix generation

## Development

```bash
# Setup
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -x -q

# Run smoke test
python examples/smoke_test.py
```

## License

MIT

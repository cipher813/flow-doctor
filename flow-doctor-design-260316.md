# Flow Doctor — Design Document

**Date:** 2026-03-16
**Author:** Brian McMahon
**Status:** Draft

---

## 1. Vision

Flow Doctor is a **call-site error handler** — a Python library that your application invokes directly when something goes wrong. There is no polling daemon, no sensor system, no separate scheduler. Your code catches an exception, hands it to Flow Doctor with context, and Flow Doctor diagnoses the failure, notifies the owner, and (when confident) opens a fix PR.

```python
try:
    run_research_pipeline()
except Exception as e:
    flow_doctor.report(e)   # that's it
```

It ships as a **pip-installable Python package** with an optional REST API for non-Python callers. Designed around Alpha Engine's 6 flows but generalizable to any scheduled job, Lambda, or CI pipeline.

**Design-first use case:** Alpha Engine (6 interconnected Lambda/EC2 flows on AWS).

### Core Loop

```
Catch → Diagnose → Act → Notify
```

1. **Catch** — The application catches an exception and calls `flow_doctor.report()` with the error, traceback, and any available context (logs, runtime state).
2. **Diagnose** — Flow Doctor assembles context (error + logs + recent git changes + known patterns), sends it to an LLM, and produces a structured root cause analysis.
3. **Act** — Based on diagnosis confidence and flow configuration: file a GitHub issue, open a fix PR, or just log it.
4. **Notify** — Alert the flow owner via configured channel (Slack, email, GitHub @mention).

### Why Inline, Not Polling

| Concern | Polling/Sensor Model | Inline Invocation Model |
|---|---|---|
| Failure detection | Inferred from missing artifacts or log patterns — indirect, prone to false positives | The application *tells* Flow Doctor it failed — zero ambiguity |
| Context quality | Must reconstruct what happened from external logs | Has direct access to the exception, traceback, local variables, and runtime state |
| Timing | Detects failure after a deadline passes — always delayed | Fires immediately at the point of failure |
| Infrastructure | Requires a running daemon/Lambda + scheduler | Zero infrastructure — it's a library call |
| "Did the job run?" | Hard to distinguish "didn't run" from "ran and succeeded silently" | Not Flow Doctor's job — use CloudWatch/cron alerting for missing runs. Flow Doctor handles *failures*, not *absences* |

**Trade-off:** The inline model doesn't detect jobs that *fail to start* (e.g., Lambda not triggered, cron not running). That's a different class of problem best solved by existing tools (CloudWatch missing-alarm, Healthchecks.io dead-man switch). Flow Doctor focuses on the higher-value problem: the job ran, it broke, now what?

### Design Principle: Flows Always Run

Flow Doctor is an error handler, not an orchestrator. It never prevents a downstream flow from running. AE's flows are designed to degrade gracefully on stale inputs — Predictor reads the latest available signals (not necessarily today's), Executor falls back to prior day's data, Backtester works with whatever history exists.

When an upstream flow fails and a downstream flow subsequently fails due to stale/missing input, Flow Doctor recognizes the cascade via dependency tagging and avoids wasting diagnosis budget on the downstream symptom. But it never kills or gates the downstream flow — that's the application's decision.

---

## 2. Rate Limiting + Cost Control

Rate limiting is a first-class concern, not an afterthought. AE is still maturing and may produce bursts of errors. Without limits, a bad Monday could burn through LLM budget on repeated diagnoses of the same root issue.

### Tiered Degradation

Rather than a hard cutoff that goes silent, Flow Doctor degrades gracefully:

```
Reports 1–N:     Full pipeline: diagnose + issue/PR + alert     (Phase 2/3)
Reports N+1...:  Log to event store + queue for daily digest     (Phase 1 only)
```

After the ceiling, Flow Doctor still captures every error (visibility is never lost), but skips the expensive actions (LLM calls, GitHub issues, individual alerts). A single daily digest summarizes what was suppressed.

### Configuration

```yaml
rate_limits:
  max_diagnosed_per_day: 3          # LLM diagnosis calls (the expensive part)
  max_issues_per_day: 3             # GitHub issues/PRs created
  max_alerts_per_day: 5             # individual Slack/email alerts
  daily_digest: true                # summarize suppressed reports at EOD
  digest_time: "17:00"              # PT — after AE's EOD emailer
  dedup_cooldown_minutes: 60        # same error signature suppressed for 1hr
```

### Budget Impact (Alpha Engine)

| Scenario | Diagnosed | LLM cost/day | GitHub issues |
|---|---|---|---|
| Good day (0–1 failures) | 0–1 | $0.00–0.15 | 0–1 |
| Bad day (3+ failures) | 3 (capped) | ~$0.45 | 3 (capped) |
| Worst case (cascade Monday) | 3 (capped) | ~$0.45 | 3 (capped) |
| Monthly worst case | — | ~$10 | ~60 |

### Cascade-Aware Budget Allocation

Dependency-linked failures don't consume diagnosis slots. When Predictor Training reports a failure and its declared dependency `research-lambda` also failed within the same cycle (configurable window, default: 4 hours):

1. The downstream report is tagged `cascade: research-lambda`
2. It is **not counted** against `max_diagnosed_per_day`
3. It gets a brief alert: "predictor-training failed — likely caused by upstream research-lambda failure (see report #01HXY)"
4. No diagnosis, no issue, no PR — the upstream report gets the full treatment

This means a Monday cascade (Research → Predictor Training → Backtester) consumes **1 diagnosis slot**, not 3.

### Implementation

```python
class RateLimiter:
    def __init__(self, store, config):
        self.store = store
        self.limits = {
            "diagnosis":    config.get("max_diagnosed_per_day", 3),
            "github_issue": config.get("max_issues_per_day", 3),
            "github_pr":    config.get("max_issues_per_day", 3),
            "slack_alert":  config.get("max_alerts_per_day", 5),
            "email_alert":  config.get("max_alerts_per_day", 5),
        }

    def check(self, action: str) -> str:
        """Returns 'allow' or 'degrade'."""
        today_count = self.store.count_actions_today(action)
        if today_count < self.limits.get(action, 10):
            return "allow"
        return "degrade"  # log + digest, skip expensive action
```

The report flow integrates rate limiting after dedup, before diagnosis:

```
fd.report(exception)
    │
    ├── Capture error + context              ← always
    ├── Dedup check                          ← always
    ├── Check cascade (dependency failed?)   ← always
    │   └── YES → tag as cascade, brief alert, DONE (no budget consumed)
    ├── Persist to event store               ← always
    │
    ├── rate_limiter.check("diagnosis")
    │   ├── allow   → run LLM diagnosis (or knowledge base)
    │   └── degrade → skip, queue for daily digest
    │
    ├── rate_limiter.check("slack_alert")
    │   ├── allow   → send alert (with diagnosis if available)
    │   └── degrade → queue for daily digest
    │
    ├── rate_limiter.check("github_issue")
    │   ├── allow   → create issue/PR
    │   └── degrade → skip, include in digest
    │
    └── Daily digest (at digest_time): summarize all degraded reports
```

---

## 3. Phased Delivery

### Phase 1 — Report + Alert

**Goal:** Structured error capture and notification. No LLM. The `report()` call captures the error, enriches it with context, persists it, and sends an alert. This alone replaces ad-hoc error emails and "check the logs" messages.

#### Functionality

| Component | Description |
|---|---|
| **Flow Registry** | Declarative config (YAML or Python) defining each flow: name, repo, owner, notification channels, auto-fix scope, dependencies |
| **Error Capture** | Extracts exception type, message, full traceback, and optionally local variables from the call site. Supports both `report(exception)` and `report(error_message, logs=...)` for non-exception failures |
| **Context Enrichment** | Automatically attaches: Python version, OS, environment variables (scrubbed), installed package versions, flow config, timestamp, run ID |
| **Log Buffer** | Optional in-process log capture. Flow Doctor can install a logging handler at flow start that buffers log records, so the full log is available at report time without external log retrieval |
| **Dependency Tagging** | When reporting, a flow can tag its upstream dependencies. If the upstream also reported a failure in the same cycle, Flow Doctor links them and suppresses duplicate alerts |
| **Alert Dispatcher** | Pluggable notification backends: Slack webhook, email (SMTP/SES), GitHub issue (diagnosis-free — just the error + traceback). Configurable per-flow |
| **Event Store** | Persists all reports to SQLite (local) or S3 (Lambda). Append-only. Powers the history API and the knowledge base in Phase 2 |
| **Deduplication** | Same error signature (exception type + top 3 stack frames) within a configurable cooldown window (default: 1 hour) → suppress duplicate alerts, increment counter |

#### Python SDK — Phase 1

```python
import flow_doctor

# Initialize once at app startup (or Lambda cold start)
fd = flow_doctor.init(
    config_path="flow-doctor.yaml",       # or configure inline
    # -- or inline config: --
    flow_name="research-lambda",
    repo="brianmcmahon/alpha-engine-research",
    owner="@brianmcmahon",
    notify=["slack:#alpha-alerts", "email:brian@example.com"],
    store="sqlite:///tmp/flow_doctor.db",  # or "s3://bucket/flow-doctor/"
)

# Option A: explicit report in except block
try:
    run_research_pipeline()
except Exception as e:
    fd.report(e)                          # captures traceback automatically
    raise                                 # re-raise if the caller needs it

# Option B: context manager (captures any unhandled exception)
with fd.guard():
    run_research_pipeline()

# Option C: decorator
@fd.monitor
def handler(event, context):
    """Lambda handler — any exception auto-reported."""
    return run_research_pipeline(event)

# Option D: report a non-exception failure (e.g., empty result set)
if not signals:
    fd.report(
        "Research pipeline produced 0 signals",
        severity="warning",               # vs "error" (default) or "critical"
        context={"tickers_scanned": 900, "candidates_found": 0},
    )

# Optional: structured log capture
with fd.capture_logs():                   # installs a logging.Handler
    logger.info("Starting scanner...")
    # ... all log records buffered and attached to any subsequent report()
```

#### Alpha Engine Integration Points

| Flow | Integration Method | Notes |
|---|---|---|
| Research Lambda | `@fd.monitor` decorator on `lambda/handler.py:handler()` | Lambda handler — decorator is cleanest. Captures CloudWatch request ID automatically |
| Predictor Training | `@fd.monitor` decorator on `training/train_handler.py:handler()` | Same pattern. Tag dependency: `research-lambda` |
| Predictor Inference | `@fd.monitor` decorator on `inference/daily_predict.py:handler()` | Daily Lambda. Non-exception report for "stale weights" warning |
| Backtester | `with fd.guard()` wrapping `backtest.py` main | EC2 cron script, not Lambda. Guard block at top level |
| Executor | `with fd.guard()` wrapping `executor/main.py` main loop | **Critical flow** — alert only, never auto-fix. Tag dependency: `predictor-inference` |
| EOD Emailer | `with fd.guard()` wrapping `executor/eod_reconcile.py` | Tag dependency: `executor` |

#### Configuration (YAML)

```yaml
# flow-doctor.yaml — placed in each repo root
flow_name: research-lambda
repo: brianmcmahon/alpha-engine-research
owner: "@brianmcmahon"

notify:
  - type: slack
    webhook_url: ${SLACK_WEBHOOK_URL}
    channel: "#alpha-alerts"
  - type: email
    sender: ${EMAIL_SENDER}
    recipients: ${EMAIL_RECIPIENTS}
    smtp_host: smtp.gmail.com
    smtp_password: ${GMAIL_APP_PASSWORD}

store:
  type: sqlite
  path: /tmp/flow_doctor.db
  # -- or for Lambda: --
  # type: s3
  # bucket: alpha-engine-research
  # prefix: flow-doctor/

dedup_cooldown_minutes: 60

dependencies:
  - research-lambda  # this flow depends on research completing first

# Phase 2+
diagnosis:
  enabled: true
  provider: anthropic
  model: claude-sonnet-4-6
  api_key: ${ANTHROPIC_API_KEY}

# Phase 3+
auto_fix:
  enabled: false          # start with diagnosis-only
  confidence_threshold: 0.90
  scope:
    allow: ["data/", "config/", "lambda/"]
    deny: ["graph/", "scoring/"]
  test_command: "python -m pytest tests/ -x"
  github_token: ${GITHUB_TOKEN}
```

#### Key Issues — Phase 1

1. **Lambda execution context.** When a Lambda fails, the runtime may have very little time remaining to run `report()`. The report path must be fast — serialize to JSON, write to S3 (or buffer for async flush), fire a Slack webhook. Target: <500ms for the report call. Heavier work (email, GitHub issue) should be async or fire-and-forget.

2. **Log capture without log mutation.** The `capture_logs()` handler must not interfere with existing logging configuration (AE modules have their own handlers). Implementation: add a non-propagating handler that buffers `LogRecord` objects, remove it on context exit. Never modify the root logger's level or existing handlers.

3. **Deduplication across processes.** AE's Research Lambda orchestrates 6 LLM agents. If the underlying Anthropic API is down, all 6 agents fail with similar errors. Dedup must work within a single invocation (easy — in-memory) and across invocations (harder — requires checking the event store). For Lambda: use S3-based store with a lightweight dedup check on report. For EC2: SQLite with a simple query.

4. **Scrubbing secrets from context.** The context enrichment step captures environment variables and local state. It must scrub: API keys, passwords, tokens, AWS credentials, and any value matching common secret patterns. Ship a default scrubber; allow users to add custom patterns via config.

5. **Error in the error handler.** If `report()` itself fails (network down, S3 unreachable), it must never crash the caller. All report paths must be wrapped in a broad try/except that logs to stderr as a last resort. The `@monitor` decorator and `guard()` context manager must guarantee they re-raise the original exception, not a Flow Doctor exception.

6. **Non-exception failures.** Several AE failure modes aren't exceptions: scanner returning 0 candidates, IC gate rejecting model, predictions all FLAT. The `report(message, severity=...)` API handles these. Severity levels: `critical` (pipeline halted), `error` (unexpected failure), `warning` (degraded output, pipeline continued). Only `critical` and `error` trigger alerts by default.

---

### Phase 2 — LLM Diagnosis

**Goal:** When `report()` fires, Flow Doctor sends the error context to an LLM and produces a structured root cause analysis. Output: a GitHub issue with diagnosis, or enriched Slack/email alert. No code generation yet.

#### Functionality

| Component | Description |
|---|---|
| **Context Assembler** | Builds the LLM prompt from: exception + traceback, captured logs, flow config, recent git commits (last 7 days), changed files, known failure patterns, dependency status |
| **Failure Classifier** | LLM classifies the failure: `TRANSIENT` (timeout, throttle, network), `DATA` (missing/malformed input), `CODE` (logic bug, import error), `CONFIG` (env var, path), `EXTERNAL` (third-party API down), `INFRA` (OOM, disk, Lambda limits) |
| **Diagnosis Engine** | Produces structured JSON: category, root cause, affected files, confidence (0–1), remediation steps, auto-fixable boolean |
| **Knowledge Base** | Error signature → known diagnosis mapping. Checked *before* calling the LLM. Populated from confirmed diagnoses (feedback loop). Short-circuits known patterns to save cost and time |
| **Git Context Loader** | Clones or reads the local repo to get recent commits and changed files. For Lambda: fetches from GitHub API. Skipped if the classifier determines the failure is external/transient |
| **Enhanced Notifications** | Slack/email/GitHub issue now includes the diagnosis: category badge, root cause summary, suggested remediation, confidence score |

#### How Diagnosis Fits Into the Report Flow

```
fd.report(exception)
    │
    ├── [Phase 1] Capture error + traceback + logs + context
    ├── [Phase 1] Dedup check → if duplicate, increment counter, skip
    ├── [Phase 1] Persist to event store
    │
    ├── [Phase 2] Check knowledge base for matching error signature
    │   ├── HIT  → use cached diagnosis (no LLM call)
    │   └── MISS → assemble context → call LLM → parse diagnosis
    │       └── Persist diagnosis to event store
    │
    ├── [Phase 1+2] Send alert (now enriched with diagnosis if available)
    │
    └── [Phase 3] If auto_fix enabled + confidence ≥ threshold → fix flow
```

#### Diagnosis Prompt Structure

```
SYSTEM: You are a pipeline reliability engineer. A scheduled job has
failed. Diagnose the root cause from the information below. Output
structured JSON only.

EXCEPTION:
{exception_type}: {exception_message}

TRACEBACK:
{formatted_traceback}

CAPTURED LOGS (last {n} lines):
{log_buffer}

FLOW CONTEXT:
- Name: {flow_name}
- Repo: {repo}
- Runtime: Python {version}, {os}, Lambda/EC2
- Dependencies: {dep_status}

RECENT GIT CHANGES (last 7 days):
{git_log}

CHANGED FILES:
{changed_file_contents}  # only files touched in recent commits

KNOWN FAILURE PATTERNS FOR THIS FLOW:
{knowledge_base_entries}

OUTPUT FORMAT:
{
  "category": "TRANSIENT|DATA|CODE|CONFIG|EXTERNAL|INFRA",
  "root_cause": "One-paragraph explanation of what went wrong and why",
  "affected_files": ["path/to/file.py:line"],
  "confidence": 0.0-1.0,
  "remediation": "Step-by-step instructions to fix this",
  "auto_fixable": true/false,
  "alternative_hypotheses": ["Other possible causes considered"],
  "reasoning": "Chain of thought: how you arrived at this diagnosis"
}
```

#### Alpha Engine — Per-Flow Diagnosis Context

Each AE flow has known failure modes that should be included in the knowledge base at bootstrap:

| Flow | Known Failure Patterns |
|---|---|
| **Research Lambda** | `AnthropicError` / `APIStatusError` → EXTERNAL (Haiku overloaded, retry later). `yfinance` timeout → EXTERNAL (RSS feed down). `scanner_results == []` → DATA (quant filter too strict or market data stale). `S3UploadFailedError` → CONFIG (IAM rotation) |
| **Predictor Training** | `yfinance.download` returns empty DataFrame → EXTERNAL (Lambda IP blocked). `IC below threshold` → not a failure (expected skip — model didn't improve). `FileNotFoundError: price_cache` → DATA (S3 sync failed). `MemoryError` → INFRA (Lambda 10GB limit hit on large universe) |
| **Backtester** | `score_performance table empty` → DATA (not enough signal history yet). `MemoryError` on VectorBT → INFRA (EC2 ran out of RAM during param sweep). `S3 PutObject denied` → CONFIG (IAM) |
| **Predictor Inference** | `KeyError` in feature engineering → CODE (new ticker missing expected columns). `FileNotFoundError: gbm_latest.txt` → DATA (training didn't run this week). `All predictions FLAT` → warning, not error (model is uncertain — log but don't alert) |
| **Executor** | `IB Gateway connection refused` → EXTERNAL (Gateway not running on port 4002). `No signals.json found` → DATA (upstream Research failed). `InsufficientFunds` → not auto-fixable. Market holiday → not a failure (check calendar) |
| **EOD Emailer** | `IBKR NAV not available` → TRANSIENT (market hasn't settled, retry in 15 min). `SMTPAuthenticationError` → CONFIG (Gmail app password expired). `No trades today` → not a failure (no positions to report) |

#### Key Issues — Phase 2

1. **Async LLM calls in Lambda.** An LLM diagnosis takes 5–30 seconds. In a Lambda with ≤60s remaining, this may timeout. Options: (a) fire-and-forget — dispatch the diagnosis to a separate Lambda/SQS queue and return immediately, (b) set a hard timeout on the LLM call and fall back to Phase 1 alert-only if it doesn't complete. Recommendation: option (a) for Lambda flows, synchronous for EC2 flows.

2. **Log volume vs. context window.** Research Lambda produces 50K+ log lines across 6 agents. The context assembler needs smart truncation: (a) always include the last N lines before the error, (b) grep for ERROR/WARNING/Traceback with surrounding context, (c) include timing summaries. Token budget: ~30K for logs, ~10K for code context, ~5K for prompt/output. For Sonnet with 200K context, this is well within limits.

3. **"Expected skips" vs. real failures.** IC gate rejection, market holidays, and empty result sets are not bugs. The report API's severity system handles this: the application reports these as `severity="warning"` (or doesn't report at all). But if an application developer forgets and reports them as errors, the knowledge base should learn to classify them correctly after feedback.

4. **Confidence calibration.** LLMs are overconfident. Mitigation: (a) require alternative hypotheses in the output, (b) track diagnosis accuracy via the feedback endpoint, (c) apply a calibration discount (e.g., LLM says 0.95 → calibrated 0.80) until enough feedback data exists to tune. Start with a conservative Phase 3 threshold of 0.90.

5. **Cost control.** Each diagnosis: ~$0.05–0.15 (Sonnet, ~50K input). AE expectation: 2–3 real failures/week = ~$0.50/week. Guardrails are centralized in Section 2 (Rate Limiting + Cost Control): daily diagnosis cap (default: 3), cascade-aware budget allocation, dedup, knowledge base short-circuits, and daily digest for suppressed reports. Additionally, only `error` and `critical` severity trigger diagnosis — `warning` does not.

6. **Git context in Lambda.** Lambda doesn't have a local git clone. Options: (a) GitHub API to fetch recent commits and file contents, (b) bundle a shallow clone in the Lambda layer, (c) skip git context for Lambda flows and rely on logs + traceback only. Recommendation: (a) — GitHub API is lightweight and doesn't require a clone. Use the `GITHUB_TOKEN` already needed for PR creation in Phase 3.

7. **LLM provider abstraction.** Ship with Anthropic (Claude) and OpenAI implementations. Interface:

```python
class DiagnosisProvider(ABC):
    @abstractmethod
    def diagnose(self, context: DiagnosisContext) -> Diagnosis: ...
```

Users can implement custom providers (local models, Azure OpenAI, etc.).

---

### Phase 3 — Auto-Fix + PR Generation

**Goal:** For high-confidence diagnoses of in-scope failures, generate a code fix, validate it, and open a PR. This is the highest-risk phase — a bad PR is worse than no PR.

#### Functionality

| Component | Description |
|---|---|
| **Fix Scope Guard** | Per-flow allowlist of files/directories the fixer may modify. Enforced by validating the generated diff against the allowlist *before* creating the branch — not just in the LLM prompt |
| **Fix Generator** | LLM generates a unified diff given: diagnosis, full content of affected files + their tests, and the repo's coding conventions (CLAUDE.md, pyproject.toml style config) |
| **Validation Runner** | Runs the configured test command against the fix. PR is only opened if tests pass. Pluggable: `pytest`, `npm test`, `make test`, shell command, or GitHub Actions trigger-and-wait |
| **PR Creator** | Opens a GitHub PR: branch `flow-doctor/{flow_name}/{date}-{short_hash}`, title includes error category, body includes full diagnosis + diff explanation + test results + confidence score |
| **Dry Run Mode** | Generates fix + runs tests but doesn't open PR. Outputs the diff to the event store and Slack. Useful for building trust before enabling live PRs |
| **Fix Replay Store** | Records rejected PRs with rejection reason. Future diagnoses of similar errors reference prior rejections to avoid repeating bad fixes |
| **Unfixable Category Gate** | Certain failure categories never attempt auto-fix regardless of confidence: `EXTERNAL`, `INFRA`, `CONFIG:credential`. Routes directly to issue-only |

#### Fix Generation Flow

```
Diagnosis (confidence ≥ threshold, category is fixable)
    │
    ├── Scope check: are affected_files within fix_scope.allow?
    │   └── NO → file issue only, skip fix
    │
    ├── Check fix replay store for similar prior rejections
    │   └── MATCH → include rejection context in LLM prompt ("don't do X")
    │
    ├── Fetch full file contents for affected files + test files
    │
    ├── LLM generates unified diff
    │
    ├── Validate diff: only touches allowed files? Parseable? Applies cleanly?
    │
    ├── Apply diff to a temporary branch
    │
    ├── Run test_command
    │   ├── PASS → create PR
    │   └── FAIL → file issue with diagnosis + "attempted fix failed tests"
    │
    └── Create PR, assign owner, add labels
```

#### Fix Scope Configuration (Alpha Engine)

```yaml
# Per-flow auto_fix settings in flow-doctor.yaml

# research-lambda
auto_fix:
  enabled: true
  confidence_threshold: 0.90
  scope:
    allow:
      - "data/"           # scanner, data fetching — most common fixable failures
      - "config/"         # YAML configs
      - "lambda/"         # handler, entry point
      - "email/"          # notification code
    deny:
      - "graph/"          # LLM agent orchestration — too complex
      - "scoring/"        # scoring changes affect portfolio composition
  test_command: "python -m pytest tests/ -x -q"
  dry_run: true           # start in dry run, enable live after confidence builds

# predictor (training + inference share a repo)
auto_fix:
  enabled: true
  confidence_threshold: 0.90
  scope:
    allow:
      - "data/"           # feature engineering, dataset
      - "training/"       # training handler
      - "inference/"      # inference handler
    deny:
      - "model/"          # GBM model internals — changes affect predictions
  test_command: "python -m pytest tests/ -x -q"
  dry_run: true

# executor — NEVER auto-fix
auto_fix:
  enabled: false          # trades money (paper or live) — diagnosis only

# eod-emailer (same repo as executor, different scope)
auto_fix:
  enabled: true
  confidence_threshold: 0.95   # higher bar — shares repo with executor
  scope:
    allow:
      - "executor/eod_reconcile.py"
      - "executor/email/"
    deny:
      - "executor/main.py"
      - "executor/risk_guard.py"
      - "executor/position_sizer.py"
      - "executor/ibkr.py"
      - "executor/strategies/"
  test_command: "python -m pytest tests/test_eod.py -x -q"
  dry_run: true

# backtester
auto_fix:
  enabled: true
  confidence_threshold: 0.90
  scope:
    allow:
      - "analysis/"       # signal quality, attribution
      - "optimizer/"      # weight/executor/veto optimization
    deny:
      - "vectorbt_bridge.py"  # simulation engine — complex
  test_command: "python -m pytest tests/ -x -q"
  dry_run: true
```

#### PR Template

```markdown
## [Flow Doctor] {category}: {flow_name} — {short_description}

### Root Cause
{diagnosis.root_cause}

**Category:** `{diagnosis.category}`
**Confidence:** {diagnosis.confidence}
**Error:** `{exception_type}: {exception_message}`

### Changes
{diff_summary — what was changed and why}

### Files Modified
- `{file_path}` — {one-line description of change}

### Test Results
✅ `{test_command}` passed ({n} tests, {duration}s)

<details>
<summary>Test output</summary>

```
{test_stdout}
```

</details>

### Diagnosis Details

<details>
<summary>Full diagnosis</summary>

**Reasoning:** {diagnosis.reasoning}

**Alternative hypotheses:** {diagnosis.alternative_hypotheses}

**Remediation steps:** {diagnosis.remediation}

</details>

---
⚕️ *Generated by [Flow Doctor](...) v{version}*
*Confidence threshold: {threshold} · This diagnosis scored: {confidence}*
*To improve future fixes: reject this PR with a comment explaining why.*
```

#### Key Issues — Phase 3

1. **The wrong fix is worse than no fix.** A bad auto-fix wastes reviewer time, risks rubber-stamp merging, and erodes trust. Mitigations:
   - High confidence threshold (0.90 default, higher for sensitive repos)
   - Test gate is mandatory — no PR without passing tests
   - Dry run mode for the first 4+ weeks of any new flow
   - Scope guard enforced server-side, not just in the prompt
   - Fix replay store prevents repeating previously-rejected approaches
   - PRs are clearly labeled with confidence scores and automated disclaimers

2. **Test environment mismatch.** Running tests requires the right Python version, dependencies, and environment. For Lambda flows: tests can't run inside the Lambda. Options:
   - (a) GitHub Actions — trigger a workflow, wait for result. Most reliable, works everywhere
   - (b) Local execution — if Flow Doctor runs on EC2 alongside the code. Works for AE's EC2 flows
   - (c) Docker — spin up a container matching the Lambda runtime
   - Recommendation: make the test runner pluggable. Ship (a) and (b). For AE, use (b) for EC2 flows, (a) for Lambda flows

3. **Cascading failure → one PR, not three.** Monday chain: Research → Predictor Training → Backtester. AE flows always run regardless of upstream status (they degrade gracefully on stale inputs), but Flow Doctor recognizes cascades via dependency tagging (see Section 2). Downstream failures linked to an upstream failure in the same cycle consume zero diagnosis budget — they get a brief cascade alert and a link to the upstream report. Only the root failure gets a diagnosis, issue, or PR.

4. **Branch conflicts in the same repo.** Executor and EOD Emailer share `alpha-engine`. If both fail and both produce fixes, the second PR may conflict. Solution: per-repo lock with a short TTL. If a fix is already in progress for a repo, queue the second fix and create a combined PR.

5. **Secret/credential failures can't be fixed.** Expired Gmail app password, rotated AWS keys — these are `CONFIG:credential` category failures. The unfixable category gate routes these to issue-only with explicit instructions ("rotate the GMAIL_APP_PASSWORD secret in AWS Secrets Manager").

6. **LLM code quality limits.** Works well for: wrong import path, missing null check, incorrect string format, stale config value. Struggles with: multi-file refactors, runtime state bugs, protocol-level issues (IB Gateway). The scope guard + test gate catch most bad fixes, but some "tests pass, logic wrong" cases will slip through. Mitigation: start in dry run mode, review every generated fix manually until accuracy is proven.

7. **Repo access and git operations.** Flow Doctor needs to: clone/pull the repo, create a branch, apply a diff, push, and open a PR. For Lambda environments: this must happen externally (GitHub API for everything, no local git). For EC2: local git operations are fine. Auth: GitHub fine-grained PAT (single user) or GitHub App (multi-tenant). The `github_token` in config handles both.

---

## 4. API Design

### Python SDK (Primary Interface)

```python
import flow_doctor

# ── Initialization ──────────────────────────────────────────────
# From YAML config
fd = flow_doctor.init(config_path="flow-doctor.yaml")

# Or inline
fd = flow_doctor.init(
    flow_name="research-lambda",
    repo="brianmcmahon/alpha-engine-research",
    owner="@brianmcmahon",
    notify=["slack:#alpha-alerts"],
    store="sqlite:///tmp/flow_doctor.db",
    diagnosis={"provider": "anthropic", "model": "claude-sonnet-4-6"},
    auto_fix={"enabled": False},
)

# ── Reporting ───────────────────────────────────────────────────
# Explicit report
try:
    run_pipeline()
except Exception as e:
    fd.report(e)
    raise

# Context manager
with fd.guard():
    run_pipeline()

# Decorator (ideal for Lambda handlers)
@fd.monitor
def handler(event, context):
    return run_pipeline(event)

# Non-exception report
fd.report(
    "Scanner returned 0 candidates",
    severity="warning",
    context={"tickers_scanned": 900},
)

# ── Log Capture ─────────────────────────────────────────────────
with fd.capture_logs(level=logging.INFO):
    logger.info("Starting scan...")
    # ... logs buffered, attached to any report() in this block

# ── Manual Diagnosis (for debugging / REPL use) ────────────────
diagnosis = fd.diagnose(
    error="KeyError: 'RSI_14'",
    traceback=tb_string,
    logs=log_string,
)
print(diagnosis.root_cause)
print(diagnosis.remediation)

# ── Feedback ────────────────────────────────────────────────────
fd.feedback(
    diagnosis_id="01HXY...",
    correct=False,
    corrected_root_cause="Actually caused by yfinance schema change, not missing data",
)

# ── History / Status ────────────────────────────────────────────
reports = fd.history(limit=10)           # last 10 reports for this flow
stats = fd.stats()                       # diagnosis accuracy, fix acceptance rate
```

### REST API (Optional — For Non-Python Callers)

For shell scripts, non-Python Lambda runtimes, or remote invocation:

```
POST /api/v1/report
{
  "flow_name": "research-lambda",
  "error": "KeyError: 'RSI_14'",
  "traceback": "...",
  "logs": "...",
  "severity": "error",
  "context": {"key": "value"}
}
→ 202 Accepted
{
  "report_id": "01HXY...",
  "diagnosis_id": "01HXZ..." | null,    # null if async or Phase 1 only
  "actions": ["slack_alert", "github_issue"]
}

GET  /api/v1/reports?flow_name=research-lambda&limit=10
GET  /api/v1/reports/{id}
GET  /api/v1/reports/{id}/diagnosis

POST /api/v1/feedback
{
  "diagnosis_id": "01HXZ...",
  "correct": false,
  "corrected_root_cause": "..."
}

GET  /api/v1/stats?flow_name=research-lambda
GET  /api/v1/health
```

The REST API is a thin FastAPI wrapper around the same core library. Deploy as: a sidecar on EC2, a separate Lambda behind API Gateway, or a standalone service.

---

## 5. Data Model

```sql
CREATE TABLE reports (
    id              TEXT PRIMARY KEY,        -- ULID
    flow_name       TEXT NOT NULL,
    severity        TEXT NOT NULL,           -- critical, error, warning
    error_type      TEXT,                    -- exception class name
    error_message   TEXT NOT NULL,
    traceback       TEXT,
    logs            TEXT,                    -- captured log buffer
    context         JSON,                   -- arbitrary key-value metadata
    error_signature TEXT,                   -- hash of (error_type + top 3 frames)
    dedup_count     INTEGER DEFAULT 1,      -- incremented on dedup hits
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE diagnoses (
    id              TEXT PRIMARY KEY,
    report_id       TEXT REFERENCES reports(id),
    flow_name       TEXT NOT NULL,
    category        TEXT NOT NULL,           -- TRANSIENT, DATA, CODE, CONFIG, EXTERNAL, INFRA
    root_cause      TEXT NOT NULL,
    affected_files  JSON,                   -- ["path/to/file.py:42"]
    confidence      REAL NOT NULL,
    remediation     TEXT,
    auto_fixable    BOOLEAN,
    reasoning       TEXT,
    alternative_hypotheses JSON,
    source          TEXT NOT NULL,           -- "llm" or "knowledge_base"
    llm_model       TEXT,
    tokens_used     INTEGER,
    cost_usd        REAL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE actions (
    id              TEXT PRIMARY KEY,
    report_id       TEXT REFERENCES reports(id),
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    action_type     TEXT NOT NULL,           -- SLACK_ALERT, EMAIL, GITHUB_ISSUE, GITHUB_PR
    target          TEXT,                    -- URL, channel, email address
    status          TEXT NOT NULL,           -- SENT, FAILED, PR_OPEN, PR_MERGED, PR_REJECTED
    metadata        JSON,                   -- PR number, issue URL, etc.
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE feedback (
    id              TEXT PRIMARY KEY,
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    correct         BOOLEAN NOT NULL,
    corrected_category    TEXT,
    corrected_root_cause  TEXT,
    notes           TEXT,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE known_patterns (
    id              TEXT PRIMARY KEY,
    flow_name       TEXT,                   -- NULL = applies to all flows
    error_signature TEXT NOT NULL,           -- regex or substring
    category        TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    resolution      TEXT,
    auto_fixable    BOOLEAN DEFAULT FALSE,
    hit_count       INTEGER DEFAULT 0,
    last_seen       TIMESTAMP,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE fix_attempts (
    id              TEXT PRIMARY KEY,
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    diff            TEXT NOT NULL,           -- unified diff
    test_passed     BOOLEAN,
    test_output     TEXT,
    pr_url          TEXT,                   -- NULL if dry run or test failed
    pr_status       TEXT,                   -- OPEN, MERGED, REJECTED, null
    rejection_reason TEXT,                  -- from PR comment, used by fix replay
    created_at      TIMESTAMP NOT NULL
);

-- Indexes
CREATE INDEX idx_reports_flow_created ON reports(flow_name, created_at DESC);
CREATE INDEX idx_reports_signature ON reports(error_signature, created_at DESC);
CREATE INDEX idx_diagnoses_report ON diagnoses(report_id);
CREATE INDEX idx_known_patterns_sig ON known_patterns(error_signature);
CREATE INDEX idx_fix_attempts_diagnosis ON fix_attempts(diagnosis_id);
```

---

## 6. Architecture

### For Alpha Engine (Recommended)

Flow Doctor is a pip-installed library in each AE repo's virtual environment. No new infrastructure.

```
alpha-engine-research/
├── requirements.txt          ← add: flow-doctor
├── flow-doctor.yaml          ← config
└── lambda/handler.py         ← @fd.monitor decorator

alpha-engine-predictor/
├── requirements.txt          ← add: flow-doctor
├── flow-doctor.yaml
└── training/train_handler.py ← @fd.monitor decorator
└── inference/daily_predict.py← @fd.monitor decorator

alpha-engine/
├── requirements.txt          ← add: flow-doctor
├── flow-doctor.yaml
└── executor/main.py          ← with fd.guard()
└── executor/eod_reconcile.py ← with fd.guard()

alpha-engine-backtester/
├── requirements.txt          ← add: flow-doctor
├── flow-doctor.yaml
└── backtest.py               ← with fd.guard()
```

**Storage:** SQLite for EC2 flows (Backtester, Executor, EOD). S3 for Lambda flows (Research, Predictor). Both implement the same storage interface.

**Diagnosis dispatch for Lambda:** When a Lambda fails, `report()` writes the error to S3 and sends a synchronous Slack alert (Phase 1). Diagnosis runs asynchronously — either via a dedicated "flow-doctor-diagnose" Lambda triggered by S3 event, or via SQS. This keeps the failing Lambda's report path fast.

### For General Use (Package)

```
pip install flow-doctor
```

Users configure via YAML or Python. No server required for basic report + alert. Optional FastAPI server for the REST API. Storage defaults to SQLite; users can plug in Postgres, DynamoDB, or S3.

---

## 7. Security Considerations

1. **Secrets in error context.** Tracebacks and local variables may contain API keys, passwords, or PII. The report path must scrub known secret patterns before persisting or sending to the LLM. Default scrubber handles: AWS keys (`AKIA...`), Bearer tokens, passwords in URLs, common env var names (`*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_TOKEN`). Users can add custom patterns.

2. **LLM data exposure.** Diagnosis sends code, logs, and tracebacks to an external LLM. Users must opt in to diagnosis (it's off by default in Phase 1). For sensitive environments, support local model backends via the provider interface.

3. **GitHub repo access.** Fix generation requires push access. Use fine-grained PATs scoped to specific repos, or a GitHub App with minimal permissions (contents: write, pull_requests: write). Never store tokens in the YAML config — always reference environment variables.

4. **Fix scope is a hard boundary.** The scope guard validates the generated diff against the allowlist *after* LLM generation, *before* creating the branch. If any hunks touch files outside scope, the entire fix is rejected. This is not a suggestion to the LLM — it's enforced in code.

5. **Rate limiting.** See Section 2 for the full rate limiting design. Key limits: max 3 diagnoses/day, max 3 issues/day, max 5 alerts/day, with cascade-aware budget allocation and daily digest for suppressed reports. Prevents runaway costs if a flow enters a failure loop.

---

## 8. Success Metrics

| Metric | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Report-to-alert latency | <2 seconds | — | — |
| Alert delivery rate | >99% (at least one channel succeeds) | — | — |
| False alert rate (dedup effectiveness) | <10% of alerts are duplicates | — | — |
| Diagnosis accuracy | — | >70% correct root cause (validated via feedback) | — |
| Knowledge base hit rate | — | >30% of diagnoses short-circuited (no LLM) after 3 months | — |
| Diagnosis latency | — | <30 seconds (LLM), <100ms (knowledge base hit) | — |
| Cost per diagnosis | — | <$0.20 | — |
| Fix PR acceptance rate | — | — | >50% merged without modification |
| Fix PR time-to-open | — | — | <5 min from report to PR |
| Test gate catch rate | — | — | >90% of bad fixes caught by tests before PR |

---

## 9. Open Questions

1. **Should `report()` block or fire-and-forget?** Current design: the alert (Slack/email) is synchronous (fast, <500ms), diagnosis is async. But some users may want to await the diagnosis (e.g., in a CLI script that wants to print the root cause). Recommendation: `report()` returns immediately with a report ID. `fd.diagnose()` is available for synchronous, blocking diagnosis when the caller wants to wait.

2. **How to handle "job didn't run at all"?** The inline model doesn't detect missing runs. Options: (a) leave this to CloudWatch/cron monitoring (recommended), (b) add a lightweight `fd.heartbeat()` call at flow start — if Flow Doctor doesn't see a heartbeat by the expected time, it alerts. Option (b) reintroduces a polling component but is much simpler than full sensor-based monitoring. Defer to Phase 2+ if needed.

3. **Multi-repo cascading failures.** Research output format changes → Predictor's input parsing breaks. The dependency tagging helps *alerting* (suppress downstream alerts), but diagnosis of the downstream failure still needs to look at the upstream repo's recent changes. Should the context assembler fetch git history from dependency repos too? Recommendation: yes, but only the commit log (not full file contents) to keep token cost manageable.

4. **LLM model selection.** Diagnosis works well with Sonnet. Fix generation may benefit from Opus for complex changes. Make the model configurable separately for diagnosis vs. fix generation:
   ```yaml
   diagnosis:
     model: claude-sonnet-4-6      # fast, cheap, good at classification
   auto_fix:
     model: claude-sonnet-4-6      # default same; override to opus for critical flows
   ```

5. **Retry orchestration.** For TRANSIENT failures, the right action is often "retry in 5 minutes," not "file an issue." Should Flow Doctor support triggering retries? Recommendation: no. The application owns its retry logic. Flow Doctor should *recommend* a retry in its diagnosis but not execute it. Exception: if users strongly request this, add an optional `retry_command` config that Flow Doctor invokes for TRANSIENT diagnoses.

6. **Dashboard.** Should Flow Doctor have a UI? For AE: add a page to the existing Streamlit dashboard that reads from the Flow Doctor event store. For the general package: ship a minimal web UI (or just expose the REST API and let users build their own). Defer standalone UI to post-Phase 3.

---

## 10. Package Structure

```
flow-doctor/
├── pyproject.toml
├── flow_doctor/
│   ├── __init__.py              # init(), public API surface
│   ├── core/
│   │   ├── config.py            # YAML + inline config parsing
│   │   ├── models.py            # Report, Diagnosis, Action, Feedback dataclasses
│   │   ├── reporter.py          # report(), guard(), monitor(), capture_logs()
│   │   ├── dedup.py             # Error signature hashing + cooldown logic
│   │   └── scrubber.py          # Secret scrubbing from context
│   ├── diagnosis/
│   │   ├── engine.py            # Orchestrates: knowledge base check → LLM call
│   │   ├── context.py           # Context assembly (error + logs + git + patterns)
│   │   ├── knowledge.py         # Known pattern matching + update
│   │   └── git.py               # Git history loader (local + GitHub API)
│   ├── fix/
│   │   ├── generator.py         # LLM diff generation
│   │   ├── scope_guard.py       # Allowlist/denylist enforcement on diffs
│   │   ├── validator.py         # Test runner (local, GitHub Actions, Docker)
│   │   ├── pr.py                # Branch creation + PR opening
│   │   └── replay.py            # Rejected fix memory
│   ├── notify/
│   │   ├── base.py              # Notifier ABC
│   │   ├── slack.py
│   │   ├── email.py
│   │   └── github_issue.py
│   ├── providers/
│   │   ├── base.py              # DiagnosisProvider ABC
│   │   ├── anthropic.py
│   │   └── openai.py
│   ├── storage/
│   │   ├── base.py              # Storage ABC
│   │   ├── sqlite.py
│   │   └── s3.py
│   ├── api/
│   │   ├── app.py               # FastAPI (optional REST API)
│   │   └── routes.py
│   └── cli.py                   # CLI: fd report, fd history, fd stats, fd diagnose
├── tests/
│   ├── test_reporter.py
│   ├── test_dedup.py
│   ├── test_diagnosis.py
│   ├── test_scope_guard.py
│   └── test_knowledge.py
└── examples/
    └── alpha_engine/
        ├── flow-doctor.yaml
        └── integration.py       # shows decorator/guard/report usage in AE
```

---

## 11. Implementation Priorities

| Order | Deliverable | Phase | Why first |
|---|---|---|---|
| 1 | `init()`, `report()`, `guard()`, `monitor()` | 1 | Core API surface — everything else builds on this |
| 2 | Error capture + scrubbing + SQLite storage | 1 | Must persist before we can alert |
| 3 | Slack + email notification | 1 | Immediate value — replaces ad-hoc error handling in AE |
| 4 | Deduplication | 1 | Prevents alert fatigue once integrated into AE flows |
| 5 | Integrate into AE's 6 flows | 1 | Validates the API in production |
| 6 | Knowledge base + pattern matching | 2 | Short-circuits known failures before LLM, reduces cost |
| 7 | LLM diagnosis engine (Anthropic provider) | 2 | Core Phase 2 value |
| 8 | Git context loader (GitHub API) | 2 | Needed for code-related diagnoses |
| 9 | Enhanced notifications (diagnosis in alerts) | 2 | Immediate value once diagnosis works |
| 10 | Feedback API + knowledge base learning | 2 | Closes the diagnosis quality loop |
| 11 | Fix generator + scope guard | 3 | Core Phase 3 |
| 12 | Test runner (local + GitHub Actions) | 3 | Gate before PR creation |
| 13 | PR creator + fix replay | 3 | Completes the auto-fix loop |
| 14 | REST API (FastAPI) | 3 | Non-Python callers, remote access |
| 15 | Dry run → live rollout per flow | 3 | Trust-building before enabling auto-fix |

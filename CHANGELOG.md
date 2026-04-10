# Changelog

## 0.3.0 (2026-04-10)

Two independent changes folded into one release because 0.2.0 was the
most recent PyPI publish and no consumers ever pinned an intermediate
build:

1. `Notifier.send()` return type changes to `Optional[str]` so the
   dispatcher can persist the target identifier in `actions.target`.
2. Conservative auto-fix defaults + new `deny_repos` field so consumers
   are safer by default without having to override everything per-install.

### Breaking changes

- `Notifier.send()` now returns `Optional[str]` instead of `bool`. On
  success, the return value is a target identifier string that flow-doctor
  persists in `actions.target`. On failure, the return value is `None`.
  Callers should check truthiness (`if send(...)`) instead of `== True`.
  Subclasses of `Notifier` outside the flow-doctor package need to update
  their `send()` return type. The semantic is backward-compatible at the
  truthiness level — `None` is falsy like `False` was — but strict type
  assertions will fail.
- `RemediationConfig.max_auto_remediations_per_day` default lowered
  from **5 → 2**. Rationale: the old default was calibrated for
  high-volume CI where fixes are dependency bumps. For application
  code, 2/day leaves room for real fixes without PR fatigue. Consumers
  needing looser settings can override per-install in `flow-doctor.yaml`.
- `RemediationConfig.fix_pr_min_confidence` default raised from
  **0.8 → 0.85**. Cuts the long tail of marginal LLM suggestions
  humans were rejecting anyway.
- Same default changes mirrored on `GateConfig` so direct constructions
  inherit the new safer baseline.

### New features

- **`actions.target` populated** for every delivered notification via
  the `Notifier.send() -> Optional[str]` return contract. Previously
  always `None`, so the DB had no link back to filed GitHub issues.
  Notifier-specific target formats:
  - **GitHubNotifier** — full `html_url` from the issue API response
    (e.g., `https://github.com/owner/repo/issues/42`). Falls back to
    `https://github.com/{repo}/issues` if the response unexpectedly
    lacks `html_url`.
  - **EmailNotifier** — comma-joined recipients string.
  - **SlackNotifier** — channel string (e.g., `"#alerts"`) or the
    literal `"slack"` if no channel is configured. **Does not return
    the webhook URL** — that's a secret and should not be persisted
    to the DB.

- **`deny_repos` field** on both `RemediationConfig` and `GateConfig`.
  Hard deny list. Repos matching any entry will ALWAYS escalate
  instead of auto-remediating or generating fix PRs, even when
  `remediation.enabled=True` and confidence exceeds thresholds. Match
  is case-insensitive substring against `diagnosis.context['repo']`,
  `flow_name`, or `diagnosis.flow_name`.

  **Issue-filing on denied repos still works.** Only the auto-fix
  code path (`auto_remediate` + `generate_fix_pr`) is blocked. Use
  case: production-critical repos where a bad auto-fix could cost
  real money or safety (trading systems, payment processors, medical
  software).

  YAML supports both list and scalar forms:
  ```yaml
  remediation:
    enabled: true
    deny_repos:
      - cipher813/alpha-engine        # trading system
      - cipher813/alpha-engine-data   # data pipeline
  # or for a single repo:
  remediation:
    deny_repos: cipher813/alpha-engine
  ```

### Migration from 0.2.0

- If you subclass `Notifier` externally, update your `send()` return
  type from `bool` to `Optional[str]`. None-is-failure semantics are
  preserved.
- If you were relying on the 5/day auto-remediation cap or 0.8
  fix-PR confidence, add explicit overrides in your `flow-doctor.yaml`:
  ```yaml
  remediation:
    max_auto_remediations_per_day: 5
    fix_pr_min_confidence: 0.8
  ```
- If you have production repos where auto-fix is risky, add them to
  `remediation.deny_repos` in your YAML. The defensive block lives in
  the package now, not just in operational discipline.

### Tests

- **`tests/test_action_target.py`** (new, 7 tests) — notifier target
  contract + dispatcher persistence.
- **`tests/test_conservative_autofix.py`** (new, 14 tests) — default
  value pins, YAML loading (list + scalar + missing + override),
  `deny_repos` enforcement across `auto_remediate` + `fix_pr` paths,
  case-insensitive matching, non-matching pass-through, empty list
  no-op.
- Updated 9 pre-existing tests in `test_notifications.py`,
  `test_github_notifier.py`, `test_coverage_gaps.py`, and
  `test_remediation_pipeline.py` for the new contracts.
- **Full suite: 264 tests passing** (243 existing + 21 new across
  the two merged PRs).

## 0.2.0 (2026-04-10)

Fail-loud contract and canonical `FLOW_DOCTOR_*` env var fallbacks. Breaking
changes to previously-silent failure paths.

### Breaking changes

- `FlowDoctor.__init__` and `flow_doctor.init()` now re-raise initialization
  errors by default instead of catching them, printing a warning, and running
  in degraded mode. Opt-in `strict=False` preserves the old behavior.
- `_init_notifiers` raises `ConfigError` when a notifier in `config.notify`
  is missing required fields (token, webhook, sender, etc.). The old behavior
  was to silently drop misconfigured notifiers, which meant users discovered
  broken notifications only during an incident.
- `_resolve_env_vars` raises `ConfigError` on unresolved `${VAR}` references
  in YAML instead of leaving the literal string (which previously ended up
  being passed to notifiers as a credential). Opt-in `allow_unresolved=True`
  for unit tests.

### New features

- **Canonical `FLOW_DOCTOR_*` env var contract** — documented in README.
  Every notifier credential has a fallback chain: config → `FLOW_DOCTOR_*`
  canonical name → common conventions (`GH_TOKEN`, `GMAIL_APP_PASSWORD`,
  `SLACK_WEBHOOK_URL`, `ANTHROPIC_API_KEY`, etc.). Same code works across
  systemd, Docker, CI, and every major deployment target.
- **Env-var-only quickstart** — `flow_doctor.init()` can now run with zero
  config file if all required settings come from env vars. Set
  `FLOW_DOCTOR_GITHUB_REPO` + `FLOW_DOCTOR_GITHUB_TOKEN`, pass a
  `notify=[{"type": "github"}]` kwarg, and you're done.
- **Notifier send failures log at CRITICAL** via the `flow_doctor` logger
  (in addition to existing stderr prints). Host apps see the failure in
  their log stream — journalctl, Sentry, Datadog, whatever.
- **Aggregate-failure signal** — when *all* notifiers fail for a single
  report, `_send_notifications` emits a distinct CRITICAL log message:
  "error monitoring is itself broken." This is the signal users most need
  to see and previously never did.
- **New `flow_doctor.errors` module** with `FlowDoctorError` base class
  and `ConfigError` subclass. Both exported from the package root.

### Migration from 0.1.0

Most users won't need code changes. If you were relying on silent-skip
behavior (notifier listed in config without credentials, unresolved
`${VAR}` references), you'll now get `ConfigError` at startup — fix the
config. If you truly need the old behavior, pass `strict=False` to
`flow_doctor.init()`.

## 0.1.0 (2026-04-09)

Initial release.

### Features

- **Phase 1 — Error Capture**: Exception and message reporting with deduplication,
  rate limiting, and automatic secret scrubbing (AWS keys, tokens, passwords).
- **Phase 2 — LLM Diagnosis**: Root cause analysis via Claude API with confidence
  scoring, knowledge base caching, and git context assembly.
- **Phase 3 — Auto-Remediation**: Decision gate routing (auto-remediate, generate PR,
  escalate, log-only) with configurable playbooks, market hours lockout, and
  daily/per-failure safety limits.
- **Phase 4 — Auto-Fix PRs**: LLM-generated unified diffs with scope guard validation,
  test runner verification, and GitHub PR creation.
- **Notifications**: GitHub issues (with machine-readable metadata), Slack webhooks,
  and SMTP email.
- **Logging Handler**: `FlowDoctorHandler` attaches to Python's logging system for
  non-blocking, async error capture at WARNING+ levels.
- **Storage**: SQLite backend with thread-safe per-thread connections, full schema
  for reports, diagnoses, actions, feedback, known patterns, and fix attempts.
- **CLI**: `flow-doctor generate-fix --issue-number N` for GitHub Actions integration.

# Changelog

## 0.3.0 (2026-04-10)

`Notifier.send()` return type changes to `Optional[str]` so the dispatcher
can persist the target identifier (GitHub issue URL, email recipients,
Slack channel) in `actions.target`. Breaking change for anyone subclassing
`Notifier` externally.

### Breaking changes

- `Notifier.send()` now returns `Optional[str]` instead of `bool`. On
  success, the return value is a target identifier string that flow-doctor
  persists in `actions.target`. On failure, the return value is `None`.
  Callers should check truthiness (`if send(...)`) instead of `== True`.
- Subclasses of `Notifier` outside the flow-doctor package need to update
  their `send()` return type. The semantic is backward-compatible at the
  truthiness level — `None` is falsy like `False` was — but strict type
  assertions will fail.

### Fixes

- `actions.target` is now populated for every delivered notification.
  Previously it was always `None`, which meant the DB had no link back
  to filed GitHub issues. Introduced by the v0.2.0 fail-loud refactor
  and surfaced during the 2026-04-10 alpha-engine incident verification.

### Notifier-specific target formats

- **GitHubNotifier**: returns the full `html_url` from the GitHub issue
  API response (e.g., `https://github.com/owner/repo/issues/42`). Falls
  back to the generic `https://github.com/{repo}/issues` if the API
  response unexpectedly lacks `html_url`.
- **EmailNotifier**: returns the comma-joined recipients string
  (e.g., `"oncall@example.com, backup@example.com"`).
- **SlackNotifier**: returns the channel string (e.g., `"#alerts"`) or
  the literal `"slack"` if no channel is configured. **Does not return
  the webhook URL** — that's a secret and should not be persisted to
  the DB.

### Tests

- New `tests/test_action_target.py`: 7 tests pinning the new contract
  across all three notifier types plus dispatcher-level persistence
  and failure paths.
- Updated 7 pre-existing tests in `test_notifications.py`,
  `test_github_notifier.py`, and `test_coverage_gaps.py` from
  `assert result is True/False` to `assert result is None/str`.
- Full suite: 250 tests passing (243 + 7 new).

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

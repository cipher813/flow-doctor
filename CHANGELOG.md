# Changelog

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

"""FlowDoctor client: init(), report(), guard(), monitor(), capture_logs()."""

from __future__ import annotations

import functools
import logging
import os
import platform
import sys
import traceback as tb_module
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from flow_doctor.core.config import FlowDoctorConfig, load_config
from flow_doctor.core.dedup import DedupChecker, compute_error_signature, compute_signature_from_exception
from flow_doctor.core.models import Action, ActionStatus, ActionType, Diagnosis, Report, Severity
from flow_doctor.core.rate_limiter import CascadeDetector, RateLimiter
from flow_doctor.core.scrubber import Scrubber
from flow_doctor.notify.base import Notifier
from flow_doctor.storage.base import StorageBackend


class _LogCaptureHandler(logging.Handler):
    """Non-propagating handler that buffers log records."""

    def __init__(self, level: int = logging.DEBUG):
        super().__init__(level)
        self.records: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            pass


class FlowDoctor:
    """Main Flow Doctor client."""

    def __init__(self, config: FlowDoctorConfig):
        self.config = config
        self._scrubber = Scrubber()
        self._store: Optional[StorageBackend] = None
        self._notifiers: List[Notifier] = []
        self._dedup: Optional[DedupChecker] = None
        self._rate_limiter: Optional[RateLimiter] = None
        self._cascade_detector: Optional[CascadeDetector] = None
        self._log_handler: Optional[_LogCaptureHandler] = None
        self._diagnosis_provider = None
        self._knowledge_base = None
        self._context_assembler = None
        self._digest_generator = None
        self._decision_gate = None
        self._remediation_executor = None
        self._healthy = False

        try:
            self._store = self._init_store(config)
            self._notifiers = self._init_notifiers(config)
            self._dedup = DedupChecker(self._store, config.dedup_cooldown_minutes)
            self._rate_limiter = RateLimiter(self._store, config.rate_limits)
            self._cascade_detector = CascadeDetector(self._store)

            # Phase 2: diagnosis components
            self._init_diagnosis(config)

            # Phase 3: remediation (decision gate + executor)
            self._init_remediation(config)

            # Daily digest
            from flow_doctor.digest.generator import DigestGenerator
            self._digest_generator = DigestGenerator(self._store)

            self._healthy = True
        except Exception:
            import sys
            print(
                f"[flow-doctor] WARNING: initialization failed, operating in degraded mode",
                file=sys.stderr,
            )
            import traceback as _tb
            _tb.print_exc(file=sys.stderr)

    @staticmethod
    def _init_store(config: FlowDoctorConfig) -> StorageBackend:
        if config.store.type == "sqlite":
            from flow_doctor.storage.sqlite import SQLiteStorage
            store = SQLiteStorage(config.store.path)
            store.init_schema()
            return store
        raise ValueError(f"Unsupported store type: {config.store.type}")

    @staticmethod
    def _init_notifiers(config: FlowDoctorConfig) -> List[Notifier]:
        notifiers: List[Notifier] = []
        for nc in config.notify:
            if nc.type == "slack" and nc.webhook_url:
                from flow_doctor.notify.slack import SlackNotifier
                notifiers.append(SlackNotifier(nc.webhook_url, nc.channel))
            elif nc.type == "email" and nc.sender and nc.recipients:
                from flow_doctor.notify.email import EmailNotifier
                notifiers.append(EmailNotifier(
                    sender=nc.sender,
                    recipients=nc.recipients,
                    smtp_host=nc.smtp_host,
                    smtp_port=nc.smtp_port,
                    smtp_password=nc.smtp_password,
                ))
            elif nc.type == "github":
                repo = nc.repo or config.repo
                token = nc.token or (config.github.token if config.github else None)
                if repo and token:
                    from flow_doctor.notify.github import GitHubNotifier
                    labels = nc.labels or (config.github.labels if config.github else ["flow-doctor"])
                    notifiers.append(GitHubNotifier(repo=repo, token=token, labels=labels))
        return notifiers

    def _init_diagnosis(self, config: FlowDoctorConfig) -> None:
        """Initialize Phase 2 diagnosis components."""
        from flow_doctor.diagnosis.context import ContextAssembler
        from flow_doctor.diagnosis.knowledge_base import KnowledgeBase

        self._knowledge_base = KnowledgeBase(self._store)
        self._context_assembler = ContextAssembler(
            repo=config.repo,
            dependencies=config.dependencies,
        )

        if config.diagnosis.enabled and config.diagnosis.api_key:
            try:
                from flow_doctor.diagnosis.provider import AnthropicProvider
                self._diagnosis_provider = AnthropicProvider(
                    api_key=config.diagnosis.api_key,
                    model=config.diagnosis.model,
                    confidence_calibration=config.diagnosis.confidence_calibration,
                    timeout_seconds=config.diagnosis.timeout_seconds,
                )
            except ImportError:
                print(
                    "[flow-doctor] WARNING: anthropic package not installed, diagnosis disabled. "
                    "Install with: pip install flow-doctor[diagnosis]",
                    file=sys.stderr,
                )

    def _init_remediation(self, config: FlowDoctorConfig) -> None:
        """Initialize Phase 3 remediation components."""
        if not config.remediation.enabled:
            return

        try:
            from flow_doctor.remediation.decision_gate import DecisionGate, GateConfig
            from flow_doctor.remediation.executor import RemediationExecutor

            gate_config = GateConfig(
                auto_remediate_min_confidence=config.remediation.auto_remediate_min_confidence,
                fix_pr_min_confidence=config.remediation.fix_pr_min_confidence,
                max_auto_remediations_per_day=config.remediation.max_auto_remediations_per_day,
                max_auto_remediations_per_failure=config.remediation.max_auto_remediations_per_failure,
            )
            if not config.remediation.market_hours_lockout:
                gate_config.market_open_hour = 0
                gate_config.market_close_hour = 0

            self._decision_gate = DecisionGate(config=gate_config, store=self._store)
            self._remediation_executor = RemediationExecutor(
                dry_run=config.remediation.dry_run,
                store=self._store,
                telegram_webhook_url=config.remediation.telegram_webhook_url,
            )
        except Exception as e:
            print(f"[flow-doctor] Remediation init failed: {e}", file=sys.stderr)

    def report(
        self,
        error: Any = None,
        *,
        severity: str = Severity.ERROR.value,
        context: Optional[Dict[str, Any]] = None,
        logs: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Optional[str]:
        """Report an error or message. Never crashes the caller.

        Args:
            error: An exception, string message, or None.
            severity: One of 'critical', 'error', 'warning'.
            context: Arbitrary key-value metadata.
            logs: Log text to attach.
            message: Explicit message string (alternative to passing a string as error).

        Returns:
            The report ID, or None if suppressed by dedup.
        """
        try:
            return self._do_report(error, severity=severity, context=context, logs=logs, message=message)
        except Exception as exc:
            # report() must NEVER crash the caller
            print(f"[flow-doctor] Internal error in report(): {exc}", file=sys.stderr)
            return None

    def _do_report(
        self,
        error: Any,
        *,
        severity: str,
        context: Optional[Dict[str, Any]],
        logs: Optional[str],
        message: Optional[str],
    ) -> Optional[str]:
        """Internal report implementation."""
        # Build the report
        error_type: Optional[str] = None
        error_message: str = ""
        traceback_str: Optional[str] = None
        error_signature: Optional[str] = None

        if isinstance(error, BaseException):
            error_type = type(error).__qualname__
            error_message = str(error)
            if error.__traceback__:
                traceback_str = "".join(tb_module.format_exception(type(error), error, error.__traceback__))
            else:
                traceback_str = "".join(tb_module.format_exception_only(type(error), error))
            error_signature = compute_signature_from_exception(error)
        elif isinstance(error, str):
            error_message = error
            error_signature = compute_error_signature(None, None)
        elif error is None and message:
            error_message = message
            error_signature = compute_error_signature(None, None)
        elif error is None:
            error_message = "Unknown error"
            error_signature = compute_error_signature(None, None)
        else:
            error_message = str(error)
            error_signature = compute_error_signature(None, None)

        # For non-exception string reports, use message content in the signature
        if error_type is None:
            import hashlib
            error_signature = hashlib.sha256(error_message.encode("utf-8")).hexdigest()[:16]

        # Attach captured logs
        captured_logs = logs
        if captured_logs is None and self._log_handler is not None:
            captured_logs = "\n".join(self._log_handler.records)

        # Scrub secrets from traceback and context
        if traceback_str:
            traceback_str = self._scrubber.scrub_string(traceback_str)
        enriched_context = self._build_context(context)

        # Dedup check
        is_dup, existing_id = self._dedup.is_duplicate(error_signature)
        if is_dup and existing_id:
            self._dedup.record_dedup_hit(existing_id)
            return None

        # Cascade check
        cascade_source = self._cascade_detector.check_cascade(
            self.config.dependencies,
            self.config.flow_name,
        )

        report = Report(
            flow_name=self.config.flow_name,
            severity=severity,
            error_type=error_type,
            error_message=error_message,
            traceback=traceback_str,
            logs=captured_logs,
            context=enriched_context,
            error_signature=error_signature,
            cascade_source=cascade_source,
        )

        # Persist (always)
        self._store.save_report(report)

        # Phase 2: Diagnosis
        diagnosis = self._run_diagnosis(report, cascade_source)

        # Send notifications (enriched with diagnosis if available)
        self._send_notifications(report, cascade_source is not None, diagnosis)

        # Phase 3: Decision gate + remediation
        if diagnosis and self._decision_gate:
            self._run_remediation(report, diagnosis)

        return report.id

    def _build_context(self, user_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Build enriched context with system info and scrubbed env vars."""
        ctx: Dict[str, Any] = {}

        # System info
        ctx["python_version"] = platform.python_version()
        ctx["os"] = f"{platform.system()} {platform.release()}"
        ctx["flow_name"] = self.config.flow_name

        # Scrubbed environment variables (only a relevant subset)
        env_subset = {}
        for key in sorted(os.environ):
            # Only include a reasonable subset
            if key.startswith(("AWS_", "FLOW_", "PYTHON", "PATH", "HOME", "USER", "LANG")):
                env_subset[key] = os.environ[key]
        ctx["environment"] = self._scrubber.scrub_env_vars(env_subset)

        # User-supplied context
        if user_context:
            ctx["user"] = self._scrubber.scrub_dict(user_context)

        return ctx

    def _run_diagnosis(
        self,
        report: Report,
        cascade_source: Optional[str],
    ) -> Optional[Diagnosis]:
        """Run Phase 2 diagnosis pipeline. Returns Diagnosis or None."""
        # Skip diagnosis for warnings and cascades
        if report.severity == Severity.WARNING.value:
            return None
        if cascade_source:
            return None
        if not self._knowledge_base:
            return None

        try:
            # 1. Check knowledge base first (free, no LLM call)
            kb_diagnosis = self._knowledge_base.lookup(
                report.error_signature, report.id, report.flow_name
            )
            if kb_diagnosis:
                self._store.save_diagnosis(kb_diagnosis)
                return kb_diagnosis

            # 2. Rate limit check for LLM diagnosis
            if not self._diagnosis_provider or not self._rate_limiter:
                return None
            decision = self._rate_limiter.check("diagnosis")
            if decision == "degrade":
                # Log the degraded diagnosis action
                action = Action(
                    report_id=report.id,
                    action_type="diagnosis",
                    status=ActionStatus.DEGRADED.value,
                    target="degraded - diagnosis rate limited",
                )
                self._store.save_action(action)
                return None

            # 2b. Daily cost cap check
            max_cost = self.config.diagnosis.max_daily_cost_usd
            if max_cost > 0:
                daily_cost = self._store.get_daily_diagnosis_cost()
                if daily_cost >= max_cost:
                    action = Action(
                        report_id=report.id,
                        action_type="diagnosis",
                        status=ActionStatus.DEGRADED.value,
                        target=f"degraded - daily cost cap ${max_cost:.2f} reached (spent ${daily_cost:.2f})",
                    )
                    self._store.save_action(action)
                    return None

            # 3. Assemble context and call LLM
            git_context = self._load_git_context()
            context = self._context_assembler.assemble(
                report=report,
                git_context=git_context,
            )
            diagnosis = self._diagnosis_provider.diagnose(context, self._context_assembler)
            diagnosis.report_id = report.id

            # 4. Save diagnosis and record the action
            self._store.save_diagnosis(diagnosis)
            action = Action(
                report_id=report.id,
                action_type="diagnosis",
                status=ActionStatus.SENT.value,
                diagnosis_id=diagnosis.id,
            )
            self._store.save_action(action)

            return diagnosis

        except Exception as e:
            print(f"[flow-doctor] Diagnosis failed: {e}", file=sys.stderr)
            return None

    def _load_git_context(self) -> Optional[dict]:
        """Load git context for diagnosis, preferring local over GitHub API."""
        try:
            from flow_doctor.diagnosis.git_context import GitContextLoader

            # Try local git first
            ctx = GitContextLoader.load_local()
            if ctx:
                return ctx

            # Fall back to GitHub API if repo and token are configured
            if self.config.repo and self.config.github and self.config.github.token:
                return GitContextLoader.load_github(
                    self.config.repo, self.config.github.token
                )
        except Exception:
            pass
        return None

    def _run_remediation(self, report: Report, diagnosis: Diagnosis) -> None:
        """Run the decision gate and execute remediation if appropriate."""
        try:
            decision = self._decision_gate.decide(
                diagnosis=diagnosis,
                error_type=report.error_type,
                error_message=report.error_message,
                flow_name=report.flow_name,
            )

            if decision.decision_type.value == "auto_remediate" and self._remediation_executor:
                result = self._remediation_executor.execute(decision)
                if not result.success and not result.dry_run:
                    print(
                        f"[flow-doctor] Remediation failed: {result.error}",
                        file=sys.stderr,
                    )
            elif decision.decision_type.value == "generate_fix_pr":
                # PR generation is handled separately (Phase 4)
                # Log the decision for now
                if self._store:
                    self._store.save_remediation_action(
                        report_id=report.id,
                        diagnosis_id=diagnosis.id,
                        decision_type="generate_fix_pr",
                        playbook_pattern=decision.playbook_match.name if decision.playbook_match else None,
                    )
            elif decision.decision_type.value == "escalate":
                if self._store:
                    self._store.save_remediation_action(
                        report_id=report.id,
                        diagnosis_id=diagnosis.id,
                        decision_type="escalate",
                        playbook_pattern=decision.playbook_match.name if decision.playbook_match else None,
                        output=decision.reason,
                    )

        except Exception as e:
            print(f"[flow-doctor] Remediation pipeline error: {e}", file=sys.stderr)

    def _send_notifications(
        self,
        report: Report,
        is_cascade: bool,
        diagnosis: Optional[Diagnosis] = None,
    ) -> None:
        """Send notifications, respecting rate limits."""
        # Warnings don't trigger alerts by default
        if report.severity == Severity.WARNING.value:
            return

        for notifier in self._notifiers:
            from flow_doctor.notify.slack import SlackNotifier
            from flow_doctor.notify.email import EmailNotifier
            from flow_doctor.notify.github import GitHubNotifier

            if isinstance(notifier, SlackNotifier):
                action_type = ActionType.SLACK_ALERT.value
            elif isinstance(notifier, EmailNotifier):
                action_type = ActionType.EMAIL_ALERT.value
            elif isinstance(notifier, GitHubNotifier):
                action_type = ActionType.GITHUB_ISSUE.value
            else:
                action_type = "unknown_alert"

            # Rate limit check
            decision = self._rate_limiter.check(action_type)
            if decision == "degrade":
                action = Action(
                    report_id=report.id,
                    action_type=action_type,
                    status=ActionStatus.DEGRADED.value,
                    target="degraded - queued for digest",
                    diagnosis_id=diagnosis.id if diagnosis else None,
                )
                self._store.save_action(action)
                continue

            # Send
            try:
                success = notifier.send(report, self.config.flow_name, diagnosis)
                action = Action(
                    report_id=report.id,
                    action_type=action_type,
                    status=ActionStatus.SENT.value if success else ActionStatus.FAILED.value,
                    diagnosis_id=diagnosis.id if diagnosis else None,
                )
                self._store.save_action(action)
            except Exception as e:
                print(f"[flow-doctor] Notification error: {e}", file=sys.stderr)
                action = Action(
                    report_id=report.id,
                    action_type=action_type,
                    status=ActionStatus.FAILED.value,
                    diagnosis_id=diagnosis.id if diagnosis else None,
                )
                self._store.save_action(action)

    @contextmanager
    def guard(self):
        """Context manager that reports and re-raises any exception.

        Usage:
            with fd.guard():
                run_pipeline()
        """
        try:
            yield
        except Exception as exc:
            try:
                self.report(exc)
            except Exception:
                pass  # report() already guards itself, but belt-and-suspenders
            raise

    def monitor(self, func: Optional[Callable] = None, **kwargs: Any) -> Any:
        """Decorator that reports and re-raises any exception.

        Usage:
            @fd.monitor
            def handler(event, context):
                ...

            # or with arguments:
            @fd.monitor
            def my_func():
                ...
        """
        if func is None:
            # Called with arguments: @fd.monitor(...)
            def decorator(f: Callable) -> Callable:
                return self._wrap_monitor(f)
            return decorator

        # Called without arguments: @fd.monitor
        return self._wrap_monitor(func)

    def _wrap_monitor(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kw: Any) -> Any:
            try:
                return func(*args, **kw)
            except Exception as exc:
                try:
                    self.report(exc)
                except Exception:
                    pass
                raise
        return wrapper

    @contextmanager
    def capture_logs(self, level: int = logging.INFO, logger_name: Optional[str] = None):
        """Context manager that captures log records for attachment to reports.

        Usage:
            with fd.capture_logs():
                logger.info("Starting scan...")
                # ... all logs buffered
        """
        handler = _LogCaptureHandler(level)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

        target_logger = logging.getLogger(logger_name)
        target_logger.addHandler(handler)

        prev_handler = self._log_handler
        self._log_handler = handler
        try:
            yield handler
        finally:
            target_logger.removeHandler(handler)
            self._log_handler = prev_handler

    def get_handler(self, level: int = logging.ERROR, **kwargs: Any) -> "FlowDoctorHandler":
        """Return a logging.Handler that reports ERROR+ records via Flow Doctor.

        Merges defaults from config.handler if present. kwargs override everything.
        """
        from flow_doctor.core.handler import FlowDoctorHandler

        handler_kwargs: Dict[str, Any] = {}
        if self.config.handler:
            handler_kwargs["level"] = getattr(logging, self.config.handler.level, logging.ERROR)
            handler_kwargs["include_patterns"] = self.config.handler.include_patterns
            handler_kwargs["exclude_patterns"] = self.config.handler.exclude_patterns
            handler_kwargs["queue_size"] = self.config.handler.queue_size

        # Explicit level arg overrides config
        handler_kwargs["level"] = level
        handler_kwargs.update(kwargs)

        return FlowDoctorHandler(self, **handler_kwargs)

    def history(self, limit: int = 10) -> List[Report]:
        """Get recent reports for this flow."""
        try:
            return self._store.get_reports(flow_name=self.config.flow_name, limit=limit)
        except Exception as e:
            print(f"[flow-doctor] history() error: {e}", file=sys.stderr)
            return []

    def digest(self, since: Optional[datetime] = None) -> Optional[str]:
        """Generate and optionally send the daily digest.

        Args:
            since: Cutoff time. Defaults to 24 hours ago.

        Returns:
            The digest content string, or None if nothing to report.
        """
        try:
            if not self._digest_generator:
                return None
            content = self._digest_generator.generate(since)
            if content and self._notifiers:
                self._digest_generator.send(
                    self._notifiers, self.config.flow_name, since
                )
            return content
        except Exception as e:
            print(f"[flow-doctor] digest() error: {e}", file=sys.stderr)
            return None


def init(
    config_path: Optional[str] = None,
    **kwargs: Any,
) -> FlowDoctor:
    """Initialize Flow Doctor.

    Args:
        config_path: Path to a flow-doctor.yaml config file.
        **kwargs: Inline config overrides (flow_name, repo, owner, notify, store, etc.)

    Returns:
        A configured FlowDoctor instance.
    """
    config = load_config(config_path=config_path, **kwargs)
    return FlowDoctor(config)

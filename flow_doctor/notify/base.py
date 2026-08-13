"""Abstract base class for notification backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Set

from flow_doctor.core.models import Diagnosis, Report


class Notifier(ABC):
    """Pluggable notification interface."""

    # Severity routing for this notifier instance, set by
    # ``FlowDoctor._init_notifiers`` from the config's ``notify_on``. A set
    # of severity strings (e.g. {"critical", "error", "info"}); when None
    # the dispatcher applies the default set {critical, error}. Custom
    # notifier subclasses inherit this attribute and need not set it.
    notify_on: Optional[Set[str]] = None

    # Diagnosis-category routing for this notifier instance, set by
    # ``FlowDoctor._init_notifiers`` from the config's ``notify_on_category``.
    # A set of uppercased category strings (e.g. {"CODE", "CONFIG"}); when
    # None, every category reaches this notifier (unchanged pre-0.8.0
    # behavior). Requires Phase 2 diagnosis to be enabled — a report with no
    # diagnosis always passes this gate regardless of what's configured
    # here, since an unavailable enrichment must never silently blank a
    # channel. Custom notifier subclasses inherit this attribute and need
    # not set it.
    notify_on_category: Optional[Set[str]] = None

    # Cascade routing for this notifier instance, set by
    # ``FlowDoctor._init_notifiers`` from the config's ``notify_on_cascade``.
    # When False (the default) this notifier does NOT receive reports whose
    # ``cascade_source`` is set — the failure is derived from an upstream one
    # that was already reported, so paging on it multiplies one root cause
    # into N pages. The report is still persisted and still recorded as a
    # DEGRADED action queued for the digest, so it is never silently dropped;
    # it simply stops being a push. Archival or aggregate sinks that want
    # everything set this True. Custom notifier subclasses inherit this
    # attribute and need not set it.
    notify_on_cascade: bool = False

    # Set by ``send()`` on its most recent call: ``None`` after a success,
    # a short human-readable reason after a failure (alpha-engine-config-
    # I7276). The dispatcher (``FlowDoctor._dispatch``) reads this after a
    # falsy ``send()`` return so the ONE operator-visible CRITICAL it logs
    # names the actual cause instead of deferring to a per-notifier log
    # line the operator may never see (WARNING is below the handler's
    # capture threshold; a notifier's own CRITICAL is now excluded from
    # producing a new report by the self-exclusion fix in handler.py, so it
    # only reaches whatever raw log sink the host app has configured).
    # Every concrete notifier's send() MUST set this on every failure
    # return/raise and clear it (None) at the top of send() — see any
    # notify/*.py for the pattern.
    last_error: Optional[str] = None

    @abstractmethod
    def send(
        self,
        report: Report,
        flow_name: str,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Optional[str]:
        """Send a notification for the given report.

        Args:
            report: The error report.
            flow_name: Name of the flow that failed.
            diagnosis: Optional diagnosis to enrich the notification.

        Returns:
            On success, a target identifier string that will be stored in
            the action record's ``target`` field — typically a user-facing
            URL (GitHub issue URL, Slack webhook endpoint) or address
            (email recipients). On failure, ``None``.

            Callers should use truthiness (``if send(...)``) to distinguish
            success from failure, and use the value to construct follow-up
            links when it is non-empty.

            Implementations MUST set ``self.last_error`` to a short
            human-readable reason on every failure path (exception or
            plain falsy return), and clear it to ``None`` at the start of
            ``send()`` so a reused instance never reports a stale reason
            for what turns out to be a success.
        """

    def validate(self) -> None:
        """Lightweight auth/reachability preflight.

        Called by ``FlowDoctor.__init__`` in strict mode so revoked tokens
        and unreachable backends fail fast at startup instead of silently
        dropping error reports later. Subclasses that can cheaply verify
        their credentials (e.g., a GitHub ``GET /user`` call) should
        override this and raise on auth failure. Default is no-op.
        """
        return None

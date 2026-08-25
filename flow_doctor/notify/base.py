"""Abstract base class for notification backends."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Optional, Set

from flow_doctor.core.models import Diagnosis, Report

_logger = logging.getLogger("flow_doctor")

# Default per-call socket timeout for a notifier preflight, in seconds.
#
# Deliberately small. A preflight runs on the IMPORT path of the process
# flow-doctor instruments, and on AWS Lambda that import happens inside a
# hard 10-second INIT budget. On 2026-08-24 a 10s-per-call preflight
# against an unresponsive api.telegram.org consumed the whole INIT budget
# of `alpha-engine-predictor-inference` and the trading pipeline's
# market-hours gate could not run (alpha-engine-config-I8298). A preflight
# is a cheap reachability probe, not a retry loop: if the endpoint has not
# answered in a few seconds, the answer we would get is not worth the
# caller's startup time.
DEFAULT_PREFLIGHT_TIMEOUT_S = 3.0

# Stable log markers for the two preflight outcomes a detector needs to see.
#
# A metric filter matching PROSE is a detector shaped to one notifier: the
# first filter written for this (alpha-engine-config-I8298 deliverable 5)
# matched "preflight unreachable", which is TelegramNotifier's wording alone
# and misses the class-level guard in `FlowDoctor._run_notifier_preflights`
# — the one covering every notifier including third-party ones — and misses
# GitHubNotifier entirely. That is a detector that would have caught the
# 2026-08-24 incident and nothing else like it.
#
# Every code path that reports a preflight it could not complete emits one of
# these tokens, so a filter matches the CONDITION rather than a sentence, and
# the prose stays free to change. Mirrors the `[LEGACY_PRICE_READ]` marker
# convention already in use on the alpha-engine Lambda log groups.
PREFLIGHT_UNREACHABLE_MARKER = "[FLOW_DOCTOR_PREFLIGHT_UNREACHABLE]"
PREFLIGHT_UNVALIDATED_MARKER = "[FLOW_DOCTOR_PREFLIGHT_UNVALIDATED]"


def preflight_timeout() -> float:
    """Per-call socket timeout for notifier preflights, in seconds.

    Override with ``FLOW_DOCTOR_PREFLIGHT_TIMEOUT_S``. An unparseable or
    non-positive value falls back to :data:`DEFAULT_PREFLIGHT_TIMEOUT_S`
    with a warning rather than disabling the bound — an unbounded
    preflight is the failure mode this exists to prevent.
    """
    raw = os.environ.get("FLOW_DOCTOR_PREFLIGHT_TIMEOUT_S")
    if not raw:
        return DEFAULT_PREFLIGHT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        _logger.warning(
            "FLOW_DOCTOR_PREFLIGHT_TIMEOUT_S=%r is not a number; using %ss",
            raw, DEFAULT_PREFLIGHT_TIMEOUT_S,
        )
        return DEFAULT_PREFLIGHT_TIMEOUT_S
    if value <= 0:
        _logger.warning(
            "FLOW_DOCTOR_PREFLIGHT_TIMEOUT_S=%r is not positive; using %ss",
            raw, DEFAULT_PREFLIGHT_TIMEOUT_S,
        )
        return DEFAULT_PREFLIGHT_TIMEOUT_S
    return value


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

    # Recording-vs-delivery split (alpha-engine-config-I7663): when a report
    # is a dedup hit — same signature already notified within the cooldown —
    # ``FlowDoctor`` skips the delivery notifiers (email/Telegram/GitHub) but
    # still gives durable archival sinks a chance to record the occurrence,
    # so a still-open, still-recurring defect stays queryable across every
    # day it fires even on days nothing was sent. Only a notifier with this
    # set True is called on a dedup hit; ``send()`` receives the same Report
    # either way and cannot tell the difference. Defaults False — a normal
    # delivery channel is unaffected and continues to fire only on a fresh
    # signature. Custom notifier subclasses inherit this attribute and need
    # not set it.
    records_on_dedup: bool = False

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

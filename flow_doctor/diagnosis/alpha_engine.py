"""Alpha Engine-specific diagnosis context and system prompt.

Extends the base ContextAssembler with:
- S3 health marker checks (module heartbeats, data freshness)
- Step Function execution history
- IB Gateway connection status
- Pipeline architecture knowledge baked into the system prompt
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from flow_doctor.core.models import Report
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext


# ── Alpha Engine system prompt ───────────────────────────────────────────────

_ALPHA_ENGINE_SYSTEM_PROMPT = """\
You are a pipeline reliability engineer for the Alpha Engine automated trading \
system. A component has failed. Diagnose the root cause using the information \
below. Output structured JSON only.

## System Architecture

Six modules run on AWS (Lambda + EC2), connected through S3:

WEEKLY (Saturday) — Step Function: alpha-engine-saturday-pipeline
  DataPhase1 → RAGIngestion → Research → DataPhase2 → PredictorTraining → Backtester

DAILY (weekdays) — Step Function: alpha-engine-weekday-pipeline
  DailyData → PredictorInference → StartExecutorEC2
  Then on EC2: Planner → Daemon (intraday orders) → EOD Reconcile

Key dependencies:
- DataPhase1 runs first in both pipelines
- Research and Predictor Training are independent (no data dependency)
- Executor reads signals.json + predictions.json from S3
- IB Gateway on EC2 provides market data and order execution

## Common Failure Patterns

1. IAM/permissions: missing policy for Step Function, Lambda, EC2, S3
2. Step Function state machine: incorrect Choice state routing, missing wait states
3. IB Gateway: stale sessions (error 10197), competing live sessions, connection drops
4. Data staleness: missing daily_closes, stale price_cache, empty signals.json
5. EC2 lifecycle: instance not started, systemd services not running, OOM
6. Lambda: timeout, memory exhaustion, cold start failures
7. S3 contract: path changes breaking downstream consumers, schema mismatches
8. Holiday/market schedule: false holiday detection, trading day check errors

## Response Format

You MUST respond with valid JSON matching this exact schema:
{
  "category": "TRANSIENT|DATA|CODE|CONFIG|EXTERNAL|INFRA",
  "root_cause": "One-paragraph explanation of what went wrong and why",
  "affected_module": "research|predictor|executor|backtester|data|dashboard|orchestration",
  "affected_files": ["path/to/file.py:line"],
  "confidence": 0.0-1.0,
  "remediation": "Step-by-step instructions to fix this",
  "auto_fixable": true or false,
  "auto_fix_type": "restart_service|rerun_step|update_config|code_fix|escalate",
  "alternative_hypotheses": ["Other possible causes considered"],
  "reasoning": "Chain of thought: how you arrived at this diagnosis",
  "cascade_risk": "Description of what downstream modules may be affected"
}

Categories:
- TRANSIENT: timeout, throttle, network blip — will likely resolve on retry
- DATA: missing or malformed input data (signals, prices, predictions)
- CODE: logic bug, import error, type error in application code
- CONFIG: environment variable, IAM policy, path, credential issue
- EXTERNAL: third-party API/service down (yfinance, IB Gateway, Polygon)
- INFRA: OOM, disk full, Lambda limits, EC2 not running, resource exhaustion"""


# ── Module-to-repo mapping ───────────────────────────────────────────────────

_MODULE_REPOS = {
    "research": "alpha-engine-research",
    "predictor": "alpha-engine-predictor",
    "executor": "alpha-engine",
    "backtester": "alpha-engine-backtester",
    "data": "alpha-engine-data",
    "dashboard": "alpha-engine-dashboard",
}

_MODULE_FROM_FLOW = {
    "research-lambda": "research",
    "predictor-training": "predictor",
    "predictor-inference": "predictor",
    "executor-planner": "executor",
    "executor-daemon": "executor",
    "backtester": "backtester",
    "data-phase1": "data",
    "data-phase2": "data",
    "daily-data": "data",
    "dashboard": "dashboard",
    "weekday-pipeline": "orchestration",
    "saturday-pipeline": "orchestration",
}


@dataclass
class AlphaEngineContext:
    """Additional context specific to Alpha Engine infrastructure."""
    health_markers: Optional[Dict[str, Any]] = None
    step_function_history: Optional[str] = None
    ib_gateway_status: Optional[str] = None
    s3_data_freshness: Optional[Dict[str, str]] = None
    recent_config_changes: Optional[Dict[str, Any]] = None
    module: Optional[str] = None


class AlphaEngineContextAssembler(ContextAssembler):
    """Extended context assembler with Alpha Engine infrastructure awareness."""

    def __init__(
        self,
        repo: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
        s3_bucket: str = "alpha-engine-research",
    ):
        super().__init__(repo=repo, dependencies=dependencies)
        self.s3_bucket = s3_bucket

    @property
    def system_prompt(self) -> str:
        return _ALPHA_ENGINE_SYSTEM_PROMPT

    def assemble_with_infra(
        self,
        report: Report,
        git_context: Optional[Dict[str, str]] = None,
        infra_context: Optional[AlphaEngineContext] = None,
    ) -> DiagnosisContext:
        """Assemble context including Alpha Engine infrastructure state."""
        base_ctx = self.assemble(report, git_context=git_context)

        if infra_context:
            # Infer module from flow_name
            module = infra_context.module or _MODULE_FROM_FLOW.get(
                report.flow_name, "unknown"
            )

            # Append infra context to logs
            infra_sections = []
            infra_sections.append(f"AFFECTED MODULE: {module}")

            if infra_context.health_markers:
                infra_sections.append(
                    "HEALTH MARKERS:\n"
                    + json.dumps(infra_context.health_markers, indent=2, default=str)
                )

            if infra_context.s3_data_freshness:
                freshness_lines = [
                    f"  {k}: {v}" for k, v in infra_context.s3_data_freshness.items()
                ]
                infra_sections.append(
                    "S3 DATA FRESHNESS:\n" + "\n".join(freshness_lines)
                )

            if infra_context.step_function_history:
                infra_sections.append(
                    f"STEP FUNCTION HISTORY:\n{infra_context.step_function_history}"
                )

            if infra_context.ib_gateway_status:
                infra_sections.append(
                    f"IB GATEWAY STATUS:\n{infra_context.ib_gateway_status}"
                )

            if infra_context.recent_config_changes:
                infra_sections.append(
                    "RECENT CONFIG CHANGES:\n"
                    + json.dumps(infra_context.recent_config_changes, indent=2, default=str)
                )

            # Merge infra context into the base context's logs
            infra_text = "\n\n".join(infra_sections)
            if base_ctx.logs:
                base_ctx.logs = base_ctx.logs + "\n\n--- INFRASTRUCTURE CONTEXT ---\n\n" + infra_text
            else:
                base_ctx.logs = "--- INFRASTRUCTURE CONTEXT ---\n\n" + infra_text

        return base_ctx

    def build_prompt(self, ctx: DiagnosisContext) -> str:
        """Build the user message, including any infra context in logs."""
        return super().build_prompt(ctx)


# ── Infrastructure context collectors ────────────────────────────────────────
# These are standalone functions that can be called from Lambda or EC2.
# Each returns a partial AlphaEngineContext; callers compose them.


def collect_health_markers(s3_client, bucket: str) -> Dict[str, Any]:
    """Read health status markers from S3 for all modules.

    Each module writes: s3://{bucket}/health/{module}.json
    """
    markers = {}
    modules = ["research", "predictor", "executor", "backtester", "data", "dashboard"]

    for module in modules:
        key = f"health/{module}.json"
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            data = json.loads(resp["Body"].read())
            markers[module] = {
                "status": data.get("status"),
                "last_run": data.get("last_run"),
                "duration_s": data.get("duration_seconds"),
            }
        except Exception:
            markers[module] = {"status": "unknown", "error": "marker not found"}

    return markers


def collect_s3_freshness(s3_client, bucket: str) -> Dict[str, str]:
    """Check freshness of key S3 data artifacts."""
    artifacts = {
        "signals": "signals/",
        "predictions": "predictor/predictions/",
        "daily_closes": "predictor/daily_closes/",
        "trades": "trades/trades_full.csv",
        "eod_pnl": "trades/eod_pnl.csv",
    }

    freshness = {}
    for name, prefix in artifacts.items():
        try:
            if prefix.endswith("/"):
                resp = s3_client.list_objects_v2(
                    Bucket=bucket, Prefix=prefix, MaxKeys=1,
                    Delimiter="/",  # Only list "directories"
                )
                # Get the most recent common prefix
                prefixes = resp.get("CommonPrefixes", [])
                if prefixes:
                    latest = sorted(p["Prefix"] for p in prefixes)[-1]
                    # Extract date from prefix like "signals/2026-04-06/"
                    date_part = latest.rstrip("/").split("/")[-1]
                    freshness[name] = date_part
                else:
                    freshness[name] = "no data"
            else:
                resp = s3_client.head_object(Bucket=bucket, Key=prefix)
                last_mod = resp["LastModified"]
                freshness[name] = last_mod.strftime("%Y-%m-%d %H:%M UTC")
        except Exception as e:
            freshness[name] = f"error: {e}"

    return freshness


def collect_step_function_history(
    sfn_client,
    state_machine_arn: str,
    limit: int = 5,
) -> str:
    """Get recent Step Function execution history."""
    try:
        resp = sfn_client.list_executions(
            stateMachineArn=state_machine_arn,
            maxResults=limit,
        )
        lines = []
        for exe in resp.get("executions", []):
            status = exe["status"]
            name = exe["name"]
            start = exe.get("startDate", "")
            stop = exe.get("stopDate", "")
            if isinstance(start, datetime):
                start = start.strftime("%Y-%m-%d %H:%M UTC")
            if isinstance(stop, datetime):
                stop = stop.strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"  {name}: {status} (started {start}, stopped {stop})")
        return "\n".join(lines) if lines else "No recent executions"
    except Exception as e:
        return f"Error fetching history: {e}"

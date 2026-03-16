"""SQLite storage backend."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, date
from typing import List, Optional

from flow_doctor.core.models import Action, Report
from flow_doctor.storage.base import StorageBackend

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reports (
    id              TEXT PRIMARY KEY,
    flow_name       TEXT NOT NULL,
    severity        TEXT NOT NULL,
    error_type      TEXT,
    error_message   TEXT NOT NULL,
    traceback       TEXT,
    logs            TEXT,
    context         TEXT,
    error_signature TEXT,
    dedup_count     INTEGER DEFAULT 1,
    cascade_source  TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id              TEXT PRIMARY KEY,
    report_id       TEXT REFERENCES reports(id),
    flow_name       TEXT NOT NULL,
    category        TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    affected_files  TEXT,
    confidence      REAL NOT NULL,
    remediation     TEXT,
    auto_fixable    INTEGER,
    reasoning       TEXT,
    alternative_hypotheses TEXT,
    source          TEXT NOT NULL,
    llm_model       TEXT,
    tokens_used     INTEGER,
    cost_usd        REAL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id              TEXT PRIMARY KEY,
    report_id       TEXT REFERENCES reports(id),
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    action_type     TEXT NOT NULL,
    target          TEXT,
    status          TEXT NOT NULL,
    metadata        TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id              TEXT PRIMARY KEY,
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    correct         INTEGER NOT NULL,
    corrected_category    TEXT,
    corrected_root_cause  TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS known_patterns (
    id              TEXT PRIMARY KEY,
    flow_name       TEXT,
    error_signature TEXT NOT NULL,
    category        TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    resolution      TEXT,
    auto_fixable    INTEGER DEFAULT 0,
    hit_count       INTEGER DEFAULT 0,
    last_seen       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fix_attempts (
    id              TEXT PRIMARY KEY,
    diagnosis_id    TEXT REFERENCES diagnoses(id),
    diff            TEXT NOT NULL,
    test_passed     INTEGER,
    test_output     TEXT,
    pr_url          TEXT,
    pr_status       TEXT,
    rejection_reason TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_flow_created ON reports(flow_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_signature ON reports(error_signature, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_diagnoses_report ON diagnoses(report_id);
CREATE INDEX IF NOT EXISTS idx_known_patterns_sig ON known_patterns(error_signature);
CREATE INDEX IF NOT EXISTS idx_fix_attempts_diagnosis ON fix_attempts(diagnosis_id);
CREATE INDEX IF NOT EXISTS idx_actions_type_created ON actions(action_type, created_at);
"""


class SQLiteStorage(StorageBackend):
    """SQLite-backed storage. Thread-safe via per-thread connections."""

    def __init__(self, db_path: str = "/tmp/flow_doctor.db"):
        self.db_path = db_path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    def save_report(self, report: Report) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO reports
               (id, flow_name, severity, error_type, error_message, traceback,
                logs, context, error_signature, dedup_count, cascade_source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report.id,
                report.flow_name,
                report.severity,
                report.error_type,
                report.error_message,
                report.traceback,
                report.logs,
                json.dumps(report.context) if report.context else None,
                report.error_signature,
                report.dedup_count,
                report.cascade_source,
                report.created_at.isoformat(),
            ),
        )
        conn.commit()

    def save_action(self, action: Action) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO actions
               (id, report_id, diagnosis_id, action_type, target, status, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                action.id,
                action.report_id,
                action.diagnosis_id,
                action.action_type,
                action.target,
                action.status,
                json.dumps(action.metadata) if action.metadata else None,
                action.created_at.isoformat(),
            ),
        )
        conn.commit()

    def find_report_by_signature(
        self,
        error_signature: str,
        since: datetime,
    ) -> Optional[Report]:
        conn = self._conn()
        row = conn.execute(
            """SELECT * FROM reports
               WHERE error_signature = ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT 1""",
            (error_signature, since.isoformat()),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_report(row)

    def increment_dedup_count(self, report_id: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE reports SET dedup_count = dedup_count + 1 WHERE id = ?",
            (report_id,),
        )
        conn.commit()

    def count_actions_today(self, action_type: str) -> int:
        conn = self._conn()
        today_start = datetime.combine(date.today(), datetime.min.time()).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM actions WHERE action_type = ? AND created_at >= ?",
            (action_type, today_start),
        ).fetchone()
        return row["cnt"] if row else 0

    def has_recent_failure(self, flow_name: str, since: datetime) -> bool:
        conn = self._conn()
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM reports
               WHERE flow_name = ? AND severity IN ('error', 'critical')
               AND created_at >= ?""",
            (flow_name, since.isoformat()),
        ).fetchone()
        return (row["cnt"] if row else 0) > 0

    def get_reports(
        self,
        flow_name: Optional[str] = None,
        limit: int = 10,
    ) -> List[Report]:
        conn = self._conn()
        if flow_name:
            rows = conn.execute(
                "SELECT * FROM reports WHERE flow_name = ? ORDER BY created_at DESC LIMIT ?",
                (flow_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_report(r) for r in rows]

    @staticmethod
    def _row_to_report(row: sqlite3.Row) -> Report:
        ctx = row["context"]
        return Report(
            id=row["id"],
            flow_name=row["flow_name"],
            severity=row["severity"],
            error_type=row["error_type"],
            error_message=row["error_message"],
            traceback=row["traceback"],
            logs=row["logs"],
            context=json.loads(ctx) if ctx else None,
            error_signature=row["error_signature"],
            dedup_count=row["dedup_count"],
            cascade_source=row["cascade_source"] if "cascade_source" in row.keys() else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

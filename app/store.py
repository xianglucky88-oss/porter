"""运行记录持久化。

两张表：
    runs      每轮一条，带 flow_version / flow_content_hash / failure_kind，
              用于把一次运行绑定到确定的定义版本，并区分发布问题与执行期问题。
    submissions  每条录入成功的业务单据，带 source_key 与 receipt_id。

连接用 psycopg3。初始化时建表，幂等；新增列以 ALTER TABLE IF NOT EXISTS 补齐。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import settings
from .models import Run

log = logging.getLogger("porter.store")

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    flow        TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS runs_flow_idx ON runs (flow, started_at DESC);

ALTER TABLE runs ADD COLUMN IF NOT EXISTS flow_version INTEGER;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS flow_content_hash TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS failure_kind TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS failed_step INTEGER;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS failed_step_kind TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS resumed_from TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS batch_id TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS receipt_id TEXT;

CREATE TABLE IF NOT EXISTS submissions (
    receipt_id   TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    flow         TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    idempotency_key TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS submissions_source_idx ON submissions (source_key);

ALTER TABLE submissions ADD COLUMN IF NOT EXISTS flow_version INTEGER;
"""


class Store:
    """薄封装。生产环境会换成连接池，这里保持简单。"""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or settings.database_url

    def _conn(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                with self._conn() as conn:
                    conn.execute("SELECT 1")
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1)
        raise RuntimeError(f"database not ready after {timeout}s: {last}")

    def migrate(self) -> None:
        with self._conn() as conn:
            conn.execute(DDL)

    def save_run(self, run: Run, source_key: str = "", receipt_id: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO runs (run_id, flow, status, started_at, finished_at, error,
                                     attempts, flow_version, flow_content_hash, failure_kind,
                                     failed_step, failed_step_kind,
                                     resumed_from, batch_id, receipt_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id) DO UPDATE
                     SET status = EXCLUDED.status,
                         finished_at = EXCLUDED.finished_at,
                         error = EXCLUDED.error,
                         attempts = EXCLUDED.attempts,
                         failure_kind = EXCLUDED.failure_kind,
                         failed_step = EXCLUDED.failed_step,
                         failed_step_kind = EXCLUDED.failed_step_kind,
                         receipt_id = EXCLUDED.receipt_id""",
                (
                    run.run_id,
                    run.flow,
                    run.status.value,
                    run.started_at,
                    run.finished_at,
                    run.error,
                    run.attempts,
                    run.flow_version,
                    run.flow_content_hash,
                    run.failure_kind,
                    run.failed_step,
                    run.failed_step_kind,
                    run.resumed_from,
                    run.batch_id,
                    run.receipt_id,
                ),
            )
            if run.status.value == "succeeded" and receipt_id:
                conn.execute(
                    """INSERT INTO submissions (receipt_id, run_id, flow, source_key, flow_version)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (receipt_id) DO NOTHING""",
                    (receipt_id, run.run_id, run.flow, source_key, run.flow_version),
                )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,)).fetchone()
            return dict(row) if row else None

    def existing_receipt(self, source_key: str) -> dict[str, Any] | None:
        """恢复执行前查重：该业务单号是否已经成功提交过。"""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT receipt_id, run_id, flow, flow_version, created_at
                   FROM submissions WHERE source_key = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (source_key,),
            ).fetchone()
            return dict(row) if row else None

    def list_runs(self, limit: int = 20, flow: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM runs"
        params: list[Any] = []
        if flow:
            q += " WHERE flow = %s"
            params.append(flow)
        q += " ORDER BY started_at DESC LIMIT %s"
        params.append(limit)
        with self._conn() as conn:
            return list(conn.execute(q, params).fetchall())

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            by_status = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, count(*) AS n FROM runs GROUP BY status"
                ).fetchall()
            }
            by_flow = {
                row["flow"]: row["n"]
                for row in conn.execute(
                    "SELECT flow, count(*) AS n FROM submissions GROUP BY flow"
                ).fetchall()
            }
            dup_sources = conn.execute(
                """SELECT count(*) AS n FROM (
                     SELECT source_key FROM submissions GROUP BY source_key HAVING count(*) > 1
                   ) t"""
            ).fetchone()["n"]
            total = conn.execute("SELECT count(*) AS n FROM submissions").fetchone()["n"]
        return {
            "runs_by_status": by_status,
            "submissions_by_flow": by_flow,
            "submissions_total": total,
            "duplicate_source_keys": dup_sources,
        }

"""运行记录持久化。

两张表：
    runs      每轮一条，只有 started_at / finished_at / status / error。
              没有步骤级快照。
    submissions  每条录入成功的业务单据，带 source_key 与 receipt_id。
              当前不会写 idempotency_key（因为流程没传）。

连接用 psycopg3。初始化时建表，幂等。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
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

CREATE TABLE IF NOT EXISTS submissions (
    receipt_id   TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    flow         TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    idempotency_key TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS submissions_source_idx ON submissions (source_key);
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
                """INSERT INTO runs (run_id, flow, status, started_at, finished_at, error, attempts)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id) DO UPDATE
                     SET status = EXCLUDED.status,
                         finished_at = EXCLUDED.finished_at,
                         error = EXCLUDED.error,
                         attempts = EXCLUDED.attempts""",
                (
                    run.run_id,
                    run.flow,
                    run.status.value,
                    run.started_at,
                    run.finished_at,
                    run.error,
                    run.attempts,
                ),
            )
            if run.status.value == "succeeded" and receipt_id:
                conn.execute(
                    """INSERT INTO submissions (receipt_id, run_id, flow, source_key)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (receipt_id) DO NOTHING""",
                    (receipt_id, run.run_id, run.flow, source_key),
                )

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

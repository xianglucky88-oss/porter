"""运行记录与批次状态的持久化。

表结构按版本演进（schema_migrations 记录已应用的版本）：

    v1  runs / submissions
        历史基线。老库已存在这两张表时直接登记版本，不重建、不丢数据。
    v2  批次执行能力
        runs       增加 batch_id / worker_id（历史行保持 NULL，仍可读）
        submissions 增加 item_id / batch_id，run_id 放开 NOT NULL，
                   idempotency_key 加部分唯一索引（历史行该列为 NULL，不受影响）
        batches / batch_items / item_steps / item_attempts  新建

连接用 psycopg3，每次调用独立连接，因此 Store 实例可跨线程共享。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import settings
from .models import BatchItem, ItemStatus, Run, canonical_json

log = logging.getLogger("porter.store")

DDL_V1 = """
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

DDL_V2 = """
ALTER TABLE runs ADD COLUMN IF NOT EXISTS batch_id TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS worker_id TEXT;

ALTER TABLE submissions ADD COLUMN IF NOT EXISTS item_id TEXT;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS batch_id TEXT;
ALTER TABLE submissions ALTER COLUMN run_id DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS submissions_idem_uniq
    ON submissions (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    batch_key   TEXT NOT NULL UNIQUE,
    flow        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    total       INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    aborted_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS batch_items (
    item_id          TEXT PRIMARY KEY,
    batch_id         TEXT NOT NULL REFERENCES batches(batch_id),
    seq              INTEGER NOT NULL,
    source_key       TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    record_json      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    checkpoint_idx   INTEGER NOT NULL DEFAULT -1,
    -- 提交意图的写前日志：调用目标系统之前先把载荷与指纹落库。
    -- 恢复时只认这份载荷，绝不凭记忆重放。
    submit_payload   TEXT,
    payload_hash     TEXT,
    receipt_id       TEXT,
    evidence         TEXT,
    error            TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    reconcile_attempts INTEGER NOT NULL DEFAULT 0,
    owner            TEXT,
    lease_until      TIMESTAMPTZ,
    last_submit_at   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, source_key)
);
CREATE INDEX IF NOT EXISTS batch_items_batch_idx ON batch_items (batch_id, status);
CREATE INDEX IF NOT EXISTS batch_items_lease_idx ON batch_items (status, lease_until);
CREATE INDEX IF NOT EXISTS batch_items_idem_idx ON batch_items (idempotency_key);

CREATE TABLE IF NOT EXISTS item_steps (
    id         BIGSERIAL PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES batch_items(item_id),
    step_idx   INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    phase      TEXT NOT NULL,
    detail     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS item_steps_item_idx ON item_steps (item_id, step_idx);

CREATE TABLE IF NOT EXISTS item_attempts (
    id         BIGSERIAL PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES batch_items(item_id),
    phase      TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    receipt_id TEXT,
    detail     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS item_attempts_item_idx ON item_attempts (item_id);
"""

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
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

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------

    def migrate(self) -> None:
        """按版本推进表结构。老库（只有 v1 表、无版本表）会被收养为基线 v1。"""
        with self._conn() as conn:
            conn.execute(_SCHEMA_TABLE)
            applied = {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            if 1 not in applied:
                legacy = conn.execute("SELECT to_regclass('runs') AS t").fetchone()["t"]
                if legacy:
                    # 历史库：v1 表已存在，直接登记基线，历史数据原样保留。
                    log.info("adopting existing schema as baseline v1")
                else:
                    conn.execute(DDL_V1)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (1)")
            if 2 not in applied:
                conn.execute(DDL_V2)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (2)")

    def migration_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
            ).fetchone()
            return int(row["v"])

    # ------------------------------------------------------------------
    # 运行记录（v1 能力，保留给单发执行路径）
    # ------------------------------------------------------------------

    def save_run(
        self,
        run: Run,
        source_key: str = "",
        receipt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO runs (run_id, flow, status, started_at, finished_at,
                                     error, attempts, batch_id, worker_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    run.batch_id,
                    run.worker_id,
                ),
            )
            if run.status.value == "succeeded" and receipt_id:
                conn.execute(
                    """INSERT INTO submissions
                         (receipt_id, run_id, flow, source_key, idempotency_key)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (receipt_id) DO NOTHING""",
                    (receipt_id, run.run_id, run.flow, source_key, idempotency_key),
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

    # ------------------------------------------------------------------
    # 批次：创建（幂等）与查询
    # ------------------------------------------------------------------

    def create_batch(
        self,
        batch_id: str,
        batch_key: str,
        flow: str,
        items: list[BatchItem],
        note: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """创建批次。batch_key 相同则返回既有批次——创建调用本身可以安全重试。"""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO batches (batch_id, batch_key, flow, status, total, note)
                   VALUES (%s,%s,%s,'pending',%s,%s)
                   ON CONFLICT (batch_key) DO NOTHING
                   RETURNING batch_id""",
                (batch_id, batch_key, flow, len(items), note),
            ).fetchone()
            if cur is None:
                row = conn.execute(
                    "SELECT * FROM batches WHERE batch_key = %s", (batch_key,)
                ).fetchone()
                return row, False
            for it in items:
                conn.execute(
                    """INSERT INTO batch_items
                         (item_id, batch_id, seq, source_key, idempotency_key, record_json)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (
                        it.item_id,
                        batch_id,
                        it.seq,
                        it.source_key,
                        it.idempotency_key,
                        canonical_json(it.record),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_id = %s", (batch_id,)
            ).fetchone()
            return row, True

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM batches WHERE batch_id = %s", (batch_id,)
            ).fetchone()

    def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC LIMIT %s", (limit,)
                ).fetchall()
            )

    def list_items(self, batch_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM batch_items WHERE batch_id = %s ORDER BY seq",
                    (batch_id,),
                ).fetchall()
            )

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM batch_items WHERE item_id = %s", (item_id,)
            ).fetchone()

    def item_counts(self, batch_id: str) -> dict[str, int]:
        with self._conn() as conn:
            return {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, count(*) AS n FROM batch_items WHERE batch_id = %s GROUP BY status",
                    (batch_id,),
                ).fetchall()
            }

    def item_steps(self, item_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM item_steps WHERE item_id = %s ORDER BY id", (item_id,)
                ).fetchall()
            )

    def item_attempts(self, item_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM item_attempts WHERE item_id = %s ORDER BY id", (item_id,)
                ).fetchall()
            )

    # ------------------------------------------------------------------
    # 批次：认领（CAS + 租约，解决并发恢复竞争）
    # ------------------------------------------------------------------

    def claim_next_pending(
        self, batch_id: str, worker_id: str, lease_seconds: float
    ) -> dict[str, Any] | None:
        """原子认领下一条 pending 记录。租约过期且仍 running 的也可被重新认领
        （崩溃发生在提交意图之前，重放提交前步骤可证明安全）。"""
        with self._conn() as conn:
            return conn.execute(
                """UPDATE batch_items
                     SET status = 'running', owner = %s,
                         lease_until = now() + make_interval(secs => %s),
                         updated_at = now()
                   WHERE item_id = (
                       SELECT item_id FROM batch_items
                        WHERE batch_id = %s
                          AND (status = 'pending'
                               OR (status = 'running' AND lease_until < now()))
                        ORDER BY seq
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                   )
                   RETURNING *""",
                (worker_id, lease_seconds, batch_id),
            ).fetchone()

    def claim_next_unknown(
        self, batch_id: str, worker_id: str, lease_seconds: float, grace_seconds: float = 0
    ) -> dict[str, Any] | None:
        """原子认领下一条待确认记录。确认期间状态保持 unknown，只更新租约。

        仍处于提交宽限期内的记录不参与本轮认领：它们的提交可能仍在途，
        提前确认会把在途提交误判为"未受理"。"""
        with self._conn() as conn:
            return conn.execute(
                """UPDATE batch_items
                     SET owner = %s,
                         lease_until = now() + make_interval(secs => %s),
                         updated_at = now()
                   WHERE item_id = (
                       SELECT item_id FROM batch_items
                        WHERE batch_id = %s
                          AND status = 'unknown'
                          AND (lease_until IS NULL OR lease_until < now())
                          AND (last_submit_at IS NULL
                               OR last_submit_at <= now() - make_interval(secs => %s))
                        ORDER BY seq
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                   )
                   RETURNING *""",
                (worker_id, lease_seconds, batch_id, grace_seconds),
            ).fetchone()

    def release_running_to_pending(self, item_id: str) -> None:
        """本轮放弃认领：running → pending。running 必在提交意图之前，无副作用。"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_items
                     SET status = 'pending', owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE item_id = %s AND status = 'running'""",
                (item_id,),
            )

    def sweep_stale(self, batch_id: str) -> dict[str, int]:
        """清理租约过期的僵尸状态。

        running→pending：崩溃发生在提交意图落库之前，没有任何外部副作用，
                         重新执行是可证明安全的。
        submitting→unknown：崩溃发生在意图落库之后，目标系统可能已受理，
                         只能走证据确认，绝不允许直接重跑。
        """
        with self._conn() as conn:
            back = conn.execute(
                """UPDATE batch_items
                     SET status = 'pending', owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE batch_id = %s AND status = 'running' AND lease_until < now()""",
                (batch_id,),
            ).rowcount
            unknown = conn.execute(
                """UPDATE batch_items
                     SET status = 'unknown', owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE batch_id = %s AND status = 'submitting' AND lease_until < now()""",
                (batch_id,),
            ).rowcount
        return {"running_to_pending": back, "submitting_to_unknown": unknown}

    def has_active_leases(self, batch_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT count(*) AS n FROM batch_items
                    WHERE batch_id = %s AND lease_until IS NOT NULL AND lease_until >= now()""",
                (batch_id,),
            ).fetchone()
            return row["n"] > 0

    def release_lease_if_open(self, item_id: str) -> None:
        """确认轮次结束时释放未安置记录的租约。

        租约的作用是确认期间的互斥；确认返回后若记录仍处开放状态
        （如宽限期等待、查询通道失败），必须立刻放锁，否则后续恢复
        在租约期内无法再次认领——恢复会被自己的租约锁死。
        """
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_items
                     SET owner = NULL, lease_until = NULL, updated_at = now()
                   WHERE item_id = %s AND status = 'unknown'""",
                (item_id,),
            )

    # ------------------------------------------------------------------
    # 批次：生命周期状态迁移（每次迁移都留下审计痕迹）
    # ------------------------------------------------------------------

    def record_step(
        self, item_id: str, step_idx: int, kind: str, phase: str, detail: str = ""
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO item_steps (item_id, step_idx, kind, phase, detail)
                   VALUES (%s,%s,%s,%s,%s)""",
                (item_id, step_idx, kind, phase, detail[:2000]),
            )
            if phase == "done":
                conn.execute(
                    """UPDATE batch_items
                         SET checkpoint_idx = GREATEST(checkpoint_idx, %s), updated_at = now()
                       WHERE item_id = %s""",
                    (step_idx, item_id),
                )

    def record_attempt(
        self,
        item_id: str,
        phase: str,
        outcome: str,
        receipt_id: str | None = None,
        detail: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO item_attempts (item_id, phase, outcome, receipt_id, detail)
                   VALUES (%s,%s,%s,%s,%s)""",
                (item_id, phase, outcome, receipt_id, detail[:2000]),
            )

    def record_submit_intent(self, item_id: str, payload: dict[str, Any], payload_hash: str) -> None:
        """提交意图的写前日志：先落库，再调用目标系统。"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_items
                     SET status = 'submitting', submit_payload = %s, payload_hash = %s,
                         attempts = attempts + 1, last_submit_at = now(), updated_at = now()
                   WHERE item_id = %s AND status = 'running'""",
                (canonical_json(payload), payload_hash, item_id),
            )

    def mark_succeeded(self, item_id: str, receipt_id: str, evidence: str) -> None:
        """确认成功。只允许在持有目标系统证据时调用（回执/重放/查询/人工核验）。"""
        with self._conn() as conn:
            row = conn.execute(
                """UPDATE batch_items
                     SET status = 'succeeded', receipt_id = %s, evidence = %s,
                         error = NULL, owner = NULL, lease_until = NULL, updated_at = now()
                   WHERE item_id = %s
                   RETURNING batch_id, source_key, idempotency_key""",
                (receipt_id, evidence, item_id),
            ).fetchone()
            if row:
                flow = conn.execute(
                    "SELECT flow FROM batches WHERE batch_id = %s", (row["batch_id"],)
                ).fetchone()["flow"]
                conn.execute(
                    """INSERT INTO submissions
                         (receipt_id, run_id, flow, source_key, idempotency_key, item_id, batch_id)
                       VALUES (%s, NULL, %s, %s, %s, %s, %s)
                       ON CONFLICT (receipt_id) DO NOTHING""",
                    (
                        receipt_id,
                        flow,
                        row["source_key"],
                        row["idempotency_key"],
                        item_id,
                        row["batch_id"],
                    ),
                )

    def mark_failed(self, item_id: str, error: str) -> None:
        """目标系统明确拒绝（终态）。未确认的副作用绝不允许走这条路。"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_items
                     SET status = 'failed', error = %s, owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE item_id = %s""",
                (error[:2000], item_id),
            )

    def mark_unknown(self, item_id: str, error: str) -> None:
        """提交结果未知。不是失败更不是成功：必须等证据。"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_items
                     SET status = 'unknown', error = %s, owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE item_id = %s""",
                (error[:2000], item_id),
            )

    def mark_needs_review(self, item_id: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_items
                     SET status = 'needs_review', error = %s, owner = NULL,
                         lease_until = NULL, updated_at = now()
                   WHERE item_id = %s""",
                (reason[:2000], item_id),
            )

    def back_to_pending(self, item_id: str, error: str = "") -> None:
        """提交前失败：无外部副作用，安全地回到待执行。"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_items
                     SET status = 'pending', error = %s, owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE item_id = %s AND status IN ('running', 'pending')""",
                (error[:2000], item_id),
            )

    def bump_reconcile(self, item_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                """UPDATE batch_items
                     SET reconcile_attempts = reconcile_attempts + 1, updated_at = now()
                   WHERE item_id = %s
                   RETURNING reconcile_attempts""",
                (item_id,),
            ).fetchone()
            return int(row["reconcile_attempts"])

    def touch_last_submit(self, item_id: str) -> None:
        """重放提交前刷新提交时间：宽限期应相对最近一次真实提交计算。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE batch_items SET last_submit_at = now(), updated_at = now() WHERE item_id = %s",
                (item_id,),
            )

    def requeue(self, item_id: str) -> None:
        """人工要求重试。有意图日志的只能回到 unknown（走证据确认），
        没有的才能回到 pending（重新执行提交前步骤是安全的）。"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_items
                     SET status = CASE WHEN submit_payload IS NULL THEN 'pending' ELSE 'unknown' END,
                         owner = NULL, lease_until = NULL, updated_at = now()
                   WHERE item_id = %s AND status IN ('failed', 'needs_review', 'unknown')""",
                (item_id,),
            )

    def cancel_pre_commit(self, item_id: str, note: str) -> bool:
        """补偿边界：只允许取消从未产生提交意图的记录（无任何外部副作用）。"""
        with self._conn() as conn:
            n = conn.execute(
                """UPDATE batch_items
                     SET status = 'cancelled', error = %s, owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE item_id = %s AND submit_payload IS NULL
                     AND status IN ('pending', 'running', 'failed')""",
                (note[:2000], item_id),
            ).rowcount
            return n > 0

    def abort_open_items(self, batch_id: str, note: str) -> dict[str, int]:
        """批次中止：提交前的记录直接取消（补偿=放弃，无副作用）；
        提交后未知的记录转入人工处置（不能当作可补偿）。"""
        with self._conn() as conn:
            cancelled = conn.execute(
                """UPDATE batch_items
                     SET status = 'cancelled', error = %s, owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE batch_id = %s AND submit_payload IS NULL
                     AND status IN ('pending', 'running')""",
                (note[:2000], batch_id),
            ).rowcount
            review = conn.execute(
                """UPDATE batch_items
                     SET status = 'needs_review', error = %s, owner = NULL, lease_until = NULL,
                         updated_at = now()
                   WHERE batch_id = %s AND status = 'unknown'""",
                (f"{note}（提交结果未知，需人工核对目标系统）"[:2000], batch_id),
            ).rowcount
            conn.execute(
                "UPDATE batches SET aborted_at = now(), updated_at = now() WHERE batch_id = %s",
                (batch_id,),
            )
        return {"cancelled": cancelled, "needs_review": review}

    def refresh_batch_status(self, batch_id: str) -> str:
        """由记录状态推导批次状态。批次只有全部 succeeded 才算 succeeded。"""
        counts = self.item_counts(batch_id)
        total = sum(counts.values())
        open_n = sum(
            counts.get(s.value, 0)
            for s in (ItemStatus.PENDING, ItemStatus.RUNNING, ItemStatus.SUBMITTING)
        )
        unknown_n = counts.get(ItemStatus.UNKNOWN.value, 0)
        review_n = counts.get(ItemStatus.NEEDS_REVIEW.value, 0)
        succeeded_n = counts.get(ItemStatus.SUCCEEDED.value, 0)

        with self._conn() as conn:
            aborted = conn.execute(
                "SELECT aborted_at FROM batches WHERE batch_id = %s", (batch_id,)
            ).fetchone()["aborted_at"]
            if open_n > 0:
                status = "running"
                finished = False
            elif unknown_n > 0 or review_n > 0:
                status = "needs_attention"
                finished = False
            elif succeeded_n == total:
                status = "succeeded"
                finished = True
            elif aborted is not None:
                status = "aborted"
                finished = True
            else:
                status = "completed"
                finished = True
            conn.execute(
                """UPDATE batches
                     SET status = %s, updated_at = now(),
                         finished_at = CASE WHEN %s THEN COALESCE(finished_at, now()) ELSE finished_at END
                   WHERE batch_id = %s""",
                (status, finished, batch_id),
            )
        return status

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------

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
            batches = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, count(*) AS n FROM batches GROUP BY status"
                ).fetchall()
            }
            items = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, count(*) AS n FROM batch_items GROUP BY status"
                ).fetchall()
            }
        return {
            "runs_by_status": by_status,
            "submissions_by_flow": by_flow,
            "submissions_total": total,
            "duplicate_source_keys": dup_sources,
            "batches_by_status": batches,
            "batch_items_by_status": items,
        }

"""历史运行记录的 schema 演进测试。

模拟一个只有 v1 表的老库：迁移到 v2 后历史数据必须原样保留，
新表必须就位，且迁移本身幂等。

需要 PostgreSQL（DATABASE_URL）。
"""

from __future__ import annotations

import os

import psycopg
import pytest

from app.models import Run, RunStatus, utcnow
from app.store import DDL_V1, Store

DSN = os.environ.get("DATABASE_URL", "postgresql://porter:porter@localhost:5432/porter")


def _pg_alive() -> bool:
    try:
        Store(DSN).wait_ready(timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _pg_alive(), reason="needs postgres")


@pytest.fixture()
def legacy_db():
    """把库打回 v1 时代：只有 runs/submissions，且各有一行历史数据。"""
    with psycopg.connect(DSN) as conn:
        conn.execute(
            """DROP TABLE IF EXISTS item_attempts, item_steps, batch_items,
                                  batches, submissions, runs, schema_migrations CASCADE"""
        )
        conn.execute(DDL_V1)
        conn.execute(
            """INSERT INTO runs (run_id, flow, status, started_at, finished_at, error, attempts)
               VALUES ('legacy-run-1', 'reimbursement_batch', 'succeeded',
                       now() - interval '30 days', now() - interval '30 days' + interval '5 seconds',
                       NULL, 1)"""
        )
        conn.execute(
            """INSERT INTO submissions (receipt_id, run_id, flow, source_key)
               VALUES ('legacy-receipt-1', 'legacy-run-1', 'reimbursement_batch', 'EXP-OLD-1')"""
        )
    yield DSN
    # 收尾：恢复到最新结构，避免影响其他测试
    Store(DSN).migrate()


def test_migration_preserves_legacy_rows(legacy_db):
    store = Store(legacy_db)
    store.migrate()
    assert store.migration_version() == 2

    runs = store.list_runs(limit=10)
    assert any(r["run_id"] == "legacy-run-1" for r in runs), "历史运行记录必须保留"
    legacy = next(r for r in runs if r["run_id"] == "legacy-run-1")
    assert legacy["batch_id"] is None, "历史行没有批次字段，应为 NULL 而不是报错"

    stats = store.stats()
    assert stats["submissions_total"] == 1
    assert stats["duplicate_source_keys"] == 0

    # 新表已就位
    with psycopg.connect(legacy_db) as conn:
        for table in ("batches", "batch_items", "item_steps", "item_attempts"):
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row is not None

    # 迁移幂等：再跑一遍不出错、版本不变
    store.migrate()
    assert store.migration_version() == 2


def test_legacy_path_still_writes_after_migration(legacy_db):
    """迁移后，旧的单发执行路径仍能写运行记录与回执关联。"""
    store = Store(legacy_db)
    store.migrate()
    run = Run(
        run_id="new-run-1",
        flow="reimbursement_batch",
        status=RunStatus.SUCCEEDED,
        started_at=utcnow(),
        finished_at=utcnow(),
        attempts=1,
    )
    store.save_run(run, source_key="EXP-NEW-1", receipt_id="rcpt-new-1",
                   idempotency_key="reimbursement_batch::EXP-NEW-1")
    runs = store.list_runs(limit=10)
    assert any(r["run_id"] == "new-run-1" for r in runs)
    stats = store.stats()
    assert stats["submissions_total"] == 2

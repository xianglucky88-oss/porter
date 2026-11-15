"""Store 的 SQL 契约测试（不需要 PostgreSQL）。

用记录型假连接验证：
  * save_run 的占位符数量与列顺序一致；
  * 运行绑定字段（flow_version / content_hash）确实落库；
  * 已成功运行不会因为再次保存而改变绑定的版本。
"""

from __future__ import annotations

import re

from app.models import Run, RunStatus, utcnow
from app.store import Store


class FakeCursor:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self.log = log
        self.rowcount = 1

    def fetchone(self):
        return None


class FakeConn:
    """只记录 execute 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple = ()) -> FakeCursor:
        self.calls.append((query, params))
        return FakeCursor(self.calls)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _store_with(conn: FakeConn) -> Store:
    store = Store(dsn="postgresql://unused")
    store._conn = lambda: conn  # type: ignore[method-assign]
    return store


def _success_run(**kwargs) -> Run:
    return Run(
        run_id="run-1",
        flow="reimbursement_batch",
        status=RunStatus.SUCCEEDED,
        started_at=utcnow(),
        finished_at=utcnow(),
        flow_version=3,
        flow_content_hash="sha256:abc",
        receipt_id="rcpt-1",
        attempts=1,
        **kwargs,
    )


def test_save_run_placeholder_count_matches_params():
    conn = FakeConn()
    _store_with(conn).save_run(_success_run(), source_key="EXP-1", receipt_id="rcpt-1")

    insert, params = conn.calls[0]
    assert "INSERT INTO runs" in insert
    assert insert.count("%s") == len(params), (insert, params)
    assert "flow_version" in insert and "flow_content_hash" in insert
    assert params[7] == 3 and params[8] == "sha256:abc"


def test_save_run_records_submission_with_version():
    conn = FakeConn()
    _store_with(conn).save_run(_success_run(), source_key="EXP-1", receipt_id="rcpt-1")
    insert, params = conn.calls[1]
    assert "INSERT INTO submissions" in insert
    assert insert.count("%s") == len(params)
    assert params == ("rcpt-1", "run-1", "reimbursement_batch", "EXP-1", 3)


def test_failed_run_does_not_write_submission():
    conn = FakeConn()
    run = _success_run()
    run.status = RunStatus.FAILED
    run.failure_kind = "definition"
    run.failed_step = 2
    _store_with(conn).save_run(run, receipt_id=None)
    assert len(conn.calls) == 1


def test_save_run_does_not_rebind_existing_version():
    """冲突更新只允许改状态/错误/回执，不改版本绑定。"""
    conn = FakeConn()
    _store_with(conn).save_run(_success_run(), source_key="EXP-1", receipt_id="rcpt-1")
    update_clause = conn.calls[0][0].split("DO UPDATE", 1)[1]
    assert "flow_version" not in update_clause
    assert "flow_content_hash" not in update_clause


def test_ddl_is_idempotent_and_adds_binding_columns():
    from app.store import DDL

    assert "ADD COLUMN IF NOT EXISTS flow_version" in DDL
    assert "ADD COLUMN IF NOT EXISTS flow_content_hash" in DDL
    assert "ADD COLUMN IF NOT EXISTS failure_kind" in DDL
    assert not re.search(r"ALTER TABLE \w+ ADD COLUMN (?!IF NOT EXISTS)", DDL)

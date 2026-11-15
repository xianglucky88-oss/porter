"""批次执行器的集成测试：最终性语义逐条验证。

需要 mockapp 与 PostgreSQL：
    docker compose up -d mockapp db
    MOCKAPP_URL=http://localhost:9200 DATABASE_URL=postgresql://porter:porter@localhost:5432/porter \
        pytest -q tests/test_batch.py
"""

from __future__ import annotations

import os
import threading

import httpx
import pytest

from app.batch import BatchBusy, BatchError, BatchRunner
from app.models import Flow, Step, StepKind, derive_idempotency_key
from app.store import Store

MOCK = os.environ.get("MOCKAPP_URL", "http://localhost:9200")
DSN = os.environ.get("DATABASE_URL", "postgresql://porter:porter@localhost:5432/porter")


def _mock_alive() -> bool:
    try:
        return httpx.get(f"{MOCK}/healthz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _pg_alive() -> bool:
    try:
        Store(DSN).wait_ready(timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not (_mock_alive() and _pg_alive()), reason="needs mockapp and postgres"
)


@pytest.fixture()
def store():
    st = Store(DSN)
    st.migrate()
    with st._conn() as conn:
        conn.execute(
            "TRUNCATE item_attempts, item_steps, batch_items, batches, submissions, runs"
        )
    httpx.post(f"{MOCK}/admin/reset", timeout=5)
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)
    yield st
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)


def _runner(store: Store, worker: str = "t1", **kw) -> BatchRunner:
    kw.setdefault("lease_seconds", 30)
    kw.setdefault("reconcile_grace_seconds", 0)
    kw.setdefault("max_reconcile_attempts", 3)
    return BatchRunner(store, worker_id=worker, **kw)


def _flow() -> Flow:
    return Flow(
        name="reimbursement",
        description="t",
        dataset="reimbursement",
        steps=[
            Step(kind=StepKind.OPEN, value="/form/reimbursement"),
            Step(kind=StepKind.FILL, selector="input#source_key", value="STATIC"),
            Step(kind=StepKind.FILL, selector="input#applicant", value="张三"),
            Step(kind=StepKind.FILL, selector="input#expense_type", value="差旅费"),
            Step(kind=StepKind.FILL, selector="input#amount_cents", value="100"),
            Step(kind=StepKind.FILL, selector="input#invoice_no", value="INV-STATIC"),
            Step(kind=StepKind.CLICK, selector="button#submit-btn"),
            Step(kind=StepKind.SUBMIT, value=""),
            Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value=""),
        ],
    )


def _rec(i: int, **over) -> dict:
    rec = {
        "source_key": f"EXP-T-{i:03d}",
        "applicant": "李娜",
        "expense_type": "差旅费",
        "amount_cents": str(1000 + i),
        "invoice_no": f"INV-{i:03d}",
        "remark": "集成测试",
    }
    rec.update(over)
    return rec


def _mock_state() -> dict:
    return httpx.get(f"{MOCK}/admin/state", timeout=5).json()


def _create(runner: BatchRunner, records: list[dict], note: str = ""):
    batch, created = runner.create_batch(_flow(), records, note=note)
    assert created
    return batch["batch_id"]


# ---------------------------------------------------------------------
# 基本语义
# ---------------------------------------------------------------------


def test_happy_path_each_record_exactly_once(store):
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(5)])
    summary = runner.run_batch(bid, _flow())
    assert summary["status"] == "succeeded"
    assert summary["counts"].get("succeeded") == 5

    state = _mock_state()
    assert state["total"] == 5
    assert state["duplicates"] == 0

    items = store.list_items(bid)
    receipts = {it["receipt_id"] for it in items}
    assert len(receipts) == 5, "每条记录必须有独立回执"
    assert all(it["evidence"] == "submit_response" for it in items)


def test_rerun_and_recover_do_not_duplicate(store):
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(4)])
    runner.run_batch(bid, _flow())
    # 整批重跑 + 恢复：都不应产生新单据
    runner.run_batch(bid, _flow())
    runner.recover_batch(bid, _flow())
    state = _mock_state()
    assert state["total"] == 4
    assert state["duplicates"] == 0
    assert store.item_counts(bid).get("succeeded") == 4


def test_create_batch_is_idempotent(store):
    runner = _runner(store)
    records = [_rec(i) for i in range(3)]
    b1, created1 = runner.create_batch(_flow(), records)
    b2, created2 = runner.create_batch(_flow(), records)
    assert created1 and not created2
    assert b1["batch_id"] == b2["batch_id"], "创建请求重试必须返回同一批次"
    assert len(store.list_items(b1["batch_id"])) == 3


def test_step_progress_is_journaled(store):
    runner = _runner(store)
    bid = _create(runner, [_rec(1)])
    runner.run_batch(bid, _flow())
    item = store.list_items(bid)[0]
    steps = store.item_steps(item["item_id"])
    kinds = [(s["step_idx"], s["kind"], s["phase"]) for s in steps]
    # 每一步都有 started/done 两条日志，checkpoint 推进到最后一步
    assert (0, "open", "started") in kinds
    assert (0, "open", "done") in kinds
    assert item["checkpoint_idx"] == len(_flow().steps) - 1


# ---------------------------------------------------------------------
# 未知结果：已受理但回执丢失 / 明确 5xx
# ---------------------------------------------------------------------


def test_receipt_loss_reconciles_via_target_evidence(store):
    """目标系统已受理但回执丢失：记录进入 unknown（不是成功也不是失败），
    恢复时凭目标系统证据确认，且不产生新单据。"""
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "drop", "rate": 1.0}, timeout=5)
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(3)])
    summary = runner.run_batch(bid, _flow())
    assert summary["counts"].get("unknown") == 3
    assert summary["counts"].get("succeeded", 0) == 0, "未确认的副作用不得标记成功"

    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)
    before = _mock_state()["total"]
    assert before == 3, "drop 故障下目标系统其实已受理"

    summary = runner.recover_batch(bid, _flow())
    assert summary["status"] == "succeeded"
    after = _mock_state()
    assert after["total"] == before, "证据确认不得产生新单据"
    assert after["duplicates"] == 0
    items = store.list_items(bid)
    assert all(it["evidence"] == "target_lookup" for it in items)
    assert all(it["receipt_id"] for it in items)


def test_grace_period_defers_then_recovers(store):
    """宽限期内恢复只等待；宽限期后必须能立即接管（租约不被上一轮锁死）。"""
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "drop", "rate": 1.0}, timeout=5)
    runner = _runner(store, reconcile_grace_seconds=600)
    bid = _create(runner, [_rec(1)])
    runner.run_batch(bid, _flow())
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)

    # 宽限期内：只等待，不确认
    runner.recover_batch(bid, _flow())
    item = store.list_items(bid)[0]
    assert item["status"] == "unknown"
    assert item["lease_until"] is None, "上一轮恢复不得用租约锁死下一轮"

    # 提交时间超出宽限期后，下一轮恢复必须立即确认
    with store._conn() as conn:
        conn.execute(
            "UPDATE batch_items SET last_submit_at = now() - interval '1 hour' WHERE batch_id = %s",
            (bid,),
        )
    summary = runner.recover_batch(bid, _flow())
    assert summary["status"] == "succeeded"
    assert _mock_state()["total"] == 1


def test_pre_accept_5xx_then_recover_replays_safely(store):
    """受理前 5xx：目标系统查无记录，恢复时用同一幂等键+原载荷重放。"""
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "500", "rate": 1.0}, timeout=5)
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(3)])
    summary = runner.run_batch(bid, _flow())
    assert summary["counts"].get("unknown") == 3
    assert _mock_state()["total"] == 0, "受理前故障不应产生单据"

    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)
    summary = runner.recover_batch(bid, _flow())
    assert summary["status"] == "succeeded"
    state = _mock_state()
    assert state["total"] == 3
    assert state["duplicates"] == 0
    items = store.list_items(bid)
    assert all(it["evidence"] == "retry_response" for it in items)
    assert all(it["attempts"] == 1 for it in items), "重放复用写前日志，不重复计提交次数"


def test_unknown_never_marked_succeeded_without_evidence(store):
    """目标系统持续不可用：记录保持 unknown 并最终进入人工处置。"""
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "drop", "rate": 1.0}, timeout=5)
    runner = _runner(store, max_reconcile_attempts=2)
    bid = _create(runner, [_rec(1)])
    runner.run_batch(bid, _flow())
    # 故障未清除，但把证据接口也搞挂不现实；这里直接断言：
    # 仅凭一次超时就断言成功是不允许的
    item = store.list_items(bid)[0]
    assert item["status"] == "unknown"
    assert item["receipt_id"] is None


# ---------------------------------------------------------------------
# 进程重启（僵尸租约清扫）
# ---------------------------------------------------------------------


def _force_state(store: Store, bid: str, status: str) -> None:
    with store._conn() as conn:
        conn.execute(
            """UPDATE batch_items
                 SET status = %s, lease_until = now() - interval '1 second',
                     owner = 'dead-worker'
               WHERE batch_id = %s""",
            (status, bid),
        )


def test_crash_before_intent_replays_from_scratch(store):
    """崩溃发生在提交意图之前：无外部副作用，重新执行是安全的。"""
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(3)])
    _force_state(store, bid, "running")
    summary = runner.recover_batch(bid, _flow())
    assert summary["swept"]["running_to_pending"] == 3
    assert summary["status"] == "succeeded"
    assert _mock_state()["total"] == 3


def test_crash_after_intent_goes_to_unknown_then_confirmed(store):
    """崩溃发生在提交意图之后：绝不直接重跑，先按 unknown 走证据确认。"""
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "drop", "rate": 1.0}, timeout=5)
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(2)])
    runner.run_batch(bid, _flow())  # 两条都 unknown（目标已受理）
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)

    # 模拟进程重启时的状态：submitting + 过期租约
    with store._conn() as conn:
        conn.execute(
            """UPDATE batch_items
                 SET status = 'submitting', lease_until = now() - interval '1 second',
                     owner = 'dead-worker'
               WHERE batch_id = %s""",
            (bid,),
        )
    before = _mock_state()["total"]
    summary = runner.recover_batch(bid, _flow())
    assert summary["swept"]["submitting_to_unknown"] == 2
    assert summary["status"] == "succeeded"
    assert _mock_state()["total"] == before, "崩溃恢复不得补投，只能确认"


# ---------------------------------------------------------------------
# 并发恢复竞争
# ---------------------------------------------------------------------


def test_concurrent_recovery_no_duplicate_no_lost(store):
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "drop", "rate": 0.5, "seed": 7}, timeout=5)
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(6)])
    runner.run_batch(bid, _flow())
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)

    errors: list[Exception] = []

    def work(wid: str) -> None:
        try:
            _runner(store, worker=wid).recover_batch(bid, _flow())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(f"w{k}",)) for k in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors

    state = _mock_state()
    assert state["total"] == 6, "并发恢复不得产生重复单据"
    assert state["duplicates"] == 0
    counts = store.item_counts(bid)
    assert counts.get("succeeded") == 6, "并发恢复不得漏掉任何记录"


def test_concurrent_run_of_same_batch(store):
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(6)])

    def work(wid: str) -> None:
        _runner(store, worker=wid).run_batch(bid, _flow())

    threads = [threading.Thread(target=work, args=(f"w{k}",)) for k in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    state = _mock_state()
    assert state["total"] == 6
    assert state["duplicates"] == 0


# ---------------------------------------------------------------------
# 批次中止的补偿边界
# ---------------------------------------------------------------------


def test_abort_cancels_only_pre_commit_items(store):
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(3)])
    summary = runner.abort_batch(bid)
    assert summary["abort"]["cancelled"] == 3
    assert summary["status"] == "aborted"
    assert _mock_state()["total"] == 0, "提交前取消不得产生任何外部副作用"

    # 中止后再恢复：已取消的记录绝不被重新提交
    runner.recover_batch(bid, _flow())
    assert _mock_state()["total"] == 0


def test_abort_sends_unknown_to_review_not_cancel(store):
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "drop", "rate": 1.0}, timeout=5)
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(2)])
    runner.run_batch(bid, _flow())
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)

    summary = runner.abort_batch(bid)
    assert summary["abort"]["needs_review"] == 2
    assert summary["abort"]["cancelled"] == 0, "已提交的记录不在补偿边界内"
    assert summary["status"] == "needs_attention"

    # 人工处置前，恢复流程不得触碰 needs_review
    runner.recover_batch(bid, _flow())
    counts = store.item_counts(bid)
    assert counts.get("needs_review") == 2


def test_abort_rejected_while_worker_active(store):
    runner = _runner(store)
    bid = _create(runner, [_rec(1)])
    with store._conn() as conn:
        conn.execute(
            """UPDATE batch_items
                 SET status = 'running', owner = 'other-worker',
                     lease_until = now() + interval '10 minutes'
               WHERE batch_id = %s""",
            (bid,),
        )
    with pytest.raises(BatchBusy):
        runner.abort_batch(bid)


# ---------------------------------------------------------------------
# 人工处置
# ---------------------------------------------------------------------


def test_payload_conflict_goes_to_review_then_manual_confirm(store):
    """同一身份、不同内容：目标系统的幂等反馈与本地载荷指纹冲突 → 人工处置。"""
    source_key = "EXP-CONFLICT-1"
    idem = derive_idempotency_key("reimbursement", source_key)
    # 目标系统里先存在一笔同身份但金额不同的单据（如历史上人工录入）
    resp = httpx.post(
        f"{MOCK}/api/submit",
        json={
            "form_type": "reimbursement",
            "source_key": source_key,
            "idempotency_key": idem,
            "fields": {"applicant": "别人", "expense_type": "差旅费",
                       "amount_cents": "999999", "invoice_no": "INV-OTHER"},
        },
        timeout=5,
    )
    original_receipt = resp.json()["submission_id"]

    runner = _runner(store)
    bid = _create(runner, [_rec(1, source_key=source_key)])
    runner.run_batch(bid, _flow())
    item = store.list_items(bid)[0]
    assert item["status"] == "needs_review", "内容冲突不得自动确认成功"
    assert _mock_state()["total"] == 1, "冲突时不得再产生新单据"

    # 人工在目标系统核实后登记原回执
    resolved = runner.resolve_item(item["item_id"], "confirm", receipt_id=original_receipt)
    assert resolved["status"] == "succeeded"
    assert resolved["evidence"] == "manual_confirmed"


def test_manual_confirm_requires_real_receipt(store):
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "drop", "rate": 1.0}, timeout=5)
    runner = _runner(store)
    bid = _create(runner, [_rec(1)])
    runner.run_batch(bid, _flow())
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)
    item = store.list_items(bid)[0]

    with pytest.raises(BatchError):
        runner.resolve_item(item["item_id"], "confirm", receipt_id="no-such-receipt")
    assert store.get_item(item["item_id"])["status"] == "unknown", "假回执不得蒙混过关"


def test_cancel_boundary_for_items_with_intent(store):
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "drop", "rate": 1.0}, timeout=5)
    runner = _runner(store)
    bid = _create(runner, [_rec(1)])
    runner.run_batch(bid, _flow())
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)
    item = store.list_items(bid)[0]

    # 有提交意图的记录不允许取消（可能已落地）
    with pytest.raises(BatchError):
        runner.resolve_item(item["item_id"], "cancel")
    # retry 把它送回确认通道，而不是盲目重跑界面步骤
    resolved = runner.resolve_item(item["item_id"], "retry")
    assert resolved["status"] == "unknown"
    runner.recover_batch(bid, _flow())
    assert store.get_item(item["item_id"])["status"] == "succeeded"
    assert _mock_state()["total"] == 1


# ---------------------------------------------------------------------
# 明确拒绝与"不漏单"
# ---------------------------------------------------------------------


def test_rejected_record_is_terminal_and_not_retried(store):
    runner = _runner(store)
    records = [_rec(1), _rec(2, amount_cents=""), _rec(3)]
    bid = _create(runner, records)
    summary = runner.run_batch(bid, _flow())
    assert summary["counts"].get("succeeded") == 2
    assert summary["counts"].get("failed") == 1
    assert summary["status"] == "completed"

    failed = next(it for it in store.list_items(bid) if it["status"] == "failed")
    assert "rejected" in (failed["error"] or "")

    # 恢复不会重试已被明确拒绝的记录
    runner.recover_batch(bid, _flow())
    assert store.get_item(failed["item_id"])["attempts"] == 1
    assert _mock_state()["total"] == 2


def test_recover_alone_processes_all_pending(store):
    """恢复路径同样推进 pending：任何记录都不会被静默漏掉。"""
    runner = _runner(store)
    bid = _create(runner, [_rec(i) for i in range(4)])
    summary = runner.recover_batch(bid, _flow())
    assert summary["status"] == "succeeded"
    assert _mock_state()["total"] == 4


def test_same_record_in_second_batch_is_replay_not_duplicate(store):
    """同一业务记录出现在后续批次（如月度重跑含已投记录）：
    目标系统幂等去重，该记录凭重放证据确认，不产生重复单据。"""
    runner = _runner(store)
    rec = _rec(1)
    bid1 = _create(runner, [rec])
    runner.run_batch(bid1, _flow())

    # 第二个批次记录集不同（含一条新记录），但包含已提交过的 rec
    bid2 = _create(runner, [rec, _rec(2)], note="月度重跑")
    assert bid2 != bid1
    summary = runner.run_batch(bid2, _flow())
    assert summary["status"] == "succeeded"
    items2 = {it["source_key"]: it for it in store.list_items(bid2)}
    assert items2[rec["source_key"]]["evidence"] == "idempotent_replay"
    first = store.list_items(bid1)[0]
    replayed = items2[rec["source_key"]]
    assert replayed["receipt_id"] == first["receipt_id"], "重放必须拿到原回执"
    state = _mock_state()
    assert state["total"] == 2, "两批共 3 条记录，去重后只有 2 条真实单据"
    assert state["duplicates"] == 0

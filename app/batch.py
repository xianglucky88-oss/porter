"""批次执行器：具备最终性语义的批量录入。

设计约束（与需求逐条对应）：

1. 稳定的提交身份
   每条记录的身份 = (流程名, 业务单号) 派生的幂等键，与进程/机器/尝试次数无关。
   批次本身也有批次键：创建请求重试返回同一批次，不会整批重投。

2. 只重试可证明安全的动作
   - 提交前步骤（open/fill/...）：驱动会话内的本地动作，无副作用，崩溃后重放安全；
   - 提交：只有拿着"写前日志"里同一份载荷 + 同一个幂等键重放才安全
     （目标系统按幂等键去重，语义是 at-most-once）；
   - 其余一切情况：不猜、不赌，走证据确认或人工处置。

3. 未知结果的确认
   unknown 状态的记录先查目标系统的证据（按幂等键/业务单号查询）：
   查到且内容指纹一致 → 确认成功；查无记录 → 用原载荷重放提交；
   查不到证据接口或内容冲突 → needs_review，只能人工处置。

4. 不变量
   - 任何路径都不会把未确认的外部副作用标记为成功（succeeded 必须持有证据）；
   - 任何路径都不会为了避免重复而静默漏掉待提交记录（pending 一定会被推进，
     批次只有全部记录到达终态才算结束）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx

from .config import settings
from .executor import (
    DriverError,
    SubmitRejected,
    SubmitTransient,
    _ExecContext,
    make_driver,
    prepare_steps,
    run_prepared_steps,
)
from .models import (
    BatchItem,
    Flow,
    ItemStatus,
    Run,
    RunStatus,
    Step,
    canonical_json,
    derive_idempotency_key,
    payload_fingerprint,
    utcnow,
)
from .store import Store

log = logging.getLogger("porter.batch")


class BatchError(RuntimeError):
    """批次操作的调用方错误（4xx 语义）。"""


class BatchBusy(BatchError):
    """批次仍有活跃执行者（租约未过期），拒绝中止。"""


class ItemSettled(RuntimeError):
    """内部信号：当前记录已被安置到某个状态，停止它的步骤循环。"""


def _batch_key(flow_name: str, records: list[dict[str, Any]]) -> str:
    """批次键：同一流程 + 同一批规范化记录 ⇒ 同一批次（创建可安全重试）。"""
    digest = hashlib.sha256(
        canonical_json({"flow": flow_name, "records": records}).encode("utf-8")
    ).hexdigest()
    return f"bk-{digest[:32]}"


class BatchRunner:
    """一个执行者实例。多实例并发安全：认领是数据库级的 CAS + 租约。

    租约必须长于单条记录的最长执行时间，否则同一记录可能被两个执行者
    同时推进（fencing 失效）。即使如此，目标系统的幂等去重仍保证不产生
    重复单据——幂等键是最后一道防线。
    """

    def __init__(
        self,
        store: Store,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
        reconcile_grace_seconds: float | None = None,
        max_reconcile_attempts: int | None = None,
    ) -> None:
        self.store = store
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = (
            lease_seconds if lease_seconds is not None else settings.lease_seconds
        )
        self.reconcile_grace = (
            reconcile_grace_seconds
            if reconcile_grace_seconds is not None
            else settings.reconcile_grace_seconds
        )
        self.max_reconcile_attempts = (
            max_reconcile_attempts
            if max_reconcile_attempts is not None
            else settings.max_reconcile_attempts
        )

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_batch(
        self, flow: Flow, records: list[dict[str, Any]], note: str = ""
    ) -> tuple[dict[str, Any], bool]:
        if not records:
            raise BatchError("records must not be empty")
        norm: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in records:
            rec = {str(k): ("" if v is None else str(v)) for k, v in dict(raw).items()}
            source_key = rec.get("source_key", "").strip()
            if not source_key:
                # 业务单号缺失时，以记录内容派生稳定单号：同一内容永远是同一记录。
                source_key = "SK-" + hashlib.sha256(
                    canonical_json(rec).encode("utf-8")
                ).hexdigest()[:16]
                rec["source_key"] = source_key
            if source_key in seen:
                raise BatchError(f"duplicate source_key in batch: {source_key}")
            seen.add(source_key)
            norm.append(rec)

        batch_id = "batch-" + uuid.uuid4().hex[:12]
        items = [
            BatchItem(
                item_id=uuid.uuid4().hex[:16],
                batch_id=batch_id,
                seq=i,
                source_key=rec["source_key"],
                idempotency_key=derive_idempotency_key(flow.name, rec["source_key"]),
                record=rec,
            )
            for i, rec in enumerate(norm)
        ]
        return self.store.create_batch(
            batch_id=batch_id,
            batch_key=_batch_key(flow.name, norm),
            flow=flow.name,
            items=items,
            note=note,
        )

    # ------------------------------------------------------------------
    # 执行 / 恢复
    # ------------------------------------------------------------------

    def run_batch(self, batch_id: str, flow: Flow) -> dict[str, Any]:
        """推进所有 pending 记录。可重复调用：已完成的不受影响。

        每条记录每轮最多推进一次：处理失败回到 pending 的记录留到下一轮，
        避免目标系统持续故障时空转。
        """
        self._require_batch(batch_id)
        started = utcnow()
        error: str | None = None
        try:
            seen: set[str] = set()
            while True:
                item = self.store.claim_next_pending(
                    batch_id, self.worker_id, self.lease_seconds
                )
                if item is None:
                    break
                if item["item_id"] in seen:
                    self.store.release_running_to_pending(item["item_id"])
                    break
                seen.add(item["item_id"])
                self._execute_item(flow, item)
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            status = self.store.refresh_batch_status(batch_id)
            self._record_pass(batch_id, flow.name, "run", started, error)
        return self._summary(batch_id, status)

    def recover_batch(self, batch_id: str, flow: Flow) -> dict[str, Any]:
        """恢复：清扫僵尸租约 → 推进 pending → 逐条确认 unknown。

        只重试可证明安全的动作；无法自动确认的进入人工处置。
        """
        self._require_batch(batch_id)
        started = utcnow()
        error: str | None = None
        swept: dict[str, int] = {"running_to_pending": 0, "submitting_to_unknown": 0}
        try:
            swept = self.store.sweep_stale(batch_id)
            if swept["running_to_pending"] or swept["submitting_to_unknown"]:
                log.info("batch %s swept stale leases: %s", batch_id, swept)

            seen_pending: set[str] = set()
            while True:
                item = self.store.claim_next_pending(
                    batch_id, self.worker_id, self.lease_seconds
                )
                if item is None:
                    break
                if item["item_id"] in seen_pending:
                    self.store.release_running_to_pending(item["item_id"])
                    break
                seen_pending.add(item["item_id"])
                try:
                    self._execute_item(flow, item)
                except Exception:
                    log.exception("execute item %s failed during recover", item["item_id"])

            # 每条 unknown 记录每轮最多确认一次：未安置的留到下一轮恢复。
            seen_unknown: set[str] = set()
            while True:
                item = self.store.claim_next_unknown(
                    batch_id, self.worker_id, self.lease_seconds, self.reconcile_grace
                )
                if item is None:
                    break
                if item["item_id"] in seen_unknown:
                    self.store.release_lease_if_open(item["item_id"])
                    break
                seen_unknown.add(item["item_id"])
                try:
                    self._reconcile_item(flow, item)
                except Exception:
                    log.exception("reconcile item %s failed", item["item_id"])
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            status = self.store.refresh_batch_status(batch_id)
            self._record_pass(batch_id, flow.name, "recover", started, error)
        summary = self._summary(batch_id, status)
        summary["swept"] = swept
        return summary

    def abort_batch(self, batch_id: str, note: str = "aborted by operator") -> dict[str, Any]:
        """中止批次。补偿边界：

        - 从未产生提交意图的记录：直接取消。提交前没有任何外部副作用，
          补偿就是放弃，不需要也不能对目标系统做"反向操作"。
        - 提交后结果未知的记录：不能当作可补偿，转入人工处置。
        - 已有回执/明确失败的记录：保持原状。
        """
        self._require_batch(batch_id)
        self.store.sweep_stale(batch_id)
        if self.store.has_active_leases(batch_id):
            raise BatchBusy("batch has active workers, abort rejected")
        outcome = self.store.abort_open_items(batch_id, note)
        status = self.store.refresh_batch_status(batch_id)
        summary = self._summary(batch_id, status)
        summary["abort"] = outcome
        return summary

    def resolve_item(
        self,
        item_id: str,
        action: str,
        receipt_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """人工处置 needs_review / failed 记录。

        retry：重新排队。有提交意图的回到 unknown（只能走证据确认），
               没有的回到 pending（重放提交前步骤安全）。
        confirm：人工在目标系统核实后登记回执。回执必须在目标系统真实存在，
               否则拒绝——人工路径同样不允许凭空标记成功。
        cancel：只允许取消从未产生提交意图的记录（补偿边界）。
        """
        item = self.store.get_item(item_id)
        if item is None:
            raise BatchError(f"item not found: {item_id}")
        status = ItemStatus(item["status"])

        if action == "retry":
            if status not in (ItemStatus.FAILED, ItemStatus.NEEDS_REVIEW, ItemStatus.UNKNOWN):
                raise BatchError(f"cannot retry item in status {status.value}")
            self.store.requeue(item_id)
            self.store.record_attempt(item_id, "manual", "requeue", None, note)
        elif action == "confirm":
            if status not in (ItemStatus.NEEDS_REVIEW, ItemStatus.UNKNOWN, ItemStatus.FAILED):
                raise BatchError(f"cannot confirm item in status {status.value}")
            if not receipt_id:
                raise BatchError("confirm requires receipt_id")
            self._verify_receipt_at_target(receipt_id, item["source_key"])
            self.store.record_attempt(item_id, "manual", "confirmed", receipt_id, note)
            self.store.mark_succeeded(item_id, receipt_id, evidence="manual_confirmed")
        elif action == "cancel":
            if not self.store.cancel_pre_commit(item_id, note or "cancelled by operator"):
                raise BatchError(
                    "only items without a submit intent can be cancelled; "
                    "items with an unconfirmed submit must be confirmed or retried"
                )
            self.store.record_attempt(item_id, "manual", "cancelled", None, note)
        else:
            raise BatchError(f"unknown action: {action}")

        self.store.refresh_batch_status(item["batch_id"])
        return self.store.get_item(item_id)

    # ------------------------------------------------------------------
    # 单条记录：执行
    # ------------------------------------------------------------------

    def _execute_item(self, flow: Flow, item: dict[str, Any]) -> None:
        item_id = item["item_id"]
        record = json.loads(item["record_json"])
        steps = prepare_steps(flow, record)
        ctx = _ExecContext()
        ctx.values = dict(record)
        drv = make_driver()
        try:
            drv.start(flow)

            def on_step(idx: int, step: Step, phase: str, detail: str) -> None:
                try:
                    self.store.record_step(item_id, idx, step.kind.value, phase, detail)
                except Exception:
                    log.warning("record_step failed for %s", item_id, exc_info=True)

            def submit_handler(idx: int, step: Step, d: Any, c: _ExecContext) -> None:
                self._submit_for_item(item, step, d, c)

            run_prepared_steps(
                flow.name, steps, drv, ctx, on_step=on_step, submit_handler=submit_handler
            )
            if not ctx.submitted:
                # 流程没有真正走到 submit（如定义被改坏）：不能算成功，回到 pending。
                self.store.record_attempt(
                    item_id, "pre_submit", "no_submit_reached", None, ""
                )
                self.store.back_to_pending(item_id, "flow finished without a submit step")
        except ItemSettled:
            pass
        except DriverError as exc:
            if ctx.submitted:
                # 提交已确认，后续步骤（如回执断言）失败不改写提交结果。
                log.warning("post-submit step failed for %s: %s", item_id, exc)
                self.store.record_attempt(item_id, "post_submit", "step_failed", None, str(exc))
            else:
                # 提交前失败：无任何外部副作用，安全地回到 pending 等待下次推进。
                self.store.record_attempt(item_id, "pre_submit", "step_failed", None, str(exc))
                self.store.back_to_pending(item_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - 驱动创建等意外
            self.store.record_attempt(item_id, "pre_submit", "error", None, str(exc))
            self.store.back_to_pending(item_id, str(exc))
        finally:
            try:
                drv.close()
            except Exception:
                log.debug("driver close failed for %s", item_id, exc_info=True)

    def _submit_for_item(self, item: dict[str, Any], step: Step, drv: Any, ctx: _ExecContext) -> None:
        """批次路径的提交策略：写前日志 → 提交 → 按证据分类结果。"""
        item_id = item["item_id"]
        idem_key = item["idempotency_key"]
        payload = drv.build_payload()
        phash = payload_fingerprint(payload["form_type"], payload["source_key"], payload["fields"])

        # 写前日志：先落库，再调用目标系统。崩溃后恢复只认这份载荷。
        self.store.record_submit_intent(item_id, payload, phash)

        try:
            result = drv.submit(idempotency_key=idem_key, payload=payload)
        except SubmitRejected as exc:
            # 目标系统明确拒绝：同样的数据重试无意义，终态失败。
            self.store.record_attempt(item_id, "submit", "rejected", None, str(exc))
            self.store.mark_failed(item_id, f"target rejected: {exc}")
            raise ItemSettled
        except SubmitTransient as exc:
            # 超时/5xx/连接错误：目标系统可能已受理但回执丢失 → 未知。
            self.store.record_attempt(item_id, "submit", "transient", None, str(exc))
            self.store.mark_unknown(item_id, str(exc))
            raise ItemSettled
        except Exception as exc:  # noqa: BLE001 - 意图已落库，任何意外都按未知处理
            self.store.record_attempt(item_id, "submit", "error", None, str(exc))
            self.store.mark_unknown(item_id, f"unexpected submit error: {exc}")
            raise ItemSettled

        if result.replayed:
            # 目标系统的幂等反馈：同一身份已被受理。必须核对内容指纹——
            # 身份相同但内容不同是冲突，不能当作成功。
            if result.fields is not None:
                replay_hash = payload_fingerprint(
                    payload["form_type"], payload["source_key"], result.fields
                )
                if replay_hash != phash:
                    self.store.record_attempt(
                        item_id, "submit", "conflict", result.receipt_id,
                        "idempotent replay returned different payload",
                    )
                    self.store.mark_needs_review(
                        item_id,
                        f"目标系统已存在同一身份但内容不同的单据（回执 {result.receipt_id}）",
                    )
                    raise ItemSettled
            self.store.record_attempt(item_id, "submit", "replay", result.receipt_id, "")
            self.store.mark_succeeded(item_id, result.receipt_id, evidence="idempotent_replay")
        else:
            self.store.record_attempt(item_id, "submit", "receipt", result.receipt_id, "")
            self.store.mark_succeeded(item_id, result.receipt_id, evidence="submit_response")
        ctx.receipt_id = result.receipt_id
        ctx.submitted = True

    # ------------------------------------------------------------------
    # 单条记录：未知结果的证据确认
    # ------------------------------------------------------------------

    def _reconcile_item(self, flow: Flow, item: dict[str, Any]) -> None:
        try:
            self._reconcile_item_inner(flow, item)
        finally:
            # 确认轮次结束：未安置的记录必须放锁，让下一轮恢复能立即接管。
            self.store.release_lease_if_open(item["item_id"])

    def _reconcile_item_inner(self, flow: Flow, item: dict[str, Any]) -> None:
        item_id = item["item_id"]

        # 提交可能仍在途（如慢网关）：宽限期内不碰，避免把在途提交误判为"未受理"。
        last_submit = item.get("last_submit_at")
        if (
            last_submit is not None
            and self.reconcile_grace > 0
            and utcnow() - last_submit < timedelta(seconds=self.reconcile_grace)
        ):
            self.store.record_attempt(item_id, "reconcile", "grace_wait", None, "")
            return

        payload = json.loads(item["submit_payload"]) if item.get("submit_payload") else None
        if payload is None:
            # 没有意图日志的 unknown 不该出现；出现了就回到 pending 走安全重放。
            self.store.record_attempt(item_id, "reconcile", "no_intent", None, "")
            self.store.back_to_pending(item_id, "no submit intent recorded")
            return

        try:
            evidence = self._lookup_evidence(item)
        except Exception as exc:  # noqa: BLE001 - 查询通道本身不可用
            self.store.record_attempt(item_id, "reconcile", "query_failed", None, str(exc))
            n = self.store.bump_reconcile(item_id)
            if n >= self.max_reconcile_attempts:
                self.store.mark_needs_review(
                    item_id, f"目标系统查询通道不可用（{n} 次）：{exc}"
                )
            return

        if evidence is not None:
            # 目标系统证据：已受理。核对内容指纹后确认成功。
            ev_hash = payload_fingerprint(
                evidence["form_type"], evidence["source_key"], evidence.get("fields") or {}
            )
            if ev_hash != item["payload_hash"]:
                self.store.record_attempt(
                    item_id, "reconcile", "conflict", evidence["submission_id"],
                    "evidence payload mismatch",
                )
                self.store.mark_needs_review(
                    item_id,
                    f"目标系统存在同一身份但内容不同的单据（回执 {evidence['submission_id']}）",
                )
                return
            self.store.record_attempt(
                item_id, "reconcile", "confirmed", evidence["submission_id"], ""
            )
            self.store.mark_succeeded(
                item_id, evidence["submission_id"], evidence="target_lookup"
            )
            return

        # 目标系统查无记录：上一次提交未落地。用写前日志里的同一份载荷 +
        # 同一个幂等键重放——目标系统按幂等键去重，这是可证明安全的重试。
        self.store.record_attempt(item_id, "reconcile", "not_found", None, "")
        self.store.touch_last_submit(item_id)
        try:
            result = self._replay_submit(payload, item["idempotency_key"])
        except SubmitRejected as exc:
            self.store.record_attempt(item_id, "reconcile", "rejected", None, str(exc))
            self.store.mark_failed(item_id, f"target rejected on replay: {exc}")
            return
        except SubmitTransient as exc:
            self.store.record_attempt(item_id, "reconcile", "transient", None, str(exc))
            n = self.store.bump_reconcile(item_id)
            if n >= self.max_reconcile_attempts:
                self.store.mark_needs_review(
                    item_id, f"重放提交持续失败（{n} 次）：{exc}"
                )
            return
        if result.replayed and result.fields is not None:
            replay_hash = payload_fingerprint(
                payload["form_type"], payload["source_key"], result.fields
            )
            if replay_hash != item["payload_hash"]:
                self.store.record_attempt(
                    item_id, "reconcile", "conflict", result.receipt_id,
                    "replayed payload mismatch",
                )
                self.store.mark_needs_review(
                    item_id, f"重放命中不同内容的单据（回执 {result.receipt_id}）"
                )
                return
        outcome = "replay" if result.replayed else "receipt"
        self.store.record_attempt(item_id, "reconcile", outcome, result.receipt_id, "")
        self.store.mark_succeeded(
            item_id,
            result.receipt_id,
            evidence="idempotent_replay" if result.replayed else "retry_response",
        )

    def _lookup_evidence(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """向目标系统查询证据：先按幂等键，再按业务单号。"""
        with httpx.Client(base_url=settings.mockapp_url, timeout=10.0) as client:
            resp = client.get(
                "/api/submissions/lookup",
                params={"idempotency_key": item["idempotency_key"]},
            )
            resp.raise_for_status()
            found = resp.json().get("submissions") or []
            if found:
                return found[0]
            resp = client.get(
                "/api/submissions/lookup", params={"source_key": item["source_key"]}
            )
            resp.raise_for_status()
            found = resp.json().get("submissions") or []
            return found[0] if found else None

    def _replay_submit(self, payload: dict[str, Any], idem_key: str):
        """绕过界面步骤，直接用写前日志里的载荷重放提交。"""
        from .executor import HttpDriver

        drv = HttpDriver()
        try:
            return drv.submit(idempotency_key=idem_key, payload=dict(payload))
        finally:
            drv.close()

    def _verify_receipt_at_target(self, receipt_id: str, source_key: str) -> None:
        """人工确认也要拿目标系统的证据：回执页必须真实存在。"""
        try:
            with httpx.Client(base_url=settings.mockapp_url, timeout=10.0) as client:
                resp = client.get(f"/receipt/{receipt_id}")
        except Exception as exc:
            raise BatchError(f"cannot verify receipt at target: {exc}") from exc
        if resp.status_code != 200:
            raise BatchError(f"receipt {receipt_id} not found at target system")

    def _record_pass(
        self,
        batch_id: str,
        flow_name: str,
        kind: str,
        started: Any,
        error: str | None = None,
    ) -> None:
        """每个执行/恢复轮次在 runs 表留痕（v2 演进字段：batch_id / worker_id）。"""
        run = Run(
            run_id=uuid.uuid4().hex[:16],
            flow=flow_name,
            status=RunStatus.FAILED if error else RunStatus.SUCCEEDED,
            started_at=started,
            finished_at=utcnow(),
            error=error,
            attempts=1,
            batch_id=batch_id,
            worker_id=f"{self.worker_id}:{kind}",
        )
        try:
            self.store.save_run(run)
        except Exception:
            log.warning("persist batch pass failed", exc_info=True)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        out = dict(batch)
        out["counts"] = self.store.item_counts(batch_id)
        out["items"] = self.store.list_items(batch_id)
        return out

    def _require_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise BatchError(f"batch not found: {batch_id}")
        return batch

    def _summary(self, batch_id: str, status: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "status": status,
            "counts": self.store.item_counts(batch_id),
        }

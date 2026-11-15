"""流程定义、运行记录与批次生命周期的数据结构。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import yaml


class StepKind(str, Enum):
    OPEN = "open"
    FILL = "fill"
    SELECT = "select"
    CLICK = "click"
    SUBMIT = "submit"
    WAIT = "wait"
    ASSERT_TEXT = "assert_text"


@dataclass
class Step:
    """流程中的一个步骤。"""

    kind: StepKind
    selector: str | None = None
    value: str | None = None
    text: str | None = None
    # 显式幂等键。留空时执行引擎会按 (流程名, 业务单号) 派生稳定键。
    idempotency_key: str | None = None
    optional: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Step:
        kind = StepKind(raw["kind"])
        return cls(
            kind=kind,
            selector=raw.get("selector"),
            value=raw.get("value"),
            text=raw.get("text"),
            idempotency_key=raw.get("idempotency_key"),
            optional=bool(raw.get("optional", False)),
        )


@dataclass
class Flow:
    """一个可执行的业务流程。"""

    name: str
    description: str
    steps: list[Step]
    dataset: str | None = None

    @classmethod
    def from_yaml(cls, text: str) -> Flow:
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict) or "steps" not in raw:
            raise ValueError("flow yaml must contain a 'steps' list")
        return cls(
            name=raw["name"],
            description=raw.get("description", ""),
            steps=[Step.from_dict(s) for s in raw["steps"]],
            dataset=raw.get("dataset"),
        )

    @classmethod
    def from_file(cls, path: str) -> Flow:
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_yaml(fh.read())


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class Run:
    """一次执行记录。批次执行时每个执行轮次也会写一条（带 batch_id）。"""

    run_id: str
    flow: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    attempts: int = 0
    # 提交成功后目标系统返回的回执号，用于关联 submissions 表。
    receipt_id: str | None = None
    # v2 演进字段：批次执行轮次关联的批次与执行者。历史记录中为 NULL。
    batch_id: str | None = None
    worker_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "flow": self.flow,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "attempts": self.attempts,
            "receipt_id": self.receipt_id,
            "batch_id": self.batch_id,
            "worker_id": self.worker_id,
        }


class ItemStatus(str, Enum):
    """批次内单条业务记录的生命周期。

    状态机（commit point 是 submit 意图落库的那一刻）：

        pending ──认领──▶ running ──意图落库──▶ submitting ──回执──▶ succeeded
                          │  │                    │ 超时/5xx/崩溃
                          │  └──崩溃(租约过期)──▶ 回到 pending        ▼
                          │  （无副作用，重放安全）              unknown ──证据确认──▶ succeeded
                          ▼                                    │ 证据缺失/冲突
                  提交前失败 → 回到 pending                     ▼
                        （可证明安全，允许重试）            needs_review ──人工──▶ succeeded/cancelled

        failed：目标系统明确拒绝（4xx），重试同样的数据无意义，终态。
        cancelled：仅在 commit point 之前由人工中止产生，无任何外部副作用。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUBMITTING = "submitting"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


#: 恢复流程需要继续推进的开放状态。
ITEM_OPEN_STATUSES = {
    ItemStatus.PENDING,
    ItemStatus.RUNNING,
    ItemStatus.SUBMITTING,
    ItemStatus.UNKNOWN,
}

#: 真正终态（人工处置前不会再被任何自动路径触碰）。
ITEM_FINAL_STATUSES = {
    ItemStatus.SUCCEEDED,
    ItemStatus.FAILED,
    ItemStatus.CANCELLED,
}


class BatchStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    # 存在 unknown / needs_review 记录，需要恢复或人工介入。
    NEEDS_ATTENTION = "needs_attention"
    # 全部记录 succeeded。
    SUCCEEDED = "succeeded"
    # 全部记录到达终态，但含 failed/cancelled。
    COMPLETED = "completed"
    # 人工中止且剩余记录均已处置。
    ABORTED = "aborted"


@dataclass
class BatchItem:
    """批次内一条业务记录：稳定的提交身份 + 可恢复的生命周期。"""

    item_id: str
    batch_id: str
    seq: int
    source_key: str
    idempotency_key: str
    record: dict[str, Any]
    status: ItemStatus = ItemStatus.PENDING
    checkpoint_idx: int = -1
    receipt_id: str | None = None
    error: str | None = None
    attempts: int = 0


@dataclass
class SubmitResult:
    """一次提交的结果。replayed=True 表示目标系统按幂等键去重后返回了原回执。"""

    receipt_id: str
    replayed: bool = False
    # 目标系统回显的业务字段（用于与本地载荷指纹比对）。浏览器路径拿不到，为 None。
    fields: dict[str, str] | None = None


def canonical_json(obj: Any) -> str:
    """确定性序列化：用于指纹与批次键，键序与空白不影响结果。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def derive_idempotency_key(flow_name: str, source_key: str) -> str:
    """每条业务记录的稳定提交身份。

    只由 (流程, 业务单号) 决定，与进程、机器、尝试次数无关：
    同一记录无论重试、重启还是跨批次重投，身份都相同，目标系统据此去重。
    """
    return f"{flow_name}::{source_key}"


def payload_fingerprint(form_type: str, source_key: str, fields: dict[str, Any]) -> str:
    """提交载荷的内容指纹。用于检测"同一身份、不同内容"的冲突。"""
    canonical = canonical_json(
        {
            "form_type": form_type,
            "source_key": source_key,
            "fields": {k: str(v) for k, v in fields.items()},
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)

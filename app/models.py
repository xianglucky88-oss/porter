"""流程定义与运行记录的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    # 在 mockapp 上，提交步骤需要携带业务幂等键。
    # 当前流程定义中该字段均为空。
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
    """一次流程执行记录。

    `failure_class` 是排障与告警的分界线：
      * business   —— 目标系统拒绝，改数据后重跑；
      * flow       —— 流程与页面不匹配，改流程；
      * environment/transient/internal —— 运维问题，不应计入业务失败率。
    字符串 `error` 只用于人读，不能用于自动判定。
    """

    run_id: str
    flow: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    attempts: int = 0
    # 提交成功后目标系统返回的回执号，用于关联 submissions 表。
    receipt_id: str | None = None
    # 实际使用的驱动：http / browser。
    driver: str = "http"
    # 本次运行是否走了降级路径（如浏览器不可用后改用 HTTP）。
    degraded: bool = False
    degraded_reason: str | None = None
    failure_class: str | None = None
    failure_code: str | None = None
    failed_step: int | None = None
    # 失败证据（已脱敏）。只放定位环境问题所需的字段。
    evidence: dict[str, Any] = field(default_factory=dict)
    # 失败时内核给出的重试建议；仅当失败发生在提交前才可能为真。
    retryable: bool = False

    @property
    def is_business_failure(self) -> bool:
        return self.failure_class == "business"

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
            "driver": self.driver,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "failure_class": self.failure_class,
            "failure_code": self.failure_code,
            "failed_step": self.failed_step,
            "evidence": self.evidence,
            "retryable": self.retryable,
            # 供上游脚本直接判断，无需解析字符串。
            "business_failure": self.is_business_failure,
        }


def utcnow() -> datetime:
    return datetime.now(UTC)

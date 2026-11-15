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
    """流程中的一个步骤。

    定位方式二选一：
      * field    —— 逻辑字段名（推荐）。由 resolver 按确定优先级在
                    当前页面快照上解析，页面改版时自动回退。
      * selector —— 显式 CSS。仅用于无法抽象为逻辑字段的场景
                    （如回执页断言），页面改版不会自动适配。
    """

    kind: StepKind
    field: str | None = None
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
            field=raw.get("field"),
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
    # 字段可见标签的流程级覆盖：{逻辑字段名: [标签文本, ...]}。
    # 与全局注册表合并时以此为准。
    field_labels: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, text: str) -> Flow:
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict) or "steps" not in raw:
            raise ValueError("flow yaml must contain a 'steps' list")
        field_labels: dict[str, list[str]] = {}
        for fname, spec in (raw.get("fields") or {}).items():
            if isinstance(spec, dict) and spec.get("labels"):
                field_labels[fname] = [str(t) for t in spec["labels"]]
            elif isinstance(spec, list):
                field_labels[fname] = [str(t) for t in spec]
        return cls(
            name=raw["name"],
            description=raw.get("description", ""),
            steps=[Step.from_dict(s) for s in raw["steps"]],
            dataset=raw.get("dataset"),
            field_labels=field_labels,
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
    """一次流程执行记录。"""

    run_id: str
    flow: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    attempts: int = 0
    # 提交成功后目标系统返回的回执号，用于关联 submissions 表。
    receipt_id: str | None = None
    # 失败语义分类（failures.FailureKind 的 value）；成功时为 None。
    failure_kind: str | None = None
    # 执行证据（已脱敏）：页面版本特征、逐步定位决策与候选冲突。
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "flow": self.flow,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "failure_kind": self.failure_kind,
            "attempts": self.attempts,
            "receipt_id": self.receipt_id,
            "evidence": self.evidence,
        }


def utcnow() -> datetime:
    return datetime.now(UTC)

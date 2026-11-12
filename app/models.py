"""流程定义与运行记录的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    def from_dict(cls, raw: dict[str, Any]) -> "Step":
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
    def from_yaml(cls, text: str) -> "Flow":
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
    def from_file(cls, path: str) -> "Flow":
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
    # 运行记录只有状态与时间字段，没有步骤级快照。
    error: str | None = None
    attempts: int = 0
    # 提交成功后目标系统返回的回执号，用于关联 submissions 表。
    receipt_id: str | None = None

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
        }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

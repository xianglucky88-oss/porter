"""失败语义分类。

执行失败必须可归因到下列互斥语义之一，调用方（API / 运维）
据此决定是修流程、修页面知识库，还是联系业务方：

  * PAGE_LOAD_FAILED     目标页面加载失败（网络错误、非 2xx、超时）
  * STRUCTURE_CHANGED    页面结构变化：定位候选出现歧义，或提交控件
                         无法按已知规则唯一确定。拒绝猜测，整轮失败。
  * FIELD_MISSING        业务字段在页面上无任何候选命中
  * VALIDATION_REJECTED  目标系统业务校验拒绝（HTTP 422）
  * SUBMIT_FAILED        提交动作本身失败（5xx、连接中断等）
  * ASSERTION_FAILED     提交后断言不成立
  * FLOW_INVALID         流程定义自身不合法（静态校验未拦截到的）
  * UNKNOWN              未分类异常（兜底，出现即应视为缺陷排查）
"""

from __future__ import annotations

from enum import Enum


class FailureKind(str, Enum):
    PAGE_LOAD_FAILED = "page_load_failed"
    STRUCTURE_CHANGED = "structure_changed"
    FIELD_MISSING = "field_missing"
    VALIDATION_REJECTED = "validation_rejected"
    SUBMIT_FAILED = "submit_failed"
    ASSERTION_FAILED = "assertion_failed"
    FLOW_INVALID = "flow_invalid"
    UNKNOWN = "unknown"


class FlowFailure(Exception):
    """携带失败语义的执行期异常。

    step_index 便于在证据与日志中定位到具体步骤；kind 是唯一的
    分类依据，消息文本只用于人读，不作为程序判断依据。
    resolution 保存导致失败的定位评估（候选冲突明细），供证据采集。
    """

    def __init__(
        self,
        kind: FailureKind,
        message: str,
        step_index: int | None = None,
        resolution: object | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.step_index = step_index
        self.resolution = resolution

    def describe(self) -> str:
        prefix = f"step {self.step_index}: " if self.step_index is not None else ""
        return f"{prefix}[{self.kind.value}] {self}"


# ---------------------------------------------------------------------------
# 驱动层异常：驱动只抛这些类型，executor 负责映射到 FailureKind。
# 这样失败语义的判定集中在 executor 一处，两种驱动行为一致。
# ---------------------------------------------------------------------------


class PageLoadError(RuntimeError):
    """open/导航失败：网络错误、非 2xx 响应、超时。"""


class SubmitRejected(RuntimeError):
    """目标系统业务校验拒绝（HTTP 422）。消息含服务端返回的字段级原因。"""


class SubmitFailed(RuntimeError):
    """提交动作失败（5xx、连接中断、跳转超时等）。"""

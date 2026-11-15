"""失败分类。

原则：**只有目标系统明确拒绝（4xx 业务校验）才算业务失败**。
浏览器启动失败、沙箱/共享内存/字体缺失、驱动崩溃、下游不可达、超时与排队超限
都属于环境或瞬时故障，必须能和"表单填错了"区分开，否则重试和告警都会打错方向。

驱动层抛出下面的类型化异常；执行引擎不再靠字符串匹配猜测。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class FailureClass(str, Enum):
    """运行失败的归因。顺序即优先级：越靠前越"确定是业务问题"。"""

    # 目标系统返回明确的业务拒绝：422 必填校验、业务规则拒绝。
    BUSINESS = "business"
    # 流程定义与页面不匹配：选择器失效、断言失败、打开的表单不存在。
    FLOW = "flow"
    # 运行环境问题：浏览器缺失/崩溃、沙箱、共享内存、字体、下游不可达。
    ENVIRONMENT = "environment"
    # 可重试的瞬时问题：超时、5xx、并发槽位耗尽。
    TRANSIENT = "transient"
    # 未归类的程序错误。宁可归到这里，也不要误报成业务失败。
    INTERNAL = "internal"


class PorterError(RuntimeError):
    """带归因与证据的运行错误。"""

    failure_class: FailureClass = FailureClass.INTERNAL
    code: str = "internal"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
        retryable: bool | None = None,
        code: str | None = None,
        step_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.evidence: dict[str, Any] = evidence or {}
        self.step_index = step_index
        if retryable is not None:
            self.retryable = retryable
        if code is not None:
            self.code = code


class BusinessRejection(PorterError):
    """目标系统明确拒绝。不可重试，需要人看业务数据。"""

    failure_class = FailureClass.BUSINESS
    code = "business_rejected"
    retryable = False


class FlowDefinitionError(PorterError):
    """流程 YAML / 选择器与当前页面不一致。不可重试，需要改流程。"""

    failure_class = FailureClass.FLOW
    code = "flow_mismatch"
    retryable = False


class EnvironmentFailure(PorterError):
    """浏览器、系统依赖或下游环境不可用。"""

    failure_class = FailureClass.ENVIRONMENT
    code = "environment"
    retryable = False


class BrowserUnavailable(EnvironmentFailure):
    code = "browser_unavailable"


class BrowserCrashed(EnvironmentFailure):
    code = "browser_crashed"
    retryable = True


class TransientFailure(PorterError):
    """超时、5xx 等可以安全重试的问题。"""

    failure_class = FailureClass.TRANSIENT
    code = "transient"
    retryable = True


class SessionCapacityError(TransientFailure):
    """并发浏览器会话已达上限且排队超时。"""

    code = "session_capacity"


def failure_class_of(exc: BaseException) -> FailureClass:
    if isinstance(exc, PorterError):
        return exc.failure_class
    return FailureClass.INTERNAL


def retryable_of(exc: BaseException) -> bool:
    if isinstance(exc, PorterError):
        return exc.retryable
    return False


def code_of(exc: BaseException) -> str:
    if isinstance(exc, PorterError):
        return exc.code
    return "unclassified"


def evidence_of(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, PorterError):
        return dict(exc.evidence)
    return {}

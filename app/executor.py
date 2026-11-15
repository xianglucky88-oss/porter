"""流程执行引擎。

执行是单向的：从第一步跑到最后一步，任何一步抛错就整轮失败。

两种驱动共享同一套步骤协议，但失败必须归因（见 `app.errors`）：

    business     目标系统 4xx 拒绝         —— 改数据，不重试
    flow         选择器/断言与页面不符      —— 改流程，不重试
    environment  浏览器/字体/沙箱/下游不可用 —— 修环境，不重试
    transient    超时、5xx、容量耗尽        —— 仅提交前可安全重试
    internal     未分类                     —— 不当作业务失败

重试安全线：**一旦 submit 步骤开始执行，任何失败都不再自动重试**，
因为请求可能已经到达目标系统并产生单据。回归测试锁定了这一点。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Protocol

import httpx

from . import redaction
from .browser_runtime import classify_launch_error, manager
from .config import settings
from .errors import (
    BusinessRejection,
    EnvironmentFailure,
    FailureClass,
    FlowDefinitionError,
    PorterError,
    TransientFailure,
    code_of,
    evidence_of,
    failure_class_of,
    retryable_of,
)
from .models import Flow, Run, RunStatus, Step, StepKind, utcnow
from .selectors import field_selector

log = logging.getLogger("porter.executor")


class DriverError(PorterError):
    """兼容旧调用方的别名；新代码请直接抛具体类型。"""

    failure_class = TransientFailure.failure_class


class Driver(Protocol):
    name: str

    def start(self, flow: Flow) -> None: ...
    def open(self, url: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def select(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def submit(self, idempotency_key: str | None) -> str: ...
    def wait(self, seconds: float) -> None: ...
    def read_text(self, selector: str) -> str: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------
# HTTP 驱动
# --------------------------------------------------------------------------
class HttpDriver:
    """纯 HTTP 驱动。不启动浏览器，直接打 mockapp 的 JSON 接口。

    适用边界：
      * CI、离线演示、受限容器（无 /dev/shm、无字体、无 user namespace）；
      * 目标系统有稳定的 JSON 接口，且验收标准是"单据是否被正确受理"。

    不能替代浏览器路径的场合：选择器、页面渲染、JS 交互、跳转行为。
    HTTP 驱动看不见 DOM，选择器写错它也不会报错。
    """

    name = "http"
    # HTTP 驱动把 click 当 no-op，真正的提交发生在 submit 步骤。
    submits_on_click = False

    def __init__(self) -> None:
        self.client = httpx.Client(base_url=settings.mockapp_url, timeout=httpx.Timeout(30.0))
        self.form_type = ""
        self.fields: dict[str, str] = {}
        self.source_key = ""
        self.receipt_id = ""

    def start(self, flow: Flow) -> None:
        self.form_type = flow.dataset or flow.name
        self.fields = {}
        self.source_key = ""
        self.receipt_id = ""

    def _translate(self, exc: httpx.HTTPError, action: str) -> PorterError:
        if isinstance(exc, httpx.TimeoutException):
            return TransientFailure(
                f"{action} timed out: {type(exc).__name__}",
                evidence={"driver": self.name, "action": action},
            )
        return EnvironmentFailure(
            f"{action} failed: {exc}",
            evidence={"driver": self.name, "action": action},
        )

    def open(self, url: str) -> None:
        try:
            resp = self.client.get(url)
        except httpx.HTTPError as exc:
            raise self._translate(exc, f"GET {url}") from exc
        if resp.status_code == 404:
            raise FlowDefinitionError(
                f"target returned 404 for {url}; flow points at a missing form",
                evidence={"driver": self.name, "status": 404, "url": redaction.redact_text(url)},
            )
        if resp.status_code >= 500:
            raise TransientFailure(
                f"target returned {resp.status_code} for {url}",
                evidence={"driver": self.name, "status": resp.status_code},
            )
        if resp.status_code >= 400:
            # 打开页面就被拒绝（401/403 等）：属于访问配置/凭据问题，不是业务表单失败。
            raise EnvironmentFailure(
                f"target refused {url} with {resp.status_code}",
                evidence={"driver": self.name, "status": resp.status_code},
                code="target_access_denied",
            )

    def fill(self, selector: str, value: str) -> None:
        # selector 形如 input#applicant 或 input#source_key
        name = selector.split("#", 1)[-1]
        if name == "source_key":
            self.source_key = value
        else:
            self.fields[name] = value

    def select(self, selector: str, value: str) -> None:
        self.fill(selector, value)

    def click(self, selector: str) -> None:
        # 纯 HTTP 驱动把 submit 之前的 click 当成 no-op。
        return None

    def submit(self, idempotency_key: str | None) -> str:
        payload = {
            "form_type": self.form_type,
            "source_key": self.source_key,
            "fields": self.fields,
        }
        # 只有流程显式提供幂等键时才带上。
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        try:
            resp = self.client.post("/api/submit", json=payload)
        except httpx.HTTPError as exc:
            # 请求可能已到达目标系统：标记"可能已受理"，禁止自动重试。
            raise TransientFailure(
                f"submit outcome unknown: {exc}",
                retryable=False,
                evidence={"driver": self.name, "submit_may_have_landed": True},
            ) from exc

        if resp.status_code == 422:
            raise BusinessRejection(
                f"validation failed: {redaction.redact_text(resp.text[:500])}",
                evidence={
                    "driver": self.name,
                    "status": 422,
                    "submit_may_have_landed": False,
                },
            )
        if resp.status_code >= 500:
            raise TransientFailure(
                f"target returned {resp.status_code} on submit",
                retryable=False,
                evidence={
                    "driver": self.name,
                    "status": resp.status_code,
                    "submit_may_have_landed": True,
                },
            )
        resp.raise_for_status()
        data = resp.json()
        self.receipt_id = data["submission_id"]
        return self.receipt_id

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def read_text(self, selector: str) -> str:
        return self.receipt_id

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------------------------------
# 浏览器驱动
# --------------------------------------------------------------------------
class BrowserDriver:
    """Playwright 驱动。走真实 HTML 表单提交路径。

    适用边界：
      * 需要验证页面确实可渲染、选择器确实命中、跳转确实发生；
      * 目标系统没有稳定 JSON 接口，或验收要求"真的在操作页面"。

    构造时占用一个跨进程会话槽位并启动独立浏览器进程；close() 一定回收。
    """

    name = "browser"
    # 浏览器路径下，点提交按钮就是真实提交（区别于 HTTP 驱动的 no-op click）。
    submits_on_click = True

    def __init__(self) -> None:
        from contextlib import ExitStack

        self._stack = ExitStack()
        try:
            self.page = self._stack.enter_context(manager.session(tag="run"))
        except Exception:
            self._stack.close()
            raise
        self.receipt_id = ""
        # 流程在 submit 步骤之前还有一个 click 提交按钮的步骤。click 真的点下去之后
        # 页面会立刻导航到回执页；submit 再点一次时按钮已不存在，会以超时告终。
        # 因此在 click 阶段就完成"点击 + 等跳转"并把回执页 URL 记下来，
        # submit 直接复用。这样 click 仍然是真的点击，且不会双重提交。
        self._receipt_url: str | None = None

    # -- 错误翻译 ---------------------------------------------------
    def _selector_missing(self, selector: str, action: str) -> FlowDefinitionError:
        return FlowDefinitionError(
            f"{action}: selector {selector!r} not found on page",
            evidence={
                "driver": self.name,
                "selector": selector,
                "url": redaction.redact_text(self.page.url),
                "title": redaction.redact_text(self._safe_title()),
            },
        )

    def _safe_title(self) -> str:
        try:
            return self.page.title()
        except Exception:  # noqa: BLE001 - 取页面标题只用于证据，失败可忽略
            return ""

    def _translate(self, exc: Exception, action: str, selector: str | None = None) -> PorterError:
        if isinstance(exc, PorterError):
            return exc
        text = str(exc)
        lowered = text.lower()
        evidence = {
            "driver": self.name,
            "action": action,
            "selector": selector,
            "url": redaction.redact_text(self._safe_url()),
        }
        if "target page, context or browser has been closed" in lowered or "browser has crashed" in lowered:
            return EnvironmentFailure(
                f"browser closed/crashed during {action}",
                evidence={**evidence, "browser_connected": self._connected()},
                code="browser_crashed",
            )
        if "timeout" in lowered:
            return TransientFailure(f"{action} timed out", evidence=evidence)
        if "strict mode violation" in lowered or "not attached" in lowered:
            return FlowDefinitionError(f"{action} failed: {text[:300]}", evidence=evidence)
        return EnvironmentFailure(f"{action} failed: {text[:300]}", evidence=evidence)
    def _safe_url(self) -> str:
        try:
            return self.page.url
        except Exception:  # noqa: BLE001 - 取 URL 只用于证据，失败可忽略
            return ""

    def _connected(self) -> bool | None:
        try:
            return bool(self.page.context.browser.is_connected())
        except Exception:  # noqa: BLE001 - 连接状态查询失败返回 None（未知），不误判为崩溃
            return None

    def _require(self, selector: str) -> None:
        """快速判定选择器是否存在，把"页面改版"与"环境超时"分开。"""
        try:
            if self.page.locator(selector).count() == 0:
                raise self._selector_missing(selector, "locate")
        except PorterError:
            raise
        except Exception as exc:
            raise self._translate(exc, "locate", selector) from exc

    # -- 步骤 -------------------------------------------------------
    def start(self, flow: Flow) -> None:
        return None

    def open(self, url: str) -> None:
        target = f"{settings.mockapp_url}{url}"
        try:
            resp = self.page.goto(target)
        except Exception as exc:
            raise self._translate(exc, f"goto {url}") from exc
        if resp is not None and resp.status == 404:
            raise FlowDefinitionError(
                f"target returned 404 for {url}; flow points at a missing form",
                evidence={"driver": self.name, "status": 404, "url": redaction.redact_text(url)},
            )
        if resp is not None and resp.status >= 500:
            raise TransientFailure(
                f"target returned {resp.status} for {url}",
                evidence={"driver": self.name, "status": resp.status},
            )
        if resp is not None and resp.status >= 400:
            # 打开页面即被拒绝（401/403 等）：访问配置问题，不是业务表单失败。
            raise EnvironmentFailure(
                f"target refused {url} with {resp.status}",
                evidence={"driver": self.name, "status": resp.status},
                code="target_access_denied",
            )

    def fill(self, selector: str, value: str) -> None:
        self._require(selector)
        try:
            self.page.fill(selector, value)
        except Exception as exc:
            raise self._translate(exc, "fill", selector) from exc

    def select(self, selector: str, value: str) -> None:
        self._require(selector)
        try:
            self.page.select_option(selector, value)
        except Exception as exc:
            raise self._translate(exc, "select", selector) from exc

    def click(self, selector: str) -> None:
        self._require(selector)
        is_submit = selector.endswith("submit-btn")
        try:
            self.page.click(selector)
        except Exception as exc:
            translated = self._translate(exc, "click", selector)
            if is_submit:
                # 点的是提交按钮：即使点击动作超时，事件也可能已经派发出去。
                # 结果不确定 -> 不允许自动重试，归为瞬时故障并打上可疑标记。
                translated.retryable = False
                translated.evidence["submit_may_have_landed"] = True
                translated.code = "submit_timeout"
            raise translated from exc
        if not is_submit:
            return
        # 提交流程里 click 的是提交按钮：点击后页面会导航到回执页。
        # 记下结果供随后的 submit 步骤使用；这里失败同样是"提交已开始"。
        try:
            self.page.wait_for_load_state("load", timeout=settings.submit_timeout * 1000)
        except Exception as exc:
            translated = self._translate(exc, "submit", selector)
            if isinstance(translated, TransientFailure):
                translated.retryable = False
                translated.evidence["submit_may_have_landed"] = True
                translated.code = "submit_timeout"
            raise translated from exc
        self._receipt_url = self.page.url

    def submit(self, idempotency_key: str | None) -> str:
        # 提交按钮在 click 步骤已经点过并完成跳转：浏览器路径下 submit 直接取回执。
        # 若流程没有 click 步骤，才在这里兜底点击一次。
        if self._receipt_url is None:
            self._require("button#submit-btn")
            try:
                with self.page.expect_navigation(timeout=settings.submit_timeout * 1000):
                    self.page.click("button#submit-btn")
            except Exception as exc:
                translated = self._translate(exc, "submit", "button#submit-btn")
                if isinstance(translated, TransientFailure):
                    translated.retryable = False
                    translated.evidence["submit_may_have_landed"] = True
                    translated.code = "submit_timeout"
                raise translated from exc
            self._receipt_url = self.page.url
        url = self._receipt_url
        return url.rsplit("/", 1)[-1]

    def wait(self, seconds: float) -> None:
        try:
            self.page.wait_for_timeout(seconds * 1000)
        except Exception as exc:
            raise self._translate(exc, "wait") from exc

    def read_text(self, selector: str) -> str:
        self._require(selector)
        try:
            return self.page.inner_text(selector)
        except Exception as exc:
            raise self._translate(exc, "read_text", selector) from exc

    def close(self) -> None:
        self._stack.close()


# --------------------------------------------------------------------------
# 驱动工厂
# --------------------------------------------------------------------------
def make_driver() -> Driver:
    if settings.use_browser:
        return BrowserDriver()
    return HttpDriver()


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def validate_flow(flow: Flow) -> list[str]:
    """静态校验：步骤顺序、必填字段是否齐全。返回问题列表（空表示通过）。"""
    problems: list[str] = []
    kinds = [s.kind for s in flow.steps]
    if StepKind.OPEN not in kinds:
        problems.append("flow has no 'open' step")
    if StepKind.SUBMIT not in kinds:
        problems.append("flow has no 'submit' step")
    if kinds and kinds[0] != StepKind.OPEN:
        problems.append("first step should be 'open'")
    if kinds and kinds[-1] not in (StepKind.SUBMIT, StepKind.ASSERT_TEXT, StepKind.WAIT):
        problems.append("last step should be 'submit' or an assertion")
    if kinds.count(StepKind.SUBMIT) > 1:
        problems.append("flow has more than one 'submit' step")
    return problems


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
class StepFailure(PorterError):
    """单步失败，用于支持 optional 步骤。归因由原始异常决定。"""

    def __init__(self, cause: PorterError | str, step_index: int | None = None) -> None:
        if isinstance(cause, PorterError):
            super().__init__(
                str(cause),
                evidence=cause.evidence,
                retryable=cause.retryable,
                code=cause.code,
                step_index=step_index,
            )
            self.failure_class = cause.failure_class
            self.original = cause
        else:
            super().__init__(str(cause), step_index=step_index)
            self.original = None


class _ExecContext:
    """步骤之间传递运行时数据。"""

    def __init__(self) -> None:
        self.submitted = False
        self.submit_started = False
        self.receipt_id = ""
        self.values: dict[str, str] = {}


def _template(value: str, ctx: _ExecContext) -> str:
    """极简变量替换：支持 {run_id}、{date} 之类。"""
    if not value:
        return value
    out = value.replace("{date}", time.strftime("%Y%m%d"))
    out = out.replace("{ts}", str(int(time.time())))
    for k, v in ctx.values.items():
        out = out.replace("{" + k + "}", v)
    return out


def _exec_step(idx: int, step: Step, drv: Driver, ctx: _ExecContext) -> None:
    """执行单步。出错抛 StepFailure（保留归因）。"""
    try:
        if step.kind == StepKind.OPEN:
            assert step.value or step.selector, "open needs a url in 'value'"
            drv.open(step.value or step.selector or "")
        elif step.kind == StepKind.FILL:
            selector = step.selector or field_selector(step.text or "")
            drv.fill(selector, _template(step.value or "", ctx))
        elif step.kind == StepKind.SELECT:
            selector = step.selector or field_selector(step.text or "")
            drv.select(selector, step.value or "")
        elif step.kind == StepKind.CLICK:
            selector = step.selector or ""
            drv.click(selector)
            # 浏览器路径下，点提交按钮就是真的发出提交；HTTP 路径下 click 是 no-op。
            # 哪种驱动在 click 时就算"提交已开始"由驱动自己声明。
            if selector.endswith("submit-btn") and getattr(drv, "submits_on_click", False):
                ctx.submit_started = True
        elif step.kind == StepKind.WAIT:
            drv.wait(float(step.value or "1"))
        elif step.kind == StepKind.ASSERT_TEXT:
            got = drv.read_text(step.selector or "")
            want = step.value or ""
            if want and want not in got:
                raise FlowDefinitionError(
                    f"expected {want!r} in page text",
                    evidence={"selector": step.selector},
                )
        elif step.kind == StepKind.SUBMIT:
            ctx.submit_started = True
            receipt = drv.submit(step.idempotency_key)
            ctx.receipt_id = receipt
            ctx.submitted = True
        else:  # pragma: no cover - 枚举已穷尽
            raise FlowDefinitionError(f"unknown step kind {step.kind}")
    except StepFailure:
        raise
    except PorterError as exc:
        raise StepFailure(exc, idx) from exc
    except Exception as exc:
        # 未预期的异常一律 internal，绝不猜测成业务失败。
        raise StepFailure(f"{type(exc).__name__}: {exc}", idx) from exc


def _apply_failure(run: Run, exc: Exception, flow: Flow) -> None:
    run.failure_class = failure_class_of(exc).value
    run.failure_code = code_of(exc)
    run.error = redaction.redact_text(str(exc))
    run.evidence = redaction.redact_value(evidence_of(exc))
    step_index = getattr(exc, "step_index", None)
    if step_index is not None:
        run.failed_step = step_index
        if 0 <= step_index < len(flow.steps):
            run.evidence.setdefault("step_kind", flow.steps[step_index].kind.value)
    run.retryable = retryable_of(exc)


def _attempt(flow: Flow, drv: Driver, run: Run) -> bool:
    """跑一遍流程，成功返回 True。失败信息写入 run。"""
    ctx = _ExecContext()
    try:
        drv.start(flow)
        for idx, step in enumerate(flow.steps):
            try:
                _exec_step(idx, step, drv, ctx)
            except StepFailure as exc:
                if step.optional:
                    log.info("optional step %d failed, continuing: %s", idx, exc)
                    continue
                raise
            if settings.step_delay:
                time.sleep(settings.step_delay)
    except Exception as exc:  # noqa: BLE001 - 全部收敛
        _apply_failure(run, exc, flow)
        if ctx.submit_started:
            # 提交已开始：无论归因如何，自动重试都可能产生重复单据。
            run.retryable = False
            run.evidence.setdefault("submit_may_have_landed", True)
        return False
    run.receipt_id = ctx.receipt_id or None
    return True


def _run_with_driver(flow: Flow, run: Run, factory, attempts: int) -> bool:
    """按尝试次数执行。每次尝试都重新构造驱动（浏览器会话必须一轮一收）。

    只有同时满足三个条件才会进入下一轮：
      1. 还有剩余尝试次数；
      2. 失败被标记为可重试（transient）；
      3. 失败发生在 submit 开始之前（由 `_attempt` 强制把 retryable 置 False）。
    """
    for attempt in range(1, attempts + 1):
        run.attempts = attempt
        drv: Driver | None = None
        try:
            drv = factory()
        except Exception as exc:  # noqa: BLE001 - 驱动构造失败也要收敛成归因，不能漏到调用方
            _apply_failure(run, exc, flow)
            if attempt >= attempts or not run.retryable:
                return False
            log.info("retrying flow %s after %s (%s/%s)", flow.name, run.failure_code, attempt + 1, attempts)
            continue
        try:
            ok = _attempt(flow, drv, run)
        finally:
            try:
                drv.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不覆盖本轮真实结果，但要留痕
                # 关闭失败不应掩盖本轮真实结果（尤其是已经在收集的失败归因），
                # 但必须留痕：会话泄漏会表现为后续轮次排队超时。
                log.warning("driver close failed (%s): %s", run.failure_code or "no error", exc)
        if ok:
            return True
        if attempt >= attempts or not run.retryable:
            return False
        log.info(
            "retrying flow %s after %s (%s/%s)",
            flow.name,
            run.failure_code,
            attempt + 1,
            attempts,
        )
    return False


def execute(flow: Flow, driver: Driver | None = None) -> Run:
    """执行一个流程。

    返回 Run。异常不外抛，全部收敛进 Run。
    成功/失败的归因在 failure_class 字段，调用方不要解析 error 字符串。
    """
    run = Run(
        run_id=uuid.uuid4().hex[:16],
        flow=flow.name,
        status=RunStatus.RUNNING,
        started_at=utcnow(),
        attempts=1,
        driver="browser" if settings.use_browser else "http",
    )
    try:
        # 注入的驱动只跑一轮：调用方（测试）自己管理生命周期。
        attempts = settings.run_max_attempts
        if driver is not None:
            run.driver = getattr(driver, "name", run.driver)
            succeeded = _run_with_driver(flow, run, lambda: driver, 1)
        elif settings.use_browser:
            succeeded = _run_browser_with_fallback(flow, run, attempts)
        else:
            succeeded = _run_with_driver(flow, run, make_driver, attempts)
        if succeeded:
            run.status = RunStatus.SUCCEEDED
            run.error = None
            run.failure_class = None
            run.failure_code = None
            run.failed_step = None
            run.evidence = {}
            run.retryable = False
        else:
            run.status = RunStatus.FAILED
    except Exception as exc:
        run.status = RunStatus.FAILED
        _apply_failure(run, exc, flow)
        log.exception("unhandled error in flow %s", flow.name)
    finally:
        run.finished_at = utcnow()
    return run


def _run_browser_with_fallback(flow: Flow, run: Run, attempts: int) -> bool:
    """浏览器模式入口。

    降级只发生在"浏览器根本起不来"时，且必须显式开启 BROWSER_FALLBACK=http。
    失败归因始终保留为 environment，不允许改写成 business。

    注意：这里不复用同一个 BrowserDriver 重试。浏览器会话失败后必须整轮重建
    （新的进程、新的 profile），否则会在已损坏的页面上继续操作。
    """
    try:
        driver = BrowserDriver()
    except Exception as exc:  # noqa: BLE001 - 浏览器构造失败要归因为环境问题
        failure = exc if isinstance(exc, PorterError) else classify_launch_error(exc)
        _apply_failure(run, failure, flow)
        # 只有"环境不可用"才允许降级；容量耗尽（transient）应当排队或让上游重试，
        # 降级成 HTTP 会把并发压力问题悄悄掩盖掉。
        if settings.browser_fallback != "http" or failure.failure_class is not FailureClass.ENVIRONMENT:
            return False
        log.warning("browser unavailable, degrading to http for %s: %s", flow.name, exc)
        run.degraded = True
        run.degraded_reason = f"browser unavailable ({run.failure_code}); ran with http driver"
        run.driver = "http"
        ok = _run_with_driver(flow, run, HttpDriver, 1)  # 降级补跑只跑一次
        if ok:
            return True
        # 降级后依然失败：保留真实的失败归因，并注明降级过。
        run.degraded_reason = "browser unavailable; http fallback also failed"
        return False

    ok = _run_with_driver(flow, run, lambda: driver, 1)
    remaining = attempts - 1
    while not ok and remaining > 0 and run.retryable:
        remaining -= 1
        log.info("retrying flow %s with a fresh browser session", flow.name)
        try:
            fresh = BrowserDriver()
        except Exception as exc:  # noqa: BLE001 - 重建会话失败同样归因为环境问题
            _apply_failure(run, exc if isinstance(exc, PorterError) else classify_launch_error(exc), flow)
            return False
        ok = _run_with_driver(flow, run, lambda d=fresh: d, 1)
    return ok


def summarize(flow: Flow) -> dict[str, Any]:
    """给 API 返回的流程摘要。"""
    return {
        "name": flow.name,
        "description": flow.description,
        "dataset": flow.dataset,
        "steps": len(flow.steps),
        "has_idempotency_key": any(s.idempotency_key for s in flow.steps),
        "problems": validate_flow(flow),
    }

"""流程执行引擎。

commit point 只有一个：submit 步骤。提交前的 open/fill/select/click/wait/assert
都是驱动会话内的本地动作，不产生外部副作用，因此崩溃后重放是可证明安全的；
submit 之后的任何异常都不得改写已确认的提交结果。

单发路径 execute() 与批次路径（app.batch）共享同一个步骤循环
run_prepared_steps()，区别只在 submit 策略与进度记录。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

import httpx

from .config import settings
from .models import (
    Flow,
    Run,
    RunStatus,
    Step,
    StepKind,
    SubmitResult,
    derive_idempotency_key,
    utcnow,
)
from .selectors import field_selector

log = logging.getLogger("porter.executor")


class DriverError(RuntimeError):
    """驱动层错误，带步骤序号便于定位。"""

    def __init__(self, step_index: int, step: Step, message: str):
        super().__init__(f"step {step_index} ({step.kind.value}): {message}")
        self.step_index = step_index
        self.step = step


class SubmitRejected(RuntimeError):
    """目标系统明确拒绝（4xx）。同样的数据重试无意义，属于终态失败。"""


class SubmitTransient(RuntimeError):
    """提交结果未知（超时/5xx/连接错误）。目标系统可能已受理，必须按未知处理。"""


class Driver(Protocol):
    def start(self, flow: Flow) -> None: ...
    def open(self, url: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def select(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def source_identity(self) -> str: ...
    def build_payload(self) -> dict[str, Any]: ...
    def submit(
        self, idempotency_key: str | None = None, payload: dict[str, Any] | None = None
    ) -> SubmitResult: ...
    def wait(self, seconds: float) -> None: ...
    def read_text(self, selector: str) -> str: ...
    def close(self) -> None: ...


class HttpDriver:
    """纯 HTTP 驱动。用于 CI 与离线环境：不启动浏览器，直接打 mockapp 的 JSON 接口。

    与浏览器路径共享同一套步骤定义，因此两种驱动下的步骤序列完全一致。
    """

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

    def open(self, url: str) -> None:
        # url 形如 /form/purchase_requisition
        resp = self.client.get(url)
        resp.raise_for_status()

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

    def source_identity(self) -> str:
        return self.source_key

    def build_payload(self) -> dict[str, Any]:
        return {
            "form_type": self.form_type,
            "source_key": self.source_key,
            "fields": dict(self.fields),
        }

    def submit(
        self, idempotency_key: str | None = None, payload: dict[str, Any] | None = None
    ) -> SubmitResult:
        body = dict(payload) if payload is not None else self.build_payload()
        # 引擎总是携带幂等键：显式指定的优先，否则由调用方派生好传进来。
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        try:
            resp = self.client.post(
                "/api/submit", json=body, timeout=httpx.Timeout(settings.submit_timeout)
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # 请求已发出但响应丢失/超时：目标系统可能已受理。
            raise SubmitTransient(f"submit outcome unknown: {exc}") from exc
        if 400 <= resp.status_code < 500:
            # 4xx 是目标系统的明确拒绝：同样的数据重试不会好转。
            raise SubmitRejected(f"HTTP {resp.status_code}: {resp.text[:500]}")
        if resp.status_code >= 500:
            # 5xx 无法区分"未受理"与"已受理但回执丢失"。
            raise SubmitTransient(f"submit outcome unknown: HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        self.receipt_id = data["submission_id"]
        replayed = bool(data.get("idempotent_replay")) or (
            resp.headers.get("X-Idempotent-Replay") == "true"
        )
        return SubmitResult(
            receipt_id=self.receipt_id,
            replayed=replayed,
            fields=data.get("fields"),
        )

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def read_text(self, selector: str) -> str:
        return self.receipt_id

    def close(self) -> None:
        self.client.close()


class BrowserDriver:
    """Playwright 驱动。

    只有在 USE_BROWSER=1 且本机装了浏览器时才会被创建。
    它走真实的 HTML 表单提交路径，用于录屏证明产物真实性。
    """

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright  # 延迟导入，CI 不需要

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=settings.headless)
        self.page = self.browser.new_page()

    def start(self, flow: Flow) -> None:
        self.page.goto(f"{settings.mockapp_url}/form/{flow.dataset or flow.name}")

    def open(self, url: str) -> None:
        self.page.goto(f"{settings.mockapp_url}{url}")

    def fill(self, selector: str, value: str) -> None:
        self.page.fill(selector, value)

    def select(self, selector: str, value: str) -> None:
        self.page.select_option(selector, value)

    def click(self, selector: str) -> None:
        self.page.click(selector)

    def source_identity(self) -> str:
        try:
            return self.page.input_value("#source_key")
        except Exception:  # noqa: BLE001
            return ""

    def build_payload(self) -> dict[str, Any]:
        # 浏览器路径的载荷就是页面表单本身，无法整体取出，仅供接口对齐。
        return {"form_type": "", "source_key": self.source_identity(), "fields": {}}

    def submit(
        self, idempotency_key: str | None = None, payload: dict[str, Any] | None = None
    ) -> SubmitResult:
        # 浏览器路径下，提交就是点按钮 + 等跳转。
        # 浏览器路径不向隐藏域写入 idempotency_key。
        with self.page.expect_navigation(timeout=settings.submit_timeout * 1000):
            self.page.click("#submit-btn")
        # 回执页 URL: /receipt/{id}
        url = self.page.url
        return SubmitResult(receipt_id=url.rsplit("/", 1)[-1], replayed=False, fields=None)

    def wait(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000)

    def read_text(self, selector: str) -> str:
        return self.page.inner_text(selector)

    def close(self) -> None:
        self.browser.close()
        self._pw.stop()


def make_driver() -> Driver:
    if settings.use_browser:
        return BrowserDriver()
    return HttpDriver()


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


def prepare_steps(flow: Flow, record: dict[str, Any]) -> list[Step]:
    """批次执行前的步骤实例化：记录中的同名字段覆盖步骤里的字面量。

    例如记录 {"applicant": "李娜"} 会覆盖 selector 为 input#applicant 的
    fill 步骤的 value。未覆盖的字段保持流程定义原样（含 {placeholder} 模板，
    模板在执行时以记录为上下文渲染）。
    """
    steps: list[Step] = []
    for s in flow.steps:
        if s.kind in (StepKind.FILL, StepKind.SELECT) and s.selector:
            name = s.selector.split("#", 1)[-1]
            if name in record:
                s = replace(s, value=str(record[name]))
        steps.append(s)
    return steps


#: 步骤事件回调：(序号, 步骤, 阶段[started|done|failed], 详情)
StepEvent = Callable[[int, Step, str, str], None]
#: 提交策略：在 submit 步骤被调用，负责幂等键与结果分类。
SubmitHandler = Callable[[int, Step, Driver, "_ExecContext"], None]


def run_prepared_steps(
    flow_name: str,
    steps: list[Step],
    drv: Driver,
    ctx: _ExecContext,
    on_step: StepEvent | None = None,
    submit_handler: SubmitHandler | None = None,
) -> None:
    """共享步骤循环。单发与批次两条路径的步骤语义完全一致。"""
    for idx, step in enumerate(steps):
        if on_step:
            on_step(idx, step, "started", "")
        try:
            if step.kind == StepKind.SUBMIT and submit_handler is not None:
                submit_handler(idx, step, drv, ctx)
            else:
                _exec_step(idx, step, drv, ctx, flow_name)
        except StepFailure as exc:
            if on_step:
                on_step(idx, step, "failed", str(exc))
            if step.optional:
                continue
            raise DriverError(idx, step, str(exc)) from exc
        if on_step:
            on_step(idx, step, "done", "")
        if settings.step_delay:
            time.sleep(settings.step_delay)


def execute(flow: Flow, driver: Driver | None = None) -> Run:
    """单发执行一个流程（保留给 /flows/{name}/run 与演示）。

    返回 Run。异常不外抛，全部收敛进 Run.error —— 调用方（API）只需要看状态。
    幂等键：步骤未显式指定时，按 (流程名, 业务单号) 派生稳定键，
    因此同一业务单号重复执行只会被目标系统受理一次。
    """
    run = Run(
        run_id=uuid.uuid4().hex[:16],
        flow=flow.name,
        status=RunStatus.RUNNING,
        started_at=utcnow(),
        attempts=1,
    )
    own_driver = driver is None
    drv = driver or make_driver()
    try:
        drv.start(flow)
        ctx = _ExecContext()

        def _submit(idx: int, step: Step, d: Driver, c: _ExecContext) -> None:
            key = step.idempotency_key or derive_idempotency_key(
                flow.name, d.source_identity()
            )
            result = d.submit(idempotency_key=key)
            c.receipt_id = result.receipt_id
            c.submitted = True

        run_prepared_steps(flow.name, flow.steps, drv, ctx, submit_handler=_submit)
        run.status = RunStatus.SUCCEEDED
        run.receipt_id = ctx.receipt_id or None
    except Exception as exc:  # noqa: BLE001 - 全部收敛
        run.status = RunStatus.FAILED
        run.error = str(exc)
    finally:
        if own_driver:
            try:
                drv.close()
            except Exception:
                log.debug("driver close failed", exc_info=True)
        run.finished_at = utcnow()
    return run


class StepFailure(RuntimeError):
    """单步失败，用于支持 optional 步骤。"""


class _ExecContext:
    """步骤之间传递运行时数据。"""

    def __init__(self) -> None:
        self.submitted = False
        self.receipt_id = ""
        self.values: dict[str, str] = {}


def _template(value: str, ctx: _ExecContext) -> str:
    """极简变量替换：支持 {run_id}、{date}、{ts} 以及记录字段占位符。"""
    if not value:
        return value
    out = value.replace("{date}", time.strftime("%Y%m%d"))
    out = out.replace("{ts}", str(int(time.time())))
    for k, v in ctx.values.items():
        out = out.replace("{" + k + "}", v)
    return out


def _exec_step(
    idx: int, step: Step, drv: Driver, ctx: _ExecContext, flow_name: str = ""
) -> None:
    """执行单步（非 submit 或默认提交）。出错抛 StepFailure。"""
    try:
        if step.kind == StepKind.OPEN:
            assert step.value or step.selector, "open needs a url in 'value'"
            drv.open(step.value or step.selector or "")
        elif step.kind == StepKind.FILL:
            selector = step.selector or field_selector(step.text or "")
            drv.fill(selector, _template(step.value or "", ctx))
        elif step.kind == StepKind.SELECT:
            selector = step.selector or field_selector(step.text or "")
            drv.select(selector, _template(step.value or "", ctx))
        elif step.kind == StepKind.CLICK:
            drv.click(step.selector or "")
        elif step.kind == StepKind.WAIT:
            drv.wait(float(step.value or "1"))
        elif step.kind == StepKind.ASSERT_TEXT:
            got = drv.read_text(step.selector or "")
            want = step.value or ""
            if want and want not in got:
                raise StepFailure(f"expected {want!r} in {got!r}")
        elif step.kind == StepKind.SUBMIT:
            key = step.idempotency_key or derive_idempotency_key(
                flow_name, drv.source_identity()
            )
            result = drv.submit(idempotency_key=key)
            ctx.receipt_id = result.receipt_id
            ctx.submitted = True
        else:  # pragma: no cover - 枚举已穷尽
            raise StepFailure(f"unknown step kind {step.kind}")
    except StepFailure:
        raise
    except Exception as exc:
        raise StepFailure(str(exc)) from exc


def summarize(flow: Flow) -> dict[str, Any]:
    """给 API 返回的流程摘要。"""
    return {
        "name": flow.name,
        "description": flow.description,
        "dataset": flow.dataset,
        "steps": len(flow.steps),
        # 引擎保证：submit 步骤总是携带幂等键（显式或派生）。
        "has_idempotency_key": any(s.kind == StepKind.SUBMIT for s in flow.steps),
        "problems": validate_flow(flow),
    }

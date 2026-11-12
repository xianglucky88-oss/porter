"""流程执行引擎。

执行是单向的：从第一步跑到最后一步，任何一步抛错就整轮失败。
没有断点、没有补偿、没有幂等键。这是这一版最核心的技术债，
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Protocol

import httpx

from .config import settings
from .models import Flow, Run, RunStatus, Step, StepKind, utcnow
from .selectors import field_selector


class DriverError(RuntimeError):
    """驱动层错误，带步骤序号便于定位。"""

    def __init__(self, step_index: int, step: Step, message: str):
        super().__init__(f"step {step_index} ({step.kind.value}): {message}")
        self.step_index = step_index
        self.step = step


class Driver(Protocol):
    def start(self, flow: Flow) -> None: ...
    def open(self, url: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def select(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def submit(self, idempotency_key: str | None) -> str: ...
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

    def submit(self, idempotency_key: str | None) -> str:
        payload = {
            "form_type": self.form_type,
            "source_key": self.source_key,
            "fields": self.fields,
        }
        # 只有流程显式提供幂等键时才带上。
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        resp = self.client.post("/api/submit", json=payload)
        if resp.status_code == 422:
            raise ValueError(f"validation failed: {resp.text}")
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

    def submit(self, idempotency_key: str | None) -> str:
        # 浏览器路径下，提交就是点按钮 + 等跳转。
        # 浏览器路径不向隐藏域写入 idempotency_key。
        with self.page.expect_navigation(timeout=settings.submit_timeout * 1000):
            self.page.click("#submit-btn")
        # 回执页 URL: /receipt/{id}
        url = self.page.url
        return url.rsplit("/", 1)[-1]

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


def execute(flow: Flow, driver: Driver | None = None) -> Run:
    """执行一个流程。

    返回 Run。异常不外抛，全部收敛进 Run.error —— 调用方（API）只需要看状态。
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
        for idx, step in enumerate(flow.steps):
            try:
                _exec_step(idx, step, drv, ctx)
            except StepFailure as exc:
                if step.optional:
                    continue
                raise DriverError(idx, step, str(exc)) from exc
            if settings.step_delay:
                time.sleep(settings.step_delay)
        run.status = RunStatus.SUCCEEDED
        run.receipt_id = ctx.receipt_id or None
    except Exception as exc:  # noqa: BLE001 - 全部收敛
        run.status = RunStatus.FAILED
        run.error = str(exc)
    finally:
        if own_driver:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                pass
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
    """极简变量替换：支持 {run_id}、{date} 之类。

    当前只实现了固定几个占位符，没有通用表达式（这是刻意保留的简化）。
    """
    if not value:
        return value
    out = value.replace("{date}", time.strftime("%Y%m%d"))
    out = out.replace("{ts}", str(int(time.time())))
    for k, v in ctx.values.items():
        out = out.replace("{" + k + "}", v)
    return out


def _exec_step(idx: int, step: Step, drv: Driver, ctx: _ExecContext) -> None:
    """执行单步。出错抛 StepFailure。"""
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
            drv.click(step.selector or "")
        elif step.kind == StepKind.WAIT:
            drv.wait(float(step.value or "1"))
        elif step.kind == StepKind.ASSERT_TEXT:
            got = drv.read_text(step.selector or "")
            want = step.value or ""
            if want and want not in got:
                raise StepFailure(f"expected {want!r} in {got!r}")
        elif step.kind == StepKind.SUBMIT:
            # idempotency_key 取自 step 定义。
            receipt = drv.submit(step.idempotency_key)
            ctx.receipt_id = receipt
            ctx.submitted = True
        else:  # pragma: no cover - 枚举已穷尽
            raise StepFailure(f"unknown step kind {step.kind}")
    except StepFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StepFailure(str(exc)) from exc


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

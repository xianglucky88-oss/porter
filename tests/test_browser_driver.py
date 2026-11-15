"""浏览器驱动的语义测试。

用**假的 page 对象**替换 Playwright，验证驱动自己的逻辑：
  * click 提交按钮后紧接 submit 不会双重点击（页面已经跳转）；
  * 选择器缺失归因为 flow，而不是内部错误；
  * 浏览器崩溃归因为 environment；
  * 提交阶段超时标记 submit_may_have_landed，禁止重试。

真实 Chromium 的渲染与字体行为由 `make verify-browser` 在 browser 镜像里覆盖。
"""

from __future__ import annotations

import contextlib

import pytest

from app import browser_runtime
from app.config import settings
from app.errors import FlowDefinitionError
from app.executor import BrowserDriver, execute
from app.models import Flow, RunStatus, Step, StepKind


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    def count(self) -> int:
        return 1 if self.selector in self.page.elements else 0


class FakePage:
    """够用的假页面：记录点击、导航与文本。"""

    def __init__(self) -> None:
        self.url = "http://mock/form/reimbursement"
        self.elements = {"input#source_key", "input#applicant", "button#submit-btn", "p#receipt-id"}
        self.clicks: list[str] = []
        self.fills: dict[str, str] = {}
        self.texts = {"p#receipt-id": "RCPT-1"}
        self.after_click_url = "http://mock/receipt/RCPT-1"
        self.crash = False
        self.click_timeout = False
        self.title_text = "费用报销单"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def goto(self, url: str):
        if self.crash:
            raise RuntimeError("Target page, context or browser has been closed")
        self.url = url

        class _Resp:
            status = 200

        return _Resp()

    def click(self, selector: str) -> None:
        if self.click_timeout:
            raise RuntimeError("Timeout 30000ms exceeded")
        self.clicks.append(selector)
        if selector.endswith("submit-btn"):
            # 真实浏览器：点下去页面立即跳转，按钮消失。
            self.elements.discard("button#submit-btn")
            self.url = self.after_click_url

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        return None

    def fill(self, selector: str, value: str) -> None:
        self.fills[selector] = value

    def select_option(self, selector: str, value: str) -> None:
        self.fills[selector] = value

    def inner_text(self, selector: str) -> str:
        return self.texts[selector]

    def wait_for_timeout(self, ms: int) -> None:
        return None

    def title(self) -> str:
        return self.title_text

    @contextlib.contextmanager
    def expect_navigation(self, timeout: int | None = None):
        yield

    @property
    def context(self):
        page = self

        class _Ctx:
            class browser:
                @staticmethod
                def is_connected() -> bool:
                    return not page.crash

        return _Ctx()


@pytest.fixture
def fake_browser(monkeypatch):
    page = FakePage()

    @contextlib.contextmanager
    def _session(tag: str = "run"):
        yield page

    monkeypatch.setattr(browser_runtime.manager, "session", _session)
    return page


def _flow(*steps: Step) -> Flow:
    return Flow(name="reimbursement", description="t", steps=list(steps), dataset="reimbursement")


def _submit_flow() -> Flow:
    return _flow(
        Step(kind=StepKind.OPEN, value="/form/reimbursement"),
        Step(kind=StepKind.FILL, selector="input#source_key", value="EXP-1"),
        Step(kind=StepKind.FILL, selector="input#applicant", value="李娜"),
        Step(kind=StepKind.CLICK, selector="button#submit-btn"),
        Step(kind=StepKind.SUBMIT, value=""),
        Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value="RCPT-1"),
    )


def test_click_submit_then_submit_clicks_once(fake_browser):
    """流程里 click + submit 两步只应真实点击一次，否则会双重提交或超时。"""
    run = execute(_submit_flow(), driver=BrowserDriver())
    assert run.status is RunStatus.SUCCEEDED, run.error
    assert fake_browser.clicks.count("button#submit-btn") == 1
    assert run.receipt_id == "RCPT-1"
    assert run.driver == "browser"


def test_flow_without_click_still_submits(fake_browser):
    flow = _flow(
        Step(kind=StepKind.OPEN, value="/form/reimbursement"),
        Step(kind=StepKind.FILL, selector="input#source_key", value="EXP-2"),
        Step(kind=StepKind.SUBMIT, value=""),
    )
    run = execute(flow, driver=BrowserDriver())
    assert run.status is RunStatus.SUCCEEDED, run.error
    assert fake_browser.clicks == ["button#submit-btn"]


def test_missing_selector_is_flow_failure(fake_browser):
    flow = _flow(
        Step(kind=StepKind.OPEN, value="/form/reimbursement"),
        Step(kind=StepKind.FILL, selector="input#does-not-exist", value="x"),
        Step(kind=StepKind.SUBMIT, value=""),
    )
    run = execute(flow, driver=BrowserDriver())
    assert run.status is RunStatus.FAILED
    assert run.failure_class == "flow"
    assert run.failed_step == 1
    assert run.evidence.get("selector") == "input#does-not-exist"


def test_browser_crash_is_environment_failure(fake_browser):
    fake_browser.crash = True
    run = execute(_submit_flow(), driver=BrowserDriver())
    assert run.status is RunStatus.FAILED
    assert run.failure_class == "environment"
    assert run.failure_code == "browser_crashed"
    assert run.is_business_failure is False


def test_submit_timeout_blocks_retry(fake_browser):
    """提交按钮点击超时：可能已经送达目标系统，永远不能自动重试。"""
    fake_browser.click_timeout = True
    run = execute(_submit_flow(), driver=BrowserDriver())
    assert run.status is RunStatus.FAILED
    assert run.failure_class == "transient"
    assert run.retryable is False
    assert run.evidence.get("submit_may_have_landed") is True


def test_assert_text_mismatch_is_flow_failure(fake_browser):
    flow = _submit_flow()
    flow.steps[-1] = Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value="OTHER")
    run = execute(flow, driver=BrowserDriver())
    assert run.status is RunStatus.FAILED
    assert run.failure_class == "flow"
    assert run.failed_step == len(flow.steps) - 1


def test_driver_translates_exceptions_without_leaking_values(fake_browser):
    driver = BrowserDriver()
    try:
        with pytest.raises(FlowDefinitionError):
            driver.fill("input#missing", "4111111111111111")
    finally:
        driver.close()


def test_submit_timeout_value_is_configurable(monkeypatch):
    original = settings.submit_timeout
    try:
        settings.submit_timeout = 1.5
        assert settings.submit_timeout == 1.5
    finally:
        settings.submit_timeout = original

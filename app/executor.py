"""流程执行引擎。

执行是单向的：从第一步跑到最后一步，任何一步抛错就整轮失败。
没有断点、没有补偿、没有幂等键。这是这一版最核心的技术债，

本版完成的改造：
  * 字段定位从"写死的 CSS"改为"页面快照 + 多候选解析"（resolver），
    回退优先级确定、歧义拒绝，绝不靠猜测继续提交；
  * 失败语义分类（failures.FailureKind）：页面加载失败 / 结构变化 /
    字段缺失 / 业务校验拒绝 / 提交失败，分别对应不同的处置路径；
  * 两种驱动（HTTP / 浏览器）共享同一份页面解析器与解析规则，
    同一流程在两种驱动下的定位决策一致；
  * 每轮执行产出脱敏证据（evidence）：最终采用的定位规则、候选
    冲突、页面版本特征，可用于事后排查。
"""

from __future__ import annotations

import contextlib
import time
import uuid
from typing import Any, Protocol

import httpx

from .config import settings
from .evidence import EvidenceCollector, sanitize_run_error
from .failures import (
    FailureKind,
    FlowFailure,
    PageLoadError,
    SubmitFailed,
    SubmitRejected,
)
from .field_registry import labels_for
from .models import Flow, Run, RunStatus, Step, StepKind, utcnow
from .pageview import PageView, parse_page
from .resolver import Resolution, resolve_field, resolve_submit


class DriverError(RuntimeError):
    """驱动层错误，带步骤序号便于定位。（兼容旧调用方，内部已改用 FlowFailure）"""

    def __init__(self, step_index: int, step: Step, message: str):
        super().__init__(f"step {step_index} ({step.kind.value}): {message}")
        self.step_index = step_index
        self.step = step


class Locator:
    """一次定位的产出：驱动各取所需。

    HTTP 驱动用 key（业务字段名）组装 JSON；浏览器驱动用 css 找元素。
    """

    def __init__(self, key: str, css: str, strategy: str) -> None:
        self.key = key
        self.css = css
        self.strategy = strategy

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"Locator(key={self.key!r}, css={self.css!r}, via={self.strategy})"


class Driver(Protocol):
    def start(self, flow: Flow) -> None: ...
    def open(self, url: str) -> None: ...
    def inspect(self) -> PageView: ...
    def fill(self, locator: Locator, value: str) -> None: ...
    def select(self, locator: Locator, value: str) -> None: ...
    def click(self, locator: Locator) -> None: ...
    def submit(self, idempotency_key: str | None, submitter_css: str | None = None) -> str: ...
    def wait(self, seconds: float) -> None: ...
    def read_text(self, selector: str) -> str: ...
    def close(self) -> None: ...


class HttpDriver:
    """纯 HTTP 驱动。用于 CI 与离线环境：不启动浏览器，直接打 mockapp 的 JSON 接口。

    与浏览器路径共享同一套页面解析器（pageview.parse_page）与定位规则
    （resolver），因此两种驱动下同一流程的定位决策完全一致。
    """

    def __init__(self) -> None:
        self.client = httpx.Client(base_url=settings.mockapp_url, timeout=httpx.Timeout(30.0))
        self.form_type = ""
        self.fields: dict[str, str] = {}
        self.source_key = ""
        self.receipt_id = ""
        self._html = ""
        self._url = ""

    def start(self, flow: Flow) -> None:
        self.form_type = flow.dataset or flow.name
        self.fields = {}
        self.source_key = ""
        self.receipt_id = ""
        self._html = ""
        self._url = ""

    def open(self, url: str) -> None:
        # url 形如 /form/purchase_requisition
        try:
            resp = self.client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise PageLoadError(f"open {url} failed: {exc}") from exc
        self._html = resp.text
        self._url = url

    def inspect(self) -> PageView:
        return parse_page(self._html, url=self._url)

    def fill(self, locator: Locator, value: str) -> None:
        # JSON 提交按业务字段名组织，与页面上的 id/name 形态无关。
        if locator.key == "source_key":
            self.source_key = value
        else:
            self.fields[locator.key] = value

    def select(self, locator: Locator, value: str) -> None:
        self.fill(locator, value)

    def click(self, locator: Locator) -> None:
        # 纯 HTTP 驱动把 submit 之前的 click 当成 no-op。
        return None

    def submit(self, idempotency_key: str | None, submitter_css: str | None = None) -> str:
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
            raise SubmitFailed(f"submit request failed: {exc}") from exc
        if resp.status_code == 422:
            # 业务校验拒绝：服务端返回的 detail 只含字段名，不含取值。
            raise SubmitRejected(f"validation failed (422): {resp.text}")
        if resp.status_code >= 400:
            raise SubmitFailed(f"submit returned {resp.status_code}: {resp.text[:200]}")
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
    页面快照同样走 parse_page（对 page.content() 的 HTML 解析），
    保证与 HTTP 驱动的定位决策一致。
    """

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright  # 延迟导入，CI 不需要

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=settings.headless)
        self.page = self.browser.new_page()

    def start(self, flow: Flow) -> None:
        try:
            self.page.goto(f"{settings.mockapp_url}/form/{flow.dataset or flow.name}")
        except Exception as exc:
            raise PageLoadError(f"open form page failed: {exc}") from exc

    def open(self, url: str) -> None:
        try:
            self.page.goto(f"{settings.mockapp_url}{url}")
        except Exception as exc:
            raise PageLoadError(f"open {url} failed: {exc}") from exc

    def inspect(self) -> PageView:
        return parse_page(self.page.content(), url=self.page.url)

    def fill(self, locator: Locator, value: str) -> None:
        try:
            self.page.fill(locator.css, value)
        except Exception as exc:
            # 解析出的选择器在真实页面上失效：页面在我们快照之后又变了。
            raise FlowFailure(
                FailureKind.STRUCTURE_CHANGED,
                f"resolved selector {locator.css!r} not usable in browser: {exc}",
            ) from exc

    def select(self, locator: Locator, value: str) -> None:
        try:
            self.page.select_option(locator.css, value)
        except Exception as exc:
            raise FlowFailure(
                FailureKind.STRUCTURE_CHANGED,
                f"resolved selector {locator.css!r} not usable in browser: {exc}",
            ) from exc

    def click(self, locator: Locator) -> None:
        try:
            self.page.click(locator.css)
        except Exception as exc:
            raise FlowFailure(
                FailureKind.STRUCTURE_CHANGED,
                f"resolved selector {locator.css!r} not clickable: {exc}",
            ) from exc

    def submit(self, idempotency_key: str | None, submitter_css: str | None = None) -> str:
        # 浏览器路径下，提交就是点按钮 + 等跳转。
        # 浏览器路径不向隐藏域写入 idempotency_key。
        css = submitter_css or "#submit-btn"
        try:
            with self.page.expect_navigation(timeout=settings.submit_timeout * 1000):
                self.page.click(css)
        except Exception as exc:
            raise SubmitFailed(f"submit click/navigation failed: {exc}") from exc
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
    for i, s in enumerate(flow.steps):
        if s.kind in (StepKind.FILL, StepKind.SELECT) and not (s.field or s.selector):
            problems.append(f"step {i} ({s.kind.value}) needs 'field' or 'selector'")
        if s.kind == StepKind.CLICK and not (s.field or s.selector):
            problems.append(f"step {i} (click) needs 'field' or 'selector'")
    return problems


def execute(flow: Flow, driver: Driver | None = None) -> Run:
    """执行一个流程。

    返回 Run。异常不外抛，全部收敛进 Run.error / Run.failure_kind ——
    调用方（API）只需要看状态与分类。证据（脱敏）挂在 Run.evidence。
    """
    run = Run(
        run_id=uuid.uuid4().hex[:16],
        flow=flow.name,
        status=RunStatus.RUNNING,
        started_at=utcnow(),
        attempts=1,
    )
    ev = EvidenceCollector()
    own_driver = driver is None
    drv = driver or make_driver()
    try:
        try:
            drv.start(flow)
        except PageLoadError:
            raise
        except Exception as exc:
            raise PageLoadError(f"driver start failed: {exc}") from exc
        ctx = _ExecContext(drv, flow, ev)
        for idx, step in enumerate(flow.steps):
            try:
                _exec_step(idx, step, ctx)
            except FlowFailure as exc:
                exc.step_index = exc.step_index if exc.step_index is not None else idx
                ev.record_step(
                    idx,
                    step.kind.value,
                    field=step.field,
                    resolution=exc.resolution if isinstance(exc.resolution, Resolution) else None,
                    ok=False,
                    error=exc.describe(),
                )
                if step.optional:
                    ev.steps[-1].skipped_optional = True
                    continue
                raise
            if settings.step_delay:
                time.sleep(settings.step_delay)
        run.status = RunStatus.SUCCEEDED
        run.receipt_id = ctx.receipt_id or None
    except FlowFailure as exc:
        run.status = RunStatus.FAILED
        run.failure_kind = exc.kind.value
        run.error = sanitize_run_error(exc.describe(), ev.secrets)
    except PageLoadError as exc:
        # drv.start() 阶段的导航失败：与 open 步骤失败同一语义。
        run.status = RunStatus.FAILED
        run.failure_kind = FailureKind.PAGE_LOAD_FAILED.value
        run.error = sanitize_run_error(str(exc), ev.secrets)
    except Exception as exc:  # noqa: BLE001 - 全部收敛
        run.status = RunStatus.FAILED
        run.failure_kind = FailureKind.UNKNOWN.value
        run.error = sanitize_run_error(str(exc), ev.secrets)
    finally:
        if own_driver:
            # 驱动关闭失败不影响本轮已确定的执行结论。
            with contextlib.suppress(Exception):
                drv.close()
        run.evidence = ev.build()
        run.finished_at = utcnow()
    return run


class _ExecContext:
    """步骤之间传递运行时数据。"""

    def __init__(self, drv: Driver, flow: Flow, ev: EvidenceCollector) -> None:
        self.drv = drv
        self.flow = flow
        self.ev = ev
        self.submitted = False
        self.receipt_id = ""
        self.values: dict[str, str] = {}
        self._page: PageView | None = None

    def page(self) -> PageView:
        """当前页面快照（缓存）。open/submit 之后由执行器置 None。"""
        if self._page is None:
            self._page = self.drv.inspect()
        return self._page

    def invalidate_page(self) -> None:
        self._page = None


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


def _explicit_locator(step: Step) -> Locator:
    """显式 selector 的兼容路径：从 CSS 反推业务键（input#foo → foo）。"""
    css = step.selector or ""
    key = css.split("#", 1)[-1] if "#" in css else css
    return Locator(key=key, css=css, strategy="explicit")


def _resolve_step_locator(step: Step, ctx: _ExecContext) -> tuple[Locator, Resolution]:
    """把 fill/select/click 步骤解析为 Locator。

    显式 selector 优先（兼容旧流程）；否则走 resolver 的多候选回退。
    解析失败按语义抛 FlowFailure：歧义 → STRUCTURE_CHANGED，
    零命中 → FIELD_MISSING。
    """
    if step.selector and not step.field:
        loc = _explicit_locator(step)
        return loc, Resolution(key=loc.key, ok=True, css=loc.css, strategy="explicit")

    key = step.field or ""
    if not key:
        raise FlowFailure(FailureKind.FLOW_INVALID, "step needs 'field' or 'selector'")

    if key == "submit":
        res = resolve_submit(ctx.page())
    else:
        labels = labels_for(key, ctx.flow.field_labels)
        res = resolve_field(ctx.page(), key, labels)

    if res.ok and res.css:
        return Locator(key=key, css=res.css, strategy=res.strategy or ""), res

    if res.failure == "ambiguous_match":
        raise FlowFailure(
            FailureKind.STRUCTURE_CHANGED,
            f"ambiguous candidates for {key!r}: {res.detail}",
            resolution=res,
        )
    raise FlowFailure(
        FailureKind.FIELD_MISSING,
        f"field {key!r} not found on page: {res.detail}",
        resolution=res,
    )


def _exec_step(idx: int, step: Step, ctx: _ExecContext) -> None:
    """执行单步。出错抛 FlowFailure（带失败语义）。"""
    kind = step.kind
    if kind == StepKind.OPEN:
        url = step.value or step.selector or ""
        if not url:
            raise FlowFailure(FailureKind.FLOW_INVALID, "open needs a url in 'value'")
        try:
            ctx.drv.open(url)
        except PageLoadError as exc:
            raise FlowFailure(FailureKind.PAGE_LOAD_FAILED, str(exc)) from exc
        ctx.invalidate_page()
        # open 之后立即留页面版本特征：后续任何失败都能对应到页面形态。
        try:
            ctx.ev.record_page(ctx.page(), url)
        except FlowFailure:
            raise
        except Exception as exc:
            raise FlowFailure(FailureKind.PAGE_LOAD_FAILED, f"inspect failed: {exc}") from exc
        ctx.ev.record_step(idx, kind.value, ok=True)
        return

    if kind in (StepKind.FILL, StepKind.SELECT):
        value = _template(step.value or "", ctx)
        # 取值先登记为敏感，再参与任何可能产生日志/证据的操作。
        ctx.ev.register_secret(value)
        locator, res = _resolve_step_locator(step, ctx)
        if kind == StepKind.FILL:
            ctx.drv.fill(locator, value)
        else:
            ctx.drv.select(locator, value)
        ctx.values[step.field or locator.key] = value
        ctx.ev.record_step(
            idx, kind.value, field=step.field or locator.key, resolution=res, value=value
        )
        return

    if kind == StepKind.CLICK:
        locator, res = _resolve_step_locator(step, ctx)
        ctx.drv.click(locator)
        ctx.ev.record_step(idx, kind.value, field=step.field or locator.key, resolution=res)
        return

    if kind == StepKind.WAIT:
        ctx.drv.wait(float(step.value or "1"))
        ctx.ev.record_step(idx, kind.value)
        return

    if kind == StepKind.ASSERT_TEXT:
        got = ctx.drv.read_text(step.selector or "")
        want = _template(step.value or "", ctx)
        if want and want not in got:
            raise FlowFailure(
                FailureKind.ASSERTION_FAILED,
                f"expected {want!r} in page text",
            )
        ctx.ev.record_step(idx, kind.value)
        return

    if kind == StepKind.SUBMIT:
        # 提交控件同样走 resolver：按钮 id/形态改版不应导致流程崩溃，
        # 但歧义时必须拒绝提交。
        locator, res = _resolve_step_locator(
            Step(kind=StepKind.CLICK, field=step.field or "submit"), ctx
        )
        try:
            receipt = ctx.drv.submit(step.idempotency_key, locator.css)
        except SubmitRejected as exc:
            raise FlowFailure(FailureKind.VALIDATION_REJECTED, str(exc)) from exc
        except SubmitFailed as exc:
            raise FlowFailure(FailureKind.SUBMIT_FAILED, str(exc)) from exc
        ctx.receipt_id = receipt
        ctx.submitted = True
        ctx.invalidate_page()
        ctx.ev.record_step(idx, kind.value, field="submit", resolution=res)
        return

    raise FlowFailure(FailureKind.FLOW_INVALID, f"unknown step kind {kind}")  # pragma: no cover


def summarize(flow: Flow) -> dict[str, Any]:
    """给 API 返回的流程摘要。"""
    return {
        "name": flow.name,
        "description": flow.description,
        "dataset": flow.dataset,
        "steps": len(flow.steps),
        "logic_fields": [s.field for s in flow.steps if s.field],
        "has_idempotency_key": any(s.idempotency_key for s in flow.steps),
        "problems": validate_flow(flow),
    }

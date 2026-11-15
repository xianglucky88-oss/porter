"""执行引擎与 mockapp 的集成测试。

不需要 PostgreSQL：直接跑执行引擎 + mockapp 的 HTTP 接口。
需要 mockapp 在 MOCKAPP_URL 上运行（compose 里是 http://mockapp:9200）。

    docker compose up -d mockapp
    MOCKAPP_URL=http://localhost:9200 pytest -q tests/test_executor.py
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.executor import execute, validate_flow
from app.models import Flow, RunStatus

MOCK = os.environ.get("MOCKAPP_URL", "http://localhost:9200")


def _mock_alive() -> bool:
    try:
        return httpx.get(f"{MOCK}/healthz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _mock_alive(), reason=f"mockapp not reachable at {MOCK}")


@pytest.fixture(autouse=True)
def clean_mock():
    httpx.post(f"{MOCK}/admin/reset", timeout=5)
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)
    yield
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)


def _flow(*steps) -> Flow:
    return Flow(name="reimbursement", description="t", steps=list(steps), dataset="reimbursement")


def _fill(selector: str, value: str, optional: bool = False):
    from app.models import Step, StepKind

    return Step(kind=StepKind.FILL, selector=selector, value=value, optional=optional)


def _valid_flow() -> Flow:
    from app.models import Step, StepKind

    return _flow(
        Step(kind=StepKind.OPEN, value="/form/reimbursement"),
        _fill("input#source_key", "EXP-TEST-1"),
        _fill("input#applicant", "测试员"),
        _fill("input#expense_type", "差旅费"),
        _fill("input#amount_cents", "100"),
        _fill("input#invoice_no", "INV-T-1"),
        Step(kind=StepKind.CLICK, selector="button#submit-btn"),
        Step(kind=StepKind.SUBMIT, value=""),
        Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value=""),
    )


def test_happy_path_creates_one_submission():
    run = execute(_valid_flow())
    assert run.status is RunStatus.SUCCEEDED, run.error
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 1
    assert state["duplicates"] == 0

def test_missing_required_field_fails_at_submit():
    """account_opening 流程漏了 uscc，服务端 422，执行应失败并带出原因。"""
    flow = Flow.from_file("flows/account_opening.yaml")
    run = execute(flow)
    assert run.status is RunStatus.FAILED
    assert "422" in (run.error or "") or "required" in (run.error or "").lower()


def test_optional_step_can_fail_without_aborting():
    from app.models import Step, StepKind

    flow = _valid_flow()
    # 在最后插一个必然失败的 optional 断言，不应影响整体成功。
    flow.steps.append(
        Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value="definitely-not-there", optional=True)
    )
    run = execute(flow)
    assert run.status is RunStatus.SUCCEEDED, run.error


def test_required_assertion_failure_marks_run_failed():
    from app.models import Step, StepKind

    flow = _valid_flow()
    flow.steps.append(Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value="nope"))
    run = execute(flow)
    assert run.status is RunStatus.FAILED
    assert "nope" in (run.error or "")


def test_fault_injection_surfaces_as_failure():
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "500", "rate": 1.0, "seed": 1}, timeout=5)
    run = execute(_valid_flow())
    assert run.status is RunStatus.FAILED
    # 5xx 是瞬时/环境问题，不是业务表单失败。
    assert run.failure_class == "transient"
    assert run.is_business_failure is False


def test_business_rejection_is_classified_business_and_not_retryable():
    """account_opening 漏 uscc：服务端 422 -> business，且不可自动重试。"""
    run = execute(Flow.from_file("flows/account_opening.yaml"))
    assert run.status is RunStatus.FAILED
    assert run.failure_class == "business"
    assert run.failure_code == "business_rejected"
    assert run.is_business_failure is True
    assert run.retryable is False


def test_environment_failure_is_not_business():
    """目标系统不可达：归 environment，且不冒充业务失败。"""
    from app.config import settings

    original = settings.mockapp_url
    settings.mockapp_url = "http://127.0.0.1:1"
    try:
        run = execute(_valid_flow())
    finally:
        settings.mockapp_url = original
    assert run.status is RunStatus.FAILED
    assert run.failure_class == "environment"
    assert run.is_business_failure is False
    assert "422" not in (run.error or "")


def test_failure_evidence_is_a_whitelist_not_a_data_dump():
    """失败证据只放定位用的字段，不把整张表单的值倒进日志/工单。"""
    from app.config import settings

    original = settings.mockapp_url
    settings.mockapp_url = "http://127.0.0.1:1"
    try:
        run = execute(_valid_flow())
    finally:
        settings.mockapp_url = original
    blob = repr(run.to_dict())
    # 填进表单的业务值不应出现在失败证据里
    assert "EXP-TEST-1" not in blob
    assert "INV-T-1" not in blob
    assert "测试员" not in blob
    assert set(run.evidence) <= {"driver", "action", "selector", "url", "step_kind", "submit_may_have_landed"}


def test_failed_step_is_located():
    from app.models import Step, StepKind

    flow = _valid_flow()
    flow.steps.append(Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value="nope"))
    run = execute(flow)
    assert run.status is RunStatus.FAILED
    assert run.failed_step == len(flow.steps) - 1
    assert run.evidence.get("step_kind") == "assert_text"
    # 断言不匹配是流程问题，不是业务问题。
    assert run.failure_class == "flow"


def test_validate_flow_flags_problems():
    from app.models import Step, StepKind

    bad = _flow(Step(kind=StepKind.FILL, selector="input#a", value="1"))
    problems = validate_flow(bad)
    assert any("no 'open'" in p for p in problems)
    assert any("no 'submit'" in p for p in problems)

    good = _valid_flow()
    assert validate_flow(good) == []


# --------------------------------------------------------------------------
# 重试边界：只有"提交前 + 瞬时故障"才允许重试
# --------------------------------------------------------------------------
class _FlakyDriver:
    """第 N 次之前都在 open 阶段抛瞬时错误，用于验证重试边界。"""

    name = "http"
    submits_on_click = False

    def __init__(self, fail_open_times: int) -> None:
        self.fail_open_times = fail_open_times
        self.opens = 0
        self.submits = 0

    def start(self, flow):
        return None

    def open(self, url):
        from app.errors import TransientFailure

        self.opens += 1
        if self.opens <= self.fail_open_times:
            raise TransientFailure("gateway hiccup")

    def fill(self, selector, value):
        return None

    def select(self, selector, value):
        return None

    def click(self, selector):
        return None

    def submit(self, idempotency_key):
        self.submits += 1
        return "RCPT-RETRY"

    def wait(self, seconds):
        return None

    def read_text(self, selector):
        return "RCPT-RETRY"

    def close(self):
        return None


def _retry_flow():
    from app.models import Step, StepKind

    return Flow(
        name="reimbursement",
        description="t",
        steps=[
            Step(kind=StepKind.OPEN, value="/form/reimbursement"),
            Step(kind=StepKind.FILL, selector="input#source_key", value="EXP-R"),
            Step(kind=StepKind.SUBMIT, value=""),
        ],
        dataset="reimbursement",
    )


def test_retry_happens_for_pre_submit_transient_failure(monkeypatch):
    """提交前的瞬时故障应重试，且每次都新建驱动（避免复用坏掉的状态）。"""
    from app import executor as ex
    from app.config import settings
    from app.models import Run, RunStatus, utcnow

    monkeypatch.setattr(settings, "run_max_attempts", 3)
    created: list[_FlakyDriver] = []

    def factory():
        driver = _FlakyDriver(fail_open_times=1 if not created else 0)
        created.append(driver)
        return driver

    run = Run(run_id="r", flow="reimbursement", status=RunStatus.RUNNING, started_at=utcnow())
    ok = ex._run_with_driver(_retry_flow(), run, factory, 3)
    assert ok is True, run.error
    assert run.attempts == 2
    assert len(created) == 2, "每次尝试都必须新建驱动"
    assert created[0].opens == 1


def test_no_retry_when_failure_is_not_retryable(monkeypatch):
    """业务拒绝/流程错误不重试，一次就结束。"""
    from app import executor as ex
    from app.config import settings
    from app.models import Run, RunStatus, utcnow

    monkeypatch.setattr(settings, "run_max_attempts", 3)
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        driver = _FlakyDriver(fail_open_times=0)
        driver.open = lambda url: (_ for _ in ()).throw(_business_error())
        return driver

    run = Run(run_id="r", flow="reimbursement", status=RunStatus.RUNNING, started_at=utcnow())
    ok = ex._run_with_driver(_retry_flow(), run, factory, 3)
    assert ok is False
    assert calls["n"] == 1
    assert run.failure_class == "business"


def _business_error():
    from app.errors import BusinessRejection

    return BusinessRejection("validation failed")


def test_default_is_single_attempt(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "run_max_attempts", 1)
    run = execute(_retry_flow(), driver=_FlakyDriver(fail_open_times=1))
    assert run.attempts == 1


def test_no_retry_after_submit_started(monkeypatch):
    """提交已经开始：即使失败可重试，也不能自动重跑（会产生重复单据）。"""
    from app.config import settings
    from app.errors import TransientFailure

    monkeypatch.setattr(settings, "run_max_attempts", 3)

    class _SubmitTimesOut(_FlakyDriver):
        def submit(self, idempotency_key):
            self.submits += 1
            raise TransientFailure("gateway timeout after submit", evidence={"submit_may_have_landed": True})

    driver = _SubmitTimesOut(fail_open_times=0)
    run = execute(_retry_flow(), driver=driver)
    assert run.status is RunStatus.FAILED
    assert run.attempts == 1
    assert driver.submits == 1
    assert run.evidence.get("submit_may_have_landed") is True
    assert run.retryable is False


def test_business_rejection_marks_submit_not_landed():
    """422 是明确的业务拒绝：可以确定没有落单，证据里要写清。"""
    run = execute(Flow.from_file("flows/account_opening.yaml"))
    assert run.failure_class == "business"
    assert run.evidence.get("submit_may_have_landed") is False
    assert run.attempts == 1


def test_open_access_denied_is_environment_not_business():
    """打开页面就被 401/403 拒绝：属于访问配置问题，不能报成业务表单失败。"""
    import httpx

    original_get = httpx.Client.get

    def fake_get(self, url, *args, **kwargs):
        request = httpx.Request("GET", f"{settings.mockapp_url}{url}")
        return httpx.Response(403, request=request)

    from app.config import settings

    httpx.Client.get = fake_get
    try:
        run = execute(_retry_flow())
    finally:
        httpx.Client.get = original_get

    assert run.status is RunStatus.FAILED
    assert run.failure_class == "environment"
    assert run.failure_code == "target_access_denied"
    assert run.is_business_failure is False
    assert run.retryable is False

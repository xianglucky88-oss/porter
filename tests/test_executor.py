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

from app.executor import execute, summarize, validate_flow
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


def test_validate_flow_flags_problems():
    from app.models import Step, StepKind

    bad = _flow(Step(kind=StepKind.FILL, selector="input#a", value="1"))
    problems = validate_flow(bad)
    assert any("no 'open'" in p for p in problems)
    assert any("no 'submit'" in p for p in problems)

    good = _valid_flow()
    assert validate_flow(good) == []

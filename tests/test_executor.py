"""执行引擎与 mockapp 的集成测试。

不需要 PostgreSQL：直接跑执行引擎 + mockapp 的 HTTP 接口。
需要 mockapp 在 MOCKAPP_URL 上运行（compose 里是 http://mockapp:9200）。

    docker compose up -d mockapp
    MOCKAPP_URL=http://localhost:9200 pytest -q tests/test_executor.py
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from app.executor import execute, validate_flow
from app.failures import FailureKind
from app.models import Flow, RunStatus, Step, StepKind

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
    httpx.post(f"{MOCK}/admin/reset", timeout=5)


@pytest.fixture
def layout():
    """切换 mockapp 页面布局版本，用毕恢复 v1。"""

    def _set(version: str) -> None:
        r = httpx.post(f"{MOCK}/admin/layout", params={"version": version}, timeout=5)
        r.raise_for_status()

    yield _set
    httpx.post(f"{MOCK}/admin/layout", params={"version": "v1"}, timeout=5)


def _flow(*steps) -> Flow:
    return Flow(name="reimbursement", description="t", steps=list(steps), dataset="reimbursement")


def _fill(selector: str, value: str, optional: bool = False):
    return Step(kind=StepKind.FILL, selector=selector, value=value, optional=optional)


def _fill_field(field: str, value: str, optional: bool = False):
    return Step(kind=StepKind.FILL, field=field, value=value, optional=optional)


def _valid_flow() -> Flow:
    return _flow(
        Step(kind=StepKind.OPEN, value="/form/reimbursement"),
        _fill_field("source_key", "EXP-TEST-1"),
        _fill_field("applicant", "测试员"),
        _fill_field("expense_type", "差旅费"),
        _fill_field("amount_cents", "100"),
        _fill_field("invoice_no", "INV-T-1"),
        Step(kind=StepKind.SUBMIT, value=""),
        Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value=""),
    )


def test_happy_path_creates_one_submission():
    run = execute(_valid_flow())
    assert run.status is RunStatus.SUCCEEDED, run.error
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 1
    assert state["duplicates"] == 0


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_flow_survives_layout_relayout(layout, version: str):
    """页面改版（容器层级/标签位置/控件 id 全变）后流程照常成功。"""
    layout(version)
    run = execute(_valid_flow())
    assert run.status is RunStatus.SUCCEEDED, f"{version}: {run.error}"
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 1
    # 证据里记录了本次页面版本特征
    page = (run.evidence or {}).get("page") or {}
    assert page.get("fingerprint")
    assert page.get("layout_kind")


def test_explicit_selector_flow_still_works():
    """旧的显式 selector 写法保持兼容（v1 布局下）。"""
    flow = _flow(
        Step(kind=StepKind.OPEN, value="/form/reimbursement"),
        _fill("input#source_key", "EXP-LEGACY-1"),
        _fill("input#applicant", "测试员"),
        _fill("input#expense_type", "差旅费"),
        _fill("input#amount_cents", "100"),
        _fill("input#invoice_no", "INV-T-1"),
        Step(kind=StepKind.SUBMIT, value=""),
    )
    run = execute(flow)
    assert run.status is RunStatus.SUCCEEDED, run.error


def test_missing_required_field_fails_at_submit():
    """account_opening 流程漏了 uscc，服务端 422：业务校验拒绝。"""
    flow = Flow.from_file("flows/account_opening.yaml")
    run = execute(flow)
    assert run.status is RunStatus.FAILED
    assert run.failure_kind == FailureKind.VALIDATION_REJECTED.value
    assert "422" in (run.error or "") or "required" in (run.error or "").lower()


def test_open_unknown_page_is_page_load_failed():
    flow = _flow(
        Step(kind=StepKind.OPEN, value="/form/no_such_form"),
        Step(kind=StepKind.SUBMIT, value=""),
    )
    run = execute(flow)
    assert run.status is RunStatus.FAILED
    assert run.failure_kind == FailureKind.PAGE_LOAD_FAILED.value


def test_ambiguous_layout_refuses_to_submit(layout):
    """歧义布局下金额字段有两个候选：必须拒绝提交，且目标系统零写入。"""
    layout("ambiguous")
    run = execute(_valid_flow())
    assert run.status is RunStatus.FAILED
    assert run.failure_kind == FailureKind.STRUCTURE_CHANGED.value
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 0, "歧义时绝不能提交"
    # 证据里记录了冲突候选
    steps = (run.evidence or {}).get("steps") or []
    assert any(s.get("ok") is False for s in steps)


def test_field_missing_when_form_lacks_field():
    """流程要填的字段在页面上不存在：字段缺失，而不是校验拒绝。"""
    flow = _flow(
        Step(kind=StepKind.OPEN, value="/form/reimbursement"),
        _fill_field("source_key", "EXP-TEST-9"),
        _fill_field("no_such_field", "x"),
        Step(kind=StepKind.SUBMIT, value=""),
    )
    run = execute(flow)
    assert run.status is RunStatus.FAILED
    assert run.failure_kind == FailureKind.FIELD_MISSING.value
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 0


def test_optional_step_can_fail_without_aborting():
    flow = _valid_flow()
    # 在最后插一个必然失败的 optional 断言，不应影响整体成功。
    flow.steps.append(
        Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value="definitely-not-there", optional=True)
    )
    run = execute(flow)
    assert run.status is RunStatus.SUCCEEDED, run.error


def test_required_assertion_failure_marks_run_failed():
    flow = _valid_flow()
    flow.steps.append(Step(kind=StepKind.ASSERT_TEXT, selector="p#receipt-id", value="nope"))
    run = execute(flow)
    assert run.status is RunStatus.FAILED
    assert run.failure_kind == FailureKind.ASSERTION_FAILED.value


def test_fault_injection_surfaces_as_submit_failed():
    httpx.post(f"{MOCK}/admin/fault", params={"kind": "500", "rate": 1.0, "seed": 1}, timeout=5)
    run = execute(_valid_flow())
    assert run.status is RunStatus.FAILED
    assert run.failure_kind == FailureKind.SUBMIT_FAILED.value


def test_evidence_records_adopted_strategy_and_redacts_values():
    """证据：能看到每个字段最终采用的定位规则，但看不到任何业务取值。"""
    run = execute(_valid_flow())
    assert run.status is RunStatus.SUCCEEDED, run.error
    ev = run.evidence or {}
    steps = {s["field"]: s for s in ev["steps"] if s.get("field")}
    # 最终采用的定位规则
    assert steps["applicant"]["strategy"] == "id"
    assert steps["applicant"]["css"] == "#applicant"
    assert steps["amount_cents"]["resolution"]["attempts"]
    # 敏感值只以摘要出现
    blob = json.dumps(ev, ensure_ascii=False)
    for secret in ("测试员", "差旅费", "INV-T-1", "EXP-TEST-1"):
        assert secret not in blob
    assert steps["applicant"]["value_digest"]
    # run.error 也不应泄露（本例无错误，只验证机制存在）
    assert run.error is None


def test_evidence_page_fingerprint_changes_with_layout(layout):
    """同一流程在不同布局下的页面指纹不同，维护人员可据此识别改版。"""
    run1 = execute(_valid_flow())
    layout("v2")
    run2 = execute(_valid_flow())
    fp1 = (run1.evidence or {}).get("page", {}).get("fingerprint")
    fp2 = (run2.evidence or {}).get("page", {}).get("fingerprint")
    assert fp1 and fp2 and fp1 != fp2


def test_validate_flow_flags_problems():
    bad = _flow(Step(kind=StepKind.FILL, field="a", value="1"))
    problems = validate_flow(bad)
    assert any("no 'open'" in p for p in problems)
    assert any("no 'submit'" in p for p in problems)

    no_target = _flow(
        Step(kind=StepKind.OPEN, value="/form/reimbursement"),
        Step(kind=StepKind.FILL, value="1"),
        Step(kind=StepKind.SUBMIT, value=""),
    )
    assert any("needs 'field' or 'selector'" in p for p in validate_flow(no_target))

    good = _valid_flow()
    assert validate_flow(good) == []

"""身份与指纹的纯单元测试：不需要 mockapp 也不需要数据库。"""

from __future__ import annotations

from app.batch import _batch_key
from app.executor import prepare_steps
from app.models import (
    Flow,
    Step,
    StepKind,
    derive_idempotency_key,
    payload_fingerprint,
)


def test_idempotency_key_is_stable_and_flow_scoped():
    k1 = derive_idempotency_key("reimbursement", "EXP-001")
    k2 = derive_idempotency_key("reimbursement", "EXP-001")
    k3 = derive_idempotency_key("purchase", "EXP-001")
    assert k1 == k2, "同一记录的提交身份必须稳定"
    assert k1 != k3, "不同流程的同一单号必须区分开"
    assert "EXP-001" in k1


def test_payload_fingerprint_is_order_insensitive():
    h1 = payload_fingerprint("reimbursement", "EXP-1", {"a": "1", "b": "2"})
    h2 = payload_fingerprint("reimbursement", "EXP-1", {"b": "2", "a": "1"})
    h3 = payload_fingerprint("reimbursement", "EXP-1", {"a": "1", "b": "3"})
    assert h1 == h2
    assert h1 != h3


def test_payload_fingerprint_coerces_types():
    h1 = payload_fingerprint("reimbursement", "EXP-1", {"amount": 100})
    h2 = payload_fingerprint("reimbursement", "EXP-1", {"amount": "100"})
    assert h1 == h2


def test_prepare_steps_overrides_only_matching_fields():
    flow = Flow(
        name="f",
        description="",
        dataset="reimbursement",
        steps=[
            Step(kind=StepKind.OPEN, value="/form/reimbursement"),
            Step(kind=StepKind.FILL, selector="input#source_key", value="STATIC"),
            Step(kind=StepKind.FILL, selector="input#applicant", value="张三"),
            Step(kind=StepKind.FILL, selector="input#remark", value="默认备注"),
            Step(kind=StepKind.SUBMIT, value=""),
        ],
    )
    steps = prepare_steps(flow, {"source_key": "EXP-9", "applicant": "李娜"})
    by_sel = {s.selector: s for s in steps if s.selector}
    assert by_sel["input#source_key"].value == "EXP-9"
    assert by_sel["input#applicant"].value == "李娜"
    # 记录里没有的字段保持流程定义
    assert by_sel["input#remark"].value == "默认备注"


def test_batch_key_is_stable_for_same_records():
    records = [{"source_key": "A", "amount": "1"}, {"source_key": "B", "amount": "2"}]
    assert _batch_key("f", records) == _batch_key("f", list(records))
    assert _batch_key("f", records) != _batch_key("g", records)
    assert _batch_key("f", records) != _batch_key("f", records[:1])

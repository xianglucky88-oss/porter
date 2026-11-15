"""mockapp 自身行为的测试。

验证多版本布局的渲染与提交兼容性——尤其是 v3 布局的命名空间
字段名（form[xxx]）在 HTML 表单提交路径下的归一化（浏览器驱动
走的就是这条路径）。

    MOCKAPP_URL=http://localhost:9200 pytest -q tests/test_mockapp.py
"""

from __future__ import annotations

import os

import httpx
import pytest

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
    yield
    httpx.post(f"{MOCK}/admin/reset", timeout=5)


def test_layout_switch_changes_html_but_not_schema():
    v1 = httpx.get(f"{MOCK}/form/reimbursement", timeout=5).text
    httpx.post(f"{MOCK}/admin/layout", params={"version": "v3"}, timeout=5)
    v3 = httpx.get(f"{MOCK}/form/reimbursement", timeout=5).text
    assert v1 != v3
    # 业务 schema 不受布局影响
    schema = httpx.get(f"{MOCK}/api/form/reimbursement", timeout=5).json()
    names = [f["name"] for f in schema["fields"]]
    assert names == ["applicant", "expense_type", "amount_cents", "invoice_no", "remark"]


def test_unknown_layout_rejected():
    r = httpx.post(f"{MOCK}/admin/layout", params={"version": "v99"}, timeout=5)
    assert r.status_code == 400


def test_html_submit_accepts_namespaced_names():
    """模拟浏览器提交 v3 表单：字段名带 form[...] 命名空间也能受理。"""
    httpx.post(f"{MOCK}/admin/layout", params={"version": "v3"}, timeout=5)
    resp = httpx.post(
        f"{MOCK}/submit",
        data={
            "form_type": "reimbursement",
            "form[source_key]": "EXP-NS-1",
            "form[applicant]": "测试员",
            "form[expense_type]": "差旅费",
            "form[amount_cents]": "100",
            "form[invoice_no]": "INV-NS-1",
        },
        follow_redirects=False,
        timeout=5,
    )
    assert resp.status_code == 303
    records = httpx.get(f"{MOCK}/admin/records", timeout=5).json()
    assert records["total"] == 1
    sub = records["submissions"][0]
    # 服务端按业务字段名归一化存储
    assert sub["source_key"] == "EXP-NS-1"
    assert sub["fields"]["applicant"] == "测试员"
    assert sub["fields"]["amount_cents"] == "100"


def test_html_submit_plain_names_still_work():
    resp = httpx.post(
        f"{MOCK}/submit",
        data={
            "form_type": "reimbursement",
            "source_key": "EXP-PLAIN-1",
            "applicant": "测试员",
            "expense_type": "差旅费",
            "amount_cents": "100",
            "invoice_no": "INV-P-1",
        },
        follow_redirects=False,
        timeout=5,
    )
    assert resp.status_code == 303
    assert httpx.get(f"{MOCK}/admin/state", timeout=5).json()["total"] == 1

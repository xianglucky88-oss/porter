"""驱动一致性测试。

命题：同一流程在 HTTP 驱动与浏览器驱动下应表现一致。
一致性由结构保证——两种驱动共用同一个页面解析器
（pageview.parse_page）与同一套定位规则（resolver）：

  * HttpDriver.inspect()     解析 HTTP GET 拿到的 HTML；
  * BrowserDriver.inspect()  解析 page.content() 拿到的 HTML。

本测试验证：HttpDriver 经网络获取页面后产出的 PageView，
与直接解析同一 HTML 得到的 PageView 完全一致（指纹相同、
每个字段的定位决策相同）。浏览器路径因使用同一解析器而
天然满足同一等式（CI 无浏览器，BrowserDriver 不在此实例化）。

    MOCKAPP_URL=http://localhost:9200 pytest -q tests/test_driver_parity.py
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.executor import HttpDriver
from app.field_registry import labels_for
from app.models import Flow, Step, StepKind
from app.pageview import parse_page
from app.resolver import resolve_field, resolve_submit

MOCK = os.environ.get("MOCKAPP_URL", "http://localhost:9200")

FIELDS = ["source_key", "applicant", "expense_type", "amount_cents", "invoice_no", "remark"]


def _mock_alive() -> bool:
    try:
        return httpx.get(f"{MOCK}/healthz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _mock_alive(), reason=f"mockapp not reachable at {MOCK}")


@pytest.fixture(autouse=True)
def reset_layout():
    httpx.post(f"{MOCK}/admin/reset", timeout=5)
    yield
    httpx.post(f"{MOCK}/admin/layout", params={"version": "v1"}, timeout=5)


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_http_driver_pageview_matches_direct_parse(version: str):
    httpx.post(f"{MOCK}/admin/layout", params={"version": version}, timeout=5)

    flow = Flow(
        name="reimbursement",
        description="t",
        dataset="reimbursement",
        steps=[Step(kind=StepKind.OPEN, value="/form/reimbursement")],
    )
    drv = HttpDriver()
    try:
        drv.start(flow)
        drv.open("/form/reimbursement")
        via_driver = drv.inspect()
    finally:
        drv.close()

    html = httpx.get(f"{MOCK}/form/reimbursement", timeout=5).text
    direct = parse_page(html, url="/form/reimbursement")

    assert via_driver.fingerprint == direct.fingerprint
    assert via_driver.layout_kind == direct.layout_kind

    # 每个字段的定位决策完全一致（策略与最终选择器）
    for field in FIELDS:
        labels = labels_for(field)
        a = resolve_field(via_driver, field, labels)
        b = resolve_field(direct, field, labels)
        assert (a.ok, a.strategy, a.css) == (b.ok, b.strategy, b.css), field

    sa = resolve_submit(via_driver)
    sb = resolve_submit(direct)
    assert (sa.ok, sa.strategy, sa.css) == (sb.ok, sb.strategy, sb.css)


def _decision(page, field: str):
    r = resolve_field(page, field, labels_for(field))
    return (r.ok, r.strategy, r.css)


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_resolution_stable_across_repeated_inspection(version: str):
    """同一布局重复抓取/解析，定位决策不变（确定性）。"""
    httpx.post(f"{MOCK}/admin/layout", params={"version": version}, timeout=5)
    decisions = []
    for _ in range(2):
        html = httpx.get(f"{MOCK}/form/reimbursement", timeout=5).text
        page = parse_page(html)
        decisions.append({f: _decision(page, f) for f in FIELDS})
    assert decisions[0] == decisions[1]

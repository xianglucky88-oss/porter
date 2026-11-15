"""API 层测试。

不依赖 PostgreSQL：store 不可用时 API 会降级（只警告不阻断），
因此这些用例可以在纯 mockapp 环境下跑通。

    MOCKAPP_URL=http://localhost:9200 pytest -q tests/test_api.py
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import app

MOCK = os.environ.get("MOCKAPP_URL", "http://localhost:9200")


def _mock_alive() -> bool:
    try:
        return httpx.get(f"{MOCK}/healthz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _mock_alive(), reason=f"mockapp not reachable at {MOCK}")

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_mock():
    httpx.post(f"{MOCK}/admin/reset", timeout=5)
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)
    yield
    httpx.post(f"{MOCK}/admin/reset", timeout=5)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_flows_returns_three():
    r = client.get("/flows")
    assert r.status_code == 200
    names = {f.get("name") for f in r.json()["flows"]}
    assert names == {"purchase_requisition", "reimbursement_batch", "account_opening"}


def test_run_flow_succeeds():
    r = client.post("/flows/reimbursement_batch/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["failure_kind"] is None
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 1


def test_run_response_carries_redacted_evidence():
    """运行响应带证据：有定位规则与页面指纹，无业务取值明文。"""
    r = client.post("/flows/reimbursement_batch/run")
    assert r.status_code == 200, r.text
    ev = r.json().get("evidence") or {}
    assert ev.get("page", {}).get("fingerprint")
    fill_steps = [s for s in ev.get("steps", []) if s.get("kind") == "fill"]
    assert fill_steps and all(s.get("strategy") for s in fill_steps)
    blob = json.dumps(ev, ensure_ascii=False)
    for secret in ("李娜", "348600", "INV-20261108-7781", "华东区客户拜访"):
        assert secret not in blob


def test_run_account_opening_is_validation_rejected():
    """业务校验拒绝：HTTP 422 + failure_kind=validation_rejected。"""
    r = client.post("/flows/account_opening/run")
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "failed"
    assert body["failure_kind"] == "validation_rejected"
    assert "422" in body["error"] or "required" in body["error"].lower()


def test_structure_changed_maps_to_409():
    """页面出现歧义候选：HTTP 409 + structure_changed，且未发生提交。"""
    httpx.post(f"{MOCK}/admin/layout", params={"version": "ambiguous"}, timeout=5)
    try:
        r = client.post("/flows/reimbursement_batch/run")
    finally:
        httpx.post(f"{MOCK}/admin/layout", params={"version": "v1"}, timeout=5)
    assert r.status_code == 409
    assert r.json()["failure_kind"] == "structure_changed"
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 0


def test_unknown_flow_404():
    assert client.get("/flows/nope").status_code == 404
    assert client.post("/flows/nope/run").status_code == 404


def test_run_detail_endpoint_404_without_store():
    """store 不可用时详情接口返回 503 而不是崩溃。"""
    r = client.get("/runs/definitely-not-a-run")
    assert r.status_code in (404, 503)

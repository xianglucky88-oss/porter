"""API 层测试。

不依赖 PostgreSQL：store 不可用时 API 会降级（只警告不阻断），
因此这些用例可以在纯 mockapp 环境下跑通。

    MOCKAPP_URL=http://localhost:9200 pytest -q tests/test_api.py
"""

from __future__ import annotations

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
    assert r.json()["status"] == "succeeded"
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 1


def test_run_account_opening_fails_with_422():
    """目标系统明确拒绝 -> HTTP 422，且归因为 business，不是环境故障。"""
    r = client.post("/flows/account_opening/run")
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "failed"
    assert body["failure_class"] == "business"
    assert body["business_failure"] is True
    assert body["retryable"] is False
    assert "422" in r.json()["error"] or "required" in r.json()["error"].lower()
    assert r.headers["X-Porter-Driver"] == "http"


def test_environment_failure_is_not_reported_as_business():
    """目标系统不可达时必须归为 environment，绝不能算成业务表单失败。"""
    from app.config import settings

    original = settings.mockapp_url
    settings.mockapp_url = "http://127.0.0.1:1"  # 关掉的端口
    try:
        r = client.post("/flows/reimbursement_batch/run")
    finally:
        settings.mockapp_url = original

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "failed"
    assert body["failure_class"] == "environment"
    assert body["business_failure"] is False
    assert body["evidence"].get("submit_may_have_landed") is not True


def test_unknown_flow_404():
    assert client.get("/flows/nope").status_code == 404
    assert client.post("/flows/nope/run").status_code == 404

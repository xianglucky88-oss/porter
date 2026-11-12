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


def test_flow_detail_exposes_missing_idempotency_key():
    r = client.get("/flows/purchase_requisition")
    assert r.status_code == 200
    body = r.json()
    assert body["has_idempotency_key"] is False
    assert len(body["steps_detail"]) == 10


def test_run_flow_succeeds():
    r = client.post("/flows/reimbursement_batch/run")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    state = httpx.get(f"{MOCK}/admin/state", timeout=5).json()
    assert state["total"] == 1


def test_run_account_opening_fails_with_422():
    r = client.post("/flows/account_opening/run")
    assert r.status_code == 502
    assert r.json()["status"] == "failed"
    assert "422" in r.json()["error"] or "required" in r.json()["error"].lower()


def test_unknown_flow_404():
    assert client.get("/flows/nope").status_code == 404
    assert client.post("/flows/nope/run").status_code == 404

"""FastAPI 服务。

端点：
    GET  /healthz
    GET  /flows                     列出可用流程与静态校验结果
    GET  /flows/{name}              单个流程详情
    POST /flows/{name}/run          同步执行一次（失败语义体现在 failure_kind 与状态码）
    GET  /runs?limit=20&flow=...    运行历史
    GET  /runs/{run_id}             单轮详情（含脱敏证据）
    GET  /stats                     汇总
    POST /admin/reset-batch?flow=.. 清空该流程的 submissions（demo 用）

失败语义 → HTTP 状态码：
    validation_rejected → 422（业务校验拒绝，调用方应修数据）
    page_load_failed    → 502（目标系统不可用，可稍后重试）
    structure_changed   → 409（页面改版/候选歧义，需维护定位知识）
    field_missing       → 409（字段缺失，需核对流程与页面版本）
    其他失败            → 502
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import __version__
from .config import settings
from .executor import execute, summarize
from .failures import FailureKind
from .models import Flow
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("porter.api")

app = FastAPI(title="porter", version=__version__)
_store: Store | None = None

_FAILURE_STATUS = {
    FailureKind.VALIDATION_REJECTED.value: 422,
    FailureKind.PAGE_LOAD_FAILED.value: 502,
    FailureKind.STRUCTURE_CHANGED.value: 409,
    FailureKind.FIELD_MISSING.value: 409,
}


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def load_flow(name: str) -> Flow:
    path = os.path.join(settings.flows_dir, f"{name}.yaml")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"flow not found: {name}")
    return Flow.from_file(path)


@app.on_event("startup")
def startup() -> None:
    # 数据库只用于记录运行历史，不是执行链路的必需品。
    # 因此这里不阻塞启动：在后台线程里等库就绪，失败就降级。
    # 早期版本是在 startup 里同步等 60 秒，容器编排下会把健康检查拖死。
    import threading

    def _prepare() -> None:
        try:
            st = get_store()
            st.wait_ready(timeout=float(os.environ.get("DB_WAIT_TIMEOUT", "20")))
            st.migrate()
            log.info("store ready")
        except Exception as exc:  # noqa: BLE001
            log.warning("store unavailable, running in degraded mode: %s", exc)

    threading.Thread(target=_prepare, name="store-init", daemon=True).start()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/flows")
def list_flows() -> dict[str, Any]:
    out = []
    for path in sorted(glob.glob(os.path.join(settings.flows_dir, "*.yaml"))):
        try:
            flow = Flow.from_file(path)
        except Exception as exc:  # noqa: BLE001
            out.append({"file": os.path.basename(path), "error": str(exc)})
            continue
        out.append(summarize(flow))
    return {"flows": out}


@app.get("/flows/{name}")
def get_flow(name: str) -> dict[str, Any]:
    flow = load_flow(name)
    data = summarize(flow)
    data["steps_detail"] = [
        {
            "index": i,
            "kind": s.kind.value,
            "selector": s.selector,
            "value": s.value,
            "text": s.text,
            "idempotency_key": s.idempotency_key,
            "optional": s.optional,
        }
        for i, s in enumerate(flow.steps)
    ]
    return data


@app.post("/flows/{name}/run")
def run_flow(name: str) -> JSONResponse:
    flow = load_flow(name)
    problems = [
        p
        for p in summarize(flow)["problems"]
        if "idempotency" not in p
    ]
    if any("no 'submit'" in p or "first step" in p for p in problems):
        raise HTTPException(status_code=422, detail={"message": "flow is invalid", "problems": problems})

    run = execute(flow)
    if run.status.value == "succeeded":
        code = 200
    else:
        code = _FAILURE_STATUS.get(run.failure_kind or "", 502)
    try:
        get_store().save_run(run, source_key=_source_key_of(flow), receipt_id=run.receipt_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("persist run failed: %s", exc)
    return JSONResponse(run.to_dict(), status_code=code)


def _source_key_of(flow: Flow) -> str:
    """从 flow 的 fill 步骤里找 source_key 的值。用于落库统计，不影响执行。"""
    for s in flow.steps:
        if s.kind.value == "fill" and (s.field == "source_key" or (s.selector or "").endswith("#source_key")):
            return s.value or ""
    return ""


@app.get("/runs")
def runs(limit: int = 20, flow: str | None = None) -> dict[str, Any]:
    try:
        return {"runs": get_store().list_runs(limit=limit, flow=flow)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


@app.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    """单轮执行详情，含脱敏证据（定位规则、候选冲突、页面版本特征）。"""
    try:
        row = get_store().get_run(run_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return row


@app.get("/stats")
def stats() -> dict[str, Any]:
    try:
        return get_store().stats()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


@app.post("/admin/reset-batch")
def reset_batch(flow: str) -> dict[str, Any]:
    with get_store()._conn() as conn:
        cur = conn.execute("DELETE FROM submissions WHERE flow = %s", (flow,))
        return {"deleted": cur.rowcount}

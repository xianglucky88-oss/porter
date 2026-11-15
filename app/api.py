"""FastAPI 服务。

端点：
    GET  /healthz
    GET  /flows                     列出可用流程与静态校验结果
    GET  /flows/{name}              单个流程详情
    POST /flows/{name}/run          同步执行一次（单发路径）
    POST /flows/{name}/batches      创建批次（幂等：同批记录重复创建返回原批次）
    GET  /batches                   批次列表
    GET  /batches/{batch_id}        批次详情（含每条记录的生命周期状态）
    POST /batches/{batch_id}/run    推进批次（可重复调用）
    POST /batches/{batch_id}/recover  恢复：清扫僵尸租约、确认未知记录
    POST /batches/{batch_id}/abort  中止：提交前取消，未知转人工
    POST /items/{item_id}/resolve   人工处置（retry/confirm/cancel）
    GET  /items/{item_id}           记录详情（含步骤日志与尝试审计）
    GET  /runs?limit=20&flow=...    运行历史
    GET  /stats                     汇总
    POST /admin/reset-batch?flow=.. 清空该流程的 submissions（demo 用）
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__
from .batch import BatchError, BatchRunner
from .config import settings
from .executor import execute, summarize
from .models import Flow
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="porter", version=__version__)
_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def get_runner() -> BatchRunner:
    return BatchRunner(get_store())


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
            logging.info("store ready (schema v%s)", st.migration_version())
        except Exception as exc:  # noqa: BLE001
            logging.warning("store unavailable, running in degraded mode: %s", exc)

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
    code = 200 if run.status.value == "succeeded" else 502
    try:
        get_store().save_run(run, source_key=_source_key_of(flow), receipt_id=run.receipt_id)
    except Exception as exc:  # noqa: BLE001
        logging.warning("persist run failed: %s", exc)
    return JSONResponse(run.to_dict(), status_code=code)


def _source_key_of(flow: Flow) -> str:
    """从 flow 的 fill 步骤里找 source_key 的值。用于落库统计，不影响执行。"""
    for s in flow.steps:
        if s.kind.value == "fill" and (s.selector or "").endswith("#source_key"):
            return s.value or ""
    return ""


# ---------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------


class BatchCreate(BaseModel):
    records: list[dict[str, Any]]
    note: str = ""


class ResolveIn(BaseModel):
    action: str  # retry | confirm | cancel
    receipt_id: str | None = None
    note: str = ""


@app.post("/flows/{name}/batches", status_code=201)
def create_batch(name: str, body: BatchCreate) -> JSONResponse:
    flow = load_flow(name)
    try:
        batch, created = get_runner().create_batch(flow, body.records, note=body.note)
    except BatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc
    return JSONResponse(
        {
            "batch_id": batch["batch_id"],
            "flow": batch["flow"],
            "status": batch["status"],
            "total": batch["total"],
            "created": created,
        },
        status_code=201 if created else 200,
    )


@app.get("/batches")
def list_batches(limit: int = 20) -> dict[str, Any]:
    try:
        return {"batches": get_store().list_batches(limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    try:
        return get_runner().get_batch(batch_id)
    except BatchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


@app.post("/batches/{batch_id}/run")
def run_batch(batch_id: str) -> dict[str, Any]:
    try:
        runner = get_runner()
        batch = runner.get_batch(batch_id)
        return runner.run_batch(batch_id, load_flow(batch["flow"]))
    except BatchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


@app.post("/batches/{batch_id}/recover")
def recover_batch(batch_id: str) -> dict[str, Any]:
    try:
        runner = get_runner()
        batch = runner.get_batch(batch_id)
        return runner.recover_batch(batch_id, load_flow(batch["flow"]))
    except BatchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


@app.post("/batches/{batch_id}/abort")
def abort_batch(batch_id: str, note: str = "aborted by operator") -> dict[str, Any]:
    try:
        return get_runner().abort_batch(batch_id, note=note)
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


@app.get("/items/{item_id}")
def get_item(item_id: str) -> dict[str, Any]:
    try:
        store = get_store()
        item = store.get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"item not found: {item_id}")
        return {
            "item": item,
            "steps": store.item_steps(item_id),
            "attempts": store.item_attempts(item_id),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


@app.post("/items/{item_id}/resolve")
def resolve_item(item_id: str, body: ResolveIn) -> dict[str, Any]:
    try:
        return get_runner().resolve_item(
            item_id, body.action, receipt_id=body.receipt_id, note=body.note
        )
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


# ---------------------------------------------------------------------
# 历史与汇总
# ---------------------------------------------------------------------


@app.get("/runs")
def runs(limit: int = 20, flow: str | None = None) -> dict[str, Any]:
    try:
        return {"runs": get_store().list_runs(limit=limit, flow=flow)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


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

"""FastAPI 服务。

端点：
    GET  /healthz                   存活：本进程能否响应（永不依赖浏览器/数据库）
    GET  /readyz                    就绪：按驱动模式分别判定，见下
    GET  /health/driver             驱动与浏览器运行环境详情
    POST /health/driver/probe       主动重新探测（运维用）
    GET  /flows                     列出可用流程与静态校验结果
    GET  /flows/{name}              单个流程详情
    POST /flows/{name}/run          同步执行一次
    GET  /runs?limit=20&flow=...    运行历史
    GET  /stats                     汇总（业务失败与运行故障分开）
    POST /admin/reset-batch?flow=.. 清空该流程的 submissions（demo 用）

健康语义分三层，刻意不合并：

  * liveness（/healthz）：只说明进程活着。浏览器崩溃、数据库断开都不该重启进程。
  * readiness（/readyz）：说明现在能不能接活。
      - HTTP 模式：目标系统可达；store 只影响历史查询，不可用时标 degraded 但保持 ready；
      - 浏览器模式：额外要求一次真实浏览器启动探测通过。失败 → 503，
        code 给出 chromium_missing / chromium_launch_failed 等具体原因，
        而不是让后续表单步骤"超时失败"。
  * driver health（/health/driver）：给出沙箱/shm/字体/参数等证据，便于定位环境差异。

HTTP 状态码映射（让上游不会把环境问题当业务问题）：
    成功            200
    business 拒绝   422  （目标系统明确拒绝，改数据）
    flow 不匹配     422  （流程/选择器需要改）
    transient       503 + Retry-After（可安全重试）
    environment     503  （修环境，不要重试表单）
    internal        500
"""

from __future__ import annotations

import glob
import logging
import os
import threading
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import __version__, redaction
from .browser_runtime import (
    cleanup_stale_profiles,
    dev_shm_disabled,
    font_count,
    manager,
    sandbox_enabled,
    shm_size_mb,
)
from .config import settings
from .errors import FailureClass
from .executor import execute, summarize
from .models import Flow
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("porter.api")

app = FastAPI(title="porter", version=__version__)
_store: Store | None = None
_store_ready = threading.Event()
# 浏览器预热只允许一个在飞行，避免高频就绪探针堆积线程。
_warmup_inflight = threading.Lock()

_STATUS_BY_CLASS = {
    FailureClass.BUSINESS.value: 422,
    FailureClass.FLOW.value: 422,
    FailureClass.TRANSIENT.value: 503,
    FailureClass.ENVIRONMENT.value: 503,
    FailureClass.INTERNAL.value: 500,
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
    cleanup_stale_profiles()

    def _prepare_store() -> None:
        try:
            st = get_store()
            st.wait_ready(timeout=float(os.environ.get("DB_WAIT_TIMEOUT", "20")))
            st.migrate()
            _store_ready.set()
            log.info("store ready")
        except Exception as exc:  # noqa: BLE001 - 存储初始化失败只降级，不影响执行链路
            log.warning("store unavailable, running in degraded mode: %s", exc)

    threading.Thread(target=_prepare_store, name="store-init", daemon=True).start()

    if settings.use_browser:
        _kick_browser_warmup()


def _kick_browser_warmup() -> None:
    """在后台做一次真实浏览器探测。多次调用无害：manager 内部串行化。

    用一个 in-flight 标记避免高频 /readyz 把线程堆起来；探测本身在 manager 里
    还有第二道串行化，标记只是线程层面的节流。
    """
    if not settings.use_browser or not _warmup_inflight.acquire(blocking=False):
        return
    threading.Thread(target=_warm_browser, name="browser-warmup", daemon=True).start()


def _warm_browser() -> None:
    try:
        result = manager.warmup()
        if result.ok:
            log.info("browser driver ready: %s", result.details.get("launch_ms"))
        elif result.state != "warming":
            log.error("browser driver unavailable: %s (%s)", result.code, result.reason)
    finally:
        _warmup_inflight.release()


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """存活探针：绝不依赖浏览器、数据库或下游。"""
    return {"status": "ok", "version": __version__, "build": settings.build_tag}


@app.get("/readyz")
def readyz() -> JSONResponse:
    checks: dict[str, Any] = {}
    ready = True

    checks["target"] = _check_target()
    if checks["target"]["state"] != "ok":
        # 目标系统不可达：两种模式都不能接活。归为 environment。
        ready = False

    checks["store"] = {
        "state": "ok" if _store_ready.is_set() else "degraded",
        "detail": "run history only; execution continues without it",
    }

    checks["driver"] = {
        "mode": "browser" if settings.use_browser else "http",
        "fallback": settings.browser_fallback,
    }
    if settings.use_browser:
        probe = manager.cached_probe()
        if probe is not None and probe.ok and probe.age() > settings.browser_probe_ttl:
            # 探测结果过期：后台重探，但本次仍用旧结果答复，避免就绪状态抖动。
            _kick_browser_warmup()
        if probe is None:
            # 预热尚未完成。就绪判为 warming，而不是把它当成失败。
            # 同时触发一次后台预热，避免探测失败后缓存被清空、/readyz 永远停在 warming。
            _kick_browser_warmup()
            checks["driver"].update(state="warming", code="probe_pending", ready=False)
            ready = False
        elif not probe.ok and settings.browser_fallback == "http":
            # 显式开启降级：浏览器不可用仍能接活，但必须标成 degraded，
            # 让调用方知道这轮跑的不是真实浏览器路径。
            checks["driver"].update(
                state="degraded",
                code=probe.code,
                ready=True,
                reason=probe.reason,
                effective_driver="http",
            )
        else:
            checks["driver"].update(state=probe.state, code=probe.code, ready=probe.ok)
            if not probe.ok:
                ready = False
            elif probe.details.get("fonts") == 0:
                # 浏览器能起但没字体：可执行，渲染与断言不可信，明确降级。
                checks["driver"].update(font_state="missing")
        checks["sessions"] = {
            "max": settings.browser_max_sessions,
            "queue_timeout": settings.browser_queue_timeout,
        }
    else:
        checks["driver"]["ready"] = True

    body = {
        "status": "ok" if ready else "not_ready",
        "version": __version__,
        "build": settings.build_tag,
        "checks": checks,
    }
    result = JSONResponse(body, status_code=200 if ready else 503)
    if not ready:
        result.headers["Retry-After"] = "5"
    return result


def _check_target() -> dict[str, Any]:
    # 用 HTTP 探测；驱动内部的失败仍会单独归因，不依赖这里。
    try:
        resp = httpx.get(
            f"{settings.mockapp_url}/healthz",
            timeout=settings.ready_target_timeout,
        )
        if resp.status_code == 200:
            return {"state": "ok", "code": "target_ok"}
        return {"state": "unavailable", "code": "target_status", "status": resp.status_code}
    except Exception as exc:  # noqa: BLE001 - 目标系统探测：任何失败都等价于不可达
        return {
            "state": "unavailable",
            "code": "target_unreachable",
            "detail": redaction.redact_text(str(exc))[:200],
        }


@app.get("/health/driver")
def health_driver() -> dict[str, Any]:
    """驱动与运行环境证据。HTTP 模式下也返回运行环境快照，便于对比两台机器。

    注意：这里**不触发**真实启动，避免运维轮询把浏览器压满；需要重探请用 POST。
    """
    probe = manager.cached_probe()
    return {
        "mode": "browser" if settings.use_browser else "http",
        "headless": settings.headless,
        "fallback": settings.browser_fallback,
        "build": settings.build_tag,
        "sandbox": sandbox_enabled(),
        "dev_shm_mb": shm_size_mb(),
        "dev_shm_disabled": dev_shm_disabled(),
        "fonts": font_count(),
        "max_sessions": settings.browser_max_sessions,
        "probe": probe.to_dict() if probe else None,
    }


@app.post("/health/driver/probe")
def reprobe_driver() -> dict[str, Any]:
    result = manager.refresh_probe()
    return {"probe": result.to_dict()}


@app.get("/flows")
def list_flows() -> dict[str, Any]:
    out = []
    for path in sorted(glob.glob(os.path.join(settings.flows_dir, "*.yaml"))):
        try:
            flow = Flow.from_file(path)
        except Exception as exc:  # noqa: BLE001 - 单个流程文件损坏不应让整个列表接口失败
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
    problems = [p for p in summarize(flow)["problems"] if "idempotency" not in p]
    if any("no 'submit'" in p or "first step" in p for p in problems):
        return JSONResponse(
            {
                "status": "failed",
                "failure_class": FailureClass.FLOW.value,
                "failure_code": "invalid_flow",
                "business_failure": False,
                "error": "flow is invalid",
                "problems": problems,
            },
            status_code=422,
        )

    run = execute(flow)
    code = 200 if run.status.value == "succeeded" else _status_for(run.failure_class)
    try:
        get_store().save_run(run, source_key=_source_key_of(flow), receipt_id=run.receipt_id)
    except Exception as exc:  # noqa: BLE001 - 落库失败不影响本次执行结果
        log.warning("persist run failed: %s", exc)
    payload = run.to_dict()
    if run.degraded:
        payload["warning"] = run.degraded_reason
    headers = {"X-Porter-Driver": run.driver}
    if code == 503:
        headers["Retry-After"] = "5" if run.retryable else "60"
    return JSONResponse(payload, status_code=code, headers=headers)


def _status_for(failure_class: str | None) -> int:
    return _STATUS_BY_CLASS.get(failure_class or "", 500)


def _source_key_of(flow: Flow) -> str:
    """从 flow 的 fill 步骤里找 source_key 的值。用于落库统计，不影响执行。"""
    for s in flow.steps:
        if s.kind.value == "fill" and (s.selector or "").endswith("#source_key"):
            return s.value or ""
    return ""


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
    if not settings.enable_admin:
        raise HTTPException(status_code=404, detail="admin endpoints disabled")
    with get_store()._conn() as conn:
        cur = conn.execute("DELETE FROM submissions WHERE flow = %s", (flow,))
        return {"deleted": cur.rowcount}

"""FastAPI 服务。

端点分为三类：

运行
    GET  /healthz
    GET  /flows                            已治理流程列表（含发布状态）
    GET  /flows/{name}                     流程详情（发布版 + 草稿 + 版本历史）
    POST /flows/{name}/run                 按已发布版本执行；可显式钉版本 / 恢复
    GET  /runs                             运行历史（含版本绑定与失败分类）
    GET  /runs/{run_id}                    单次运行详情
    GET  /stats                            汇总

定义生命周期
    POST   /flows/{name}/drafts            创建草稿
    PUT    /flows/{name}/drafts/{version}  编辑草稿（乐观并发：expected_revision）
    DELETE /flows/{name}/drafts/{version}  丢弃草稿
    POST   /flows/{name}/versions/{v}/review  重新执行静态审查
    GET    /flows/{name}/versions          版本列表
    GET    /flows/{name}/versions/{v}      版本详情（含完整步骤快照）
    GET    /flows/{name}/compatibility     两个版本的兼容性结论
    POST   /flows/{name}/publish           发布草稿（可确认高风险动作 / 放行不兼容变更）
    POST   /flows/{name}/deprecate         废弃已发布版本
    POST   /flows/{name}/rollback          回退到历史版本（生成新版本）

治理辅助
    GET  /audit                            审计事件
    GET  /datasets                         数据集目录（含删除状态）
    PUT  /datasets/{name}                  登记 / 更新数据集
    DELETE /datasets/{name}                删除数据集（保留登记，执行时拒绝）
    POST /admin/sync-flows                 把 flows/*.yaml 的新变更导入为草稿（绝不自动发布）
    POST /admin/reset-batch?flow=..        清空该流程的 submissions（demo 用）
"""

from __future__ import annotations

import glob
import logging
import os
import threading
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .executor import execute, summarize
from .governance import (
    ConflictError,
    DefinitionRegistry,
    GovernanceError,
    NotFoundError,
    get_registry,
    parse_definition_payload,
)
from .models import Flow, Run
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="porter", version=__version__)
_store: Store | None = None
_store_lock = threading.Lock()

# 数据库不可用时的降级索引：至少保证同进程内 run_id 不会二次执行。
_memory_runs: dict[str, dict[str, Any]] = {}
_memory_runs_lock = threading.Lock()
_inflight_runs: set[str] = set()
_run_guard = threading.RLock()


def get_store() -> Store:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Store()
    return _store


def get_definitions() -> DefinitionRegistry:
    return get_registry()


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #


class DraftRequest(BaseModel):
    definition: dict[str, Any] | None = None
    yaml: str | None = None
    base_version: int | None = None
    release_notes: str | None = None


class DraftUpdateRequest(BaseModel):
    definition: dict[str, Any] | None = None
    yaml: str | None = None
    expected_revision: int | None = None
    release_notes: str | None = None


class PublishRequest(BaseModel):
    version: int | None = None
    base_version: int | None = None
    acknowledge: list[str] = Field(default_factory=list)
    allow_breaking: bool = False
    reason: str = ""


class DeprecateRequest(BaseModel):
    version: int | None = None
    reason: str


class RollbackRequest(BaseModel):
    to_version: int
    base_version: int | None = None
    reason: str


class RunRequest(BaseModel):
    version: int | None = None
    run_id: str | None = None
    batch_id: str | None = None
    # true 时若 run_id 已存在则直接返回既有记录；默认 true（幂等重试）。
    idempotent: bool = True
    # 显式回放已废弃版本（审计复现用）。默认拒绝。
    allow_deprecated: bool = False


class ResumeRequest(BaseModel):
    run_id: str | None = None
    # resumed_from 的原运行必须失败；force 可重跑（仍绑定原版本）。
    force: bool = False


class DatasetRequest(BaseModel):
    description: str = ""
    risk_level: str = "normal"
    fields: list[dict[str, Any]] = Field(default_factory=list)
    sensitive_fields: list[str] = Field(default_factory=list)
    deleted: bool = False


class DiscardRequest(BaseModel):
    reason: str = ""


# --------------------------------------------------------------------------- #
# 错误映射
# --------------------------------------------------------------------------- #


@app.exception_handler(GovernanceError)
def governance_error_handler(_request: Any, exc: GovernanceError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=exc.status_code)


def _actor(x_actor: str | None) -> str:
    # 生产环境应来自认证主体；这里只要求调用方显式声明，便于审计追溯。
    return (x_actor or "anonymous").strip() or "anonymous"


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #


@app.on_event("startup")
def startup() -> None:
    # 数据库只用于记录运行历史，不是执行链路的必需品。
    # 因此这里不阻塞启动：在后台线程里等库就绪，失败就降级。
    # 早期版本是在 startup 里同步等 60 秒，容器编排下会把健康检查拖死。
    def _prepare() -> None:
        try:
            st = get_store()
            st.wait_ready(timeout=float(os.environ.get("DB_WAIT_TIMEOUT", "20")))
            st.migrate()
            logging.info("store ready")
        except Exception as exc:  # noqa: BLE001
            logging.warning("store unavailable, running in degraded mode: %s", exc)

    def _definitions() -> None:
        try:
            get_definitions()
            logging.info("definition registry ready")
        except Exception as exc:  # noqa: BLE001
            logging.error("definition registry unavailable: %s", exc)

    threading.Thread(target=_definitions, name="definitions-init", daemon=True).start()
    threading.Thread(target=_prepare, name="store-init", daemon=True).start()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# --------------------------------------------------------------------------- #
# 流程读取
# --------------------------------------------------------------------------- #


@app.get("/flows")
def list_flows() -> dict[str, Any]:
    return {"flows": get_definitions().list_flows()}


@app.get("/flows/{name}")
def get_flow(name: str) -> dict[str, Any]:
    return get_definitions().get_flow(name)


@app.get("/flows/{name}/versions")
def list_versions(name: str) -> dict[str, Any]:
    return {"flow": name, "versions": get_definitions().list_versions(name)}


@app.get("/flows/{name}/versions/{version}")
def get_version(name: str, version: int) -> dict[str, Any]:
    return get_definitions().get_version(name, version)


@app.get("/flows/{name}/compatibility")
def compatibility(
    name: str,
    from_version: int = Query(alias="from"),
    to_version: int = Query(alias="to"),
) -> dict[str, Any]:
    return get_definitions().compatibility(name, from_version=from_version, to_version=to_version)


@app.post("/flows/{name}/versions/{version}/review")
def review_version(name: str, version: int) -> dict[str, Any]:
    return get_definitions().review_version(name, version)


@app.post("/flows/review")
def review_payload(payload: DraftRequest) -> dict[str, Any]:
    raw = parse_definition_payload(definition=payload.definition, yaml_text=payload.yaml)
    return get_definitions().review_payload(raw)


# --------------------------------------------------------------------------- #
# 草稿
# --------------------------------------------------------------------------- #


@app.post("/flows/{name}/drafts", status_code=201)
def create_draft(
    name: str,
    payload: DraftRequest,
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = parse_definition_payload(definition=payload.definition, yaml_text=payload.yaml)
    return get_definitions().create_draft(
        name,
        raw,
        base_version=payload.base_version,
        actor=_actor(x_actor),
        release_notes=payload.release_notes,
    )


@app.put("/flows/{name}/drafts/{version}")
def update_draft(
    name: str,
    version: int,
    payload: DraftUpdateRequest,
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    registry = get_definitions()
    current = registry.get_version(name, version)
    if current["status"] != "draft":
        raise ConflictError(f"版本 v{version} 不是草稿", code="version_not_draft")
    flow_detail = registry.get_flow(name)
    if flow_detail.get("draft_version") != version:
        raise ConflictError(f"v{version} 不是当前草稿", code="version_conflict")
    raw = parse_definition_payload(definition=payload.definition, yaml_text=payload.yaml)
    return registry.update_draft(
        name,
        raw,
        expected_revision=payload.expected_revision,
        actor=_actor(x_actor),
        release_notes=payload.release_notes,
    )


@app.delete("/flows/{name}/drafts/{version}")
def discard_draft(
    name: str,
    version: int,
    payload: DiscardRequest | None = Body(default=None),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    detail = get_definitions().get_flow(name)
    if detail.get("draft_version") != version:
        raise ConflictError(f"v{version} 不是当前草稿", code="version_conflict")
    return get_definitions().discard_draft(
        name, actor=_actor(x_actor), reason=(payload.reason if payload else "")
    )


# --------------------------------------------------------------------------- #
# 发布 / 废弃 / 回退
# --------------------------------------------------------------------------- #


@app.post("/flows/{name}/publish")
def publish(
    name: str,
    payload: PublishRequest | None = Body(default=None),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    payload = payload or PublishRequest()
    return get_definitions().publish(
        name,
        version=payload.version,
        base_version=payload.base_version,
        actor=_actor(x_actor),
        acknowledge=payload.acknowledge,
        allow_breaking=payload.allow_breaking,
        reason=payload.reason,
    )


@app.post("/flows/{name}/deprecate")
def deprecate(
    name: str,
    payload: DeprecateRequest,
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    return get_definitions().deprecate(
        name, version=payload.version, actor=_actor(x_actor), reason=payload.reason
    )


@app.post("/flows/{name}/rollback")
def rollback(
    name: str,
    payload: RollbackRequest,
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    return get_definitions().rollback(
        name,
        payload.to_version,
        actor=_actor(x_actor),
        reason=payload.reason,
        base_version=payload.base_version,
    )


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #


def _record_memory(run: Run) -> None:
    with _memory_runs_lock:
        _memory_runs[run.run_id] = run.to_dict()


def _lookup_run(run_id: str) -> dict[str, Any] | None:
    try:
        row = get_store().get_run(run_id)
        if row:
            return row
    except Exception:  # noqa: BLE001 - 降级
        pass
    with _memory_runs_lock:
        return _memory_runs.get(run_id)


def _claim_run_id(run_id: str | None) -> bool:
    """原子地认领一个 run_id。

    返回 True 表示本次调用获得了执行权；False 表示该 run_id 已在执行中。
    认领一直持有到运行记录落盘（内存索引更新）之后才释放，否则"执行结束"与
    "记录可查"之间的缝隙会让并发请求重复执行同一个 run_id。
    """
    if not run_id:
        return True
    with _run_guard:
        if run_id in _inflight_runs or run_id in _memory_runs:
            return False
        _inflight_runs.add(run_id)
    return True


def _release_run_id(run_id: str | None) -> None:
    if run_id:
        with _run_guard:
            _inflight_runs.discard(run_id)


def _execute_binding(
    binding: Any,
    *,
    actor: str,
    run_id: str | None,
    batch_id: str | None,
    resumed_from: str | None = None,
) -> tuple[Run, dict[str, Any]]:
    """在已确认的版本绑定上执行一次，并落运行记录。

    版本在这里是常量：执行过程中即使有新版本发布，这次运行也不会改变行为。
    调用方必须先经 `_claim_run_id` 成功认领 run_id。
    """
    try:
        run = execute(
            binding.flow,
            run_id=run_id,
            flow_version=binding.version,
            flow_content_hash=binding.content_hash,
            resumed_from=resumed_from,
            batch_id=batch_id,
        )

        record = {
            **run.to_dict(),
            "flow_status_at_start": binding.status.value,
        }
        _record_memory(run)
        try:
            get_store().save_run(
                run, source_key=_source_key_of(binding.flow), receipt_id=run.receipt_id
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("persist run failed: %s", exc)

        try:
            get_definitions().record_run_event(
                "run_finished",
                actor=actor,
                run_id=run.run_id,
                flow=binding.flow_name,
                version=binding.version,
                detail={
                    "status": run.status.value,
                    "failure_kind": run.failure_kind,
                    "error": run.error,
                    "content_hash": binding.content_hash,
                    "batch_id": batch_id,
                    "resumed_from": resumed_from,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不阻断执行结果
            logging.warning("record run audit failed: %s", exc)
        return run, record
    finally:
        _release_run_id(run_id)


@app.post("/flows/{name}/run")
def run_flow(
    name: str,
    payload: RunRequest | None = Body(default=None),
    x_actor: str | None = Header(default=None),
) -> JSONResponse:
    payload = payload or RunRequest()
    registry = get_definitions()
    actor = _actor(x_actor)

    # 1) 幂等重试：同 run_id 已存在则不再执行，也不会切换到别的版本。
    if payload.run_id:
        existing = _lookup_run(payload.run_id)
        if existing is not None:
            if payload.idempotent:
                return JSONResponse(
                    {**existing, "already_exists": True},
                    status_code=200 if existing.get("status") == "succeeded" else 502,
                )
            # 非幂等却复用已存在的 run_id 会造成运行记录被覆盖、版本绑定被偷换。
            raise ConflictError(
                f"run_id {payload.run_id} 已存在；重跑请使用 /runs/{payload.run_id}/resume，"
                "它会沿用原运行绑定的版本",
                code="run_exists",
            )

    # 2) 确认本次运行绑定的版本；草稿/无发布版本/数据集删除都在这里被拒绝。
    binding = registry.resolve_for_run(
        name, version=payload.version, allow_deprecated=payload.allow_deprecated
    )
    if not _claim_run_id(payload.run_id):
        raise ConflictError(
            f"run_id {payload.run_id} 正在执行", code="run_in_flight"
        )
    run, record = _execute_binding(
        binding, actor=actor, run_id=payload.run_id, batch_id=payload.batch_id
    )
    code = 200 if run.status.value == "succeeded" else 502
    return JSONResponse(record, status_code=code)


@app.post("/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    payload: ResumeRequest | None = Body(default=None),
    x_actor: str | None = Header(default=None),
) -> JSONResponse:
    """沿用原运行绑定的版本重新执行。

    这是"重试/恢复不能偷偷切换行为"的落点：即使该版本之后被废弃、甚至有
    更新版本发布，恢复也只使用原运行记录的 `(flow_version, flow_content_hash)`。
    """
    payload = payload or ResumeRequest()
    actor = _actor(x_actor)
    original = _lookup_run(run_id)
    if original is None:
        raise NotFoundError(f"run not found: {run_id}", code="run_not_found")
    if original.get("status") == "succeeded" and not payload.force:
        raise ConflictError(
            f"运行 {run_id} 已成功，恢复会产生重复单据；如确需重跑请显式 force=true",
            code="run_already_succeeded",
        )
    version = original.get("flow_version")
    if version is None:
        raise ConflictError(
            f"运行 {run_id} 没有记录定义版本（来自未治理的历史数据），无法安全恢复",
            code="run_version_unknown",
        )
    registry = get_definitions()
    binding = registry.resolve_for_run(
        original["flow"], version=int(version), allow_deprecated=True
    )
    expected_hash = original.get("flow_content_hash")
    if expected_hash and expected_hash != binding.content_hash:
        raise ConflictError(
            "版本内容哈希与运行记录不一致，拒绝恢复",
            code="content_hash_mismatch",
            detail={"expected": expected_hash, "actual": binding.content_hash},
        )
    new_run_id = payload.run_id
    if new_run_id and _lookup_run(new_run_id) is not None:
        raise ConflictError(f"run_id {new_run_id} 已存在", code="run_exists")
    if not _claim_run_id(new_run_id):
        raise ConflictError(f"run_id {new_run_id} 正在执行", code="run_in_flight")
    new_run, record = _execute_binding(
        binding,
        actor=actor,
        run_id=new_run_id,
        batch_id=original.get("batch_id"),
        resumed_from=run_id,
    )
    code = 200 if new_run.status.value == "succeeded" else 502
    return JSONResponse(record, status_code=code)


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
    except Exception as exc:  # noqa: BLE001
        with _memory_runs_lock:
            items = list(_memory_runs.values())
        if flow:
            items = [r for r in items if r["flow"] == flow]
        items.sort(key=lambda r: r["started_at"], reverse=True)
        return {"runs": items[:limit], "degraded": True, "reason": str(exc)}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    row = _lookup_run(run_id)
    if row is None:
        raise NotFoundError(f"run not found: {run_id}", code="run_not_found")
    return _enrich_run(row)


_FAILURE_HINTS = {
    "definition": "定义发布/数据契约问题：步骤、选择器或数据集字段与目标系统不一致，"
    "应回到流程定义审查与发布流程处理。",
    "runtime": "执行期问题：目标系统不可用、超时或网关错误，与定义版本无关；"
    "可在同一版本上重试/恢复。",
    "unknown": "无法自动归类，需要结合错误文本与定义审查记录人工判断。",
}


def _enrich_run(row: dict[str, Any]) -> dict[str, Any]:
    """给运行记录补充版本绑定与归因信息，便于区分定义问题与执行期问题。"""
    out = dict(row)
    version = row.get("flow_version")
    flow_name = row.get("flow")
    if flow_name and version is not None:
        try:
            binding = get_definitions().get_version(flow_name, int(version))
            out["definition"] = {
                "version": binding["version"],
                "status": binding["status"],
                "content_hash": binding["content_hash"],
                "published_at": binding.get("published_at"),
                "deprecated_at": binding.get("deprecated_at"),
                "deprecated_reason": binding.get("deprecated_reason"),
                "review": binding.get("review"),
                "content": binding.get("content"),
            }
            if row.get("flow_content_hash") and row["flow_content_hash"] != binding["content_hash"]:
                out["binding_warning"] = (
                    "运行记录的内容哈希与注册表当前该版本内容不一致，"
                    "说明注册表状态文件被外部改动过"
                )
        except GovernanceError:
            out["definition"] = None
            out["binding_warning"] = f"注册表中找不到 {flow_name} v{version}"
    if row.get("status") == "failed":
        kind = row.get("failure_kind") or "unknown"
        out["diagnosis"] = {
            "failure_kind": kind,
            "hint": _FAILURE_HINTS.get(kind, _FAILURE_HINTS["unknown"]),
            "failed_step": row.get("failed_step"),
            "failed_step_kind": row.get("failed_step_kind"),
        }
        if row.get("failed_step") is not None and out.get("definition"):
            content = (out.get("definition") or {}).get("content") or {}
            steps = content.get("steps") or []
            idx = row["failed_step"]
            if isinstance(idx, int) and 0 <= idx < len(steps):
                out["diagnosis"]["failed_step_definition"] = steps[idx]
    try:
        out["audit"] = get_definitions().audit_events(run_id=row["run_id"], limit=50)
    except Exception:  # noqa: BLE001
        out["audit"] = []
    return out


@app.get("/stats")
def stats() -> dict[str, Any]:
    try:
        return get_store().stats()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"store unavailable: {exc}") from exc


# --------------------------------------------------------------------------- #
# 审计与数据集
# --------------------------------------------------------------------------- #


@app.get("/audit")
def audit(
    flow: str | None = None,
    action: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {
        "events": get_definitions().audit_events(
            flow=flow, action=action, run_id=run_id, limit=limit
        )
    }


@app.get("/datasets")
def list_datasets() -> dict[str, Any]:
    return {"datasets": get_definitions().list_datasets()}


@app.put("/datasets/{name}")
def upsert_dataset(
    name: str,
    payload: DatasetRequest,
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    return get_definitions().upsert_dataset(name, payload.model_dump(), actor=_actor(x_actor))


@app.delete("/datasets/{name}")
def delete_dataset(
    name: str,
    reason: str = "",
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    return get_definitions().delete_dataset(name, actor=_actor(x_actor), reason=reason)


@app.post("/admin/sync-flows")
def sync_flows(x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    imported = get_definitions().sync_from_files(actor=_actor(x_actor))
    return {"imported": imported, "count": len(imported)}


@app.post("/admin/reset-batch")
def reset_batch(flow: str) -> dict[str, Any]:
    with get_store()._conn() as conn:  # noqa: SLF001 - 内部管理接口
        cur = conn.execute("DELETE FROM submissions WHERE flow = %s", (flow,))
        return {"deleted": cur.rowcount}


# 保留给脚本 / 调试：直接读文件而不经过治理，明确标注为只读预览。
@app.get("/admin/flows-on-disk")
def flows_on_disk() -> dict[str, Any]:
    out = []
    for path in sorted(glob.glob(os.path.join(settings.flows_dir, "*.yaml"))):
        try:
            flow = Flow.from_file(path)
        except Exception as exc:  # noqa: BLE001
            out.append({"file": os.path.basename(path), "error": str(exc)})
            continue
        out.append({"file": os.path.basename(path), **summarize(flow)})
    return {"flows": out}

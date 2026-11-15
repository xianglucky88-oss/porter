"""mockapp —— 模拟内部表单系统。

这是 RPA 流程要操作的目标站点。它刻意做成了"看起来像内部老系统"的样子：

  * 表单一页一个，提交后 302 跳转到回执页；
  * 服务端有必填校验；
  * 某些提交会很慢（用于触发客户端超时重试）；
  * 提交接口支持幂等键，但如果客户端不传，就会真实产生重复单据；
  * 页面会"改版"：同一表单存在多套布局（字段标签位置、容器层级、
    控件 id、按钮形态都不同，但业务含义完全一致），可通过
    /admin/layout 切换，用于验证 RPA 的定位回退能力。

所有随机行为由 seed 与提交序号决定，因此完全可复现。

端点：
    GET  /form/{form_type}              HTML 表单（按当前布局版本渲染）
    GET  /api/form/{form_type}          表单 schema（JSON）
    POST /api/submit                    JSON 提交
    POST /submit                        HTML 表单提交（浏览器路径）
    GET  /receipt/{submission_id}       回执页
    GET  /admin/records                 所有已受理单据
    GET  /admin/state                   计数与故障状态（含当前布局版本）
    POST /admin/layout?version=v2       切换页面布局版本
    POST /admin/fault?kind=500&rate=0.2&seed=3
    POST /admin/fault/clear
    POST /admin/reset
    GET  /healthz
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="mockapp", version="2.7.1")

FORMS: dict[str, dict[str, Any]] = {
    "purchase_requisition": {
        "title": "采购申请单",
        "fields": [
            {"name": "applicant", "label": "申请人", "required": True},
            {"name": "department", "label": "申请部门", "required": True},
            {"name": "supplier_code", "label": "供应商编码", "required": True},
            {"name": "amount_cents", "label": "金额（分）", "required": True},
            {"name": "budget_code", "label": "预算科目", "required": False},
        ],
    },
    "reimbursement": {
        "title": "费用报销单",
        "fields": [
            {"name": "applicant", "label": "申请人", "required": True},
            {"name": "expense_type", "label": "费用类型", "required": True},
            {"name": "amount_cents", "label": "金额（分）", "required": True},
            {"name": "invoice_no", "label": "发票号", "required": True},
            {"name": "remark", "label": "备注", "required": False},
        ],
    },
    "account_opening": {
        "title": "开户申请",
        "fields": [
            {"name": "company_name", "label": "企业名称", "required": True},
            {"name": "legal_person", "label": "法人", "required": True},
            {"name": "uscc", "label": "统一社会信用代码", "required": True},
            {"name": "bank_branch", "label": "开户网点", "required": False},
        ],
    },
}


@dataclass
class Submission:
    submission_id: str
    form_type: str
    source_key: str
    idempotency_key: str | None
    fields: dict[str, str]
    received_at: float
    replayed: bool = False


class State:
    """进程内状态。单实例足够，重启即清空（mockapp 不持久化）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.submissions: list[Submission] = []
        self.by_idem: dict[str, Submission] = {}
        self.by_source: dict[str, list[Submission]] = {}
        self.seq = 0
        self.fault: dict[str, Any] | None = None
        self.fault_rnd = random.Random(0)
        self.layout = "v1"

    def reset(self) -> None:
        with self.lock:
            self.submissions.clear()
            self.by_idem.clear()
            self.by_source.clear()
            self.seq = 0
            self.fault = None
            self.layout = "v1"


STATE = State()


def _maybe_fault() -> None:
    """按配置的概率注入 5xx。"""
    f = STATE.fault
    if not f or f["rate"] <= 0:
        return
    with STATE.lock:
        hit = STATE.fault_rnd.random() < f["rate"]
    if hit:
        raise HTTPException(status_code=500, detail="upstream gateway error")


def _next_seq() -> int:
    with STATE.lock:
        STATE.seq += 1
        return STATE.seq


def _is_slow(seq: int) -> bool:
    """每第 7 次提交故意慢 12 秒，用于触发客户端超时重试。

    用提交序号而不是随机数，保证"第几次提交会慢"是确定的，
    因此同一个流程每次回放的表现一致。
    """
    return seq % 7 == 0


def _render_v1(spec: dict[str, Any], form_type: str) -> str:
    """v1 经典布局：<p> 容器，label[for] + input#id，按钮 #submit-btn。"""
    rows = []
    for f in spec["fields"]:
        req = "required" if f["required"] else ""
        star = "*" if f["required"] else ""
        rows.append(
            f'<p><label for="{f["name"]}">{f["label"]}{star}</label>'
            f'<input id="{f["name"]}" name="{f["name"]}" {req}></p>'
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{spec['title']}</title></head>
<body>
<h1>{spec['title']}</h1>
<form id="entry-form" method="post" action="/submit">
  <input type="hidden" name="form_type" value="{form_type}">
  <p><label for="source_key">业务单号</label><input id="source_key" name="source_key" required></p>
  {' '.join(rows)}
  <button id="submit-btn" type="submit">提交</button>
</form>
</body></html>"""


def _render_v2(spec: dict[str, Any], form_type: str) -> str:
    """v2 改版：div.form-row 容器，label 包裹 input，控件无 id，按钮改名。

    业务含义与 v1 完全一致（字段集合、必填规则、提交语义不变），
    只是 DOM 结构变了——这正是内部系统前端重构的典型形态。
    """
    rows = []
    for f in spec["fields"]:
        req = "required" if f["required"] else ""
        star = "*" if f["required"] else ""
        rows.append(
            f'<div class="form-row"><label>{f["label"]}{star}'
            f'<input name="{f["name"]}" {req}></label></div>'
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{spec['title']}</title></head>
<body>
<main class="page"><h1>{spec['title']}</h1>
<form id="entry-form" method="post" action="/submit">
  <input type="hidden" name="form_type" value="{form_type}">
  <div class="form-row"><label>业务单号<input name="source_key" required></label></div>
  {' '.join(rows)}
  <div class="actions"><button id="primary-action" type="submit">提交</button></div>
</form>
</main></body></html>"""


def _render_v3(spec: dict[str, Any], form_type: str) -> str:
    """v3 组件化改版：section/grid 布局，label 与控件分离（无 for），
    id 为组件库自动生成（cmp-N），name 带命名空间（form[xxx]），
    稳定标识只剩 data-field；提交按钮是 input[type=submit]。
    """
    rows = []
    for i, f in enumerate(spec["fields"]):
        req = "required" if f["required"] else ""
        star = "*" if f["required"] else ""
        rows.append(
            f'<div class="grid-row"><span class="cell-label">{f["label"]}{star}</span>'
            f'<span class="cell-control">'
            f'<input id="cmp-{100 + i}" name="form[{f["name"]}]" data-field="{f["name"]}" {req}>'
            f"</span></div>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{spec['title']}</title></head>
<body>
<section class="panel"><header><h1>{spec['title']}</h1></header>
<form id="entry-form" method="post" action="/submit">
  <input type="hidden" name="form_type" value="{form_type}">
  <div class="grid-row"><span class="cell-label">业务单号</span>
    <span class="cell-control"><input id="cmp-99" name="form[source_key]" data-field="source_key" required></span></div>
  {' '.join(rows)}
  <div class="grid-row"><span class="cell-label"></span>
    <span class="cell-control"><input type="submit" id="do-submit" value="提交"></span></div>
</form>
</section></body></html>"""


def _render_ambiguous(spec: dict[str, Any], form_type: str) -> str:
    """歧义布局：v1 基础上多了一个"批量填充"折叠区块，其中的输入框
    与主表单的金额字段 id、name 完全重复（前端复制粘贴的典型事故）。

    用于验证定位器遇到多个候选时拒绝猜测、绝不写入相邻/错误字段。
    """
    base = _render_v1(spec, form_type)
    dup = (
        '<fieldset id="bulk-fill"><legend>批量填充</legend>'
        '<p><label for="amount_cents">金额（分）</label>'
        '<input id="amount_cents" name="amount_cents"></p>'
        "</fieldset>"
    )
    return base.replace('<button id="submit-btn"', dup + '<button id="submit-btn"')


LAYOUTS = {
    "v1": _render_v1,
    "v2": _render_v2,
    "v3": _render_v3,
    "ambiguous": _render_ambiguous,
}


def _render_form(form_type: str) -> str:
    spec = FORMS[form_type]
    with STATE.lock:
        layout = STATE.layout
    return LAYOUTS.get(layout, _render_v1)(spec, form_type)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/form/{form_type}", response_class=HTMLResponse)
def form_page(form_type: str) -> str:
    if form_type not in FORMS:
        raise HTTPException(status_code=404, detail="unknown form")
    return _render_form(form_type)


@app.get("/api/form/{form_type}")
def form_schema(form_type: str) -> dict[str, Any]:
    if form_type not in FORMS:
        raise HTTPException(status_code=404, detail="unknown form")
    return {"form_type": form_type, **FORMS[form_type]}


@app.post("/api/submit")
async def api_submit(request: Request) -> JSONResponse:
    """JSON 提交。

    请求体：
        {
          "form_type": "...",
          "source_key": "...",
          "idempotency_key": "...",   # 可选；不传则不去重
          "fields": {...}
        }
    """
    _maybe_fault()
    body = await request.json()
    return _accept(body, via_html=False)


@app.post("/submit")
async def html_submit(request: Request) -> Any:
    """HTML 表单提交。字段从 form 里逐个取。

    浏览器提交的是 application/x-www-form-urlencoded，用标准库解析，
    不引入 python-multipart 依赖。

    v3 布局的控件 name 带命名空间（form[xxx]），这里做兼容提取——
    服务端按业务字段名归一化，布局差异不影响受理逻辑。
    """
    _maybe_fault()
    raw = await request.body()
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    form = {k: v[0] for k, v in parsed.items()}

    def _get(key: str) -> str | None:
        v = form.get(key)
        if v is None:
            v = form.get(f"form[{key}]")
        return None if v is None else str(v)

    form_type = _get("form_type") or ""
    spec = FORMS.get(form_type)
    if not spec:
        raise HTTPException(status_code=404, detail="unknown form")
    source_key = _get("source_key") or ""
    fields = {}
    for f in spec["fields"]:
        v = _get(f["name"])
        if v is not None:
            fields[f["name"]] = v
    body = {
        "form_type": form_type,
        "source_key": source_key,
        "idempotency_key": _get("idempotency_key"),
        "fields": fields,
    }
    resp = _accept(body, via_html=True)
    return resp


def _accept(body: dict[str, Any], via_html: bool) -> Any:
    form_type = str(body.get("form_type") or "")
    spec = FORMS.get(form_type)
    if not spec:
        raise HTTPException(status_code=400, detail=f"unknown form_type {form_type!r}")

    source_key = (body.get("source_key") or "").strip()
    if not source_key:
        raise HTTPException(status_code=422, detail="source_key is required")

    fields = body.get("fields") or {}
    missing = [f["name"] for f in spec["fields"] if f["required"] and not str(fields.get(f["name"], "")).strip()]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing required fields: {', '.join(missing)}")

    idem = body.get("idempotency_key") or None

    # 幂等重放：同一 idempotency_key 只受理一次。
    if idem:
        with STATE.lock:
            seen = STATE.by_idem.get(idem)
        if seen:
            seen.replayed = True
            payload = _submission_json(seen)
            payload["idempotent_replay"] = True
            return JSONResponse(payload, status_code=200, headers={"X-Idempotent-Replay": "true"})

    seq = _next_seq()
    slow = _is_slow(seq)
    if slow:
        # 慢提交：模拟网关重试窗口内的后端阻塞。
        time.sleep(12)

    sub = Submission(
        submission_id=uuid.uuid4().hex[:12],
        form_type=form_type,
        source_key=source_key,
        idempotency_key=idem,
        fields={k: str(v) for k, v in fields.items()},
        received_at=time.time(),
    )
    with STATE.lock:
        STATE.submissions.append(sub)
        if idem:
            STATE.by_idem[idem] = sub
        STATE.by_source.setdefault(source_key, []).append(sub)

    if via_html:
        return RedirectResponse(url=f"/receipt/{sub.submission_id}", status_code=303)
    return JSONResponse(_submission_json(sub), status_code=201)


def _submission_json(s: Submission) -> dict[str, Any]:
    return {
        "submission_id": s.submission_id,
        "form_type": s.form_type,
        "source_key": s.source_key,
        "idempotency_key": s.idempotency_key,
        "fields": s.fields,
    }


@app.get("/receipt/{submission_id}", response_class=HTMLResponse)
def receipt(submission_id: str) -> str:
    with STATE.lock:
        found = next((s for s in STATE.submissions if s.submission_id == submission_id), None)
    if not found:
        raise HTTPException(status_code=404, detail="no such receipt")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>
<body><h1>提交成功</h1><p id="receipt-id">{found.submission_id}</p>
<p id="source-key">{found.source_key}</p></body></html>"""


@app.get("/admin/records")
def records() -> dict[str, Any]:
    with STATE.lock:
        per_source: dict[str, int] = {k: len(v) for k, v in STATE.by_source.items()}
        return {
            "total": len(STATE.submissions),
            "distinct_source_keys": len(STATE.by_source),
            "per_source": per_source,
            "submissions": [_submission_json(s) for s in STATE.submissions],
        }


@app.get("/admin/state")
def state() -> dict[str, Any]:
    with STATE.lock:
        per_source = {k: len(v) for k, v in STATE.by_source.items()}
        return {
            "total": len(STATE.submissions),
            "seq": STATE.seq,
            "distinct_source_keys": len(STATE.by_source),
            "duplicates": sum(max(0, n - 1) for n in per_source.values()),
            "per_source": per_source,
            "fault": STATE.fault,
            "layout": STATE.layout,
        }


@app.post("/admin/layout")
def set_layout(version: str) -> dict[str, Any]:
    """切换页面布局版本，模拟内部系统改版（业务含义不变）。"""
    if version not in LAYOUTS:
        raise HTTPException(
            status_code=400, detail=f"unknown layout {version!r}; known: {sorted(LAYOUTS)}"
        )
    with STATE.lock:
        STATE.layout = version
    return {"layout": version}


@app.post("/admin/fault")
def set_fault(kind: str = "500", rate: float = 0.2, seed: int = 3) -> dict[str, Any]:
    if kind not in ("500",):
        raise HTTPException(status_code=400, detail="kind must be 500")
    with STATE.lock:
        STATE.fault = {"kind": kind, "rate": rate, "seed": seed}
        STATE.fault_rnd = random.Random(seed)
    return STATE.fault


@app.post("/admin/fault/clear")
def clear_fault() -> dict[str, bool]:
    with STATE.lock:
        STATE.fault = None
    return {"cleared": True}


@app.post("/admin/reset")
def reset() -> dict[str, bool]:
    STATE.reset()
    return {"reset": True}


# 用于独立启动：python -m mockapp
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9200)

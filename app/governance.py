"""流程定义的受治理生命周期。

目标：流程定义不再"能被解析就直接执行"。任何进入可运行状态的定义都必须

1. 以**不可变版本**的形式存在；
2. 在发布前通过静态审查（结构 / 步骤依赖 / 敏感数据 / 高风险动作 / 数据集引用）；
3. 发布时做向后兼容性检查；
4. 每一次状态变化写入审计日志。

状态机：

    draft ──publish──> published ──deprecate──> deprecated
      │                    │                        │
      │                    └──superseded by vN──┐   │
      └──────── updated (revision+1)            ▼   │
                                              deprecated
    rollback(to_version) 会复制历史版本内容，生成一个**新**版本再发布。

版本号在同一流程内单调递增、永不回收；已发布版本的 content 永不修改。
运行记录绑定 `(flow, version, content_hash)`，因此新旧定义并存时，
重试/恢复只会沿用启动时确认的那一版，不会静默切换。

持久化：进程内用 `threading.RLock`，进程间用 `fcntl.flock` 锁同一个
状态文件，写入采用 tmp-file + os.replace 的原子替换。这样同名定义的并发
编辑会表现为 revision/base_version 冲突，而不是互相覆盖。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .config import settings
from .models import Flow, StepKind

log = logging.getLogger("porter.governance")

try:  # pragma: no cover - 平台分支
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class GovernanceError(Exception):
    """生命周期操作的领域错误。API 层统一映射为 HTTP 状态码。"""

    status_code = 400
    code = "governance_error"

    def __init__(self, message: str, *, code: str | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            body["detail"] = self.detail
        return body


class NotFoundError(GovernanceError):
    status_code = 404
    code = "not_found"


class ConflictError(GovernanceError):
    status_code = 409
    code = "conflict"


class ValidationError(GovernanceError):
    status_code = 422
    code = "validation_failed"


# --------------------------------------------------------------------------- #
# 枚举与数据结构
# --------------------------------------------------------------------------- #


class VersionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class Finding:
    """一条静态审查结论。"""

    code: str
    severity: Severity
    message: str
    step_index: int | None = None
    # 高风险动作需要发布人显式确认（acknowledge）后才能发布。
    requires_acknowledgement: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "step_index": self.step_index,
            "requires_acknowledgement": self.requires_acknowledgement,
        }


@dataclass
class ReviewReport:
    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        code: str,
        severity: Severity,
        message: str,
        step_index: int | None = None,
        requires_acknowledgement: bool = False,
    ) -> None:
        self.findings.append(
            Finding(
                code=code,
                severity=severity,
                message=message,
                step_index=step_index,
                requires_acknowledgement=requires_acknowledgement,
            )
        )

    def error(self, code: str, message: str, step_index: int | None = None) -> None:
        self.add(code, Severity.ERROR, message, step_index)

    def warn(self, code: str, message: str, step_index: int | None = None) -> None:
        self.add(code, Severity.WARNING, message, step_index)

    @property
    def errors(self) -> list[Finding]:
        """真正阻断发布的问题。

        需要显式确认的高风险动作不计入这里：它们由 `ack_required` 单独表达，
        发布时可以通过 `acknowledge` 逐项放行，而不是被一律拒绝。
        """
        return [
            f
            for f in self.findings
            if f.severity is Severity.ERROR and not f.requires_acknowledgement
        ]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ack_required(self) -> list[Finding]:
        return [f for f in self.findings if f.requires_acknowledgement]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.ack_required

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "publishable": not self.errors and not self.ack_required,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "ack_required": sorted({f.code for f in self.ack_required}),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class VersionBinding:
    """一次运行在启动时确认的定义版本。"""

    flow_name: str
    version: int
    status: VersionStatus
    content_hash: str
    flow: Flow

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow": self.flow_name,
            "version": self.version,
            "status": self.status.value,
            "content_hash": self.content_hash,
        }


# --------------------------------------------------------------------------- #
# 数据集目录
# --------------------------------------------------------------------------- #

# 与 mockapp 的表单 schema 对齐。risk_level=high 表示涉及资金/主体资格，
# sensitive_fields 中的字段出现在流程定义里时需要额外关注。
DEFAULT_DATASETS: dict[str, dict[str, Any]] = {
    "purchase_requisition": {
        "description": "采购申请单",
        "risk_level": "high",
        "fields": [
            {"name": "applicant", "required": True},
            {"name": "department", "required": True},
            {"name": "supplier_code", "required": True},
            {"name": "amount_cents", "required": True},
            {"name": "budget_code", "required": False},
        ],
        "sensitive_fields": ["amount_cents"],
    },
    "reimbursement": {
        "description": "费用报销单",
        "risk_level": "high",
        "fields": [
            {"name": "applicant", "required": True},
            {"name": "expense_type", "required": True},
            {"name": "amount_cents", "required": True},
            {"name": "invoice_no", "required": True},
            {"name": "remark", "required": False},
        ],
        "sensitive_fields": ["invoice_no", "amount_cents"],
    },
    "account_opening": {
        "description": "开户申请",
        "risk_level": "high",
        "fields": [
            {"name": "company_name", "required": True},
            {"name": "legal_person", "required": True},
            {"name": "uscc", "required": True},
            {"name": "bank_branch", "required": False},
        ],
        "sensitive_fields": ["legal_person", "uscc", "bank_branch"],
    },
}

_SENSITIVE_HINTS = (
    "id_card",
    "idcard",
    "uscc",
    "bank_account",
    "bank_card",
    "card_no",
    "credit_card",
    "phone",
    "mobile",
    "password",
    "passwd",
    "secret",
    "token",
    "salary",
    "wage",
    "身份证",
    "银行卡",
    "密码",
    "手机号",
    "工资",
    "薪酬",
    "信用代码",
    "法人",
)

_DESTRUCTIVE_RE = re.compile(
    r"(?i)(delete|remove|approve|pay\b|transfer|refund|void|destroy|drop|注销|删除|审批|付款|转账)"
)

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_KNOWN_PLACEHOLDERS = {"date", "ts", "run_id"}


def _is_sensitive_name(name: str) -> bool:
    low = (name or "").lower()
    return any(hint in low for hint in _SENSITIVE_HINTS)


def _selector_field(selector: str | None, text: str | None) -> str:
    """从选择器 / 字段名里取出业务字段名。"""
    if text:
        return text.strip()
    if not selector:
        return ""
    return selector.split("#", 1)[-1].strip()


# --------------------------------------------------------------------------- #
# 静态审查
# --------------------------------------------------------------------------- #


def review_definition(raw: Any, datasets: dict[str, dict[str, Any]]) -> ReviewReport:
    """发布前静态审查：结构、步骤依赖、敏感数据、高风险动作、数据集引用。

    不抛异常：解析失败会变成 `invalid_definition` 错误，便于草稿阶段保存半成品。
    """
    report = ReviewReport()
    if not isinstance(raw, dict):
        report.error("invalid_definition", "流程定义必须是 YAML/JSON 对象")
        return report

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        report.error("missing_name", "流程定义缺少 name")
    elif not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
        report.warn("unusual_name", f"name={name!r} 不符合小写下划线命名约定")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        report.error("missing_steps", "流程定义必须包含非空的 steps 列表")
        return report

    # 先做不依赖完整解析的结构检查，尽量多收集问题。
    for idx, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            report.error("invalid_step", f"step {idx} 不是对象", idx)
            continue
        kind = item.get("kind")
        if kind not in {k.value for k in StepKind}:
            report.error("unknown_step_kind", f"step {idx} 的 kind={kind!r} 不受支持", idx)

    try:
        flow = Flow.from_dict(raw)
    except Exception as exc:  # noqa: BLE001 - 解析错误就是审查结论
        report.error("invalid_definition", f"流程定义无法解析: {exc}")
        return report

    report.findings.extend(_review_structure(flow))
    report.findings.extend(_review_dependencies(flow))
    report.findings.extend(_review_sensitive_data(flow, datasets))
    report.findings.extend(_review_high_risk(flow, datasets))
    report.findings.extend(_review_dataset(flow, datasets))
    return report


def _review_structure(flow: Flow) -> list[Finding]:
    out: list[Finding] = []
    kinds = [s.kind for s in flow.steps]
    if kinds[0] is not StepKind.OPEN:
        out.append(Finding("first_step_not_open", Severity.ERROR, "第一个步骤必须是 open"))
    if StepKind.OPEN not in kinds:
        out.append(Finding("missing_open", Severity.ERROR, "流程缺少 open 步骤"))
    if StepKind.SUBMIT not in kinds:
        out.append(Finding("missing_submit", Severity.ERROR, "流程缺少 submit 步骤"))
    if kinds.count(StepKind.SUBMIT) > 1:
        out.append(Finding("multiple_submit", Severity.ERROR, "流程只能有一个 submit 步骤"))
    if kinds[-1] not in (StepKind.SUBMIT, StepKind.ASSERT_TEXT, StepKind.WAIT):
        out.append(
            Finding(
                "bad_last_step",
                Severity.ERROR,
                "最后一步必须是 submit 或断言/等待",
            )
        )

    opens = [i for i, s in enumerate(flow.steps) if s.kind is StepKind.OPEN]
    if len(opens) > 1:
        out.append(Finding("multiple_open", Severity.WARNING, "流程包含多个 open 步骤"))

    for idx, step in enumerate(flow.steps):
        if step.kind is StepKind.OPEN and not (step.value or step.selector):
            out.append(Finding("open_without_url", Severity.ERROR, "open 步骤缺少 value(url)", idx))
        if step.kind in (StepKind.FILL, StepKind.SELECT) and not (
            step.selector or step.text
        ):
            out.append(
                Finding(
                    "step_without_target",
                    Severity.ERROR,
                    "fill/select 缺少 selector 或 text",
                    idx,
                )
            )
        if step.kind is StepKind.CLICK and not step.selector:
            out.append(Finding("click_without_target", Severity.ERROR, "click 缺少 selector", idx))
        if step.kind is StepKind.WAIT:
            try:
                float(step.value or "1")
            except ValueError:
                out.append(Finding("invalid_wait", Severity.ERROR, "wait 的 value 必须是数字", idx))
        if step.kind is StepKind.ASSERT_TEXT and not step.selector:
            out.append(
                Finding(
                    "assert_without_selector", Severity.WARNING, "assert_text 缺少 selector", idx
                )
            )
        if step.kind is StepKind.SUBMIT and step.value:
            out.append(
                Finding("submit_value_ignored", Severity.WARNING, "submit 的 value 会被忽略", idx)
            )
    return out


def _review_dependencies(flow: Flow) -> list[Finding]:
    """步骤依赖：断言必须在提交之后、提交之后不应再修改表单、占位符必须可解析。"""
    out: list[Finding] = []
    submit_index = next(
        (i for i, s in enumerate(flow.steps) if s.kind is StepKind.SUBMIT), None
    )

    for idx, step in enumerate(flow.steps):
        if (
            step.kind is StepKind.ASSERT_TEXT
            and (step.selector or "").endswith("#receipt-id")
            and (submit_index is None or idx < submit_index)
        ):
            out.append(
                Finding(
                    "assert_before_submit",
                    Severity.ERROR,
                    "回执断言之前没有 submit，步骤顺序不成立",
                    idx,
                )
            )
        if submit_index is not None and idx > submit_index and step.kind in (
            StepKind.FILL,
            StepKind.SELECT,
            StepKind.CLICK,
            StepKind.OPEN,
        ):
            out.append(
                Finding(
                    "mutation_after_submit",
                    Severity.WARNING,
                    f"submit 之后仍有 {step.kind.value} 步骤，可能不会生效",
                    idx,
                )
            )

    fills = [
        _selector_field(s.selector, s.text)
        for s in flow.steps
        if s.kind in (StepKind.FILL, StepKind.SELECT)
    ]
    if fills.count("source_key") > 1:
        out.append(Finding("duplicate_source_key", Severity.WARNING, "source_key 被重复填写"))
    if "source_key" not in fills:
        out.append(Finding("missing_source_key", Severity.WARNING, "流程没有填写 source_key"))

    for idx, step in enumerate(flow.steps):
        for text in (step.selector, step.value, step.text, step.idempotency_key):
            for token in _PLACEHOLDER_RE.findall(text or ""):
                if token not in _KNOWN_PLACEHOLDERS:
                    out.append(
                        Finding(
                            "unknown_placeholder",
                            Severity.WARNING,
                            f"占位符 {{{token}}} 没有运行时来源，会按字面量发送",
                            idx,
                        )
                    )
    return out


def _review_sensitive_data(flow: Flow, datasets: dict[str, dict[str, Any]]) -> list[Finding]:
    out: list[Finding] = []
    dataset = datasets.get(flow.dataset or flow.name) or {}
    sensitive_fields = set(dataset.get("sensitive_fields") or [])
    for idx, step in enumerate(flow.steps):
        field = _selector_field(step.selector, step.text)
        blob = " ".join(
            part for part in (step.selector or "", step.value or "", step.text or "") if part
        )
        if not blob:
            continue
        sensitive = _is_sensitive_name(blob) or field in sensitive_fields
        if not sensitive:
            continue
        # 纯占位符不是硬编码敏感值；有字面量内容才值得关注。
        literal = _PLACEHOLDER_RE.sub("", step.value or "").strip()
        if literal:
            out.append(
                Finding(
                    "sensitive_literal",
                    Severity.WARNING,
                    f"字段 {field or step.selector!r} 在定义中硬编码了敏感值，"
                    "建议改由运行时凭据/数据源注入",
                    idx,
                )
            )
    return out


def _review_high_risk(flow: Flow, datasets: dict[str, dict[str, Any]]) -> list[Finding]:
    out: list[Finding] = []
    dataset = datasets.get(flow.dataset or flow.name) or {}
    if dataset.get("risk_level") == "high":
        out.append(
            Finding(
                "high_risk_dataset",
                Severity.WARNING,
                f"数据集 {flow.dataset or flow.name} 被标记为高风险（资金/主体资格类）",
            )
        )

    for idx, step in enumerate(flow.steps):
        target = step.selector or ""
        if step.kind in (StepKind.CLICK, StepKind.SUBMIT) and _DESTRUCTIVE_RE.search(target):
            out.append(
                Finding(
                    "destructive_action",
                    Severity.ERROR,
                    f"步骤命中高风险动作选择器 {target!r}，发布前需要显式确认",
                    idx,
                    requires_acknowledgement=True,
                )
            )
        if step.kind is StepKind.SUBMIT and not step.idempotency_key:
            out.append(
                Finding(
                    "submit_without_idempotency",
                    Severity.WARNING,
                    "submit 没有携带 idempotency_key，重试可能产生重复单据",
                    idx,
                )
            )
    return out


def _review_dataset(flow: Flow, datasets: dict[str, dict[str, Any]]) -> list[Finding]:
    out: list[Finding] = []
    declared = flow.dataset
    if declared is None:
        out.append(Finding("dataset_unspecified", Severity.WARNING, "流程未声明 dataset，将回退使用流程名"))
        declared = flow.name
    dataset = datasets.get(declared)
    if dataset is None:
        out.append(Finding("dataset_unknown", Severity.ERROR, f"dataset {declared!r} 未在目录中登记"))
        return out
    if dataset.get("deleted"):
        out.append(Finding("dataset_deleted", Severity.ERROR, f"dataset {declared!r} 已被删除，不能发布"))
        return out

    schema_fields = {f["name"]: f for f in dataset.get("fields") or [] if "name" in f}
    filled = {
        _selector_field(s.selector, s.text)
        for s in flow.steps
        if s.kind in (StepKind.FILL, StepKind.SELECT)
    }
    filled.discard("")
    missing_required = sorted(
        name for name, spec in schema_fields.items() if spec.get("required") and name not in filled
    )
    if missing_required:
        out.append(
            Finding(
                "dataset_required_fields_missing",
                Severity.WARNING,
                "数据集必填字段未在流程中填写: " + ", ".join(missing_required),
            )
        )
    unknown = sorted(
        name
        for name in filled
        if schema_fields and name not in schema_fields and name != "source_key"
    )
    if unknown:
        out.append(
            Finding(
                "dataset_unknown_field",
                Severity.WARNING,
                "流程填写了数据集未声明的字段: " + ", ".join(unknown),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 向后兼容性
# --------------------------------------------------------------------------- #


def diff_definitions(old_raw: Any, new_raw: Any) -> dict[str, Any]:
    """比较两个定义，返回兼容性结论。

    `breaking` 列表非空时，发布默认被拒绝（除非显式 allow_breaking + reason）。
    """
    breaking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    try:
        old = Flow.from_dict(old_raw)
        new = Flow.from_dict(new_raw)
    except Exception as exc:  # noqa: BLE001
        return {
            "comparable": False,
            "breaking": [],
            "warnings": [{"code": "diff_unavailable", "message": f"无法比较: {exc}"}],
        }

    def _break(code: str, message: str) -> None:
        breaking.append({"code": code, "message": message})

    def _warn(code: str, message: str) -> None:
        warnings.append({"code": code, "message": message})

    if (old.dataset or old.name) != (new.dataset or new.name):
        _break(
            "dataset_changed",
            f"dataset 从 {old.dataset or old.name!r} 变为 {new.dataset or new.name!r}，"
            "新版本会写入不同业务表",
        )

    old_kinds = [s.kind.value for s in old.steps]
    new_kinds = [s.kind.value for s in new.steps]
    for kind in sorted(set(old_kinds) - set(new_kinds)):
        _break("step_kind_removed", f"移除了 {kind} 步骤")

    def _signature(flow: Flow) -> list[str]:
        out = []
        for step in flow.steps:
            target = step.selector or _selector_field(step.selector, step.text)
            out.append(f"{step.kind.value}:{target or ''}")
        return out

    old_sig, new_sig = _signature(old), _signature(new)
    if old_sig != new_sig and sorted(old_sig) == sorted(new_sig):
        _break("step_order_changed", "步骤顺序发生变化，已有批次的回放语义会不同")

    removed_targets = {
        sig for sig in set(old_sig) - set(new_sig) if sig.split(":", 1)[1]
    }
    for sig in sorted(removed_targets):
        if sig.split(":", 1)[0] in ("fill", "select"):
            _warn("step_target_removed", f"不再操作 {sig!r}")

    def _source_key(flow: Flow) -> str:
        for step in flow.steps:
            if (
                step.kind is StepKind.FILL
                and _selector_field(step.selector, step.text) == "source_key"
            ):
                return step.value or ""
        return ""

    if _source_key(old) != _source_key(new):
        _break(
            "source_key_changed",
            f"source_key 取值从 {_source_key(old)!r} 变为 {_source_key(new)!r}，"
            "会改变业务单据的幂等身份",
        )

    old_open = next((s.value or s.selector or "" for s in old.steps if s.kind is StepKind.OPEN), "")
    new_open = next((s.value or s.selector or "" for s in new.steps if s.kind is StepKind.OPEN), "")
    if old_open != new_open:
        _warn("open_url_changed", f"入口地址从 {old_open!r} 变为 {new_open!r}")

    old_idem = any(s.idempotency_key for s in old.steps if s.kind is StepKind.SUBMIT)
    new_idem = any(s.idempotency_key for s in new.steps if s.kind is StepKind.SUBMIT)
    if old_idem != new_idem:
        _warn("idempotency_changed", "submit 的幂等键策略发生变化")

    return {
        "comparable": True,
        "breaking": breaking,
        "warnings": warnings,
        "changed": bool(breaking or warnings),
    }


# --------------------------------------------------------------------------- #
# 规范化与哈希
# --------------------------------------------------------------------------- #


def canonical_definition(raw: Any) -> dict[str, Any]:
    """把定义规范化为稳定结构；无法解析时原样返回，留给审查阶段报告。"""
    if not isinstance(raw, dict):
        return {"raw": raw}
    try:
        flow = Flow.from_dict(raw)
    except Exception:  # noqa: BLE001 - 草稿允许不完整
        return copy.deepcopy(raw)
    return {
        "name": flow.name,
        "description": flow.description,
        "dataset": flow.dataset,
        "steps": [
            {
                "kind": step.kind.value,
                "selector": step.selector,
                "value": step.value,
                "text": step.text,
                "idempotency_key": step.idempotency_key,
                "optional": step.optional,
            }
            for step in flow.steps
        ],
    }


def content_hash(content: Any) -> str:
    payload = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_definition_payload(*, definition: Any = None, yaml_text: str | None = None) -> Any:
    """接受 JSON 对象或 YAML 文本。"""
    if definition is not None and yaml_text is not None:
        raise ValidationError("definition 与 yaml 只能提供一个", code="ambiguous_definition")
    if yaml_text is not None:
        import yaml

        try:
            return yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            raise ValidationError(f"YAML 解析失败: {exc}", code="invalid_yaml") from exc
    if definition is None:
        raise ValidationError("缺少 definition 或 yaml", code="missing_definition")
    return definition


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class DefinitionRegistry:
    """流程定义的版本仓库。

    状态文件结构::

        {
          "schema": 1,
          "flows": {
            "name": {
              "head_version": 2,
              "published_version": 2,
              "draft_version": null,
              "versions": {"1": {...}, "2": {...}}
            }
          },
          "datasets": {"name": {...}},
          "audit": [ {...} ]
        }
    """

    MAX_AUDIT = 5000

    def __init__(self, path: str | None = None, flows_dir: str | None = None) -> None:
        self.path = path or settings.definitions_path
        self.flows_dir = flows_dir or settings.flows_dir
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._bootstrap()

    # -- 文件读写 ---------------------------------------------------------- #

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "flows": {},
            "datasets": {name: copy.deepcopy(spec) for name, spec in DEFAULT_DATASETS.items()},
            "audit": [],
        }

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except FileNotFoundError:
            return self._empty_state()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"definition store corrupted at {self.path}: {exc}") from exc
        # 兼容旧状态文件：补齐新字段。
        state.setdefault("flows", {})
        state.setdefault("audit", [])
        datasets = state.setdefault("datasets", {})
        for name, spec in DEFAULT_DATASETS.items():
            if name not in datasets:
                datasets[name] = copy.deepcopy(spec)
        return state

    def _write(self, state: dict[str, Any]) -> None:
        state["audit"] = state.get("audit", [])[-self.MAX_AUDIT :]
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix=".definitions-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    class _FileLock:
        """跨进程写锁。Windows 退化为进程内锁。"""

        def __init__(self, path: str) -> None:
            self.path = path + ".lock"
            self.fh = None

        def __enter__(self) -> "DefinitionRegistry._FileLock":
            self.fh = open(self.path, "a+")
            if fcntl is not None:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc: Any) -> None:
            if self.fh is not None:
                if fcntl is not None:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()
                self.fh = None

    def _mutate(self, fn: Any) -> Any:
        with self._lock, self._FileLock(self.path):
            state = self._load()
            before = json.dumps(state, sort_keys=True, default=str)
            result = fn(state)
            if json.dumps(state, sort_keys=True, default=str) != before:
                self._write(state)
            return result

    # -- 审计 -------------------------------------------------------------- #

    @staticmethod
    def _audit(
        state: dict[str, Any],
        action: str,
        *,
        actor: str,
        flow: str | None = None,
        version: int | None = None,
        detail: Any = None,
        run_id: str | None = None,
    ) -> None:
        event = {
            "event_id": uuid.uuid4().hex,
            "at": _now(),
            "action": action,
            "actor": actor,
            "flow": flow,
            "version": version,
            "run_id": run_id,
            "detail": detail,
        }
        state["audit"].append(event)

    def audit_events(
        self,
        *,
        flow: str | None = None,
        action: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        state = self._load()
        events = state.get("audit", [])
        if flow:
            events = [e for e in events if e.get("flow") == flow]
        if action:
            events = [e for e in events if e.get("action") == action]
        if run_id:
            events = [e for e in events if e.get("run_id") == run_id]
        return list(reversed(events))[: max(0, limit)]

    # -- 初始化 ------------------------------------------------------------ #

    def _bootstrap(self) -> None:
        with self._lock, self._FileLock(self.path):
            state = self._load()
            changed = False
            if not state["flows"]:
                state, changed = self._seed_from_files(state)
            if changed:
                self._write(state)

    def _seed_from_files(self, state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """把仓库里既有的 YAML 作为 v1 引导发布。

        这是历史文件式定义到治理模型的迁移入口：审查照常执行，高风险项由
        `system:bootstrap` 记录显式确认，并留审计事件。之后的文件变更不会再
        自动发布，必须走 draft → publish。
        """
        import glob

        changed = False
        for path in sorted(glob.glob(os.path.join(self.flows_dir, "*.yaml"))):
            try:
                flow = Flow.from_file(path)
                raw = canonical_definition(
                    {
                        "name": flow.name,
                        "description": flow.description,
                        "dataset": flow.dataset,
                        "steps": [
                            {
                                "kind": s.kind.value,
                                "selector": s.selector,
                                "value": s.value,
                                "text": s.text,
                                "idempotency_key": s.idempotency_key,
                                "optional": s.optional,
                            }
                            for s in flow.steps
                        ],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("skip seeding %s: %s", path, exc)
                continue
            report = review_definition(raw, state["datasets"])
            ack = sorted({f.code for f in report.ack_required})
            # 引导发布只处理"无需人工确认"的定义：有错误或高风险动作时降级为草稿，
            # 让维护者走正常的确认-发布流程，而不是绕过治理。
            if report.errors or ack:
                log.warning(
                    "seed %s requires review: errors=%s ack=%s",
                    flow.name,
                    [f.code for f in report.errors],
                    ack,
                )
                status = VersionStatus.DRAFT
            else:
                status = VersionStatus.PUBLISHED
            entry = {
                "version": 1,
                "status": status.value,
                "content": raw,
                "content_hash": content_hash(raw),
                "created_at": _now(),
                "created_by": "system:bootstrap",
                "revision": 1,
                "base_version": None,
                "release_notes": "从 flows/ 目录引导发布",
                "review": report.to_dict(),
                "acknowledged": ack,
                "published_at": _now() if status is VersionStatus.PUBLISHED else None,
                "deprecated_at": None,
                "deprecated_reason": None,
                "rolled_back_from": None,
            }
            state["flows"][flow.name] = {
                "head_version": 1,
                "published_version": 1 if status is VersionStatus.PUBLISHED else None,
                "draft_version": None if status is VersionStatus.PUBLISHED else 1,
                "versions": {"1": entry},
            }
            self._audit(
                state,
                "seeded",
                actor="system:bootstrap",
                flow=flow.name,
                version=1,
                detail={
                    "status": status.value,
                    "content_hash": entry["content_hash"],
                    "acknowledged": ack,
                    "review": report.to_dict(),
                    "source_file": os.path.basename(path),
                },
            )
            changed = True
        return state, changed

    def sync_from_files(self, actor: str = "system:sync") -> list[dict[str, Any]]:
        """把新增或内容变化的 YAML 导入为草稿（绝不自动发布）。"""
        import glob

        imported: list[dict[str, Any]] = []

        def _sync(state: dict[str, Any]) -> list[dict[str, Any]]:
            for path in sorted(glob.glob(os.path.join(self.flows_dir, "*.yaml"))):
                try:
                    flow = Flow.from_file(path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("skip sync %s: %s", path, exc)
                    continue
                raw = canonical_definition(
                    {
                        "name": flow.name,
                        "description": flow.description,
                        "dataset": flow.dataset,
                        "steps": [
                            {
                                "kind": s.kind.value,
                                "selector": s.selector,
                                "value": s.value,
                                "text": s.text,
                                "idempotency_key": s.idempotency_key,
                                "optional": s.optional,
                            }
                            for s in flow.steps
                        ],
                    }
                )
                entry = state["flows"].get(flow.name)
                if entry is None:
                    imported.append(
                        self._new_draft(
                            state,
                            flow.name,
                            raw,
                            base_version=None,
                            actor=actor,
                            release_notes="自动导入新文件",
                        )
                    )
                elif entry.get("draft_version") is None:
                    # 与最近一个版本比较，而不是只跟发布版比较：
                    # 流程被废弃后没有发布版，否则每次同步都会重复生成草稿。
                    head = self._current_version(
                        state, flow.name, entry.get("head_version")
                    )
                    if head is None or content_hash(head["content"]) != content_hash(raw):
                        imported.append(
                            self._new_draft(
                                state,
                                flow.name,
                                raw,
                                base_version=entry.get("published_version"),
                                actor=actor,
                                release_notes="文件内容变化，导入为草稿",
                            )
                        )
            return imported

        return self._mutate(_sync)

    # -- 内部工具 ---------------------------------------------------------- #

    @staticmethod
    def _flow_entry(state: dict[str, Any], name: str) -> dict[str, Any] | None:
        return state["flows"].get(name)

    @staticmethod
    def _current_version(
        state: dict[str, Any], name: str, version: int | None
    ) -> dict[str, Any] | None:
        if version is None:
            return None
        entry = state["flows"].get(name, {}).get("versions", {}).get(str(version))
        return entry

    def _require_flow(self, state: dict[str, Any], name: str) -> dict[str, Any]:
        entry = self._flow_entry(state, name)
        if entry is None:
            raise NotFoundError(f"flow not found: {name}", code="flow_not_found")
        return entry

    @staticmethod
    def _verify_integrity(name: str, version: int, record: dict[str, Any]) -> None:
        """已发布/草稿版本的内容都必须与记录的 content_hash 一致。

        状态文件被外部手工改动时，内容会变而 hash 不会变；这里在任何读取
        版本内容的路径上重新计算并比对，避免"绑定了版本号但内容已被偷换"。
        """
        actual = content_hash(record.get("content"))
        recorded = record.get("content_hash")
        if recorded and actual != recorded:
            raise ConflictError(
                f"flow {name} v{version} 的内容与其 content_hash 不一致，"
                "定义仓库可能被外部改动，拒绝使用该版本",
                code="content_integrity_violation",
                detail={"version": version, "expected": recorded, "actual": actual},
            )

    def _require_version(self, state: dict[str, Any], name: str, version: int) -> dict[str, Any]:
        entry = self._require_flow(state, name)
        record = entry["versions"].get(str(version))
        if record is None:
            raise NotFoundError(
                f"flow {name} version {version} not found", code="version_not_found"
            )
        self._verify_integrity(name, version, record)
        return record

    @staticmethod
    def _latest_published(state: dict[str, Any], name: str) -> dict[str, Any] | None:
        """最近一次被发布过的版本（含现已废弃的）。

        用于在流程被废弃后重新发布时，仍以最后一次上线的内容作为兼容性基线。
        """
        entry = state["flows"].get(name) or {}
        candidates = [
            record
            for record in entry.get("versions", {}).values()
            if record.get("published_at")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.get("published_at") or "")

    @staticmethod
    def _version_summary(version: int, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": version,
            "status": record["status"],
            "content_hash": record["content_hash"],
            "revision": record.get("revision"),
            "created_at": record.get("created_at"),
            "created_by": record.get("created_by"),
            "published_at": record.get("published_at"),
            "deprecated_at": record.get("deprecated_at"),
            "release_notes": record.get("release_notes"),
            "review": {
                "ok": record.get("review", {}).get("ok"),
                "error_count": record.get("review", {}).get("error_count", 0),
                "warning_count": record.get("review", {}).get("warning_count", 0),
                "ack_required": record.get("review", {}).get("ack_required", []),
            },
        }

    def _new_draft(
        self,
        state: dict[str, Any],
        name: str,
        raw: Any,
        *,
        base_version: int | None,
        actor: str,
        release_notes: str | None,
        revision: int = 1,
    ) -> dict[str, Any]:
        entry = state["flows"].setdefault(
            name,
            {"head_version": 0, "published_version": None, "draft_version": None, "versions": {}},
        )
        version = int(entry["head_version"]) + 1
        canonical = canonical_definition(raw)
        report = review_definition(canonical, state["datasets"])
        record = {
            "version": version,
            "status": VersionStatus.DRAFT.value,
            "content": canonical,
            "content_hash": content_hash(canonical),
            "created_at": _now(),
            "created_by": actor,
            "revision": revision,
            "base_version": base_version,
            "release_notes": release_notes or "",
            "review": report.to_dict(),
            "acknowledged": [],
            "published_at": None,
            "deprecated_at": None,
            "deprecated_reason": None,
            "rolled_back_from": None,
        }
        entry["versions"][str(version)] = record
        entry["head_version"] = version
        entry["draft_version"] = version
        self._audit(
            state,
            "draft_created",
            actor=actor,
            flow=name,
            version=version,
            detail={
                "base_version": base_version,
                "content_hash": record["content_hash"],
                "review": report.to_dict(),
                "release_notes": release_notes or "",
            },
        )
        return record

    # -- 读接口 ------------------------------------------------------------ #

    def list_flows(self) -> list[dict[str, Any]]:
        state = self._load()
        out = []
        for name in sorted(state["flows"]):
            out.append(self._flow_summary(state, name))
        return out

    def _flow_summary(self, state: dict[str, Any], name: str) -> dict[str, Any]:
        entry = state["flows"][name]
        published = self._current_version(state, name, entry.get("published_version"))
        draft = self._current_version(state, name, entry.get("draft_version"))
        if published:
            self._verify_integrity(name, int(published["version"]), published)
        if draft:
            self._verify_integrity(name, int(draft["version"]), draft)
        if published:
            status = VersionStatus.PUBLISHED.value
            reference = published
        elif draft:
            status = "unpublished"
            reference = draft
        else:
            reference = max(entry["versions"].values(), key=lambda r: r["version"])
            self._verify_integrity(name, int(reference["version"]), reference)
            status = VersionStatus.DEPRECATED.value
        content = reference.get("content") or {}
        flow = None
        try:
            flow = Flow.from_dict(content)
        except Exception:  # noqa: BLE001
            pass
        summary: dict[str, Any] = {
            "name": name,
            "status": status,
            "published_version": entry.get("published_version"),
            "draft_version": entry.get("draft_version"),
            "head_version": entry.get("head_version"),
            "content_hash": reference.get("content_hash"),
            "description": (flow.description if flow else content.get("description", "")),
            "dataset": (flow.dataset if flow else content.get("dataset")),
            "steps": len(flow.steps) if flow else len(content.get("steps") or []),
            "has_idempotency_key": (
                any(s.idempotency_key for s in flow.steps)
                if flow
                else any(
                    isinstance(s, dict) and s.get("idempotency_key")
                    for s in content.get("steps") or []
                )
            ),
            "problems": [f["message"] for f in reference.get("review", {}).get("findings", [])],
            "review": reference.get("review", {}),
        }
        # 是否可运行必须同时满足：有发布版本，且其数据集未被删除。
        dataset_name = (flow.dataset if flow else content.get("dataset")) or name
        dataset_state = self._dataset_state(state, dataset_name)
        runnable = status == VersionStatus.PUBLISHED.value and not dataset_state.get("deleted")
        summary["runnable"] = runnable
        summary["dataset_state"] = dataset_state
        if status != VersionStatus.PUBLISHED.value:
            summary["problems"] = summary["problems"] + ["没有已发布版本，不能执行"]
        elif dataset_state.get("deleted"):
            summary["problems"] = summary["problems"] + [
                f"数据集 {dataset_name!r} 已被删除，不能执行"
            ]
        return summary

    def get_flow(self, name: str) -> dict[str, Any]:
        state = self._load()
        entry = self._require_flow(state, name)
        summary = self._flow_summary(state, name)
        summary["versions"] = [
            self._version_summary(int(v), record)
            for v, record in sorted(entry["versions"].items(), key=lambda kv: int(kv[0]))
        ]
        published = self._current_version(state, name, entry.get("published_version"))
        summary["published"] = (
            self._version_detail(state, name, entry["published_version"]) if published else None
        )
        draft = self._current_version(state, name, entry.get("draft_version"))
        summary["draft"] = (
            self._version_detail(state, name, entry["draft_version"]) if draft else None
        )
        return summary

    def _dataset_state(self, state: dict[str, Any], dataset: str) -> dict[str, Any]:
        spec = state.get("datasets", {}).get(dataset)
        if spec is None:
            return {"name": dataset, "registered": False, "deleted": False}
        return {
            "name": dataset,
            "registered": True,
            "deleted": bool(spec.get("deleted")),
            "risk_level": spec.get("risk_level"),
        }

    def _version_detail(self, state: dict[str, Any], name: str, version: int) -> dict[str, Any]:
        record = self._require_version(state, name, version)
        detail = self._version_summary(version, record)
        detail["content"] = record["content"]
        detail["review"] = record.get("review", {})
        detail["acknowledged"] = record.get("acknowledged", [])
        detail["base_version"] = record.get("base_version")
        detail["rolled_back_from"] = record.get("rolled_back_from")
        return detail

    def get_version(self, name: str, version: int) -> dict[str, Any]:
        state = self._load()
        return self._version_detail(state, name, version)

    def list_versions(self, name: str) -> list[dict[str, Any]]:
        state = self._load()
        entry = self._require_flow(state, name)
        return [
            self._version_summary(int(v), record)
            for v, record in sorted(entry["versions"].items(), key=lambda kv: int(kv[0]))
        ]

    # -- 草稿 -------------------------------------------------------------- #

    def create_draft(
        self,
        name: str,
        raw: Any,
        *,
        base_version: int | None = None,
        actor: str = "unknown",
        release_notes: str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name or ""):
            raise ValidationError(f"invalid flow name: {name!r}", code="invalid_name")
        if isinstance(raw, dict) and raw.get("name") not in (None, name):
            raise ValidationError(
                f"定义中的 name={raw.get('name')!r} 与路径 {name!r} 不一致", code="name_mismatch"
            )

        def _create(state: dict[str, Any]) -> dict[str, Any]:
            entry = state["flows"].get(name)
            if entry is None:
                if base_version not in (None, 0):
                    raise ConflictError(
                        f"flow {name} 不存在，不能基于 base_version={base_version} 创建",
                        code="base_version_conflict",
                    )
            else:
                if entry.get("draft_version") is not None:
                    raise ConflictError(
                        f"已存在草稿 v{entry['draft_version']}，请先发布或丢弃",
                        code="draft_in_progress",
                        detail={"draft_version": entry["draft_version"]},
                    )
                published = entry.get("published_version")
                # 有发布版本时必须声明基于哪一版，防止覆盖别人的并发编辑；
                # 流程已废弃（没有发布版本）时没有可比对的基线，允许不传。
                if published is not None:
                    if base_version is None:
                        raise ConflictError(
                            "已发布的流程创建草稿时必须提供 base_version（当前发布版本）",
                            code="base_version_required",
                            detail={"published_version": published},
                        )
                    if base_version != published:
                        raise ConflictError(
                            f"base_version={base_version} 与当前发布版本 "
                            f"{published} 不一致",
                            code="base_version_conflict",
                            detail={"published_version": published},
                        )
                elif base_version not in (None, 0):
                    raise ConflictError(
                        f"流程 {name} 当前没有发布版本，base_version 只能是 null",
                        code="base_version_conflict",
                    )
            return self._new_draft(
                state,
                name,
                raw,
                base_version=base_version,
                actor=actor,
                release_notes=release_notes,
            )

        return self._mutate(_create)

    def update_draft(
        self,
        name: str,
        raw: Any,
        *,
        expected_revision: int | None = None,
        actor: str = "unknown",
        release_notes: str | None = None,
    ) -> dict[str, Any]:
        def _update(state: dict[str, Any]) -> dict[str, Any]:
            entry = self._require_flow(state, name)
            version = entry.get("draft_version")
            if version is None:
                raise ConflictError(f"flow {name} 没有可编辑的草稿", code="no_draft")
            record = entry["versions"][str(version)]
            if expected_revision is not None and expected_revision != record.get("revision"):
                raise ConflictError(
                    f"草稿 v{version} 的 revision={record.get('revision')} 与请求的 "
                    f"{expected_revision} 不一致（并发编辑已被拒绝）",
                    code="revision_conflict",
                    detail={"current_revision": record.get("revision"), "version": version},
                )
            if isinstance(raw, dict) and raw.get("name") not in (None, name):
                raise ValidationError(
                    f"定义中的 name={raw.get('name')!r} 与路径 {name!r} 不一致", code="name_mismatch"
                )
            record["content"] = canonical_definition(raw)
            record["content_hash"] = content_hash(record["content"])
            record["revision"] = int(record.get("revision") or 0) + 1
            record["review"] = review_definition(record["content"], state["datasets"]).to_dict()
            record["acknowledged"] = []
            if release_notes is not None:
                record["release_notes"] = release_notes
            self._audit(
                state,
                "draft_updated",
                actor=actor,
                flow=name,
                version=version,
                detail={
                    "revision": record["revision"],
                    "content_hash": record["content_hash"],
                    "review": record["review"],
                },
            )
            return record

        return self._mutate(_update)

    def discard_draft(
        self, name: str, *, actor: str = "unknown", reason: str = ""
    ) -> dict[str, Any]:
        def _discard(state: dict[str, Any]) -> dict[str, Any]:
            entry = self._require_flow(state, name)
            version = entry.get("draft_version")
            if version is None:
                raise ConflictError(f"flow {name} 没有草稿", code="no_draft")
            del entry["versions"][str(version)]
            entry["draft_version"] = None
            if not entry["versions"]:
                # 从未发布过的流程丢弃唯一草稿后，不再留下空洞的流程条目。
                del state["flows"][name]
            self._audit(
                state,
                "draft_discarded",
                actor=actor,
                flow=name,
                version=version,
                detail={"reason": reason},
            )
            return {"flow": name, "discarded_version": version}

        return self._mutate(_discard)

    # -- 发布 / 废弃 / 回退 ------------------------------------------------ #

    def publish(
        self,
        name: str,
        *,
        version: int | None = None,
        base_version: int | None = None,
        actor: str = "unknown",
        acknowledge: Iterable[str] | None = None,
        allow_breaking: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        acks = set(acknowledge or [])

        def _publish(state: dict[str, Any]) -> dict[str, Any]:
            entry = self._require_flow(state, name)
            target = version if version is not None else entry.get("draft_version")
            if target is None:
                raise ConflictError(f"flow {name} 没有待发布草稿", code="no_draft")
            record = self._require_version(state, name, target)
            if record["status"] != VersionStatus.DRAFT.value:
                raise ConflictError(
                    f"版本 v{target} 的状态是 {record['status']}，不是 draft",
                    code="version_not_draft",
                )
            if entry.get("draft_version") != target:
                raise ConflictError(
                    f"v{target} 不是当前草稿（当前为 v{entry.get('draft_version')}）",
                    code="version_conflict",
                )
            if base_version is not None and base_version != entry.get("published_version"):
                raise ConflictError(
                    f"base_version={base_version} 与当前发布版本 "
                    f"{entry.get('published_version')} 不一致",
                    code="base_version_conflict",
                )

            report = review_definition(record["content"], state["datasets"])
            if report.errors:
                raise ValidationError(
                    "发布前静态审查未通过",
                    code="review_failed",
                    detail={"review": report.to_dict()},
                )
            missing_ack = sorted({f.code for f in report.ack_required} - acks)
            if missing_ack:
                raise ValidationError(
                    "存在需要显式确认的高风险动作: " + ", ".join(missing_ack),
                    code="acknowledgement_required",
                    detail={"ack_required": missing_ack, "review": report.to_dict()},
                )

            compatibility: dict[str, Any] = {"comparable": True, "breaking": [], "warnings": []}
            previous = self._current_version(state, name, entry.get("published_version"))
            # 流程曾发布后又被废弃时，用"最近一个发布过的版本"作为兼容性基线，
            # 否则废弃 -> 重新发布可以绕过不兼容变更检查。
            baseline = previous or self._latest_published(state, name)
            if baseline is not None and baseline["version"] != target:
                compatibility = diff_definitions(baseline["content"], record["content"])
                if compatibility.get("breaking") and not allow_breaking:
                    raise ValidationError(
                        "检测到向后不兼容变更，发布被拒绝",
                        code="breaking_change",
                        detail={
                            "breaking": compatibility["breaking"],
                            "warnings": compatibility["warnings"],
                            "baseline_version": baseline["version"],
                            "allow_breaking_hint": "确认影响后以 allow_breaking=true 并填写 reason 重新发布",
                        },
                    )
                if compatibility.get("breaking") and allow_breaking and not reason.strip():
                    raise ValidationError(
                        "允许不兼容变更时必须提供 reason", code="reason_required"
                    )

            now = _now()
            if previous is not None:
                previous["status"] = VersionStatus.DEPRECATED.value
                previous["deprecated_at"] = now
                previous["deprecated_reason"] = f"superseded by v{target}"
                self._audit(
                    state,
                    "deprecated",
                    actor=actor,
                    flow=name,
                    version=previous["version"],
                    detail={"reason": f"superseded by v{target}"},
                )
            record["status"] = VersionStatus.PUBLISHED.value
            record["published_at"] = now
            record["deprecated_at"] = None
            record["deprecated_reason"] = None
            record["acknowledged"] = sorted(acks)
            record["review"] = report.to_dict()
            entry["published_version"] = target
            entry["draft_version"] = None
            self._audit(
                state,
                "published",
                actor=actor,
                flow=name,
                version=target,
                detail={
                    "content_hash": record["content_hash"],
                    "acknowledged": sorted(acks),
                    "allow_breaking": allow_breaking,
                    "reason": reason,
                    "compatibility": compatibility,
                    "compatibility_baseline": baseline["version"] if baseline else None,
                    "review": report.to_dict(),
                    "previous_version": previous["version"] if previous else None,
                },
            )
            return self._version_detail(state, name, target)

        return self._mutate(_publish)

    def deprecate(
        self,
        name: str,
        *,
        version: int | None = None,
        actor: str = "unknown",
        reason: str = "",
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("废弃流程必须填写 reason", code="reason_required")

        def _deprecate(state: dict[str, Any]) -> dict[str, Any]:
            entry = self._require_flow(state, name)
            target = version if version is not None else entry.get("published_version")
            if target is None:
                raise ConflictError(f"flow {name} 没有已发布版本", code="flow_not_published")
            record = self._require_version(state, name, target)
            if record["status"] != VersionStatus.PUBLISHED.value:
                raise ConflictError(
                    f"版本 v{target} 的状态是 {record['status']}，不能废弃",
                    code="version_not_published",
                )
            record["status"] = VersionStatus.DEPRECATED.value
            record["deprecated_at"] = _now()
            record["deprecated_reason"] = reason
            if entry.get("published_version") == target:
                entry["published_version"] = None
            self._audit(
                state,
                "deprecated",
                actor=actor,
                flow=name,
                version=target,
                detail={"reason": reason, "content_hash": record["content_hash"]},
            )
            detail = self._version_detail(state, name, target)
            detail["published_version"] = entry.get("published_version")
            return detail

        return self._mutate(_deprecate)

    def rollback(
        self,
        name: str,
        to_version: int,
        *,
        actor: str = "unknown",
        reason: str = "",
        base_version: int | None = None,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("回退必须填写 reason", code="reason_required")

        def _rollback(state: dict[str, Any]) -> dict[str, Any]:
            entry = self._require_flow(state, name)
            if entry.get("draft_version") is not None:
                raise ConflictError(
                    f"存在未处理草稿 v{entry['draft_version']}，请先发布或丢弃",
                    code="draft_in_progress",
                )
            if base_version is not None and base_version != entry.get("published_version"):
                raise ConflictError(
                    f"base_version={base_version} 与当前发布版本 "
                    f"{entry.get('published_version')} 不一致",
                    code="base_version_conflict",
                )
            target = self._require_version(state, name, to_version)
            if target["status"] == VersionStatus.DRAFT.value:
                raise ConflictError(
                    f"v{to_version} 从未发布过，不能作为回退目标", code="version_never_published"
                )
            raw = copy.deepcopy(target["content"])
            record = self._new_draft(
                state,
                name,
                raw,
                base_version=entry.get("published_version"),
                actor=actor,
                release_notes=f"回退到 v{to_version}: {reason}",
            )
            record["rolled_back_from"] = to_version
            # 回退的兼容性结论记录在审计里，但内容本身就是历史发布过的版本，
            # 因此允许 breaking，前提是调用方已经给出 reason。
            previous = self._current_version(state, name, entry.get("published_version"))
            compatibility = (
                diff_definitions(previous["content"], raw) if previous is not None else {}
            )
            report = review_definition(record["content"], state["datasets"])
            if report.errors:
                # 目标版本当时能发布，但数据集可能已经被删除。此时回退同样不安全。
                # `_mutate` 只在 fn 正常返回时写盘，所以这里直接抛错即可，
                # 不会把临时生成的版本留下。
                raise ValidationError(
                    "回退目标当前无法通过静态审查（可能引用了已删除的数据集）",
                    code="rollback_blocked",
                    detail={"review": report.to_dict()},
                )
            now = _now()
            if previous is not None:
                previous["status"] = VersionStatus.DEPRECATED.value
                previous["deprecated_at"] = now
                previous["deprecated_reason"] = f"rolled back to v{to_version}"
                self._audit(
                    state,
                    "deprecated",
                    actor=actor,
                    flow=name,
                    version=previous["version"],
                    detail={"reason": f"rolled back to v{to_version}"},
                )
            record["status"] = VersionStatus.PUBLISHED.value
            record["published_at"] = now
            record["review"] = report.to_dict()
            entry["published_version"] = record["version"]
            entry["draft_version"] = None
            self._audit(
                state,
                "rolled_back",
                actor=actor,
                flow=name,
                version=record["version"],
                detail={
                    "to_version": to_version,
                    "from_version": previous["version"] if previous else None,
                    "content_hash": record["content_hash"],
                    "reason": reason,
                    "compatibility": compatibility,
                },
            )
            return self._version_detail(state, name, record["version"])

        return self._mutate(_rollback)

    def compatibility(self, name: str, *, from_version: int, to_version: int) -> dict[str, Any]:
        state = self._load()
        old = self._require_version(state, name, from_version)
        new = self._require_version(state, name, to_version)
        return diff_definitions(old["content"], new["content"])

    def review_version(self, name: str, version: int) -> dict[str, Any]:
        state = self._load()
        record = self._require_version(state, name, version)
        return review_definition(record["content"], state["datasets"]).to_dict()

    def review_payload(self, raw: Any) -> dict[str, Any]:
        state = self._load()
        return review_definition(raw, state["datasets"]).to_dict()

    # -- 数据集 ------------------------------------------------------------ #

    def list_datasets(self) -> list[dict[str, Any]]:
        state = self._load()
        return [
            {"name": name, **copy.deepcopy(spec)}
            for name, spec in sorted(state.get("datasets", {}).items())
        ]

    def upsert_dataset(
        self, name: str, spec: dict[str, Any], *, actor: str = "unknown"
    ) -> dict[str, Any]:
        def _upsert(state: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(spec, dict):
                raise ValidationError("dataset 定义必须是对象", code="invalid_dataset")
            record = {
                "description": spec.get("description", ""),
                "risk_level": spec.get("risk_level", "normal"),
                "fields": spec.get("fields", []),
                "sensitive_fields": spec.get("sensitive_fields", []),
                "deleted": bool(spec.get("deleted", False)),
            }
            state["datasets"][name] = record
            self._audit(
                state,
                "dataset_upserted",
                actor=actor,
                flow=None,
                detail={"dataset": name, "spec": record},
            )
            return {"name": name, **record}

        return self._mutate(_upsert)

    def delete_dataset(
        self, name: str, *, actor: str = "unknown", reason: str = ""
    ) -> dict[str, Any]:
        def _delete(state: dict[str, Any]) -> dict[str, Any]:
            spec = state["datasets"].get(name)
            if spec is None:
                raise NotFoundError(f"dataset not found: {name}", code="dataset_not_found")
            spec["deleted"] = True
            spec["deleted_at"] = _now()
            spec["deleted_by"] = actor
            spec["delete_reason"] = reason
            affected = [
                flow_name
                for flow_name, entry in state["flows"].items()
                if self._current_version(state, flow_name, entry.get("published_version"))
                and (
                    entry["versions"][str(entry["published_version"])]["content"].get("dataset")
                    or flow_name
                )
                == name
            ]
            self._audit(
                state,
                "dataset_deleted",
                actor=actor,
                detail={"dataset": name, "reason": reason, "affected_flows": affected},
            )
            return {
                "name": name,
                **copy.deepcopy(spec),
                "affected_published_flows": affected,
            }

        return self._mutate(_delete)

    # -- 运行解析 ---------------------------------------------------------- #

    def resolve_for_run(
        self,
        name: str,
        *,
        version: int | None = None,
        allow_deprecated: bool = False,
    ) -> VersionBinding:
        """确认一次运行要使用的版本。不修改状态，但会拒绝不可运行的版本。

        `version=None` 表示"用当前发布版"，这种情况下没有发布版就直接拒绝；
        显式钉版本时允许回放已废弃版本（用于重试/恢复/审计复现），
        但仍拒绝草稿。`allow_deprecated` 为 False 时，即使是显式钉版本也会拒绝废弃版本，
        避免新业务误用旧定义。
        """
        state = self._load()
        entry = self._require_flow(state, name)
        if version is None:
            target = entry.get("published_version")
            if target is None:
                raise ConflictError(
                    f"flow {name} 没有已发布版本，不能执行（草稿需要先 publish）",
                    code="flow_not_published",
                    detail={"draft_version": entry.get("draft_version")},
                )
        else:
            target = version
        record = self._require_version(state, name, target)
        if record["status"] == VersionStatus.DRAFT.value:
            raise ConflictError(
                f"版本 v{target} 是草稿，不能执行", code="version_not_runnable"
            )
        if record["status"] == VersionStatus.DEPRECATED.value and not allow_deprecated:
            raise ConflictError(
                f"版本 v{target} 已废弃，不能用于新的运行；恢复既有运行请走 /runs/{{run_id}}/resume",
                code="version_deprecated",
                detail={"deprecated_reason": record.get("deprecated_reason")},
            )
        content = record["content"]
        dataset = content.get("dataset") or name
        spec = state.get("datasets", {}).get(dataset)
        if spec is None:
            raise ConflictError(
                f"dataset {dataset!r} 未登记，拒绝执行", code="dataset_unknown"
            )
        if spec.get("deleted"):
            raise ConflictError(
                f"dataset {dataset!r} 已被删除，拒绝执行",
                code="dataset_deleted",
                detail={"dataset": dataset},
            )
        flow = Flow.from_dict(content)
        return VersionBinding(
            flow_name=name,
            version=target,
            status=VersionStatus(record["status"]),
            content_hash=record["content_hash"],
            flow=flow,
        )

    def record_run_event(
        self,
        action: str,
        *,
        actor: str,
        run_id: str,
        flow: str,
        version: int | None,
        detail: Any = None,
    ) -> None:
        def _record(state: dict[str, Any]) -> None:
            self._audit(
                state,
                action,
                actor=actor,
                flow=flow,
                version=version,
                run_id=run_id,
                detail=detail,
            )

        self._mutate(_record)

    # -- 测试 / 演示辅助 --------------------------------------------------- #

    def reset(self, *, actor: str = "system:reset") -> dict[str, Any]:
        def _reset(state: dict[str, Any]) -> dict[str, Any]:
            state.clear()
            state.update(self._empty_state())
            self._audit(state, "registry_reset", actor=actor)
            self._seed_from_files(state)
            return {"flows": sorted(state["flows"]), "reset_by": actor}

        return self._mutate(_reset)


_registry: DefinitionRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> DefinitionRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = DefinitionRegistry()
    return _registry


def reset_registry_instance() -> None:
    """仅供测试替换路径后重新创建单例。"""
    global _registry
    with _registry_lock:
        _registry = None

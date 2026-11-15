"""执行证据采集。

目标：维护人员事后能回答三个问题——
  1. 每个字段最终采用了哪条定位规则（strategy + css）；
  2. 过程中出现过哪些候选冲突（attempts / failure）；
  3. 当时的页面版本特征是什么（fingerprint / layout_kind）。

同时必须保证：证据里绝不出现业务表单中的敏感取值（金额、申请人、
供应商、单号等）。取值一律以 sha256 摘要 + 长度表示；断言文本、
错误消息在入库前统一脱敏。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .pageview import PageView
from .resolver import Resolution


def digest_value(value: str) -> str:
    """取值的不可逆摘要（前 12 位十六进制），用于对账而非阅读。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def redact(text: str, secrets: list[str]) -> str:
    """把文本中出现的已知敏感取值替换为摘要形式。

    secrets 来自本轮所有 fill/select 的实际取值（模板渲染后）。
    替换基于精确子串匹配；空串与过短串（<2 字符）不参与，
    避免误伤通用文本。
    """
    out = text or ""
    for s in secrets:
        if s and len(s) >= 2 and s in out:
            out = out.replace(s, f"<redacted:{digest_value(s)}>")
    return out


@dataclass
class StepEvidence:
    index: int
    kind: str
    field: str | None = None
    strategy: str | None = None
    css: str | None = None
    resolution: dict[str, Any] | None = None
    value_digest: str | None = None
    value_length: int | None = None
    ok: bool = True
    skipped_optional: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "field": self.field,
            "strategy": self.strategy,
            "css": self.css,
            "resolution": self.resolution,
            "value_digest": self.value_digest,
            "value_length": self.value_length,
            "ok": self.ok,
            "skipped_optional": self.skipped_optional,
            "error": self.error,
        }


@dataclass
class EvidenceCollector:
    """一轮执行的证据聚合器。"""

    secrets: list[str] = field(default_factory=list)
    page: dict[str, Any] | None = None
    steps: list[StepEvidence] = field(default_factory=list)

    def register_secret(self, value: str) -> None:
        if value and value not in self.secrets:
            self.secrets.append(value)

    def record_page(self, view: PageView, url: str) -> None:
        """页面版本特征：指纹 + 结构摘要。只含结构信息。"""
        self.page = {
            "url": url,
            "fingerprint": view.fingerprint,
            "layout_kind": view.layout_kind,
            "field_names": view.field_names(),
            "controls": [c.signature for c in view.controls],
            "labels": [lb.text for lb in view.labels],
            "buttons": [b.signature for b in view.buttons],
        }

    def record_step(
        self,
        index: int,
        kind: str,
        *,
        field: str | None = None,
        resolution: Resolution | None = None,
        value: str | None = None,
        ok: bool = True,
        skipped_optional: bool = False,
        error: str | None = None,
    ) -> None:
        self.steps.append(
            StepEvidence(
                index=index,
                kind=kind,
                field=field,
                strategy=resolution.strategy if resolution else None,
                css=resolution.css if resolution else None,
                resolution=resolution.to_evidence() if resolution else None,
                value_digest=digest_value(value) if value is not None else None,
                value_length=len(value) if value is not None else None,
                ok=ok,
                skipped_optional=skipped_optional,
                error=redact(error, self.secrets) if error else None,
            )
        )

    def build(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "steps": [s.to_dict() for s in self.steps],
        }


def sanitize_run_error(message: str, secrets: list[str]) -> str:
    """写入 Run.error 前的最终脱敏。"""
    # 折叠超长堆栈，只保留单行可读消息
    first_line = (message or "").splitlines()[0] if message else ""
    first_line = re.sub(r"\s+", " ", first_line).strip()
    return redact(first_line[:500], secrets)

"""证据与持久化边界的单元测试（不需要数据库与 mockapp）。

锁定两条契约：
  * 证据在任何失败形态下都不含业务取值（金额、申请人、单号）；
  * runs 表序列化把时间戳转为 ISO 字符串，evidence 可 JSON 化。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.evidence import EvidenceCollector, digest_value, redact, sanitize_run_error
from app.models import Run, RunStatus, utcnow
from app.store import _serialize_run


def test_digest_is_irreversible_and_stable():
    d = digest_value("张伟")
    assert d == digest_value("张伟")
    assert "张伟" not in d
    assert len(d) == 12


def test_redact_replaces_known_secrets_only():
    text = "step 2: fill applicant=张伟 failed near 张伟的输入框"
    out = redact(text, ["张伟"])
    assert "张伟" not in out
    assert out.count("<redacted:") == 2
    # 过短的串不参与替换，避免误伤
    assert redact("a b c", ["b"]) == "a b c"


def test_sanitize_run_error_collapses_stack_and_redacts():
    msg = "step 3 (fill): 金额 1289000 写入失败\nTraceback...\n  File x.py"
    out = sanitize_run_error(msg, ["1289000"])
    assert "\n" not in out
    assert "1289000" not in out
    assert "<redacted:" in out


def test_evidence_collector_never_stores_plain_values():
    ev = EvidenceCollector()
    ev.register_secret("SUP-00042")
    ev.record_step(1, "fill", field="supplier_code", value="SUP-00042")
    ev.record_step(2, "fill", field="amount_cents", value="1289000", ok=False,
                   error="write SUP-00042 rejected by target")
    blob = json.dumps(ev.build(), ensure_ascii=False)
    assert "SUP-00042" not in blob
    assert "1289000" not in blob
    steps = ev.build()["steps"]
    assert steps[0]["value_digest"] == digest_value("SUP-00042")
    assert steps[0]["value_length"] == len("SUP-00042")
    assert "<redacted:" in steps[1]["error"]


def test_serialize_run_converts_datetimes():
    row = {
        "run_id": "r1",
        "flow": "f",
        "status": "succeeded",
        "started_at": datetime(2026, 11, 15, 8, 0, tzinfo=UTC),
        "finished_at": None,
        "error": None,
        "attempts": 1,
        "failure_kind": None,
        "evidence": {"page": {"fingerprint": "abc"}},
    }
    out = _serialize_run(row)
    assert out["started_at"].startswith("2026-11-15T08:00")
    assert out["finished_at"] is None
    # evidence 必须可 JSON 序列化（JSONB 列）
    json.dumps(out["evidence"])


def test_run_to_dict_carries_failure_semantics_and_evidence():
    run = Run(
        run_id="r2",
        flow="reimbursement_batch",
        status=RunStatus.FAILED,
        started_at=utcnow(),
        failure_kind="structure_changed",
        evidence={"page": {"fingerprint": "x"}, "steps": []},
    )
    d = run.to_dict()
    assert d["failure_kind"] == "structure_changed"
    assert d["evidence"]["page"]["fingerprint"] == "x"
    json.dumps(d)  # 整体必须可 JSON 化

"""流程定义治理生命周期测试。

不依赖 PostgreSQL：只测定义注册表与 API 的治理端点。
需要 mockapp 在 MOCKAPP_URL 上运行才能测执行绑定，`RUN_TESTS` 标记。

    MOCKAPP_URL=http://localhost:9200 pytest -q tests/test_governance.py
"""

from __future__ import annotations

import copy
import json
import os
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app.governance import (
    DefinitionRegistry,
    GovernanceError,
    diff_definitions,
    review_definition,
)

MOCK = os.environ.get("MOCKAPP_URL", "http://localhost:9200")

BASE_DEFINITION = {
    "name": "demo_flow",
    "description": "治理测试用流程",
    "dataset": "reimbursement",
    "steps": [
        {"kind": "open", "value": "/form/reimbursement"},
        {"kind": "fill", "selector": "input#source_key", "value": "EXP-DEMO-1"},
        {"kind": "fill", "selector": "input#applicant", "value": "测试员"},
        {"kind": "fill", "selector": "input#expense_type", "value": "差旅费"},
        {"kind": "fill", "selector": "input#amount_cents", "value": "100"},
        {"kind": "fill", "selector": "input#invoice_no", "value": "INV-DEMO-1"},
        {"kind": "click", "selector": "button#submit-btn"},
        {"kind": "submit", "value": ""},
    ],
}

DATASETS = {
    "reimbursement": {
        "description": "费用报销单",
        "risk_level": "high",
        "fields": [
            {"name": "applicant", "required": True},
            {"name": "expense_type", "required": True},
            {"name": "amount_cents", "required": True},
            {"name": "invoice_no", "required": True},
        ],
        "sensitive_fields": ["amount_cents", "invoice_no"],
    }
}


@pytest.fixture()
def registry(tmp_path):
    """每个用例独立的注册表；flows_dir 指向空目录，避免被仓库 flows 引导污染。"""
    empty = tmp_path / "flows"
    empty.mkdir()
    return DefinitionRegistry(path=str(tmp_path / "defs.json"), flows_dir=str(empty))


def _draft(registry: DefinitionRegistry, name: str = "demo_flow", **overrides) -> dict:
    payload = copy.deepcopy(BASE_DEFINITION)
    payload["name"] = name
    payload.update(overrides)
    return registry.create_draft(name, payload, actor="tester", release_notes="init")


# --------------------------------------------------------------------------- #
# 静态审查
# --------------------------------------------------------------------------- #


def test_review_accepts_valid_definition():
    report = review_definition(BASE_DEFINITION, DATASETS)
    assert report.errors == []
    codes = {f.code for f in report.warnings}
    assert "submit_without_idempotency" in codes
    assert "high_risk_dataset" in codes


def test_review_flags_structure_and_order():
    bad = copy.deepcopy(BASE_DEFINITION)
    bad["steps"] = [
        {"kind": "fill", "selector": "input#source_key", "value": "X"},
        {"kind": "assert_text", "selector": "p#receipt-id", "value": ""},
    ]
    report = review_definition(bad, DATASETS)
    codes = {f.code for f in report.errors}
    assert "first_step_not_open" in codes
    assert "missing_open" in codes
    assert "missing_submit" in codes
    assert "assert_before_submit" in codes  # 提交前断言


def test_review_flags_unknown_dataset_and_deleted_dataset():
    report = review_definition({**BASE_DEFINITION, "dataset": "ghost"}, DATASETS)
    assert any(f.code == "dataset_unknown" and f.severity.value == "error" for f in report.findings)

    deleted = {**DATASETS, "reimbursement": {**DATASETS["reimbursement"], "deleted": True}}
    report = review_definition(BASE_DEFINITION, deleted)
    assert any(f.code == "dataset_deleted" for f in report.errors)


def test_review_flags_sensitive_literal_and_high_risk_action():
    risky = copy.deepcopy(BASE_DEFINITION)
    risky["steps"].append({"kind": "click", "selector": "button#delete-account"})
    report = review_definition(risky, DATASETS)
    assert any(
        f.code == "destructive_action" and f.requires_acknowledgement for f in report.findings
    )
    assert any(f.code == "sensitive_literal" for f in report.findings)


def test_review_reports_unknown_placeholder_and_missing_required():
    bad = copy.deepcopy(BASE_DEFINITION)
    bad["steps"][1]["value"] = "EXP-{batch_no}"
    bad["steps"] = [s for s in bad["steps"] if s.get("selector") != "input#invoice_no"]
    report = review_definition(bad, DATASETS)
    codes = {f.code for f in report.warnings}
    assert "unknown_placeholder" in codes
    assert "dataset_required_fields_missing" in codes


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


def test_publish_requires_draft_and_freezes_content(registry):
    record = _draft(registry)
    assert record["status"] == "draft"

    published = registry.publish("demo_flow", actor="tester")
    assert published["status"] == "published"
    assert published["version"] == 1

    # 草稿申请后版本不可再改
    with pytest.raises(GovernanceError):
        registry.update_draft("demo_flow", BASE_DEFINITION)
    # 已发布版本内容不可变：改内容只会产生新的草稿版本
    draft2 = registry.create_draft(
        "demo_flow", {**BASE_DEFINITION, "description": "v2"}, base_version=1, actor="tester"
    )
    assert draft2["version"] == 2
    assert draft2["base_version"] == 1
    assert registry.get_version("demo_flow", 1)["content"]["description"] == "治理测试用流程"


def test_publish_blocked_by_errors_until_fixed(registry):
    bad = copy.deepcopy(BASE_DEFINITION)
    bad["steps"] = [{"kind": "fill", "selector": "input#source_key", "value": "X"}]
    registry.create_draft("demo_flow", bad, actor="tester")
    with pytest.raises(GovernanceError) as exc:
        registry.publish("demo_flow", actor="tester")
    assert exc.value.code == "review_failed"

    registry.update_draft("demo_flow", BASE_DEFINITION, actor="tester")
    assert registry.publish("demo_flow", actor="tester")["status"] == "published"


def test_high_risk_action_requires_explicit_acknowledgement(registry):
    risky = copy.deepcopy(BASE_DEFINITION)
    risky["steps"].insert(-1, {"kind": "click", "selector": "button#delete-record"})
    registry.create_draft("demo_flow", risky, actor="tester")

    with pytest.raises(GovernanceError) as exc:
        registry.publish("demo_flow", actor="tester")
    assert exc.value.code == "acknowledgement_required"
    assert "destructive_action" in exc.value.detail["ack_required"]

    published = registry.publish(
        "demo_flow", actor="tester", acknowledge=["destructive_action"]
    )
    assert published["acknowledged"] == ["destructive_action"]


def test_breaking_change_requires_allow_and_reason(registry):
    _draft(registry)
    registry.publish("demo_flow", actor="tester")

    breaking = copy.deepcopy(BASE_DEFINITION)
    breaking["steps"][1]["value"] = "EXP-DEMO-CHANGED"  # source_key 变化 -> 不兼容
    registry.create_draft("demo_flow", breaking, base_version=1, actor="tester")

    with pytest.raises(GovernanceError) as exc:
        registry.publish("demo_flow", actor="tester")
    assert exc.value.code == "breaking_change"
    assert any(b["code"] == "source_key_changed" for b in exc.value.detail["breaking"])

    with pytest.raises(GovernanceError) as exc:
        registry.publish("demo_flow", actor="tester", allow_breaking=True)
    assert exc.value.code == "reason_required"

    published = registry.publish(
        "demo_flow", actor="tester", allow_breaking=True, reason="单号规则调整，已通知业务"
    )
    assert published["version"] == 2
    assert registry.get_version("demo_flow", 1)["status"] == "deprecated"


def test_concurrent_edit_same_draft_is_rejected(registry):
    record = _draft(registry)
    first = registry.update_draft(
        "demo_flow",
        {**BASE_DEFINITION, "description": "A"},
        expected_revision=record["revision"],
        actor="alice",
    )
    assert first["revision"] == record["revision"] + 1

    with pytest.raises(GovernanceError) as exc:
        registry.update_draft(
            "demo_flow",
            {**BASE_DEFINITION, "description": "B"},
            expected_revision=record["revision"],  # 旧 revision
            actor="bob",
        )
    assert exc.value.code == "revision_conflict"


def test_only_one_draft_per_flow(registry):
    _draft(registry)
    with pytest.raises(GovernanceError) as exc:
        _draft(registry)
    assert exc.value.code == "draft_in_progress"


def test_create_draft_for_existing_flow_requires_base_version(registry):
    _draft(registry)
    registry.publish("demo_flow", actor="tester")
    with pytest.raises(GovernanceError) as exc:
        registry.create_draft("demo_flow", BASE_DEFINITION, actor="tester")
    assert exc.value.code == "base_version_required"

    with pytest.raises(GovernanceError) as exc:
        registry.create_draft("demo_flow", BASE_DEFINITION, base_version=99, actor="tester")
    assert exc.value.code == "base_version_conflict"


def test_deprecate_blocks_new_runs_but_keeps_history(registry):
    _draft(registry)
    registry.publish("demo_flow", actor="tester")
    detail = registry.deprecate("demo_flow", actor="tester", reason="业务下线")
    assert detail["status"] == "deprecated"
    assert detail["published_version"] is None

    with pytest.raises(GovernanceError) as exc:
        registry.resolve_for_run("demo_flow")
    assert exc.value.code == "flow_not_published"

    # 显式钉版本仍可回放（审计复现），但默认拒绝
    with pytest.raises(GovernanceError):
        registry.resolve_for_run("demo_flow", version=1)
    assert registry.resolve_for_run("demo_flow", version=1, allow_deprecated=True).version == 1


def test_rollback_creates_new_version_copy(registry):
    _draft(registry)
    v1 = registry.publish("demo_flow", actor="tester")
    v2def = copy.deepcopy(BASE_DEFINITION)
    v2def["description"] = "v2 描述"
    registry.create_draft("demo_flow", v2def, base_version=1, actor="tester")
    v2 = registry.publish("demo_flow", actor="tester")
    assert v2["version"] == 2

    with pytest.raises(GovernanceError):  # 回退必须给原因
        registry.rollback("demo_flow", 1, actor="tester")

    rolled = registry.rollback("demo_flow", 1, actor="tester", reason="v2 引发异常")
    assert rolled["version"] == 3
    assert rolled["rolled_back_from"] == 1
    assert rolled["content_hash"] == v1["content_hash"]
    assert registry.get_version("demo_flow", 2)["status"] == "deprecated"
    # 历史版本仍然完整保留
    assert registry.get_version("demo_flow", 2)["content"]["description"] == "v2 描述"


def test_republish_after_deprecate_is_still_compat_checked(registry):
    _draft(registry)
    registry.publish("demo_flow", actor="tester")
    registry.deprecate("demo_flow", actor="tester", reason="下线")

    # 废弃后没有发布版本，允许不带 base_version 创建草稿
    changed = copy.deepcopy(BASE_DEFINITION)
    changed["steps"][1]["value"] = "EXP-AFTER-DEPRECATE"
    registry.create_draft("demo_flow", changed, base_version=None, actor="tester")
    with pytest.raises(GovernanceError) as exc:
        registry.publish("demo_flow", actor="tester")
    assert exc.value.code == "breaking_change"
    assert exc.value.detail["baseline_version"] == 1


def test_deleted_dataset_blocks_publish_and_run_and_rollback(registry):
    _draft(registry)
    registry.publish("demo_flow", actor="tester")
    registry.delete_dataset("reimbursement", actor="admin", reason="系统下线")

    with pytest.raises(GovernanceError) as exc:
        registry.resolve_for_run("demo_flow")
    assert exc.value.code == "dataset_deleted"

    # 新草稿即使内容正确也发布不了
    registry.create_draft(
        "demo_flow", {**BASE_DEFINITION, "description": "x"}, base_version=1, actor="tester"
    )
    with pytest.raises(GovernanceError) as exc:
        registry.publish("demo_flow", actor="tester")
    assert exc.value.code == "review_failed"


def test_run_binding_survives_new_publish(registry):
    _draft(registry)
    registry.publish("demo_flow", actor="tester")
    binding_v1 = registry.resolve_for_run("demo_flow")

    changed = copy.deepcopy(BASE_DEFINITION)
    changed["steps"][2]["value"] = "另一个人"
    registry.create_draft("demo_flow", changed, base_version=1, actor="tester")
    registry.publish("demo_flow", actor="tester")

    # 先前拿到手的绑定不会因为新发布而改变
    assert binding_v1.version == 1
    assert binding_v1.flow.steps[2].value == "测试员"
    assert registry.resolve_for_run("demo_flow").version == 2


def test_draft_is_not_runnable(registry):
    _draft(registry)
    with pytest.raises(GovernanceError) as exc:
        registry.resolve_for_run("demo_flow")
    assert exc.value.code == "flow_not_published"


def test_audit_trail_records_every_transition(registry):
    _draft(registry)
    registry.publish("demo_flow", actor="tester")
    registry.deprecate("demo_flow", actor="tester", reason="下线")
    actions = [e["action"] for e in registry.audit_events(flow="demo_flow")]
    assert "draft_created" in actions
    assert "published" in actions
    assert "deprecated" in actions
    for event in registry.audit_events(flow="demo_flow"):
        assert event["actor"] == "tester"
        assert event["at"]


def test_registry_persists_across_instances(tmp_path):
    empty = tmp_path / "flows"
    empty.mkdir()
    path = str(tmp_path / "defs.json")
    r1 = DefinitionRegistry(path=path, flows_dir=str(empty))
    r1.create_draft("demo_flow", BASE_DEFINITION, actor="tester")
    r1.publish("demo_flow", actor="tester")

    r2 = DefinitionRegistry(path=path, flows_dir=str(empty))
    assert r2.resolve_for_run("demo_flow").version == 1
    assert r2.get_flow("demo_flow")["status"] == "published"


def test_state_file_is_valid_json(registry):
    _draft(registry)
    with open(registry.path, encoding="utf-8") as fh:
        state = json.load(fh)
    assert "flows" in state and "audit" in state


def test_tampered_version_content_is_detected(registry):
    """内容被外部改动但 hash 未变时，读取与运行都必须拒绝。"""
    _draft(registry)
    registry.publish("demo_flow", actor="tester")

    with open(registry.path, encoding="utf-8") as fh:
        state = json.load(fh)
    state["flows"]["demo_flow"]["versions"]["1"]["content"]["description"] = "被篡改"
    with open(registry.path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)

    with pytest.raises(GovernanceError) as exc:
        registry.get_version("demo_flow", 1)
    assert exc.value.code == "content_integrity_violation"
    with pytest.raises(GovernanceError) as exc:
        registry.resolve_for_run("demo_flow")
    assert exc.value.code == "content_integrity_violation"


# --------------------------------------------------------------------------- #
# 并发
# --------------------------------------------------------------------------- #


def test_parallel_drafts_do_not_lose_versions(tmp_path):
    empty = tmp_path / "flows"
    empty.mkdir()
    path = str(tmp_path / "defs.json")
    DefinitionRegistry(path=path, flows_dir=str(empty))
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker(worker_id: int) -> None:
        registry = DefinitionRegistry(path=path, flows_dir=str(empty))
        barrier.wait()
        try:
            registry.create_draft(
                f"flow_{worker_id}",
                {**BASE_DEFINITION, "name": f"flow_{worker_id}"},
                actor=f"w{worker_id}",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    final = DefinitionRegistry(path=path, flows_dir=str(empty))
    assert len(final.list_flows()) == 4


def test_diff_definitions_reports_breaking_change():
    changed = copy.deepcopy(BASE_DEFINITION)
    changed["dataset"] = "purchase_requisition"
    result = diff_definitions(BASE_DEFINITION, changed)
    assert any(b["code"] == "dataset_changed" for b in result["breaking"])


def test_diff_detects_reorder_distinct_from_removal():
    reordered = copy.deepcopy(BASE_DEFINITION)
    # 交换两个 fill 的顺序：同一组步骤，只是顺序不同 -> breaking
    steps = reordered["steps"]
    steps[1], steps[2] = steps[2], steps[1]
    result = diff_definitions(BASE_DEFINITION, reordered)
    assert any(b["code"] == "step_order_changed" for b in result["breaking"])

    removed = copy.deepcopy(BASE_DEFINITION)
    removed["steps"] = [s for s in removed["steps"] if s.get("selector") != "input#invoice_no"]
    result = diff_definitions(BASE_DEFINITION, removed)
    assert not any(b["code"] == "step_order_changed" for b in result["breaking"])


def test_diff_compatible_change_has_no_breaking():
    changed = copy.deepcopy(BASE_DEFINITION)
    changed["description"] = "只是描述变了"
    result = diff_definitions(BASE_DEFINITION, changed)
    assert result["breaking"] == []


def test_discard_only_draft_removes_empty_flow(registry):
    _draft(registry)
    registry.discard_draft("demo_flow", actor="tester", reason="不要了")
    assert registry.list_flows() == []


def test_seed_degrades_high_risk_definition_to_draft(tmp_path):
    """引导发布不能绕过确认：带高风险动作的定义应停在草稿。"""
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "risky.yaml").write_text(
        "name: risky_flow\ndataset: reimbursement\nsteps:\n"
        "  - kind: open\n    value: /form/reimbursement\n"
        "  - kind: fill\n    selector: input#source_key\n    value: R-1\n"
        "  - kind: click\n    selector: button#delete-record\n"
        "  - kind: submit\n    value: ''\n",
        encoding="utf-8",
    )
    reg = DefinitionRegistry(path=str(tmp_path / "defs.json"), flows_dir=str(flows_dir))
    flow = reg.get_flow("risky_flow")
    assert flow["status"] == "unpublished"
    assert flow["draft_version"] == 1
    assert flow["runnable"] is False
    with pytest.raises(GovernanceError):
        reg.resolve_for_run("risky_flow")


def test_sync_is_idempotent_after_deprecate(tmp_path):
    """已废弃流程：文件没变时重复同步不应每次生成新草稿。"""
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "demo_flow.yaml").write_text(
        "name: demo_flow\ndataset: reimbursement\nsteps:\n"
        "  - kind: open\n    value: /form/reimbursement\n"
        "  - kind: fill\n    selector: input#source_key\n    value: EXP-1\n"
        "  - kind: fill\n    selector: input#applicant\n    value: a\n"
        "  - kind: fill\n    selector: input#expense_type\n    value: b\n"
        "  - kind: fill\n    selector: input#amount_cents\n    value: '1'\n"
        "  - kind: fill\n    selector: input#invoice_no\n    value: c\n"
        "  - kind: submit\n    value: ''\n",
        encoding="utf-8",
    )
    reg = DefinitionRegistry(path=str(tmp_path / "defs.json"), flows_dir=str(flows_dir))
    # 引导阶段已把这份文件发布为 v1
    assert reg.get_flow("demo_flow")["published_version"] == 1
    reg.deprecate("demo_flow", actor="tester", reason="下线")

    assert reg.sync_from_files() == []
    assert reg.sync_from_files() == []
    assert reg.get_flow("demo_flow")["head_version"] == 1


def test_rollback_failure_leaves_state_unchanged(tmp_path):
    empty = tmp_path / "flows"
    empty.mkdir()
    reg = DefinitionRegistry(path=str(tmp_path / "defs.json"), flows_dir=str(empty))
    _draft(reg)
    reg.publish("demo_flow", actor="tester")

    reg.delete_dataset("reimbursement", actor="admin", reason="下线")
    with pytest.raises(GovernanceError) as exc:
        reg.rollback("demo_flow", 1, actor="tester", reason="回退")
    assert exc.value.code == "rollback_blocked"

    after = reg.get_flow("demo_flow")
    assert after["head_version"] == 1
    assert after["draft_version"] is None
    assert [v["version"] for v in after["versions"]] == [1]


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.governance as governance
    import app.api as api

    empty = tmp_path / "flows"
    empty.mkdir()
    reg = DefinitionRegistry(path=str(tmp_path / "defs.json"), flows_dir=str(empty))
    monkeypatch.setattr(governance, "_registry", reg)
    monkeypatch.setattr(api, "_store", None)
    api._memory_runs.clear()
    return TestClient(api.app, raise_server_exceptions=False)


def test_api_full_lifecycle(client):
    r = client.post(
        "/flows/demo_flow/drafts",
        json={"definition": BASE_DEFINITION, "release_notes": "init"},
        headers={"X-Actor": "alice"},
    )
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    assert r.json()["status"] == "draft"

    # 草稿不可运行
    r = client.post("/flows/demo_flow/run")
    assert r.status_code == 409
    assert r.json()["code"] == "flow_not_published"

    r = client.put(
        f"/flows/demo_flow/drafts/{version}",
        json={"definition": BASE_DEFINITION, "expected_revision": 1},
        headers={"X-Actor": "alice"},
    )
    assert r.status_code == 200

    r = client.post("/flows/demo_flow/publish", json={}, headers={"X-Actor": "alice"})
    assert r.status_code == 200, r.text
    published = r.json()
    assert published["status"] == "published"
    assert published["published_at"]

    r = client.get("/flows/demo_flow")
    assert r.json()["status"] == "published"
    assert r.json()["published"]["content_hash"] == published["content_hash"]
    assert r.json()["versions"][0]["version"] == version

    r = client.post("/flows/demo_flow/deprecate", json={"reason": "下线"})
    assert r.status_code == 200
    r = client.post("/flows/demo_flow/run")
    assert r.status_code == 409

    events = client.get("/audit?flow=demo_flow").json()["events"]
    assert {e["action"] for e in events} >= {
        "draft_created",
        "draft_updated",
        "published",
        "deprecated",
    }
    assert all(e["actor"] for e in events)


def test_api_validation_errors_are_structured(client):
    bad = copy.deepcopy(BASE_DEFINITION)
    bad["steps"] = [{"kind": "fill", "selector": "input#a", "value": "1"}]
    client.post("/flows/demo_flow/drafts", json={"definition": bad})
    r = client.post("/flows/demo_flow/publish", json={})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "review_failed"
    assert body["detail"]["review"]["error_count"] > 0


def test_api_review_endpoint_does_not_persist(client):
    r = client.post(
        "/flows/review", json={"definition": {**BASE_DEFINITION, "dataset": "ghost"}}
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert client.get("/flows").json()["flows"] == []


def test_api_tests_are_isolated_from_repo_registry(client):
    """每个用例都从空注册表起步，不受仓库 data/definitions.json 影响。"""
    assert client.get("/flows").json()["flows"] == []
    r = client.post("/flows/demo_flow/drafts", json={"definition": BASE_DEFINITION})
    assert r.status_code == 201
    assert [f["name"] for f in client.get("/flows").json()["flows"]] == ["demo_flow"]


def test_api_yaml_draft_and_name_mismatch(client):
    yaml_text = (
        "name: demo_flow\n"
        "dataset: reimbursement\n"
        "steps:\n"
        "  - kind: open\n"
        "    value: /form/reimbursement\n"
        "  - kind: submit\n"
        "    value: ''\n"
    )
    r = client.post("/flows/demo_flow/drafts", json={"yaml": yaml_text})
    assert r.status_code == 201, r.text

    r = client.post(
        "/flows/other/drafts", json={"definition": BASE_DEFINITION}
    )
    assert r.status_code == 422
    assert r.json()["code"] == "name_mismatch"


def test_api_sync_imports_files_as_drafts_only(client, tmp_path, monkeypatch):
    flows_dir = tmp_path / "fileflows"
    flows_dir.mkdir()
    import app.governance as governance

    # 注册表先建立（此时目录为空），再落一个新文件，模拟"后来改动了 flows/*.yaml"。
    reg = DefinitionRegistry(path=str(tmp_path / "defs.json"), flows_dir=str(flows_dir))
    monkeypatch.setattr(governance, "_registry", reg)
    (flows_dir / "demo_flow.yaml").write_text(
        "name: demo_flow\ndataset: reimbursement\nsteps:\n"
        "  - kind: open\n    value: /form/reimbursement\n"
        "  - kind: submit\n    value: ''\n",
        encoding="utf-8",
    )

    r = client.post("/admin/sync-flows")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    # 导入的是草稿，绝不自动发布
    assert client.get("/flows/demo_flow").json()["status"] == "unpublished"
    assert client.post("/flows/demo_flow/run").status_code == 409


def test_api_unknown_flow_and_version(client):
    assert client.get("/flows/nope").status_code == 404
    assert client.post("/flows/nope/run").status_code == 404
    client.post("/flows/demo_flow/drafts", json={"definition": BASE_DEFINITION})
    assert client.get("/flows/demo_flow/versions/99").status_code == 404


def test_api_compatibility_endpoint(client):
    client.post("/flows/demo_flow/drafts", json={"definition": BASE_DEFINITION})
    client.post("/flows/demo_flow/publish", json={})
    changed = copy.deepcopy(BASE_DEFINITION)
    changed["steps"][1]["value"] = "EXP-CHANGED"
    r = client.post(
        "/flows/demo_flow/drafts", json={"definition": changed, "base_version": 1}
    )
    v2 = r.json()["version"]
    r = client.get(f"/flows/demo_flow/compatibility?from=1&to={v2}")
    assert r.status_code == 200
    assert any(b["code"] == "source_key_changed" for b in r.json()["breaking"])


# --------------------------------------------------------------------------- #
# 执行绑定（需要 mockapp）
# --------------------------------------------------------------------------- #


def _mock_alive() -> bool:
    try:
        return httpx.get(f"{MOCK}/healthz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


requires_mock = pytest.mark.skipif(
    not _mock_alive(), reason=f"mockapp not reachable at {MOCK}"
)


@pytest.fixture()
def clean_mock():
    httpx.post(f"{MOCK}/admin/reset", timeout=5)
    httpx.post(f"{MOCK}/admin/fault/clear", timeout=5)
    yield


@requires_mock
def test_run_binds_version_and_resume_keeps_it(client, clean_mock):
    client.post("/flows/demo_flow/drafts", json={"definition": BASE_DEFINITION})
    client.post("/flows/demo_flow/publish", json={})

    first = client.post("/flows/demo_flow/run").json()
    assert first["status"] == "succeeded", first
    assert first["flow_version"] == 1

    # 发布 v2 后，历史运行仍然指向 v1
    changed = copy.deepcopy(BASE_DEFINITION)
    changed["description"] = "v2"
    client.post(
        "/flows/demo_flow/drafts", json={"definition": changed, "base_version": 1}
    )
    client.post("/flows/demo_flow/publish", json={})

    stored = client.get(f"/runs/{first['run_id']}").json()
    assert stored["flow_version"] == 1

    # 恢复（强制重跑）沿用 v1，而不是切换到 v2
    resumed = client.post(
        f"/runs/{first['run_id']}/resume", json={"force": True}
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["flow_version"] == 1
    assert resumed.json()["resumed_from"] == first["run_id"]


@requires_mock
def test_run_of_deprecated_version_is_forced_to_new_or_rejected(client, clean_mock):
    client.post("/flows/demo_flow/drafts", json={"definition": BASE_DEFINITION})
    client.post("/flows/demo_flow/publish", json={})
    client.post("/flows/demo_flow/deprecate", json={"reason": "下线"})

    r = client.post("/flows/demo_flow/run")
    assert r.status_code == 409
    assert r.json()["code"] == "flow_not_published"

    r = client.post("/flows/demo_flow/run", json={"version": 1})
    assert r.status_code == 409
    assert r.json()["code"] == "version_deprecated"


@requires_mock
def test_run_failure_is_classified(client, clean_mock):
    broken = copy.deepcopy(BASE_DEFINITION)
    # 去掉必填字段 -> 服务端 422；审查只是 warning，仍可发布
    broken["steps"] = [s for s in broken["steps"] if s.get("selector") != "input#invoice_no"]
    client.post("/flows/demo_flow/drafts", json={"definition": broken})
    client.post("/flows/demo_flow/publish", json={})

    r = client.post("/flows/demo_flow/run")
    assert r.status_code == 502
    assert r.json()["flow_version"] == 1
    assert r.json()["failure_kind"] == "definition"

    detail = client.get(f"/runs/{r.json()['run_id']}").json()
    assert detail["diagnosis"]["failure_kind"] == "definition"
    assert detail["diagnosis"]["failed_step"] == len(broken["steps"]) - 1  # submit
    assert detail["diagnosis"]["failed_step_definition"]["kind"] == "submit"


@requires_mock
def test_run_id_reuse_is_rejected_without_switching_version(client, clean_mock):
    client.post("/flows/demo_flow/drafts", json={"definition": BASE_DEFINITION})
    client.post("/flows/demo_flow/publish", json={})
    first = client.post("/flows/demo_flow/run", json={"run_id": "same-id"}).json()
    assert first["flow_version"] == 1

    # 发布 v2 后复用同一 run_id 且非幂等：必须拒绝，不能静默改绑到 v2
    v2 = copy.deepcopy(BASE_DEFINITION)
    v2["description"] = "v2"
    client.post("/flows/demo_flow/drafts", json={"definition": v2, "base_version": 1})
    client.post("/flows/demo_flow/publish", json={})

    r = client.post(
        "/flows/demo_flow/run", json={"run_id": "same-id", "idempotent": False}
    )
    assert r.status_code == 409
    assert r.json()["code"] == "run_exists"

    # 幂等模式返回原记录，仍是 v1
    r = client.post("/flows/demo_flow/run", json={"run_id": "same-id"})
    assert r.status_code == 200
    assert r.json()["flow_version"] == 1

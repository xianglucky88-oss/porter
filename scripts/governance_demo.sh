#!/bin/sh
# governance_demo.sh -- 流程定义生命周期演示。
#
# 演示内容：
#   1. 引导发布的历史流程
#   2. 草稿未发布 -> 不可运行
#   3. 发布 v1，运行并记录版本绑定
#   4. 发布 v2：历史运行仍绑定 v1
#   5. 不兼容变更被拦截，显式放行后发布
#   6. 废弃后不能再发起新运行
#   7. 回退生成新版本，旧版本内容不可变
#
# 用法：
#   docker compose up -d && make govern-demo
#   或 API=http://localhost:9201 MOCK=http://localhost:9200 sh scripts/governance_demo.sh
set -eu

API="${API:-http://localhost:9201}"
MOCK="${MOCK:-http://localhost:9200}"
ACTOR="demo"

say() { printf '\n=== %s ===\n' "$1"; }

# 发送 JSON 请求。$1=method $2=path $3=body(可空)
api() {
  if [ -n "${3:-}" ]; then
    curl -s -X "$1" "$API$2" -H 'Content-Type: application/json' -H "X-Actor: $ACTOR" -d "$3"
  else
    curl -s -X "$1" "$API$2" -H "X-Actor: $ACTOR"
  fi
}

say "0. 重置 mockapp"
curl -fsS -X POST "$MOCK/admin/reset" >/dev/null
curl -fsS -X POST "$MOCK/admin/fault/clear" >/dev/null

say "1. 引导发布的既有流程（flows/*.yaml -> v1）"
api GET /flows | python3 -c '
import json, sys
for f in json.load(sys.stdin)["flows"]:
    print("  %-24s status=%-12s published=v%s runnable=%s"
          % (f["name"], f["status"], f["published_version"], f.get("runnable")))
'

DRAFT='{
  "definition": {
    "name": "demo_onboarding",
    "description": "演示：受治理的新流程",
    "dataset": "reimbursement",
    "steps": [
      {"kind": "open", "value": "/form/reimbursement"},
      {"kind": "fill", "selector": "input#source_key", "value": "DEMO-001"},
      {"kind": "fill", "selector": "input#applicant", "value": "演示员"},
      {"kind": "fill", "selector": "input#expense_type", "value": "差旅费"},
      {"kind": "fill", "selector": "input#amount_cents", "value": "100"},
      {"kind": "fill", "selector": "input#invoice_no", "value": "INV-DEMO"},
      {"kind": "click", "selector": "button#submit-btn"},
      {"kind": "submit", "value": ""}
    ]
  },
  "release_notes": "首次创建"
}'

say "2. 创建草稿（未发布 -> 不可运行）"
api POST /flows/demo_onboarding/drafts "$DRAFT" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("  draft v%s revision=%s review_ok=%s warnings=%s"
      % (d["version"], d["revision"], d["review"]["ok"], d["review"]["warning_count"]))
print("  findings: " + ", ".join(f["code"] for f in d["review"]["findings"]))
'
echo "  运行草稿："
api POST /flows/demo_onboarding/run | python3 -c '
import json, sys; d = json.load(sys.stdin); print("   -> %s: %s" % (d["code"], d["message"]))
'

say "3. 发布 v1"
api POST /flows/demo_onboarding/publish '{}' | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("  published v%s hash=%s..." % (d["version"], d["content_hash"][:20]))
'

say "3.1 以固定 run_id 运行一次"
RUN1=$(api POST /flows/demo_onboarding/run '{"run_id": "demo-run-1"}' | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d["run_id"])
print("  run %s status=%s flow_version=%s" % (d["run_id"], d["status"], d["flow_version"]), file=sys.stderr)
')
echo "  RUN1=$RUN1"

V2_DEF='{
  "base_version": 1,
  "definition": {
    "name": "demo_onboarding",
    "description": "演示：受治理的新流程（v2，仅描述变化）",
    "dataset": "reimbursement",
    "steps": [
      {"kind": "open", "value": "/form/reimbursement"},
      {"kind": "fill", "selector": "input#source_key", "value": "DEMO-001"},
      {"kind": "fill", "selector": "input#applicant", "value": "演示员"},
      {"kind": "fill", "selector": "input#expense_type", "value": "差旅费"},
      {"kind": "fill", "selector": "input#amount_cents", "value": "100"},
      {"kind": "fill", "selector": "input#invoice_no", "value": "INV-DEMO"},
      {"kind": "click", "selector": "button#submit-btn"},
      {"kind": "submit", "value": ""}
    ]
  }
}'

say "4. 发布 v2（兼容变更）"
api POST /flows/demo_onboarding/drafts "$V2_DEF" >/dev/null
api POST /flows/demo_onboarding/publish '{}' | python3 -c '
import json, sys; print("  published v%s" % json.load(sys.stdin)["version"])
'

say "4.1 历史运行仍绑定 v1"
api GET "/runs/$RUN1" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("  run %s: flow_version=%s definition.status=%s"
      % (d["run_id"], d["flow_version"], d["definition"]["status"]))
'

V3_DEF='{
  "base_version": 2,
  "definition": {
    "name": "demo_onboarding",
    "description": "演示：不兼容变更",
    "dataset": "reimbursement",
    "steps": [
      {"kind": "open", "value": "/form/reimbursement"},
      {"kind": "fill", "selector": "input#source_key", "value": "DEMO-999"},
      {"kind": "fill", "selector": "input#applicant", "value": "演示员"},
      {"kind": "fill", "selector": "input#expense_type", "value": "差旅费"},
      {"kind": "fill", "selector": "input#amount_cents", "value": "100"},
      {"kind": "fill", "selector": "input#invoice_no", "value": "INV-DEMO"},
      {"kind": "click", "selector": "button#submit-btn"},
      {"kind": "submit", "value": ""}
    ]
  }
}'

say "5. 不兼容变更（改 source_key）被拦截"
api POST /flows/demo_onboarding/drafts "$V3_DEF" >/dev/null
api POST /flows/demo_onboarding/publish '{}' | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("  blocked: %s - %s" % (d["code"], d["message"]))
print("  breaking: " + ", ".join(b["code"] for b in d["detail"]["breaking"]))
'
echo "  放行但不给 reason："
api POST /flows/demo_onboarding/publish '{"allow_breaking": true}' | python3 -c '
import json, sys; print("   -> %s" % json.load(sys.stdin)["code"])
'
echo "  给 reason 后发布："
api POST /flows/demo_onboarding/publish \
  '{"allow_breaking": true, "reason": "单号规则调整，已通知业务"}' | python3 -c '
import json, sys; print("   published v%s" % json.load(sys.stdin)["version"])
'

say "6. 版本历史"
api GET /flows/demo_onboarding/versions | python3 -c '
import json, sys
for v in json.load(sys.stdin)["versions"]:
    print("  v%s: %-11s hash=%s... notes=%s"
          % (v["version"], v["status"], v["content_hash"][:16], v["release_notes"]))
'

say "7. 审计事件"
api GET "/audit?flow=demo_onboarding&limit=20" | python3 -c '
import json, sys
for e in reversed(json.load(sys.stdin)["events"]):
    print("  %s %-15s v%s by %s" % (e["at"][11:19], e["action"], e["version"], e["actor"]))
'

say "8. 废弃 v3 -> 新运行被拒绝"
api POST /flows/demo_onboarding/deprecate '{"reason": "演示结束"}' >/dev/null
api POST /flows/demo_onboarding/run | python3 -c '
import json, sys; print("  new run -> %s" % json.load(sys.stdin)["code"])
'

say "9. 回退到 v1（生成 v4，v1 内容不变）"
api POST /flows/demo_onboarding/rollback \
  '{"to_version": 1, "reason": "v3 不符合业务预期"}' | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("  rolled back -> v%s (from v%s) hash=%s..."
      % (d["version"], d["rolled_back_from"], d["content_hash"][:16]))
'

echo
echo "完成。"

#!/bin/sh
# demo.sh -- 90 秒内可录完的固定演示。
#
# 场景：
#   1. 清空 mockapp
#   2. 跑一次报销流程（v1 经典布局）
#   3. 模拟页面改版（切换到 v2/v3 布局），同一流程不改代码照常成功
#   4. 切到歧义布局：流程拒绝提交，失败语义为 structure_changed
#   5. 展示运行记录中的失败分类与脱敏证据
set -eu
API="${API:-http://localhost:9201}"
MOCK="${MOCK:-http://localhost:9200}"

say() { printf '\n=== %s ===\n' "$1"; }

say "1. 重置"
curl -fsS -X POST "$MOCK/admin/reset" >/dev/null
curl -fsS -X POST "$MOCK/admin/fault/clear" >/dev/null

say "2. v1 布局下执行"
curl -fsS -X POST "$API/flows/reimbursement_batch/run" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], d.get('failure_kind'))"

say "3. 模拟页面改版：v2 / v3 布局"
for v in v2 v3; do
  curl -fsS -X POST "$MOCK/admin/layout?version=$v" >/dev/null
  printf 'layout=%s -> ' "$v"
  curl -fsS -X POST "$API/flows/reimbursement_batch/run" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], 'fingerprint:', (d.get('evidence') or {}).get('page',{}).get('fingerprint'))"
done

say "4. 歧义布局：拒绝提交"
curl -fsS -X POST "$MOCK/admin/layout?version=ambiguous" >/dev/null
curl -s -X POST "$API/flows/reimbursement_batch/run" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], d.get('failure_kind')); print((d.get('error') or '')[:120])"
printf 'mockapp 单据数（应为 3，歧义轮未写入）: '
curl -fsS "$MOCK/admin/state" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])"

say "5. 恢复 v1，查看运行历史"
curl -fsS -X POST "$MOCK/admin/layout?version=v1" >/dev/null
curl -fsS "$API/runs?limit=10" | python3 -m json.tool | head -40

say "6. account_opening 流程（业务校验拒绝示例）"
curl -s -X POST "$API/flows/account_opening/run" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], d.get('failure_kind'))"

echo "\ndone"

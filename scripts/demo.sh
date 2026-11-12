#!/bin/sh
# demo.sh -- 90 秒内可录完的固定演示。
#
# 场景：
#   1. 清空 mockapp
#   2. 跑一次报销流程
#   3. 重跑同一个流程，观察单据数变化
#   4. 展示 runs 表与业务单据的对应关系
set -eu
API="${API:-http://localhost:9201}"
MOCK="${MOCK:-http://localhost:9200}"

say() { printf '\n=== %s ===\n' "$1"; }

say "1. 重置"
curl -fsS -X POST "$MOCK/admin/reset" >/dev/null
curl -fsS -X POST "$MOCK/admin/fault/clear" >/dev/null

say "2. 第一次执行"
curl -fsS -X POST "$API/flows/reimbursement_batch/run"; echo

say "2.1 mockapp 侧单据"
curl -fsS "$MOCK/admin/state" | python3 -m json.tool

say "3. 模拟重试：同一流程再跑一次"
curl -fsS -X POST "$API/flows/reimbursement_batch/run"; echo

say "3.1 单据数"
curl -fsS "$MOCK/admin/state" | python3 -m json.tool

say "4. 运行记录"
curl -fsS "$API/runs?limit=10" | python3 -m json.tool | head -40

say "5. account_opening 流程"
curl -s -X POST "$API/flows/account_opening/run" | head -c 400; echo

echo "\ndone"

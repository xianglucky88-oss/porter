#!/bin/sh
# demo.sh -- 90 秒内可录完的固定演示。
#
# 场景：
#   1. 清空 mockapp
#   2. 跑一次报销流程（单发路径，重跑不再产生重复单据）
#   3. 批次执行：3 条记录一次录入
#   4. 故障演练：回执丢失 → unknown → 恢复时凭证据确认
#   5. 展示运行记录与批次终态
#
# 批次键由记录集决定：每次运行打上时间标签，保证演示自包含。
set -eu
API="${API:-http://localhost:9201}"
MOCK="${MOCK:-http://localhost:9200}"
TAG="$(date +%H%M%S)"

say() { printf '\n=== %s ===\n' "$1"; }

say "1. 重置"
curl -fsS -X POST "$MOCK/admin/reset" >/dev/null
curl -fsS -X POST "$MOCK/admin/fault/clear" >/dev/null

say "2. 单发执行（同一业务单号重跑不重复）"
curl -fsS -X POST "$API/flows/reimbursement_batch/run" >/dev/null
curl -fsS -X POST "$API/flows/reimbursement_batch/run" >/dev/null
curl -fsS "$MOCK/admin/state" | python3 -m json.tool

say "3. 批次执行：3 条记录"
BATCH=$(curl -fsS -X POST -H 'Content-Type: application/json' \
  -d "{\"records\":[
    {\"source_key\":\"EXP-D${TAG}-01\",\"applicant\":\"李娜\",\"expense_type\":\"差旅费\",\"amount_cents\":\"120000\",\"invoice_no\":\"INV-D${TAG}-01\"},
    {\"source_key\":\"EXP-D${TAG}-02\",\"applicant\":\"王强\",\"expense_type\":\"差旅费\",\"amount_cents\":\"86000\",\"invoice_no\":\"INV-D${TAG}-02\"},
    {\"source_key\":\"EXP-D${TAG}-03\",\"applicant\":\"赵敏\",\"expense_type\":\"住宿费\",\"amount_cents\":\"54000\",\"invoice_no\":\"INV-D${TAG}-03\"}]}" \
  "$API/flows/reimbursement_batch/batches" | python3 -c 'import json,sys; print(json.load(sys.stdin)["batch_id"])')
echo "batch_id=$BATCH"
curl -fsS -X POST "$API/batches/$BATCH/run" | python3 -m json.tool

say "4. 故障演练：回执丢失（目标已受理，客户端看到 5xx）"
curl -fsS -X POST "$MOCK/admin/fault?kind=drop&rate=1.0" >/dev/null
BATCH2=$(curl -fsS -X POST -H 'Content-Type: application/json' \
  -d "{\"records\":[
    {\"source_key\":\"EXP-D${TAG}-04\",\"applicant\":\"陈晨\",\"expense_type\":\"差旅费\",\"amount_cents\":\"33000\",\"invoice_no\":\"INV-D${TAG}-04\"}]}" \
  "$API/flows/reimbursement_batch/batches" | python3 -c 'import json,sys; print(json.load(sys.stdin)["batch_id"])')
curl -fsS -X POST "$API/batches/$BATCH2/run" | python3 -m json.tool
echo "-- 此时记录处于 unknown：目标系统其实已受理，但绝不能盲目重投 --"
curl -fsS -X POST "$MOCK/admin/fault/clear" >/dev/null

say "4.1 恢复：凭目标系统证据确认（宽限期后生效）"
i=0
while [ "$i" -lt 6 ]; do
  OUT=$(curl -fsS -X POST "$API/batches/$BATCH2/recover")
  STATUS=$(printf '%s' "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  if [ "$STATUS" = "succeeded" ]; then break; fi
  i=$((i + 1)); sleep 2
done
printf '%s\n' "$OUT" | python3 -m json.tool

say "5. 终态与运行记录"
curl -fsS "$MOCK/admin/state" | python3 -m json.tool
curl -fsS "$API/stats" | python3 -m json.tool

echo "\ndone"

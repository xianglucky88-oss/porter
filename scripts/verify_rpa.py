#!/usr/bin/env python3
"""verify_rpa.py —— RPA 运行时的一致性断言。

核心命题：**重试不应产生重复业务单据，故障不应产生"状态不明"的记录**。

检查项：
    V1 对同一批单据执行两次，mockapp 侧的业务单据数应等于 source_key 去重数
    V2 idempotency_key 应在提交时被携带（流程摘要字段）
    V3 每个成功轮次在 runs 表里都有对应记录
    V4 失败轮次的 error 应能定位到具体步骤
    V5 批次重跑 + 恢复不产生重复单据，且全部记录到达终态
    V6 回执丢失（drop 故障）后，unknown 记录凭目标系统证据确认成功

用法:
    python3 scripts/verify_rpa.py --base http://localhost:9201 --mock http://localhost:9200
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode())


def post(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # 4xx/5xx 也要拿到 body
        return json.loads(e.read().decode())


def check(items, name, ok, detail):
    items.append((name, ok, detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:9201")
    ap.add_argument("--mock", default="http://localhost:9200")
    ap.add_argument("--flow", default="reimbursement_batch")
    args = ap.parse_args()

    items: list[tuple[str, bool, str]] = []

    post(f"{args.mock}/admin/reset")

    flows = get(f"{args.base}/flows")["flows"]
    target = next((f for f in flows if f.get("name") == args.flow), None)
    if target is None:
        print(f"[FAIL] flow {args.flow} not found", file=sys.stderr)
        return 1

    # V2: 幂等键缺失
    check(
        items,
        "V2/idempotency",
        bool(target.get("has_idempotency_key")),
        f"flow={args.flow} has_idempotency_key={target.get('has_idempotency_key')}",
    )

    # V1: 连续执行两次
    r1 = post(f"{args.base}/flows/{args.flow}/run")
    r2 = post(f"{args.base}/flows/{args.flow}/run")
    state = get(f"{args.mock}/admin/state")
    distinct = state["distinct_source_keys"]
    total = state["total"]
    check(
        items,
        "V1/retry-idempotent",
        total == distinct,
        f"业务单据 {total} 条 / 去重单号 {distinct} 个 / 重复 {state['duplicates']} 条；"
        f"两次执行状态 {r1.get('status')}/{r2.get('status')}",
    )

    # V3: 运行记录
    try:
        runs = get(f"{args.base}/runs?limit=50")["runs"]
        succeeded = [r for r in runs if r["status"] == "succeeded"]
        check(items, "V3/runs-persisted", bool(succeeded), f"成功轮次 {len(succeeded)} / 共 {len(runs)}")
    except Exception as exc:  # noqa: BLE001
        check(items, "V3/runs-persisted", False, f"无法读取 /runs: {exc}")

    # V4: 失败应带步骤定位
    bad = next((f for f in flows if f.get("name") == "account_opening"), None)
    if bad:
        rb = post(f"{args.base}/flows/account_opening/run")
        err = rb.get("error") or ""
        check(
            items,
            "V4/failure-locatable",
            rb.get("status") == "failed" and ("step" in err or "422" in err or "required" in err.lower()),
            f"status={rb.get('status')} error={err[:120]!r}",
        )

    # V5: 批次重跑 + 恢复不产生重复
    # 注意：批次键由记录集决定，跨次运行必须换单号，否则会命中上次运行留下的批次
    tag = str(int(time.time()))
    records = [
        {
            "source_key": f"EXP-V5-{tag}-{i:02d}",
            "applicant": "验证员",
            "expense_type": "差旅费",
            "amount_cents": str(1000 + i),
            "invoice_no": f"INV-V5-{tag}-{i:02d}",
        }
        for i in range(3)
    ]
    try:
        b = post(f"{args.base}/flows/reimbursement_batch/batches", {"records": records})
        bid = b["batch_id"]
        post(f"{args.base}/batches/{bid}/run")
        post(f"{args.base}/batches/{bid}/run")
        rec = post(f"{args.base}/batches/{bid}/recover")
        detail = get(f"{args.base}/batches/{bid}")
        counts = detail.get("counts", {})
        state = get(f"{args.mock}/admin/state")
        ok = (
            rec.get("status") == "succeeded"
            and counts.get("succeeded") == 3
            and state["duplicates"] == 0
        )
        check(
            items,
            "V5/batch-rerun-idempotent",
            ok,
            f"批次状态={rec.get('status')} 成功记录={counts.get('succeeded')}/3 "
            f"全局重复={state['duplicates']}",
        )
    except Exception as exc:  # noqa: BLE001
        check(items, "V5/batch-rerun-idempotent", False, f"批次接口异常: {exc}")

    # V6: 回执丢失 → unknown → 证据确认
    try:
        post(f"{args.mock}/admin/fault?kind=drop&rate=1.0")
        b = post(
            f"{args.base}/flows/reimbursement_batch/batches",
            {
                "records": [
                    {**rec, "source_key": rec["source_key"].replace("V5", "V6"),
                     "invoice_no": rec["invoice_no"].replace("V5", "V6")}
                    for rec in records
                ]
            },
        )
        bid6 = b["batch_id"]
        run_summary = post(f"{args.base}/batches/{bid6}/run")
        unknown_n = run_summary.get("counts", {}).get("unknown", 0)
        post(f"{args.mock}/admin/fault/clear")
        before = get(f"{args.mock}/admin/state")["total"]
        # 证据确认有宽限期（提交可能在途），宽限期内恢复只等待；宽限期后必须确认完毕
        rec6, after = {}, before
        for _ in range(15):
            rec6 = post(f"{args.base}/batches/{bid6}/recover")
            after = get(f"{args.mock}/admin/state")["total"]
            if rec6.get("status") == "succeeded":
                break
            time.sleep(3)
        ok = unknown_n == 3 and rec6.get("status") == "succeeded" and after == before
        check(
            items,
            "V6/lost-receipt-reconciled",
            ok,
            f"回执丢失后 unknown={unknown_n}/3，恢复后状态={rec6.get('status')}，"
            f"确认前后单据数 {before}→{after}（不应新增）",
        )
    except Exception as exc:  # noqa: BLE001
        post(f"{args.mock}/admin/fault/clear")
        check(items, "V6/lost-receipt-reconciled", False, f"异常: {exc}")

    print("\n== RPA 一致性检查 ==")
    failed = 0
    for name, ok, detail in items:
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(f"\n结果: {len(items) - failed}/{len(items)} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

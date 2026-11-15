#!/usr/bin/env python3
"""verify_rpa.py —— RPA 运行时的一致性断言。

核心命题：**重试不应产生重复业务单据**；**失败必须可归因、证据必须可查**。

检查项：
    V1 对同一批单据执行两次，mockapp 侧的业务单据数应等于 source_key 去重数
    V2 idempotency_key 应在提交时被携带（流程摘要字段）
    V3 每个成功轮次在 runs 表里都有对应记录
    V4 失败轮次带 failure_kind，四类失败语义可区分
    V5 执行证据包含定位规则与页面指纹，且不泄露业务取值

用法:
    python3 scripts/verify_rpa.py --base http://localhost:9201 --mock http://localhost:9200
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode())


def post(url: str) -> dict:
    req = urllib.request.Request(url, method="POST")
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

    # V4: 失败语义可区分——account_opening 缺必填字段，应是 validation_rejected
    bad = next((f for f in flows if f.get("name") == "account_opening"), None)
    if bad:
        rb = post(f"{args.base}/flows/account_opening/run")
        kind = rb.get("failure_kind") or ""
        check(
            items,
            "V4/failure-classified",
            rb.get("status") == "failed" and kind == "validation_rejected",
            f"status={rb.get('status')} failure_kind={kind!r} error={(rb.get('error') or '')[:100]!r}",
        )

    # V5: 证据可解释且脱敏——定位规则、页面指纹存在；业务取值不出现
    ev = r1.get("evidence") or {}
    page = ev.get("page") or {}
    steps = ev.get("steps") or []
    fill_steps = [s for s in steps if s.get("kind") == "fill"]
    has_strategy = bool(fill_steps) and all(s.get("strategy") for s in fill_steps)
    blob = json.dumps(ev, ensure_ascii=False)
    leaked = [s for s in ("李娜", "348600", "INV-20261108-7781") if s in blob]
    check(
        items,
        "V5/evidence",
        bool(page.get("fingerprint")) and has_strategy and not leaked,
        f"fingerprint={page.get('fingerprint')!r} 定位步骤={len(fill_steps)} "
        f"均有策略={has_strategy} 泄露值={leaked or '无'}",
    )

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

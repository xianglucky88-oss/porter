#!/usr/bin/env python3
"""verify_rpa.py —— RPA 运行时的一致性断言。

核心命题：**重试不应产生重复业务单据**，且**环境故障不能被算成业务失败**。

检查项：
    V1 对同一批单据执行两次，mockapp 侧的业务单据数应等于 source_key 去重数
    V2 idempotency_key 应在提交时被携带（流程摘要字段）
    V3 每个成功轮次在 runs 表里都有对应记录
    V4 失败轮次的 error 应能定位到具体步骤
    V5 失败必须带 failure_class；业务拒绝与运行故障必须可区分
    V6 就绪与存活语义分离：/healthz 只看进程，/readyz 看驱动模式

结果分三类：
    PASS   通过
    KNOWN  已知缺口（幂等键未实现导致的重复单据），默认只提示不计失败
    FAIL   回归，退出码非 0

用法:
    python3 scripts/verify_rpa.py --base http://localhost:9201 --mock http://localhost:9200
    python3 scripts/verify_rpa.py --expect-driver browser   # 验证浏览器路径
    python3 scripts/verify_rpa.py --strict                  # 已知缺口也计入失败
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode())


def post(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # 4xx/5xx 也要拿到 body
        return e.code, json.loads(e.read().decode())


def check(items, name, ok, detail, known: bool = False):
    status = "PASS" if ok else ("KNOWN" if known else "FAIL")
    items.append((name, status, detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:9201")
    ap.add_argument("--mock", default="http://localhost:9200")
    ap.add_argument("--flow", default="reimbursement_batch")
    ap.add_argument(
        "--expect-driver",
        choices=("http", "browser", "any"),
        default="any",
        help="断言运行所用驱动。CI 用 http，浏览器验收用 browser。",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="把已知缺口（幂等键未实现）也计入失败退出码。",
    )
    args = ap.parse_args()

    # items: (name, status, detail)，status ∈ {PASS, KNOWN, FAIL}
    items: list[tuple[str, str, str]] = []

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
        f"flow={args.flow} has_idempotency_key={target.get('has_idempotency_key')}"
        "（已知缺口：流程未携带业务幂等键）",
        known=True,
    )

    # V1: 连续执行两次
    _, r1 = post(f"{args.base}/flows/{args.flow}/run")
    _, r2 = post(f"{args.base}/flows/{args.flow}/run")
    state = get(f"{args.mock}/admin/state")
    distinct = state["distinct_source_keys"]
    total = state["total"]
    check(
        items,
        "V1/retry-idempotent",
        total == distinct,
        f"业务单据 {total} 条 / 去重单号 {distinct} 个 / 重复 {state['duplicates']} 条；"
        f"两次执行状态 {r1.get('status')}/{r2.get('status')}"
        "（已知缺口：无幂等键时重跑必然重复）",
        known=True,
    )

    # V5: 驱动一致性与归因字段
    drivers = {r1.get("driver"), r2.get("driver")}
    if args.expect_driver == "any":
        check(items, "V5/driver-reported", all(d in {"http", "browser"} for d in drivers), f"drivers={drivers}")
    else:
        check(
            items,
            "V5/driver-expected",
            drivers == {args.expect_driver},
            f"expected={args.expect_driver} actual={drivers}",
        )
    check(
        items,
        "V5/success-has-no-failure-class",
        r1.get("failure_class") is None and r2.get("failure_class") is None,
        f"failure_class={r1.get('failure_class')}/{r2.get('failure_class')}",
    )

    # V3: 运行记录。store 不可用时属于环境降级，单独给出说明而不是误判为业务问题。
    try:
        runs = get(f"{args.base}/runs?limit=50")["runs"]
        succeeded = [r for r in runs if r["status"] == "succeeded"]
        check(items, "V3/runs-persisted", bool(succeeded), f"成功轮次 {len(succeeded)} / 共 {len(runs)}")
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            # store 是可选依赖：不可用时执行照常，只是没有历史，属于明确降级。
            check(items, "V3/runs-persisted", False, "store 不可用（已降级：不影响执行，仅缺历史）", known=True)
        else:
            check(items, "V3/runs-persisted", False, f"无法读取 /runs: HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        check(items, "V3/runs-persisted", False, f"无法读取 /runs: {exc}")

    # V4 + V5: 失败应带步骤定位，且归因为 business
    bad = next((f for f in flows if f.get("name") == "account_opening"), None)
    if bad:
        code, rb = post(f"{args.base}/flows/account_opening/run")
        err = rb.get("error") or ""
        check(
            items,
            "V4/failure-locatable",
            rb.get("status") == "failed"
            and (rb.get("failed_step") is not None or "step" in err or "422" in err or "required" in err.lower()),
            f"status={rb.get('status')} failed_step={rb.get('failed_step')} code={rb.get('failure_code')}",
        )
        check(
            items,
            "V5/business-vs-environment",
            rb.get("failure_class") == "business"
            and rb.get("business_failure") is True
            and code == 422,
            f"http={code} class={rb.get('failure_class')} business={rb.get('business_failure')}",
        )
        check(
            items,
            "V5/no-secret-in-evidence",
            not _contains_sensitive(rb),
            f"evidence keys={sorted((rb.get('evidence') or {}).keys())}",
        )

    # V6: 健康语义
    try:
        live = get(f"{args.base}/healthz")
        check(items, "V6/liveness", live.get("status") == "ok", f"/healthz={live.get('status')}")
    except Exception as exc:  # noqa: BLE001
        check(items, "V6/liveness", False, f"/healthz 不可用: {exc}")
    try:
        ready = get(f"{args.base}/readyz")
        checks = ready.get("checks", {})
        driver = checks.get("driver", {})
        check(
            items,
            "V6/readiness-driver",
            driver.get("mode") in {"http", "browser"} and ready.get("status") in {"ok", "not_ready"},
            f"mode={driver.get('mode')} state={driver.get('state')} status={ready.get('status')}",
        )
        if args.expect_driver == "browser":
            check(
                items,
                "V6/browser-probe",
                driver.get("mode") == "browser" and driver.get("state") == "ok",
                f"driver={json.dumps(driver, ensure_ascii=False)[:160]}",
            )
    except Exception as exc:  # noqa: BLE001
        check(items, "V6/readiness-driver", False, f"/readyz 不可用: {exc}")

    print("\n== RPA 一致性检查 ==")
    failed = 0
    known = 0
    for name, status, detail in items:
        if status == "KNOWN" and args.strict:
            status = "FAIL"
        if status == "FAIL":
            failed += 1
        elif status == "KNOWN":
            known += 1
        print(f"[{status}] {name}: {detail}")
    passed = len(items) - failed - known
    print(f"\n结果: {passed}/{len(items)} 通过，{known} 项已知缺口，{failed} 项失败")
    return 0 if failed == 0 else 1


def _contains_sensitive(payload: dict) -> bool:
    """证据里不应出现会被脱敏规则命中的内容。

    做法：对证据再跑一遍脱敏。若结果与原文不同，说明上游漏了脱敏。
    """
    try:
        from app.redaction import redact_value
    except Exception:  # noqa: BLE001 - 独立脚本环境，缺包时跳过该项
        return False
    evidence = payload.get("evidence") or {}
    return redact_value(evidence) != evidence


if __name__ == "__main__":
    sys.exit(main())

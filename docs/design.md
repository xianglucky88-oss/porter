# RPA 执行链路设计说明

## 目标

把"业务同事手工在内部系统里逐条填单"这件事自动化，
在保证**不漏单、不重单**的前提下提高批量录入效率。

## 数据流

```
flows/*.yaml
     │  解析
     ▼
  Flow / Step
     │  execute()
     ▼
  Driver（Http | Browser）
     │  open / fill / click / submit / read_text
     ▼
  mockapp（内部表单系统）
     │  受理
     ▼
  receipt_id ──> PorterStore.runs + submissions
```

## 步骤语义

| kind | 说明 | 失败是否可忽略 |
|---|---|---|
| `open` | 打开表单页 | 否 |
| `fill` | 填写输入框 | 否（`optional: true` 时可） |
| `select` | 下拉选择 | 否（同上） |
| `click` | 点击（提交前的按钮） | 否 |
| `wait` | 固定等待 | 否 |
| `submit` | 提交并拿回执 | 否 |
| `assert_text` | 断言页面上出现指定文本 | 否（`optional: true` 时可） |

## 两种驱动

`HttpDriver` 与 `BrowserDriver` 共享 `Driver` 协议，因此同一个 YAML 在两种驱动下
步骤序列完全一致。差异只在"怎么打目标系统"：

- HTTP 驱动直接调 `/api/submit`，字段与选择器做简单映射（`input#applicant` → `applicant`）；
- Browser 驱动真的打开页面、真的点按钮、真的等跳转。

这也意味着 **HTTP 驱动能通过的流程，Browser 驱动不一定能通过**——
比如选择器写错时 HTTP 驱动察觉不到（它不读 DOM），Browser 驱动会直接超时。
这个差异在出 "页面改版后自愈" 一类题目时很关键。

## 幂等性设计（未实现部分）

`mockapp` 的幂等约定是：

```
POST /api/submit
{ "idempotency_key": "<business-scoped key>", ... }
→ 首次 201，重放 200 + X-Idempotent-Replay: true
```

正确的 key 应该是"业务单据的稳定标识"，例如
`sha256(source_key + form_type + 关键业务字段)`，
而不是运行 ID、时间戳或随机数。

当前所有流程都没传，因此第一次提交都是 201。

## 观察点

| 指标 | 位置 | 正常范围 |
|---|---|---|
| 单据总数 | mockapp `/admin/state` → `total` | 等于去重单号数 |
| 重复条数 | 同上 → `duplicates` | 0 |
| 运行成功率 | `/stats` → `runs_by_status` | 与业务量匹配 |
| 重复单号 | `/stats` → `duplicate_source_keys` | 0 |

`scripts/verify_rpa.py` 把前两项写成了断言。

## 批量执行

当前没有批量入口。上层脚本循环调用 `/flows/{name}/run`。
一旦某次调用超时或返回 5xx，脚本的重试逻辑会再打一次，
而由于没有幂等键，两次都会成功建单。

## 待决策

1. 幂等键的生成口径（业务字段 vs 上游单号 vs 内容哈希）。
2. 是否引入"批次"概念，让一次批次的重试能整体判断"已生效多少条"。
3. 步骤级快照存哪：PG 表、还是仅写日志文件。

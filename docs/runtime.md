# 浏览器模式与 HTTP 模式的运行方案

本文给出两种驱动的适用边界、依赖组合、资源约束、健康语义与排障路径。
目标是让"浏览器路径的环境故障"不再表现为"业务表单提交失败"。

## 1. 适用边界

| 维度 | HTTP 驱动（`USE_BROWSER=0`） | 浏览器驱动（`USE_BROWSER=1`） |
|---|---|---|
| 验证对象 | 单据是否被目标系统正确受理 | 页面是否真的渲染、选择器是否命中、跳转是否发生 |
| 依赖 | Python 包 + 目标系统 JSON 接口 | 以上 + Chromium + 系统图形库 + 中文字体 + 足够 /dev/shm |
| 典型环境 | CI、离线演示、受限容器（只读根、小 shm、无字体） | 验收录屏、页面改版回归、真实提交链路 |
| 启动成本 | ~180MB 镜像，秒级启动 | ~1.2GB 镜像，首次启动需真实拉起浏览器 |
| 盲区 | 看不见 DOM：选择器写错、页面白屏都不会报错 | 受渲染环境、字体、沙箱策略影响 |

**硬性结论**：HTTP 通过 ≠ 页面可用。凡是涉及"页面是否被正确操作"的验收，
必须用浏览器路径；凡是只关心"单据是否被受理"的批量作业，HTTP 路径更稳、更便宜。

## 2. 镜像分层

`Dockerfile` 分三段，改代码不动浏览器层：

```
base          python:3.11-slim + 运行期核心依赖 + 非 root 用户 porter(10001) + tini
 ├─ app       纯 HTTP，~180MB，CI 与受限容器用
 └─ browser-deps   playwright==${PLAYWRIGHT_VERSION} + Chromium + 图形库 + Noto CJK 字体
      └─ browser   浏览器目标，~1.2GB
```

- `requirements.txt` 是唯一依赖来源；`base` 层按前缀剔除 `playwright` 与测试包，
  因此两个镜像的**核心依赖版本完全一致**，不会出现"HTTP 通过、浏览器因版本不同失败"。
- Chromium 版本只由构建参数 `PLAYWRIGHT_VERSION` 决定，且 `pip install playwright==X`
  与 `playwright install chromium` 必须成对升级。
- `/ms-playwright` 全局可读，运行期用非 root 用户 `porter` 直接复用，不需要写权限。

构建：

```bash
docker build --target app     -t porter:http    .
docker build --target browser -t porter:browser \
  --build-arg PLAYWRIGHT_VERSION=1.49.1 --build-arg PORTER_BUILD=2026.11.15-rc1 .
```

## 3. 可复现的依赖组合

浏览器路径的偶发差异几乎都来自以下五项，逐项固定：

| 项 | 固定方式 | 失败时的表现 |
|---|---|---|
| Chromium 版本 | `PLAYWRIGHT_VERSION` 构建参数 | 启动即失败（`chromium_missing`） |
| 系统库 | `playwright install --with-deps chromium` | 启动失败，`chromium_launch_failed`，证据里列出缺失库 |
| 沙箱策略 | `BROWSER_SANDBOX=auto`：非 root 且 user namespace 可用才启用；否则加 `--no-sandbox --disable-setuid-sandbox` | 以 root 跑且强制沙箱时启动失败 |
| 共享内存 | compose 给 `shm_size: 1gb`；`BROWSER_DEV_SHM=auto` 在 shm < 256MB 时加 `--disable-dev-shm-usage` | 页面白屏、渲染进程 SIGBUS |
| 字体 | 镜像装 `fonts-noto-cjk`；预检统计字体数 | 中文渲染成方框，断言文本永远不匹配 |

渲染一致性参数（`app/browser_runtime.py: launch_args`）固定为：
`--force-color-profile=srgb`、`--font-render-hinting=none`、`--disable-lcd-text`、
禁用扩展/后台网络/同步。Context 固定 `viewport 1280x900`、`locale zh-CN`、
`timezone Asia/Shanghai`、`reduced_motion reduce`。

`make probe` 或 `GET /health/driver` 可以直接打印这台机器上的实际组合；
后者不触发浏览器启动（避免轮询压满会话），需要重探请用 `POST /health/driver/probe`。

## 4. 并发会话与资源上限

- **会话上限**：`BROWSER_MAX_SESSIONS`（默认 2）。实现是运行目录下的 `flock` 槽位文件，
  属于**跨进程**限制，多 uvicorn worker 或同机多容器共享运行目录时依然成立。
- **排队**：超限后等待 `BROWSER_QUEUE_TIMEOUT`；超时抛 `SessionCapacityError`
  （`failure_class=transient`, `code=session_capacity`）。上游可按 `Retry-After` 重试。
- **会话硬超时**：`BROWSER_SESSION_TIMEOUT`（默认 300s）。到点从定时器线程按
  profile 标记 SIGKILL 掉该会话的 Chromium 进程（**不在非创建线程调用 Playwright**），
  主线程上的等待以 "browser has been closed" 结束，归 `environment`。
- **容器侧**：`pids_limit: 512`、`init: true`、`read_only: true` + tmpfs 运行目录，
  防止僵尸进程与无限起浏览器拖垮宿主机。

## 5. 崩溃后的进程回收

每个会话与预热探测都使用**独立的 `BrowserSession` 实例**：独立 Playwright driver、
独立浏览器进程、独立 `--user-data-dir` profile。

回收顺序：关闭 context → 关闭浏览器 → 停 Playwright →
`reap_processes(marker)` 扫描 `/proc/*/cmdline` 杀掉带该 profile 标记的残留进程 →
删除 profile 目录。即使 Chromium 被 SIGKILL、`close()` 无效，第二步仍能清干净。
启动时 `cleanup_stale_profiles()` 清理容器异常退出遗留的 profile。

## 6. 敏感运行数据隔离

- 运行目录 `PORTER_RUNTIME_DIR` 在容器内是 tmpfs，**不出容器、重启即清**；
  tmpfs 挂载根为 `1777`（部分运行时忽略 tmpfs 的 uid/gid 选项），
  其下 `profiles/`、槽位目录、每个会话的 profile 都由应用收紧到 `0700`。
- 失败产物（截图/trace）默认 `BROWSER_ARTIFACTS=off`；即使打开也写在运行目录。
- 失败证据是**白名单字段**（driver / action / selector / url / step_kind 等），
  不倾倒表单值；`app/redaction.py` 对键名与值形态（手机号、证件号、邮箱、USCC、
  URL query）做不可逆打码，错误文本在离开进程前统一过一遍。
- `ENABLE_ADMIN=0` 可关闭 `/admin/reset-batch`。

## 7. 健康语义（三层，刻意不合并）

| 端点 | 含义 | 依赖 | 不通过时该做什么 |
|---|---|---|---|
| `GET /healthz` | 存活：进程能响应 | 无 | 重启容器 |
| `GET /readyz` | 就绪：现在能否接活 | 目标系统可达；浏览器模式额外要求一次**真实启动探测** | 不要重启；按 `checks.driver.code` 修环境 |
| `GET /health/driver` | 证据：沙箱/shm/字体/启动参数/探测耗时 | 无 | 用于定位跨平台差异 |

浏览器模式下 `/readyz` 的 `checks.driver.state`：

- `ok` —— Chromium 真实启动并关闭成功；
- `warming` —— 预热尚未完成（启动初期），不要据此重启；
- `unavailable` —— 带 `code`：`playwright_missing` / `chromium_missing` /
  `chromium_launch_failed` / `session_capacity`；
- `degraded` —— **仅当显式开启 `BROWSER_FALLBACK=http`**，浏览器不可用但仍可接活，
  响应里给出 `effective_driver=http`。

`store`（PostgreSQL）只影响历史查询，`/readyz` 明确标为 `degraded` 而不判失败。

预检缓存的自愈规则：只有"原本探测可用、但运行中会话坏掉"才会让缓存失效并重新探测；
启动阶段就失败的结论会被保留，避免 `/readyz` 在 `warming` 与 `unavailable` 之间抖动。
若缓存为空（例如被手动清除），`/readyz` 会后台触发一次预热并返回 `warming`。

liveness 使用 `HEALTHCHECK`，就绪判定建议用编排层探针指向 `/readyz`：

```yaml
readinessProbe:
  httpGet: { path: /readyz, port: 9201 }
  periodSeconds: 15
  failureThreshold: 4      # 浏览器启动慢，给足余量
```

## 8. 失败归因：不要把环境故障报成业务失败

驱动层抛出类型化异常，执行引擎只写 `Run.failure_class`，调用方**不要解析 `error` 字符串**。

| failure_class | 含义 | HTTP | 可重试 | 处理 |
|---|---|---|---|---|
| `business` | 目标系统 4xx 明确拒绝（必填校验/业务规则） | 422 | 否 | 改数据后重跑 |
| `flow` | 选择器失效、断言不符、表单 404 | 422 | 否 | 改流程/选择器 |
| `environment` | 浏览器起不来/崩溃、字体/shm/沙箱、下游不可达、打开页面即被 401/403 拒绝 | 503 | 否 | 修环境，不要重试表单 |
| `transient` | 超时、5xx、会话容量耗尽 | 503 | 视情况 | 按 `Retry-After` 重试 |
| `internal` | 未分类 | 500 | 否 | 查日志 |

关键规则：

1. 响应体固定带 `failure_class`、`failure_code`、`business_failure`（布尔）、
   `failed_step`、`evidence`、`retryable`，以及 `X-Porter-Driver` 响应头。
2. **提交一旦开始（CLICK 提交按钮或 SUBMIT 步骤），任何失败都不再自动重试**，
   并标记 `evidence.submit_may_have_landed=true`——请求可能已经落单。
3. `RUN_MAX_ATTEMPTS > 1` 时，只有提交前、且 `retryable=true` 的失败才会重试。
4. 监控口径：`/stats` 的 `business_failures` 与 `non_business_failures` 分开统计，
   业务失败率不应包含环境故障。

## 9. 受限容器里的降级策略

| 场景 | 默认行为 | 可选降级 |
|---|---|---|
| 镜像没有 Chromium | `environment` / `chromium_missing`，本轮失败 | `BROWSER_FALLBACK=http` 用 HTTP 驱动补跑，结果标 `degraded=true` |
| 没有中文字体 | 仍可运行，`/readyz` 带 `font_state=missing` | 换 browser 镜像（自带 Noto CJK） |
| /dev/shm 很小 | `auto` 加 `--disable-dev-shm-usage` | 提高 `shm_size` 或显式 `BROWSER_DEV_SHM=on` |
| 容器以 root 运行 | `auto` 关闭 Chromium 沙箱 | 改用 `USER porter`（镜像默认） |
| 会话占满 | 排队，超时 `session_capacity`（transient） | 增大 `BROWSER_MAX_SESSIONS` 或多副本 |
| 只需要单据受理结果 | —— | 直接用 HTTP 目标镜像 |

降级的三条纪律：

1. **默认关闭**。生产默认 `BROWSER_FALLBACK=off`，避免"以为在测浏览器，其实在打接口"。
2. **只对环境故障降级**。容量耗尽（transient）不降级，否则并发问题被掩盖。
3. **降级必须留痕**。`degraded=true` + `degraded_reason` 落库、进响应、进 `/readyz`。

## 10. 升级与回滚

- 一次发布 = `PORTER_BUILD` 标签 + 固定的 `PLAYWRIGHT_VERSION`，二者写入镜像 LABEL
  并在 `/healthz`、`/health/driver` 自报。
- 升级 Chromium 时同时改 `requirements.txt` 与 compose 的 `PLAYWRIGHT_VERSION`，
  `browser-deps` 层整体重建；`app` 层不受影响。
- 灰度：先起新 `runner-browser`（如 9203）跑 `make verify-browser`，再切流量。
- 回滚：切回上一个 `PORTER_BUILD` 镜像即可；数据库迁移只做 `ADD COLUMN IF NOT EXISTS`，
  新旧版本共用同一张 `runs` 表，无需回滚 schema。
- 升级前后各跑一次 `make verify-browser`，对比 `/health/driver` 的启动参数与探测耗时。

## 11. 验证命令

```bash
make test             # 单测：归因、配额、回收、驱动语义（不需要 Chromium）
make up && make verify          # HTTP 路径一致性
make browser-up && make verify-browser   # 浏览器路径一致性（需要 Docker）
make probe            # 打印本机浏览器运行环境预检
```

`verify_rpa.py` 的结果分三类：`PASS`、`KNOWN`（已知缺口，默认不阻塞）、`FAIL`（回归）。
CI 用默认模式，让既有缺口保持可见；`--strict` 可把 KNOWN 也计为失败。

## 12. 已知缺口

- 流程仍未传 `idempotency_key`，重跑同一单据会产生重复记录（`make verify` 的 V1/V2 会显式失败）。
- 这是业务语义问题，不属于本方案的运行环境范畴；修复需要在流程 YAML 里给出
  业务作用域幂等键（如 `sha256(source_key + form_type + 关键字段)`）。

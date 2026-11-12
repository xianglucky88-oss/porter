# porter

给业务部门用的**批量表单录入**自动化工具。把一个 YAML 流程编译成浏览器/HTTP 操作序列，
打到内部系统（这里由仓库自带的 `mockapp` 模拟）上，逐条录入采购申请、费用报销、开户申请。

已经在用，但只在"一切顺利"时可靠：中途失败只能整批重来，重来会产生重复单据。

## 快速开始

```bash
cp .env.example .env
docker compose up --build -d
make demo      # 固定演示序列
make verify    # 一致性断言
make test      # 单测（需要 mockapp 在 9200）
```

不使用 Docker 的话：

```bash
pip install -r requirements.txt
python -m uvicorn mockapp.main:app --port 9200 &
MOCKAPP_URL=http://localhost:9200 python -m uvicorn app.api:app --port 9201
```

## 结构

```
app/config.py      环境变量与默认值
app/models.py      Flow / Step / Run 数据结构
app/selectors.py   选择器表
app/executor.py    执行引擎 + HttpDriver + BrowserDriver
app/store.py       PostgreSQL 运行记录
app/api.py         FastAPI：/flows /runs /stats
mockapp/main.py    目标表单系统（模拟内部老系统）
flows/*.yaml       业务流程定义
```

## 执行模型

一个 flow = 一串有序步骤：`open` → 若干 `fill` → `click` → `submit` → `assert_text`。
引擎逐条执行，任何一步抛错即整轮失败，不回溯、不补偿。

两种驱动共享同一套步骤定义：

- `HttpDriver`（默认）：直接调 mockapp 的 JSON 接口，启动快，适合 CI 与录屏。
- `BrowserDriver`（`USE_BROWSER=1`）：Playwright 驱动 Chromium，走真实 HTML 表单。
  用来证明"真的在操作页面"，而不是在后端伪造。

## 接口

```
GET  /healthz
GET  /flows                      列出流程 + 静态校验结果
GET  /flows/{name}               流程详情（含步骤级明细）
POST /flows/{name}/run           同步执行一次
GET  /runs?limit=20&flow=...     运行历史
GET  /stats                      汇总统计
```

## 批量录入

当前没有内置的批量入口：上层用脚本循环调用 `POST /flows/{name}/run`。
这也是重试语义混乱的根源之一——循环脚本在 5 次重试里各自独立提交。

## 测试

```bash
docker compose up -d mockapp
MOCKAPP_URL=http://localhost:9200 python -m pytest -q
```

测试不依赖 PostgreSQL：store 不可用时 API 会降级运行。

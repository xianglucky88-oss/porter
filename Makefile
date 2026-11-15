export PATH := ./.venv/bin:$(HOME)/.local/bin:$(PATH)
SHELL := /bin/sh
PY ?= python3
API ?= http://localhost:9201
BROWSER_API ?= http://localhost:9202
MOCK ?= http://localhost:9200

.PHONY: help
help:
	@echo "make up           启动 db / mockapp / runner（HTTP 驱动）"
	@echo "make browser-up   启动 db / mockapp / runner-browser（真实 Chromium）"
	@echo "make down         停止并清理"
	@echo "make demo         固定演示序列（90 秒内可录完）"
	@echo "make verify       一致性断言（HTTP）"
	@echo "make verify-browser 一致性断言（浏览器，端口 9202）"
	@echo "make test         单测（需要 mockapp 在 9200）"
	@echo "make test-browser 浏览器降级/配额/回收相关单测（无需 Chromium）"
	@echo "make lint         ruff + mypy（如已安装）"
	@echo "make local-up     不用 docker：本机同时起 mockapp 与 runner"
	@echo "make browser      用 Chromium 驱动跑一次（需要 playwright install）"
	@echo "make probe        打印浏览器运行环境预检结果"

.PHONY: up
up:
	docker compose up --build -d
	@sh scripts/wait_for.sh $(API)/healthz 90
	@sh scripts/wait_for.sh $(API)/readyz 90

.PHONY: browser-up
browser-up:
	docker compose --profile browser up --build -d db mockapp runner-browser
	@sh scripts/wait_for.sh $(BROWSER_API)/healthz 90
	@sh scripts/wait_for.sh $(BROWSER_API)/readyz 180

.PHONY: down
down:
	docker compose --profile browser down -v --remove-orphans

.PHONY: logs
logs:
	docker compose logs -f --tail=100 runner

.PHONY: browser-logs
browser-logs:
	docker compose --profile browser logs -f --tail=100 runner-browser

.PHONY: demo
demo:
	@sh scripts/demo.sh

.PHONY: verify
verify:
	$(PY) scripts/verify_rpa.py --base $(API) --mock $(MOCK) --expect-driver http

.PHONY: verify-browser
verify-browser:
	$(PY) scripts/verify_rpa.py --base $(BROWSER_API) --mock $(MOCK) --expect-driver browser

.PHONY: test
test:
	MOCKAPP_URL=$(MOCK) $(PY) -m pytest -q

.PHONY: test-browser
test-browser:
	MOCKAPP_URL=$(MOCK) $(PY) -m pytest -q tests/test_browser_runtime.py

.PHONY: lint
lint:
	-$(PY) -m ruff check app mockapp tests scripts
	-$(PY) -m mypy app

.PHONY: probe
probe:
	$(PY) -c "from app.browser_runtime import probe; import json; print(json.dumps(probe(launch=True).to_dict(), indent=2, ensure_ascii=False))"

.PHONY: local-up
local-up:
	@echo "两个进程需要各开一个终端；这里只演示命令"
	@echo "  python -m uvicorn mockapp.main:app --port 9200"
	@echo "  python -m uvicorn app.api:app --port 9201"

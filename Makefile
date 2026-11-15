export PATH := ./.venv/bin:$(HOME)/.local/bin:$(PATH)
SHELL := /bin/sh
PY ?= python3
API ?= http://localhost:9201
MOCK ?= http://localhost:9200

.PHONY: help
help:
	@echo "make up        启动 db / mockapp / runner"
	@echo "make down      停止并清理"
	@echo "make demo      固定演示序列（90 秒内可录完）"
	@echo "make govern-demo  定义生命周期演示（草稿→发布→废弃→回退→版本绑定）"
	@echo "make verify    一致性断言脚本"
	@echo "make test      单测（需要 mockapp 在 9200）"
	@echo "make lint      ruff + mypy（如已安装）"
	@echo "make local-up  不用 docker：本机同时起 mockapp 与 runner"
	@echo "make browser   用 Chromium 驱动跑一次（需要 playwright install）"

.PHONY: up
up:
	docker compose up --build -d
	@sh scripts/wait_for.sh $(API)/healthz 90

.PHONY: down
down:
	docker compose down -v --remove-orphans

.PHONY: logs
logs:
	docker compose logs -f --tail=100 runner

.PHONY: demo
demo:
	@sh scripts/demo.sh

.PHONY: govern-demo
govern-demo:
	@sh scripts/governance_demo.sh

.PHONY: verify
verify:
	$(PY) scripts/verify_rpa.py --base $(API) --mock $(MOCK)

.PHONY: test
test:
	MOCKAPP_URL=$(MOCK) $(PY) -m pytest -q

.PHONY: lint
lint:
	-$(PY) -m ruff check app mockapp tests scripts
	-$(PY) -m mypy app

.PHONY: local-up
local-up:
	@echo "两个进程需要各开一个终端；这里只演示命令"
	@echo "  python -m uvicorn mockapp.main:app --port 9200"
	@echo "  python -m uvicorn app.api:app --port 9201"

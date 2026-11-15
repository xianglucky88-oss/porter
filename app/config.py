"""运行配置。全部来自环境变量，提供可用的本地默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    """服务配置。"""

    database_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL", "postgresql://porter:porter@localhost:5432/porter"
        )
    )
    mockapp_url: str = field(default_factory=lambda: _env("MOCKAPP_URL", "http://localhost:9200"))
    flows_dir: str = field(default_factory=lambda: _env("FLOWS_DIR", "flows"))
    # 受治理流程定义的版本仓库。运行记录与审计都以此为事实来源。
    definitions_path: str = field(
        default_factory=lambda: _env("DEFINITIONS_PATH", "data/definitions.json")
    )
    headless: bool = field(default_factory=lambda: _env("HEADLESS", "1") == "1")
    # 提交后等待跳转的最长秒数。超时会被当成失败并触发重试。
    submit_timeout: float = field(default_factory=lambda: float(_env("SUBMIT_TIMEOUT", "8")))
    # 单步之间的人为停顿，模拟人工操作的节奏。
    step_delay: float = field(default_factory=lambda: float(_env("STEP_DELAY", "0")))
    # 是否使用 Playwright 驱动真实浏览器。0 时走纯 HTTP 驱动（便于 CI）。
    use_browser: bool = field(default_factory=lambda: _env("USE_BROWSER", "0") == "1")


settings = Settings()

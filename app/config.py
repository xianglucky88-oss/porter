"""运行配置。全部来自环境变量，提供可用的本地默认值。

配置分三层：
  * 通用：数据库、目标站点、流程目录；
  * 驱动边界：HTTP 模式与浏览器模式各自的超时、并发、降级策略；
  * 浏览器运行环境：沙箱、/dev/shm、字体、会话回收与产物目录。

非法取值在导入时直接报错（快速失败），避免跑到一半才发现配置写错。
"""

from __future__ import annotations

import os
import shlex
import tempfile
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _env_args(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    return shlex.split(raw) if raw else []


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
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))
    # 提交后等待跳转的最长秒数。超时归为瞬时故障（可重试），不是业务失败。
    submit_timeout: float = field(default_factory=lambda: _env_float("SUBMIT_TIMEOUT", 8.0, 0.1))
    # 单步之间的人为停顿，模拟人工操作的节奏。
    step_delay: float = field(default_factory=lambda: _env_float("STEP_DELAY", 0.0))
    # 是否使用 Playwright 驱动真实浏览器。0 时走纯 HTTP 驱动（便于 CI）。
    use_browser: bool = field(default_factory=lambda: _env_bool("USE_BROWSER", False))

    # ---- 驱动边界 -------------------------------------------------
    # 浏览器不可用时的降级策略：
    #   off  —— 直接失败，归因为 environment，绝不伪装成业务失败；
    #   http —— 用同一份流程走 HTTP 驱动补跑，明确标记 degraded（仅在提交前安全）。
    browser_fallback: str = field(
        default_factory=lambda: _env_choice("BROWSER_FALLBACK", "off", {"off", "http"})
    )
    # 一条运行最多尝试几次。默认 1：提交后重试可能产生重复单据，
    # 只有显式调大且失败发生在 submit 之前才会重试（见 executor）。
    run_max_attempts: int = field(default_factory=lambda: _env_int("RUN_MAX_ATTEMPTS", 1))

    # ---- 浏览器会话与资源上限 -------------------------------------
    browser_max_sessions: int = field(
        default_factory=lambda: _env_int("BROWSER_MAX_SESSIONS", 2)
    )
    browser_queue_timeout: float = field(
        default_factory=lambda: _env_float("BROWSER_QUEUE_TIMEOUT", 30.0, 0.0)
    )
    browser_launch_timeout: float = field(
        default_factory=lambda: _env_float("BROWSER_LAUNCH_TIMEOUT", 30.0, 1.0)
    )
    browser_navigation_timeout: float = field(
        default_factory=lambda: _env_float("BROWSER_NAVIGATION_TIMEOUT", 15.0, 1.0)
    )
    # 单个会话的硬上限；超过后该轮以 environment 失败并回收会话。
    browser_session_timeout: float = field(
        default_factory=lambda: _env_float("BROWSER_SESSION_TIMEOUT", 300.0, 10.0)
    )
    # 沙箱：auto 依据 uid 与 user namespace 能力决定；on/off 强制。
    browser_sandbox: str = field(
        default_factory=lambda: _env_choice("BROWSER_SANDBOX", "auto", {"auto", "on", "off"})
    )
    # /dev/shm：auto 时小于阈值就加 --disable-dev-shm-usage。
    browser_dev_shm: str = field(
        default_factory=lambda: _env_choice("BROWSER_DEV_SHM", "auto", {"auto", "on", "off"})
    )
    browser_min_shm_mb: int = field(
        default_factory=lambda: _env_int("BROWSER_MIN_SHM_MB", 256)
    )
    browser_extra_args: list[str] = field(
        default_factory=lambda: _env_args("BROWSER_EXTRA_ARGS")
    )
    # 失败产物（截图/trace）可能含业务数据，默认关闭。
    browser_artifacts: str = field(
        default_factory=lambda: _env_choice("BROWSER_ARTIFACTS", "off", {"off", "on"})
    )
    # 进程级敏感数据目录：浏览器 profile、产物都放这里，按会话 0700 隔离。
    # 容器里由 compose 指定为 tmpfs；本机默认落在按 uid 区分、0700 的临时目录，
    # 避免默认路径不可写导致 HTTP 模式也被拖累。
    runtime_dir: str = field(
        default_factory=lambda: _env(
            "PORTER_RUNTIME_DIR",
            os.path.join(tempfile.gettempdir(), f"porter-runtime-{os.getuid()}"),
        )
    )
    browser_probe_ttl: float = field(
        default_factory=lambda: _env_float("BROWSER_PROBE_TTL", 30.0, 1.0)
    )
    # 就绪检查里探测目标系统的超时。
    ready_target_timeout: float = field(
        default_factory=lambda: _env_float("READY_TARGET_TIMEOUT", 3.0, 0.1)
    )
    # 对外的管理接口（reset-batch）。生产建议关闭。
    enable_admin: bool = field(default_factory=lambda: _env_bool("ENABLE_ADMIN", True))

    # 运行数据脱敏：命中这些键的值在错误/证据/日志里一律打码。
    # REDACT_KEYS 可追加业务特有的敏感字段名（逗号分隔）。
    redact_keys: tuple[str, ...] = field(
        default_factory=lambda: (
            "password",
            "passwd",
            "secret",
            "token",
            "authorization",
            "cookie",
            "id_card",
            "id_number",
            "uscc",
            "bank_account",
            "account_no",
            "phone",
            "mobile",
            "email",
        )
        + tuple(
            item.strip()
            for item in os.environ.get("REDACT_KEYS", "").split(",")
            if item.strip()
        )
    )

    # 构建信息，仅用于运行时自报，不参与逻辑。
    build_tag: str = field(default_factory=lambda: _env("PORTER_BUILD", "dev"))


settings = Settings()

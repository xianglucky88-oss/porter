"""浏览器运行环境与降级策略的测试。

这些用例**不需要安装 Playwright 或 Chromium**：它们验证的是判定逻辑本身
（沙箱/shm/字体/会话配额/降级归因），而不是浏览器能不能渲染页面。
真实浏览器路径由 `make verify-browser` 在 browser 镜像里跑。
"""

from __future__ import annotations

import os
import time

import pytest

from app import browser_runtime, executor
from app.browser_runtime import (
    SlotLimiter,
    dev_shm_disabled,
    reap_processes,
    sandbox_enabled,
)
from app.config import settings
from app.errors import SessionCapacityError
from app.models import Flow, RunStatus


# --------------------------------------------------------------------------
# 环境判定
# --------------------------------------------------------------------------
def test_sandbox_auto_never_enables_as_root():
    original = settings.browser_sandbox
    try:
        settings.browser_sandbox = "auto"
        assert sandbox_enabled() == (os.geteuid() != 0 and browser_runtime.user_namespaces_available())
        settings.browser_sandbox = "off"
        assert sandbox_enabled() is False
        settings.browser_sandbox = "on"
        assert sandbox_enabled() is True
    finally:
        settings.browser_sandbox = original


def test_small_dev_shm_adds_disable_flag(monkeypatch):
    original = settings.browser_dev_shm
    try:
        settings.browser_dev_shm = "auto"
        monkeypatch.setattr(browser_runtime, "shm_size_mb", lambda path="/dev/shm": 64)
        assert dev_shm_disabled() is True
        assert "--disable-dev-shm-usage" in browser_runtime.launch_args()
        monkeypatch.setattr(browser_runtime, "shm_size_mb", lambda path="/dev/shm": 1024)
        assert dev_shm_disabled() is False
        assert "--disable-dev-shm-usage" not in browser_runtime.launch_args()
    finally:
        settings.browser_dev_shm = original


def test_launch_args_are_stable_across_runs():
    """同一镜像、同一配置下启动参数必须完全一致，否则渲染差异无法复现。"""
    assert browser_runtime.launch_args() == browser_runtime.launch_args()
    args = browser_runtime.launch_args()
    assert "--force-color-profile=srgb" in args
    assert "--disable-extensions" in args


def test_launch_args_include_user_overrides():
    original = list(settings.browser_extra_args)
    try:
        settings.browser_extra_args = ["--js-flags=--max-old-space-size=512"]
        assert "--js-flags=--max-old-space-size=512" in browser_runtime.launch_args()
    finally:
        settings.browser_extra_args = original


# --------------------------------------------------------------------------
# 会话配额
# --------------------------------------------------------------------------
def test_slot_limiter_caps_concurrent_sessions(tmp_path):
    limiter = SlotLimiter(str(tmp_path), slots=1)
    first = limiter.acquire(timeout=0)
    try:
        with pytest.raises(SessionCapacityError) as excinfo:
            limiter.acquire(timeout=0.1)
        assert excinfo.value.failure_class.value == "transient"
        assert excinfo.value.code == "session_capacity"
        assert excinfo.value.retryable is True
    finally:
        first.release()
    # 释放后立即可重新占用
    second = limiter.acquire(timeout=1)
    second.release()


def test_slot_limiter_allows_parallel_within_limit(tmp_path):
    limiter = SlotLimiter(str(tmp_path), slots=2)
    a = limiter.acquire(timeout=0)
    b = limiter.acquire(timeout=0)
    a.release()
    b.release()


def test_slot_limiter_is_cross_process_visible(tmp_path):
    """flock 是进程级的：另一个 limiter 实例共享同一目录也必须受限。"""
    one = SlotLimiter(str(tmp_path), slots=1)
    two = SlotLimiter(str(tmp_path), slots=1)
    slot = one.acquire(timeout=0)
    try:
        with pytest.raises(SessionCapacityError):
            two.acquire(timeout=0.05)
    finally:
        slot.release()


# --------------------------------------------------------------------------
# 崩溃回收
# --------------------------------------------------------------------------
def test_reap_processes_ignores_unrelated_processes():
    assert reap_processes("--user-data-dir=/nonexistent-port-marker-xyz") == 0


def test_reap_processes_kills_matching_process():
    import subprocess
    import sys

    marker = "--user-data-dir=/tmp/porter-reap-test-abc123"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", marker]
    )
    try:
        time.sleep(0.2)
        assert proc.poll() is None
        killed = reap_processes(marker)
        assert killed >= 1
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


# --------------------------------------------------------------------------
# 探测与降级
# --------------------------------------------------------------------------
def test_probe_reports_missing_playwright(monkeypatch):
    monkeypatch.setattr(browser_runtime, "_playwright_installed", lambda: False)
    result = browser_runtime.probe(launch=False)
    assert result.state == "unavailable"
    assert result.code == "playwright_missing"


def test_browser_mode_without_playwright_fails_as_environment():
    """浏览器模式但镜像没装浏览器：必须报 environment，不能当成表单失败。"""
    original = settings.use_browser
    original_fallback = settings.browser_fallback
    try:
        settings.use_browser = True
        settings.browser_fallback = "off"
        flow = Flow.from_file("flows/account_opening.yaml")
        run = executor.execute(flow)
    finally:
        settings.use_browser = original
        settings.browser_fallback = original_fallback

    assert run.status is RunStatus.FAILED
    assert run.failure_class == "environment"
    assert run.is_business_failure is False
    assert run.driver == "browser"
    assert run.degraded is False


def test_browser_fallback_runs_http_and_marks_degraded(monkeypatch):
    """显式开启降级：流程被 HTTP 驱动补跑，结果标记 degraded，仍保留降级原因。"""
    original = settings.use_browser
    original_fallback = settings.browser_fallback
    monkeypatch.setattr(browser_runtime, "_playwright_installed", lambda: False)
    monkeypatch.setattr(executor, "BrowserDriver", _unavailable_driver)
    try:
        settings.use_browser = True
        settings.browser_fallback = "http"
        flow = Flow.from_file("flows/reimbursement_batch.yaml")
        run = executor.execute(flow)
    finally:
        settings.use_browser = original
        settings.browser_fallback = original_fallback

    assert run.status is RunStatus.SUCCEEDED, run.error
    assert run.degraded is True
    assert "browser unavailable" in (run.degraded_reason or "")
    assert run.driver == "http"


def _unavailable_driver(*_args, **_kwargs):
    from app.errors import BrowserUnavailable

    raise BrowserUnavailable("chromium executable not found", code="chromium_missing")


def test_session_capacity_does_not_fall_back_to_http(monkeypatch):
    """容量耗尽是并发/资源问题，降级成 HTTP 会掩盖它。"""
    from app.errors import SessionCapacityError

    original = settings.use_browser
    original_fallback = settings.browser_fallback
    try:
        settings.use_browser = True
        settings.browser_fallback = "http"

        def _capacity(*_a, **_k):
            raise SessionCapacityError("no slot", evidence={"max_sessions": 1})

        monkeypatch.setattr(executor, "BrowserDriver", _capacity)
        flow = Flow.from_file("flows/reimbursement_batch.yaml")
        run = executor.execute(flow)
    finally:
        settings.use_browser = original
        settings.browser_fallback = original_fallback

    assert run.status is RunStatus.FAILED
    assert run.failure_class == "transient"
    assert run.degraded is False

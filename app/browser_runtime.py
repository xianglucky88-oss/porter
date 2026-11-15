"""浏览器运行环境：预检、启动参数、会话配额、崩溃回收。

这里的每个决定都对应一类容器里真实发生过的故障：

  * 沙箱：以 root 运行、或容器禁用 user namespace 时，Chromium 的 setuid sandbox
    起不来。`auto` 会据 uid 与 `max_user_namespaces` 判断，而不是无脑加 --no-sandbox。
  * 共享内存：Docker 默认 /dev/shm 只有 64MB，页面稍复杂就 SIGBUS 或白屏。
    `auto` 在 shm 小于阈值时加 `--disable-dev-shm-usage`，同时 compose 里显式给 1g。
  * 字体：镜像没装中文字体时页面渲染成方框，导致断言文本永远不匹配。
    预检会统计字体数量；缺失时就绪状态降级，而不是让业务断言背锅。
  * 会话上限：浏览器是本项目最贵的资源。用运行目录下的 flock 槽位文件做
    **跨进程**上限（多 worker 时也成立），超限排队，排队超时归为容量问题。
  * 崩溃回收：每个会话独占一个浏览器进程 + 独立 profile 目录，会话结束或崩溃后
    按 profile 标记扫描 /proc 杀掉残留进程，再删目录。一个会话崩溃不会污染其他会话。
  * 敏感数据：profile 与截图/trace 都在 0700 的运行目录里，产物默认关闭。
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import logging
import os
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .config import settings
from .errors import BrowserUnavailable, SessionCapacityError

log = logging.getLogger("porter.browser")

MB = 1024 * 1024


# --------------------------------------------------------------------------
# 预检
# --------------------------------------------------------------------------
@dataclass
class Probe:
    """一次浏览器可用性探测的结果。"""

    state: str  # ok | unavailable | warming
    code: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)
    # 是否真正启动过浏览器（只查可执行文件时为 False）。
    launched: bool = False

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def age(self) -> float:
        return time.time() - self.checked_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "code": self.code,
            "reason": self.reason,
            "launched": self.launched,
            "checked_at": self.checked_at,
            "age_seconds": round(self.age(), 1),
            "details": self.details,
        }


def shm_size_mb(path: str = "/dev/shm") -> int | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return int(st.f_frsize * st.f_blocks // MB)


def user_namespaces_available() -> bool:
    try:
        with open("/proc/sys/user/max_user_namespaces", "r", encoding="ascii") as fh:
            return int(fh.read().strip()) > 0
    except (OSError, ValueError):
        return False


def sandbox_enabled() -> bool:
    """auto: 非 root 且可用 user namespace 时才启用 Chromium 沙箱。"""
    if settings.browser_sandbox == "on":
        return True
    if settings.browser_sandbox == "off":
        return False
    return os.geteuid() != 0 and user_namespaces_available()


def dev_shm_disabled() -> bool:
    if settings.browser_dev_shm == "on":
        return True
    if settings.browser_dev_shm == "off":
        return False
    size = shm_size_mb()
    return size is None or size < settings.browser_min_shm_mb


def font_count() -> int:
    roots = ("/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"))
    total = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for _dirpath, _dirnames, filenames in os.walk(root):
            total += sum(1 for name in filenames if name.lower().endswith((".ttf", ".otf", ".ttc")))
    return total


def launch_args() -> list[str]:
    """固定一组"渲染尽量一致"的启动参数，并叠加环境适配。"""
    args = [
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-features=Translate,BackForwardCache,AcceptCHFrame",
        "--disable-sync",
        "--hide-scrollbars",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        # 渲染一致性：固定色彩配置与字体渲染，减少跨平台截图/文本差异。
        "--force-color-profile=srgb",
        "--font-render-hinting=none",
        "--disable-lcd-text",
    ]
    if dev_shm_disabled():
        args.append("--disable-dev-shm-usage")
    if not sandbox_enabled():
        args.append("--no-sandbox")
        args.append("--disable-setuid-sandbox")
    args.extend(settings.browser_extra_args)
    return args


def _playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _chromium_executable() -> str | None:
    """只读探测，不启动浏览器。"""
    if not _playwright_installed():
        return None
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            return pw.chromium.executable_path
        finally:
            pw.stop()
    except Exception:  # noqa: BLE001 - 探测失败就是不可用
        return None


def probe(launch: bool = False) -> Probe:
    """探测浏览器可用性。launch=True 会真的启动一次（能发现缺系统库）。"""
    details: dict[str, Any] = {
        "playwright_installed": _playwright_installed(),
        "uid": os.geteuid(),
        "sandbox": sandbox_enabled(),
        "dev_shm_disabled": dev_shm_disabled(),
        "dev_shm_mb": shm_size_mb(),
        "user_namespaces": user_namespaces_available(),
        "fonts": font_count(),
        "args": launch_args(),
        "headless": settings.headless,
    }
    exe = _chromium_executable()
    details["executable"] = exe
    if not _playwright_installed():
        return Probe("unavailable", "playwright_missing", "python package playwright is not installed", details)
    if not exe or not os.path.exists(exe):
        return Probe(
            "unavailable",
            "chromium_missing",
            "chromium executable not found; run 'playwright install chromium'",
            details,
        )
    if not launch:
        return Probe("ok", "executable_present", "chromium executable present (not launched)", details)

    manager = BrowserManager()
    return manager.warmup()


# --------------------------------------------------------------------------
# 跨进程会话槽位
# --------------------------------------------------------------------------
class Slot:
    def __init__(self, path: str, fd: int) -> None:
        self.path = path
        self.fd = fd

    def release(self) -> None:
        if self.fd < 0:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = -1


def _ensure_private_dir(path: str) -> None:
    """创建目录，并把**运行目录之下的整段新层级**都设成 0700。

    运行目录根可能是 1777 的 tmpfs（挂载选项不一定支持 uid/gid），
    但 profiles/、槽位目录、每个会话的 profile 必须只有运行用户可进。
    收紧范围严格限制在 `settings.runtime_dir` 之内，绝不动系统目录。
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    root = os.path.abspath(settings.runtime_dir)
    current = os.path.abspath(path)
    while current == root or current.startswith(root + os.sep):
        with contextlib.suppress(OSError):
            os.chmod(current, 0o700)
        if current == root:
            break
        current = os.path.dirname(current)


class SlotLimiter:
    """用运行目录下的 flock 槽位文件限制**同时存在的浏览器会话数**。

    与线程信号量相比，这套限制在多 uvicorn worker、甚至同机多容器共享运行目录时
    依然成立。排队超过 timeout 抛 SessionCapacityError（归为瞬时/容量，不是业务失败）。
    """

    def __init__(self, directory: str, slots: int, name: str = "browser") -> None:
        self.directory = os.path.join(directory, f"{name}-slots")
        self.slots = max(1, slots)
        _ensure_private_dir(self.directory)

    def acquire(self, timeout: float) -> Slot:
        deadline = time.monotonic() + max(0.0, timeout)
        tried = 0
        while True:
            # 每轮都把全部槽位扫一遍，再决定是否超时：
            # 只试一个槽位就判定耗尽，会让"有空槽但不在起始位置"被误报为容量不足。
            for idx in range(self.slots):
                path = os.path.join(self.directory, f"slot-{idx}.lock")
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()}\n".encode())
                    return Slot(path, fd)
                except BlockingIOError:
                    os.close(fd)
            tried += 1
            if time.monotonic() >= deadline:
                raise SessionCapacityError(
                    f"no browser session slot within {timeout:.0f}s "
                    f"(max {self.slots})",
                    evidence={
                        "max_sessions": self.slots,
                        "queue_timeout": timeout,
                        "attempts": tried,
                    },
                )
            time.sleep(0.05)


# --------------------------------------------------------------------------
# 进程回收
# --------------------------------------------------------------------------
def reap_processes(marker: str) -> int:
    """杀掉命令行里带唯一标记的残留 Chromium 进程。返回杀掉的进程数。"""
    if not marker or not os.path.isdir("/proc"):
        return 0
    killed = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\x00", b" ")
        except OSError:
            continue
        if marker.encode() not in cmdline:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    if killed:
        log.warning("reaped %d leftover browser processes for %s", killed, marker)
    return killed


def cleanup_stale_profiles(max_age_seconds: float = 6 * 3600) -> int:
    """清理启动前遗留的 profile（容器异常退出后最常见）。"""
    profiles = os.path.join(settings.runtime_dir, "profiles")
    removed = 0
    now = time.time()
    try:
        names = os.listdir(profiles)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(profiles, name)
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------
# 会话管理
# --------------------------------------------------------------------------
def concise_launch_error(text: str, limit: int = 240) -> str:
    """把 Playwright 的多行告警压成一行可读原因，完整文本放 evidence。"""
    if "missing dependencies to run browsers" in text:
        missing = [
            token
            for token in ("libnss3", "libnspr4", "libdbus-1-3", "libatk1.0-0", "libatspi2.0-0",
                          "libcups2", "libgbm1", "libdrm2", "libxkbcommon0", "libasound2")
            if token in text
        ]
        detail = f" ({', '.join(missing[:6])}{'…' if len(missing) > 6 else ''})" if missing else ""
        return f"chromium cannot start: host system libraries are missing{detail}"
    if "Executable doesn't exist" in text or "executable doesn't exist" in text:
        return "chromium executable not found; run 'playwright install chromium'"
    single = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return single[:limit]


class BrowserSession:
    """一次真实的浏览器会话：独立的浏览器进程、独立的 profile 目录。

    句柄全部挂在实例上，因此**并发会话之间、会话与预热探测之间互不影响**。
    退出（正常、异常、超时）都会：关闭 context/浏览器 → 停 Playwright →
    按 profile 标记回收残留 Chromium → 删除 profile。
    """

    def __init__(self, tag: str) -> None:
        self.profile = os.path.join(
            settings.runtime_dir, "profiles", f"session-{tag}-{uuid.uuid4().hex[:8]}"
        )
        _ensure_private_dir(self.profile)
        self.marker = f"--user-data-dir={self.profile}"
        self.args = launch_args()
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._timer: threading.Timer | None = None
        self.closed = False
        self.timed_out = False
        self._lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------
    def launch(self) -> None:
        if not _playwright_installed():
            raise BrowserUnavailable(
                "playwright is not installed in this image",
                evidence={"image_target": "browser"},
                code="playwright_missing",
            )
        from playwright.sync_api import sync_playwright

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=settings.headless,
                args=self.args,
                timeout=settings.browser_launch_timeout * 1000,
            )
        except Exception as exc:
            self.close()
            text = str(exc)
            code = "chromium_launch_failed"
            if "Executable doesn't exist" in text or "executable doesn't exist" in text:
                code = "chromium_missing"
            raise BrowserUnavailable(
                concise_launch_error(text),
                evidence={
                    "launch_args": self.args,
                    "headless": settings.headless,
                    "profile": self.profile,
                    "dev_shm_mb": shm_size_mb(),
                    "sandbox": sandbox_enabled(),
                    "fonts": font_count(),
                    "launch_error": text[:1500],
                },
                code=code,
            ) from exc

    def new_page(self) -> Any:
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id=os.environ.get("BROWSER_TIMEZONE", "Asia/Shanghai"),
            color_scheme="light",
            reduced_motion="reduce",
            accept_downloads=False,
            service_workers="block",
        )
        page = self._context.new_page()
        page.set_default_timeout(settings.browser_navigation_timeout * 1000)
        page.set_default_navigation_timeout(settings.browser_navigation_timeout * 1000)
        self._start_watchdog()
        return page

    def _start_watchdog(self) -> None:
        if settings.browser_session_timeout <= 0:
            return
        self._timer = threading.Timer(settings.browser_session_timeout, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _on_timeout(self) -> None:
        """超时回收：**只杀进程，不在别的线程调用 Playwright**。

        Playwright 的同步 API 绑定创建它的线程，从定时器线程调用 close() 会直接抛
        "can only be used inside the same thread"，会话反而回收不掉。这里按 profile
        标记 SIGKILL 掉 Chromium 进程组，主线程上正在等待的调用会以 Target closed
        结束（归 environment），随后主线程正常走 close() 完成清理。
        """
        log.warning(
            "browser session exceeded %ss, killing browser processes",
            settings.browser_session_timeout,
        )
        self.timed_out = True
        reap_processes(self.marker)

    def connected(self) -> bool | None:
        try:
            return bool(self._browser.is_connected())
        except Exception:  # noqa: BLE001 - is_connected 在进程已退时也会抛，一律视为未知
            return None

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        with contextlib.suppress(Exception):
            if self._context is not None:
                self._context.close()
        with contextlib.suppress(Exception):
            if self._browser is not None:
                self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                self._pw.stop()
        # 浏览器进程被 SIGKILL 时 close() 不会等子进程退出，这里按 profile 标记兜底。
        reap_processes(self.marker)
        if self.profile and os.path.isdir(self.profile):
            shutil.rmtree(self.profile, ignore_errors=True)


class BrowserManager:
    """会话配额 + 预检缓存 + 崩溃回收。

    并发模型：每个 Driver 一个 BrowserSession，槽位用 flock 做跨进程限流。
    这里只保存"探测结果"这种无状态缓存，不保存任何活动浏览器句柄。
    """

    def __init__(self) -> None:
        self.limiter = SlotLimiter(settings.runtime_dir, settings.browser_max_sessions)
        # 预检单独一个槽位：就绪探测不应和业务会话抢配额，
        # 否则会话占满时 /readyz 会被错误地判成"浏览器不可用"。
        self.probe_limiter = SlotLimiter(settings.runtime_dir, 1, name="probe")
        self._probe: Probe | None = None
        self._probe_lock = threading.Lock()
        self._probe_running = threading.Lock()

    # -- 预检缓存 ---------------------------------------------------
    def cached_probe(self) -> Probe | None:
        with self._probe_lock:
            return self._probe

    def _store_probe(self, result: Probe) -> Probe:
        with self._probe_lock:
            self._probe = result
        return result

    def invalidate_probe(self) -> None:
        with self._probe_lock:
            self._probe = None

    def _base_details(self) -> dict[str, Any]:
        return {
            "uid": os.geteuid(),
            "sandbox": sandbox_enabled(),
            "dev_shm_disabled": dev_shm_disabled(),
            "dev_shm_mb": shm_size_mb(),
            "fonts": font_count(),
            "args": launch_args(),
            "headless": settings.headless,
        }

    def warmup(self) -> Probe:
        """真实启动一次浏览器后关闭，作为浏览器模式的就绪依据。

        用 _probe_running 串行化：并发的预热/重探不会同时拉起多个浏览器。
        """
        if not self._probe_running.acquire(blocking=False):
            existing = self.cached_probe()
            if existing is not None:
                return existing
            # 已有探测在跑且没有历史结果：给一个明确的"预热中"状态。
            return Probe("warming", "probe_in_progress", "another probe is running")
        try:
            details = self._base_details()
            details["executable"] = _chromium_executable()
            details["playwright_installed"] = _playwright_installed()
            if not details["playwright_installed"]:
                return self._store_probe(
                    Probe("unavailable", "playwright_missing", "python package playwright is not installed", details)
                )
            if not details["executable"] or not os.path.exists(details["executable"]):
                return self._store_probe(
                    Probe(
                        "unavailable",
                        "chromium_missing",
                        "chromium executable not found; run 'playwright install chromium'",
                        details,
                    )
                )
            session = BrowserSession("warmup")
            started = time.monotonic()
            try:
                slot = self.probe_limiter.acquire(settings.browser_queue_timeout)
                try:
                    session.launch()
                finally:
                    slot.release()
                details["launch_ms"] = int((time.monotonic() - started) * 1000)
                result = Probe("ok", "launched", "chromium launched and closed", details, launched=True)
            except SessionCapacityError as exc:
                result = Probe("unavailable", exc.code, str(exc), details)
            except Exception as exc:  # noqa: BLE001 - 探测阶段任何异常都映射成 Probe，不向上抛
                details["error"] = str(exc)[:1500]
                code = exc.code if isinstance(exc, BrowserUnavailable) else "chromium_launch_failed"
                result = Probe("unavailable", code, concise_launch_error(str(exc)), details)
            finally:
                session.close()
            if details.get("fonts") == 0:
                details["fonts_warning"] = "no font files found; page text may render as tofu"
                details["font_state"] = "missing"
            return self._store_probe(result)
        finally:
            self._probe_running.release()

    def refresh_probe(self) -> Probe:
        return self.warmup()

    # -- 供驱动使用的会话 -------------------------------------------
    @contextlib.contextmanager
    def session(self, tag: str = "run") -> Iterator[Any]:
        """占用一个槽位，启动独立浏览器会话并给出一张新页面；退出时彻底回收。"""
        slot = self.limiter.acquire(settings.browser_queue_timeout)
        session = BrowserSession(tag)
        healthy = True
        try:
            session.launch()
            page = session.new_page()
            try:
                yield page
            finally:
                # 关闭前判定：超时被杀、或浏览器已断开，才说明环境本身有问题。
                # 业务失败（如 422 拒绝）时浏览器仍然健康，不应让 /readyz 抖动。
                healthy = not session.timed_out and session.connected() is not False
                session.close()
        except BaseException:
            healthy = False
            session.close()
            raise
        finally:
            slot.release()
            # 只有在"原本可用"的浏览器上发生了会话级故障，才需要重新真实探测；
            # 启动失败或本来就没探测通过时，保留缓存里的失败结论，避免 /readyz
            # 在 warming 与 unavailable 之间反复抖动。
            if not healthy:
                cached = self.cached_probe()
                if cached is not None and cached.ok:
                    self.invalidate_probe()


# 单例：API 进程内共享槽位与预检缓存。
manager = BrowserManager()


def classify_launch_error(exc: BaseException) -> BrowserUnavailable:
    text = str(exc)
    code = "browser_unavailable"
    lowered = text.lower()
    if "executable doesn't exist" in lowered or "executable does not exist" in lowered:
        code = "chromium_missing"
    return BrowserUnavailable(concise_launch_error(text), evidence={"cause": code}, code=code)

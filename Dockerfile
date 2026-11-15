# 两个可用的镜像目标：
#   app      纯 HTTP 驱动，CI、离线演示、受限容器（无 /dev/shm / 字体）用这个
#   browser  带 Chromium + 中文字体 + 系统依赖，验证真实页面路径时用这个
#
# 分层原则：依赖装在前面的层，代码放最后的层。改业务代码只重建最后一层，
# Chromium 与系统库（约 1GB）始终命中缓存，避免每次发布都重新下载浏览器。
#
# 升级/回滚：镜像用 PORTER_BUILD 标签标记（如 2026.11.15-rc1），
# 浏览器版本由 PLAYWRIGHT_VERSION 唯一决定；回滚 = 切回上一个 PORTER_BUILD 镜像。

ARG PYTHON_VERSION=3.11
ARG PLAYWRIGHT_VERSION=1.49.1

FROM python:${PYTHON_VERSION}-slim AS base
# 浏览器与运行数据统一放 /run/porter，挂载为 tmpfs/卷，不写容器可写层。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORTER_RUNTIME_DIR=/run/porter
WORKDIR /app

# 系统层只装运行期必需的最小集合；浏览器依赖在 browser 层单独安装。
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# 依赖层：从 requirements.txt 派生核心依赖（去掉仅浏览器/测试用的包）。
COPY requirements.txt ./
RUN grep -v '^#' requirements.txt | grep -v '^$' \
        | grep -vE '^(playwright|pytest|pytest-asyncio)' > /tmp/req-core.txt \
    && pip install -r /tmp/req-core.txt

# 非 root 运行：Chromium 的 setuid/user-namespace 沙箱在非 root 下才可能启用；
# 同时把 profile、截图等敏感运行数据限制在该用户下。
ARG PORTER_UID=10001
RUN groupadd --gid ${PORTER_UID} porter \
    && useradd --uid ${PORTER_UID} --gid ${PORTER_UID} --create-home --shell /usr/sbin/nologin porter \
    && mkdir -p ${PORTER_RUNTIME_DIR} \
    && chown -R porter:porter ${PORTER_RUNTIME_DIR} \
    && chmod 0700 ${PORTER_RUNTIME_DIR}

# tini 作为 PID 1，回收 Chromium 崩溃后留下的僵尸子进程。
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]


# ---------------------------------------------------------------------------
# 纯 HTTP 目标
# ---------------------------------------------------------------------------
FROM base AS app
ARG PORTER_BUILD=dev
ENV PORTER_BUILD=${PORTER_BUILD} \
    USE_BROWSER=0
LABEL org.opencontainers.image.title="porter" \
      org.opencontainers.image.description="porter HTTP driver" \
      org.opencontainers.image.version="${PORTER_BUILD}" \
      porter.driver="http"
COPY app ./app
COPY mockapp ./mockapp
COPY flows ./flows
COPY scripts ./scripts
RUN chown -R porter:porter /app
USER porter
EXPOSE 9200 9201
# 存活探针只验证进程能响应，不依赖数据库/浏览器。
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9201/healthz', timeout=2).status==200 else 1)"
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "9201"]


# ---------------------------------------------------------------------------
# 浏览器目标
# ---------------------------------------------------------------------------
# 浏览器依赖层单独成型：PyPI 包、Chromium 二进制、系统库、字体各自成层。
# 只有 PLAYWRIGHT_VERSION 变化时才会重新下载浏览器；改代码只重建 browser 层。
FROM base AS browser-deps
ARG PLAYWRIGHT_VERSION
# 浏览器二进制与系统库装到独立层；/ms-playwright 全局只读，
# 运行期（非 root 用户 porter）直接复用，无需任何写权限。
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0
# --with-deps 安装 Chromium 依赖的字体/图形/音频系统库；
# 中文字体单独补，避免页面渲染成方框导致断言永远不匹配。
RUN pip install playwright==${PLAYWRIGHT_VERSION} \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        fonts-dejavu-core \
        fonts-liberation \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/ms-playwright \
    && chmod -R a+rX ${PLAYWRIGHT_BROWSERS_PATH}


FROM browser-deps AS browser
ARG PORTER_BUILD=dev
# 不设默认值：沿用构建根部的 PLAYWRIGHT_VERSION，保证 LABEL 与实际安装版本一致。
ARG PLAYWRIGHT_VERSION
# 默认不降级：浏览器起不来就明确报 environment，绝不静默改走 HTTP。
ENV PORTER_BUILD=${PORTER_BUILD} \
    USE_BROWSER=1 \
    HEADLESS=1 \
    BROWSER_FALLBACK=off
LABEL org.opencontainers.image.title="porter" \
      org.opencontainers.image.description="porter browser driver (Chromium)" \
      org.opencontainers.image.version="${PORTER_BUILD}" \
      porter.driver="browser" \
      porter.playwright.version="${PLAYWRIGHT_VERSION}"
COPY app ./app
COPY mockapp ./mockapp
COPY flows ./flows
COPY scripts ./scripts
RUN chown -R porter:porter /app
USER porter
EXPOSE 9200 9201
# 存活探针同上；浏览器是否可用由 /readyz 判定（需要真实启动一次，较慢，
# 不适合放在高频 liveness 里）。
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9201/healthz', timeout=2).status==200 else 1)"
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "9201"]

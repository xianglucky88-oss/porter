# 两个可用的镜像目标：
#   app      纯 HTTP 驱动，~180MB，CI 与离线演示用这个
#   browser  带 Chromium，~1.2GB，录屏证明"真的在操作页面"时用这个
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
# 只装运行期依赖，测试依赖在 CI 里单独装
RUN grep -v '^#' requirements.txt | grep -v '^$' > /tmp/req.txt && \
    sed -i '/pytest/d' /tmp/req.txt && \
    pip install -r /tmp/req.txt
COPY app ./app
COPY mockapp ./mockapp
COPY flows ./flows
COPY scripts ./scripts

FROM base AS app
EXPOSE 9200 9201
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "9201"]

FROM base AS browser
RUN pip install playwright==1.49.1 && playwright install --with-deps chromium
ENV USE_BROWSER=1
EXPOSE 9200 9201
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "9201"]

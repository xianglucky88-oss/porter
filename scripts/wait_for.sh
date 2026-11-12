#!/bin/sh
# 等待 HTTP 端点就绪。
# 用法: wait_for.sh <url> [timeout_seconds]
set -u
url="${1:?usage: wait_for.sh <url> [timeout]}"
timeout="${2:-60}"
i=0
while [ "$i" -lt "$timeout" ]; do
  if curl -fsS -o /dev/null "$url" 2>/dev/null; then
    echo "ready: $url (${i}s)"
    exit 0
  fi
  i=$((i + 1))
  sleep 1
done
echo "timeout waiting for $url after ${timeout}s" >&2
exit 1

#!/usr/bin/env bash
# 启动 Draft-Forge 工程图纸服务（STEP→2D 工程图 / ECAD→制程图纸）
#   在工程根目录下执行:  ./start.sh [端口]      端口默认 8000
set -euo pipefail

PORT="${1:-8000}"

if [ ! -f "web/server.py" ]; then
  echo "✗ 请在 draft-forge 工程根目录下执行本脚本（当前目录找不到 web/server.py）" >&2
  exit 1
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "✗ 未找到 .venv —— 先创建虚拟环境并装依赖:" >&2
  echo "    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

echo "▶ Draft-Forge 启动中 …  http://127.0.0.1:${PORT}/   (Ctrl+C 停止)"
# 用 python -m 而非 uvicorn 脚本：目录改名/迁移后仍能启动（不依赖脚本内的绝对路径）
exec .venv/bin/python -m uvicorn web.server:app --port "${PORT}"

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

command -v node >/dev/null 2>&1 || { echo "未找到 Node.js，请先安装 Node.js 18 或更高版本。"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "未找到 Python 3。"; exit 1; }

if [[ -z "${DASHSCOPE_API_KEY:-}" && ! -s "$HOME/.dashscope_api_key" ]]; then
  echo "未找到 DashScope API Key。请设置 DASHSCOPE_API_KEY，或写入 ~/.dashscope_api_key。"
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -c "import fastapi, uvicorn, multipart, PIL" >/dev/null 2>&1; then
  "$VENV_DIR/bin/pip" install -r "$ROOT_DIR/backend/requirements.txt"
fi

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  npm install --prefix "$ROOT_DIR/frontend"
fi

cleanup() {
  [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(
  cd "$ROOT_DIR/backend"
  "$VENV_DIR/bin/python" -m uvicorn app:app --host 127.0.0.1 --port 8000
) &
BACKEND_PID=$!

npm run dev --prefix "$ROOT_DIR/frontend" -- --host 127.0.0.1 &
FRONTEND_PID=$!

echo "中药主图 Demo 已启动：http://127.0.0.1:5173"
wait "$BACKEND_PID" "$FRONTEND_PID"

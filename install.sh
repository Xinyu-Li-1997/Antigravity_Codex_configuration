#!/usr/bin/env bash
# Antigravity & Codex 统一工作流与双向协作安装脚本 (Linux / macOS / WSL2)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1

echo "[*] 启动 Antigravity & Codex 统一配置安装器..."

if command -v python3 >/dev/null 2>&1; then
    python3 -B -X utf8 "${SCRIPT_DIR}/install.py" "$@"
elif command -v python >/dev/null 2>&1; then
    python -B -X utf8 "${SCRIPT_DIR}/install.py" "$@"
else
    echo "[!] 错误：未找到 Python 3 环境，请先安装 Python 3.9+。" >&2
    exit 1
fi

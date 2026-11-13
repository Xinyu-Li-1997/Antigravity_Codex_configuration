#!/usr/bin/env bash
# Antigravity & Codex 统一工作流与双向协作安装脚本 (Linux / macOS / WSL2)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1

echo "[*] 启动 Antigravity & Codex 统一配置安装器..." >&2

for candidate in python3 python; do
    executable="$(type -P "${candidate}")" || continue
    if "${executable}" -I -B -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        exec "${executable}" -B -X utf8 "${SCRIPT_DIR}/install.py" "$@"
    fi
done

echo "[!] 错误：未找到可执行的 Python 3.9+，请检查 PATH。" >&2
exit 1

#!/usr/bin/env bash
# Antigravity & Codex 统一工作流与双向协作安装脚本 (Linux / WSL2)
set -euo pipefail

# 清空 CDPATH，防止 cd 的额外输出混入安装器路径。
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export PYTHONDONTWRITEBYTECODE=1

printf '%s\n' "[*] 启动 Antigravity & Codex 统一配置安装器..." >&2

for candidate in python3 python; do
    executable="$(type -P "${candidate}")" || continue
    if probe_output="$("${executable}" -I -B -c 'import sys; print("user-guard-python-ok"); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null)" &&
        [[ "${probe_output}" == "user-guard-python-ok" ]]; then
        exec "${executable}" -B -X utf8 "${SCRIPT_DIR}/install.py" "$@"
    fi
done

printf '%s\n' "[!] 错误：未找到可执行的 Python 3.11+，请检查 PATH。" >&2
exit 1

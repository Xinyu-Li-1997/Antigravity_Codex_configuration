#!/usr/bin/env python3
"""
================================================================================
  Antigravity & Codex 统一工程／科研工作流与双向协作一键安装器
================================================================================
功能特性：
1. 支持三种部署形态，自动探测本机已安装客户端并按需适配（--app auto|both|antigravity|codex）：
   - 单反重力独立模式：部署 ~/.gemini/config/plugins/user-guard 全套插件（含双模压缩恢复与两阶段长会话交接停机门禁）、CLI 默认模型补缺与可选 Zotero MCP；
   - 单 Codex 独立模式：部署 ~/.codex/AGENTS.md（保留自定义本机硬件段）、agents/readonly-*.toml 只读子代理、
     hooks/ 原话压缩恢复和长会话交接钩子、config.toml 安全沙箱和默认模型补缺及可选 Zotero MCP；
   - 双端完整协作模式：同时部署上述两套独立工作流，并同步双端共享的 cae-harness 跨平台派工与验收技能。
2. 当本机仅检测到单客户端时，清晰提示是否先安装另一方；选择不安装（或非交互 --yes）即单独配置当前客户端，零报错闭环。
3. 提供 --probe-apps 纯只读环境探测 JSON 输出，供 Codex 或反重力读取仓库后自动引导自举配置。
4. 以 -B 无字节码模式运行 tests/ 下全套离线与端到端回归测试。
================================================================================
"""

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import tomllib
except ImportError:  # Python 3.9 和 3.10
    tomllib = None

# 全平台强制标准流为 UTF-8
for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

IGNORED_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", ".gitkeep"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd"}
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _is_mnt_windows_path(path_str: Optional[str]) -> bool:
    """在 POSIX/WSL 下判断可执行文件路径是否位于 /mnt/<盘符>/ 挂载的 Windows 侧"""
    if os.name == "nt" or not path_str:
        return False
    return bool(re.match(r"^/mnt/[a-zA-Z]/", path_str.replace("\\", "/")))


def _find_windows_codex_desktop_bin() -> Optional[str]:
    """在 Windows 下扫描 Codex 桌面端内置的 codex.exe"""
    if os.name != "nt":
        return None
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    roots = [Path(local_app) / "OpenAI" / "Codex"] if local_app else []
    roots.append(Path.home() / "AppData" / "Local" / "OpenAI" / "Codex")
    candidates: List[Path] = []
    for r in roots:
        if r.is_dir():
            candidates.extend(p for p in r.rglob("codex.exe") if p.is_file())
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0])


def _probe_wsl_windows_profile_dir() -> Optional[Path]:
    """在 WSL 环境下探测对应的 Windows 宿主用户主目录（如 /mnt/c/Users/<user>），只读零副作用"""
    if os.name == "nt" or not shutil.which("cmd.exe") or not shutil.which("wslpath"):
        return None
    try:
        cmd_exe = shutil.which("cmd.exe") or "cmd.exe"
        win_profile = subprocess.run(
            [cmd_exe, "/d", "/c", "echo", "%USERPROFILE%"],
            cwd=str(Path(cmd_exe).parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        ).stdout.strip()
        if not win_profile or win_profile.startswith("%"):
            return None
        wsl_home = subprocess.run(
            ["wslpath", "-u", win_profile],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        ).stdout.strip()
        p = Path(wsl_home)
        return p if p.is_dir() else None
    except Exception:
        return None


def detect_installed_apps() -> Dict[str, Any]:
    """
    只读探测当前运行环境的 Antigravity 与 Codex 安装状态（区分本侧原生与跨子系统互操作）。
    """
    home = Path.home()
    gemini_root = home / ".gemini"
    configured_codex = os.environ.get("CODEX_HOME", "").strip()
    codex_root = Path(configured_codex).expanduser() if configured_codex else (home / ".codex")

    # 1. Antigravity 检测
    agy_exe_name = "agy.exe" if os.name == "nt" else "agy"
    raw_agy_which = shutil.which("agy") or shutil.which("agy.exe")
    local_agy_bin = gemini_root / "bin" / agy_exe_name
    ag_native_bin: Optional[str] = None
    ag_interop_bin: Optional[str] = None
    if local_agy_bin.is_file():
        ag_native_bin = str(local_agy_bin)
    elif raw_agy_which:
        if _is_mnt_windows_path(raw_agy_which):
            ag_interop_bin = raw_agy_which
        else:
            ag_native_bin = raw_agy_which

    ag_native_dir = any((gemini_root / sub).is_dir() for sub in ("antigravity", "antigravity-cli", "config"))
    ag_native = bool(ag_native_bin or ag_native_dir)

    # 2. Codex 检测
    raw_codex_which = shutil.which("codex") or shutil.which("codex.exe")
    cx_native_bin: Optional[str] = None
    cx_interop_bin: Optional[str] = None
    if raw_codex_which:
        if _is_mnt_windows_path(raw_codex_which):
            cx_interop_bin = raw_codex_which
        else:
            cx_native_bin = raw_codex_which
    if not cx_native_bin and os.name == "nt":
        cx_native_bin = _find_windows_codex_desktop_bin()

    cx_native_dir = codex_root.is_dir()
    cx_native = bool(cx_native_bin or cx_native_dir)

    # 3. WSL -> Windows 宿主互操作检测
    win_profile_in_wsl = _probe_wsl_windows_profile_dir()
    ag_interop = bool(ag_interop_bin)
    cx_interop = bool(cx_interop_bin)
    if win_profile_in_wsl is not None:
        if (win_profile_in_wsl / ".gemini").is_dir():
            ag_interop = True
        if (win_profile_in_wsl / ".codex").is_dir():
            cx_interop = True

    if ag_native and cx_native:
        recommended = "both"
    elif ag_native:
        recommended = "antigravity"
    elif cx_native:
        recommended = "codex"
    else:
        recommended = "none"

    zotero_exe = detect_zotero_mcp_executable()

    return {
        "platform": sys.platform,
        "os_name": os.name,
        "is_wsl": win_profile_in_wsl is not None,
        "antigravity": {
            "native_installed": ag_native,
            "native_binary": ag_native_bin,
            "native_config_dir_exists": ag_native_dir,
            "interop_available": ag_interop,
            "target_plugin_dir": str(gemini_root / "config" / "plugins" / "user-guard"),
        },
        "codex": {
            "native_installed": cx_native,
            "native_binary": cx_native_bin,
            "native_config_dir_exists": cx_native_dir,
            "interop_available": cx_interop,
            "target_codex_home": str(codex_root),
        },
        "zotero_mcp_detected": zotero_exe,
        "recommended_app_mode": recommended,
        "single_app_only_on_host": (
            (ag_native and not cx_native and not cx_interop)
            or (cx_native and not ag_native and not ag_interop)
        ),
    }


def _is_working_python_cmd(cmd_tokens: List[str]) -> bool:
    """验证候选 Python 命令真实可执行且版本 >= 3.9（排除 WindowsApps 空存根）"""
    if not cmd_tokens:
        return False
    exe_candidate = cmd_tokens[0].strip("\"'")
    exe_path = exe_candidate if Path(exe_candidate).is_file() else shutil.which(exe_candidate)
    if not exe_path:
        return False
    try:
        proc = subprocess.run(
            [exe_path] + cmd_tokens[1:] + ["-I", "-B", "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def detect_hook_python_command() -> str:
    """探测宿主机 shell (cmd /c 或 sh -c) 可直接调用的跨平台 Python 命令前缀"""
    is_win = os.name == "nt"
    flags = "-X utf8 -I -B"

    if is_win:
        for tokens, prefix in (
            (["python.exe"], f"python.exe {flags}"),
            (["python"], f"python {flags}"),
            (["py", "-3"], f"py -3 {flags}"),
        ):
            if _is_working_python_cmd(tokens):
                return prefix
    else:
        for tokens, prefix in (
            (["python3"], f"python3 {flags}"),
            (["python"], f"python {flags}"),
        ):
            if _is_working_python_cmd(tokens):
                return prefix

    # 回退至当前运行安装器的 Python 绝对路径（带双引号兼容空格路径）
    return f'"{sys.executable}" {flags}'


def detect_zotero_mcp_executable(explicit_cmd: str = "") -> Optional[str]:
    """动态探测本机是否存在可用的 zotero-mcp 可执行文件（零个人路径硬编码）。"""
    if explicit_cmd:
        p = Path(os.path.expanduser(explicit_cmd))
        if p.exists() and p.is_file():
            return str(p.resolve())
        found = shutil.which(explicit_cmd)
        if found:
            return found
        return None

    in_path = shutil.which("zotero-mcp")
    if in_path and not _is_mnt_windows_path(in_path):
        return in_path

    home = Path.home()
    exe_names = ["zotero-mcp.exe", "zotero-mcp"] if os.name == "nt" else ["zotero-mcp"]

    # 1. 扫描 ~/.local/share/codex-tools/zotero-mcp-* 虚拟环境
    codex_tools_dir = home / ".local" / "share" / "codex-tools"
    if codex_tools_dir.exists() and codex_tools_dir.is_dir():
        venv_dirs = sorted(codex_tools_dir.glob("zotero-mcp*"), reverse=True)
        for vd in venv_dirs:
            for sub in ("Scripts", "bin"):
                for ename in exe_names:
                    cand = vd / ".venv" / sub / ename
                    if cand.exists() and cand.is_file():
                        return str(cand.resolve())

    # 2. 扫描常见 pipx / uv / user local bin 路径
    common_bins = [
        home / ".local" / "bin",
        home / "pipx" / "venvs" / "zotero-mcp" / ("Scripts" if os.name == "nt" else "bin"),
        home / ".local" / "pipx" / "venvs" / "zotero-mcp" / ("Scripts" if os.name == "nt" else "bin"),
    ]
    for bdir in common_bins:
        for ename in exe_names:
            cand = bdir / ename
            if cand.exists() and cand.is_file():
                return str(cand.resolve())

    # 3. 兼容从本机 ~/.codex/config.toml 中读取已验证存在的 zotero 路径
    codex_cfg = home / ".codex" / "config.toml"
    if codex_cfg.exists() and codex_cfg.is_file():
        try:
            cfg_text = codex_cfg.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"\[mcp_servers\.zotero\][\s\S]*?^\s*command\s*=\s*['\"]([^'\"]+)['\"]", cfg_text, re.MULTILINE)
            if m:
                cand = Path(m.group(1))
                if cand.exists() and cand.is_file():
                    return str(cand.resolve())
        except Exception:
            pass

    return None


def configure_user_zotero_mcp(config_root: Path, zotero_exe: str, dry_run: bool = False) -> None:
    """将探测到的 zotero-mcp 无损合并至反重力用户级 ~/.gemini/config/mcp_config.json"""
    mcp_cfg_path = config_root / "mcp_config.json"
    mcp_data: Dict[str, Any] = {"mcpServers": {}}

    if mcp_cfg_path.exists():
        try:
            raw = mcp_cfg_path.read_text(encoding="utf-8").strip()
            if raw:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    mcp_data = loaded
        except Exception:
            pass

    servers = mcp_data.setdefault("mcpServers", {})
    existing = servers.get("zotero")

    if isinstance(existing, dict):
        old_cmd = str(existing.get("command") or "").strip()
        if old_cmd and (Path(old_cmd).exists() or shutil.which(old_cmd)):
            print(f"[*] 反重力侧 Zotero MCP 已就绪，保持现有有效配置: {old_cmd}")
            return

    if dry_run:
        print(f"[Dry-Run] 将在 {mcp_cfg_path} 中配置 zotero MCP -> {zotero_exe}")
        return

    servers["zotero"] = {
        "command": zotero_exe,
        "args": [],
        "env": {
            "ZOTERO_LOCAL": "true",
            "PYTHON_DOTENV_DISABLED": "1",
            "NO_PROXY": "localhost,127.0.0.1",
        },
    }
    mcp_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_cfg_path.write_text(json.dumps(mcp_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[+] 已在反重力用户级 {mcp_cfg_path} 中配置有效 Zotero MCP: {zotero_exe}")


def _toml_basic_or_literal_str(val: str) -> str:
    """优先使用单引号 TOML 字面量字符串，避免 Windows 反斜杠转义问题"""
    if "'" not in val and "\n" not in val and "\r" not in val:
        return f"'{val}'"
    escaped = val.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def configure_codex_zotero_mcp(codex_root: Path, zotero_exe: str, dry_run: bool = False) -> None:
    """将探测到的 zotero-mcp 无损合并至 Codex 用户级 <codex_root>/config.toml"""
    cfg_path = codex_root / "config.toml"
    raw_text = ""
    if cfg_path.is_file():
        try:
            raw_text = cfg_path.read_text(encoding="utf-8")
        except Exception:
            return

    m_existing = re.search(
        r"^\[mcp_servers\.zotero\]\s*$([\s\S]*?)(?=^\[|\Z)",
        raw_text,
        re.MULTILINE,
    )
    if m_existing:
        sec_body = m_existing.group(1)
        m_cmd = re.search(r"^\s*command\s*=\s*(['\"])(.*?)\1", sec_body, re.MULTILINE)
        if m_cmd:
            old_cmd = m_cmd.group(2).strip()
            if old_cmd and (Path(old_cmd).exists() or shutil.which(old_cmd)):
                print(f"[*] Codex 侧 Zotero MCP 已就绪，保持现有有效配置: {old_cmd}")
                return

    if dry_run:
        print(f"[Dry-Run] 将在 {cfg_path} 中配置 [mcp_servers.zotero] -> {zotero_exe}")
        return

    cmd_literal = _toml_basic_or_literal_str(zotero_exe)
    zotero_block = (
        f"[mcp_servers.zotero]\n"
        f"command = {cmd_literal}\n\n"
        f"[mcp_servers.zotero.env]\n"
        f'ZOTERO_LOCAL = "true"\n'
        f'PYTHON_DOTENV_DISABLED = "1"\n'
        f'NO_PROXY = "localhost,127.0.0.1"\n'
    )

    if m_existing:
        # 仅更新失效的 command 行，保留其它自定义字段
        sec_start, sec_end = m_existing.span(1)
        sec_body = m_existing.group(1)
        if re.search(r"^\s*command\s*=", sec_body, re.MULTILINE):
            new_sec = re.sub(
                r"^\s*command\s*=\s*(['\"]).*?\1",
                lambda _: f"command = {cmd_literal}",
                sec_body,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            new_sec = f"\ncommand = {cmd_literal}" + sec_body
        updated = raw_text[:sec_start] + new_sec + raw_text[sec_end:]
    else:
        sep = "\n\n" if raw_text.strip() else ""
        updated = raw_text.rstrip() + sep + zotero_block

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(updated.rstrip() + "\n", encoding="utf-8")
    print(f"[+] 已在 Codex 用户级 {cfg_path} 中配置有效 Zotero MCP: {zotero_exe}")


def _parse_agy_model_lines(output: str) -> List[Dict[str, str]]:
    parsed: List[Dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "\t" not in line:
            continue
        slug, display_name = line.split("\t", 1)
        slug, display_name = slug.strip(), display_name.strip()
        if slug and display_name:
            parsed.append({"slug": slug, "display_name": display_name})
    return parsed


def _load_wsl_windows_agy_cache() -> List[Dict[str, str]]:
    win_profile = _probe_wsl_windows_profile_dir()
    if win_profile is None:
        return []
    try:
        cache_path = win_profile / ".gemini" / "antigravity-cli" / "cache" / "cae_models_cache.json"
        if cache_path.is_file():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            items = data.get("models", []) if isinstance(data, dict) else []
            return [m for m in items if isinstance(m, dict) and m.get("slug") and m.get("display_name")]
    except Exception:
        pass
    return []


def _select_agy_frontier_max_label(models: List[Dict[str, str]]) -> Optional[str]:
    skip_tokens = {"lite", "mini", "fast", "flash", "hydrogen", "orchestrator", "router"}
    tiered: List[Tuple[str, str, str]] = []
    for item in models:
        m = re.match(r"^(.+)-(minimal|low|medium|high|xhigh|max|ultra)$", item["slug"].lower())
        if m and m.group(2) != "ultra":
            tiered.append((m.group(1), m.group(2), item["display_name"]))
    if not tiered:
        return next(
            (item["display_name"] for item in models if not item["slug"].lower().endswith("-ultra")),
            None,
        )
    primary_family = next(
        (fam for fam, _, _ in tiered if not (set(re.split(r"[^a-z0-9]+", fam)) & skip_tokens)),
        tiered[0][0],
    )
    variants = {tier: label for fam, tier, label in tiered if fam == primary_family}
    best_tier = max(variants, key=EFFORT_ORDER.index)
    return variants[best_tier]


def _sync_codex_frontier_max(
    codex_root: Path, dry_run: bool = False, *, overwrite: bool = False,
) -> None:
    cfg_path = codex_root / "config.toml"
    cache_path = codex_root / "models_cache.json"
    try:
        raw_text = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
        parsed = tomllib.loads(raw_text) if tomllib is not None else None
    except (OSError, ValueError) as exc:
        print(f"[!] 无法读取 {cfg_path} 的模型设置，已保留原文件: {exc}", file=sys.stderr)
        return

    raw_lines = raw_text.splitlines()
    split_idx = next((i for i, line in enumerate(raw_lines) if line.lstrip().startswith("[")), len(raw_lines))
    header, body = raw_lines[:split_idx], raw_lines[split_idx:]
    values: Dict[str, str] = {}
    lines: Dict[str, Tuple[int, str, str]] = {}
    keys = ("model", "model_reasoning_effort")
    for key in keys:
        assignments = []
        for idx, line in enumerate(header):
            match = re.match(rf"^(\s*(?:{key}|'{key}'|\"{key}\")\s*=\s*)(.*)$", line)
            if match:
                assignments.append((idx, match))
        if len(assignments) > 1:
            print(f"[!] {cfg_path} 的 {key} 存在多个顶层赋值，已保留原文件。", file=sys.stderr)
            return
        if assignments:
            idx, assignment = assignments[0]
            literal = re.fullmatch(r'''("(?:[^"\\]|\\.)*"|'[^']*')(\s*(?:#.*)?)''', assignment[2])
            if literal:
                lines[key] = (idx, assignment[1], literal[2])
                if parsed is None:
                    values[key] = literal[1][1:-1]
        if parsed is not None and key in parsed:
            if not isinstance(parsed[key], str):
                print(f"[!] {cfg_path} 的 {key} 不是字符串，已保留原文件。", file=sys.stderr)
                return
            values[key] = parsed[key]
        elif assignments and key not in values:
            print(f"[!] 无法读取 {cfg_path} 的 {key}，已保留原文件。", file=sys.stderr)
            return

    cur_model = values.get("model", "").strip()
    cur_effort = values.get("model_reasoning_effort", "").strip()
    if cur_model and cur_effort and not overwrite:
        print("[*] 保留 Codex 已配置的默认模型和推理挡位。")
        return

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8-sig"))
        models = [m for m in cache["models"] if isinstance(m, dict) and isinstance(m.get("slug"), str) and m["slug"]]
    except (OSError, ValueError, KeyError, TypeError):
        print(f"[*] 未读取到有效的 Codex 模型目录 {cache_path}，默认模型设置保持原样。")
        return

    def supported_levels(model: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_levels = model.get("supported_reasoning_levels")
        if not isinstance(raw_levels, list):
            return []
        return [level for level in raw_levels if isinstance(level, dict) and level.get("effort") in EFFORT_ORDER]

    if cur_model and not overwrite:
        selected = next((m for m in models if m["slug"] == cur_model), None)
    else:
        visible = [m for m in models if m.get("visibility") != "hide"] or models
        if cur_effort and not overwrite:
            visible = [m for m in visible if any(level["effort"] == cur_effort for level in supported_levels(m))]
        selected = min(
            visible,
            key=lambda m: m["priority"] if type(m.get("priority")) is int else 9999,
            default=None,
        )
    if selected is None:
        print("[*] Codex 模型目录未找到与现有模型设置匹配的条目，已保留原配置。")
        return

    slug = selected["slug"]
    efforts = [
        level["effort"] for level in supported_levels(selected)
        if level["effort"] != "ultra" and "delegation" not in str(level.get("description") or "").lower()
    ]
    effort = cur_effort if cur_effort and not overwrite else max(efforts, key=EFFORT_ORDER.index, default="")
    if not effort:
        print(f"[*] Codex 模型 {slug} 缺少可用的非委派推理挡位元数据，未补齐挡位。")
        if overwrite and cur_effort:
            return

    updates = {key: val for key, val in zip(keys, (slug, effort)) if val and val != values.get(key, "").strip()}
    if not updates:
        return
    if any(key in values and key not in lines for key in updates):
        print(f"[!] {cfg_path} 含无法安全更新的模型赋值，已保留原文件。", file=sys.stderr)
        return
    description = ", ".join(f"{key}={val}" for key, val in updates.items())
    if dry_run:
        print(f"[Dry-Run] 将写入 {cfg_path} 的模型设置: {description}")
        return

    missing_lines = []
    for key, val in updates.items():
        quoted = json.dumps(val, ensure_ascii=False)
        if key in lines:
            idx, prefix, suffix = lines[key]
            header[idx] = prefix + quoted + suffix
        else:
            missing_lines.append(f"{key} = {quoted}")
    updated = "\n".join(missing_lines + header + body) + "\n"
    if tomllib is not None:
        try:
            tomllib.loads(updated)
        except ValueError as exc:
            print(f"[!] Codex 模型设置合并后无法解析，已保留 {cfg_path}: {exc}", file=sys.stderr)
            return
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(updated, encoding="utf-8")
    print(f"[+] 已写入 Codex 默认模型设置: {description}")


def _sync_codex_workspace_boundary(codex_root: Path, dry_run: bool = False) -> None:
    """将 Codex 默认沙箱收紧为项目目录内写入 (workspace-write) + 越界强制审批提示 (on-request)，保留出站网络与原有表结构。"""
    cfg_path = codex_root / "config.toml"
    if not cfg_path.is_file():
        return
    try:
        raw_lines = cfg_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return

    split_idx = next((i for i, line in enumerate(raw_lines) if line.lstrip().startswith("[")), len(raw_lines))
    header, body = raw_lines[:split_idx], raw_lines[split_idx:]

    sbx_val = app_val = None
    sbx_idx = app_idx = None
    for idx, line in enumerate(header):
        m_sbx = re.match(r'^\s*sandbox_mode\s*=\s*["\']([^"\']+)["\']', line)
        if m_sbx:
            sbx_val, sbx_idx = m_sbx.group(1), idx
        m_app = re.match(r'^\s*approval_policy\s*=\s*["\']([^"\']+)["\']', line)
        if m_app:
            app_val, app_idx = m_app.group(1), idx

    target_sbx = "workspace-write" if sbx_val in (None, "danger-full-access") else sbx_val
    target_app = "on-request" if app_val is None else app_val

    has_sww_table = False
    sww_has_net = False
    sww_header_idx = None
    in_sww = False
    for idx, line in enumerate(body):
        stripped = line.strip()
        if stripped.startswith("["):
            in_sww = stripped == "[sandbox_workspace_write]"
            if in_sww:
                has_sww_table = True
                sww_header_idx = idx
            continue
        if in_sww and re.match(r"^\s*network_access\s*=", line):
            sww_has_net = True

    needs_header_update = (sbx_val != target_sbx) or (app_val != target_app)
    needs_net_update = (target_sbx == "workspace-write") and (not has_sww_table or not sww_has_net)

    if not needs_header_update and not needs_net_update:
        print(f"[*] Codex 项目目录边界硬约束已就绪: sandbox_mode={sbx_val}, approval_policy={app_val}")
        return

    if dry_run:
        print(
            f"[Dry-Run] 将收紧 {cfg_path} 项目目录边界硬约束 -> "
            f"sandbox_mode={target_sbx}, approval_policy={target_app}, network_access=true"
        )
        return

    if sbx_idx is not None:
        header[sbx_idx] = f'sandbox_mode = "{target_sbx}"'
    else:
        header.insert(0, f'sandbox_mode = "{target_sbx}"')
        sbx_idx = 0
        if app_idx is not None:
            app_idx += 1

    if app_idx is not None:
        header[app_idx] = f'approval_policy = "{target_app}"'
    else:
        header.insert(sbx_idx + 1, f'approval_policy = "{target_app}"')

    if needs_net_update:
        if has_sww_table and sww_header_idx is not None:
            body.insert(sww_header_idx + 1, "network_access = true")
        else:
            lead = [] if (not header or not header[-1].strip()) else [""]
            trail = [""] if (body and body[0].strip()) else []
            body = lead + ["[sandbox_workspace_write]", "network_access = true"] + trail + body

    cfg_path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    print(f"[+] 已收紧 Codex 项目目录边界硬约束: sandbox_mode={target_sbx}, approval_policy={target_app}")


def _ensure_toml_section_defaults(lines: List[str], section_header: str, defaults: List[Tuple[str, str]]) -> Tuple[List[str], bool]:
    """在 TOML 行列表中无损补齐指定表的缺失默认键（已有键一律保留原值不覆盖）"""
    sec_idx: Optional[int] = None
    sec_end = len(lines)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            if sec_idx is not None:
                sec_end = idx
                break
            if stripped == section_header:
                sec_idx = idx

    if sec_idx is None:
        addition = [""] if (lines and lines[-1].strip()) else []
        addition.append(section_header)
        for k, v in defaults:
            addition.append(f"{k} = {v}")
        return lines + addition, True

    existing_keys: Set[str] = set()
    for line in lines[sec_idx + 1 : sec_end]:
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        if m:
            existing_keys.add(m.group(1))

    missing_lines = [f"{k} = {v}" for k, v in defaults if k not in existing_keys]
    if not missing_lines:
        return lines, False

    insert_pos = sec_end
    while insert_pos > sec_idx + 1 and not lines[insert_pos - 1].strip():
        insert_pos -= 1
    new_lines = lines[:insert_pos] + missing_lines + lines[insert_pos:]
    return new_lines, True


def _sync_codex_standalone_baseline(codex_root: Path, include_sandbox: bool = True, dry_run: bool = False) -> None:
    """
    确保 Codex 独立工作流所需的 config.toml 安全与防污染基线完备（新机自动初始化，旧机无损补缺、零覆盖个性化设置）。
    """
    cfg_path = codex_root / "config.toml"
    created_new = not cfg_path.is_file()
    raw_lines: List[str] = []
    if not created_new:
        try:
            raw_lines = cfg_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return

    split_idx = next((i for i, line in enumerate(raw_lines) if line.lstrip().startswith("[")), len(raw_lines))
    header, body = raw_lines[:split_idx], raw_lines[split_idx:]
    changed = False

    has_summary = any(re.match(r"^\s*model_reasoning_summary\s*=", ln) for ln in header)
    if not has_summary:
        insert_pos = len(header)
        while insert_pos > 0 and not header[insert_pos - 1].strip():
            insert_pos -= 1
        header.insert(insert_pos, 'model_reasoning_summary = "auto"')
        changed = True

    lines = header + ([""] if (header and body and header[-1].strip()) else []) + body
    for sec_hdr, sec_defaults in (
        ("[features]", [("memories", "false"), ("chronicle", "false")]),
        (
            "[memories]",
            [
                ("generate_memories", "false"),
                ("use_memories", "false"),
                ("disable_on_external_context", "true"),
            ],
        ),
        ("[desktop.external-agent-import-sync-item-types]", [("HOOKS", "false")]),
    ):
        lines, sec_changed = _ensure_toml_section_defaults(lines, sec_hdr, sec_defaults)
        changed = changed or sec_changed

    if created_new or changed:
        if dry_run:
            action = "初始化创建" if created_new else "无损补齐缺失基线键"
            print(f"[Dry-Run] 将对 {cfg_path} 执行 Codex 独立工作流基线{action}")
        else:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            print(f"[+] 已对齐 Codex 独立工作流配置基线: {cfg_path}")
    else:
        print("[*] Codex 独立工作流 config.toml 基线已完备。")

    if include_sandbox:
        if created_new and dry_run:
            print(f"[Dry-Run] 将配置 {cfg_path} 项目目录边界硬约束 -> sandbox_mode=workspace-write, approval_policy=on-request")
        else:
            _sync_codex_workspace_boundary(codex_root, dry_run=dry_run)


def _sync_codex_global_rules(template_rules_path: Path, codex_root: Path, dry_run: bool = False) -> None:
    """将模板 R0-R9 全局核心总纲无损同步至 <codex_root>/AGENTS.md，完整保留用户自定义的『## 工具与...』本机硬件与环境说明段。"""
    if not template_rules_path.is_file():
        return
    try:
        template_text = template_rules_path.read_text(encoding="utf-8")
    except Exception:
        return

    m_tpl_env = re.search(r"\n##\s+工具与[^\r\n]*[\s\S]*$", template_text)
    template_core = template_text[: m_tpl_env.start()].rstrip() if m_tpl_env else template_text.rstrip()

    codex_agents = codex_root / "AGENTS.md"
    existing = ""
    if codex_agents.is_file():
        try:
            existing = codex_agents.read_text(encoding="utf-8")
        except Exception:
            return

    m_usr_env = re.search(r"\n##\s+工具与[^\r\n]*[\s\S]*$", existing) if existing else None
    if m_usr_env:
        merged = template_core + "\n\n" + m_usr_env.group(0).strip() + "\n"
    else:
        merged = template_text.rstrip() + "\n"

    if existing == merged:
        print("[*] Codex 全局规则 AGENTS.md 已对齐最新总纲（保留本机环境段）。")
        return

    if dry_run:
        print(f"[Dry-Run] 将同步更新 {codex_agents} 全局核心总纲 R0-R9（保留本机环境段）")
        return

    codex_agents.parent.mkdir(parents=True, exist_ok=True)
    codex_agents.write_text(merged, encoding="utf-8")
    print(f"[+] 已同步更新 Codex 全局规则 {codex_agents}（保留本机环境段）")


def _sync_codex_agents(codex_template_dir: Path, codex_root: Path, dry_run: bool = False) -> None:
    """同步 Codex 只读子代理定义 (*.toml) 至 <codex_root>/agents/，保留用户其它自定义子代理。"""
    src_agents_dir = codex_template_dir / "agents"
    if not src_agents_dir.is_dir():
        return
    dst_agents_dir = codex_root / "agents"
    updated_names: List[str] = []
    for src_toml in sorted(src_agents_dir.glob("*.toml")):
        dst_toml = dst_agents_dir / src_toml.name
        src_bytes = src_toml.read_bytes()
        if dst_toml.is_file() and dst_toml.read_bytes() == src_bytes:
            continue
        updated_names.append(src_toml.name)
        if not dry_run:
            dst_agents_dir.mkdir(parents=True, exist_ok=True)
            dst_toml.write_bytes(src_bytes)

    if not updated_names:
        print("[*] Codex 只读子代理 (agents/*.toml) 已是最新状态。")
    elif dry_run:
        print(f"[Dry-Run] 将同步 Codex 只读子代理至 {dst_agents_dir}: {', '.join(updated_names)}")
    else:
        print(f"[+] 已同步 Codex 只读子代理至 {dst_agents_dir}: {', '.join(updated_names)}")


def _reject_duplicate_json_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 键重复: {key}")
        result[key] = value
    return result


def _codex_hook_tokens(handler: Dict[str, Any]) -> List[str]:
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        return []
    try:
        return [token.strip('"') for token in shlex.split(handler["command"], posix=(os.name != "nt"))]
    except ValueError:
        return []


def _codex_managed_hook(handler: Dict[str, Any], codex_root: Path) -> Tuple[Optional[str], List[str]]:
    tokens = _codex_hook_tokens(handler)
    for idx, token in enumerate(tokens[1:], start=1):
        script = Path(token).expanduser()
        if script.name not in {"session_context_loader.py", "handoff_gate.py"}:
            continue
        if not script.is_absolute() or script.resolve() != codex_root / "hooks" / script.name:
            continue
        args = tokens[idx + 1:]
        executable = Path(tokens[0]).stem.lower()
        if not re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*w?)?|py)", executable) and Path(tokens[0]) != Path(sys.executable):
            continue
        if script.name == "session_context_loader.py" and args[:2] == ["--client", "codex"]:
            return "loader", tokens[:idx]
        if (
            script.name == "handoff_gate.py" and len(args) == 8
            and tokens[idx - 4:idx] == ["-X", "utf8", "-I", "-B"]
            and (idx == 5 or (idx == 6 and executable == "py" and tokens[1] == "-3"))
            and args[:3] == ["--client", "codex", "--mode"] and args[3] in {"advisory", "enforce"}
            and args[4] == "--compact-limit" and args[5].isdigit() and args[6] == "--state-dir"
        ):
            state_dir = Path(args[7]).expanduser()
            if state_dir.is_absolute() and state_dir.resolve() == codex_root / "hooks" / "state" / "handoff":
                return "gate", tokens[:idx]
    return None, []


def _read_codex_hooks(path: Path, raw: Optional[bytes]) -> Dict[str, Any]:
    if raw is None:
        return {"hooks": {}}
    try:
        doc = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_json_keys)
        if not isinstance(doc, dict) or ("hooks" in doc and not isinstance(doc["hooks"], dict)):
            raise ValueError("顶层和 hooks 字段必须是对象")
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法安全合并 {path}，原文件已保留: {exc}") from exc
    return doc


def _plan_codex_hooks(
    hooks_path: Path, template_path: Path, py_cmd: str, mode: str, compact_limit: int,
) -> Optional[Tuple[Optional[bytes], Dict[str, Any]]]:
    if _is_link_or_junction(hooks_path):
        raise ValueError(f"Codex 钩子注册表是链接，请先核对: {hooks_path}")
    codex_root = hooks_path.parent.resolve()
    try:
        original = hooks_path.read_bytes()
    except FileNotFoundError:
        original = None
    doc = _read_codex_hooks(hooks_path, original)
    defaults = _read_codex_hooks(template_path, template_path.read_bytes())["hooks"]
    events = doc.setdefault("hooks", {})
    codex_path = codex_root.as_posix()

    changed = False
    for event, groups in defaults.items():
        matcher = groups[0].get("matcher")
        desired = {}
        for handler in groups[0]["hooks"]:
            kind = "gate" if "handoff_gate.py" in handler["command"] else "loader"
            if kind == "gate" and mode == "off":
                continue
            command = handler["command"].replace("--mode advisory --compact-limit 0", f"--mode {mode} --compact-limit {compact_limit}")
            script_start = command.index('"~/.codex/')
            quote_path = (lambda path: '"' + path + '"') if os.name == "nt" else shlex.quote
            script_command = re.sub(
                r'"~/.codex/([^"\n]+)"',
                lambda match: quote_path(codex_path + '/' + match.group(1)),
                command[script_start:],
            )
            command = f'{py_cmd} {script_command}'
            desired[kind] = {**handler, "command": command}

        if not desired and event not in events:
            continue
        existing = events.get(event, [])
        if not isinstance(existing, list) or any(
            not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
            or any(not isinstance(handler, dict) for handler in group["hooks"])
            for group in existing
        ):
            raise ValueError(f"无法安全合并 {hooks_path} 的 {event}，原文件已保留。事件须包含钩子组列表及各组的 handler 对象列表。")

        retained = set()
        first_matching_group = None
        merged = []
        for group in existing:
            matching = group.get("matcher") == matcher
            handlers = []
            for handler in group["hooks"]:
                kind, python = _codex_managed_hook(handler, codex_root)
                if kind is None or (kind == "loader" and event != "SessionStart"):
                    handlers.append(handler)
                    continue
                if matching and kind in desired and kind not in retained:
                    updated_handler = handler
                    if kind == "gate":
                        updated_handler = {**handler, "command": desired[kind]["command"]}
                    elif handler["command"] != desired[kind]["command"]:
                        if not _is_working_python_cmd(python):
                            updated_handler = {**handler, "command": desired[kind]["command"]}
                    handlers.append(updated_handler)
                    retained.add(kind)
            if handlers or not group["hooks"]:
                updated_group = {**group, "hooks": handlers}
                if matching and first_matching_group is None:
                    first_matching_group = updated_group
                merged.append(updated_group)

        missing = [handler for kind, handler in desired.items() if kind not in retained]
        if missing:
            if first_matching_group is None:
                first_matching_group = {} if matcher is None else {"matcher": matcher}
                first_matching_group["hooks"] = []
                merged.append(first_matching_group)
            first_matching_group["hooks"].extend(missing)
        if merged != existing:
            if merged:
                events[event] = merged
            else:
                events.pop(event, None)
            changed = True
    return (original, doc) if changed else None


def _sync_codex_hooks(
    user_guard_hooks_dir: Path,
    codex_root: Path,
    plan: Optional[Tuple[Optional[bytes], Dict[str, Any]]],
    dry_run: bool = False,
    *,
    mode: str = "advisory",
    compact_limit: int = 0,
) -> None:
    """同步 Codex 原话恢复和长会话交接脚本，逐条合并已登记的 handler。"""
    hooks_path = codex_root / "hooks.json"
    dst_hooks_dir = codex_root / "hooks"
    hook_scripts = ["session_context_loader.py", "user_words.py"]
    if mode != "off":
        hook_scripts.append("handoff_gate.py")
    for target in (dst_hooks_dir, *(dst_hooks_dir / name for name in hook_scripts)):
        if _is_link_or_junction(target):
            raise ValueError(f"Codex 钩子脚本或目录是链接，请先核对: {target}")
    scripts_updated: List[str] = []

    for name in hook_scripts:
        src_bytes = (user_guard_hooks_dir / name).read_bytes()
        dst_file = dst_hooks_dir / name
        if dst_file.is_file() and dst_file.read_bytes() == src_bytes:
            continue
        scripts_updated.append(name)
        if not dry_run:
            dst_hooks_dir.mkdir(parents=True, exist_ok=True)
            dst_file.write_bytes(src_bytes)

    pycache_dir = dst_hooks_dir / "__pycache__"
    if pycache_dir.is_dir() and not _is_link_or_junction(pycache_dir):
        if dry_run:
            print(f"  [Dry-Run CLEAN] 将清理 Codex 钩子字节码缓存: {pycache_dir}")
        else:
            shutil.rmtree(pycache_dir, ignore_errors=True)

    if scripts_updated:
        action = "将同步" if dry_run else "已同步"
        prefix = "[Dry-Run]" if dry_run else "[+]"
        print(f"{prefix} {action} Codex 钩子脚本至 {dst_hooks_dir}: {', '.join(scripts_updated)}")

    if plan is None:
        print("[*] Codex 钩子注册表已是最新状态，保留现有文件字节。")
    elif dry_run:
        print(f"[Dry-Run] 将合并 Codex 钩子注册表 {hooks_path}（交接模式 {mode}，压缩次数阈值 {compact_limit}）")
    else:
        original, hooks_doc = plan
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(hooks_doc, indent=2, ensure_ascii=False) + "\n"
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=hooks_path.parent, mode="w", encoding="utf-8", delete=False) as temp:
                temp_path = Path(temp.name)
                temp.write(content)
            current = hooks_path.read_bytes() if hooks_path.exists() else None
            if _is_link_or_junction(hooks_path) or current != original:
                raise ValueError(f"{hooks_path} 在安装期间发生变更，请重新运行安装器。")
            if current is not None:
                temp_path.chmod(stat.S_IMODE(hooks_path.stat().st_mode))
            temp_path.replace(hooks_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        print(f"[+] 已合并 Codex 钩子注册表 {hooks_path}（交接模式 {mode}，压缩次数阈值 {compact_limit}）")


def configure_agy_frontier_max_default(
    gemini_root: Path, dry_run: bool = False, *, overwrite: bool = False,
) -> None:
    """使用反重力模型目录补齐 CLI 默认模型，或按显式选项覆盖。"""
    cli_dir = gemini_root / "antigravity-cli"
    settings_path = cli_dir / "settings.json"
    cache_path = cli_dir / "cache" / "cae_models_cache.json"
    settings_data: Dict[str, Any] = {}
    if settings_path.is_file():
        try:
            settings_data = json.loads(settings_path.read_text(encoding="utf-8-sig"), object_pairs_hook=_reject_duplicate_json_keys)
            if not isinstance(settings_data, dict):
                raise ValueError("顶层必须是对象")
        except (OSError, ValueError) as exc:
            print(f"[!] 无法读取 {settings_path} 的模型设置，已保留原文件: {exc}", file=sys.stderr)
            return
    current_model = settings_data.get("model")
    if not overwrite and not (isinstance(current_model, str) and current_model.strip()):
        desktop_config = gemini_root / "config" / "config.json"
        if desktop_config.is_file():
            try:
                desktop_data = json.loads(desktop_config.read_text(encoding="utf-8-sig"))
                desktop_settings = desktop_data.get("userSettings", {}) if isinstance(desktop_data, dict) else {}
                current_model = desktop_settings.get("model") if isinstance(desktop_settings, dict) else None
            except (OSError, ValueError):
                print(f"[!] 无法读取 {desktop_config} 的模型设置，已保留原配置。", file=sys.stderr)
                return
    keep_model = isinstance(current_model, str) and current_model.strip() and not overwrite

    agy_exe_name = "agy.exe" if os.name == "nt" else "agy"
    raw_which = shutil.which("agy")
    agy_bin = (
        str(Path.home() / ".gemini" / "bin" / agy_exe_name)
        if (Path.home() / ".gemini" / "bin" / agy_exe_name).is_file()
        else (raw_which if raw_which and not _is_mnt_windows_path(raw_which) else None)
    )
    models: List[Dict[str, str]] = []
    if agy_bin:
        try:
            proc = subprocess.run(
                [agy_bin, "models"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=6,
            )
            if proc.returncode == 0:
                models = _parse_agy_model_lines(proc.stdout)
        except Exception:
            pass
    if not models:
        models = _load_wsl_windows_agy_cache()
    if models and not dry_run:
        cache_content = json.dumps({"models": models}, indent=2, ensure_ascii=False) + "\n"
        if not cache_path.is_file() or cache_path.read_bytes() != cache_content.encode("utf-8"):
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(cache_content.encode("utf-8"))

    if keep_model:
        print("[*] 保留 Antigravity CLI 已配置的默认模型。")
        return
    frontier_label = _select_agy_frontier_max_label(models)
    if not frontier_label:
        print("[*] 未取得 Antigravity CLI 默认模型的可用元数据，已保留原配置。")
        return
    if current_model == frontier_label:
        print(f"[*] Antigravity CLI 的默认模型已是 {frontier_label}。")
    elif dry_run:
        print(f"[Dry-Run] 将写入 {settings_path} 的默认模型: {frontier_label}")
    else:
        settings_data["model"] = frontier_label
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[+] 已写入 Antigravity CLI 的默认模型: {frontier_label}")


def configure_frontier_max_defaults(gemini_root: Path, dry_run: bool = False, *, overwrite: bool = False) -> None:
    """兼容入口，同时处理反重力和 Codex 默认模型设置。"""
    configure_agy_frontier_max_default(gemini_root, dry_run=dry_run, overwrite=overwrite)
    configured_codex = os.environ.get("CODEX_HOME", "").strip()
    codex_root = Path(configured_codex).expanduser() if configured_codex else Path.home() / ".codex"
    _sync_codex_frontier_max(codex_root, dry_run=dry_run, overwrite=overwrite)


def rewrite_hooks_json_commands(hooks_json_path: Path, py_cmd: str) -> None:
    """将反重力插件 hooks.json 中的命令前缀自适应替换为目标平台 Python 命令"""
    hooks_data = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    lifecycle = hooks_data.get("user-guard-lifecycle", {})

    if "SessionStart" in lifecycle:
        for h in lifecycle["SessionStart"]:
            h["command"] = f"{py_cmd} ./hooks/session_context_loader.py --event SessionStart"

    if "PreInvocation" in lifecycle:
        for h in lifecycle["PreInvocation"]:
            h["command"] = f"{py_cmd} ./hooks/session_context_loader.py --event PreInvocation"

    if "PreToolUse" in lifecycle:
        for grp in lifecycle["PreToolUse"]:
            for h in grp.get("hooks", []):
                h["command"] = f"{py_cmd} ./hooks/status_guard.py"

    if "Stop" in lifecycle:
        for h in lifecycle["Stop"]:
            cmd = h.get("command", "")
            if "reply_repeat_guard.py" in cmd:
                h["command"] = f"{py_cmd} ./hooks/reply_repeat_guard.py"

    hooks_json_path.write_text(json.dumps(hooks_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def should_ignore_template_item(path: Path) -> bool:
    if any(part in IGNORED_NAMES for part in path.parts):
        return True
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return True
    return False


def mirror_sync_plugin(template_dir: Path, target_plugin_dir: Path, dry_run: bool = False) -> None:
    """
    将 template_dir 镜像同步至 target_plugin_dir，并精准清理陈旧废弃文件与 __pycache__。
    保留运行态历史归档（如 hooks/state/*_user_words.md 与 hooks/logs/lifecycle.log），但清除废弃 *_stop_gate.json。
    """
    expected_rel_files: Set[Path] = set()
    for item in sorted(template_dir.rglob("*")):
        rel = item.relative_to(template_dir)
        if should_ignore_template_item(rel):
            continue
        if item.is_file():
            expected_rel_files.add(rel)

    if dry_run:
        print(f"[Dry-Run] 将同步 {len(expected_rel_files)} 个核心文件至: {target_plugin_dir}")
        for rel in sorted(expected_rel_files):
            print(f"  [SYNC] {rel}")
    else:
        target_plugin_dir.mkdir(parents=True, exist_ok=True)
        for rel in sorted(expected_rel_files):
            src = template_dir / rel
            dst = target_plugin_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    if not target_plugin_dir.exists():
        return

    stale_files: List[Path] = []
    stale_dirs: List[Path] = []

    for existing in sorted(target_plugin_dir.rglob("*")):
        rel = existing.relative_to(target_plugin_dir)
        if existing.is_dir():
            if existing.name == "__pycache__":
                stale_dirs.append(existing)
            continue

        if "__pycache__" in rel.parts:
            continue

        if len(rel.parts) >= 2 and rel.parts[0] == "hooks" and rel.parts[1] in ("state", "logs"):
            if existing.name == ".gitkeep" or existing.name.endswith("_stop_gate.json"):
                stale_files.append(existing)
            continue

        if rel not in expected_rel_files:
            stale_files.append(existing)

    for sf in stale_files:
        rel = sf.relative_to(target_plugin_dir)
        if dry_run:
            print(f"  [Dry-Run CLEAN] 将清理过时文件: {rel}")
        else:
            sf.unlink(missing_ok=True)
            print(f"  [-] 已清理过时残留文件: {rel}")

    for sd in stale_dirs:
        rel = sd.relative_to(target_plugin_dir)
        if dry_run:
            print(f"  [Dry-Run CLEAN] 将清理缓存目录: {rel}")
        else:
            shutil.rmtree(sd, ignore_errors=True)
            print(f"  [-] 已清理字节码缓存目录: {rel}")


def codex_skill_target(skills_dir: str, plugin_target_explicit: bool, disabled: bool) -> Optional[Path]:
    """显式插件目标仅配合显式技能目标，避免隔离安装触及真实用户目录。"""
    if disabled:
        return None
    if skills_dir:
        return Path(skills_dir).expanduser().absolute() / "cae-harness"
    if plugin_target_explicit:
        return None
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    codex_root = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    if not codex_root.is_dir():
        return None
    return codex_root.absolute() / "skills" / "cae-harness"


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def validate_install_target(target: Path, repo_root: Path) -> Path:
    """在镜像同步前核实绝对目标，拒绝源树、用户根目录和危险链接。"""
    if _is_link_or_junction(target):
        raise ValueError(f"安装目标是链接或目录联接，请指定独立目标目录: {target}")
    resolved = target.resolve()
    lexical_target = Path(os.path.abspath(str(target)))
    repo_root = repo_root.resolve()
    user_home = Path.home()
    user_root = user_home.resolve()
    protected = {
        user_root,
        user_root / ".gemini",
        user_root / ".gemini" / "config",
        user_root / ".gemini" / "config" / "plugins",
        user_root / ".codex",
        user_root / ".codex" / "skills",
    }
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    production_targets = [
        user_home / ".gemini" / "config" / "plugins" / "user-guard",
        user_home / ".codex" / "skills" / "cae-harness",
    ]
    if configured_home:
        codex_home = Path(configured_home).expanduser()
        codex_root = codex_home.resolve()
        protected.update((codex_root, codex_root / "skills"))
        production_targets.append(codex_home / "skills" / "cae-harness")
    for production_target in production_targets:
        production_absolute = Path(os.path.abspath(str(production_target)))
        production_resolved = production_absolute.resolve()
        if resolved == production_resolved and lexical_target not in (production_absolute, production_resolved):
            raise ValueError(
                f"安装目标通过祖先链接指向用户生产目录，请直接指定真实目标: {production_resolved}"
            )
    if resolved in protected or resolved in user_root.parents:
        raise ValueError(f"安装目标不能是用户根目录或共享配置目录: {resolved}")
    if resolved == repo_root or resolved in repo_root.parents or repo_root in resolved.parents:
        raise ValueError(f"安装目标与源码仓库重叠: {resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"安装目标必须为目录: {resolved}")
    if resolved.exists():
        for entry in resolved.rglob("*"):
            if _is_link_or_junction(entry):
                raise ValueError(f"安装目标包含链接或目录联接，请先核对: {entry}")
    return resolved


def validate_codex_home_target(codex_home: Path, repo_root: Path) -> Path:
    """校验 Codex 主配置目录（支持真实 ~/.codex 或独立沙箱 --codex-home，拒绝源仓库重叠、用户主目录及链接重定向）"""
    if _is_link_or_junction(codex_home):
        raise ValueError(f"Codex 目标目录是链接或目录联接，请指定真实目录: {codex_home}")
    resolved = codex_home.resolve()
    lexical_target = Path(os.path.abspath(str(codex_home)))
    repo_resolved = repo_root.resolve()
    user_home = Path.home()
    user_root = user_home.resolve()

    prod_codex_abs = Path(os.path.abspath(str(user_home / ".codex")))
    prod_codex_res = prod_codex_abs.resolve()
    if resolved == prod_codex_res and lexical_target not in (prod_codex_abs, prod_codex_res):
        raise ValueError(f"Codex 目标目录通过祖先链接指向真实 ~/.codex，请直接指定真实路径: {prod_codex_res}")

    if resolved == user_root or resolved in user_root.parents:
        raise ValueError(f"Codex 目标目录不能是用户主目录或上级系统目录: {resolved}")
    if resolved in {user_root / ".gemini", user_root / ".gemini" / "config"}:
        raise ValueError(f"Codex 目标目录不能与反重力插件宿主目录混用: {resolved}")
    if resolved == repo_resolved or resolved in repo_resolved.parents or repo_resolved in resolved.parents:
        raise ValueError(f"Codex 目标目录与源码仓库重叠: {resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Codex 目标路径必须为目录: {resolved}")
    return resolved


def _confirm_single_app_install(
    installed_label: str,
    missing_label: str,
    interactive: bool,
    auto_yes: bool,
    dry_run: bool,
) -> bool:
    """当本机仅检测到单客户端时提示用户：是否先去安装另一端，或不安装直接单独配置当前客户端"""
    print("-" * 80)
    print(f"[!] 单客户端提示：检测到本机已安装 {installed_label}，未检测到 {missing_label}。")
    print(f"    - 若希望启用『反重力 <-> Codex』双端协作，可先退出安装 {missing_label} 后再运行本脚本；")
    print(f"    - 若暂不安装 {missing_label}，可直接单独配置 {installed_label} 的完整独立工作流。")
    if dry_run or auto_yes or not interactive:
        print(f"[*] 当前为非交互或自动确认模式，将仅针对已安装的 {installed_label} 生成/执行独立配置。")
        return True
    try:
        ans = input(f"是否不安装 {missing_label}，直接单独配置本机 {installed_label} 独立工作流？[Y/n]: ").strip().lower()
    except EOFError:
        return True
    if ans in ("n", "no", "0", "false"):
        print(f"[*] 已按选择取消本次配置。待安装 {missing_label} 后重新运行即可一键完成双端配置。")
        return False
    return True


def resolve_app_plan(
    app_arg: str,
    has_explicit_ag_target: bool,
    has_explicit_cx_target: bool,
    detection: Dict[str, Any],
    interactive: bool,
    auto_yes: bool,
    dry_run: bool,
) -> Tuple[bool, bool]:
    """返回 (install_antigravity, install_codex)"""
    if app_arg == "both":
        return True, True
    if app_arg == "antigravity":
        return True, False
    if app_arg == "codex":
        return False, True

    # app_arg == "auto"
    if has_explicit_ag_target or has_explicit_cx_target:
        return bool(has_explicit_ag_target), bool(has_explicit_cx_target)

    ag_info = detection["antigravity"]
    cx_info = detection["codex"]
    ag_native = bool(ag_info["native_installed"])
    cx_native = bool(cx_info["native_installed"])

    if ag_native and cx_native:
        return True, True

    if ag_native and not cx_native:
        if cx_info["interop_available"]:
            print(
                "[*] 检测到本侧已安装反重力，且跨子系统（Windows 宿主侧）已安装 Codex；"
                "本次将同步本侧反重力端配置（Windows 侧 Codex 配置可在 PowerShell 运行 install.ps1 更新）。"
            )
            return True, False
        if not _confirm_single_app_install("反重力 (Antigravity)", "Codex", interactive, auto_yes, dry_run):
            sys.exit(0)
        return True, False

    if cx_native and not ag_native:
        if ag_info["interop_available"]:
            print(
                "[*] 检测到本侧已安装 Codex，且跨子系统一侧已安装反重力；"
                "本次将同步本侧 Codex 配置（另一侧反重力端配置可在对应终端运行安装脚本更新）。"
            )
            return False, True
        if not _confirm_single_app_install("Codex", "反重力 (Antigravity)", interactive, auto_yes, dry_run):
            sys.exit(0)
        return False, True

    print(
        "[!] 未在本机检测到反重力（Antigravity）或 Codex 客户端目录与可执行文件。\n"
        "    请先安装至少一个客户端，或显式指定 --app codex / --app antigravity / --app both 预置配置目录。",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity & Codex 统一工作流与双向协作安装器")
    parser.add_argument(
        "--app",
        choices=("auto", "both", "antigravity", "codex"),
        default="auto",
        help="目标客户端模式：auto（默认自动检测，单端提示确认）、both（双端全配）、antigravity（仅配反重力）、codex（仅配 Codex）",
    )
    parser.add_argument("--probe-apps", action="store_true", help="纯只读探测本机反重力、Codex 与 Zotero 安装状态并输出 JSON")
    parser.add_argument("-y", "--yes", action="store_true", help="非交互自动确认（仅检测到单客户端时直接单独配置已安装的一端）")
    parser.add_argument("--target-dir", type=str, default="", help="自定义反重力插件安装目录（默认 ~/.gemini/config/plugins/user-guard）")
    parser.add_argument("--codex-home", type=str, default="", help="自定义 Codex 主配置目录（默认 $CODEX_HOME 或 ~/.codex，用于沙箱全量验证）")
    parser.add_argument("--codex-skills-dir", default="", help="仅自定义 Codex 技能根目录（其下安装 cae-harness，兼容隔离验证）")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，仅打印同步与配置计划不实际写入")
    parser.add_argument("--skip-test", action="store_true", help="跳过安装后的全套自动化回归测试")
    parser.add_argument("--no-zotero", action="store_true", help="跳过本机可选 Zotero MCP 自动探测与配置")
    parser.add_argument("--zotero-cmd", type=str, default="", help="显式指定本地 zotero-mcp 可执行文件路径")
    parser.add_argument("--no-codex-skill", action="store_true", help="跳过 Codex cae-harness 技能安装")
    parser.add_argument("--no-codex-rules", action="store_true", help="跳过 Codex 全局 AGENTS.md 核心总纲同步")
    parser.add_argument("--no-codex-agents", action="store_true", help="跳过 Codex 只读子代理 (agents/*.toml) 同步")
    parser.add_argument("--no-codex-hooks", action="store_true", help="跳过 Codex 钩子 (hooks.json + hooks/) 同步")
    parser.add_argument("--codex-handoff-mode", choices=("advisory", "enforce", "off"), default="advisory",
                        help="Codex 长会话交接模式：提示（默认）、门禁或关闭")
    parser.add_argument("--codex-handoff-compactions", type=int, default=0,
                        help="Codex 交接使用的压缩次数阈值；0 表示不按次数触发（默认）")
    model_options = parser.add_mutually_exclusive_group()
    model_options.add_argument("--no-model-config", action="store_true", help="跳过默认模型设置")
    model_options.add_argument("--sync-model-defaults", action="store_true", help="用本机模型目录覆盖已有默认模型与挡位")
    parser.add_argument("--no-sandbox-config", action="store_true", help="跳过 Codex 项目目录边界硬约束配置（workspace-write + on-request）")
    args = parser.parse_args()

    if args.codex_handoff_compactions < 0:
        parser.error("--codex-handoff-compactions 必须为非负整数")

    if args.probe_apps:
        print(json.dumps(detect_installed_apps(), indent=2, ensure_ascii=False))
        return

    if args.no_codex_skill and args.codex_skills_dir:
        parser.error("--no-codex-skill 与 --codex-skills-dir 不能同时指定")
    if args.codex_home and args.codex_skills_dir:
        parser.error("--codex-home 已包含 skills/cae-harness 管理，请勿同时指定 --codex-skills-dir")
    if args.app == "codex" and args.target_dir:
        parser.error("--app codex 模式下不应指定反重力 --target-dir")
    if args.app == "antigravity" and (args.codex_home or args.codex_skills_dir):
        parser.error("--app antigravity 模式下不应指定 --codex-home 或 --codex-skills-dir")

    repo_root = Path(__file__).resolve().parent
    template_dir = repo_root / "template" / "user-guard"
    codex_template_dir = repo_root / "template" / "codex"
    tests_dir = repo_root / "tests"

    if not template_dir.exists():
        print(f"[!] 错误：找不到反重力插件模板目录 {template_dir}", file=sys.stderr)
        sys.exit(1)

    detection = detect_installed_apps()
    has_explicit_ag = bool(args.target_dir)
    has_explicit_cx = bool(args.codex_home or args.codex_skills_dir)
    is_interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    install_ag, install_cx = resolve_app_plan(
        args.app,
        has_explicit_ag_target=has_explicit_ag,
        has_explicit_cx_target=has_explicit_cx,
        detection=detection,
        interactive=is_interactive,
        auto_yes=args.yes,
        dry_run=args.dry_run,
    )

    default_config_root = Path.home() / ".gemini" / "config"
    configured_codex_env = os.environ.get("CODEX_HOME", "").strip()
    default_codex_root = Path(configured_codex_env).expanduser() if configured_codex_env else (Path.home() / ".codex")

    target_plugin_dir: Optional[Path] = None
    config_root: Optional[Path] = None
    if install_ag:
        if args.target_dir:
            target_plugin_dir = Path(args.target_dir).expanduser().absolute()
        else:
            target_plugin_dir = default_config_root / "plugins" / "user-guard"

    codex_root: Optional[Path] = None
    full_codex_sync = False
    target_skill_dir: Optional[Path] = None

    if install_cx:
        if args.codex_skills_dir and not args.codex_home:
            # 兼容仅指定 --codex-skills-dir 的轻量技能隔离同步模式
            full_codex_sync = False
            target_skill_dir = codex_skill_target(args.codex_skills_dir, False, args.no_codex_skill)
        else:
            full_codex_sync = True
            codex_root = Path(args.codex_home).expanduser().absolute() if args.codex_home else default_codex_root.absolute()
            if not args.no_codex_skill:
                target_skill_dir = codex_root / "skills" / "cae-harness"

    try:
        if target_plugin_dir is not None:
            target_plugin_dir = validate_install_target(target_plugin_dir, repo_root)
            config_root = target_plugin_dir.parent.parent
        if codex_root is not None:
            codex_root = validate_codex_home_target(codex_root, repo_root)
            if not args.no_codex_skill:
                target_skill_dir = codex_root / "skills" / "cae-harness"
        if target_skill_dir is not None:
            target_skill_dir = validate_install_target(target_skill_dir, repo_root)
        if target_plugin_dir is not None and target_skill_dir is not None:
            if (
                target_skill_dir == target_plugin_dir
                or target_skill_dir in target_plugin_dir.parents
                or target_plugin_dir in target_skill_dir.parents
            ):
                raise ValueError("反重力插件与 Codex 技能安装目标不能相互包含")
        if target_plugin_dir is not None and codex_root is not None:
            if (
                codex_root == target_plugin_dir
                or codex_root in target_plugin_dir.parents
                or target_plugin_dir in codex_root.parents
            ):
                raise ValueError("反重力插件与 Codex 主配置目录不能相互包含")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    py_cmd = detect_hook_python_command()
    codex_hooks_plan = None
    if full_codex_sync and codex_root is not None and not args.no_codex_hooks:
        try:
            codex_hooks_plan = _plan_codex_hooks(
                codex_root / "hooks.json", codex_template_dir / "hooks.json", py_cmd,
                args.codex_handoff_mode, args.codex_handoff_compactions,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    mode_desc = (
        "双端完整配置（反重力 + Codex 独立工作流及双向协作）"
        if (install_ag and install_cx)
        else ("单反重力独立工作流配置" if install_ag else "单 Codex 独立工作流配置")
    )

    print("=" * 80)
    print("  Antigravity & Codex 统一工作流与协作安装程序")
    print("=" * 80)
    print(f"[*] 仓库根目录   : {repo_root}")
    print(f"[*] 部署目标模式 : {mode_desc}")
    print(f"[*] 钩子运行命令 : {py_cmd}")
    if target_plugin_dir is not None:
        print(f"[*] 反重力插件路径: {target_plugin_dir}")
    if codex_root is not None:
        print(f"[*] Codex 主目录 : {codex_root}")
    if target_skill_dir is not None:
        print(f"[*] Codex 技能路径: {target_skill_dir}")
    print("-" * 80)

    is_sandbox_run = bool(args.target_dir or args.codex_home or args.codex_skills_dir)
    zotero_exe: Optional[str] = None
    if not args.no_zotero and (not is_sandbox_run or args.zotero_cmd):
        if args.target_dir and config_root is not None and config_root.resolve() == default_config_root.resolve():
            is_default_plugin = target_plugin_dir == (default_config_root / "plugins" / "user-guard").resolve()
            if not is_default_plugin:
                parser.error("自定义 --target-dir 的父级目录指向真实 ~/.gemini/config，请使用独立沙箱目录或指定 --no-zotero")
        zotero_exe = detect_zotero_mcp_executable(args.zotero_cmd)
        if not zotero_exe:
            print("[*] 未在本机检测到 zotero-mcp 可执行文件，已跳过可选 Zotero MCP 配置（不影响工作流功能）。")

    # 1. 部署反重力侧
    if install_ag and target_plugin_dir is not None and config_root is not None:
        mirror_sync_plugin(template_dir, target_plugin_dir, dry_run=args.dry_run)
        hooks_json_path = target_plugin_dir / "hooks.json"
        if not args.dry_run and hooks_json_path.exists():
            rewrite_hooks_json_commands(hooks_json_path, py_cmd)
            print(f"[+] 反重力 hooks.json 已自适应写入平台命令前缀: {py_cmd}")
        if zotero_exe:
            configure_user_zotero_mcp(config_root, zotero_exe, dry_run=args.dry_run)
        is_default_plugin_target = target_plugin_dir == (default_config_root / "plugins" / "user-guard").resolve()
        if is_default_plugin_target and not args.no_model_config:
            configure_agy_frontier_max_default(config_root.parent, dry_run=args.dry_run, overwrite=args.sync_model_defaults)

    # 2. 部署 Codex 侧
    if install_cx:
        if target_skill_dir is not None:
            mirror_sync_plugin(template_dir / "skills" / "cae-harness", target_skill_dir, dry_run=args.dry_run)
        if full_codex_sync and codex_root is not None:
            if not args.dry_run:
                codex_root.mkdir(parents=True, exist_ok=True)
            if not args.no_codex_rules:
                _sync_codex_global_rules(template_dir / "rules" / "AGENTS.md", codex_root, dry_run=args.dry_run)
            if not args.no_codex_agents:
                _sync_codex_agents(codex_template_dir, codex_root, dry_run=args.dry_run)
            if not args.no_codex_hooks:
                try:
                    _sync_codex_hooks(
                        template_dir / "hooks", codex_root, codex_hooks_plan, dry_run=args.dry_run,
                        mode=args.codex_handoff_mode, compact_limit=args.codex_handoff_compactions,
                    )
                except (OSError, ValueError) as exc:
                    parser.error(str(exc))
            _sync_codex_standalone_baseline(
                codex_root,
                include_sandbox=(not args.no_sandbox_config),
                dry_run=args.dry_run,
            )
            if zotero_exe:
                configure_codex_zotero_mcp(codex_root, zotero_exe, dry_run=args.dry_run)
            if not args.no_model_config:
                _sync_codex_frontier_max(codex_root, dry_run=args.dry_run, overwrite=args.sync_model_defaults)

    if args.dry_run:
        print("\n[+] Dry-Run 演练检查完成，未修改磁盘文件。")
        return

    if not args.skip_test:
        if not tests_dir.exists():
            print(f"[!] 错误：找不到测试套件目录 {tests_dir}", file=sys.stderr)
            sys.exit(1)

        print("\n" + "=" * 80)
        print("  正在执行全量无字节码 (-B) 自动化测试回归...")
        print("=" * 80)

        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["USER_GUARD_INSTALLED_DIR"] = str(target_plugin_dir if (install_ag and target_plugin_dir is not None) else template_dir)
        if install_cx and codex_root is not None:
            env["USER_GUARD_INSTALLED_CODEX_DIR"] = str(codex_root)

        test_cmd = [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "unittest",
            "discover",
            "-s",
            str(tests_dir),
            "-p",
            "test_*.py",
            "-v",
        ]
        res = subprocess.run(test_cmd, env=env)

        if res.returncode != 0:
            print("\n[!] 自动化回归测试未全部通过，请检查上方输出日志。", file=sys.stderr)
            sys.exit(res.returncode)

    print("\n" + "=" * 80)
    if args.skip_test:
        print("  指定目标配置同步完成，本次已按参数跳过回归测试。")
    else:
        print("  指定目标配置同步完成，全量离线回归测试通过。")
    print("=" * 80)


if __name__ == "__main__":
    main()

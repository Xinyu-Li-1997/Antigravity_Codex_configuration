#!/usr/bin/env python3
"""
================================================================================
  Antigravity 全域工程化防护与生命周期引擎 (user-guard) 跨平台一键安装器
================================================================================
功能特性：
1. 镜像同步部署 template/user-guard/ 至全局插件目录（默认 ~/.gemini/config/plugins/user-guard），
   自动清除旧版残留文件、废弃状态文件 (*_stop_gate.json) 与 __pycache__ 字节码缓存；
2. 智能探测当前平台可用的 Python 解释器，自适应重写 hooks.json 命令前缀（全平台开启 -X utf8 -I -B）；
3. 可选智能探测本机已安装的 zotero-mcp 可执行文件，无损合并至用户级 ~/.gemini/config/mcp_config.json
   （与插件本体解耦：本机自动恢复可用，无 Zotero 的新机器静默跳过零报错）；
4. 将同一份 cae-harness 技能源安装到 Codex 技能目录，支持双目标隔离安装与只读预演；
5. 以 -B 无字节码模式运行 tests/ 下全套离线与端到端集成测试。
================================================================================
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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


def _is_working_python_cmd(cmd_tokens: List[str]) -> bool:
    """验证候选 Python 命令真实可执行且版本 >= 3.9（排除 WindowsApps 空存根）"""
    exe_path = shutil.which(cmd_tokens[0])
    if not exe_path:
        return False
    if os.name == "nt" and "windowsapps" in exe_path.replace("/", "\\").lower():
        # Microsoft Store 未安装时的空存根会返回 9009，必须实测执行验证
        pass
    try:
        proc = subprocess.run(
            cmd_tokens + ["-I", "-B", "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"],
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
    """
    动态探测本机是否存在可用的 zotero-mcp 可执行文件（零个人路径硬编码）。
    """
    if explicit_cmd:
        p = Path(os.path.expanduser(explicit_cmd))
        if p.exists() and p.is_file():
            return str(p.resolve())
        found = shutil.which(explicit_cmd)
        if found:
            return found
        return None

    in_path = shutil.which("zotero-mcp")
    if in_path:
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
    """将探测到的 zotero-mcp 无损合并至用户级 ~/.gemini/config/mcp_config.json"""
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

    # 若已有配置且其可执行文件仍然有效，则尊重现有配置不随意覆盖
    if isinstance(existing, dict):
        old_cmd = str(existing.get("command") or "").strip()
        if old_cmd and (Path(old_cmd).exists() or shutil.which(old_cmd)):
            print(f"[*] 用户级 Zotero MCP 已就绪，保持现有有效配置: {old_cmd}")
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
            # 确保本机 Zotero 桌面端本地回环接口 (127.0.0.1) 直连通信
            "NO_PROXY": "localhost,127.0.0.1",
        },
    }
    mcp_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_cfg_path.write_text(json.dumps(mcp_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[+] 已在用户级 {mcp_cfg_path} 中配置有效 Zotero MCP: {zotero_exe}")


EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


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
    if os.name == "nt" or not shutil.which("cmd.exe") or not shutil.which("wslpath"):
        return []
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
            return []
        wsl_home = subprocess.run(
            ["wslpath", "-u", win_profile],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        ).stdout.strip()
        cache_path = Path(wsl_home) / ".gemini" / "antigravity-cli" / "cache" / "cae_models_cache.json"
        if cache_path.is_file():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            items = data.get("models", []) if isinstance(data, dict) else []
            return [m for m in items if isinstance(m, dict) and m.get("slug") and m.get("display_name")]
    except Exception:
        pass
    return []


def _select_agy_frontier_max_label(models: List[Dict[str, str]]) -> Optional[str]:
    primary_family = ""
    variants: Dict[str, str] = {}
    for item in models:
        m = re.match(r"^(.+)-(minimal|low|medium|high|xhigh|max|ultra)$", item["slug"].lower())
        if not m:
            continue
        fam, tier = m.group(1), m.group(2)
        if not primary_family:
            primary_family = fam
        if fam == primary_family:
            variants[tier] = item["display_name"]
    if not variants:
        return models[0]["display_name"] if models else None
    best_tier = max(variants, key=EFFORT_ORDER.index)
    return variants[best_tier]


def _sync_codex_frontier_max(codex_root: Path, dry_run: bool = False) -> None:
    cfg_path = codex_root / "config.toml"
    cache_path = codex_root / "models_cache.json"
    if not cfg_path.is_file() or not cache_path.is_file():
        return
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8-sig"))
        models = [m for m in cache.get("models", []) if isinstance(m, dict) and m.get("slug")]
        visible = [m for m in models if m.get("visibility") != "hide"] or models
        if not visible:
            return
        frontier = min(visible, key=lambda m: (int(m.get("priority") or 9999), 0))
        slug = str(frontier["slug"])
        levels = [
            lvl.get("effort")
            for lvl in frontier.get("supported_reasoning_levels", [])
            if isinstance(lvl, dict) and lvl.get("effort") in EFFORT_ORDER
        ]
        max_effort = max(levels, key=EFFORT_ORDER.index) if levels else None
        raw_lines = cfg_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return

    split_idx = next((i for i, line in enumerate(raw_lines) if line.lstrip().startswith("[")), len(raw_lines))
    header, body = raw_lines[:split_idx], raw_lines[split_idx:]
    cur_model = cur_effort = None
    model_idx = effort_idx = None
    for idx, line in enumerate(header):
        m_mod = re.match(r'^\s*model\s*=\s*["\']([^"\']+)["\']', line)
        if m_mod:
            cur_model, model_idx = m_mod.group(1), idx
        m_eff = re.match(r'^\s*model_reasoning_effort\s*=\s*["\']([^"\']+)["\']', line)
        if m_eff:
            cur_effort, effort_idx = m_eff.group(1), idx

    if cur_model == slug and (not max_effort or cur_effort == max_effort):
        print(f"[*] Codex 本机默认模型已处于最高挡: {slug} ({cur_effort or 'default'})")
        return
    if dry_run:
        print(f"[Dry-Run] 将更新 {cfg_path} 默认模型挡位 -> {slug} ({max_effort or 'default'})")
        return

    if model_idx is not None:
        header[model_idx] = f'model = "{slug}"'
    else:
        header.insert(0, f'model = "{slug}"')
    if max_effort:
        if effort_idx is not None:
            header[effort_idx] = f'model_reasoning_effort = "{max_effort}"'
        else:
            insert_at = (model_idx + 1) if model_idx is not None else 1
            header.insert(insert_at, f'model_reasoning_effort = "{max_effort}"')
    cfg_path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    print(f"[+] 已配置 Codex 本机默认最高挡模型: {slug} ({max_effort or 'default'})")


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


def _sync_codex_global_rules(template_rules_path: Path, codex_root: Path, dry_run: bool = False) -> None:
    """将模板 R0-R9 全局核心总纲无损同步至 ~/.codex/AGENTS.md，完整保留用户自定义的『## 工具与...』本机硬件与环境说明段。"""
    if not codex_root.is_dir() or not template_rules_path.is_file():
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

    codex_agents.write_text(merged, encoding="utf-8")
    print(f"[+] 已同步更新 Codex 全局规则 {codex_agents}（保留本机环境段）")


def configure_frontier_max_defaults(gemini_root: Path, dry_run: bool = False) -> None:
    """动态探测当前账号可用模型目录，将双端默认配置对齐至最先进模型最高挡位。"""
    agy_exe_name = "agy.exe" if os.name == "nt" else "agy"
    agy_bin = shutil.which("agy") or (
        str(Path.home() / ".gemini" / "bin" / agy_exe_name)
        if (Path.home() / ".gemini" / "bin" / agy_exe_name).is_file()
        else None
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
                timeout=20,
            )
            if proc.returncode == 0:
                models = _parse_agy_model_lines(proc.stdout)
        except Exception:
            pass
    if not models:
        models = _load_wsl_windows_agy_cache()

    if models:
        frontier_label = _select_agy_frontier_max_label(models)
        cli_dir = gemini_root / "antigravity-cli"
        settings_path = cli_dir / "settings.json"
        cache_path = cli_dir / "cache" / "cae_models_cache.json"
        settings_data: Dict[str, Any] = {}
        if settings_path.is_file():
            try:
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    settings_data = loaded
            except Exception:
                pass
        if frontier_label:
            if dry_run:
                print(f"[Dry-Run] 将配置 {settings_path} 默认最高挡模型 -> {frontier_label}")
            else:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({"models": models}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                if settings_data.get("model") == frontier_label:
                    print(f"[*] Antigravity CLI 本机默认模型已处于最高挡: {frontier_label}")
                else:
                    settings_data["model"] = frontier_label
                    settings_path.parent.mkdir(parents=True, exist_ok=True)
                    settings_path.write_text(json.dumps(settings_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    print(f"[+] 已配置 Antigravity CLI 本机默认最高挡模型: {frontier_label}")

    configured_codex = os.environ.get("CODEX_HOME", "").strip()
    codex_root = Path(configured_codex).expanduser() if configured_codex else Path.home() / ".codex"
    _sync_codex_frontier_max(codex_root, dry_run=dry_run)


def rewrite_hooks_json_commands(hooks_json_path: Path, py_cmd: str) -> None:
    """将 hooks.json 中的命令前缀自适应替换为目标平台 Python 命令"""
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
        print(f"[Dry-Run] 将同步 {len(expected_rel_files)} 个插件核心文件至: {target_plugin_dir}")
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

    # 扫描目标目录清理陈旧/废弃文件
    stale_files: List[Path] = []
    stale_dirs: List[Path] = []

    for existing in sorted(target_plugin_dir.rglob("*")):
        rel = existing.relative_to(target_plugin_dir)
        if existing.is_dir():
            if existing.name == "__pycache__":
                stale_dirs.append(existing)
            continue

        # __pycache__ 内的文件随目录一并删除
        if "__pycache__" in rel.parts:
            continue

        # 处理本地回退运行态目录 hooks/state 与 hooks/logs
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity user-guard 跨平台一键安装器")
    parser.add_argument("--target-dir", type=str, default="", help="自定义目标插件目录（默认 ~/.gemini/config/plugins/user-guard）")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，仅打印同步与配置计划不实际写入")
    parser.add_argument("--skip-test", action="store_true", help="跳过安装后的全套自动化回归测试")
    parser.add_argument("--no-zotero", action="store_true", help="跳过本机可选 Zotero MCP 自动探测与配置")
    parser.add_argument("--zotero-cmd", type=str, default="", help="显式指定本地 zotero-mcp 可执行文件路径")
    parser.add_argument("--codex-skills-dir", default="", help="Codex 技能根目录，其下安装 cae-harness；显式指定可用于隔离验证")
    parser.add_argument("--no-codex-skill", action="store_true", help="跳过 Codex 技能安装")
    parser.add_argument("--no-codex-rules", action="store_true", help="跳过 Codex 全局 AGENTS.md 核心总纲同步")
    parser.add_argument("--no-model-config", action="store_true", help="跳过本机双端默认最高挡模型对齐")
    parser.add_argument("--no-sandbox-config", action="store_true", help="跳过 Codex 项目目录边界硬约束配置（workspace-write + on-request）")
    args = parser.parse_args()
    if args.no_codex_skill and args.codex_skills_dir:
        parser.error("--no-codex-skill 与 --codex-skills-dir 不能同时指定")

    repo_root = Path(__file__).resolve().parent
    template_dir = repo_root / "template" / "user-guard"
    tests_dir = repo_root / "tests"

    if not template_dir.exists():
        print(f"[!] 错误：找不到插件模板目录 {template_dir}", file=sys.stderr)
        sys.exit(1)

    default_config_root = Path.home() / ".gemini" / "config"
    if args.target_dir:
        target_plugin_dir = Path(args.target_dir).expanduser().absolute()
        config_root = target_plugin_dir.parent.parent
    else:
        config_root = default_config_root
        target_plugin_dir = config_root / "plugins" / "user-guard"

    target_skill_dir = codex_skill_target(args.codex_skills_dir, bool(args.target_dir), args.no_codex_skill)
    try:
        target_plugin_dir = validate_install_target(target_plugin_dir, repo_root)
        if target_skill_dir is not None:
            target_skill_dir = validate_install_target(target_skill_dir, repo_root)
            if (
                target_skill_dir == target_plugin_dir
                or target_skill_dir in target_plugin_dir.parents
                or target_plugin_dir in target_skill_dir.parents
            ):
                raise ValueError("插件与 Codex 技能安装目标不能相互包含")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    config_root = target_plugin_dir.parent.parent

    py_cmd = detect_hook_python_command()

    print("=" * 80)
    print("  user-guard 跨平台插件与协作技能安装程序")
    print("=" * 80)
    print(f"[*] 仓库根目录   : {repo_root}")
    print(f"[*] 插件模板源   : {template_dir}")
    print(f"[*] 目标安装路径 : {target_plugin_dir}")
    print(f"[*] 钩子运行命令 : {py_cmd}")
    if target_skill_dir is not None:
        print(f"[*] Codex 技能路径: {target_skill_dir}")
    else:
        print("[*] 本次跳过 Codex 技能安装。")
    print("-" * 80)

    mirror_sync_plugin(template_dir, target_plugin_dir, dry_run=args.dry_run)
    if target_skill_dir is not None:
        mirror_sync_plugin(template_dir / "skills" / "cae-harness", target_skill_dir, dry_run=args.dry_run)

    hooks_json_path = target_plugin_dir / "hooks.json"
    if not args.dry_run and hooks_json_path.exists():
        rewrite_hooks_json_commands(hooks_json_path, py_cmd)
        print(f"[+] hooks.json 已自适应写入平台命令前缀: {py_cmd}")

    # 可选配置用户级 Zotero MCP（隔离安装到非默认目标时禁止触碰真实 ~/.gemini/config）
    is_default_plugin_target = target_plugin_dir == (default_config_root / "plugins" / "user-guard").resolve()
    if not args.no_zotero and (not args.target_dir or args.zotero_cmd):
        if not is_default_plugin_target and config_root.resolve() == default_config_root.resolve():
            parser.error("自定义 --target-dir 的父级目录指向真实 ~/.gemini/config，请使用独立沙箱目录或指定 --no-zotero")
        zotero_exe = detect_zotero_mcp_executable(args.zotero_cmd)
        if zotero_exe:
            configure_user_zotero_mcp(config_root, zotero_exe, dry_run=args.dry_run)
        else:
            print("[*] 未在本机检测到 zotero-mcp 可执行文件，已跳过可选 Zotero MCP 配置（不影响插件功能）。")

    if is_default_plugin_target and not args.no_model_config:
        configure_frontier_max_defaults(config_root.parent, dry_run=args.dry_run)
    configured_codex = os.environ.get("CODEX_HOME", "").strip()
    codex_root = Path(configured_codex).expanduser() if configured_codex else Path.home() / ".codex"
    if is_default_plugin_target and not args.no_sandbox_config and not args.codex_skills_dir:
        _sync_codex_workspace_boundary(codex_root, dry_run=args.dry_run)
    if is_default_plugin_target and not args.no_codex_rules and not args.codex_skills_dir and not args.no_codex_skill:
        _sync_codex_global_rules(template_dir / "rules" / "AGENTS.md", codex_root, dry_run=args.dry_run)

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
        env["USER_GUARD_INSTALLED_DIR"] = str(target_plugin_dir)

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
        print("  指定目标安装完成，本次已按参数跳过回归测试。")
    else:
        print("  指定目标安装完成，离线回归测试通过。")
    print("=" * 80)


if __name__ == "__main__":
    main()

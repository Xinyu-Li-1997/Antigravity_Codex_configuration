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
import copy
import datetime
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

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
ZOTERO_ENV = {"ZOTERO_LOCAL": "true", "PYTHON_DOTENV_DISABLED": "1", "NO_PROXY": "localhost,127.0.0.1"}


class FileChange(NamedTuple):
    path: Path
    original: Optional[bytes]
    content: bytes


def _read_optional_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write(change: FileChange) -> None:
    path, original, content = change
    _validate_path(path, directory=False)
    if _read_optional_bytes(path) != original:
        raise ValueError(f"{path} 在安装期间发生变更，请重新运行安装器。")
    if content == original:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(content)
        _validate_path(path, directory=False)
        if _read_optional_bytes(path) != original:
            raise ValueError(f"{path} 在安装期间发生变更，请重新运行安装器。")
        if original is not None:
            temp_path.chmod(stat.S_IMODE(path.stat().st_mode))
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _apply_file_change(change: Optional[FileChange], dry_run: bool) -> None:
    if change is None:
        return
    if dry_run:
        print(f"[Dry-Run] 将更新 {change.path}")
    else:
        _atomic_write(change)
        print(f"[+] 已更新 {change.path}")


def _json_bytes(doc: Dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 中包含非法数值 {value}")


def _read_json_object(path: Path, raw: Optional[bytes]) -> Dict[str, Any]:
    if raw is None:
        return {}
    try:
        result = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(result, dict):
            raise ValueError("顶层必须是对象")
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"无法安全合并 {path}，原文件已保留: {exc}") from exc
    return result


def _plan_user_zotero(config_root: Path, zotero_exe: Optional[str]) -> Optional[FileChange]:
    path = config_root / "mcp_config.json"
    _validate_path(path, directory=False)
    if not zotero_exe:
        return None
    raw = _read_optional_bytes(path)
    data = _read_json_object(path, raw)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict) or any(not isinstance(server, dict) for server in servers.values()):
        raise ValueError(f"{path} 的 mcpServers 及各服务器条目必须为对象，原文件已保留。")
    if servers.get("zotero"):
        print("[*] 保留反重力已配置的 Zotero MCP。")
        return None
    servers["zotero"] = {"command": zotero_exe, "args": [], "env": ZOTERO_ENV}
    return FileChange(path, raw, _json_bytes(data))


TOML_STRING = r'''(?:"(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*')'''
TOML_KEY = rf"(?:[A-Za-z0-9_-]+|{TOML_STRING})"
TOML_KEY_PATH = rf"{TOML_KEY}(?:[ \t]*\.[ \t]*{TOML_KEY})*"
TOML_TABLE = re.compile(rf"^[ \t]*(\[{{1,2}})[ \t]*({TOML_KEY_PATH})[ \t]*(\]{{1,2}})[ \t]*(?:#.*)?$")
TOML_ASSIGNMENT = re.compile(rf"^([ \t]*({TOML_KEY_PATH})[ \t]*=[ \t]*)(.*)$")
TOML_ARRAY_TOKEN = re.compile(rf"{TOML_STRING}|[^, \t]+")
TOML_ESCAPE = re.compile(r'\\(?:(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})|([btnfr"\\]))')
TOML_ESCAPED_CHAR = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", r"\u007f")


def _simple_toml_value(source: str) -> Any:
    """Python 3.9/3.10 支持常见单行值，对其余语法要求使用 tomllib。"""
    if re.fullmatch(r'"(?:[^"\\\x00-\x08\x0a-\x1f\x7f]|\\(?:[btnfr"\\]|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}))*"', source):
        def unescape(match: re.Match) -> str:
            if match[2]:
                return TOML_ESCAPED_CHAR[match[2]]
            codepoint = int(match[1][1:], 16)
            if not 0 <= codepoint <= 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                raise ValueError("TOML Unicode 转义无效")
            return chr(codepoint)
        return TOML_ESCAPE.sub(unescape, source[1:-1])
    if re.fullmatch(r"'[^'\x00-\x08\x0a-\x1f\x7f]*'", source):
        return source[1:-1]
    if source in {"true", "false"}:
        return source == "true"
    if re.fullmatch(r"[+-]?(?:0|[1-9](?:_?[0-9])*)", source):
        return int(source.replace("_", ""))
    if re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)", source):
        return float(source)
    raise ValueError("此 TOML 值需要 Python 3.11+ 才能安全解析")


def _toml_key_parts(source: str) -> Tuple[str, ...]:
    return tuple(_simple_toml_value(key) if key[0] in "\"'" else key for key in re.findall(TOML_KEY, source))


def _simple_toml_loads(source: str) -> Dict[str, Any]:
    if re.search(r"\r(?!\n)|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", source):
        raise ValueError("TOML 含非法控制字符")
    data: Dict[str, Any] = {}
    section: Tuple[str, ...] = ()
    explicit_tables: Set[Tuple[str, ...]] = set()
    dotted_tables: Set[Tuple[str, ...]] = set()

    def table(path: Tuple[str, ...]) -> Dict[str, Any]:
        value = data
        for key in path:
            child = value.setdefault(key, {})
            if not isinstance(child, dict):
                raise ValueError("TOML 表与已有键冲突")
            value = child
        return value

    for number, line in enumerate(source.split("\n"), start=1):
        line = line.rstrip("\r")
        if not line.strip(" \t") or line.lstrip(" \t").startswith("#"):
            continue
        try:
            header = TOML_TABLE.fullmatch(line)
            if header and header[1] == "[" and header[3] == "]":
                section = _toml_key_parts(header[2])
                if section in explicit_tables or section in dotted_tables:
                    raise ValueError("TOML 表重复定义")
                table(section)
                explicit_tables.add(section)
                continue
            assignment = TOML_ASSIGNMENT.fullmatch(line)
            if not assignment:
                raise ValueError("该 TOML 语法需要 Python 3.11+ 才能安全解析")
            keys = _toml_key_parts(assignment[2])
            destination = table(section + keys[:-1])
            if keys[-1] in destination:
                raise ValueError("TOML 键重复")
            for index in range(1, len(keys)):
                path = section + keys[:index]
                if path in explicit_tables:
                    raise ValueError("点分键重定义 TOML 表")
                dotted_tables.add(path)
            raw = re.fullmatch(rf"({TOML_STRING}|\[.*\]|[^#]*?)[ \t]*(?:#.*)?", assignment[3])
            if not raw:
                raise ValueError("无法识别 TOML 赋值")
            value = raw[1]
            if value.startswith("[") and value.endswith("]"):
                body = value[1:-1].strip(" \t")
                items = []
                while body:
                    item = TOML_ARRAY_TOKEN.match(body)
                    if not item:
                        raise ValueError("无效的 TOML 数组")
                    items.append(_simple_toml_value(item[0]))
                    body = body[item.end():].lstrip(" \t")
                    if body and not body.startswith(","):
                        raise ValueError("TOML 数组元素缺少逗号")
                    body = body[1:].lstrip(" \t")
                destination[keys[-1]] = items
            else:
                destination[keys[-1]] = _simple_toml_value(value)
        except ValueError as exc:
            raise ValueError(f"第 {number} 行无法安全解析：{exc}") from exc
    return data


def _parse_toml(source: str, path: Path) -> Dict[str, Any]:
    try:
        return tomllib.loads(source) if tomllib is not None else _simple_toml_loads(source)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"无法安全合并 {path}，原文件已保留: {exc}") from exc


def _toml_inline_table_end(line: str, start: int) -> Optional[int]:
    depth = 0
    cursor = start
    while cursor < len(line):
        char = line[cursor]
        if char in "\"'":
            string = re.match(TOML_STRING, line[cursor:])
            if string is None:
                return None
            cursor += string.end()
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _toml_statement_end(lines: List[str], start: int, offset: int) -> int:
    """跳过多行值，避免把值中的文本当成表头或键。"""
    quote = ""
    depth = 0
    for index in range(start, len(lines)):
        line = lines[index][offset if index == start else 0:]
        pos = 0
        while pos < len(line):
            char = line[pos]
            if quote:
                if quote[0] == '"' and char == "\\":
                    pos += 2
                    continue
                if line.startswith(quote, pos):
                    pos += len(quote)
                    if len(quote) == 3:
                        while pos < len(line) and line[pos] == quote[0]:
                            pos += 1
                    quote = ""
                    continue
            elif char in "\"'":
                quote = char * (3 if line.startswith(char * 3, pos) else 1)
                pos += len(quote)
                continue
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            elif char == "#":
                break
            pos += 1
        if not quote and not depth:
            return index
    return len(lines) - 1


class CodexConfig:
    """语义识别已有键，只在原文中插入缺项或替换显式要改的值。"""

    def __init__(self, path: Path):
        self.path = path
        _validate_path(path, directory=False)
        self.original = _read_optional_bytes(path)
        self.source = (self.original or b"").decode("utf-8")
        self.newline = "\r\n" if "\r\n" in self.source else "\n"
        self.data = _parse_toml(self.source, self.path)

    def _locations(self) -> Tuple[List[str], Dict[Tuple[str, ...], int], Dict[Tuple[str, ...], Tuple[int, re.Match]]]:
        parts = self.source.split("\n")
        lines = [part + "\n" for part in parts[:-1]]
        if parts[-1]:
            lines.append(parts[-1])
        tables: Dict[Tuple[str, ...], int] = {}
        assignments: Dict[Tuple[str, ...], Tuple[int, re.Match]] = {}
        section: Tuple[str, ...] = ()
        index = 0
        while index < len(lines):
            line = lines[index].rstrip("\r\n")
            header = TOML_TABLE.fullmatch(line)
            if header:
                section = _toml_key_parts(header[2])
                if header[1] == "[":
                    tables[section] = index
            assignment = TOML_ASSIGNMENT.fullmatch(line)
            if assignment:
                assignments[section + _toml_key_parts(assignment[2])] = (index, assignment)
                index = _toml_statement_end(lines, index, assignment.end(1))
            index += 1
        return lines, tables, assignments

    def _update(self, lines: List[str]) -> None:
        source = "".join(lines)
        self.data = _parse_toml(source, self.path)
        self.source = source

    def add_defaults(self, path: Tuple[str, ...], defaults: Dict[str, str]) -> None:
        section = self.data
        for key in path:
            section = section.get(key, {})
            if not isinstance(section, dict):
                raise ValueError(f"{self.path} 的 {'.'.join(path)} 配置必须为表")
        missing = [f"{key} = {value}" for key, value in defaults.items() if key not in section]
        if not missing:
            return
        lines, tables, assignments = self._locations()
        missing_lines = [line + self.newline for line in missing]
        if not path:
            lines[0:0] = missing_lines
        elif path in tables:
            insert = tables[path] + 1
            if not lines[insert - 1].endswith("\n"):
                lines[insert - 1] += self.newline
            lines[insert:insert] = missing_lines
        elif path in assignments and assignments[path][1][3].startswith("{"):
            index, assignment = assignments[path]
            line = lines[index]
            start = assignment.end(1)
            end = _toml_inline_table_end(line, start)
            if end is None:
                raise ValueError(f"{self.path} 的内联表 {'.'.join(path)} 需要手工补齐缺失配置")
            insert = len(line[:end].rstrip(" \t"))
            separator = ", " if line[start + 1:insert].strip() else " "
            padding = " " if insert == end else ""
            lines[index] = line[:insert] + separator + ", ".join(missing) + padding + line[insert:]
        else:
            # 如果已有点分键，沿其所在的显式父表补缺，避免重定义点分键建立的表。
            existing = any(key[:len(path)] == path for key in assignments)
            if existing:
                parent = next((path[:i] for i in range(len(path) - 1, 0, -1) if path[:i] in tables), ())
                insert = tables[parent] + 1 if parent else 0
                if insert and not lines[insert - 1].endswith("\n"):
                    lines[insert - 1] += self.newline
                prefix = ".".join(path[len(parent):]) + "."
                lines[insert:insert] = [prefix + line for line in missing_lines]
            else:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += self.newline
                if lines and lines[-1].strip():
                    lines.append(self.newline)
                lines += [f"[{'.'.join(path)}]{self.newline}", *missing_lines]
        self._update(lines)

    def set_string(self, key: str, value: str) -> None:
        if key not in self.data:
            self.add_defaults((), {key: _toml_string(value)})
            return
        if self.data[key] == value:
            return
        lines, _, assignments = self._locations()
        if (key,) in assignments:
            index, assignment = assignments[(key,)]
            literal = re.fullmatch(rf"{TOML_STRING}([ \t]*(?:#.*)?)", assignment[3])
            if literal:
                ending = lines[index][len(lines[index].rstrip("\r\n")):]
                lines[index] = assignment[1] + _toml_string(value) + literal[1] + ending
                self._update(lines)
                return
        raise ValueError(f"{self.path} 的 {key} 不是可安全替换的单行字符串，原文件已保留")

    def change(self) -> Optional[FileChange]:
        updated = self.source.encode("utf-8")
        return FileChange(self.path, self.original, updated) if updated != (self.original or b"") else None


def _is_mnt_windows_path(path_str: Optional[str]) -> bool:
    """在 POSIX/WSL 下判断可执行文件路径是否位于 /mnt/<盘符>/ 挂载的 Windows 侧"""
    if os.name == "nt" or not path_str:
        return False
    return bool(re.match(r"^/mnt/[a-zA-Z]/", path_str.replace("\\", "/")))


def _first_executable(candidates: List[Path], *, windows: bool = False) -> Optional[str]:
    for candidate in candidates:
        try:
            if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
                return str(candidate)
        except OSError:
            continue
    return None


def _windows_app_binaries(home: Path, local_app: Path, name: str) -> Tuple[Optional[str], Optional[str]]:
    if name == "agy":
        cli = [home / ".gemini" / "bin" / "agy.exe", home / ".local" / "bin" / "agy.exe"]
        desktop = [
            local_app / "Programs" / "antigravity" / "Antigravity.exe",
            local_app / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe",
        ]
    else:
        codex_dir = local_app / "OpenAI" / "Codex"
        cli = [home / ".local" / "bin" / "codex.exe", home / ".cargo" / "bin" / "codex.exe"]
        desktop = [codex_dir / "Codex.exe"]
        try:
            if codex_dir.is_dir():
                cli += sorted(codex_dir.glob("bin/**/codex.exe"), reverse=True)
        except OSError:
            pass
    return _first_executable(cli, windows=True), _first_executable(desktop, windows=True)


def _native_cli(name: str, home: Path) -> Optional[str]:
    if os.name == "nt":
        local_app = Path(os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local"))
        local_cli, _ = _windows_app_binaries(home, local_app, name)
        return local_cli or shutil.which(f"{name}.exe") or shutil.which(name)
    candidates = [home / ".local" / "bin" / name]
    candidates.insert(0, home / (".gemini" if name == "agy" else ".cargo") / "bin" / name)
    found = _first_executable(candidates) or shutil.which(name)
    return found if found and not _is_mnt_windows_path(found) and not found.lower().endswith(".exe") else None


def _find_windows_codex_package() -> Tuple[Optional[str], Optional[str]]:
    """读取当前用户的 Codex 应用包注册，不启动应用或 CLI。"""
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        return None, None
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
             "(Get-AppxPackage -Name OpenAI.Codex).InstallLocation"],
            capture_output=True, text=True, encoding="utf-8", timeout=5,
        )
        location = result.stdout.strip()
        if result.returncode != 0 or not location:
            return None, None
        app = Path(location) / "app"
        return (_first_executable([app / "resources" / "codex.exe"], windows=True),
                _first_executable([app / "Codex.exe"], windows=True))
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None, None


def _is_wsl() -> bool:
    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except (OSError, UnicodeError):
        return False


def _probe_wsl_windows_profile_dir() -> Optional[Path]:
    """在 WSL 环境下探测对应的 Windows 宿主用户主目录（如 /mnt/c/Users/<user>），只读零副作用"""
    if not _is_wsl():
        return None
    cmd_exe = shutil.which("cmd.exe")
    wslpath = shutil.which("wslpath")
    if not cmd_exe or not wslpath:
        return None
    try:
        profile = subprocess.run(
            [cmd_exe, "/d", "/c", "echo", "%USERPROFILE%"],
            cwd=str(Path(cmd_exe).parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        win_profile = profile.stdout.strip()
        if profile.returncode != 0 or not PureWindowsPath(win_profile).is_absolute():
            return None
        converted = subprocess.run(
            [wslpath, "-u", win_profile],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        wsl_home = converted.stdout.strip()
        if converted.returncode != 0 or not wsl_home:
            return None
        p = Path(wsl_home)
        return p if p.is_absolute() and p.is_dir() else None
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None


def _wsl_distro_list(wsl: str, *options: str) -> Optional[List[str]]:
    try:
        result = subprocess.run([wsl, "--list", *options, "--quiet"], capture_output=True, timeout=5)
        if result.returncode != 0:
            return None
        # 不同版本的 wsl.exe 重定向输出可能是 UTF-16 或 UTF-8。
        encoding = "utf-16-le" if b"\0" in result.stdout else "utf-8-sig"
        return [line.strip().lstrip("\ufeff") for line in result.stdout.decode(encoding).splitlines() if line.strip()]
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None


def _probe_windows_wsl() -> Dict[str, Any]:
    """列出发行版，只检查已运行发行版的用户目录和可执行路径。"""
    result: Dict[str, Any] = {"status": "unknown", "installed_distros": [], "running_distros": [], "apps": {}}
    wsl = shutil.which("wsl.exe")
    if not wsl:
        return result
    installed = _wsl_distro_list(wsl)
    running = _wsl_distro_list(wsl, "--running")
    result["installed_distros"] = installed or []
    result["running_distros"] = running or []
    if installed is None or running is None:
        return result

    # --exec 不加载交互配置，不运行被探测的 CLI。
    script = (
        'for name in agy codex; do '
        '  bin=$(command -v "$name"); '
        '  for candidate in "$HOME/.gemini/bin/$name" "$HOME/.local/bin/$name" "$HOME/.cargo/bin/$name"; do '
        '    if [ -f "$candidate" ] && [ -x "$candidate" ]; then bin=$candidate; break; fi; '
        '  done; '
        '  case "$bin" in /*) case "$bin" in /mnt/[a-zA-Z]/*|*.exe) bin=;; esac;; *) bin=;; esac; '
        '  trace=false; '
        '  if [ "$name" = agy ]; then '
        '    [ -d "$HOME/.gemini/config" ] || [ -d "$HOME/.gemini/antigravity-cli" ] || [ -d "$HOME/.gemini/antigravity" ]; '
        '  else [ -d "$HOME/.codex" ]; fi && trace=true; '
        '  printf "%s\\t%s\\t%s\\n" "$name" "$bin" "$trace"; '
        'done'
    )
    complete = set(installed) == set(running)
    for distro in running:
        try:
            probe = subprocess.run(
                [wsl, "--distribution", distro, "--exec", "/bin/sh", "-c", script],
                capture_output=True, text=True, encoding="utf-8", timeout=5,
            )
            if probe.returncode != 0:
                complete = False
                continue
            for line in probe.stdout.splitlines():
                fields = line.split("\t")
                if len(fields) == 3 and fields[0] in {"agy", "codex"}:
                    name, binary, trace = fields
                    result["apps"].setdefault(name, []).append(
                        {"distro": distro, "binary": binary or None, "config_dir_exists": trace == "true"}
                    )
            if any(not any(item["distro"] == distro for item in result["apps"].get(name, [])) for name in ("agy", "codex")):
                complete = False
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            complete = False
    result["status"] = "checked" if complete else "unknown"
    return result


def detect_installed_apps() -> Dict[str, Any]:
    """
    只读探测当前运行环境的 Antigravity 与 Codex 安装状态（区分本侧原生与跨子系统互操作）。
    """
    home = Path.home()
    gemini_root = home / ".gemini"
    configured_codex = os.environ.get("CODEX_HOME", "").strip()
    codex_root = Path(configured_codex).expanduser() if configured_codex else (home / ".codex")

    wsl = _is_wsl()
    windows_home = _probe_wsl_windows_profile_dir() if wsl else None
    wsl_probe = _probe_windows_wsl() if os.name == "nt" else None

    def app_info(name: str, config_dir_exists: bool) -> Dict[str, Any]:
        cli = _native_cli(name, home)
        desktop = None
        if os.name == "nt":
            local_app = Path(os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local"))
            _, desktop = _windows_app_binaries(home, local_app, name)
            if name == "codex" and not (cli or desktop):
                cli, desktop = _find_windows_codex_package()
        elif name == "agy":
            desktop = _first_executable([Path("/usr/bin/antigravity"), Path("/opt/Antigravity/antigravity"),
                                         Path("/Applications/Antigravity.app/Contents/MacOS/Antigravity")])
        elif sys.platform == "darwin":
            desktop = _first_executable([Path("/Applications/Codex.app/Contents/MacOS/Codex"),
                                         home / "Applications" / "Codex.app" / "Contents" / "MacOS" / "Codex"])

        evidence = []
        interop_complete = not wsl
        if wsl:
            found = shutil.which(name)
            win_cli = shutil.which(f"{name}.exe") or (found if _is_mnt_windows_path(found) else None)
            win_desktop = None
            trace = False
            if windows_home is not None:
                known_cli, win_desktop = _windows_app_binaries(windows_home, windows_home / "AppData" / "Local", name)
                win_cli = win_cli or known_cli
                trace = (windows_home / (".gemini" if name == "agy" else ".codex")).is_dir()
            evidence.append({"binary": win_cli, "desktop_binary": win_desktop, "config_dir_exists": trace})
        elif wsl_probe is not None:
            evidence = wsl_probe["apps"].get(name, [])
            interop_complete = wsl_probe["status"] == "checked"
        executable = bool(cli or desktop)
        interop_available = any(item.get("binary") or item.get("desktop_binary") for item in evidence)
        return {
            "native_installed": executable,
            "native_binary": cli,
            "native_desktop_binary": desktop,
            "native_config_dir_exists": config_dir_exists,
            "native_status": "executable_detected" if executable else ("config_only" if config_dir_exists else "not_detected"),
            "interop_available": interop_available,
            "interop_status": "executable_detected" if interop_available else ("not_detected" if interop_complete else "unknown"),
            "interop_evidence": evidence,
            "authentication_status": "not_checked",
        }

    ag = app_info("agy", any((gemini_root / sub).is_dir() for sub in ("antigravity", "antigravity-cli", "config")))
    cx = app_info("codex", codex_root.is_dir())
    ag["target_plugin_dir"] = str(gemini_root / "config" / "plugins" / "user-guard")
    cx["target_codex_home"] = str(codex_root)
    ag_native, cx_native = ag["native_installed"], cx["native_installed"]

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
        "is_wsl": wsl,
        "wsl_probe": wsl_probe,
        "antigravity": ag,
        "codex": cx,
        "zotero_mcp_detected": zotero_exe,
        "recommended_app_mode": recommended,
        "single_app_only_on_host": (
            (ag_native and not cx_native and cx["interop_status"] == "not_detected")
            or (cx_native and not ag_native and ag["interop_status"] == "not_detected")
        ),
    }


HOOK_PYTHON_FLAGS = ["-X", "utf8", "-I", "-B"]
LEGACY_PYTHON_FLAGS = ([], ["-B"], ["-I", "-B"], HOOK_PYTHON_FLAGS)


def _hook_python_candidates() -> List[Tuple[str, str, List[str]]]:
    """从安装器自己的 PATH 和当前解释器发现候选，不读取钩子注册表。"""
    candidates: List[Tuple[str, str, List[str]]] = []
    path_dirs = [Path(entry) for entry in os.get_exec_path() if Path(entry).is_absolute()]
    if os.name == "nt":
        for name, args in (("python.exe", []), ("py.exe", ["-3"])):
            for directory in path_dirs:
                path = directory / name
                if not any(part.lower() == "windowsapps" for part in path.parts) and path.is_file():
                    candidates.append((name, str(path), args))
                    break
    else:
        for name in ("python3", "python"):
            path = shutil.which(name, path=os.pathsep.join(map(str, path_dirs)))
            if path:
                candidates.append((name, path, []))
    if sys.executable:
        candidates.append(("", sys.executable, []))
    return candidates


def _is_working_python_cmd(executable: str, launcher_args: List[str]) -> bool:
    """只执行安装器发现的解释器，并只传入固定版本探测参数。"""
    if not Path(executable).is_absolute() or launcher_args not in ([], ["-3"]):
        return False
    try:
        proc = subprocess.run(
            [executable, *launcher_args, *HOOK_PYTHON_FLAGS,
             "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def detect_hook_python_command(*, antigravity: bool = False) -> str:
    """生成宿主钩子可调用的 Python 命令。Windows 反重力要求 PATH 前缀。"""
    flags = " ".join(HOOK_PYTHON_FLAGS)
    for name, executable, launcher_args in _hook_python_candidates():
        if not name:
            continue
        shell_path = shutil.which(name)
        if shell_path and Path(shell_path) == Path(executable) and _is_working_python_cmd(executable, launcher_args):
            return " ".join([name, *launcher_args, flags])
    if antigravity and os.name == "nt":
        raise ValueError("Windows 反重力钩子需要 PATH 中可执行的 Python 3.9+（python.exe 或 py.exe -3）。")
    executable = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    return f"{executable} {flags}"


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
    if codex_cfg.is_file():
        try:
            cfg = _parse_toml(codex_cfg.read_bytes().decode("utf-8"), codex_cfg)
            command = cfg.get("mcp_servers", {}).get("zotero", {}).get("command")
            if isinstance(command, str):
                candidate = Path(command).expanduser()
                if candidate.is_absolute() and candidate.is_file():
                    return str(candidate.resolve())
        except (AttributeError, OSError, ValueError):
            pass

    return None


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


def _apply_codex_frontier_max(
    editor: "CodexConfig", cache_path: Path, *, overwrite: bool,
) -> None:
    keys = ("model", "model_reasoning_effort")
    values = {key: editor.data[key] for key in keys if key in editor.data}
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError(f"{editor.path} 的默认模型和推理挡位必须为字符串")

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

    for key, value in zip(keys, (slug, effort)):
        if value and value != values.get(key, "").strip():
            editor.set_string(key, value)


def _apply_codex_workspace_boundary(editor: "CodexConfig") -> None:
    sandbox_mode = editor.data.get("sandbox_mode")
    if sandbox_mode is None or sandbox_mode == "danger-full-access":
        sandbox_mode = "workspace-write"
        editor.set_string("sandbox_mode", sandbox_mode)
    editor.add_defaults((), {"approval_policy": '"on-request"'})
    if sandbox_mode == "workspace-write":
        editor.add_defaults(("sandbox_workspace_write",), {"network_access": "true"})


def _apply_codex_baseline(editor: "CodexConfig", include_sandbox: bool) -> None:
    editor.add_defaults((), {"model_reasoning_summary": '"auto"'})
    editor.add_defaults(("features",), {"memories": "false", "chronicle": "false"})
    editor.add_defaults(("memories",), {
        "generate_memories": "false", "use_memories": "false", "disable_on_external_context": "true",
    })
    editor.add_defaults(("desktop", "external-agent-import-sync-item-types"), {"HOOKS": "false"})
    if include_sandbox:
        _apply_codex_workspace_boundary(editor)


def _apply_codex_zotero(editor: "CodexConfig", zotero_exe: str) -> None:
    servers = editor.data.get("mcp_servers", {})
    if not isinstance(servers, dict) or ("zotero" in servers and not isinstance(servers["zotero"], dict)):
        raise ValueError(f"{editor.path} 的 mcp_servers 和 zotero 配置必须为表")
    if servers.get("zotero"):
        print("[*] 保留 Codex 已配置的 Zotero MCP。")
        return
    editor.add_defaults(("mcp_servers", "zotero"), {"command": _toml_string(zotero_exe)})
    editor.add_defaults(("mcp_servers", "zotero", "env"), {
        key: _toml_string(value) for key, value in ZOTERO_ENV.items()
    })


def _plan_codex_config(
    codex_root: Path, *, include_sandbox: bool, include_model: bool,
    overwrite_model: bool, zotero_exe: Optional[str],
) -> Optional["FileChange"]:
    editor = CodexConfig(codex_root / "config.toml")
    _apply_codex_baseline(editor, include_sandbox)
    if zotero_exe:
        _apply_codex_zotero(editor, zotero_exe)
    if include_model:
        _apply_codex_frontier_max(editor, codex_root / "models_cache.json", overwrite=overwrite_model)
    return editor.change()


def _sync_codex_frontier_max(
    codex_root: Path, dry_run: bool = False, *, overwrite: bool = False,
) -> None:
    editor = CodexConfig(codex_root / "config.toml")
    _apply_codex_frontier_max(editor, codex_root / "models_cache.json", overwrite=overwrite)
    _apply_file_change(editor.change(), dry_run)


def _sync_codex_workspace_boundary(codex_root: Path, dry_run: bool = False) -> None:
    editor = CodexConfig(codex_root / "config.toml")
    if editor.original is not None:
        _apply_codex_workspace_boundary(editor)
        _apply_file_change(editor.change(), dry_run)


def _sync_codex_global_rules(template_rules_path: Path, codex_root: Path, dry_run: bool = False) -> None:
    """将模板 R0-R9 全局核心总纲同步至 <codex_root>/AGENTS.md；若用户自定义了『## 工具与本机』等非默认尾段则原样保留。"""
    if not template_rules_path.is_file():
        return
    try:
        template_text = template_rules_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except Exception:
        return

    env_sec_re = re.compile(r"(?:^|\n)(##\s+工具与([^\r\n]*)[\s\S]*)$")
    m_tpl_env = env_sec_re.search(template_text)
    template_core = template_text[: m_tpl_env.start()].rstrip() if m_tpl_env else template_text.rstrip()

    codex_agents = codex_root / "AGENTS.md"
    original = _read_optional_bytes(codex_agents)
    existing = (original or b"").decode("utf-8").replace("\r\n", "\n")

    m_usr_env = env_sec_re.search(existing) if existing else None
    has_custom_env = bool(m_usr_env and m_usr_env.group(2).strip() != "运行环境")
    if has_custom_env and m_usr_env is not None:
        merged = (template_core + "\n\n" + m_usr_env.group(1).strip() + "\n") if template_core else (m_usr_env.group(1).strip() + "\n")
    else:
        merged = template_text.rstrip() + "\n"

    if existing == merged:
        print("[*] Codex 全局规则 AGENTS.md 已是最新状态。")
        return

    if dry_run:
        print(f"[Dry-Run] 将同步更新 {codex_agents} 全局规则")
        return

    _atomic_write(FileChange(codex_agents, original, merged.encode("utf-8")))
    print(f"[+] 已同步更新 Codex 全局规则 {codex_agents}")


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
        original = _read_optional_bytes(dst_toml)
        if original == src_bytes:
            continue
        updated_names.append(src_toml.name)
        if not dry_run:
            _atomic_write(FileChange(dst_toml, original, src_bytes))

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
        tokens = shlex.split(handler["command"], posix=(os.name != "nt"))
        return [token.strip('"') for token in tokens] if os.name == "nt" else tokens
    except ValueError:
        return []


class ManagedCodexHook(NamedTuple):
    kind: str
    interpreter: Optional[Tuple[str, List[str]]]
    policy: Optional[Tuple[str, int]] = None


def _codex_managed_hook(
    handler: Dict[str, Any], codex_root: Path, candidates: List[Tuple[str, str, List[str]]],
) -> Optional[ManagedCodexHook]:
    tokens = _codex_hook_tokens(handler)
    hook_root = codex_root / "hooks"

    def matches(token: str, path: Path) -> bool:
        try:
            return Path(token).expanduser() == path
        except (RuntimeError, ValueError):
            return False

    for idx, token in enumerate(tokens[1:], start=1):
        script_name = Path(token).name
        if script_name not in {"session_context_loader.py", "handoff_gate.py"} or not matches(token, hook_root / script_name):
            continue
        args = tokens[idx + 1:]
        executable = tokens[0]
        launcher = ["-3"] if os.name == "nt" and executable.lower() in {"py", "py.exe"} else []
        path_prefix = Path(executable).is_absolute()
        command = executable.lower() if os.name == "nt" else executable
        known_names = {"python", "python.exe", "py", "py.exe"} if os.name == "nt" else {"python", "python3"}
        if command not in known_names and not path_prefix:
            return ManagedCodexHook("custom", None)
        interpreter = next(
            ((path, flags) for name, path, flags in candidates
             if (path_prefix and Path(executable) == Path(path))
             or (name and command == name)
             or (os.name == "nt" and name and command + ".exe" == name)),
            None,
        )
        if path_prefix:
            if interpreter is None:
                return ManagedCodexHook("custom", None)
            launcher = interpreter[1]
        elif interpreter is not None:
            resolved = shutil.which(executable)
            if not resolved or Path(resolved) != Path(interpreter[0]):
                interpreter = None
        actual_flags = tokens[1:idx]
        if not any(actual_flags == launcher + flags for flags in LEGACY_PYTHON_FLAGS):
            return ManagedCodexHook("custom", None)
        if script_name == "session_context_loader.py" and args[:2] == ["--client", "codex"]:
            rest = args[2:]
            if rest and (
                len(rest) not in (2, 4) or rest[0] != "--state-dir"
                or not matches(rest[1], hook_root / "state" / "session-context")
                or (len(rest) == 4 and (rest[2] != "--log" or not matches(rest[3], hook_root / "logs" / "session-context.jsonl")))
            ):
                return ManagedCodexHook("custom", None)
            return ManagedCodexHook("loader", interpreter)
        if (
            script_name == "handoff_gate.py" and len(args) == 8
            and actual_flags == launcher + HOOK_PYTHON_FLAGS
            and args[:3] == ["--client", "codex", "--mode"] and args[3] in {"advisory", "enforce"}
            and args[4] == "--compact-limit" and re.fullmatch(r"[0-9]+", args[5])
            and args[6] == "--state-dir" and matches(args[7], hook_root / "state" / "handoff")
        ):
            return ManagedCodexHook("gate", interpreter, (args[3], int(args[5])))
        return ManagedCodexHook("custom", None)
    return None


HANDOFF_MARKER = re.compile(r"\[user-guard codex-handoff v1 mode=(advisory|enforce|off) compact_limit=([0-9]+)\]")


def _codex_handoff_policy(
    doc: Dict[str, Any], policies: Set[Tuple[str, int]], requested_mode: Optional[str],
    requested_limit: Optional[int],
) -> Tuple[str, int]:
    description = doc.get("description", "")
    if not isinstance(description, str):
        raise ValueError("Codex 钩子 description 字段必须是字符串")
    markers = [(index, line) for index, line in enumerate(description.split("\n"))
               if line.startswith("[user-guard codex-handoff")]
    if markers:
        match = HANDOFF_MARKER.fullmatch(markers[0][1])
        if len(markers) != 1 or match is None:
            raise ValueError("Codex 钩子 description 中的交接配置标记无效或重复")
        policies.add((match[1], int(match[2])))

    modes = {mode for mode, _ in policies}
    limits = {limit for _, limit in policies}
    if (requested_mode is None and len(modes) > 1) or (requested_limit is None and len(limits) > 1):
        raise ValueError("Codex 钩子交接策略不一致，请用 --codex-handoff-mode 和 --codex-handoff-compactions 指定需要保留的值")
    mode = requested_mode if requested_mode is not None else next(iter(modes), "advisory")
    limit = requested_limit if requested_limit is not None else next(iter(limits), 0)
    marker = f"[user-guard codex-handoff v1 mode={mode} compact_limit={limit}]"
    if markers:
        lines = description.split("\n")
        lines[markers[0][0]] = marker
        doc["description"] = "\n".join(lines)
    else:
        doc["description"] = description + ("\n" if description and not description.endswith("\n") else "") + marker
    return mode, limit


def _read_codex_hooks(path: Path, raw: Optional[bytes]) -> Dict[str, Any]:
    if raw is None:
        return {"hooks": {}}
    doc = _read_json_object(path, raw)
    if "hooks" in doc and not isinstance(doc["hooks"], dict):
        raise ValueError(f"无法安全合并 {path}，hooks 字段必须是对象。")
    return doc


class CodexHooksPlan(NamedTuple):
    change: Optional[FileChange]
    mode: str
    compact_limit: int


def _plan_codex_hooks(
    hooks_path: Path, template_path: Path, py_cmd: str,
    mode: Optional[str] = None, compact_limit: Optional[int] = None,
) -> CodexHooksPlan:
    _validate_path(hooks_path, directory=False)
    codex_root = hooks_path.parent.resolve()
    original = _read_optional_bytes(hooks_path)
    doc = _read_codex_hooks(hooks_path, original)
    initial = copy.deepcopy(doc)
    defaults = _read_codex_hooks(template_path, template_path.read_bytes())["hooks"]
    events = doc.setdefault("hooks", {})
    codex_path = codex_root.as_posix()

    candidates = _hook_python_candidates()
    classifications = {}
    policies = set()
    for event, existing in events.items():
        if not isinstance(existing, list) or any(
            not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
            or any(not isinstance(handler, dict) for handler in group["hooks"])
            for group in existing
        ):
            raise ValueError(f"无法安全合并 {hooks_path} 的 {event}，事件须包含钩子组列表及各组的 handler 对象列表。")
        if event not in defaults:
            continue
        for group in existing:
            for handler in group["hooks"]:
                managed = _codex_managed_hook(handler, codex_root, candidates)
                classifications[id(handler)] = managed
                if managed and managed.kind == "custom":
                    print(f"[!] {hooks_path} 的 {event} 包含自定义脚本命令，保留原 handler。", file=sys.stderr)
                if managed and managed.policy is not None:
                    policies.add(managed.policy)
    mode, compact_limit = _codex_handoff_policy(doc, policies, mode, compact_limit)

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
        retained = set()
        first_matching_group = None
        merged = []
        for group in existing:
            matching = group.get("matcher") == matcher
            handlers = []
            custom_slots = set()
            for handler in group["hooks"]:
                managed = classifications[id(handler)]
                if managed and managed.kind == "custom" and matching and event == "SessionStart":
                    if any(Path(token).name == "session_context_loader.py" for token in _codex_hook_tokens(handler)):
                        custom_slots.add("loader")
                if managed is None or managed.kind == "custom" or (managed.kind == "loader" and event != "SessionStart"):
                    handlers.append(handler)
                    continue
                kind = managed.kind
                if matching and kind in desired and kind not in retained:
                    updated_handler = handler
                    if kind == "gate":
                        updated_handler = {**handler, "command": desired[kind]["command"]}
                    elif handler["command"] != desired[kind]["command"]:
                        if managed.interpreter is None or not _is_working_python_cmd(*managed.interpreter):
                            updated_handler = {**handler, "command": desired[kind]["command"]}
                    handlers.append(updated_handler)
                    retained.add(kind)
            retained.update(custom_slots)
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
    try:
        content = _json_bytes(doc)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"无法安全合并 {hooks_path}，钩子注册表不能写成有效的 UTF-8 JSON: {exc}") from exc
    change = FileChange(hooks_path, original, content) if doc != initial else None
    return CodexHooksPlan(change, mode, compact_limit)


def _sync_codex_hooks(
    user_guard_hooks_dir: Path,
    codex_root: Path,
    plan: CodexHooksPlan,
    dry_run: bool = False,
) -> None:
    """同步 Codex 原话恢复和长会话交接脚本，逐条合并已登记的 handler。"""
    hooks_path = codex_root / "hooks.json"
    dst_hooks_dir = codex_root / "hooks"
    mode, compact_limit = plan.mode, plan.compact_limit
    hook_scripts = _codex_hook_script_names(mode)
    for target in (dst_hooks_dir, *(dst_hooks_dir / name for name in hook_scripts)):
        if _is_link_or_junction(target):
            raise ValueError(f"Codex 钩子脚本或目录是链接，请先核对: {target}")
    scripts_updated: List[str] = []

    for name in hook_scripts:
        src_bytes = (user_guard_hooks_dir / name).read_bytes()
        dst_file = dst_hooks_dir / name
        original = _read_optional_bytes(dst_file)
        if original == src_bytes:
            continue
        scripts_updated.append(name)
        if not dry_run:
            _atomic_write(FileChange(dst_file, original, src_bytes))

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

    if plan.change is None:
        print("[*] Codex 钩子注册表已是最新状态，保留现有文件字节。")
    elif dry_run:
        print(f"[Dry-Run] 将合并 Codex 钩子注册表 {hooks_path}（交接模式 {mode}，压缩次数阈值 {compact_limit}）")
    else:
        _atomic_write(plan.change)
        print(f"[+] 已合并 Codex 钩子注册表 {hooks_path}（交接模式 {mode}，压缩次数阈值 {compact_limit}）")


def _plan_agy_model(
    gemini_root: Path, *, dry_run: bool, overwrite: bool,
) -> List[FileChange]:
    """优先保留现有模型。预演仅使用已有的本侧目录。"""
    cli_dir = gemini_root / "antigravity-cli"
    settings_path = cli_dir / "settings.json"
    desktop_path = gemini_root / "config" / "config.json"
    cache_path = cli_dir / "cache" / "cae_models_cache.json"
    _validate_path(settings_path, directory=False)
    settings_raw = _read_optional_bytes(settings_path)
    settings = _read_json_object(settings_path, settings_raw)
    current = settings.get("model")
    if not overwrite and not (isinstance(current, str) and current.strip()):
        _validate_path(desktop_path, directory=False)
        desktop = _read_json_object(desktop_path, _read_optional_bytes(desktop_path))
        user_settings = desktop.get("userSettings", {})
        if not isinstance(user_settings, dict):
            raise ValueError(f"{desktop_path} 的 userSettings 必须为对象")
        current = user_settings.get("model")
    if isinstance(current, str) and current.strip() and not overwrite:
        print("[*] 保留 Antigravity 已配置的默认模型。")
        return []

    _validate_path(cache_path, directory=False)
    cache_raw = _read_optional_bytes(cache_path)
    models: List[Dict[str, str]] = []
    changes = []
    agy_bin = None if dry_run else _native_cli("agy", gemini_root.parent)
    if agy_bin:
        try:
            proc = subprocess.run(
                [agy_bin, "models"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=6,
            )
            if proc.returncode == 0:
                models = _parse_agy_model_lines(proc.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    if models:
        snapshot = {
            "source": "agy_models_cli",
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "models": models,
        }
        changes.append(FileChange(cache_path, cache_raw, _json_bytes(snapshot)))
    elif cache_raw is not None:
        try:
            cache = _read_json_object(cache_path, cache_raw)
            if cache.get("source") in (None, "agy_models_cli"):
                models = [item for item in cache.get("models", [])
                          if isinstance(item, dict) and isinstance(item.get("slug"), str)
                          and isinstance(item.get("display_name"), str) and item["slug"] and item["display_name"]]
        except (TypeError, ValueError):
            print(f"[*] 无法读取模型目录 {cache_path}，保留原文件。")
    label = _select_agy_frontier_max_label(models)
    if not label:
        suffix = "。预演不会查询 CLI，正式安装可尝试读取本侧模型目录。" if dry_run else "，保留原配置。"
        print("[*] 未取得 Antigravity 的可用模型目录" + suffix)
    elif settings.get("model") != label:
        settings["model"] = label
        changes.append(FileChange(settings_path, settings_raw, _json_bytes(settings)))
    return changes


def configure_agy_frontier_max_default(
    gemini_root: Path, dry_run: bool = False, *, overwrite: bool = False,
) -> None:
    for change in _plan_agy_model(gemini_root, dry_run=dry_run, overwrite=overwrite):
        _apply_file_change(change, dry_run)


def _agy_hooks_bytes(source: bytes, py_cmd: str) -> bytes:
    """以模板为输入生成目标平台命令，使重装比较最终内容。"""
    hooks_data = json.loads(source.decode("utf-8"))
    lifecycle = hooks_data["user-guard-lifecycle"]
    for event, groups in lifecycle.items():
        for group in groups:
            for hook in group.get("hooks", [group]):
                command = hook["command"]
                prefix, separator, script = command.partition(" ./hooks/")
                if not separator or prefix != "python3 -X utf8 -I -B":
                    raise ValueError(f"反重力钩子模板的 {event} 命令格式有误")
                hook["command"] = py_cmd + separator + script
    return _json_bytes(hooks_data)


def should_ignore_template_item(path: Path) -> bool:
    if any(part in IGNORED_NAMES for part in path.parts):
        return True
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return True
    return False


def _template_files(template_dir: Path) -> List[Path]:
    return [
        item for item in sorted(template_dir.rglob("*"))
        if not should_ignore_template_item(item.relative_to(template_dir)) and item.is_file()
    ]


def _validate_mirror(template_dir: Path, target_dir: Path) -> None:
    _validate_path(target_dir, directory=True)
    for item in _template_files(template_dir):
        _validate_path(target_dir / item.relative_to(template_dir), directory=False)


def mirror_sync_plugin(
    template_dir: Path, target_plugin_dir: Path, dry_run: bool = False, *, hooks_bytes: Optional[bytes] = None,
) -> None:
    """
    将 template_dir 镜像同步至 target_plugin_dir，并精准清理陈旧废弃文件与 __pycache__。
    保留运行态历史归档（如 hooks/state/*_user_words.md 与 hooks/logs/lifecycle.log），但清除废弃 *_stop_gate.json。
    """
    files = _template_files(template_dir)
    expected_rel_files = {item.relative_to(template_dir) for item in files}
    pending_updates: List[Tuple[Path, Path, Optional[bytes], bytes]] = []
    for src in files:
        rel = src.relative_to(template_dir)
        dst = target_plugin_dir / rel
        original = _read_optional_bytes(dst)
        content = hooks_bytes if hooks_bytes is not None and rel == Path("hooks.json") else src.read_bytes()
        if original != content:
            pending_updates.append((src, rel, original, content))

    if dry_run:
        if pending_updates:
            print(f"[Dry-Run] 将同步 {len(pending_updates)} 个变更文件至: {target_plugin_dir}")
            for _src, rel, _orig, _content in pending_updates:
                print(f"  [SYNC] {rel}")
        else:
            print(f"[*] 目录已是最新状态，无需写入: {target_plugin_dir}")
    else:
        target_plugin_dir.mkdir(parents=True, exist_ok=True)
        for src, rel, original, content in pending_updates:
            dst = target_plugin_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(FileChange(dst, original, content))
            if original is None:
                dst.chmod(stat.S_IMODE(src.stat().st_mode))

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


def _validate_path(path: Path, *, directory: bool) -> Path:
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise ValueError(f"安装路径不能包含未解析的上级目录: {path}")
    for ancestor in (*reversed(path.parents), path):
        if os.name == "nt" and ancestor.name.endswith((".", " ")):
            raise ValueError(f"Windows 安装路径不能包含以空格或点结尾的名称: {ancestor}")
        if _is_link_or_junction(ancestor):
            if ancestor == path:
                raise ValueError(f"安装目标是链接或目录联接，请指定真实路径: {ancestor}")
            raise ValueError(f"安装路径含祖先链接或目录联接，请指定真实路径: {ancestor}")
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            continue
        want_directory = ancestor != path or directory
        if want_directory and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"安装目标或其祖先必须为目录: {ancestor}")
        if not want_directory and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"安装目标必须为普通文件: {ancestor}")
        if not want_directory and metadata.st_nlink > 1:
            raise ValueError(f"安装目标存在硬链接，请指定独立文件: {ancestor}")
    return path


def validate_install_target(target: Path, repo_root: Path) -> Path:
    """在镜像同步前核实绝对目标，拒绝源树、用户根目录和危险链接。"""
    resolved = _validate_path(target, directory=True).resolve()
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
    if configured_home:
        codex_home = Path(configured_home).expanduser()
        codex_root = codex_home.resolve()
        protected.update((codex_root, codex_root / "skills"))
    if resolved in protected or resolved in user_root.parents:
        raise ValueError(f"安装目标不能是用户根目录或共享配置目录: {resolved}")
    if resolved == repo_root or resolved in repo_root.parents or repo_root in resolved.parents:
        raise ValueError(f"安装目标与源码仓库重叠: {resolved}")
    _validate_tree_links(resolved)
    return resolved


def _validate_tree_links(root: Path) -> None:
    if not root.is_dir():
        return

    def fail(error: OSError) -> None:
        raise error

    for parent, dirs, files in os.walk(root, onerror=fail, followlinks=False):
        for name in dirs + files:
            entry = Path(parent) / name
            if _is_link_or_junction(entry):
                raise ValueError(f"安装目标包含链接或目录联接，请先核对: {entry}")


def validate_codex_home_target(codex_home: Path, repo_root: Path) -> Path:
    """校验 Codex 主配置目录（支持真实 ~/.codex 或独立沙箱 --codex-home，拒绝源仓库重叠、用户主目录及链接重定向）"""
    resolved = _validate_path(codex_home, directory=True).resolve()
    repo_resolved = repo_root.resolve()
    user_root = Path.home().resolve()

    if resolved == user_root or resolved in user_root.parents:
        raise ValueError(f"Codex 目标目录不能是用户主目录或上级系统目录: {resolved}")
    gemini_root = user_root / ".gemini"
    if resolved == gemini_root or gemini_root in resolved.parents:
        raise ValueError(f"Codex 目标目录不能与反重力配置目录混用: {resolved}")
    if resolved == repo_resolved or resolved in repo_resolved.parents or repo_resolved in resolved.parents:
        raise ValueError(f"Codex 目标目录与源码仓库重叠: {resolved}")
    return resolved


def _codex_hook_script_names(mode: str) -> List[str]:
    names = ["session_context_loader.py", "user_words.py"]
    if mode != "off":
        names.append("handoff_gate.py")
    return names


def _validate_codex_assets(
    codex_root: Path, codex_template_dir: Path, args: argparse.Namespace, hooks_plan: Optional[CodexHooksPlan],
) -> None:
    _validate_path(codex_root / "config.toml", directory=False)
    if not args.no_codex_rules:
        rules = _validate_path(codex_root / "AGENTS.md", directory=False)
        try:
            (_read_optional_bytes(rules) or b"").decode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"{rules} 不是有效的 UTF-8，原文件已保留。") from exc
    if not args.no_codex_agents:
        _validate_path(codex_root / "agents", directory=True)
        for source in (codex_template_dir / "agents").glob("*.toml"):
            _validate_path(codex_root / "agents" / source.name, directory=False)
    if hooks_plan is not None:
        hooks_dir = codex_root / "hooks"
        _validate_path(hooks_dir, directory=True)
        for name in _codex_hook_script_names(hooks_plan.mode):
            _validate_path(hooks_dir / name, directory=False)
        for rel in ("state", "state/handoff", "state/session-context", "state/user-words", "logs"):
            _validate_path(hooks_dir / rel, directory=True)
        _validate_path(hooks_dir / "logs" / "session-context.jsonl", directory=False)
        pycache = _validate_path(hooks_dir / "__pycache__", directory=True)
        _validate_tree_links(pycache)


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
                "[*] 检测到本侧反重力与另一子系统的 Codex。"
                "本次同步本侧反重力，请在另一子系统的终端配置 Codex。"
            )
            return True, False
        if cx_info.get("interop_status") == "unknown":
            print("[*] 另一子系统的 Codex 安装状态未确认，本次只配置本侧反重力。")
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
        if ag_info.get("interop_status") == "unknown":
            print("[*] 另一子系统的反重力安装状态未确认，本次只配置本侧 Codex。")
            return False, True
        if not _confirm_single_app_install("Codex", "反重力 (Antigravity)", interactive, auto_yes, dry_run):
            sys.exit(0)
        return False, True

    print(
        "[!] 未在本侧检测到反重力（Antigravity）或 Codex 可执行文件。\n"
        "    请先安装至少一个客户端，或显式指定 --app codex / --app antigravity / --app both 预置配置目录。",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity & Codex 统一工作流与双向协作安装器")
    if sys.version_info < (3, 9):
        parser.error("需要 Python 3.9 或更高版本")
    parser.add_argument(
        "--app",
        choices=("auto", "both", "antigravity", "codex"),
        default="auto",
        help="目标客户端模式：auto（默认自动检测，单端提示确认）、both（双端全配）、antigravity（仅配反重力）、codex（仅配 Codex）",
    )
    parser.add_argument("--probe-apps", action="store_true", help="纯只读探测本机反重力、Codex 与 Zotero 安装状态并输出 JSON")
    parser.add_argument("-y", "--yes", action="store_true", help="非交互自动确认（仅检测到单客户端时直接单独配置已安装的一端）")
    parser.add_argument("--target-dir", type=str, default="",
                        help="自定义反重力插件目录。配置 Zotero 时须采用 <配置目录>/plugins/user-guard 布局")
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
    parser.add_argument("--codex-handoff-mode", choices=("advisory", "enforce", "off"),
                        help="Codex 长会话交接模式：首次默认 advisory，升级默认沿用已有值")
    parser.add_argument("--codex-handoff-compactions", type=int,
                        help="Codex 交接的压缩次数阈值：首次默认 0，升级默认沿用已有值")
    model_options = parser.add_mutually_exclusive_group()
    model_options.add_argument("--no-model-config", action="store_true", help="跳过默认模型设置")
    model_options.add_argument("--sync-model-defaults", action="store_true", help="用本机模型目录覆盖已有默认模型与挡位")
    parser.add_argument("--no-sandbox-config", action="store_true", help="跳过 Codex 项目目录边界硬约束配置（workspace-write + on-request）")
    args = parser.parse_args()

    if args.codex_handoff_compactions is not None and args.codex_handoff_compactions < 0:
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
    if args.app == "both" and bool(args.target_dir) != bool(args.codex_home or args.codex_skills_dir):
        parser.error("--app both 指定自定义目标目录时，须同时提供反重力 --target-dir 与 Codex 目录参数，避免误改另一端真实主目录")

    repo_root = Path(__file__).resolve().parent
    template_dir = repo_root / "template" / "user-guard"
    codex_template_dir = repo_root / "template" / "codex"
    tests_dir = repo_root / "tests"

    if not template_dir.exists():
        print(f"[!] 错误：找不到反重力插件模板目录 {template_dir}", file=sys.stderr)
        sys.exit(1)

    has_explicit_ag = bool(args.target_dir)
    has_explicit_cx = bool(args.codex_home or args.codex_skills_dir)
    detection = detect_installed_apps() if args.app == "auto" and not (has_explicit_ag or has_explicit_cx) else {}
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

    is_sandbox_run = bool(args.target_dir or args.codex_home or args.codex_skills_dir)
    zotero_exe: Optional[str] = None
    if not args.no_zotero and (not is_sandbox_run or args.zotero_cmd):
        zotero_exe = detect_zotero_mcp_executable(args.zotero_cmd)
        if args.zotero_cmd and not zotero_exe:
            parser.error(f"指定的 Zotero MCP 可执行文件不存在: {args.zotero_cmd}")
        if not zotero_exe:
            print("[*] 未在本机检测到 zotero-mcp 可执行文件。")

    codex_hooks_plan = None
    codex_config_change = None
    agy_hooks = None
    agy_zotero_change = None
    agy_model_changes: List[FileChange] = []
    py_cmd = None
    try:
        agy_config_root = None
        if target_plugin_dir is not None:
            target_plugin_dir = validate_install_target(target_plugin_dir, repo_root)
            for forbidden_codex in {default_codex_root.resolve(), (Path.home().resolve() / ".codex")}:
                if target_plugin_dir == forbidden_codex or forbidden_codex in target_plugin_dir.parents:
                    raise ValueError(f"反重力插件目标目录不能位于 Codex 配置目录内: {target_plugin_dir}")
            _validate_mirror(template_dir, target_plugin_dir)
            standard_layout = target_plugin_dir == target_plugin_dir.parent.parent / "plugins" / "user-guard"
            if zotero_exe and not standard_layout:
                raise ValueError("为反重力配置 Zotero MCP 时，--target-dir 必须为 <配置目录>/plugins/user-guard。")
            if standard_layout and not args.no_zotero:
                config_root = target_plugin_dir.parent.parent
                if zotero_exe:
                    user_root = Path.home().resolve()
                    unsafe_roots = {user_root, *user_root.parents, repo_root, *repo_root.parents}
                    if config_root in unsafe_roots or repo_root in config_root.parents:
                        raise ValueError(f"反重力 Zotero MCP 配置目录需独立于源码仓库和系统根目录: {config_root}")
                agy_config_root = config_root
        if codex_root is not None:
            codex_root = validate_codex_home_target(codex_root, repo_root)
            if not args.no_codex_skill:
                target_skill_dir = codex_root / "skills" / "cae-harness"
        if target_skill_dir is not None:
            target_skill_dir = validate_install_target(target_skill_dir, repo_root)
            _validate_mirror(template_dir / "skills" / "cae-harness", target_skill_dir)
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
        if agy_config_root is not None:
            mcp_path = agy_config_root / "mcp_config.json"
            if mcp_path in {codex_root, target_skill_dir}:
                raise ValueError(f"反重力 Zotero 配置文件与 Codex 目标目录冲突: {mcp_path}")
            agy_zotero_change = _plan_user_zotero(agy_config_root, zotero_exe)
        if target_plugin_dir is not None or (codex_root is not None and not args.no_codex_hooks):
            py_cmd = detect_hook_python_command(antigravity=target_plugin_dir is not None)
        if target_plugin_dir is not None:
            agy_hooks = _agy_hooks_bytes((template_dir / "hooks.json").read_bytes(), py_cmd)
        if codex_root is not None:
            if not args.no_codex_hooks:
                codex_hooks_plan = _plan_codex_hooks(
                    codex_root / "hooks.json", codex_template_dir / "hooks.json", py_cmd,
                    args.codex_handoff_mode, args.codex_handoff_compactions,
                )
            _validate_codex_assets(codex_root, codex_template_dir, args, codex_hooks_plan)
            codex_config_change = _plan_codex_config(
                codex_root, include_sandbox=not args.no_sandbox_config, include_model=not args.no_model_config,
                overwrite_model=args.sync_model_defaults, zotero_exe=zotero_exe,
            )
        if target_plugin_dir == (default_config_root / "plugins" / "user-guard").absolute() and not args.no_model_config:
            agy_model_changes = _plan_agy_model(
                default_config_root.parent, dry_run=args.dry_run, overwrite=args.sync_model_defaults,
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
    if py_cmd is not None:
        print(f"[*] 钩子运行命令 : {py_cmd}")
    if target_plugin_dir is not None:
        print(f"[*] 反重力插件路径: {target_plugin_dir}")
    if codex_root is not None:
        print(f"[*] Codex 主目录 : {codex_root}")
    if target_skill_dir is not None:
        print(f"[*] Codex 技能路径: {target_skill_dir}")
    print("-" * 80)

    # 1. 部署反重力侧
    if target_plugin_dir is not None:
        mirror_sync_plugin(template_dir, target_plugin_dir, dry_run=args.dry_run, hooks_bytes=agy_hooks)
        _apply_file_change(agy_zotero_change, args.dry_run)
        for change in agy_model_changes:
            _apply_file_change(change, args.dry_run)

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
            if codex_hooks_plan is not None:
                _sync_codex_hooks(template_dir / "hooks", codex_root, codex_hooks_plan, dry_run=args.dry_run)
            _apply_file_change(codex_config_change, args.dry_run)

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
        env.pop("USER_GUARD_STATE_DIR", None)
        env.pop("USER_GUARD_LOGS_DIR", None)
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

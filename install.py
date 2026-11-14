#!/usr/bin/env python3
"""
================================================================================
  Antigravity、Codex 与 Claude Code 工程／科研工作流安装器
================================================================================
功能特性：
1. 自动探测客户端并按需部署（--app auto|all|both|antigravity|codex|claude）：
   - 单反重力独立模式：部署 ~/.gemini/config/plugins/user-guard 全套插件（含双模压缩恢复与两阶段长会话交接停机门禁）、CLI 默认模型补缺与可选 Zotero MCP；
   - 单 Codex 独立模式：部署 ~/.codex/AGENTS.md（保留自定义本机硬件段）、agents/readonly-*.toml 只读子代理、
     hooks/ 原话压缩恢复和长会话交接钩子、config.toml 安全沙箱和默认模型补缺及可选 Zotero MCP；
   - both 保持反重力加 Codex；all 同时部署三端。Claude 可选集成本地路由器与 CC Switch 提示词清理。
2. 当本机仅检测到单客户端时，清晰提示是否先安装另一方；选择不安装（或非交互 --yes）即单独配置当前客户端，零报错闭环。
3. --probe-apps 探测客户端；--audit-upgrade 零写入检查资产、卡片与本地模型目录对齐状态。
4. 以 -B 无字节码模式运行 tests/ 下全套离线与端到端回归测试。
================================================================================
"""

import argparse
import copy
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple
from types import ModuleType
from urllib.parse import urlsplit

# 全平台强制标准流为 UTF-8
for _stream_name in ("stdin", "stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

# 一、常量、路径与平台识别

IGNORED_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", ".gitkeep"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd"}
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ZOTERO_ENV = {"ZOTERO_LOCAL": "true", "PYTHON_DOTENV_DISABLED": "1", "NO_PROXY": "localhost,127.0.0.1"}
UTF8_BOM = b"\xef\xbb\xbf"
REGRESSION_TIMEOUT_SECONDS = 600


TOML_STRING = r'''(?:"(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*')'''
TOML_KEY = rf"(?:[A-Za-z0-9_-]+|{TOML_STRING})"
TOML_KEY_PATH = rf"{TOML_KEY}(?:[ \t]*\.[ \t]*{TOML_KEY})*"
TOML_TABLE = re.compile(rf"^[ \t]*(\[{{1,2}})[ \t]*({TOML_KEY_PATH})[ \t]*(\]{{1,2}})[ \t]*(?:#.*)?$")
TOML_ASSIGNMENT = re.compile(rf"^([ \t]*({TOML_KEY_PATH})[ \t]*=[ \t]*)(.*)$")
HOOK_PYTHON_FLAGS = ["-X", "utf8", "-I", "-B"]
LEGACY_PYTHON_FLAGS = ([], ["-B"], ["-I", "-B"], HOOK_PYTHON_FLAGS)
HANDOFF_MARKER = re.compile(r"\[user-guard codex-handoff v1 mode=(advisory|enforce|off) compact_limit=([0-9]+)\]")
CLAUDE_HOOK_SCRIPTS = ("user_words.py", "session_context_loader.py", "status_guard.py", "reply_repeat_guard.py", "handoff_gate.py")
CLAUDE_RETIRED_SCRIPTS = ("background_status.py", "read_delegation_guard.py", "receipts_stop.py")


def _is_mnt_windows_path(path_str: Optional[str]) -> bool:
    """在 POSIX/WSL 下判断可执行文件路径是否位于 /mnt/<盘符>/ 挂载的 Windows 侧"""
    if os.name == "nt" or not path_str:
        return False
    return bool(re.match(r"^/mnt/[a-zA-Z]/", path_str.replace("\\", "/")))


def _is_wsl() -> bool:
    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except (OSError, UnicodeError):
        return False


# 二、路径安全、原子读写与 TOML / JSON 配置处理


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    tag = getattr(metadata, "st_reparse_tag", 0)
    if os.name == "nt" and tag:
        return tag in (
            getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
            getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
        )
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _same_nonlink_path(left: Path, right: Path) -> bool:
    if not left.is_absolute() or not right.is_absolute():
        return False
    if left == right:
        return True
    try:
        for path in (left, right):
            if any(_is_link_or_junction(item) for item in (*path.parents, path)):
                return False
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _validate_path(path: Path, *, directory: bool) -> Path:
    """逐级检查真实目录，拒绝路径跳转和目标文件硬链接。"""
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise ValueError(f"安装路径不能包含未解析的上级目录: {path}")
    for ancestor in (*reversed(path.parents), path):
        if os.name == "nt":
            if ancestor.name.endswith((".", " ")):
                raise ValueError(f"Windows 安装路径不能包含以空格或点结尾的名称: {ancestor}")
            if re.search(r'[<>:"|?*\x00-\x1f]', ancestor.name) or re.fullmatch(
                r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", ancestor.name, re.IGNORECASE,
            ):
                raise ValueError(f"Windows 安装路径包含保留名称或非法字符: {ancestor}")
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


def _validate_tree_links(root: Path) -> None:
    """在镜像同步和缓存清理前检查整棵目录树中的链接。"""
    _validate_path(root, directory=True)
    if not root.is_dir():
        return

    def fail(error: OSError) -> None:
        raise error

    for parent, dirs, files in os.walk(root, onerror=fail, followlinks=False):
        for name in dirs + files:
            entry = Path(parent) / name
            if _is_link_or_junction(entry):
                raise ValueError(f"安装目标包含链接或目录联接，请先核对: {entry}")


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


class FileChange(NamedTuple):
    path: Path
    original: Optional[bytes]
    content: bytes


def _read_optional_bytes(path: Path) -> Optional[bytes]:
    path = _validate_path(path, directory=False)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _decode_utf8(path: Path, raw: bytes) -> str:
    """接受 UTF-8 BOM，遇到损坏编码时在错误中标明原文件。"""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(f"{path} 不是有效的 UTF-8，原文件已保留。") from exc


def _atomic_write(change: FileChange) -> None:
    """先核对旧内容，再原子替换，避免中断留下半截文件或覆盖同期修改。"""
    path = _validate_path(change.path, directory=False)
    original, content = change.original, change.content
    if _read_optional_bytes(path) != original:
        raise ValueError(f"{path} 在安装期间发生变更，请重新运行安装器。")
    if content == original:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_path(path, directory=False)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        _validate_path(path, directory=False)
        if _read_optional_bytes(path) != original:
            raise ValueError(f"{path} 在安装期间发生变更，请重新运行安装器。")
        _validate_path(temp_path, directory=False)
        if original is not None:
            temp_path.chmod(stat.S_IMODE(path.stat().st_mode))
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            # Windows 无法删除带只读属性的临时文件。
            _validate_path(temp_path, directory=False)
            temp_path.chmod(stat.S_IMODE(temp_path.stat().st_mode) | stat.S_IWUSR)
            temp_path.unlink()


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


def _preserve_text_format(content: bytes, original: Optional[bytes]) -> bytes:
    """重新生成 JSON 等文本时沿用原文件的 BOM 和换行格式。"""
    previous = original or b""
    if b"\r\n" in previous:
        content = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return (UTF8_BOM if previous.startswith(UTF8_BOM) else b"") + content


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 中包含非法数值 {value}")


def _reject_duplicate_json_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 键重复: {key}")
        result[key] = value
    return result


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
        # JSON 数字溢出及未配对的 Unicode 代理项也须在写入前检出。
        _json_bytes(result)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"无法安全合并 {path}，原文件已保留: {exc}") from exc
    return result


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", r"\u007f")


def _toml_key_parts(source: str) -> Tuple[str, ...]:
    table = tomllib.loads(f"[{source}]\n")
    parts = []
    while table:
        key, table = next(iter(table.items()))
        parts.append(key)
    return tuple(parts)


def _parse_toml(source: str, path: Path) -> Dict[str, Any]:
    try:
        return tomllib.loads(source)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError) as exc:
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


def _toml_inline_table_location(
    line: str, start: int, path: Tuple[str, ...],
) -> Optional[Tuple[int, int, Tuple[str, ...]]]:
    """沿嵌套内联表找到插入位置，缺失的子表可用点分键补齐。"""
    end = _toml_inline_table_end(line, start)
    if end is None:
        return None
    if not path:
        return start, end, ()
    cursor = start + 1
    while cursor < end:
        assignment = TOML_ASSIGNMENT.match(line[cursor:end])
        if assignment is None:
            break
        keys = _toml_key_parts(assignment[2])
        value_start = cursor + assignment.end(1)
        if path[:len(keys)] == keys and line[value_start] == "{":
            return _toml_inline_table_location(line, value_start, path[len(keys):])
        cursor = value_start
        depth = 0
        while cursor < end:
            char = line[cursor]
            if char in "\"'":
                string = re.match(TOML_STRING, line[cursor:])
                if string is None:
                    return None
                cursor += string.end()
                continue
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            elif char == "," and depth == 0:
                cursor += 1
                break
            cursor += 1
    return start, end, path


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
    """保留注释、BOM 与换行，在原 TOML 文本中补缺或替换指定字符串。"""

    def __init__(self, path: Path):
        self.path = path
        _validate_path(path, directory=False)
        self.original = _read_optional_bytes(path)
        self.source = _decode_utf8(path, self.original or b"")
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
        inline_parent = next((
            path[:depth] for depth in range(len(path), 0, -1)
            if path[:depth] in assignments and assignments[path[:depth]][1][3].startswith("{")
        ), None)
        if not path:
            lines[0:0] = missing_lines
        elif path in tables:
            insert = tables[path] + 1
            if not lines[insert - 1].endswith("\n"):
                lines[insert - 1] += self.newline
            lines[insert:insert] = missing_lines
        elif inline_parent is not None:
            index, assignment = assignments[inline_parent]
            line = lines[index]
            location = _toml_inline_table_location(line, assignment.end(1), path[len(inline_parent):])
            if location is None:
                raise ValueError(f"{self.path} 的内联表 {'.'.join(path)} 需要手工补齐缺失配置")
            start, end, remaining = location
            if remaining:
                missing = [".".join(remaining) + "." + entry for entry in missing]
            insert = len(line[:end].rstrip(" \t"))
            separator = ", " if line[start + 1:insert].strip() else " "
            padding = " " if insert == end else ""
            lines[index] = line[:insert] + separator + ", ".join(missing) + padding + line[insert:]
        else:
            # 如果已有点分键，沿其所在的显式父表补缺，避免重定义点分键建立的表。
            existing = any(key[:len(path)] == path for key in assignments)
            if existing:
                parent = next((path[:depth] for depth in range(len(path) - 1, 0, -1) if path[:depth] in tables), ())
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
        if (self.original or b"").startswith(UTF8_BOM):
            updated = UTF8_BOM + updated
        return FileChange(self.path, self.original, updated) if updated != (self.original or b"") else None

    def set_integer(self, key: str, value: int) -> None:
        if key not in self.data:
            self.add_defaults((), {key: str(value)})
        elif self.data[key] != value:
            lines, _, assignments = self._locations()
            location = assignments.get((key,))
            if location is None or not re.fullmatch(r"[0-9_]+[ \t]*(?:#.*)?", location[1][3]):
                raise ValueError(f"{self.path} 的 {key} 不是可安全替换的非负整数")
            index, assignment = location
            suffix = re.sub(r"^[0-9_]+", "", assignment[3])
            ending = lines[index][len(lines[index].rstrip("\r\n")):]
            lines[index] = assignment[1] + str(value) + suffix + ending
            self._update(lines)


# 三、客户端、WSL 互操作与 Python / Zotero 探测


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
    elif name == "codex":
        codex_dir = local_app / "OpenAI" / "Codex"
        cli = [home / ".local" / "bin" / "codex.exe", home / ".cargo" / "bin" / "codex.exe"]
        desktop = [codex_dir / "Codex.exe"]
        try:
            if codex_dir.is_dir():
                cli += sorted(codex_dir.glob("bin/**/codex.exe"), reverse=True)
        except OSError:
            pass
    elif name == "claude":
        cli = [home / ".local" / "bin" / f"claude{suffix}" for suffix in (".exe", "")]
        cli += [home / ".npm-global" / "bin" / f"claude{suffix}" for suffix in (".cmd", ".exe")]
        cli += [home / "AppData" / "Roaming" / "npm" / "claude.cmd"]
        desktop = []
    elif name == "cc-switch":
        cli = [local_app / "Programs" / "cc-switch" / "cc-switch.exe",
               Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "CC Switch" / "cc-switch.exe"]
        desktop = []
    else:
        return None, None
    return _first_executable(cli, windows=True), _first_executable(desktop, windows=True)


def _native_cli(name: str, home: Path) -> Optional[str]:
    if os.name == "nt":
        local_app = Path(os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local"))
        local_cli, _ = _windows_app_binaries(home, local_app, name)
        return local_cli or shutil.which(f"{name}.exe") or shutil.which(name)
    candidates = [home / ".local" / "bin" / name]
    if name in {"agy", "codex"}:
        candidates.insert(0, home / (".gemini" if name == "agy" else ".cargo") / "bin" / name)
    if name == "claude":
        candidates.append(home / ".npm-global" / "bin" / name)
    candidates += [Path("/usr/bin") / name, Path("/usr/local/bin") / name]
    found = _first_executable(candidates) or shutil.which(name)
    return found if found and not _is_mnt_windows_path(found) and not found.lower().endswith(".exe") else None


def _is_windows() -> bool:
    return os.name == "nt"


def _find_windows_codex_package() -> Tuple[Optional[str], Optional[str]]:
    """通过注册表只读读取当前用户的 Codex 应用包位置，不启动外部进程且零磁盘写入。"""
    if not _is_windows():
        return None, None
    try:
        import winreg

        key_path = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
        locations: List[Tuple[Tuple[int, ...], str]] = []
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as root_key:
            count = winreg.QueryInfoKey(root_key)[0]
            for idx in range(count):
                try:
                    sub_name = winreg.EnumKey(root_key, idx)
                except OSError:
                    continue
                if not sub_name.lower().startswith("openai.codex_"):
                    continue
                try:
                    with winreg.OpenKey(root_key, sub_name) as sub_key:
                        loc, _ = winreg.QueryValueEx(sub_key, "PackageRootFolder")
                except OSError:
                    continue
                if isinstance(loc, str) and loc.strip():
                    parts = sub_name.split("_")
                    ver = tuple(int(part) for part in parts[1].split(".") if part.isdigit()) if len(parts) > 1 else ()
                    locations.append((ver, loc.strip()))
        for _, location in sorted(locations, reverse=True):
            app = Path(location) / "app"
            cli = _first_executable([app / "resources" / "codex.exe"], windows=True)
            desktop = _first_executable([app / "Codex.exe"], windows=True)
            if cli or desktop:
                return cli, desktop
    except (ImportError, OSError, ValueError):
        pass
    return None, None


def _probe_wsl_windows_profile_dir() -> Optional[Path]:
    """以 UTF-16 读取 Windows 用户主目录，再用 wslpath 转为本侧路径。"""
    if not _is_wsl():
        return None
    cmd_exe = shutil.which("cmd.exe")
    wslpath = shutil.which("wslpath")
    if not cmd_exe or not wslpath:
        return None
    try:
        profile = subprocess.run(
            [cmd_exe, "/d", "/u", "/c", "echo", "%USERPROFILE%"],
            cwd=str(Path(cmd_exe).parent),
            capture_output=True,
            text=True,
            encoding="utf-16-le",
            timeout=5,
        )
        win_profile = profile.stdout.strip().lstrip("\ufeff")
        if profile.returncode != 0 or not PureWindowsPath(win_profile).is_absolute():
            return None
        converted = subprocess.run(
            [wslpath, "-u", win_profile],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        wsl_home = converted.stdout.strip()
        if converted.returncode != 0 or not wsl_home:
            return None
        profile_path = Path(wsl_home)
        return profile_path if profile_path.is_absolute() and profile_path.is_dir() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _wsl_distro_list(wsl: str, *options: str) -> Optional[List[str]]:
    try:
        result = subprocess.run([wsl, "--list", *options, "--quiet"], capture_output=True, timeout=5)
        if result.returncode != 0:
            return None
        # 不同版本的 wsl.exe 重定向输出可能是 UTF-16 或 UTF-8。
        if result.stdout.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        else:
            encoding = "utf-16-le" if b"\0" in result.stdout else "utf-8-sig"
        names = [line.strip().lstrip("\ufeff").strip() for line in result.stdout.decode(encoding).splitlines()]
        return [name for name in names if name]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _probe_windows_wsl() -> Dict[str, Any]:
    """只检查已运行发行版，避免只读探测启动已停止的 WSL 环境。"""
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
        'for name in agy codex claude cc-switch; do '
        '  bin=$(command -v "$name"); '
        '  for candidate in "$HOME/.gemini/bin/$name" "$HOME/.local/bin/$name" "$HOME/.cargo/bin/$name"; do '
        '    if [ -f "$candidate" ] && [ -x "$candidate" ]; then bin=$candidate; break; fi; '
        '  done; '
        '  case "$bin" in /*) case "$bin" in /mnt/[a-zA-Z]/*|*.exe) bin=;; esac;; *) bin=;; esac; '
        '  trace=false; '
        '  if [ "$name" = agy ]; then '
        '    [ -d "$HOME/.gemini/config" ] || [ -d "$HOME/.gemini/antigravity-cli" ] || [ -d "$HOME/.gemini/antigravity" ]; '
        '  elif [ "$name" = codex ]; then [ -d "$HOME/.codex" ]; '
        '  elif [ "$name" = claude ]; then [ -d "$HOME/.claude" ]; '
        '  else [ -f "$HOME/.cc-switch/cc-switch.db" ]; fi && trace=true; '
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
                if len(fields) == 3 and fields[0] in {"agy", "codex", "claude", "cc-switch"}:
                    name, binary, trace = fields
                    result["apps"].setdefault(name, []).append(
                        {"distro": distro, "binary": binary or None, "config_dir_exists": trace == "true"}
                    )
            if any(not any(item["distro"] == distro for item in result["apps"].get(name, [])) for name in ("agy", "codex")):
                complete = False
        except (OSError, ValueError, subprocess.SubprocessError):
            complete = False
    result["status"] = "checked" if complete else "unknown"
    return result


def detect_installed_apps() -> Dict[str, Any]:
    """
    只读探测 Antigravity、Codex、Claude Code 与 CC Switch（区分本侧原生与跨子系统互操作）。
    """
    home = Path.home()
    gemini_root = home / ".gemini"
    configured_codex = os.environ.get("CODEX_HOME", "").strip()
    codex_root = Path(configured_codex).expanduser() if configured_codex else (home / ".codex")
    claude_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    cc_db = home / ".cc-switch" / "cc-switch.db"

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
            desktop = _first_executable([Path("/usr/bin/antigravity"), Path("/opt/Antigravity/antigravity")])

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
                config_name = {"agy": ".gemini", "codex": ".codex", "claude": ".claude", "cc-switch": ".cc-switch"}[name]
                trace = (windows_home / config_name).is_dir()
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
    cl = app_info("claude", claude_root.is_dir())
    cc = app_info("cc-switch", cc_db.is_file())
    ag["target_plugin_dir"] = str(gemini_root / "config" / "plugins" / "user-guard")
    cx["target_codex_home"] = str(codex_root)
    cl["target_claude_home"] = str(claude_root)
    cc.update(db_path=str(cc_db), db_exists=cc_db.is_file())
    ag_native, cx_native = ag["native_installed"], cx["native_installed"]
    cl_native = cl["native_installed"]

    if cl_native and (ag_native or cx_native):
        recommended = "all"
    elif ag_native and cx_native:
        recommended = "both"
    elif ag_native:
        recommended = "antigravity"
    elif cx_native:
        recommended = "codex"
    elif cl_native:
        recommended = "claude"
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
        "claude": cl,
        "cc_switch": cc,
        "zotero_mcp_detected": zotero_exe,
        "recommended_app_mode": recommended,
        "single_app_only_on_host": (
            sum(bool(info["native_installed"]) for info in (ag, cx, cl)) == 1
            and all(info["native_installed"] or info["interop_status"] == "not_detected" for info in (ag, cx, cl))
        ),
    }


def _hook_python_candidates() -> List[Tuple[str, str, List[str]]]:
    """从安装器自己的 PATH 和当前解释器发现候选，不读取钩子注册表。"""
    candidates: List[Tuple[str, str, List[str]]] = []
    path_dirs = []
    for entry in os.get_exec_path():
        directory = Path(entry.strip().strip('"') if os.name == "nt" else entry)
        if directory.is_absolute():
            path_dirs.append(directory)
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
             "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def detect_hook_python_command(*, antigravity: bool = False) -> str:
    """生成宿主钩子可调用的 Python 命令。Windows 反重力要求 PATH 前缀。"""
    flags = " ".join(HOOK_PYTHON_FLAGS)
    for name, executable, launcher_args in _hook_python_candidates():
        if not name:
            continue
        shell_path = shutil.which(name)
        if shell_path and _same_nonlink_path(Path(shell_path), Path(executable)) and _is_working_python_cmd(executable, launcher_args):
            return " ".join([name, *launcher_args, flags])
    if antigravity and os.name == "nt":
        raise ValueError("Windows 反重力钩子需要 PATH 中可执行的 Python 3.11+（python.exe 或 py.exe -3）。")
    executable = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    return f"{executable} {flags}"


def detect_zotero_mcp_executable(explicit_cmd: str = "") -> Optional[str]:
    """动态探测本机是否存在可用的 zotero-mcp 可执行文件（零个人路径硬编码）。"""
    if explicit_cmd:
        explicit_path = Path(explicit_cmd).expanduser()
        if explicit_path.is_file():
            return str(explicit_path.resolve())
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
    candidates: List[Path] = []
    try:
        for venv_dir in sorted(codex_tools_dir.glob("zotero-mcp*"), reverse=True):
            candidates.extend(venv_dir / ".venv" / subdir / name
                              for subdir in ("Scripts", "bin") for name in exe_names)
    except OSError:
        pass

    # 2. 扫描常见 pipx / uv / user local bin 路径
    common_bins = [
        home / ".local" / "bin",
        home / "pipx" / "venvs" / "zotero-mcp" / ("Scripts" if os.name == "nt" else "bin"),
        home / ".local" / "pipx" / "venvs" / "zotero-mcp" / ("Scripts" if os.name == "nt" else "bin"),
    ]
    candidates.extend(directory / name for directory in common_bins for name in exe_names)
    found = _first_executable(candidates, windows=os.name == "nt")
    if found:
        return str(Path(found).resolve())

    # 3. 兼容从本机 ~/.codex/config.toml 中读取已验证存在的 zotero 路径
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    codex_root = Path(configured_home).expanduser() if configured_home else home / ".codex"
    codex_cfg = codex_root / "config.toml"
    if codex_cfg.is_file():
        try:
            cfg = _parse_toml(codex_cfg.read_bytes().decode("utf-8-sig"), codex_cfg)
            command = cfg.get("mcp_servers", {}).get("zotero", {}).get("command")
            if isinstance(command, str):
                candidate = Path(command).expanduser()
                if candidate.is_absolute() and _first_executable([candidate], windows=os.name == "nt"):
                    return str(candidate.resolve())
        except (AttributeError, OSError, ValueError, RuntimeError):
            pass

    return None


# 四、反重力插件镜像、MCP 与默认模型同步


def should_ignore_template_item(path: Path) -> bool:
    return any(part in IGNORED_NAMES for part in path.parts) or path.suffix.lower() in IGNORED_SUFFIXES


def _template_files(template_dir: Path) -> List[Path]:
    return [
        item for item in sorted(template_dir.rglob("*"))
        if not should_ignore_template_item(item.relative_to(template_dir)) and item.is_file()
    ]


def _validate_mirror(template_dir: Path, target_dir: Path) -> None:
    """写入和清理共用一次完整校验，拒绝目录重叠及源、目标树中的链接。"""
    _validate_path(template_dir, directory=True)
    if not template_dir.is_dir():
        raise ValueError(f"找不到安装模板目录 {template_dir}")
    _validate_path(target_dir, directory=True)
    source, target = template_dir.resolve(), target_dir.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError(f"安装目标与模板目录重叠: {target_dir}")
    _validate_tree_links(template_dir)
    _validate_tree_links(target_dir)
    for item in _template_files(template_dir):
        _validate_path(target_dir / item.relative_to(template_dir), directory=False)


def mirror_sync_plugin(
    template_dir: Path, target_plugin_dir: Path, dry_run: bool = False, *, hooks_bytes: Optional[bytes] = None,
) -> None:
    """
    将 template_dir 镜像同步至 target_plugin_dir，并精准清理陈旧废弃文件与 __pycache__。
    保留运行态历史归档（如 hooks/state/*_user_words.md 与 hooks/logs/lifecycle.log），但清除废弃 *_stop_gate.json。
    """
    _validate_mirror(template_dir, target_plugin_dir)
    files = _template_files(template_dir)
    expected_rel_files = {item.relative_to(template_dir) for item in files}
    pending_updates: List[Tuple[Path, FileChange]] = []
    for src in files:
        rel = src.relative_to(template_dir)
        dst = target_plugin_dir / rel
        original = _read_optional_bytes(dst)
        content = hooks_bytes if hooks_bytes is not None and rel == Path("hooks.json") else src.read_bytes()
        if original != content:
            pending_updates.append((src, FileChange(dst, original, content)))

    if dry_run:
        if pending_updates:
            print(f"[Dry-Run] 将同步 {len(pending_updates)} 个变更文件至: {target_plugin_dir}")
            for source, change in pending_updates:
                print(f"  [SYNC] {change.path.relative_to(target_plugin_dir)}")
        else:
            print(f"[*] 目录已是最新状态，无需写入: {target_plugin_dir}")
    else:
        target_plugin_dir.mkdir(parents=True, exist_ok=True)
        for source, change in pending_updates:
            _atomic_write(change)
            if change.original is None:
                change.path.chmod(stat.S_IMODE(source.stat().st_mode))

    if not target_plugin_dir.exists():
        return

    stale_files: List[Path] = []
    stale_dirs: List[Path] = []

    for existing in sorted(target_plugin_dir.rglob("*")):
        rel = existing.relative_to(target_plugin_dir)
        if existing.is_dir():
            if existing.name == "__pycache__" and not any(parent.name == "__pycache__" for parent in rel.parents):
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

    for stale_file in stale_files:
        rel = stale_file.relative_to(target_plugin_dir)
        if dry_run:
            print(f"  [Dry-Run CLEAN] 将清理过时文件: {rel}")
        else:
            _validate_path(stale_file, directory=False)
            stale_file.unlink(missing_ok=True)
            print(f"  [-] 已清理过时残留文件: {rel}")

    for stale_dir in stale_dirs:
        rel = stale_dir.relative_to(target_plugin_dir)
        if dry_run:
            print(f"  [Dry-Run CLEAN] 将清理缓存目录: {rel}")
        else:
            _validate_path(stale_dir, directory=True)
            _validate_tree_links(stale_dir)
            shutil.rmtree(stale_dir)
            print(f"  [-] 已清理字节码缓存目录: {rel}")

    if not dry_run:
        for parent, _, _ in os.walk(target_plugin_dir, topdown=False, followlinks=False):
            directory = Path(parent)
            rel = directory.relative_to(target_plugin_dir)
            if directory == target_plugin_dir or (template_dir / rel).is_dir():
                continue
            _validate_path(directory, directory=True)
            if not any(directory.iterdir()):
                directory.rmdir()
                print(f"  [-] 已清理过时空目录: {rel}")


def _plan_user_zotero(config_root: Path, zotero_exe: Optional[str]) -> Optional[FileChange]:
    """仅补齐缺失的 Zotero 条目，已有远程或本地连接沿用用户配置。"""
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
    return FileChange(path, raw, _preserve_text_format(_json_bytes(data), raw))


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
        match = re.match(r"^(.+)-(minimal|low|medium|high|xhigh|max|ultra)$", item["slug"].lower())
        if match and match.group(2) != "ultra":
            tiered.append((match.group(1), match.group(2), item["display_name"]))
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


def _plan_agy_model(
    gemini_root: Path, *, dry_run: bool, overwrite: bool,
) -> List[FileChange]:
    """保留现有模型。预演仅读取本侧缓存，正式安装才查询原生 CLI。"""
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
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if models:
        snapshot = {
            "source": "agy_models_cli",
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "models": models,
        }
        changes.append(FileChange(cache_path, cache_raw, _preserve_text_format(_json_bytes(snapshot), cache_raw)))
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
        changes.append(FileChange(settings_path, settings_raw, _preserve_text_format(_json_bytes(settings), settings_raw)))
    return changes


def configure_agy_frontier_max_default(
    gemini_root: Path, dry_run: bool = False, *, overwrite: bool = False,
) -> None:
    """把已校验的模型变更逐文件原子落盘，预演只输出计划。"""
    for change in _plan_agy_model(gemini_root, dry_run=dry_run, overwrite=overwrite):
        _apply_file_change(change, dry_run)


def _agy_hooks_bytes(source: bytes, py_cmd: str) -> bytes:
    """以模板为输入生成目标平台命令，使重装比较最终内容。"""
    hooks_data = _read_json_object(Path("hooks.json"), source)
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


# 五、Codex 规则、只读子代理、钩子与沙箱配置同步


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


def _apply_codex_frontier_max(
    editor: "CodexConfig", cache_path: Path, *, overwrite: bool,
) -> None:
    """按本机模型目录补齐缺项，显式覆盖时才更新已有模型及挡位。"""
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
        cache = _read_json_object(cache_path, cache_path.read_bytes())
        if not isinstance(cache.get("models"), list):
            raise ValueError("模型目录的 models 必须是列表")
        models = [model for model in cache["models"]
                  if isinstance(model, dict) and isinstance(model.get("slug"), str) and model["slug"]]
    except (OSError, ValueError):
        print(f"[*] 未读取到有效的 Codex 模型目录 {cache_path}，默认模型设置保持原样。")
        return

    if cur_model and not overwrite:
        selected = next((model for model in models if model["slug"] == cur_model), None)
    else:
        visible = [model for model in models if model.get("visibility") != "hide"] or models
        if cur_effort and not overwrite:
            visible = [model for model in visible if cur_effort in _model_efforts(model)]
        selected = _latest_codex_model(visible, "sol") or _latest_codex_model(visible, "astra") or min(
            visible,
            key=lambda model: model["priority"] if type(model.get("priority")) is int else 9999,
            default=None,
        )
    if selected is None:
        print("[*] Codex 模型目录未找到与现有模型设置匹配的条目，已保留原配置。")
        return

    slug = selected["slug"]
    efforts = _model_efforts(selected)
    effort = cur_effort if cur_effort and not overwrite else max(efforts, key=EFFORT_ORDER.index, default="")
    if not effort:
        print(f"[*] Codex 模型 {slug} 缺少可用的非委派推理挡位元数据，未补齐挡位。")
        if overwrite and cur_effort:
            return

    for key, value in zip(keys, (slug, effort)):
        if value and value != values.get(key, "").strip():
            editor.set_string(key, value)
    maximum = _model_window(selected)
    if maximum is not None and (overwrite or "model_context_window" not in editor.data):
        editor.set_integer("model_context_window", maximum)


def _model_generation(slug: str) -> Tuple[int, ...]:
    match = re.search(r"(?:gpt|gemini)[- ](\d+(?:\.\d+)*)", slug, re.I)
    return tuple(map(int, match[1].split("."))) if match else ()


def _latest_codex_model(models: List[Dict[str, Any]], family: str) -> Optional[Dict[str, Any]]:
    candidates = [item for item in models if re.fullmatch(rf"gpt-\d+(?:\.\d+)*-{family}", item["slug"], re.I)
                  and item.get("visibility") != "hide"]
    return max(candidates, key=lambda item: _model_generation(item["slug"]), default=None)


def _model_window(model: Dict[str, Any]) -> Optional[int]:
    for key in ("max_context_window", "context_window"):
        value = model.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def _model_efforts(model: Dict[str, Any]) -> List[str]:
    levels = model.get("supported_reasoning_levels", model.get("supported_efforts", []))
    if not isinstance(levels, list):
        return []
    return [effort for level in levels for effort in [level.get("effort") if isinstance(level, dict) else level]
            if effort in EFFORT_ORDER and effort != "ultra"
            and not (isinstance(level, dict) and "delegation" in str(level.get("description", "")).lower())]


def _apply_codex_workspace_boundary(editor: "CodexConfig") -> None:
    sandbox_mode = editor.data.get("sandbox_mode")
    if sandbox_mode is None or sandbox_mode == "danger-full-access":
        sandbox_mode = "workspace-write"
        editor.set_string("sandbox_mode", sandbox_mode)
    editor.add_defaults((), {"approval_policy": '"on-request"'})
    if sandbox_mode == "workspace-write":
        editor.add_defaults(("sandbox_workspace_write",), {"network_access": "true"})


def _apply_codex_baseline(editor: "CodexConfig", include_sandbox: bool) -> None:
    """补齐工程配置基线，沿用用户已明确设置的开关。"""
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
    """先在内存中合并 TOML 并校验，双端安装可在写入前统一报错。"""
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
    _sync_global_rules(template_rules_path, codex_root / "AGENTS.md", "Codex", dry_run)


def _sync_global_rules(template_rules_path: Path, rules_path: Path, label: str, dry_run: bool) -> None:
    """更新工程规则，保留末尾自定义『## 工具与本机』段的原文及换行。"""
    if not template_rules_path.is_file():
        return
    template_text = _decode_utf8(template_rules_path, template_rules_path.read_bytes()).replace("\r\n", "\n")

    env_sec_re = re.compile(r"^##[ \t]+工具与([^\r\n]*)[^\S\n]*(?:\r?\n|$)", re.MULTILINE)
    template_env = env_sec_re.search(template_text)
    template_core = template_text[:template_env.start()].rstrip() if template_env else template_text.rstrip()

    codex_agents = rules_path
    original = _read_optional_bytes(codex_agents)
    existing = _decode_utf8(codex_agents, original or b"")

    user_env = env_sec_re.search(existing)
    has_custom_env = bool(user_env and user_env.group(1).strip() != "运行环境")
    newline = "\r\n" if "\r\n" in existing else "\n"
    if has_custom_env and user_env is not None:
        custom_tail = existing[user_env.start():]
        # 旧版安装器为尾段补一个末尾换行，继续兼容这一输出约定。
        if not custom_tail.endswith("\n"):
            custom_tail += newline
        merged = (template_core.replace("\n", newline) + newline * 2 if template_core else "") + custom_tail
    else:
        merged = (template_text.rstrip() + "\n").replace("\n", newline)

    if existing == merged:
        print(f"[*] {label} 全局规则 {rules_path.name} 已是最新状态。")
        return

    if dry_run:
        print(f"[Dry-Run] 将同步更新 {codex_agents} 全局规则")
        return

    content = (UTF8_BOM if (original or b"").startswith(UTF8_BOM) else b"") + merged.encode("utf-8")
    _atomic_write(FileChange(codex_agents, original, content))
    print(f"[+] 已同步更新 {label} 全局规则 {codex_agents}")


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


class ManagedCodexHook(NamedTuple):
    kind: str
    interpreter: Optional[Tuple[str, List[str]]]
    policy: Optional[Tuple[str, int]] = None


class CodexHooksPlan(NamedTuple):
    change: Optional[FileChange]
    mode: str
    compact_limit: int


def _codex_hook_tokens(handler: Dict[str, Any]) -> List[str]:
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        return []
    try:
        tokens = shlex.split(handler["command"], posix=(os.name != "nt"))
        return [token.strip('"') for token in tokens] if os.name == "nt" else tokens
    except ValueError:
        return []


def _codex_managed_hook(
    handler: Dict[str, Any], codex_root: Path, candidates: List[Tuple[str, str, List[str]]],
) -> Optional[ManagedCodexHook]:
    tokens = _codex_hook_tokens(handler)
    hook_root = codex_root / "hooks"

    def matches(token: str, path: Path) -> bool:
        try:
            return _same_nonlink_path(Path(token).expanduser(), path)
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
             if (path_prefix and _same_nonlink_path(Path(executable), Path(path)))
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
            if not resolved or not _same_nonlink_path(Path(resolved), Path(interpreter[0])):
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
        match = HANDOFF_MARKER.fullmatch(markers[0][1].removesuffix("\r"))
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
        ending = "\r" if markers[0][1].endswith("\r") else ""
        lines[markers[0][0]] = marker + ending
        doc["description"] = "\n".join(lines)
    else:
        newline = "\r\n" if "\r\n" in description else "\n"
        separator = newline if description and not description.endswith("\n") else ""
        doc["description"] = description + separator + marker
    return mode, limit


def _read_codex_hooks(path: Path, raw: Optional[bytes]) -> Dict[str, Any]:
    if raw is None:
        return {"hooks": {}}
    doc = _read_json_object(path, raw)
    if "hooks" in doc and not isinstance(doc["hooks"], dict):
        raise ValueError(f"无法安全合并 {path}，hooks 字段必须是对象。")
    return doc


def _plan_codex_hooks(
    hooks_path: Path, template_path: Path, py_cmd: str,
    mode: Optional[str] = None, compact_limit: Optional[int] = None,
) -> CodexHooksPlan:
    """逐条识别本项目钩子并合并，保留用户 handler、信任信息及交接策略。"""
    _validate_path(hooks_path, directory=False)
    codex_root = hooks_path.parent.resolve()
    original = _read_optional_bytes(hooks_path)
    doc = _read_codex_hooks(hooks_path, original)
    initial = copy.deepcopy(doc)
    defaults = _read_codex_hooks(template_path, template_path.read_bytes())["hooks"]
    events = doc.setdefault("hooks", {})
    codex_path = codex_root.as_posix()
    quote_path = (lambda path: '"' + path + '"') if os.name == "nt" else shlex.quote
    quote_exe = (
        (lambda exe: f'"{exe}"' if Path(exe).is_absolute() or any(ch.isspace() for ch in exe) else exe)
        if os.name == "nt" else shlex.quote
    )
    py_prefix_size = len(shlex.split(py_cmd, posix=(os.name != "nt")))

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
                    if handler["command"] != desired[kind]["command"]:
                        if managed.interpreter is None or not _is_working_python_cmd(*managed.interpreter):
                            updated_handler = {**handler, "command": desired[kind]["command"]}
                        else:
                            tokens = _codex_hook_tokens(handler)
                            launcher = managed.interpreter[1]
                            latest_args = _codex_hook_tokens(desired[kind])[py_prefix_size:]
                            if (
                                kind == "loader" and tokens[1:] != launcher + HOOK_PYTHON_FLAGS + latest_args
                            ) or (kind == "gate" and managed.policy != (mode, compact_limit)):
                                prefix = " ".join([quote_exe(tokens[0]), *launcher, *HOOK_PYTHON_FLAGS])
                                updated_handler = {**handler, "command": prefix + desired[kind]["command"][len(py_cmd):]}
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
        content = _preserve_text_format(_json_bytes(doc), original)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"无法安全合并 {hooks_path}，钩子注册表不能写成有效的 UTF-8 JSON: {exc}") from exc
    change = FileChange(hooks_path, original, content) if doc != initial else None
    return CodexHooksPlan(change, mode, compact_limit)


def _codex_hook_script_names(mode: str) -> List[str]:
    names = ["session_context_loader.py", "user_words.py"]
    if mode != "off":
        names.append("handoff_gate.py")
    return names


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
    _validate_path(pycache_dir, directory=True)
    _validate_tree_links(pycache_dir)
    if pycache_dir.is_dir():
        if dry_run:
            print(f"  [Dry-Run CLEAN] 将清理 Codex 钩子字节码缓存: {pycache_dir}")
        else:
            shutil.rmtree(pycache_dir)

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


def _validate_codex_assets(
    codex_root: Path, codex_template_dir: Path, args: argparse.Namespace, hooks_plan: Optional[CodexHooksPlan],
) -> None:
    """在双端任一目录写入前，检查目标路径及本次需要的全部 Codex 模板。"""
    _validate_path(codex_root / "config.toml", directory=False)
    if not args.no_codex_rules:
        source_rules = codex_template_dir.parent / "user-guard" / "rules" / "AGENTS.md"
        _decode_utf8(source_rules, _validate_path(source_rules, directory=False).read_bytes())
        rules = _validate_path(codex_root / "AGENTS.md", directory=False)
        _decode_utf8(rules, _read_optional_bytes(rules) or b"")
    if not args.no_codex_agents:
        source_agents = _validate_path(codex_template_dir / "agents", directory=True)
        if not source_agents.is_dir():
            raise ValueError(f"找不到 Codex 只读子代理模板目录 {source_agents}")
        _validate_path(codex_root / "agents", directory=True)
        for source in source_agents.glob("*.toml"):
            _decode_utf8(source, _validate_path(source, directory=False).read_bytes())
            _validate_path(codex_root / "agents" / source.name, directory=False)
    if hooks_plan is not None:
        hooks_dir = codex_root / "hooks"
        _validate_path(hooks_dir, directory=True)
        for name in _codex_hook_script_names(hooks_plan.mode):
            source = codex_template_dir.parent / "user-guard" / "hooks" / name
            _decode_utf8(source, _validate_path(source, directory=False).read_bytes())
            _validate_path(hooks_dir / name, directory=False)
        for rel in ("state", "state/handoff", "state/session-context", "state/user-words", "logs"):
            _validate_path(hooks_dir / rel, directory=True)
        _validate_path(hooks_dir / "logs" / "session-context.jsonl", directory=False)
        pycache = _validate_path(hooks_dir / "__pycache__", directory=True)
        _validate_tree_links(pycache)


# 六、Claude Code、路由器与 CC Switch 同步


def _sync_claude_global_rules(template_rules_path: Path, claude_root: Path, dry_run: bool = False) -> None:
    _sync_global_rules(template_rules_path, claude_root / "CLAUDE.md", "Claude Code", dry_run)


def _sync_claude_agents(claude_template_dir: Path, claude_root: Path, dry_run: bool = False) -> None:
    for source in sorted((claude_template_dir / "agents").glob("readonly-*.md")):
        target = claude_root / "agents" / source.name
        original = _read_optional_bytes(target)
        content = source.read_bytes()
        if original != content:
            _apply_file_change(FileChange(target, original, content), dry_run)


def _claude_hook_tokens(handler: Dict[str, Any]) -> List[str]:
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        return []
    args = handler.get("args")
    if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
        return [handler["command"].strip('"'), *args]
    return _codex_hook_tokens(handler)


def _claude_managed_script(handler: Dict[str, Any], claude_root: Path) -> Optional[str]:
    tokens = _claude_hook_tokens(handler)
    if len(tokens) < 2:
        return None
    exe_name = Path(tokens[0]).name.lower()
    if not re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", exe_name):
        return None
    idx = 2 if tokens[1:2] == ["-3"] else 1
    while idx < len(tokens):
        if tokens[idx] == "-X" and idx + 1 < len(tokens):
            idx += 2
        elif tokens[idx] in {"-I", "-B", "-u", "-S", "-E", "-s"}:
            idx += 1
        else:
            break
    if idx >= len(tokens):
        return None
    target = tokens[idx]
    name = Path(target).name
    if name in (*CLAUDE_HOOK_SCRIPTS, *CLAUDE_RETIRED_SCRIPTS):
        try:
            if _same_nonlink_path(Path(target).expanduser(), claude_root / "hooks" / name):
                return name
        except (OSError, RuntimeError, ValueError):
            return None
    return None


def _claude_handoff_policy(
    handlers: List[Dict[str, Any]], mode: Optional[str], compact_limit: Optional[int],
) -> Tuple[str, int]:
    policies = set()
    for handler in handlers:
        tokens = _claude_hook_tokens(handler)
        try:
            old_mode = tokens[tokens.index("--mode") + 1]
            old_limit = tokens[tokens.index("--compact-limit") + 1]
            if old_mode in {"advisory", "enforce", "off"} and re.fullmatch(r"[0-9]+", old_limit):
                policies.add((old_mode, int(old_limit)))
        except (ValueError, IndexError):
            continue
    modes, limits = {item[0] for item in policies}, {item[1] for item in policies}
    if (mode is None and len(modes) > 1) or (compact_limit is None and len(limits) > 1):
        raise ValueError("Claude 交接策略不一致，请显式指定 --claude-handoff-mode 和 --claude-handoff-compactions")
    return mode or next(iter(modes), "advisory"), compact_limit if compact_limit is not None else next(iter(limits), 0)


def _plan_claude_settings(
    settings_path: Path, template_path: Path, py_cmd: str,
    mode: Optional[str] = None, compact_limit: Optional[int] = None, *,
    include_hooks: bool = True, include_model: bool = True, overwrite_model: bool = False,
    verify_interpreters: bool = True,
) -> CodexHooksPlan:
    """按目标脚本路径迁移托管钩子，其余设置及第三方 handler 保持原值。"""
    original = _read_optional_bytes(settings_path)
    doc = _read_json_object(settings_path, original)
    initial = copy.deepcopy(doc)
    claude_root = settings_path.parent.resolve()
    env = doc.get("env", {})
    if not isinstance(env, dict):
        raise ValueError(f"{settings_path} 的 env 必须是对象")
    if include_model:
        if overwrite_model or "effortLevel" not in doc:
            doc["effortLevel"] = "xhigh"
        if overwrite_model:
            env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    if include_hooks:
        events = doc.setdefault("hooks", {})
        if not isinstance(events, dict):
            raise ValueError(f"{settings_path} 的 hooks 必须是对象")
        defaults = _read_json_object(template_path, template_path.read_bytes())["hooks"]
        managed = {}
        prefixes = {}
        verified = {}
        gates = []
        for event, groups in events.items():
            if not isinstance(groups, list) or any(
                not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
                or any(not isinstance(handler, dict) for handler in group["hooks"]) for group in groups
            ):
                raise ValueError(f"{settings_path} 的 {event} 必须包含钩子组与 handler 对象列表")
            for group in groups:
                for handler in group["hooks"]:
                    name = _claude_managed_script(handler, claude_root)
                    if name is None:
                        continue
                    managed[id(handler)] = name
                    if name == "handoff_gate.py":
                        gates.append(handler)
                    tokens = _claude_hook_tokens(handler)
                    executable = tokens[0]
                    launcher = ["-3"] if tokens[1:2] == ["-3"] else []
                    resolved = executable if Path(executable).is_absolute() else shutil.which(executable)
                    if resolved and verify_interpreters:
                        key = (resolved, tuple(launcher))
                        if key not in verified:
                            verified[key] = _is_working_python_cmd(resolved, launcher)
                        if verified[key]:
                            quote = ((lambda value: '"' + value + '"' if Path(value).is_absolute() or any(c.isspace() for c in value) else value)
                                     if os.name == "nt" else shlex.quote)
                            prefixes.setdefault(name, " ".join([quote(executable), *launcher, *HOOK_PYTHON_FLAGS]))
        # 关闭模式通过现有托管钩子中不含 gate 的状态沿用，无需添加非标准 settings 字段。
        modern = any("args" not in handler and "--client" in tokens
                     and tokens[2 if tokens[1:2] == ["-3"] else 1:][:4] == HOOK_PYTHON_FLAGS
                     for groups in events.values() for group in groups for handler in group["hooks"]
                     if id(handler) in managed for tokens in [_claude_hook_tokens(handler)])
        if mode is None and managed and modern and not gates:
            mode = "off"
        mode, compact_limit = _claude_handoff_policy(gates, mode, compact_limit)
        for event, groups in list(events.items()):
            retained = []
            canonical_matcher = defaults.get(event, [{}])[0].get("matcher")
            for group in groups:
                handlers = [handler for handler in group["hooks"] if id(handler) not in managed]
                canonical = event in defaults and group.get("matcher") == canonical_matcher
                if handlers or not group["hooks"] or canonical:
                    retained.append({**group, "hooks": handlers})
            if retained:
                events[event] = retained
            else:
                events.pop(event, None)
        quote_path = (lambda value: '"' + value + '"') if os.name == "nt" else shlex.quote
        for event, groups in defaults.items():
            default_group = groups[0]
            desired = []
            for handler in default_group["hooks"]:
                command = handler["command"]
                name = next(name for name in CLAUDE_HOOK_SCRIPTS if name in command)
                if name == "handoff_gate.py" and mode == "off":
                    continue
                command = command[command.index('"~/.claude/'):]
                command = re.sub(r'"~/.claude/([^"\n]+)"',
                                 lambda match: quote_path(claude_root.as_posix() + "/" + match[1]), command)
                command = command.replace("--mode advisory --compact-limit 0", f"--mode {mode} --compact-limit {compact_limit}")
                desired.append({**handler, "command": prefixes.get(name, next(iter(prefixes.values()), py_cmd)) + " " + command})
            if not desired:
                continue
            existing = events.setdefault(event, [])
            target = next((group for group in existing if group.get("matcher") == default_group.get("matcher")), None)
            if target is None:
                target = {key: value for key, value in default_group.items() if key != "hooks"}
                target["hooks"] = []
                existing.append(target)
            target["hooks"].extend(desired)
    else:
        mode, compact_limit = mode or "advisory", compact_limit or 0
    content = _preserve_text_format(_json_bytes(doc), original)
    return CodexHooksPlan(FileChange(settings_path, original, content) if doc != initial else None, mode, compact_limit)


def _claude_hook_script_names(mode: str) -> List[str]:
    return [name for name in CLAUDE_HOOK_SCRIPTS if mode != "off" or name != "handoff_gate.py"]


def _sync_claude_hooks(
    source_hooks: Path, claude_root: Path, plan: CodexHooksPlan, dry_run: bool = False,
) -> None:
    hooks_dir = claude_root / "hooks"
    for name in _claude_hook_script_names(plan.mode):
        target = hooks_dir / name
        original, content = _read_optional_bytes(target), (source_hooks / name).read_bytes()
        if original != content:
            _apply_file_change(FileChange(target, original, content), dry_run)
    retired = list(CLAUDE_RETIRED_SCRIPTS) + (["handoff_gate.py"] if plan.mode == "off" else [])
    for name in retired:
        target = _validate_path(hooks_dir / name, directory=False)
        if target.exists():
            if dry_run:
                print(f"[Dry-Run CLEAN] 将清理 Claude 废弃钩子 {target}")
            else:
                target.unlink()
    cache = hooks_dir / "__pycache__"
    _validate_tree_links(cache)
    if cache.is_dir():
        if dry_run:
            print(f"[Dry-Run CLEAN] 将清理 {cache}")
        else:
            shutil.rmtree(cache)
    _apply_file_change(plan.change, dry_run)


def _cc_router_files(template_dir: Path, claude_root: Path) -> List[Tuple[Path, Path]]:
    pairs = [(source, claude_root / "router" / source.name) for source in _template_files(template_dir)]
    primary = claude_root / "router" / "router.json"
    if not primary.exists():
        pairs.append((template_dir / ("router.windows.json" if os.name == "nt" else "router.wsl.json"), primary))
    return pairs


def _sync_cc_router(template_dir: Path, claude_root: Path, dry_run: bool = False) -> None:
    for source, target in _cc_router_files(template_dir, claude_root):
        original, content = _read_optional_bytes(target), source.read_bytes()
        if content != original:
            _apply_file_change(FileChange(target, original, content), dry_run)
        if not dry_run and target.suffix in {".sh", ".py"} and os.name != "nt":
            if stat.S_IMODE(target.stat().st_mode) != 0o755:
                target.chmod(0o755)


def _load_ccswitch_admin(repo_root: Path) -> ModuleType:
    source = repo_root / "template" / "claude" / "router" / "ccswitch_admin.py"
    module = ModuleType("installer_ccswitch_admin")
    module.__file__ = str(source)
    # 不通过导入缓存落盘，即使调用方没有启用 -B，体检也保持零写入。
    exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
    return module


def _validate_claude_assets(repo_root: Path, claude_root: Path, args: argparse.Namespace, mode: str) -> None:
    _validate_tree_links(claude_root)
    sources = []
    if not args.no_claude_rules:
        sources.append((repo_root / "template/user-guard/rules/AGENTS.md", claude_root / "CLAUDE.md"))
    if not args.no_claude_agents:
        sources.extend((source, claude_root / "agents" / source.name)
                       for source in (repo_root / "template/claude/agents").glob("readonly-*.md"))
    if not args.no_claude_hooks:
        sources.extend((repo_root / "template/user-guard/hooks" / name, claude_root / "hooks" / name)
                       for name in _claude_hook_script_names(mode))
    if args.with_cc_router:
        sources.extend(_cc_router_files(repo_root / "template/claude/router", claude_root))
    for source, target in sources:
        _decode_utf8(source, _validate_path(source, directory=False).read_bytes())
        original = _read_optional_bytes(target)
        if target.name == "CLAUDE.md":
            _decode_utf8(target, original or b"")


# 七、零写入升级体检


def _audit_cli(info: Dict[str, Any]) -> Dict[str, Any]:
    report = {key: info.get(key) for key in (
        "native_installed", "native_binary", "native_desktop_binary", "native_status", "interop_status",
    )}
    report["version"] = None
    binary = info.get("native_binary")
    if binary:
        try:
            cmd = (
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", binary, "--version"]
                if os.name == "nt" and str(binary).lower().endswith((".cmd", ".bat"))
                else [binary, "--version"]
            )
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
            match = re.search(r"\b\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.]+)?\b", proc.stdout or "")
            if proc.returncode == 0 and match:
                report["version"] = match[0]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return report


def _rules_core(raw: bytes) -> str:
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    return re.split(r"^##[ \t]+工具与[^\n]*", text, maxsplit=1, flags=re.MULTILINE)[0].rstrip()


def _audit_redact(value: Any, roots: List[Tuple[Path, str]], secrets: Set[str]) -> Any:
    if isinstance(value, dict):
        return {_audit_redact(key, roots, secrets): _audit_redact(item, roots, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_audit_redact(item, roots, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "<redacted>")
    for path, label in sorted(roots, key=lambda item: len(str(item[0])), reverse=True):
        for variant in {str(path), path.as_posix()}:
            value = re.sub(re.escape(variant), lambda match: label, value, flags=re.IGNORECASE if os.name == "nt" else 0)
    # URL 仅保留服务端点；查询串、片段与 userinfo 均不进入报告。
    def endpoint(match: re.Match) -> str:
        try:
            parsed = urlsplit(match[0])
            host = parsed.hostname or ""
            if ":" in host:
                host = "[" + host + "]"
            return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "") + parsed.path
        except ValueError:
            return "<redacted-url>"
    value = re.sub(r"https?://[^\s\"<>]+", endpoint, value)
    value = re.sub(r"(?i)(?:[A-Z]:[/\\]Users[/\\]|/(?:home|Users)/)[^/\\\s]+", "<home>", value)
    value = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[^\s]+)", "<redacted>", value)
    return value


def audit_upgrade_status(
    repo_root: Optional[Path] = None, target_plugin_dir: Optional[Path] = None,
    codex_root: Optional[Path] = None, claude_root: Optional[Path] = None,
    ccswitch_db: Optional[Path] = None, *, detection: Optional[Dict[str, Any]] = None,
    is_sandbox_run: bool = False, app_scope: str = "auto", expect_cc_router: bool = False,
) -> Dict[str, Any]:
    """只读本地资产、模型目录和卡片状态；不刷新目录、不修改配置。"""
    explicit_ag = target_plugin_dir is not None
    explicit_cx = codex_root is not None
    explicit_cl = claude_root is not None
    explicit_cc = ccswitch_db is not None
    repo_root = Path(repo_root or Path(__file__).resolve().parent)
    home = Path.home()
    target_plugin_dir = Path(target_plugin_dir or home / ".gemini/config/plugins/user-guard")
    codex_root = Path(codex_root or os.environ.get("CODEX_HOME") or home / ".codex")
    claude_root = Path(claude_root or os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    ccswitch_db = Path(ccswitch_db or home / ".cc-switch/cc-switch.db")
    target_plugin_dir, codex_root, claude_root, ccswitch_db = (
        path.expanduser().absolute() for path in (target_plugin_dir, codex_root, claude_root, ccswitch_db)
    )
    detection = detect_installed_apps() if detection is None else detection
    errors: Dict[str, str] = {}
    secrets: Set[str] = set()
    template = repo_root / "template/user-guard"

    def collect_secrets(doc: Any) -> None:
        if isinstance(doc, dict):
            for key, value in doc.items():
                if re.search(r"(?i)(?:auth|api.?key|secret|password|(?:^|_)token(?:$|_))", key):
                    if isinstance(value, str) and value not in {"via-router", "present", "missing"}:
                        secrets.add(value)
                collect_secrets(value)
        elif isinstance(doc, list):
            for item in doc:
                collect_secrets(item)

    def read_json(path: Path) -> Dict[str, Any]:
        try:
            result = _read_json_object(path, _read_optional_bytes(path))
            collect_secrets(result)
            return result
        except (OSError, ValueError, RuntimeError) as exc:
            errors[str(path)] = type(exc).__name__
            return {}

    def read_raw(path: Path) -> Optional[bytes]:
        try:
            return _read_optional_bytes(path)
        except (OSError, ValueError, RuntimeError) as exc:
            errors[str(path)] = type(exc).__name__
            return None

    def drift(root: Path, sources: List[Tuple[Path, str]], *, core_rules: bool = False) -> List[str]:
        result = []
        for source, relative in sources:
            actual = read_raw(root / relative)
            expected = source.read_bytes()
            try:
                equal = actual is not None and (_rules_core(actual) == _rules_core(expected) if core_rules and relative.endswith(".md") and "/" not in relative else actual == expected)
            except UnicodeError:
                equal = False
            if not equal:
                result.append(relative)
        return result

    repo_version = read_json(template / "plugin.json").get("version")
    ag = _audit_cli(detection.get("antigravity", {}))
    ag.update(plugin_exists=target_plugin_dir.is_dir(), installed_version=read_json(target_plugin_dir / "plugin.json").get("version"))
    version_tuple = lambda value: tuple(int(part) for part in re.findall(r"\d+", str(value or "")))
    ag["upgrade_recommended"] = version_tuple(ag["installed_version"]) < version_tuple(repo_version)
    ag_sources = [(source, source.relative_to(template).as_posix()) for source in _template_files(template)
                  if source.relative_to(template).parts[0] in {"rules", "hooks", "agents"}]
    ag["drift_files"] = drift(target_plugin_dir, ag_sources)
    installed_ag_hooks = read_json(target_plugin_dir / "hooks.json")
    try:
        lifecycle = installed_ag_hooks["user-guard-lifecycle"]
        first = next(iter(lifecycle.values()))[0]
        prefix = first.get("hooks", [first])[0]["command"].partition(" ./hooks/")[0]
        if installed_ag_hooks != json.loads(_agy_hooks_bytes((template / "hooks.json").read_bytes(), prefix)):
            ag["drift_files"].append("hooks.json")
    except (KeyError, IndexError, TypeError, ValueError):
        ag["drift_files"].append("hooks.json")
    gemini_root = target_plugin_dir.parent.parent.parent if target_plugin_dir.parent.name == "plugins" else target_plugin_dir.parent
    ag_settings = read_json(gemini_root / "antigravity-cli/settings.json")
    ag_desktop = read_json(gemini_root / "config/config.json").get("userSettings", {})
    desktop_model = ag_desktop.get("model") if isinstance(ag_desktop, dict) else None
    ag["configured_model"] = ag_settings.get("model") or desktop_model or read_json(gemini_root / "antigravity/settings.json").get("model")
    ag_models = read_json(gemini_root / "antigravity-cli/cache/cae_models_cache.json").get("models", [])
    if not ag_models:
        ag_models = read_json(gemini_root / "antigravity/cache/cae_models_cache.json").get("models", [])
    if not isinstance(ag_models, list):
        ag_models = []
    ag_models = [item for item in ag_models if isinstance(item, dict) and isinstance(item.get("slug"), str)]
    ag["model_catalog"] = [{"slug": item["slug"], "generation": list(_model_generation(item["slug"])),
                             "declared_max_context_window": _model_window(item), "supported_efforts": _model_efforts(item)} for item in ag_models]
    carbon = [item for item in ag_models if "carbon" in item["slug"].lower() and not item["slug"].endswith("-ultra")]
    latest_carbon = max((_model_generation(item["slug"]) for item in carbon), default=())
    carbon = [item for item in carbon if _model_generation(item["slug"]) == latest_carbon]
    ag["recommended_carbon_variants"] = [item["slug"] for item in carbon]
    ag["carbon_supported_efforts"] = sorted({item["slug"].rsplit("-", 1)[-1] for item in carbon
                                             if item["slug"].rsplit("-", 1)[-1] in EFFORT_ORDER}, key=EFFORT_ORDER.index)
    ag["retiring_models"] = [item["slug"] for item in ag_models if re.search(r"claude[- ]?4[.-]6|gpt[- ]oss[- ]120b", item["slug"], re.I)]
    if isinstance(ag["configured_model"], str) and re.search(r"claude[- ]?4[.-]6|gpt[- ]oss[- ]120b", ag["configured_model"], re.I):
        ag["retiring_models"].append(ag["configured_model"])
    ag["catalog_source"] = "local_cache"

    cx = _audit_cli(detection.get("codex", {}))
    config_path = codex_root / "config.toml"
    try:
        config = _parse_toml(_decode_utf8(config_path, read_raw(config_path) or b""), config_path)
        collect_secrets(config)
    except ValueError as exc:
        errors[str(config_path)] = type(exc).__name__
        config = {}
    cx.update({key: config.get(key) for key in ("model", "model_reasoning_effort", "model_context_window")})
    cx_models = read_json(codex_root / "models_cache.json").get("models", [])
    cx_models = [item for item in cx_models if isinstance(item, dict) and isinstance(item.get("slug"), str)] if isinstance(cx_models, list) else []
    sol, astra = _latest_codex_model(cx_models, "sol"), _latest_codex_model(cx_models, "astra")
    selected = sol or astra
    current = next((item for item in cx_models if item["slug"] == cx["model"]), None)
    family = "astra" if re.fullmatch(r"gpt-\d+(?:\.\d+)*-astra", str(cx["model"])) else "sol"
    latest = (astra if family == "astra" else sol) or selected
    cx["latest_sol_slug"], cx["latest_astra_slug"] = (sol or {}).get("slug"), (astra or {}).get("slug")
    cx["declared_max_context_window"] = _model_window(latest or {})
    cx["supported_efforts"] = _model_efforts(latest or {})
    reasons = []
    if latest is None:
        reasons.append("本地模型目录缺少 Sol / Astra，需刷新客户端目录后复查")
    else:
        if current is None or current["slug"] != latest["slug"]:
            reasons.append("当前默认模型与本地目录的最新同系列模型不一致")
        if cx["declared_max_context_window"] is not None and cx["model_context_window"] != cx["declared_max_context_window"]:
            reasons.append("声明上下文窗口未对齐")
        best_effort = max(cx["supported_efforts"], key=EFFORT_ORDER.index, default=None)
        if best_effort is None or cx["model_reasoning_effort"] != best_effort:
            reasons.append("思考强度未对齐目录中的最高单模型挡位")
    cx.update(model_aligned=not reasons, upgrade_recommended=bool(reasons), alignment_reasons=reasons, catalog_source="local_cache")
    codex_doc = read_json(codex_root / "hooks.json")
    marker = HANDOFF_MARKER.search(str(codex_doc.get("description", "")))
    codex_mode = marker[1] if marker else "advisory"
    cx_sources = [(template / "rules/AGENTS.md", "AGENTS.md")]
    cx_sources += [(source, "agents/" + source.name) for source in (repo_root / "template/codex/agents").glob("*.toml")]
    cx_sources += [(template / "hooks" / name, "hooks/" + name) for name in _codex_hook_script_names(codex_mode)]
    cx["drift_files"] = drift(codex_root, cx_sources, core_rules=True)
    if not isinstance(codex_doc.get("hooks"), dict) or not codex_doc["hooks"].get("SessionStart"):
        cx["drift_files"].append("hooks.json")

    cl = _audit_cli(detection.get("claude", {}))
    settings_path = claude_root / "settings.json"
    settings = read_json(settings_path)
    env = settings.get("env", {})
    env = env if isinstance(env, dict) else {}
    cl.update(effortLevel=settings.get("effortLevel"), locked_effort_env="CLAUDE_CODE_EFFORT_LEVEL" in env,
              model_mapping={key: env.get(key) for key in ("ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS")})
    claude_mode = "advisory"
    try:
        plan = _plan_claude_settings(settings_path, repo_root / "template/claude/hooks.json", "python3 -X utf8 -I -B",
                                    include_model=False, verify_interpreters=False)
        claude_mode = plan.mode
        expected_doc = json.loads(plan.change.content) if plan.change else settings
        def hook_layout(doc: Dict[str, Any]) -> List[Any]:
            result = []
            for event, groups in doc.get("hooks", {}).items():
                for group in groups:
                    for handler in group["hooks"]:
                        name = _claude_managed_script(handler, claude_root)
                        if name:
                            tokens = _claude_hook_tokens(handler)
                            prefix_end = 2 if tokens[1:2] == ["-3"] else 1
                            result.append((event, group.get("matcher"), name, tokens[prefix_end:], "args" in handler))
            return sorted(result, key=repr)
        cl["hooks_aligned"] = settings_path.is_file() and hook_layout(settings) == hook_layout(expected_doc)
    except (OSError, ValueError, TypeError, KeyError):
        cl["hooks_aligned"] = False
    cl_sources = [(template / "rules/AGENTS.md", "CLAUDE.md")]
    cl_sources += [(source, "agents/" + source.name) for source in (repo_root / "template/claude/agents").glob("readonly-*.md")]
    cl_sources += [(template / "hooks" / name, "hooks/" + name) for name in _claude_hook_script_names(claude_mode)]
    cl["drift_files"] = drift(claude_root, cl_sources, core_rules=True)
    cl["drift_files"].extend("hooks/" + name for name in CLAUDE_RETIRED_SCRIPTS if read_raw(claude_root / "hooks" / name) is not None)
    if not cl["hooks_aligned"]:
        cl["drift_files"].append("settings.json")
    router_sources = [(source, source.name) for source in _template_files(repo_root / "template/claude/router")]
    router_drift = drift(claude_root / "router", router_sources)
    cl["router"] = {"installed": (claude_root / "router/router.py").is_file(), "drift_files": router_drift,
                    "config_exists": (claude_root / "router/router.json").is_file(),
                    "template_hashes": {relative: hashlib.sha256(source.read_bytes()).hexdigest() for source, relative in router_sources}}

    if app_scope == "all":
        ag_active, cx_active, cl_active = True, True, True
    elif app_scope == "both":
        ag_active, cx_active, cl_active = True, True, False
    elif app_scope == "antigravity":
        ag_active, cx_active, cl_active = True, False, False
    elif app_scope == "codex":
        ag_active, cx_active, cl_active = False, True, False
    elif app_scope == "claude":
        ag_active, cx_active, cl_active = False, False, True
    elif explicit_ag or explicit_cx or explicit_cl:
        ag_active, cx_active, cl_active = explicit_ag, explicit_cx, explicit_cl
    else:
        ag_active = bool(ag.get("native_installed") or target_plugin_dir.is_dir())
        cx_active = bool(cx.get("native_installed") or codex_root.is_dir())
        cl_active = bool(cl.get("native_installed") or claude_root.is_dir())
        if not (ag_active or cx_active or cl_active):
            ag_active = cx_active = cl_active = True

    cc = _audit_cli(detection.get("cc_switch", {}))
    port = 8240 if os.name == "nt" else 8241
    card_uses_router = False
    try:
        admin = _load_ccswitch_admin(repo_root)
        cc.update(admin.inspect_status(ccswitch_db, settings_path))
        current_card = cc.get("current_claude_card") or {}
        card_uses_router = current_card.get("base_url") == f"http://127.0.0.1:{port}"
        if cl_active and (card_uses_router or (not is_sandbox_run and (expect_cc_router or cl["router"]["installed"]))):
            cc.update(admin.inspect_status(ccswitch_db, settings_path, router_port=port))
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        cc.update(db_exists=ccswitch_db.is_file(), schema_version=None, enabled_prompts=[], inspection_error=type(exc).__name__)
    cc["default_router_port"] = port
    router_active = cl_active and bool(
        expect_cc_router or cl["router"]["installed"] or cl["router"]["config_exists"] or card_uses_router
    )
    cc_active = cl_active and bool(not is_sandbox_run or explicit_cc or ccswitch_db.is_file())
    actions = []
    if ag_active:
        if ag["drift_files"] or ag["upgrade_recommended"]:
            actions.append("sync_antigravity_plugin")
        if ag["retiring_models"]:
            actions.append("review_antigravity_models")
    if cx_active:
        if cx["drift_files"]:
            actions.append("sync_codex")
        if cx["upgrade_recommended"]:
            actions.append("sync_codex_model_defaults" if latest else "refresh_codex_model_catalog")
    if cl_active:
        if cl["drift_files"]:
            actions.append("sync_claude")
        if cl["effortLevel"] != "xhigh" or cl["locked_effort_env"]:
            actions.append("sync_claude_model_defaults")
    if router_active and (router_drift or not cl["router"]["config_exists"]):
        actions.append("sync_cc_router")
    if cc_active:
        if cc.get("enabled_prompts"):
            actions.append("disable_ccswitch_prompts")
        if cc.get("settings_aligned") is False:
            actions.append("align_ccswitch_card")
        if (card_uses_router or expect_cc_router) and cc.get("router_health") and not cc["router_health"].get("ok"):
            actions.append("start_cc_router")
        if cc.get("inspection_error"):
            actions.append("inspect_ccswitch_schema")
    if errors:
        actions.append("repair_unreadable_configs")
    report = {"plugin_version": repo_version,
              "host": {"platform": sys.platform, "python_version": sys.version.split()[0], "is_wsl": detection.get("is_wsl"), "wsl_probe": detection.get("wsl_probe")},
              "antigravity": ag, "codex": cx, "claude": cl, "cc_switch": cc,
              "summary": {"all_aligned": not actions, "recommended_actions": actions}, "read_errors": errors}
    roots = [(home, "~"), (target_plugin_dir, "<antigravity>"), (codex_root, "<codex>"), (claude_root, "<claude>"), (repo_root, "<repo>")]
    return _audit_redact(report, roots, secrets)


# 八、CLI 参数、预演与主安装流程


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
        answer = input(f"是否不安装 {missing_label}，直接单独配置本机 {installed_label} 独立工作流？[Y/n]: ").strip().lower()
    except EOFError:
        return True
    if answer in ("n", "no", "0", "false"):
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


def resolve_multi_app_plan(
    app_arg: str, has_explicit_ag: bool, has_explicit_cx: bool, has_explicit_cl: bool,
    detection: Dict[str, Any], interactive: bool, auto_yes: bool, dry_run: bool,
) -> Tuple[bool, bool, bool]:
    explicit = {"all": (True, True, True), "both": (True, True, False),
                "antigravity": (True, False, False), "codex": (False, True, False), "claude": (False, False, True)}
    if app_arg in explicit:
        return explicit[app_arg]
    if has_explicit_ag or has_explicit_cx or has_explicit_cl:
        return bool(has_explicit_ag), bool(has_explicit_cx), bool(has_explicit_cl)
    names = ("antigravity", "codex", "claude")
    installed = tuple(bool(detection.get(name, {}).get("native_installed")) for name in names)
    if sum(installed) >= 2:
        return installed
    if not any(installed):
        print("[!] 未检测到本侧客户端，请先安装客户端或显式指定 --app。", file=sys.stderr)
        sys.exit(2)
    missing = [name for name, native in zip(names, installed) if not native]
    if any(detection.get(name, {}).get("interop_available") or detection.get(name, {}).get("interop_status") == "unknown" for name in missing):
        print("[*] 对侧客户端可用或状态未确认，本次只配置已检测到的本侧客户端。")
        return installed
    label = names[installed.index(True)]
    if not _confirm_single_app_install(label, " / ".join(missing), interactive, auto_yes, dry_run):
        sys.exit(0)
    return installed


def main() -> None:
    """先解析参数、完成各端预检，再按预演或正式安装路径执行。"""
    parser = argparse.ArgumentParser(description="Antigravity、Codex 与 Claude Code 统一工作流安装器")
    if sys.version_info < (3, 11):
        parser.error("需要 Python 3.11 或更高版本")
    parser.add_argument(
        "--app",
        choices=("auto", "all", "both", "antigravity", "codex", "claude"),
        default="auto",
        help="auto 按已安装集合配置；both 仅反重力加 Codex；all 配置三端；也可单独指定客户端",
    )
    parser.add_argument("--probe-apps", action="store_true", help="只读探测反重力、Codex、Claude Code、CC Switch 与 Zotero，输出 JSON")
    parser.add_argument("--audit-upgrade", action="store_true", help="零写入全栈升级与本地模型目录对齐体检，输出 JSON")
    parser.add_argument("-y", "--yes", action="store_true", help="非交互自动确认（仅检测到单客户端时直接单独配置已安装的一端）")
    parser.add_argument("--target-dir", type=str, default="",
                        help="自定义反重力插件目录。配置 Zotero 时须采用 <配置目录>/plugins/user-guard 布局")
    parser.add_argument("--codex-home", type=str, default="", help="自定义 Codex 主配置目录（默认 $CODEX_HOME 或 ~/.codex，用于沙箱全量验证）")
    parser.add_argument("--codex-skills-dir", default="", help="仅自定义 Codex 技能根目录（其下安装 cae-harness，兼容隔离验证）")
    parser.add_argument("--claude-home", default="", help="Claude Code 主目录（默认 $CLAUDE_CONFIG_DIR 或 ~/.claude）")
    parser.add_argument("--no-claude-rules", action="store_true", help="跳过 Claude 全局 CLAUDE.md")
    parser.add_argument("--no-claude-agents", action="store_true", help="跳过 Claude 只读子代理")
    parser.add_argument("--no-claude-hooks", action="store_true", help="跳过 Claude 钩子同步")
    parser.add_argument("--claude-handoff-mode", choices=("advisory", "enforce", "off"), help="首次默认 advisory，升级沿用已有策略")
    parser.add_argument("--claude-handoff-compactions", type=int, help="首次默认 0，升级沿用已有阈值")
    parser.add_argument("--with-cc-router", action="store_true", help="同步 Claude 本地路由器并保留已有 router.json")
    parser.add_argument("--ccswitch-db", default="", help="自定义 CC Switch 数据库路径")
    parser.add_argument("--disable-ccswitch-prompts", action="store_true", help="在线备份并禁用陈旧提示词，支持预演")
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
    if args.claude_handoff_compactions is not None and args.claude_handoff_compactions < 0:
        parser.error("--claude-handoff-compactions 必须为非负整数")

    repo_root = Path(__file__).resolve().parent
    is_sandbox_run = bool(args.target_dir or args.codex_home or args.codex_skills_dir or args.claude_home)
    if args.audit_upgrade:
        report = audit_upgrade_status(
            repo_root, Path(args.target_dir) if args.target_dir else None,
            Path(args.codex_home) if args.codex_home else None,
            Path(args.claude_home) if args.claude_home else None,
            Path(args.ccswitch_db) if args.ccswitch_db else None, is_sandbox_run=is_sandbox_run,
            app_scope=args.app, expect_cc_router=args.with_cc_router,
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    if args.probe_apps:
        try:
            detection = detect_installed_apps()
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(detection, indent=2, ensure_ascii=False))
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
    if args.app == "claude" and (args.target_dir or args.codex_home or args.codex_skills_dir):
        parser.error("--app claude 不能指定反重力或 Codex 目标目录")
    if args.app in {"codex", "antigravity", "both"} and args.claude_home:
        parser.error(f"--app {args.app} 不能指定 --claude-home")
    if args.app == "all" and is_sandbox_run and not (args.target_dir and (args.codex_home or args.codex_skills_dir) and args.claude_home):
        parser.error("--app all 自定义安装必须同时提供 --target-dir、Codex 目录与 --claude-home")

    repo_root = Path(__file__).resolve().parent
    template_dir = repo_root / "template" / "user-guard"
    codex_template_dir = repo_root / "template" / "codex"
    claude_template_dir = repo_root / "template" / "claude"
    tests_dir = repo_root / "tests"

    if not template_dir.is_dir():
        print(f"[!] 错误：找不到反重力插件模板目录 {template_dir}", file=sys.stderr)
        sys.exit(1)

    if not args.skip_test and not args.dry_run and not tests_dir.is_dir():
        print(f"[!] 错误：找不到测试套件目录 {tests_dir}", file=sys.stderr)
        sys.exit(1)

    has_explicit_ag = bool(args.target_dir)
    has_explicit_cx = bool(args.codex_home or args.codex_skills_dir)
    has_explicit_cl = bool(args.claude_home)
    try:
        detection = detect_installed_apps() if args.app == "auto" and not (has_explicit_ag or has_explicit_cx or has_explicit_cl) else {}
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    is_interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    install_ag, install_cx, install_cl = resolve_multi_app_plan(
        args.app, has_explicit_ag=has_explicit_ag, has_explicit_cx=has_explicit_cx, has_explicit_cl=has_explicit_cl,
        detection=detection,
        interactive=is_interactive,
        auto_yes=args.yes,
        dry_run=args.dry_run,
    )

    default_config_root = Path.home() / ".gemini" / "config"
    configured_codex_env = os.environ.get("CODEX_HOME", "").strip()
    default_codex_root = Path(configured_codex_env).expanduser() if configured_codex_env else (Path.home() / ".codex")
    default_claude_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    claude_root = Path(args.claude_home).expanduser().absolute() if args.claude_home else default_claude_root.absolute()
    claude_root = claude_root if install_cl else None
    ccswitch_db = Path(args.ccswitch_db).expanduser().absolute() if args.ccswitch_db else Path.home() / ".cc-switch/cc-switch.db"
    disable_prompts = args.disable_ccswitch_prompts and (not is_sandbox_run or bool(args.ccswitch_db))
    if args.with_cc_router and not install_cl:
        parser.error("--with-cc-router 需要选择 Claude 安装")

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

    zotero_exe: Optional[str] = None
    if not args.no_zotero and (not is_sandbox_run or args.zotero_cmd):
        try:
            zotero_exe = detect_zotero_mcp_executable(args.zotero_cmd)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
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
    claude_settings_plan = None
    ccswitch_admin = None
    try:
        agy_config_root = None
        if target_plugin_dir is not None:
            target_plugin_dir = validate_install_target(target_plugin_dir, repo_root)
            for forbidden_codex in {default_codex_root.resolve(), (Path.home().resolve() / ".codex"), default_claude_root.resolve()}:
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
            if codex_root == default_claude_root.resolve() or default_claude_root.resolve() in codex_root.parents:
                raise ValueError("Codex 目录不能位于 Claude 配置目录内")
            if not args.no_codex_skill:
                target_skill_dir = codex_root / "skills" / "cae-harness"
        if target_skill_dir is not None:
            target_skill_dir = validate_install_target(target_skill_dir, repo_root)
            _validate_mirror(template_dir / "skills" / "cae-harness", target_skill_dir)
        if claude_root is not None:
            claude_root = validate_codex_home_target(claude_root, repo_root)
            if claude_root == default_codex_root.resolve() or default_codex_root.resolve() in claude_root.parents:
                raise ValueError("Claude 目录不能位于 Codex 配置目录内")
        targets = [path for path in (target_plugin_dir, codex_root, claude_root, target_skill_dir) if path is not None]
        for index, left in enumerate(targets):
            for right in targets[index + 1:]:
                if left == codex_root and right == target_skill_dir:
                    continue  # Codex 自带的 skills/cae-harness 是明确约定的从属资产。
                if left == right or left in right.parents or right in left.parents:
                    raise ValueError("不同客户端的安装目标不能相互包含")
        if agy_config_root is not None:
            mcp_path = agy_config_root / "mcp_config.json"
            if mcp_path in {codex_root, target_skill_dir}:
                raise ValueError(f"反重力 Zotero 配置文件与 Codex 目标目录冲突: {mcp_path}")
            agy_zotero_change = _plan_user_zotero(agy_config_root, zotero_exe)
        if target_plugin_dir is not None or (codex_root is not None and not args.no_codex_hooks) or (claude_root is not None and not args.no_claude_hooks):
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
        if claude_root is not None:
            claude_settings_plan = _plan_claude_settings(
                claude_root / "settings.json", claude_template_dir / "hooks.json", py_cmd or "", args.claude_handoff_mode,
                args.claude_handoff_compactions, include_hooks=not args.no_claude_hooks,
                include_model=not args.no_model_config, overwrite_model=args.sync_model_defaults,
            )
            _validate_claude_assets(repo_root, claude_root, args, claude_settings_plan.mode)
        if disable_prompts:
            _validate_path(ccswitch_db, directory=False)
            ccswitch_admin = _load_ccswitch_admin(repo_root)
            # 先只读验证 schema，再写入客户端资产；预演不备份、不改库。
            ccswitch_admin.disable_prompts(ccswitch_db, dry_run=True)
        if target_plugin_dir == (default_config_root / "plugins" / "user-guard").absolute() and not args.no_model_config:
            agy_model_changes = _plan_agy_model(
                default_config_root.parent, dry_run=args.dry_run, overwrite=args.sync_model_defaults,
            )
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    mode_desc = " + ".join(name for name, enabled in (("反重力", install_ag), ("Codex", install_cx), ("Claude Code", install_cl)) if enabled)

    print("=" * 80)
    print("  Antigravity、Codex 与 Claude Code 统一工作流安装程序")
    print("=" * 80)
    print(f"[*] 仓库根目录   : {repo_root}")
    print(f"[*] 部署目标模式 : {mode_desc}")
    if py_cmd is not None:
        print(f"[*] 钩子运行命令 : {py_cmd}")
    if target_plugin_dir is not None:
        print(f"[*] 反重力插件路径: {target_plugin_dir}")
    if codex_root is not None:
        print(f"[*] Codex 主目录 : {codex_root}")
    if claude_root is not None:
        print(f"[*] Claude 主目录: {claude_root}")
    if target_skill_dir is not None:
        print(f"[*] Codex 技能路径: {target_skill_dir}")
    print("-" * 80)

    try:
        # 部署反重力侧。
        if target_plugin_dir is not None:
            mirror_sync_plugin(template_dir, target_plugin_dir, dry_run=args.dry_run, hooks_bytes=agy_hooks)
            _apply_file_change(agy_zotero_change, args.dry_run)
            for change in agy_model_changes:
                _apply_file_change(change, args.dry_run)

        # 部署 Codex 侧。
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
        if claude_root is not None:
            if not args.no_claude_rules:
                _sync_claude_global_rules(template_dir / "rules/AGENTS.md", claude_root, args.dry_run)
            if not args.no_claude_agents:
                _sync_claude_agents(claude_template_dir, claude_root, args.dry_run)
            if not args.no_claude_hooks:
                _sync_claude_hooks(template_dir / "hooks", claude_root, claude_settings_plan, args.dry_run)
            else:
                _apply_file_change(claude_settings_plan.change, args.dry_run)
            if args.with_cc_router:
                _sync_cc_router(claude_template_dir / "router", claude_root, args.dry_run)
        if disable_prompts:
            result = ccswitch_admin.disable_prompts(ccswitch_db, dry_run=args.dry_run)
            print(f"[*] CC Switch 提示词处理: {len(result['disabled'])} 条；备份: {result['backup']}")
        elif args.disable_ccswitch_prompts:
            print("[*] 沙箱未显式指定 --ccswitch-db，跳过宿主提示词数据库。")
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"[!] 配置同步失败: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n[+] Dry-Run 演练检查完成，未修改磁盘文件。")
        return

    if not args.skip_test:
        print("\n" + "=" * 80)
        print("  正在执行全量无字节码 (-B) 自动化测试回归...")
        print("=" * 80)

        env = os.environ.copy()
        env.pop("USER_GUARD_STATE_DIR", None)
        env.pop("USER_GUARD_LOGS_DIR", None)
        env.pop("USER_GUARD_INSTALLED_CODEX_DIR", None)
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
        try:
            result = subprocess.run(test_cmd, env=env, cwd=str(repo_root), timeout=REGRESSION_TIMEOUT_SECONDS)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"\n[!] 自动化回归测试执行失败: {exc}", file=sys.stderr)
            sys.exit(1)

        if result.returncode != 0:
            print("\n[!] 自动化回归测试未全部通过，请检查上方输出日志。", file=sys.stderr)
            sys.exit(result.returncode)

    print("\n" + "=" * 80)
    if args.skip_test:
        print("  指定目标配置同步完成，本次已按参数跳过回归测试。")
    else:
        print("  指定目标配置同步完成，全量离线回归测试通过。")
    print("=" * 80)


if __name__ == "__main__":
    main()

"""
status_guard.py - 反重力 PreToolUse 项目目录硬边界、长会话交接门禁与防空转轮询门禁 (user-guard)

核心职责：
1. 项目目录外文件改动硬约束（R4 硬门禁，防在业务盘乱建目录与越权篡改配置）：
   - 拦截 write_file / edit_file 对当前项目根目录外的业务目录或全局配置文件写入；
   - 通过内联 Python AST 语法树分析与 Shell 子句命令头精确配对，拦截 run_command 向项目目录外创建 git worktree（如 ../.codex-worktrees）、写删改外部路径或执行全局生产安装（忽略只读打开、数组 .copy()、.md 文件名与外部解释器调用，杜绝误报）；
   - 自动放行系统标准临时目录、宿主与项目关联的对话记忆/子代理轨迹/制品草稿（brain / plugin_data / memories / sessions 等）、标准工具缓存（~/.cache）及 Windows<->WSL 同项目挂载路径；
   - 交互主会话触发真实越界时返回 force_ask，在弹窗清晰列出“当前任务意图 + 具体写删动作 + 项目外目标路径 + 触发语句”请求用户明确批准；无头跨端子任务（CAE_HARNESS_DEPTH=1）直接硬 deny。
2. 反重力专有长会话两阶段交接停机门禁（联动 handoff_gate.py）：
   - 在 drain/freeze 收口期内严禁新开子代理/swarm/schedule/browser/cae_harness 派工、禁止读图及 git push，按排空预算管控写/运行动作并无阻断放行只读核对、交接档写入与本地 git add/commit。
3. 防空转轮询门禁（适配 Antigravity Reactive Wakeup 响应式唤醒机制）：
   - read_file 读取算例/任务证据文件（将 StartLine/EndLine/ByteOffset 编入查询键，放行正常长文件翻页阅读）；
   - run_command 使用只读命令查看证据文件（将归一化命令参数编入查询键，放行变参检索）；
   - manage_task (Action == "status") 轮询后台任务状态（自动联动 .system_generated/tasks/<TaskId>.log 真实物理指纹，有新输出自动放行，杜绝永久锁死）。
"""

import ast
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import handoff_gate  # noqa: E402
import user_words  # noqa: E402

user_words.ensure_utf8_stdio()

# 证据类扩展名与严格锚定文件名正则（防止误伤业务源码如 status.json / test_result.json）
EVIDENCE_EXTENSIONS = {".log", ".out", ".err", ".output"}
EVIDENCE_FILENAME_RE = re.compile(
    r"^(?:"
    r"journal[^.]*\.jsonl|"
    r"output\.[^.]+|"
    r"task[-_]?output[^.]*(?:\.[^.]+)?|"
    r"results?(?:\.(?:txt|json|jsonl|dat|csv))?|"
    r"task\.(?:json|txt|status)|"
    r"wf_[^.]+\.json"
    r")$",
    re.IGNORECASE,
)

READONLY_COMMANDS = {
    "cat",
    "tail",
    "head",
    "get-content",
    "gc",
    "type",
    "grep",
    "rg",
    "select-string",
    "sls",
}

REMOTE_OR_SUBSYSTEM_WRAPPERS = {"wsl", "wsl.exe", "ssh", "ssh.exe", "docker", "docker.exe", "podman"}
SHELL_WRAPPERS = {"bash", "sh", "zsh", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "env"}

LARGE_FILE_THRESHOLD = 4 * 1024 * 1024  # 4 MB
CHUNK_SAMPLE_BYTES = 1 * 1024 * 1024    # 1 MB
TASK_NO_LOG_COOLDOWN_SEC = 30.0
MAX_LOG_BYTES = user_words.MAX_LOG_BYTES
TRIM_LOG_BYTES = user_words.TRIM_LOG_BYTES

DENY_REASON_TEXT = (
    "【user-guard 防空转门禁阻断】目标证据文件内容与查询参数自上次读取后未发生任何变化。"
    "反重力具备后台任务就绪通知机制（Reactive Wakeup），严禁在此原地空转轮询！"
    "请等待后台任务完成通知或先推进其他工作；文件产生新输出、或改变读取区间/查询参数后将自动放行。"
)


def get_data_dirs() -> Tuple[Path, Path]:
    return user_words.resolve_data_dirs(HOOKS_DIR)


def get_current_iso_time() -> str:
    return user_words.current_iso_time()


def log_event(event_name: str, conversation_id: str, message: str) -> None:
    user_words.append_lifecycle_log(HOOKS_DIR, event_name, conversation_id, message)


def is_evidence_file(file_path_str: str) -> bool:
    """严格判定路径是否属于算例/后台任务输出证据文件"""
    if not file_path_str:
        return False

    cleaned = file_path_str.strip().strip("\"'")
    if not cleaned or cleaned.startswith("-"):
        return False

    norm_slashes = cleaned.replace("\\", "/").lower()
    if "/.system_generated/tasks/" in norm_slashes or "/.system_generated/steps/" in norm_slashes:
        return True

    p = Path(cleaned)
    name = p.name
    ext = p.suffix.lower()

    if ext in EVIDENCE_EXTENSIONS:
        return True
    if EVIDENCE_FILENAME_RE.match(name):
        return True
    return False


def _is_inside_dir(target: Path, parent: Path) -> bool:
    try:
        target.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _safe_split_tokens(segment: str) -> List[str]:
    try:
        tokens = shlex.split(segment, posix=(os.name != "nt"))
    except ValueError:
        tokens = segment.split()
    return [t.strip("\"'") for t in tokens if t.strip("\"'")]


def _unwrap_inner_command(tokens: List[str]) -> Tuple[List[str], bool]:
    """
    剥离前导 env / timeout / bash -c / powershell -Command / wsl 等包装器，
    返回 (核心命令 token 列表, 是否经过 wsl/ssh/docker 跨子系统包装)。
    """
    idx = 0
    used_subsystem = False
    n = len(tokens)

    while idx < n:
        head_raw = tokens[idx]
        head_name = Path(head_raw.replace("\\", "/")).name.lower()

        # 跳过形如 KEY=VALUE 的环境变量前缀
        if "=" in head_raw and not head_raw.startswith("-") and head_name != head_raw.lower():
            idx += 1
            continue
        if "=" in head_raw and not head_raw.startswith("-") and "/" not in head_raw and "\\" not in head_raw:
            idx += 1
            continue

        if head_name == "timeout":
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                idx += 1
            if idx < n:
                idx += 1  # 跳过秒数参数
            continue

        if head_name in REMOTE_OR_SUBSYSTEM_WRAPPERS:
            used_subsystem = True
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                flag = tokens[idx]
                idx += 1
                if flag in ("-d", "--distribution", "-u", "--user", "--cd") and idx < n:
                    idx += 1
            continue

        if head_name in SHELL_WRAPPERS:
            idx += 1
            while idx < n and tokens[idx].lower() in ("-c", "/c", "-command", "-noprofile", "-noni", "-noninteractive"):
                flag_low = tokens[idx].lower()
                idx += 1
                if flag_low in ("-c", "/c", "-command") and idx < n:
                    inner_tokens = _safe_split_tokens(" ".join(tokens[idx:]))
                    sub_tokens, sub_remote = _unwrap_inner_command(inner_tokens)
                    return sub_tokens, (used_subsystem or sub_remote)
            continue

        break

    return tokens[idx:], used_subsystem


def parse_run_command_evidence(command_line: str) -> Tuple[Optional[str], bool, str]:
    """
    解析命令行是否属于对证据文件的只读查看。
    返回 (证据文件路径或 None, 是否跨子系统命令, 归一化命令签名)。
    """
    if not command_line or not command_line.strip():
        return None, False, ""

    norm_cmd = re.sub(r"\s+", " ", command_line.strip())
    if os.name == "nt":
        norm_cmd_sig = norm_cmd.casefold()
    else:
        norm_cmd_sig = norm_cmd

    # 若含重定向写入或修改型操作符，非纯只读查询，直接放行
    if re.search(r"(?:>>|>|\btee\b|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i\b)", command_line):
        return None, False, norm_cmd_sig

    segments = re.split(r"\s*(?:\|\||&&|\||;)\s*", command_line.strip())
    any_subsystem = False

    for seg in segments:
        if not seg:
            continue
        raw_tokens = _safe_split_tokens(seg)
        if not raw_tokens:
            continue
        tokens, used_sub = _unwrap_inner_command(raw_tokens)
        any_subsystem = any_subsystem or used_sub
        if not tokens:
            continue

        cmd_head = Path(tokens[0].replace("\\", "/")).name.lower()
        if cmd_head.endswith(".exe"):
            cmd_head = cmd_head[:-4]

        if cmd_head not in READONLY_COMMANDS:
            continue

        for tok in tokens[1:]:
            if not tok or tok.startswith("-"):
                continue
            if is_evidence_file(tok):
                return tok, any_subsystem, norm_cmd_sig

    return None, any_subsystem, norm_cmd_sig


def resolve_target_path(
    raw_path_str: str,
    cwd: Optional[str],
    workspace_paths: Optional[List[str]],
) -> Path:
    """严格按 payload.cwd -> workspacePaths 优先级解析相对路径，绝不落入插件自身工作目录"""
    cleaned = os.path.expanduser(raw_path_str.strip().strip("\"'"))
    p = Path(cleaned)
    if p.is_absolute():
        return p

    search_bases: List[Path] = []
    if cwd and str(cwd).strip():
        search_bases.append(Path(str(cwd).strip()))
    if workspace_paths:
        for wp in workspace_paths:
            if wp and str(wp).strip():
                search_bases.append(Path(str(wp).strip()))

    for base in search_bases:
        cand = base / p
        if cand.exists():
            return cand

    if search_bases:
        return search_bases[0] / p
    return p


def _canonical_path_key(p: Path) -> str:
    try:
        resolved = str(p.resolve())
    except OSError:
        resolved = str(p)
    return resolved.casefold() if os.name == "nt" else resolved


def compute_file_fingerprint(target_path: Path) -> Optional[Dict[str, Any]]:
    """
    计算证据文件的快速复合指纹：
    - <= 4MB: 全量 SHA256 + size + mtime_ns
    - >  4MB: 头部 1MB + 尾部 1MB 分块 SHA256 + size + mtime_ns（防 GB 级算例日志阻塞超时）
    """
    path_key = _canonical_path_key(target_path)
    try:
        resolved_str = str(target_path.resolve())
    except OSError:
        resolved_str = str(target_path)

    if not target_path.exists():
        return {
            "pathKey": path_key,
            "path": resolved_str,
            "exists": False,
            "mtimeNs": 0,
            "size": 0,
            "sha256": "non_existent",
        }

    if not target_path.is_file():
        return None

    try:
        st = target_path.stat()
        fsize = st.st_size
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))

        hasher = hashlib.sha256()
        with open(target_path, "rb") as f:
            if fsize <= LARGE_FILE_THRESHOLD:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)
            else:
                hasher.update(f.read(CHUNK_SAMPLE_BYTES))
                f.seek(max(0, fsize - CHUNK_SAMPLE_BYTES))
                hasher.update(f.read(CHUNK_SAMPLE_BYTES))

        return {
            "pathKey": path_key,
            "path": resolved_str,
            "exists": True,
            "mtimeNs": mtime_ns,
            "size": fsize,
            "sha256": hasher.hexdigest(),
        }
    except Exception:
        return None


def _is_fingerprint_equal(rec: Dict[str, Any], fp: Dict[str, Any]) -> bool:
    return (
        rec.get("exists") == fp["exists"]
        and rec.get("mtimeNs") == fp["mtimeNs"]
        and rec.get("size") == fp["size"]
        and rec.get("sha256") == fp["sha256"]
    )


def _load_guard_state(state_file: Path, conversation_id: str) -> Dict[str, Any]:
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if (isinstance(data, dict) and data.get("version") == 2
                    and data.get("conversationId") == conversation_id and isinstance(data.get("records"), dict)):
                return data
            raise ValueError("轮询状态格式或会话身份错误")
        except (ValueError, OSError) as exc:
            log_event("PreToolUse:poll_state_error", conversation_id, f"轮询状态读取失败（{exc}），保留原文件")
            raise
    return {"version": 2, "conversationId": conversation_id, "records": {}}


def _save_guard_state(state_file: Path, state: Dict[str, Any], conversation_id: str) -> None:
    state["version"] = 2
    state["conversationId"] = conversation_id
    state["updatedAt"] = get_current_iso_time()
    user_words.atomic_write_json(state_file, state)


def _locate_task_log_file(transcript_path_str: Optional[str], task_id: str) -> Optional[Path]:
    """根据会话 transcriptPath 定位 Antigravity 后台任务日志 .system_generated/tasks/<TaskId>.log"""
    if not transcript_path_str or not task_id:
        return None
    tp = Path(transcript_path_str)
    # 标准布局: <brain>/<convId>/.system_generated/logs/transcript.jsonl
    sg_dir = tp.parent.parent
    candidates = [
        sg_dir / "tasks" / f"{task_id}.log",
        sg_dir / "tasks" / task_id,
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return candidates[0]


# ==============================================================================
# 项目目录外文件改动硬边界门禁 (Workspace Boundary Hard Guard - R4)
# ==============================================================================

_NULL_SINKS = {"/dev/null", "nul", "$null"}
_LOCAL_VAR_TOKEN_RE = re.compile(r"(?:\$\{?(?:tmp|temp|tmpdir|out|dst|work|stage)\w*\}?|%(?:tmp|temp)\w*%)", re.IGNORECASE)
_COMMIT_MSG_RE = re.compile(
    r"""(\bgit\b[^\r\n;|&]*?\bcommit\b[^\r\n;|&]*?(?:\s-(?:m|am)|--message(?:=|\s+)))(?:"(?:\\.|[^"])*"|'(?:''|[^'])*'|\S+)""",
    re.IGNORECASE,
)
_GIT_WORKTREE_ADD_RE = re.compile(r"\bgit\b[^\r\n;|&]*?\bworktree\s+add\b([^\r\n;|&]+)", re.IGNORECASE)
_INSTALLER_INVOKE_RE = re.compile(
    r"(?:^|[;&|\r\n]\s*)(?:[A-Za-z_]\w*=\S+\s+)*"
    r"(?:(?:wsl(?:\.exe)?|bash|sh|pwsh(?:\.exe)?|powershell(?:\.exe)?|cmd(?:\.exe)?|"
    r"python[0-9.]*(?:\.exe)?|py(?:\.exe)?\s+-3)\b[^\r\n;|&]*?[\s\"'\\/]+|(?:\.[\\/]))"
    r"(?<![A-Za-z0-9_.-])install\.(?:py|ps1|sh)\b\"?'?([^\r\n;|&]*)",
    re.IGNORECASE,
)

_PY_INLINE_HEAD_RE = re.compile(
    r"""(?:"[^"\r\n]*[\\/]|'[^'\r\n]*[\\/]|[^\s"';|&()]+[\\/])?\bpython[0-9.]*(?:\.exe)?\b["']?"""
    r"(?:\s+(?:-[BIuEsSqvV]+|-X\s+\S+|-W\s+\S+))*\s+-c\s+",
    re.IGNORECASE,
)
_PS_HERESTRING_PY_RE = re.compile(
    r"@(['\"])\r?\n([\s\S]*?)\r?\n\1@\s*\|\s*[^\r\n|]*\bpython[0-9.]*(?:\.exe)?\b",
    re.IGNORECASE,
)
_SH_HEREDOC_PY_RE = re.compile(
    r"\bpython[0-9.]*(?:\.exe)?\b[^\r\n]*<<-?\s*['\"]?([A-Za-z0-9_]+)['\"]?\r?\n([\s\S]*?)\r?\n\1(?=\r?\n|$)",
    re.IGNORECASE,
)
_DOTNET_IO_MUTATE_RE = re.compile(
    r"\[(?:System\.)?IO\.(File|Directory)\]::"
    r"(Write\w*|Append\w*|Create\w*|Delete\w*|Move\w*|Copy\w*)\s*\(([^)\r\n]+)\)",
    re.IGNORECASE,
)
_SHELL_REDIRECT_RE = re.compile(
    r"""(?:^|[\s;|&()])(?:\d+)?(>{1,2})(?![>&=])\s*("(?:\\.|[^"])+"|'(?:''|[^'])+'|[^\s;|&<>()]+)""",
)

_SHELL_DELETE_VERBS = {"rm", "rmdir", "del", "erase", "remove-item"}
_SHELL_CREATE_WRITE_VERBS = {
    "mkdir",
    "md",
    "touch",
    "truncate",
    "tee",
    "new-item",
    "set-content",
    "add-content",
    "clear-content",
    "out-file",
}
_SHELL_MOVE_VERBS = {"mv", "move", "ren", "rename", "move-item", "rename-item"}
_SHELL_COPY_VERBS = {"cp", "copy", "xcopy", "robocopy", "copy-item"}
_PS_NON_PATH_VALUE_FLAGS = {
    "-value",
    "-itemtype",
    "-type",
    "-encoding",
    "-filter",
    "-include",
    "-exclude",
    "-delimiter",
    "-stream",
}


def _expand_host_vars(raw_str: str) -> str:
    s = raw_str.strip().strip("\"'")
    if not s:
        return ""
    home_str = str(Path.home())
    env_map = {
        "$env:userprofile": os.environ.get("USERPROFILE") or home_str,
        "%userprofile%": os.environ.get("USERPROFILE") or home_str,
        "$env:home": home_str,
        "$home": home_str,
        "${home}": home_str,
        "$env:appdata": os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming"),
        "%appdata%": os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming"),
        "$env:localappdata": os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"),
        "%localappdata%": os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"),
        "$env:temp": tempfile.gettempdir(),
        "%temp%": tempfile.gettempdir(),
        "$env:tmp": tempfile.gettempdir(),
        "%tmp%": tempfile.gettempdir(),
    }
    low = s.lower()
    for prefix, val in env_map.items():
        if low == prefix:
            return val
        if low.startswith(prefix + "/") or low.startswith(prefix + "\\"):
            return val.rstrip("/\\") + "/" + s[len(prefix) + 1 :]
    return os.path.expanduser(s)


def _normalize_cross_env_lexical(raw_str: str, base_dir: Optional[str] = None) -> str:
    """将 Windows 盘符路径、WSL /mnt/<drive> 路径与 POSIX 路径统一为可跨端等价比较的规范键"""
    expanded = _expand_host_vars(raw_str)
    if not expanded:
        return ""
    slashes = expanded.replace("\\", "/")

    is_win_drive = bool(re.match(r"^[A-Za-z]:(?:/|$)", slashes))
    is_unc = slashes.startswith("//")
    is_posix_abs = slashes.startswith("/") and not is_unc

    if not (is_win_drive or is_unc or is_posix_abs) and base_dir:
        base_norm = _normalize_cross_env_lexical(base_dir, None)
        if base_norm:
            slashes = base_norm.rstrip("/") + "/" + slashes
            is_posix_abs = slashes.startswith("/") and not slashes.startswith("//")
            is_unc = slashes.startswith("//")

    m_drive = re.match(r"^([A-Za-z]):(?:/(.*))?$", slashes)
    if m_drive:
        drv = m_drive.group(1).lower()
        rest = m_drive.group(2) or ""
        norm = posixpath.normpath(f"/mnt/{drv}/{rest}")
        return norm.casefold()

    m_mnt = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", slashes)
    if m_mnt:
        drv = m_mnt.group(1).lower()
        rest = m_mnt.group(2) or ""
        norm = posixpath.normpath(f"/mnt/{drv}/{rest}")
        return norm.casefold()

    if is_unc:
        norm = "//" + posixpath.normpath(slashes.lstrip("/"))
        return norm.casefold()

    norm = posixpath.normpath(slashes)
    return norm.casefold() if os.name == "nt" else norm


def _is_under_norm_key(target_key: str, root_key: str) -> bool:
    if not target_key or not root_key:
        return False
    if root_key == "/":
        return target_key.startswith("/")
    return target_key == root_key or target_key.startswith(root_key.rstrip("/") + "/")


def _is_standard_os_temp_key(norm_key: str) -> bool:
    root = _normalize_cross_env_lexical(tempfile.gettempdir())
    return _is_under_norm_key(norm_key, root)


WSL_TEMP_CHECK = """
import json, os, sys, tempfile
from pathlib import Path

targets = json.load(sys.stdin)
runtime_root = tempfile.gettempdir()
allowed = []
if runtime_root == "/tmp" and Path(runtime_root).resolve() == Path(runtime_root):
    for raw in targets:
        normalized = os.path.normpath(raw)
        resolved = Path(raw).resolve()
        if (normalized.startswith("/tmp/") and resolved != Path(runtime_root)
                and resolved.is_relative_to(runtime_root)):
            allowed.append(raw)
print(json.dumps(allowed))
"""


def _is_wsl_standard_temp_path(target: str, distribution: str) -> bool:
    """在选定的本机 WSL 发行版核对标准 /tmp 的物理位置。"""
    if (os.name != "nt" or not target.startswith("/tmp/")
            or not re.fullmatch(r"/tmp/[A-Za-z0-9_./ +@=-]+", target)):
        return False
    # 固定入口与代码，待检查路径仅以 stdin 数据传递。Windows 宿主命令不会经此路径执行。
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe"
    selector = ["-d", distribution] if distribution else []
    try:
        result = subprocess.run(
            [str(executable), *selector, "--exec", "/usr/bin/python3", "-B", "-I", "-c", WSL_TEMP_CHECK],
            input=json.dumps([target]).encode("utf-8"), capture_output=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and json.loads(result.stdout) == [target]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def _wsl_command_prefix(command: str) -> Optional[Tuple[str, str]]:
    """只识别明确的本机 WSL 前缀，保留发行版与内层原文。"""
    prefix = re.match(
        r"""^\s*(?:wsl(?:\.exe)?)\s+"""
        r"""(?:(?:-d|--distribution)\s+(?:"([A-Za-z0-9][A-Za-z0-9_.-]{0,63})"|"""
        r"""([A-Za-z0-9][A-Za-z0-9_.-]{0,63}))\s+)?"""
        r"""(?:(?:--exec|-e|--)\s+)?(?=\S)(?!-)""",
        command, re.IGNORECASE,
    )
    if not prefix:
        return None
    return prefix[1] or prefix[2] or "", command[prefix.end():]


def _outer_shell_operators(command: str) -> bool:
    """检测由 Windows 外层 shell 消费的控制或重定向符号。"""
    quote = ""
    escaped = False
    for char in command:
        if escaped:
            escaped = False
        elif char == chr(96) or (quote and char == "\\"):
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in ";|&><\r\n()":
            return True
    return bool(quote or escaped)


def _resolve_nearest_existing(raw_str: str, base_dir: Optional[str]) -> Optional[Path]:
    """解析本地可达路径的真实物理位置，防御通过项目内软链接/目录联接逃逸写向项目外"""
    expanded = _expand_host_vars(raw_str)
    if not expanded:
        return None
    if os.name == "nt" and expanded.startswith("/") and not expanded.startswith("//"):
        m_mnt = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", expanded)
        if m_mnt:
            expanded = f"{m_mnt.group(1).upper()}:/{m_mnt.group(2) or ''}"
        else:
            return None
    elif os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", expanded):
        drv = expanded[0].lower()
        rest = expanded[2:].replace("\\", "/").lstrip("/")
        expanded = f"/mnt/{drv}/{rest}"

    p = Path(expanded)
    if not p.is_absolute():
        if not base_dir:
            return None
        base_p = Path(_expand_host_vars(base_dir))
        p = base_p / p

    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return None


def _collect_boundary_context(
    payload: Dict[str, Any],
    state_dir: Path,
    logs_dir: Path,
) -> Tuple[List[str], List[str], List[str], str, Optional[str]]:
    """
    构建当前会话的项目目录边界上下文：
    返回 (project_norm_keys, allowed_norm_keys, allowed_resolved_keys, project_display, base_cwd)
    """
    workspace_paths = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
    cwd_raw = str(payload.get("cwd") or "").strip()

    raw_projects = [str(wp).strip() for wp in workspace_paths if wp and str(wp).strip()]
    if not raw_projects and cwd_raw:
        raw_projects = [cwd_raw]

    if not raw_projects:
        return [], [], [], "", cwd_raw or None

    tool_call = payload.get("toolCall")
    args = tool_call.get("args") if isinstance(tool_call, dict) else None
    command_cwd = args.get("Cwd") if isinstance(args, dict) and tool_call.get("name") == "run_command" else None
    base_cwd = str(command_cwd or cwd_raw or raw_projects[0])
    project_norm_keys = [k for k in (_normalize_cross_env_lexical(p) for p in raw_projects) if k]
    workspace_in_os_temp = any(_is_standard_os_temp_key(pk) for pk in project_norm_keys)

    home_p = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME") or home_p / ".codex").expanduser()
    gemini_home = home_p / ".gemini"
    internal_raw: List[str] = [
        str(state_dir),
        str(logs_dir),
        str(home_p / ".cache"),
    ]
    internal_raw.extend(str(codex_home / name) for name in (
        "memories", "sessions", "cache", "tmp", ".tmp", "sqlite", "attachments", "visualizations",
    ))
    for host in ("antigravity", "antigravity-cli", "antigravity-ide"):
        internal_raw.extend(str(gemini_home / host / name) for name in (
            "brain", "plugin_data", "conversations", "annotations", "knowledge", "scratch",
            "sidecar_data", "cache", "log", "logs",
        ))
    internal_raw.extend(str(gemini_home / name) for name in ("logs", "sandbox"))
    if not workspace_in_os_temp:
        internal_raw.append(tempfile.gettempdir())

    art_dir = str(payload.get("artifactDirectoryPath") or "").strip()
    if art_dir:
        internal_raw.append(art_dir)

    tp_raw = str(payload.get("transcriptPath") or "").strip()
    if tp_raw:
        tp_norm = tp_raw.replace("\\", "/")
        marker = "/.system_generated/"
        idx = tp_norm.lower().find(marker)
        if idx > 0:
            internal_raw.append(tp_raw[:idx])

    allowed_norm_keys: List[str] = []
    allowed_resolved_keys: List[str] = []

    for item in raw_projects + internal_raw:
        nk = _normalize_cross_env_lexical(item)
        rp = _resolve_nearest_existing(item, None)
        rk = _normalize_cross_env_lexical(str(rp)) if rp is not None else ""
        if item in raw_projects:
            if rk:
                allowed_resolved_keys.append(rk)
        elif rk and (rk == nk or (os.name != "nt" and nk == "/tmp" and rk == "/private/tmp")):
            # macOS 的标准 /tmp 是 /private/tmp 的系统别名。
            allowed_resolved_keys.append(rk)
        else:
            # 缓存、轨迹等目录只有物理位置与登记的标准目录一致时才适用例外。
            continue
        if nk and nk not in allowed_norm_keys:
            allowed_norm_keys.append(nk)

    project_display = ", ".join(raw_projects)
    return project_norm_keys, allowed_norm_keys, allowed_resolved_keys, project_display, base_cwd


def _is_write_target_allowed(
    raw_target: str,
    base_cwd: Optional[str],
    allowed_norm_keys: List[str],
    allowed_resolved_keys: List[str],
    allow_os_temp: bool = True,
    wsl_distribution: Optional[str] = None,
) -> bool:
    cleaned = raw_target.strip().strip("\"'`;,)")
    if not cleaned or cleaned.lower() in _NULL_SINKS:
        return True
    if _LOCAL_VAR_TOKEN_RE.fullmatch(cleaned):
        return True

    lex_key = _normalize_cross_env_lexical(cleaned, base_cwd)
    if not lex_key:
        return True
    if not any(_is_under_norm_key(lex_key, rk) for rk in allowed_norm_keys):
        return (wsl_distribution is not None and allow_os_temp
                and _is_wsl_standard_temp_path(cleaned, wsl_distribution))

    # 若本地文件系统可解析真实路径，校验是否存在软链接/目录联接逃逸到允许目录外
    resolved_p = _resolve_nearest_existing(cleaned, base_cwd)
    if resolved_p is None:
        # 仅保留当前进程无法物理访问的原生 POSIX 项目路径的跨端词法比较。
        expanded = _expand_host_vars(cleaned)
        if os.name != "nt" or not expanded.startswith("/") or re.match(r"^/mnt/[A-Za-z](?:/|$)", expanded):
            return False
    if resolved_p is not None and allowed_resolved_keys:
        res_key = _normalize_cross_env_lexical(str(resolved_p))
        if not any(_is_under_norm_key(res_key, ark) for ark in allowed_resolved_keys):
            return False
    return True


def _extract_flag_value(tokens: List[str], flag_name: str) -> Optional[str]:
    for idx, tok in enumerate(tokens):
        if tok == flag_name and idx + 1 < len(tokens):
            return tokens[idx + 1]
        if tok.startswith(flag_name + "="):
            return tok.split("=", 1)[1]
    return None


def _compact_snippet(text: str, max_len: int = 140) -> str:
    flat = re.sub(r"\s+", " ", text.strip())
    return flat if len(flat) <= max_len else flat[: max_len - 3] + "..."


def _read_quoted_arg(text: str, start_idx: int) -> Optional[Tuple[str, int, str]]:
    """从 start_idx 读取单引号或双引号包裹的参数内容，返回 (inner_content, end_idx, quote_char)"""
    n = len(text)
    if start_idx >= n or text[start_idx] not in ("'", '"'):
        return None
    q = text[start_idx]
    idx = start_idx + 1
    buf: List[str] = []
    while idx < n:
        ch = text[idx]
        if q == '"':
            if ch == "\\" and idx + 1 < n:
                nxt = text[idx + 1]
                if nxt in ('"', "\\", "$", "`"):
                    buf.append(nxt)
                elif nxt == "n":
                    buf.append("\n")
                elif nxt == "t":
                    buf.append("\t")
                else:
                    buf.append("\\" + nxt)
                idx += 2
                continue
            if ch == '"':
                return "".join(buf), idx + 1, q
        else:
            # POSIX bash '\'' 转义兼容
            if ch == "'" and idx + 3 < n and text[idx : idx + 4] == "'\\''":
                buf.append("'")
                idx += 4
                continue
            if ch == "'":
                return "".join(buf), idx + 1, q
        buf.append(ch)
        idx += 1
    return None


def _extract_and_mask_inline_python(command_line: str) -> Tuple[List[str], str]:
    """提取命令中的 python -c / PowerShell here-string / heredoc 代码块，并在原命令中掩码避免误当成 Shell 命令"""
    blocks: List[str] = []
    masked = command_line

    for m_ps in list(_PS_HERESTRING_PY_RE.finditer(masked)):
        blocks.append(m_ps.group(2))
    masked = _PS_HERESTRING_PY_RE.sub("__INLINE_PY__", masked)

    for m_hd in list(_SH_HEREDOC_PY_RE.finditer(masked)):
        blocks.append(m_hd.group(2))
    masked = _SH_HEREDOC_PY_RE.sub("__INLINE_PY__", masked)

    out_parts: List[str] = []
    pos = 0
    while True:
        m = _PY_INLINE_HEAD_RE.search(masked, pos)
        if not m:
            out_parts.append(masked[pos:])
            break
        q_res = _read_quoted_arg(masked, m.end())
        if not q_res:
            out_parts.append(masked[pos : m.end()])
            pos = m.end()
            continue
        code_str, end_pos, _ = q_res
        blocks.append(code_str)
        out_parts.append(masked[pos : m.start()])
        out_parts.append("__INLINE_PY__")
        pos = end_pos

    return blocks, "".join(out_parts)


def _is_anchored_path_str(path_str: str) -> bool:
    s = path_str.strip()
    return s.startswith(("/", "\\", "~")) or bool(re.match(r"^[A-Za-z]:", s))


def _join_ast_path_parts(parts: List[str]) -> Optional[str]:
    valid = [s for s in parts if s]
    if not valid:
        return None
    acc = valid[0].rstrip("/\\") or valid[0]
    for seg in valid[1:]:
        if _is_anchored_path_str(seg):
            acc = seg.rstrip("/\\") or seg
        else:
            acc = (acc.rstrip("/\\") + "/" + seg.lstrip("/\\")) if acc else seg
    return acc


def _eval_ast_path_expr(node: ast.AST, bindings: Dict[str, str]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for val in node.values:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                parts.append(val.value)
            else:
                parts.append("_expr_")
        joined = "".join(parts)
        return joined if ("/" in joined or "\\" in joined or joined.startswith(("~", ".."))) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _eval_ast_path_expr(node.left, bindings)
        right = _eval_ast_path_expr(node.right, bindings)
        if right and _is_anchored_path_str(right):
            return right
        if left and right:
            return left.rstrip("/\\") + "/" + right.lstrip("/\\")
        return left
    if isinstance(node, ast.Call):
        fn = node.func
        fn_name = ""
        if isinstance(fn, ast.Name):
            fn_name = fn.id
        elif isinstance(fn, ast.Attribute):
            fn_name = fn.attr
        if fn_name == "home" and isinstance(fn, ast.Attribute) and not node.args:
            return "~"
        if fn_name in ("Path", "PurePath", "WindowsPath", "PosixPath", "expanduser", "abspath", "realpath") and node.args:
            subs = [_eval_ast_path_expr(a, bindings) for a in node.args]
            return _join_ast_path_parts([s for s in subs if s])
        if fn_name == "join" and node.args:
            subs = [_eval_ast_path_expr(a, bindings) for a in node.args]
            return _join_ast_path_parts([s for s in subs if s])
    return None


def _open_mode_is_mutating(call_node: ast.Call, mode_pos_idx: int) -> Tuple[bool, str]:
    mode_val: Optional[str] = None
    if len(call_node.args) > mode_pos_idx:
        arg = call_node.args[mode_pos_idx]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            mode_val = arg.value
    for kw in call_node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            mode_val = kw.value.value
    if not mode_val:
        return False, "r"
    return any(ch in mode_val for ch in ("w", "a", "x", "+")), mode_val


def _scan_inline_python_code(
    py_code: str,
    base_cwd: Optional[str],
    allowed_norm_keys: List[str],
    allowed_resolved_keys: List[str],
    allow_os_temp: bool,
    depth: int = 0,
    wsl_distribution: Optional[str] = None,
) -> Optional[Tuple[str, str, str]]:
    """使用 AST 精确识别内联 Python 脚本中真正发生写/删/改的外部路径（忽略一切只读 open 与数组 .copy()）"""
    candidates = [py_code]
    if '\\"' in py_code or "\\'" in py_code or "\\n" in py_code:
        candidates.append(py_code.replace('\\"', '"').replace("\\'", "'").replace("\\n", "\n"))

    tree: Optional[ast.AST] = None
    used_src = py_code
    for cand_src in candidates:
        try:
            tree = ast.parse(cand_src)
            used_src = cand_src
            break
        except SyntaxError:
            continue

    def _bad(target: Optional[str]) -> bool:
        return bool(
            target
            and not _is_write_target_allowed(target, base_cwd, allowed_norm_keys, allowed_resolved_keys,
                                             allow_os_temp, wsl_distribution)
        )

    if tree is not None:
        bindings: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                val = _eval_ast_path_expr(node.value, bindings)
                if val:
                    bindings[node.targets[0].id] = val

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            snippet = _compact_snippet(ast.get_source_segment(used_src, node) or "python call")
            fn = node.func

            # 1) 内置 open(path, mode)
            if isinstance(fn, ast.Name) and fn.id == "open" and node.args:
                is_mut, mode_s = _open_mode_is_mutating(node, 1)
                if is_mut:
                    tgt = _eval_ast_path_expr(node.args[0], bindings)
                    if _bad(tgt):
                        return f"Python 写入/修改文件 (open mode='{mode_s}')", str(tgt), snippet

            elif isinstance(fn, ast.Attribute):
                attr = fn.attr
                owner_name = fn.value.id if isinstance(fn.value, ast.Name) else ""

                # 2) Path(p).open(mode)
                if attr == "open":
                    is_mut, mode_s = _open_mode_is_mutating(node, 0)
                    if is_mut:
                        tgt = _eval_ast_path_expr(fn.value, bindings)
                        if _bad(tgt):
                            return f"Python 写入/修改文件 (.open mode='{mode_s}')", str(tgt), snippet

                # 3) Path(p).write_text / write_bytes / unlink / rmdir / mkdir / touch
                elif attr in ("write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch"):
                    tgt = _eval_ast_path_expr(fn.value, bindings)
                    if _bad(tgt):
                        return f"Python 文件写删操作 (.{attr})", str(tgt), snippet

                # 4) Path(p).rename(dst) / replace(dst)
                elif attr in ("rename", "replace") and owner_name != "os":
                    for expr in ([fn.value] + list(node.args[:1])):
                        tgt = _eval_ast_path_expr(expr, bindings)
                        if _bad(tgt):
                            return f"Python 移动/替换文件 (.{attr})", str(tgt), snippet

                # 5) os.* 写删改调用
                elif owner_name == "os" and attr in (
                    "remove",
                    "unlink",
                    "rmdir",
                    "removedirs",
                    "mkdir",
                    "makedirs",
                    "rename",
                    "replace",
                ):
                    check_args = node.args[:2] if attr in ("rename", "replace") else node.args[:1]
                    for a in check_args:
                        tgt = _eval_ast_path_expr(a, bindings)
                        if _bad(tgt):
                            return f"Python 系统文件操作 (os.{attr})", str(tgt), snippet

                # 6) shutil.* 写删改调用（copy 类仅目标端 args[1] 发生写入）
                elif owner_name == "shutil":
                    if attr == "rmtree" and node.args:
                        tgt = _eval_ast_path_expr(node.args[0], bindings)
                        if _bad(tgt):
                            return "Python 递归删除目录 (shutil.rmtree)", str(tgt), snippet
                    elif attr == "move" and node.args:
                        for a in node.args[:2]:
                            tgt = _eval_ast_path_expr(a, bindings)
                            if _bad(tgt):
                                return "Python 移动路径 (shutil.move)", str(tgt), snippet
                    elif attr.startswith("copy") and len(node.args) >= 2:
                        tgt = _eval_ast_path_expr(node.args[1], bindings)
                        if _bad(tgt):
                            return f"Python 复制写入外部路径 (shutil.{attr})", str(tgt), snippet

                # 7) 科学计算常用导出函数 savefig / to_csv / np.save*
                elif attr in ("savefig", "to_csv", "to_parquet", "to_excel", "to_feather", "to_pickle") and node.args:
                    tgt = _eval_ast_path_expr(node.args[0], bindings)
                    if _bad(tgt):
                        return f"Python 数据/图表导出 (.{attr})", str(tgt), snippet
                elif owner_name in ("np", "numpy") and attr in ("save", "savez", "savez_compressed", "savetxt") and node.args:
                    tgt = _eval_ast_path_expr(node.args[0], bindings)
                    if _bad(tgt):
                        return f"NumPy 数组落盘 (np.{attr})", str(tgt), snippet

                # 8) subprocess.run / os.system 嵌套命令检查
                elif depth < 2 and (
                    (owner_name == "subprocess" and attr in ("run", "Popen", "call", "check_call", "check_output"))
                    or (owner_name == "os" and attr == "system")
                ) and node.args:
                    first = node.args[0]
                    sub_cmd = ""
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        sub_cmd = first.value
                    elif isinstance(first, (ast.List, ast.Tuple)):
                        toks = [
                            elt.value
                            for elt in first.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ]
                        if toks:
                            sub_cmd = " ".join(f'"{t}"' if " " in t else t for t in toks)
                    if sub_cmd:
                        sub_hit = _detect_command_external_mutation(
                            sub_cmd, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp,
                            depth + 1, wsl_distribution,
                        )
                        if sub_hit:
                            return sub_hit
        return None

    # AST 无法解析时（如混入未展开的 Shell 语法），仅使用严格“写函数-目标参数”配对正则兜底
    fallback_pairs = (
        (
            "Python 写入文件 (open)",
            re.compile(r"""\bopen\s*\(\s*(['"][^'"]+['"])\s*,\s*['"][^'"]*[wax+][^'"]*['"]"""),
        ),
        (
            "Python 文件写删 (Path)",
            re.compile(
                r"""\bPath\s*\(\s*(['"][^'"]+['"])\s*\)\s*\.\s*(?:write_text|write_bytes|unlink|rmdir|mkdir|touch)\b"""
            ),
        ),
        (
            "Python 系统删除/建目录",
            re.compile(r"""\b(?:shutil\.rmtree|os\.(?:remove|unlink|rmdir|makedirs|mkdir))\s*\(\s*(['"][^'"]+['"])"""),
        ),
        (
            "Python 复制写入外部路径",
            re.compile(r"""\bshutil\.copy\w*\s*\([^,]+,\s*(['"][^'"]+['"])"""),
        ),
    )
    for label, pat in fallback_pairs:
        for m in pat.finditer(used_src):
            cand = m.group(1).strip("\"'")
            if _bad(cand):
                return label, cand, _compact_snippet(m.group(0))
    return None


def _unwrap_shell_script_layer(cmd: str) -> Optional[str]:
    """若整条命令是 wsl/bash/powershell -c '<inner>' 包装，提取内层脚本以精确解析子句命令头"""
    m = re.match(
        r"^\s*(?:[A-Za-z_]\w*=\S+\s+)*"
        r"""(?:wsl(?:\.exe)?\b(?:\s+(?:-(?:d|u)|--(?:distribution|user|cd))\s+(?:"(?:\\.|[^"])*"|'(?:''|[^'])*'|\S+)|\s+(?:--|-e|--exec|-[A-Za-z]+))*\s+)?"""
        r"(?:bash|sh|zsh|pwsh(?:\.exe)?|powershell(?:\.exe)?|cmd(?:\.exe)?)\b"
        r"(?:\s+(?:-(?:ExecutionPolicy|ep|WindowStyle|w|InputFormat|OutputFormat)\s+\S+|-[A-Za-z]+|/c))*\s+",
        cmd,
        re.IGNORECASE,
    )
    if not m:
        return None
    # 确认最后一个开关包含 -c / -lc / -command / /c
    prefix = cmd[: m.end()].lower()
    if not re.search(r"(?:\s-[a-z]*c|\s-command|\s/c)\s+$", prefix):
        return None
    q_res = _read_quoted_arg(cmd, m.end())
    if q_res and not cmd[q_res[1] :].strip():
        return q_res[0]
    return None


def _extract_clause_path_targets(tokens: List[str], copy_dest_only: bool = False, head_low: str = "") -> List[str]:
    """从单条写/删/复制/移动 Shell 子句参数中提取实际受影响的目标路径列表"""
    if copy_dest_only:
        for idx, tok in enumerate(tokens[1:], start=1):
            if tok.lower() in ("-destination", "-dest") and idx + 1 < len(tokens):
                return [tokens[idx + 1]]
            if tok.lower().startswith("-destination:"):
                return [tok.split(":", 1)[1]]

    positional: List[str] = []
    explicit_paths: List[str] = []
    has_explicit_script = False
    idx = 1
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        low = tok.lower()
        if head_low in ("sed", "perl"):
            if low in ("-e", "--expression", "-f", "--file"):
                has_explicit_script = True
                idx += 2
                continue
            if tok.startswith(("-e", "-f")) and not tok.startswith("--") and len(tok) > 2:
                has_explicit_script = True
                idx += 1
                continue
        if low in _PS_NON_PATH_VALUE_FLAGS:
            idx += 2
            continue
        if low in ("-path", "-literalpath", "-filepath", "-destination", "-newname") and idx + 1 < len(tokens):
            explicit_paths.append(tokens[idx + 1])
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        if os.name == "nt" and re.fullmatch(r"/[A-Za-z0-9?]", tok):
            idx += 1
            continue
        positional.append(tok)
        idx += 1

    if head_low in ("sed", "perl") and not has_explicit_script and positional:
        positional = positional[1:]

    all_paths = explicit_paths + positional
    if not copy_dest_only:
        return all_paths
    if not all_paths:
        return []
    if head_low == "robocopy" and len(positional) >= 2:
        return [positional[1]]
    return [all_paths[-1]]


def _looks_like_explicit_or_escaped_path(tok: str) -> bool:
    s = tok.strip().strip("\"'")
    if not s or s == "__INLINE_PY__":
        return False
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith(("\\\\", "//", "~", "..", "$HOME", "${HOME}", "$env:", "%")):
        return True
    if s.startswith("/") and not re.fullmatch(r"/[A-Za-z0-9?]", s):
        return True
    return False


def _pos_inside_quotes(text: str, target_pos: int) -> bool:
    in_single = False
    in_double = False
    idx = 0
    limit = min(target_pos, len(text))
    while idx < limit:
        ch = text[idx]
        if ch == "\\" and in_double and idx + 1 < limit:
            idx += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        idx += 1
    return in_single or in_double


def _detect_command_external_mutation(
    command_line: str,
    base_cwd: Optional[str],
    allowed_norm_keys: List[str],
    allowed_resolved_keys: List[str],
    allow_os_temp: bool = True,
    depth: int = 0,
    wsl_distribution: Optional[str] = None,
) -> Optional[Tuple[str, str, str]]:
    """
    精准检测命令行是否改动项目目录外的业务路径或全局配置。
    返回 None（允许），或 (具体动作说明, 项目外目标路径, 触发语句片段)。
    """
    if not command_line or not command_line.strip() or depth > 3:
        return None

    wsl_prefix = _wsl_command_prefix(command_line) if os.name == "nt" else None
    if wsl_prefix and not _outer_shell_operators(wsl_prefix[1]):
        distribution, inner = wsl_prefix
        return _detect_command_external_mutation(
            inner, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, depth + 1, distribution,
        )

    sanitized = _COMMIT_MSG_RE.sub(r'\1"__COMMIT_MSG__"', command_line)

    # 1. 专项拦截: git worktree add 指向项目目录外 (如 ../.codex-worktrees 或父目录)
    for m_wt in _GIT_WORKTREE_ADD_RE.finditer(sanitized):
        wt_tokens = _safe_split_tokens(m_wt.group(1))
        idx = 0
        wt_path: Optional[str] = None
        while idx < len(wt_tokens):
            tok = wt_tokens[idx]
            if tok in ("-b", "-B", "--reason"):
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            wt_path = tok
            break
        if wt_path and not _is_write_target_allowed(
            wt_path, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
        ):
            return "创建项目外 Git 工作树 (git worktree add)", wt_path, _compact_snippet(m_wt.group(0))

    # 2. 专项拦截: 执行生产级全局安装脚本 (默认改写 ~/.gemini/config 与 ~/.codex)
    for m_inst in _INSTALLER_INVOKE_RE.finditer(sanitized):
        prefix_seg = m_inst.group(0)
        if re.search(r"\b(?:git|rg|grep|cat|head|tail|Get-Content|Select-String|py_compile)\b", prefix_seg, re.IGNORECASE):
            continue
        tail_args = m_inst.group(1) or ""
        arg_tokens = _safe_split_tokens(tail_args)
        lower_tokens = {t.lower() for t in arg_tokens}
        if "--dry-run" in lower_tokens or "-dryrun" in lower_tokens or "--probe-apps" in lower_tokens:
            continue
        target_dir_val = _extract_flag_value(arg_tokens, "--target-dir")
        skills_dir_val = _extract_flag_value(arg_tokens, "--codex-skills-dir")
        codex_home_val = _extract_flag_value(arg_tokens, "--codex-home")
        if (target_dir_val or codex_home_val) and "--no-zotero" in lower_tokens:
            td_ok = (not target_dir_val) or _is_write_target_allowed(
                target_dir_val, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
            )
            sd_ok = (not skills_dir_val) or _is_write_target_allowed(
                skills_dir_val, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
            )
            ch_ok = (not codex_home_val) or _is_write_target_allowed(
                codex_home_val, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
            )
            if td_ok and sd_ok and ch_ok:
                continue
        return (
            "执行全局配置同步安装 (将更新本机 ~/.gemini/config 与/或 ~/.codex)",
            "~/.gemini/config/plugins/user-guard, ~/.codex",
            _compact_snippet(m_inst.group(0)),
        )

    # 3. 若整条命令被 shell 包装，拆壳检查内层脚本，同时保留已确认的 WSL 作用域。
    unwrapped = _unwrap_shell_script_layer(sanitized)
    if unwrapped:
        return _detect_command_external_mutation(
            unwrapped, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp,
            depth + 1, wsl_distribution,
        )

    # 4. 提取并用 AST 精确扫描所有内联 Python 代码块（掩码替换后不再污染外层 Shell 扫描）
    py_blocks, shell_masked = _extract_and_mask_inline_python(sanitized)
    for block in py_blocks:
        py_hit = _scan_inline_python_code(
            block, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, depth, wsl_distribution
        )
        if py_hit:
            return py_hit

    # 5. 检查 PowerShell .NET [IO.File] / [IO.Directory] 写删方法
    for m_net in _DOTNET_IO_MUTATE_RE.finditer(shell_masked):
        cls_name, method_name, raw_args = m_net.group(1), m_net.group(2), m_net.group(3)
        parts = [p.strip().strip("\"'") for p in raw_args.split(",") if p.strip()]
        if not parts:
            continue
        check_list = [parts[1]] if method_name.lower().startswith("copy") and len(parts) >= 2 else parts[:1]
        if method_name.lower().startswith("move") and len(parts) >= 2:
            check_list = parts[:2]
        for cand in check_list:
            if _looks_like_explicit_or_escaped_path(cand) and not _is_write_target_allowed(
                cand, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
            ):
                return f".NET 文件写删操作 ([IO.{cls_name}]::{method_name})", cand, _compact_snippet(m_net.group(0))

    # 6. 检查外层 Shell 重定向 (> / >>) 的直接输出目标（忽略位于引号字面量内的符号）
    for m_red in _SHELL_REDIRECT_RE.finditer(shell_masked):
        if _pos_inside_quotes(shell_masked, m_red.start()):
            continue
        op, red_tok = m_red.group(1), m_red.group(2).strip().strip("\"'")
        if _looks_like_explicit_or_escaped_path(red_tok) and not _is_write_target_allowed(
            red_tok, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
        ):
            return f"Shell 输出重定向写入 ({op})", red_tok, _compact_snippet(m_red.group(0))

    # 7. 按子句严格检查处于“命令头位置”的 Shell/PowerShell 写删移动复制命令
    clauses = re.split(r"(?:\r?\n|;|&&|\|\||\|)|[{}]", shell_masked)
    for raw_clause in clauses:
        clause = raw_clause.strip()
        if not clause:
            continue
        # 剥离前导 else / then / do 控制流关键字，跳过条件判断头部
        clause = re.sub(r"^(?:(?:else|then|do)\b\s*)+", "", clause, flags=re.IGNORECASE)
        if re.match(r"^(?:if|elif|while|until)(?:\s|\()", clause, re.IGNORECASE):
            continue
        tokens = _safe_split_tokens(clause)
        if not tokens:
            continue

        # 跳过前导 env / sudo / wsl / timeout / KEY=VAL / PowerShell & 调用符
        idx = 0
        n_tok = len(tokens)
        while idx < n_tok:
            t = tokens[idx]
            tl = Path(t.replace("\\", "/")).name.lower()
            if tl in ("sudo", "env", "&", "command"):
                idx += 1
                continue
            if tl == "timeout":
                idx += 1
                while idx < n_tok and tokens[idx].startswith("-"):
                    idx += 1
                if idx < n_tok:
                    idx += 1
                continue
            if tl in ("wsl", "wsl.exe"):
                idx += 1
                while idx < n_tok and tokens[idx].startswith("-"):
                    fl = tokens[idx].lower()
                    idx += 1
                    if fl in ("-d", "--distribution", "-u", "--user", "--cd") and idx < n_tok:
                        idx += 1
                continue
            if "=" in t and not t.startswith("-") and "/" not in t and "\\" not in t:
                idx += 1
                continue
            break
        if idx >= n_tok:
            continue

        cmd_tokens = tokens[idx:]
        raw_head = cmd_tokens[0]
        head_low = Path(raw_head.replace("\\", "/")).name.lower()
        if head_low.endswith(".exe"):
            head_low = head_low[:-4]

        action_label: Optional[str] = None
        targets: List[str] = []

        if head_low in _SHELL_DELETE_VERBS:
            action_label = f"删除文件或目录 ({raw_head})"
            targets = _extract_clause_path_targets(cmd_tokens, copy_dest_only=False, head_low=head_low)
        elif head_low in _SHELL_CREATE_WRITE_VERBS:
            action_label = f"创建或写入文件/目录 ({raw_head})"
            targets = _extract_clause_path_targets(cmd_tokens, copy_dest_only=False, head_low=head_low)
        elif head_low in _SHELL_MOVE_VERBS:
            action_label = f"移动或重命名路径 ({raw_head})"
            targets = _extract_clause_path_targets(cmd_tokens, copy_dest_only=False, head_low=head_low)
        elif head_low in _SHELL_COPY_VERBS:
            action_label = f"复制写入外部路径 ({raw_head})"
            targets = _extract_clause_path_targets(cmd_tokens, copy_dest_only=True, head_low=head_low)
        elif head_low in ("sed", "perl"):
            has_inplace = any(
                t == "-i" or t.startswith("-i") or (t.startswith("-") and not t.startswith("--") and "i" in t[1:])
                for t in cmd_tokens[1:]
            )
            if has_inplace:
                action_label = f"原地修改文件 ({raw_head} -i)"
                targets = _extract_clause_path_targets(cmd_tokens, copy_dest_only=False, head_low=head_low)

        if action_label and targets:
            for cand in targets:
                if not cand or cand.startswith("$") and not cand.lower().startswith(("$home", "${home}", "$env:")):
                    continue
                command_distribution = wsl_distribution
                if wsl_distribution is not None and any(
                    Path(tok.replace("\\", "/")).name.casefold() in REMOTE_OR_SUBSYSTEM_WRAPPERS | {"chroot", "nsenter"}
                    for tok in tokens[:idx + 1]
                ):
                    command_distribution = None
                if not _is_write_target_allowed(
                    cand, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, command_distribution
                ):
                    return action_label, cand, _compact_snippet(clause)

    return None


def _build_boundary_violation_response(
    tool_name: str,
    action_desc: str,
    external_target: str,
    project_display: str,
    conversation_id: str,
    tool_summary: str = "",
    snippet: str = "",
) -> Dict[str, Any]:
    headless_subtask = os.environ.get("CAE_HARNESS_DEPTH", "").strip() == "1"
    decision = "deny" if headless_subtask else "force_ask"
    intent_line = f" | 任务说明：{tool_summary}" if tool_summary else ""
    snippet_line = f"\n• 触发语句：{snippet}" if snippet else ""
    if headless_subtask:
        reason = (
            f"【user-guard 项目目录硬边界阻断】跨端无头子任务严禁在目标项目目录（{project_display}）之外改动业务文件或全局配置！"
            f"已拦截越界操作：{action_desc} -> {external_target}{intent_line}。"
        )
    else:
        summary_bullet = f"\n• 当前任务意图：{tool_summary}" if tool_summary else ""
        reason = (
            f"【⚠️ 项目目录外文件改动确认】"
            f"{summary_bullet}"
            f"\n• 具体写删动作：{action_desc}"
            f"\n• 项目外目标路径：{external_target}（当前项目根目录：{project_display}）"
            f"{snippet_line}"
            f"\n（注：标准临时目录与对话记忆缓存已自动放行；按 R4 硬约束，改动项目外业务目录或全局配置须经您明确批准。）"
        )
    log_event(
        "PreToolUse:workspace_boundary",
        conversation_id,
        f"{decision.upper()} {action_desc} via {tool_name} -> {external_target} (workspace={project_display})",
    )
    return {"decision": decision, "reason": reason}


def handle_pre_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    """处理 PreToolUse 门禁请求"""
    conversation_id = str(payload.get("conversationId") or "unknown_session").strip()
    workspace_paths = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
    transcript_path_str = payload.get("transcriptPath")

    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    raw_tool_name = str(tool_call.get("name") or tool_call.get("tool_name") or "").strip()
    tool_name = handoff_gate.AGY_TOOL_ALIASES.get(raw_tool_name, raw_tool_name)
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}

    cwd = (args.get("Cwd") if tool_name == "run_command" else None) or payload.get("cwd")
    state_dir, logs_dir = get_data_dirs()

    boundary: Optional[Dict[str, Any]] = None
    # 分支 0: 项目目录外文件改动硬边界门禁 (R4: write_file / edit_file / run_command)
    if tool_name in ("write_file", "edit_file", "run_command"):
        proj_keys, allowed_keys, allowed_res_keys, proj_display, base_cwd = _collect_boundary_context(
            payload, state_dir, logs_dir
        )
        if proj_keys:
            allow_os_temp = not any(_is_standard_os_temp_key(pk) for pk in proj_keys)
            if tool_name in ("write_file", "edit_file"):
                write_arg = str(
                    args.get("Path") or args.get("path") or args.get("TargetFile") or args.get("file_path") or ""
                ).strip()
                if write_arg and not _is_write_target_allowed(
                    write_arg, base_cwd, allowed_keys, allowed_res_keys, allow_os_temp
                ):
                    if tool_name == "edit_file":
                        t_lines = len(str(args.get("Target") or "").splitlines()) or 1
                        r_lines = len(str(args.get("Replacement") or "").splitlines())
                        act = f"修改项目外已有文件 (edit_file: 替换 {t_lines} 行 -> {r_lines} 行)"
                    elif args.get("Append"):
                        act = f"向项目外文件追加内容 (write_file Append=true, {len(str(args.get('Content') or ''))} 字符)"
                    else:
                        act = f"创建或覆写项目外文件 (write_file, {len(str(args.get('Content') or ''))} 字符)"
                    meta = args.get("ArtifactMetadata") if isinstance(args.get("ArtifactMetadata"), dict) else {}
                    summary_txt = str(meta.get("Summary") or "").splitlines()[0].strip() if meta.get("Summary") else ""
                    boundary = _build_boundary_violation_response(
                        tool_name, act, write_arg, proj_display, conversation_id, tool_summary=summary_txt
                    )
            else:
                cmd_str = str(args.get("CommandLine") or args.get("command_line") or args.get("command") or "")
                cmd_summary = str(args.get("toolSummary") or args.get("description") or "").strip()
                ext_hit = _detect_command_external_mutation(
                    cmd_str, base_cwd, allowed_keys, allowed_res_keys, allow_os_temp
                )
                if ext_hit:
                    act_desc, ext_target, clause_snip = ext_hit
                    boundary = _build_boundary_violation_response(
                        tool_name,
                        act_desc,
                        ext_target,
                        proj_display,
                        conversation_id,
                        tool_summary=cmd_summary,
                        snippet=clause_snip,
                    )

    try:
        with user_words.session_state_lock(state_dir, conversation_id):
            gate_response = handoff_gate.check_pre_tool_use_locked(payload, state_dir, boundary_blocked=boundary is not None)
            if gate_response is not None:
                return gate_response
            if boundary is not None:
                return boundary
            try:
                return _check_polling_locked(tool_name, args, conversation_id, cwd, workspace_paths, transcript_path_str,
                                             state_dir, logs_dir)
            except (OSError, ValueError) as exc:
                log_event("PreToolUse:poll_state_error", conversation_id, f"轮询状态未更新（{exc}）")
                return {"decision": "allow"}
    except (OSError, ValueError) as exc:
        log_event("PreToolUse:state_error", conversation_id, f"工具门禁状态未更新（{exc}）")
        if (state_dir / f"{conversation_id}_handoff_gate.json").exists():
            return {"decision": "deny", "reason": "【user-guard】无法核实会话交接阶段。请重试该工具或直接收尾。"}
        return boundary or {"decision": "allow"}


def _check_polling_locked(tool_name: str, args: Dict[str, Any], conversation_id: str, cwd: Optional[str],
                          workspace_paths: List[str], transcript_path_str: Optional[str], state_dir: Path,
                          logs_dir: Path) -> Dict[str, Any]:
    state_file = state_dir / f"{conversation_id}_status_guard.json"

    # 分支 1: read_file 或 run_command 访问证据文件
    if tool_name in ("read_file", "run_command"):
        raw_target: Optional[str] = None
        param_sig = ""
        is_subsystem_cmd = False

        if tool_name == "read_file":
            file_arg = str(args.get("AbsolutePath") or args.get("Path") or args.get("path") or "").strip()
            if not file_arg or not is_evidence_file(file_arg):
                return {"decision": "allow"}
            raw_target = file_arg
            start_line = args.get("StartLine", args.get("startLine"))
            end_line = args.get("EndLine", args.get("endLine"))
            byte_offset = args.get("ByteOffset", args.get("byteOffset"))
            param_sig = f"s={start_line}:e={end_line}:b={byte_offset}"
        else:
            cmd_line = str(args.get("CommandLine") or args.get("command_line") or args.get("command") or "")
            raw_target, is_subsystem_cmd, param_sig = parse_run_command_evidence(cmd_line)
            if not raw_target:
                return {"decision": "allow"}

        # Windows 宿主下通过 wsl/ssh/docker 访问子系统内部 POSIX 路径（宿主不可见）时豁免防死锁
        cleaned_raw = raw_target.strip().strip("\"'")
        is_posix_style_on_win = os.name == "nt" and cleaned_raw.startswith("/") and not cleaned_raw.startswith("//")
        target_path = resolve_target_path(raw_target, cwd, workspace_paths)

        if (is_subsystem_cmd or is_posix_style_on_win) and not target_path.exists():
            return {"decision": "allow"}

        # 豁免插件自身的状态与日志目录，绝不自我拦截
        if _is_inside_dir(target_path, state_dir) or _is_inside_dir(target_path, logs_dir):
            return {"decision": "allow"}

        fp = compute_file_fingerprint(target_path)
        if not fp:
            return {"decision": "allow"}

        query_key = f"{tool_name}:{fp['pathKey']}|{param_sig}"
        state = _load_guard_state(state_file, conversation_id)
        records = state.setdefault("records", {})
        last_rec = records.get(query_key)

        if isinstance(last_rec, dict) and _is_fingerprint_equal(last_rec, fp):
            query_count = int(last_rec.get("queryCount", 1)) + 1
            last_rec["queryCount"] = query_count
            last_rec["lastBlockedAt"] = get_current_iso_time()
            _save_guard_state(state_file, state, conversation_id)
            log_event(
                "PreToolUse:status_guard",
                conversation_id,
                f"DENIED unchanged {tool_name} on {fp['path']} [{param_sig}] (query #{query_count})",
            )
            return {"decision": "deny", "reason": DENY_REASON_TEXT}

        records[query_key] = {
            "type": "evidence_query",
            "tool": tool_name,
            "path": fp["path"],
            "params": param_sig,
            "exists": fp["exists"],
            "mtimeNs": fp["mtimeNs"],
            "size": fp["size"],
            "sha256": fp["sha256"],
            "queryCount": 1,
            "lastAllowedAt": get_current_iso_time(),
        }
        _save_guard_state(state_file, state, conversation_id)
        log_event(
            "PreToolUse:status_guard",
            conversation_id,
            f"ALLOWED {tool_name} on {fp['path']} [{param_sig}] (size={fp['size']})",
        )
        return {"decision": "allow"}

    # 分支 2: manage_task(Action="status") 联动后台任务日志物理指纹
    if tool_name == "manage_task":
        action = str(args.get("Action") or args.get("action") or "").strip().lower()
        if action != "status":
            return {"decision": "allow"}

        task_id = str(args.get("TaskId") or args.get("taskId") or args.get("task_id") or "default_task").strip()
        task_key = f"task:{task_id}"
        task_log_path = _locate_task_log_file(transcript_path_str, task_id)
        task_fp = compute_file_fingerprint(task_log_path) if task_log_path else None

        state = _load_guard_state(state_file, conversation_id)
        records = state.setdefault("records", {})
        last_rec = records.get(task_key)
        now_epoch = time.time()

        if isinstance(last_rec, dict):
            if task_fp and task_fp["exists"]:
                # 任务日志存在：严格依据任务日志是否产生新输出判定放行或阻断
                if _is_fingerprint_equal(last_rec, task_fp):
                    query_count = int(last_rec.get("queryCount", 1)) + 1
                    last_rec["queryCount"] = query_count
                    last_rec["lastBlockedAt"] = get_current_iso_time()
                    _save_guard_state(state_file, state, conversation_id)
                    log_event(
                        "PreToolUse:status_guard",
                        conversation_id,
                        f"DENIED unchanged manage_task(status) for {task_id} (query #{query_count})",
                    )
                    return {"decision": "deny", "reason": DENY_REASON_TEXT}
            else:
                # 尚无任务日志文件时：阻断 30 秒内的高频盲目轮询，超时后允许再次探查防永久锁死
                last_epoch = float(last_rec.get("lastAllowedEpoch", 0.0))
                if (now_epoch - last_epoch) < TASK_NO_LOG_COOLDOWN_SEC:
                    query_count = int(last_rec.get("queryCount", 1)) + 1
                    last_rec["queryCount"] = query_count
                    last_rec["lastBlockedAt"] = get_current_iso_time()
                    _save_guard_state(state_file, state, conversation_id)
                    log_event(
                        "PreToolUse:status_guard",
                        conversation_id,
                        f"DENIED rapid manage_task(status) polling for {task_id} (query #{query_count})",
                    )
                    return {"decision": "deny", "reason": DENY_REASON_TEXT}

        new_rec: Dict[str, Any] = {
            "type": "manage_task_status",
            "taskId": task_id,
            "queryCount": 1,
            "lastAllowedEpoch": now_epoch,
            "lastAllowedAt": get_current_iso_time(),
        }
        if task_fp:
            new_rec.update(
                {
                    "exists": task_fp["exists"],
                    "mtimeNs": task_fp["mtimeNs"],
                    "size": task_fp["size"],
                    "sha256": task_fp["sha256"],
                }
            )
        records[task_key] = new_rec
        _save_guard_state(state_file, state, conversation_id)
        log_event("PreToolUse:status_guard", conversation_id, f"ALLOWED manage_task(status) for {task_id}")
        return {"decision": "allow"}

    return {"decision": "allow"}


def main() -> None:
    input_text = sys.stdin.read().strip()
    payload: Dict[str, Any] = {}
    if input_text:
        try:
            payload = json.loads(input_text)
        except Exception as e:
            log_event("PreToolUse:status_guard", "unknown", f"Failed to parse stdin JSON: {e}")

    try:
        response = handle_pre_tool_use(payload)
    except Exception as e:
        stack_str = traceback.format_exc()
        log_event("PreToolUse:status_guard", str(payload.get("conversationId", "unknown")), f"Exception: {e}\n{stack_str}")
        response = {"decision": "allow"}

    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

"""
status_guard.py - 反重力 PreToolUse 项目目录硬边界、长会话交接门禁与防空转轮询门禁 (user-guard)

核心职责：
1. 项目目录外文件改动硬约束（R4 硬门禁，防在业务盘乱建目录与越权篡改配置）：
   - 拦截 write_file / edit_file 及已登记的跨表面工具别名对项目目录外的文件写改；
   - 通过内联 Python AST 语法树分析与 Shell 子句命令头精确配对，拦截 run_command 向项目目录外创建 git worktree（如 ../.codex-worktrees）、写删改外部路径或执行全局生产安装（忽略只读打开、数组 .copy()、.md 文件名与外部解释器调用，杜绝误报）；
   - 自动放行系统标准临时目录、宿主与项目关联的对话记忆/子代理轨迹/制品草稿（brain / plugin_data / memories / sessions 等）、标准工具缓存（~/.cache）及 Windows<->WSL 同项目挂载路径；
   - 交互主会话触发真实越界时返回 force_ask，在弹窗清晰列出“当前任务意图 + 具体写删动作 + 项目外目标路径 + 触发语句”请求用户明确批准；无头跨端子任务（CAE_HARNESS_DEPTH=1）直接硬 deny。
2. 反重力专有长会话两阶段交接停机门禁（联动 handoff_gate.py）：
   - 在 drain/freeze 收口期内严禁新开子代理/run_workflow/swarm/schedule/browser/cae_harness 派工、禁止读图及 git push，按排空预算管控写/运行动作并无阻断放行只读核对、交接档写入与本地 git add/commit。
3. 防空转轮询门禁（适配 Antigravity Reactive Wakeup 响应式唤醒机制）：
   - read_file / view_file 读取算例/任务证据文件（将 StartLine/EndLine/ByteOffset 编入查询键，放行正常长文件翻页阅读）；
   - run_command 使用只读命令查看证据文件（将归一化命令参数编入查询键，放行变参检索）；
   - manage_task (Action == "status") 轮询后台任务状态（自动联动 .system_generated/tasks/<TaskId>.log 真实物理指纹，有新输出自动放行，杜绝永久锁死）。
"""

# ① 常量、阈值与宿主依赖

import ast
import hashlib
import itertools
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
    r"results?\.(?:txt|json|jsonl|dat|csv)|"
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
READ_FILE_TOOLS = ("read_file", "view_file")
WRITE_FILE_TOOLS = ("write_file", "write_to_file")
EDIT_FILE_TOOLS = ("edit_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit")
FILE_MUTATION_TOOLS = WRITE_FILE_TOOLS + EDIT_FILE_TOOLS
FILE_PATH_KEYS = ("Path", "path", "AbsolutePath", "absolute_path", "FilePath", "TargetFile",
                  "target_file", "file_path", "NotebookPath", "notebook_path")

LARGE_FILE_THRESHOLD = 4 * 1024 * 1024  # 4 MB
CHUNK_SAMPLE_BYTES = 1 * 1024 * 1024    # 1 MB
MAX_WORKFLOW_SCRIPT_BYTES = 1 * 1024 * 1024  # 1 MB
TASK_NO_LOG_COOLDOWN_SEC = 30.0

DENY_REASON_TEXT = (
    "【user-guard 防空转门禁阻断】目标证据文件内容与查询参数自上次读取后未发生任何变化。"
    "反重力具备后台任务就绪通知机制（Reactive Wakeup），严禁在此原地空转轮询！"
    "请等待后台任务完成通知或先推进其他工作；文件产生新输出、或改变读取区间/查询参数后将自动放行。"
)

# 项目目录外文件改动硬边界门禁 (Workspace Boundary Hard Guard - R4)

_NULL_SINKS = {"/dev/null", "nul", "$null"}
_LOCAL_VAR_TOKEN_RE = re.compile(r"(?:\$\{?(?:tmp|temp|tmpdir|out|dst|work|stage)\w*\}?|%(?:tmp|temp)\w*%)", re.IGNORECASE)
# 引号内的转义与普通字符分支互斥；命令头限定在子句起点，避免长串失败匹配反复回扫。
_COMMIT_MSG_RE = re.compile(
    r"""((?:^|[\r\n;|&])\s*\bgit\b[^\r\n;|&]*?\bcommit\b[^\r\n;|&]*?(?:\s-(?:m|am)|--message(?:=|\s+)))(?:"(?:\\.|[^"\\])*"|'(?:''|[^'])*'|\S+)""",
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
    r"""(?<![^\s;|&()])(?:"[^"\r\n]*[\\/]|'[^'\r\n]*[\\/]|[^\s"';|&()]+[\\/]|["'])?\bpython[0-9.]*(?:\.exe)?\b["']?"""
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
    r"""(?:^|[\s;|&()])(?:\d+)?(>{1,2})(?![>&=])\s*("(?:\\.|[^"\\])+"|'(?:''|[^'])+'|[^\s;|&<>()]+)""",
)
_PY_FALLBACK_PAIRS = (
    ("Python 写入文件 (open)", re.compile(
        r"""\bopen\s*\(\s*(['"][^'"]+['"])\s*,\s*['"][^'"]*[wax+][^'"]*['"]""")),
    ("Python 文件写删 (Path)", re.compile(
        r"""\bPath\s*\(\s*(['"][^'"]+['"])\s*\)\s*\.\s*(?:write_text|write_bytes|unlink|rmdir|mkdir|touch)\b""")),
    ("Python 系统删除/建目录", re.compile(
        r"""\b(?:shutil\.rmtree|os\.(?:remove|unlink|rmdir|makedirs|mkdir))\s*\(\s*(['"][^'"]+['"])""")),
    ("Python 复制写入外部路径", re.compile(
        r"""\bshutil\.copy\w*\s*\([^,]+,\s*(['"][^'"]+['"])""")),
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


# ② 路径定位、日志与轮询状态读写

def get_data_dirs() -> Tuple[Path, Path]:
    return user_words.resolve_data_dirs(HOOKS_DIR)


def get_current_iso_time() -> str:
    return user_words.current_iso_time()


def log_event(event_name: str, conversation_id: str, message: str) -> None:
    user_words.append_lifecycle_log(HOOKS_DIR, event_name, conversation_id, message)


def _is_inside_dir(target: Path, parent: Path) -> bool:
    try:
        target.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def resolve_target_path(
    raw_path_str: str,
    cwd: Optional[str],
    workspace_paths: Optional[List[str]],
) -> Path:
    """严格按 payload.cwd -> workspacePaths 优先级解析相对路径，绝不落入插件自身工作目录"""
    return handoff_gate._resolve_candidate_path(raw_path_str, cwd, workspace_paths)


def _canonical_path_key(p: Path) -> str:
    return user_words._canonical_path_key(p)


def _load_guard_state(state_file: Path, conversation_id: str) -> Dict[str, Any]:
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8-sig"))
            if (isinstance(data, dict) and data.get("version") == 2
                    and data.get("conversationId") == conversation_id and isinstance(data.get("records"), dict)):
                return data
            raise ValueError("轮询状态格式或会话身份错误")
        except (ValueError, OSError, RecursionError) as exc:
            log_event("PreToolUse:poll_state_error", conversation_id, f"轮询状态读取失败（{exc}），保留原文件")
            raise
    return {"version": 2, "conversationId": conversation_id, "records": {}}


def _save_guard_state(state_file: Path, state: Dict[str, Any], conversation_id: str) -> None:
    state["version"] = 2
    state["conversationId"] = conversation_id
    state["updatedAt"] = get_current_iso_time()
    user_words.atomic_write_json(state_file, state)


# ③ 证据查询解析、文件指纹与防空转检测

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

    p = Path(cleaned.replace("\\", "/"))
    name = p.name
    ext = p.suffix.lower()

    if ext in EVIDENCE_EXTENSIONS:
        return True
    if EVIDENCE_FILENAME_RE.match(name):
        return True
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
            while idx < n:
                flag_low = tokens[idx].lower()
                if flag_low in ("-executionpolicy", "-ep", "-windowstyle", "-w"):
                    idx += 2
                    continue
                is_command = flag_low in ("/c", "-command") or bool(re.fullmatch(r"-[a-z]*c", flag_low))
                if not is_command and flag_low not in ("-noprofile", "-noni", "-noninteractive"):
                    break
                idx += 1
                if is_command and idx < n:
                    tokens = _safe_split_tokens(" ".join(tokens[idx:]))
                    idx = 0
                    n = len(tokens)
                    break
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

    # 查询参数中的大小写与引号内空白有实际含义，不能随 Windows 路径一起折叠。
    norm_cmd_sig = command_line.strip()

    # 若含重定向写入或修改型操作符，非纯只读查询，直接放行
    if re.search(r"(?:>>|>|\btee\b|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i\b)", command_line):
        return None, False, norm_cmd_sig

    segments = [(seg, 0) for seg in reversed(handoff_gate._split_shell_clauses(command_line))]
    any_subsystem = False
    evidence = None

    while segments:
        seg, depth = segments.pop()
        if not seg:
            continue
        raw_tokens = _safe_split_tokens(seg)
        if not raw_tokens:
            continue
        tokens, used_sub = _unwrap_inner_command(raw_tokens)
        any_subsystem = any_subsystem or used_sub
        inner = _unwrap_shell_script_layer(seg)
        if inner is not None:
            if depth >= 12:
                return None, False, norm_cmd_sig
            segments.extend((part, depth + 1) for part in reversed(handoff_gate._split_shell_clauses(inner)))
            continue
        if not tokens:
            continue

        cmd_head = Path(tokens[0].replace("\\", "/")).name.lower()
        if cmd_head.endswith(".exe"):
            cmd_head = cmd_head[:-4]

        if cmd_head not in READONLY_COMMANDS:
            return None, False, norm_cmd_sig

        search_command = cmd_head in ("grep", "rg", "select-string", "sls")
        pattern_flags = {"-e", "--regexp", "-f", "--file", "-pattern"}
        value_flags = pattern_flags | {
            "-g", "--glob", "--iglob", "-t", "--type", "--type-not", "-m", "--max-count",
            "--after-context", "--before-context", "--context", "-encoding", "-context", "-include", "-exclude",
        }
        explicit_pattern = search_command and any(
            tok.lower().split("=", 1)[0].split(":", 1)[0] in pattern_flags
            or (cmd_head in ("grep", "rg") and tok.startswith(("-e", "-f")) and not tok.startswith("--") and len(tok) > 2)
            for tok in tokens[1:]
        )
        skip_pattern = search_command and not explicit_pattern
        idx = 1
        options = True
        while idx < len(tokens):
            tok = tokens[idx]
            idx += 1
            if options and tok == "--":
                options = False
                continue
            if options and tok.startswith("-"):
                if search_command and (tok.lower() in value_flags or tok in ("-A", "-B", "-C")):
                    idx += 1
                continue
            if skip_pattern:
                skip_pattern = False
                continue
            if evidence is None and is_evidence_file(tok):
                evidence = tok

    return evidence, any_subsystem, norm_cmd_sig


def compute_file_fingerprint(target_path: Path) -> Optional[Dict[str, Any]]:
    """
    计算证据文件的快速复合指纹：
    - <= 4MB: 全量 SHA256 + size + mtime_ns
    - >  4MB: 头部 1MB + 尾部 1MB 分块 SHA256 + size + mtime_ns（防 GB 级算例日志阻塞超时）
    """
    path_key = _canonical_path_key(target_path)
    try:
        resolved_str = str(target_path.resolve())
    except (OSError, ValueError, RuntimeError):
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
    except (OSError, ValueError):
        return None


def _is_fingerprint_equal(rec: Dict[str, Any], fp: Dict[str, Any]) -> bool:
    return (
        rec.get("exists") == fp["exists"]
        and rec.get("mtimeNs") == fp["mtimeNs"]
        and rec.get("size") == fp["size"]
        and rec.get("sha256") == fp["sha256"]
    )


def _locate_task_log_file(transcript_path_str: Optional[str], task_id: str) -> Optional[Path]:
    """根据会话 transcriptPath 定位 Antigravity 后台任务日志 .system_generated/tasks/<TaskId>.log"""
    if not transcript_path_str or not user_words.valid_session_id(task_id):
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


def _check_polling_locked(tool_name: str, args: Dict[str, Any], conversation_id: str, cwd: Optional[str],
                          workspace_paths: List[str], transcript_path_str: Optional[str], state_dir: Path,
                          logs_dir: Path) -> Dict[str, Any]:
    state_file = state_dir / f"{conversation_id}_status_guard.json"

    # 分支 1: read_file / view_file 或 run_command 访问证据文件
    if tool_name in (*READ_FILE_TOOLS, "run_command"):
        raw_target: Optional[str] = None
        param_sig = ""
        is_subsystem_cmd = False

        if tool_name in READ_FILE_TOOLS:
            file_arg = str(
                args.get("AbsolutePath")
                or args.get("Path")
                or args.get("path")
                or args.get("TargetFile")
                or args.get("target_file")
                or args.get("file_path")
                or ""
            ).strip()
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

        canon_tool = "read_file" if tool_name in READ_FILE_TOOLS else tool_name
        query_key = f"{canon_tool}:{fp['pathKey']}|{param_sig}"
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


# ④ 跨系统路径归一化与目录边界

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
    """盘符与 /mnt/<drive> 共用规范键；本机 POSIX 路径保留大小写，物理路径另查软链接。"""
    expanded = user_words.strip_win_extended_prefix(_expand_host_vars(raw_str))
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


def _host_path_string(raw_str: str) -> Optional[str]:
    """将盘符或 WSL 挂载路径还原为当前宿主可解析的路径字符串。"""
    expanded = user_words.strip_win_extended_prefix(_expand_host_vars(raw_str))
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

    return expanded


def _resolve_nearest_existing(raw_str: str, base_dir: Optional[str]) -> Optional[Path]:
    """解析真实物理位置；相对路径与基准目录必须使用同一种宿主路径格式。"""
    expanded = _host_path_string(raw_str)
    if expanded is None:
        return None
    p = Path(expanded)
    if not p.is_absolute():
        if not base_dir:
            return None
        base_expanded = _host_path_string(base_dir)
        if base_expanded is None:
            return None
        p = Path(base_expanded) / p

    try:
        return p.resolve()
    except (OSError, RuntimeError, ValueError):
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
    raw_name = (tool_call.get("name") or tool_call.get("tool_name")) if isinstance(tool_call, dict) else ""
    context_tool = handoff_gate.AGY_TOOL_ALIASES.get(raw_name, raw_name)
    command_cwd = (
        (args.get("Cwd") or args.get("cwd"))
        if isinstance(args, dict) and context_tool in ("run_command", "run_workflow")
        else None
    )
    base_cwd = str(command_cwd or cwd_raw or raw_projects[0])
    if command_cwd:
        base_cwd = _ast_resolve_cwd(str(command_cwd), cwd_raw or raw_projects[0])
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


# ⑤ 内联 Python / 动态工作流提取与 AST 写入目标分析

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


# 使用对象身份标记未知值，脚本中的同名字符串不能伪造安全状态。
_AST_DYNAMIC = object()
_AST_CWD = object()
_AST_DEFINITION = object()
_AST_MAX_CANDIDATES = 64
_AST_MATCH_TYPES = (ast.Match,) if hasattr(ast, "Match") else ()
_AST_TRY_TYPES = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)
_AST_MODULE_CALLS = {
    "os": {"chdir", "open", "remove", "unlink", "rmdir", "removedirs", "mkdir", "makedirs", "rename",
           "replace", "renames", "truncate", "symlink", "link", "system"},
    "shutil": {"rmtree", "move", "copy", "copy2", "copyfile", "copytree", "make_archive", "unpack_archive"},
    "tempfile": {"gettempdir", "mkdtemp", "TemporaryDirectory", "mkstemp", "NamedTemporaryFile"},
    "subprocess": {"run", "Popen", "call", "check_call", "check_output"},
    "numpy": {"save", "savez", "savez_compressed", "savetxt"},
}
_AST_TEMP_DIR_INDEX = {"mkdtemp": 2, "TemporaryDirectory": 2, "mkstemp": 2, "NamedTemporaryFile": 6}
_AST_WRITE_CALL_ARGS = {
    **{"os." + name: ((0, ("path",)),) for name in
       ("remove", "unlink", "rmdir", "removedirs", "mkdir", "makedirs", "truncate")},
    **{"os." + name: ((0, ("src",)), (1, ("dst",))) for name in ("rename", "replace", "renames")},
    **{"os." + name: ((1, ("dst",)),) for name in ("symlink", "link")},
    "shutil.rmtree": ((0, ("path",)),),
    "shutil.move": ((0, ("src",)), (1, ("dst",))),
    **{"shutil." + name: ((1, ("dst",)),) for name in ("copy", "copy2", "copyfile", "copytree")},
    "shutil.make_archive": ((0, ("base_name",)),),
    "shutil.unpack_archive": ((1, ("extract_dir",)),),
}


class _AstModule:
    def __init__(self, name):
        self.name = name


class _AstCallable:
    def __init__(self, qualname):
        self.qualname = qualname


class _AstDict:
    def __init__(self, fields=None, uncertain=False):
        self.fields = fields or {}
        self.uncertain = uncertain


class _AstTempPath(str):
    """仅由已识别的 tempfile 工厂产生，普通字符串无法伪造此类型。"""


def _copy_ast_bindings(bindings):
    memo = {}

    def copy_value(value):
        if isinstance(value, _AstDict):
            if value not in memo:
                memo[value] = _AstDict(uncertain=value.uncertain)
                memo[value].fields = {key: frozenset(copy_value(v) for v in values) for key, values in value.fields.items()}
            return memo[value]
        return value

    return {key: frozenset(copy_value(value) for value in _ast_values(values)) for key, values in bindings.items()}


def _ast_resolve_cwd(value, base):
    normalized = _normalize_cross_env_lexical(value, base)
    resolved = _resolve_nearest_existing(normalized, None)
    return str(resolved) if resolved is not None else normalized


def _ast_values(value):
    return value if isinstance(value, frozenset) else frozenset((value,))


def _ast_product(parts, combine):
    if not parts or any(_AST_DYNAMIC in part for part in parts):
        return frozenset((_AST_DYNAMIC,))
    result = set()
    for values in itertools.product(*parts):
        if not all(isinstance(value, str) for value in values):
            return frozenset((_AST_DYNAMIC,))
        combined = combine(values)
        if any(isinstance(value, _AstTempPath) for value in values):
            root = _normalize_cross_env_lexical(tempfile.gettempdir())
            if _is_under_norm_key(_normalize_cross_env_lexical(combined), root):
                combined = _AstTempPath(combined)
        result.add(combined)
        if len(result) >= _AST_MAX_CANDIDATES:
            return frozenset((_AST_DYNAMIC,))
    return frozenset(result)


def _ast_call_qualnames(fn, bindings):
    names = {value.qualname for value in _eval_ast_path_candidates(fn, bindings) if isinstance(value, _AstCallable)}
    # 保留原有对直接模块调用的识别；显式绑定后只按该绑定判断。
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id not in bindings:
        module = "numpy" if fn.value.id == "np" else fn.value.id
        if module in _AST_MODULE_CALLS:
            names.add(module + "." + fn.attr)
    return names


def _ast_call_arg_candidates(node, index, keywords, bindings, default=_AST_DYNAMIC):
    """合并位置与关键字实参；影响该参数位置的解包及未知关键字均保留动态候选。"""
    values = set()
    if len(node.args) > index and not isinstance(node.args[index], ast.Starred):
        values.update(_eval_ast_path_candidates(node.args[index], bindings))
    for kw in node.keywords:
        if kw.arg in keywords:
            values.update(_eval_ast_path_candidates(kw.value, bindings))
    if not values:
        values.update(_ast_values(default))
    if any(isinstance(arg, ast.Starred) for arg in node.args[:index + 1]) or any(kw.arg is None for kw in node.keywords):
        values.add(_AST_DYNAMIC)
    return frozenset(values)


def _ast_has_unpacking(node):
    return any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords)


def _ast_temp_path_candidates(node, name, bindings):
    if _ast_has_unpacking(node):
        return frozenset((_AST_DYNAMIC,))
    if name == "gettempdir":
        return frozenset((_AST_DYNAMIC,)) if node.args or node.keywords else frozenset((_AstTempPath(tempfile.gettempdir()),))
    parents = _ast_call_arg_candidates(node, _AST_TEMP_DIR_INDEX[name], ("dir",), bindings, None)
    return frozenset(_AstTempPath(tempfile.gettempdir()) if parent is None else parent for parent in parents)


def _eval_ast_path_candidates(node, bindings):
    """保留全部候选绑定；表达式存在但无法求值时显式返回未知值。"""
    dynamic = frozenset((_AST_DYNAMIC,))
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, type(None))):
        return frozenset((node.value,))
    if isinstance(node, ast.Lambda):
        return frozenset((node,))
    if isinstance(node, ast.Name):
        return _ast_values(bindings.get(node.id, _AST_DYNAMIC))
    if isinstance(node, ast.Attribute):
        values = frozenset(_AstCallable(owner.name + "." + node.attr)
                           for owner in _eval_ast_path_candidates(node.value, bindings) if isinstance(owner, _AstModule))
        return values or dynamic
    if isinstance(node, (ast.List, ast.Tuple)):
        parts = [_eval_ast_path_candidates(elt, bindings) for elt in node.elts]
        if any(_AST_DYNAMIC in part for part in parts):
            return dynamic
        values = set()
        for items in itertools.product(*parts):
            values.add(items)
            if len(values) >= _AST_MAX_CANDIDATES:
                return dynamic
        return frozenset(values)
    if isinstance(node, ast.NamedExpr):
        return _eval_ast_path_candidates(node.value, bindings)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                # 动态格式说明符同样可能改变路径。
                if part.format_spec is not None or part.conversion not in (-1, 115):
                    parts.append(dynamic)
                else:
                    parts.append(_eval_ast_path_candidates(part.value, bindings))
            else:
                parts.append(_eval_ast_path_candidates(part, bindings))
        return _ast_product(parts, lambda values: "".join(values))
    if isinstance(node, ast.IfExp):
        return _eval_ast_path_candidates(node.body, bindings) | _eval_ast_path_candidates(node.orelse, bindings)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
        left, right = (_eval_ast_path_candidates(expr, bindings) for expr in (node.left, node.right))
        if isinstance(node.op, ast.Add):
            return _ast_product([left, right], lambda values: "".join(values))
        # 绝对右操作数覆盖左操作数；未知值始终传播。
        return _ast_product([left, right], lambda values: _join_ast_path_parts(list(values)))
    if isinstance(node, ast.Dict):
        states = [_AstDict()]
        for key, value in zip(node.keys, node.values):
            values = _eval_ast_path_candidates(value, bindings)
            if key is None:
                expanded = []
                for state in states:
                    for other in values:
                        if isinstance(other, _AstDict):
                            expanded.append(_AstDict({**state.fields, **other.fields}, state.uncertain or other.uncertain))
                        else:
                            expanded.append(_AstDict(dict(state.fields), True))
                states = expanded
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                for state in states:
                    state.fields[key.value] = values  # Python 重复键由后值覆盖。
            else:
                for state in states:
                    state.uncertain = True
            if len(states) > _AST_MAX_CANDIDATES:
                return dynamic
        return frozenset(states)
    if isinstance(node, ast.Subscript):
        keys = _eval_ast_path_candidates(node.slice, bindings)
        result = set()
        for value in _eval_ast_path_candidates(node.value, bindings):
            for key in keys:
                result.update(value.fields.get(key, dynamic) if isinstance(value, _AstDict) and not value.uncertain else dynamic)
        return frozenset(result) or dynamic
    if isinstance(node, ast.Call):
        temp_names = _ast_call_qualnames(node.func, bindings) & {"tempfile." + name for name in _AST_MODULE_CALLS["tempfile"]}
        if temp_names:
            return frozenset().union(*(_ast_temp_path_candidates(node, name.split(".")[1], bindings) for name in temp_names))
        name = node.func.id if isinstance(node.func, ast.Name) else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
        if name == "home" and isinstance(node.func, ast.Attribute) and not node.args:
            return frozenset(("~",))
        if name in ("Path", "PurePath", "WindowsPath", "PosixPath", "expanduser", "abspath", "realpath", "join", "joinpath", "str"):
            parts = [_eval_ast_path_candidates(arg, bindings) for arg in node.args]
            if name == "joinpath" or (not node.args and isinstance(node.func, ast.Attribute)):
                parts.insert(0, _eval_ast_path_candidates(node.func.value, bindings))
            if not parts and name in ("Path", "PurePath", "WindowsPath", "PosixPath"):
                return frozenset((".",))
            return _ast_product(parts, lambda values: _join_ast_path_parts(list(values)))
    return dynamic


def _eval_ast_path_expr(node: ast.AST, bindings: Dict[str, Any]) -> Optional[str]:
    """保留原单路径查询接口；安全检查使用完整候选集合。"""
    values = _eval_ast_path_candidates(node, bindings)
    return next(iter(values)) if len(values) == 1 and all(isinstance(v, str) for v in values) else None


def _format_ast_candidates(values):
    return ", ".join(sorted(v if isinstance(v, str) else "<DYNAMIC_UNRESOLVED>" for v in _ast_values(values)))


def _open_mode_is_mutating(call_node: ast.Call, mode_pos_idx: int, bindings=None) -> Tuple[bool, str]:
    values = _ast_call_arg_candidates(call_node, mode_pos_idx, ("mode",), bindings or {}, "r")
    if any(isinstance(arg, ast.Starred) for arg in call_node.args):
        values |= frozenset((_AST_DYNAMIC,))
    readonly = all(isinstance(v, str) and v and "r" in v and set(v) <= set("rbtU") for v in values)
    return not readonly, _format_ast_candidates(values)


def _bind_ast_target(target, value, bindings):
    if isinstance(target, ast.Name):
        bindings[target.id] = value
    elif isinstance(target, (ast.Tuple, ast.List)):
        # value 为解包后的逐项候选集合，长度不匹配时清除全部旧绑定。
        items = value if isinstance(value, list) and len(value) == len(target.elts) else [frozenset((_AST_DYNAMIC,))] * len(target.elts)
        for child, item in zip(target.elts, items):
            _bind_ast_target(child, item, bindings)
    elif isinstance(target, ast.Subscript):
        keys = _eval_ast_path_candidates(target.slice, bindings)
        for container in _eval_ast_path_candidates(target.value, bindings):
            if isinstance(container, _AstDict):
                if len(keys) == 1 and all(isinstance(key, str) for key in keys):
                    container.fields[next(iter(keys))] = value
                else:
                    container.uncertain = True


def _merge_ast_bindings(bindings, alternatives):
    for key in set().union(*(set(state) for state in alternatives)):
        bindings[key] = frozenset().union(*(_ast_values(state.get(key, _AST_DYNAMIC)) for state in alternatives))


def _iter_ast_calls_in_order(node, bindings, active=()):
    """按求值顺序遍历；分支保守合并，函数按每次调用时的绑定重新审查。"""
    walk = lambda child, scope=bindings: _iter_ast_calls_in_order(child, scope, active)
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        if node.value is not None:
            yield from walk(node.value)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, (ast.List, ast.Tuple)) and isinstance(node.value, (ast.List, ast.Tuple)):
                value = [_eval_ast_path_candidates(expr, bindings) for expr in node.value.elts]
            else:
                value = _eval_ast_path_candidates(node.value, bindings)
            _bind_ast_target(target, value, bindings)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        for decorator in getattr(node, "decorator_list", ()):
            yield from walk(decorator)
        params = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
        for value in defaults:
            yield from walk(value)
        node._guard_defaults = {arg.arg: _eval_ast_path_candidates(value, bindings)
                                for arg, value in zip((list(node.args.posonlyargs) + list(node.args.args))[-len(node.args.defaults):], node.args.defaults)}
        node._guard_defaults.update({arg.arg: _eval_ast_path_candidates(value, bindings)
                                    for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults) if value is not None})
        if not isinstance(node, ast.Lambda):
            bindings[node.name] = frozenset((node,))
        # 定义时检查已知危险路径；未知形参留待调用点叠加实参后核验。
        local = _copy_ast_bindings(bindings)
        local[_AST_DEFINITION] = frozenset((True,))
        for arg in params:
            local[arg.arg] = node._guard_defaults.get(arg.arg, frozenset((_AST_DYNAMIC,)))
        for stmt in node.body if isinstance(node.body, list) else [node.body]:
            yield from _iter_ast_calls_in_order(stmt, local, active + (node,))
        return
    if isinstance(node, ast.ClassDef):
        for expr in list(node.decorator_list) + list(node.bases) + list(node.keywords):
            yield from walk(expr)
        local = _copy_ast_bindings(bindings)
        for stmt in node.body:
            yield from walk(stmt, local)
        bindings[node.name] = frozenset((_AST_DYNAMIC,))
        return
    if isinstance(node, ast.ExceptHandler):
        if node.type is not None:
            yield from walk(node.type)
        if node.name:
            bindings[node.name] = frozenset((_AST_DYNAMIC,))
        for stmt in node.body:
            yield from walk(stmt)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        local = _copy_ast_bindings(bindings)
        for gen in node.generators:
            yield from walk(gen.iter, local)
            _bind_ast_target(gen.target, frozenset((_AST_DYNAMIC,)), local)
            for condition in gen.ifs:
                yield from walk(condition, local)
        for expr in (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,):
            yield from walk(expr, local)
        return
    if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.BoolOp) + _AST_TRY_TYPES + _AST_MATCH_TYPES):
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            yield from walk(node.test)
        if isinstance(node, (ast.For, ast.AsyncFor)):
            yield from walk(node.iter)
        branches = []
        if isinstance(node, _AST_MATCH_TYPES):
            yield from walk(node.subject)
            branches = [[case] for case in node.cases]
        elif isinstance(node, ast.BoolOp):
            branches = [[value] for value in node.values]
        else:
            for field in ("body", "orelse", "handlers"):
                branch = getattr(node, field, [])
                branches.append(branch if isinstance(branch, list) else [branch])
        alternatives = [_copy_ast_bindings(bindings)]
        for branch in branches:
            local = _copy_ast_bindings(bindings)
            if isinstance(node, (ast.For, ast.AsyncFor)):
                _bind_ast_target(node.target, frozenset((_AST_DYNAMIC,)), local)
            if isinstance(node, _AST_MATCH_TYPES):
                case = branch[0]
                for pattern in ast.walk(case.pattern):
                    # MatchAs / MatchStar 使用 name，MatchMapping 使用 rest；3.9 不会进入此分支。
                    for field in ("name", "rest"):
                        name = getattr(pattern, field, None)
                        if name:
                            local[name] = frozenset((_AST_DYNAMIC,))
                if case.guard is not None:
                    yield from walk(case.guard, local)
                branch = case.body
            for stmt in branch:
                yield from walk(stmt, local)
            alternatives.append(local)
        _merge_ast_bindings(bindings, alternatives)
        for stmt in getattr(node, "finalbody", []):
            yield from walk(stmt)
        return
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            yield from walk(item.context_expr)
            if item.optional_vars is not None:
                _bind_ast_target(item.optional_vars, _eval_ast_path_candidates(item.context_expr, bindings), bindings)
        for stmt in node.body:
            yield from walk(stmt)
        return
    if isinstance(node, ast.ImportFrom) and node.module in _AST_MODULE_CALLS:
        for alias in node.names:
            if alias.name in _AST_MODULE_CALLS[node.module]:
                bindings[alias.asname or alias.name] = frozenset((_AstCallable(node.module + "." + alias.name),))
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in _AST_MODULE_CALLS:
                bindings[alias.asname or alias.name] = frozenset((_AstModule(alias.name),))
    if isinstance(node, (ast.AugAssign, ast.Delete)):
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                bindings[child.id] = frozenset((_AST_DYNAMIC,))
    if isinstance(node, ast.Call):
        # 参数中的海象赋值先于外层调用生效。
        yield from walk(node.func)
        for arg in node.args:
            yield from walk(arg)
        for kw in node.keywords:
            yield from walk(kw.value)
        yield node, bindings
        functions = _eval_ast_path_candidates(node.func, bindings)
        for function in functions:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if function in active or len(active) >= 12:
                # 递归无法继续静态展开时，将后续绑定置脏，避免沿用安全值。
                for key in list(bindings):
                    if isinstance(key, str):
                        bindings[key] = _ast_values(bindings[key]) | frozenset((_AST_DYNAMIC,))
                continue
            local = _copy_ast_bindings(bindings)
            local.pop(_AST_DEFINITION, None)
            params = list(function.args.posonlyargs) + list(function.args.args)
            for arg in params + list(function.args.kwonlyargs):
                local[arg.arg] = function._guard_defaults.get(arg.arg, frozenset((_AST_DYNAMIC,)))
            for param, value in zip(params, node.args):
                local[param.arg] = _eval_ast_path_candidates(value, bindings)
            for kw in node.keywords:
                if kw.arg:
                    local[kw.arg] = _eval_ast_path_candidates(kw.value, bindings)
            if any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords):
                for arg in params + list(function.args.kwonlyargs):
                    local[arg.arg] = _ast_values(local[arg.arg]) | frozenset((_AST_DYNAMIC,))
            for stmt in function.body if isinstance(function.body, list) else [function.body]:
                yield from _iter_ast_calls_in_order(stmt, local, active + (function,))
            bindings[_AST_CWD] = local.get(_AST_CWD, bindings.get(_AST_CWD, frozenset(("",))))
            for stmt in ast.walk(function):
                if isinstance(stmt, (ast.Global, ast.Nonlocal)):
                    for name in stmt.names:
                        bindings[name] = local.get(name, frozenset((_AST_DYNAMIC,)))
        return
    for child in ast.iter_child_nodes(node):
        yield from walk(child)


def _ast_bad_target(values, bindings, base_cwd, allowed_norm_keys, allowed_resolved_keys,
                    allow_os_temp, wsl_distribution=None):
    cwds = _ast_values(bindings.get(_AST_CWD, base_cwd))
    for value in _ast_values(values):
        if not isinstance(value, str):
            if _AST_DEFINITION not in bindings:
                return True
            continue
        if isinstance(value, _AstTempPath):
            root = tempfile.gettempdir()
            normalized = [_normalize_cross_env_lexical(root)]
            if _is_write_target_allowed(value, base_cwd, normalized, normalized):
                continue
        if any(not isinstance(cwd, (str, type(None))) or not _is_write_target_allowed(
            value, cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
        ) for cwd in cwds):
            return True
    return False


def _track_ast_chdir(node, bindings, base_cwd, allowed_norm_keys, allowed_resolved_keys,
                     allow_os_temp, snippet, wsl_distribution=None):
    if "os.chdir" not in _ast_call_qualnames(node.func, bindings):
        return None
    values = _ast_call_arg_candidates(node, 0, ("path",), bindings)
    if _ast_bad_target(values, bindings, base_cwd, allowed_norm_keys, allowed_resolved_keys,
                       allow_os_temp, wsl_distribution):
        return "Python 越界或动态切换工作目录 (os.chdir)", _format_ast_candidates(values), snippet
    bindings[_AST_CWD] = _ast_product(
        [_ast_values(bindings.get(_AST_CWD, base_cwd or "")), values],
        lambda parts: _ast_resolve_cwd(parts[1], parts[0]),
    )
    return None


def _scan_inline_python_code(
    py_code: str,
    base_cwd: Optional[str],
    allowed_norm_keys: List[str],
    allowed_resolved_keys: List[str],
    allow_os_temp: bool,
    depth: int = 0,
    wsl_distribution: Optional[str] = None,
    tree: Optional[ast.AST] = None,
) -> Optional[Tuple[str, str, str]]:
    """使用 AST 精确识别内联 Python 脚本中真正发生写/删/改的外部路径（忽略一切只读 open 与数组 .copy()）"""
    candidates = [py_code]
    if '\\"' in py_code or "\\'" in py_code or "\\n" in py_code:
        candidates.append(py_code.replace('\\"', '"').replace("\\'", "'").replace("\\n", "\n"))

    used_src = py_code
    if tree is None:
        for cand_src in candidates:
            try:
                tree = ast.parse(cand_src)
                used_src = cand_src
                break
            except SyntaxError:
                continue

    bindings = {_AST_CWD: frozenset((base_cwd or "",))}

    def _bad(target) -> bool:
        return _ast_bad_target(target, bindings, base_cwd, allowed_norm_keys, allowed_resolved_keys,
                               allow_os_temp, wsl_distribution)

    if tree is not None:
        for node, bindings in _iter_ast_calls_in_order(tree, bindings):
            snippet = _compact_snippet(ast.get_source_segment(used_src, node) or "python call")
            fn = node.func
            chdir_hit = _track_ast_chdir(node, bindings, base_cwd, allowed_norm_keys, allowed_resolved_keys,
                                        allow_os_temp, snippet, wsl_distribution)
            if chdir_hit:
                return chdir_hit

            # 模块函数先于同名 Path 方法检查，模块与可调用标识不能作为路径字符串使用。
            qualnames = _ast_call_qualnames(fn, bindings)
            for qualname in qualnames:
                if qualname == "os.open":
                    flags = _ast_call_arg_candidates(node, 1, ("flags",), bindings)
                    if not all(type(value) is int and value == 0 for value in flags):
                        tgt = _ast_call_arg_candidates(node, 0, ("path",), bindings)
                        if _bad(tgt):
                            return "Python 系统文件写入 (os.open)", _format_ast_candidates(tgt), snippet
                elif qualname in _AST_WRITE_CALL_ARGS:
                    for index, keywords in _AST_WRITE_CALL_ARGS[qualname]:
                        default = "." if qualname == "shutil.unpack_archive" else _AST_DYNAMIC
                        tgt = _ast_call_arg_candidates(node, index, keywords, bindings, default)
                        if _bad(tgt):
                            return f"Python 系统文件写删操作 ({qualname})", _format_ast_candidates(tgt), snippet
                elif qualname.startswith("tempfile.") and qualname.split(".")[1] in _AST_TEMP_DIR_INDEX:
                    tgt = _ast_temp_path_candidates(node, qualname.split(".")[1], bindings)
                    if _bad(tgt):
                        return f"Python 创建临时目录或文件 ({qualname})", _format_ast_candidates(tgt), snippet
                elif qualname in {"numpy." + name for name in _AST_MODULE_CALLS["numpy"]}:
                    tgt = _ast_call_arg_candidates(node, 0, ("file", "fname"), bindings)
                    if _bad(tgt):
                        return f"NumPy 数组落盘 ({qualname})", _format_ast_candidates(tgt), snippet
                elif depth <= 3 and (qualname == "os.system" or qualname in {
                    "subprocess." + name for name in _AST_MODULE_CALLS["subprocess"]
                }):
                    cwds = _ast_values(bindings.get(_AST_CWD, base_cwd))
                    if qualname.startswith("subprocess.") and any(kw.arg == "cwd" for kw in node.keywords):
                        raw_cwds = frozenset().union(*(
                            _eval_ast_path_candidates(kw.value, bindings) for kw in node.keywords if kw.arg == "cwd"
                        ))
                        specified = raw_cwds - frozenset((None,))
                        if _bad(specified):
                            return "Python 子进程指定项目外或动态 Cwd", _format_ast_candidates(specified), snippet
                        resolved = _ast_product([cwds, specified], lambda parts: _ast_resolve_cwd(parts[1], parts[0])) if specified else frozenset()
                        cwds = resolved | (cwds if None in raw_cwds else frozenset())
                    commands = _ast_call_arg_candidates(node, 0, ("args", "command"), bindings)
                    for command in commands:
                        if isinstance(command, tuple) and all(isinstance(part, str) for part in command):
                            command = " ".join(shlex.quote(part) for part in command)
                        if not isinstance(command, str):
                            if _AST_DEFINITION in bindings:
                                continue
                            return "Python 子进程命令无法静态确定", "<DYNAMIC_UNRESOLVED>", snippet
                        for cwd in cwds:
                            if not isinstance(cwd, (str, type(None))):
                                if _AST_DEFINITION in bindings:
                                    continue
                                return "Python 子进程工作目录无法静态确定", "<DYNAMIC_UNRESOLVED>", snippet
                            sub_hit = _detect_command_external_mutation(
                                command, cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp,
                                depth + 1, wsl_distribution,
                            )
                            if sub_hit:
                                return sub_hit
            if qualnames:
                continue

            # 内置 open 与 Path 方法分别检查路径、模式和解包参数。
            if isinstance(fn, ast.Name) and fn.id == "open":
                is_mut, mode_s = _open_mode_is_mutating(node, 1, bindings)
                if is_mut:
                    tgt = _ast_call_arg_candidates(node, 0, ("file",), bindings)
                    if _bad(tgt):
                        return f"Python 写入/修改文件 (open mode='{mode_s}')", _format_ast_candidates(tgt), snippet

            elif isinstance(fn, ast.Attribute):
                attr = fn.attr

                # 2) Path(p).open(mode)
                if attr == "open":
                    is_mut, mode_s = _open_mode_is_mutating(node, 0, bindings)
                    if is_mut:
                        tgt = _eval_ast_path_candidates(fn.value, bindings)
                        if _bad(tgt):
                            return f"Python 写入/修改文件 (.open mode='{mode_s}')", _format_ast_candidates(tgt), snippet

                # 3) Path(p).write_text / write_bytes / unlink / rmdir / mkdir / touch
                elif attr in ("write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch", "symlink_to", "hardlink_to"):
                    tgt = _eval_ast_path_candidates(fn.value, bindings)
                    if _bad(tgt):
                        return f"Python 文件写删操作 (.{attr})", _format_ast_candidates(tgt), snippet

                # 4) Path(p).rename(dst) / replace(dst)
                elif attr in ("rename", "replace"):
                    if attr == "replace" and (
                        (not any(isinstance(arg, ast.Starred) for arg in node.args) and len(node.args) >= 2)
                        or any(kw.arg in ("old", "new", "count") for kw in node.keywords)
                    ):
                        continue
                    target = _ast_call_arg_candidates(node, 0, ("target",), bindings)
                    if _ast_has_unpacking(node):
                        target |= frozenset((_AST_DYNAMIC,))
                    for tgt in (_eval_ast_path_candidates(fn.value, bindings), target):
                        if _bad(tgt):
                            return f"Python 移动/替换文件 (.{attr})", _format_ast_candidates(tgt), snippet

                elif attr in ("savefig", "to_csv", "to_parquet", "to_excel", "to_feather", "to_pickle"):
                    keywords = ("fname",) if attr == "savefig" else (("excel_writer",) if attr == "to_excel" else ("path_or_buf", "path"))
                    default = None if attr in ("to_csv", "to_parquet") else _AST_DYNAMIC
                    tgt = _ast_call_arg_candidates(node, 0, keywords, bindings, default)
                    if attr in ("to_csv", "to_parquet"):
                        tgt -= frozenset((None,))
                    if _bad(tgt):
                        return f"Python 数据/图表导出 (.{attr})", _format_ast_candidates(tgt), snippet
        return None

    # AST 无法解析时（如混入未展开的 Shell 语法），仅使用严格“写函数-目标参数”配对正则兜底
    for label, pat in _PY_FALLBACK_PAIRS:
        for m in pat.finditer(used_src):
            cand = m.group(1).strip("\"'")
            if _bad(cand):
                return label, cand, _compact_snippet(m.group(0))
    return None


def _ast_dict_field(fields, keys, default=_AST_DYNAMIC):
    values = [fields[key] for key in keys if key in fields]
    return frozenset().union(*values) if values else _ast_values(default)


def _ast_workflow_tool_arguments(node, bindings):
    """仅展开静态确定的序列与字典；未知解包不能覆盖已知安全实参而获准。"""
    positions = [()]
    for arg in node.args:
        if isinstance(arg, ast.Starred):
            expanded = _eval_ast_path_candidates(arg.value, bindings)
            if any(not isinstance(value, tuple) for value in expanded):
                return None
            positions = [prev + tuple(_ast_values(item) for item in value) for prev in positions for value in expanded]
        else:
            values = _eval_ast_path_candidates(arg, bindings)
            positions = [prev + (values,) for prev in positions]
        if len(positions) > _AST_MAX_CANDIDATES:
            return None
    fields = _eval_ast_path_candidates(ast.Dict(
        keys=[ast.Constant(value=kw.arg) if kw.arg is not None else None for kw in node.keywords],
        values=[kw.value for kw in node.keywords],
    ), bindings)
    if any(not isinstance(value, _AstDict) or value.uncertain for value in fields):
        return None
    if len(positions) * len(fields) > _AST_MAX_CANDIDATES:
        return None
    return [
        (args[0] if args else _ast_dict_field(kw.fields, ("name", "tool_name", "ToolName")),
         args[1] if len(args) > 1 else _ast_dict_field(kw.fields, ("args", "Arguments", "arguments")))
        for args in positions for kw in fields
    ]


def _scan_workflow_python_code(
    py_code: str,
    base_cwd: Optional[str],
    allowed_norm_keys: List[str],
    allowed_resolved_keys: List[str],
    allow_os_temp: bool,
    depth: int = 0,
) -> Optional[Tuple[str, str, str]]:
    """审查 run_workflow 动态工作流脚本中的直接 Python 越界写入及 call_tool / agent 越界调用"""
    try:
        tree = ast.parse(py_code)
    except SyntaxError:
        return "动态工作流脚本语法错误，无法静态核验", "<INVALID_WORKFLOW>", _compact_snippet(py_code)
    direct_hit = _scan_inline_python_code(
        py_code, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, depth, tree=tree,
    )
    if direct_hit:
        return direct_hit

    bindings = {_AST_CWD: frozenset((base_cwd or "",))}

    def _bad(target) -> bool:
        return _ast_bad_target(target, bindings, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp)

    for node, bindings in _iter_ast_calls_in_order(tree, bindings):
        fn = node.func
        fn_name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
        snippet = _compact_snippet(ast.get_source_segment(py_code, node) or "workflow call")
        chdir_hit = _track_ast_chdir(node, bindings, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, snippet)
        if chdir_hit:
            return chdir_hit

        if fn_name == "call_tool":
            tool_arguments = _ast_workflow_tool_arguments(node, bindings)
            if tool_arguments is None:
                if _AST_DEFINITION in bindings:
                    continue
                return "动态工作流 call_tool 解包参数无法静态核验", "<DYNAMIC_UNRESOLVED>", snippet
            for raw_name, param_values in ((name, params) for names, params in tool_arguments for name in names):
                if not isinstance(raw_name, str):
                    if _AST_DEFINITION in bindings:
                        continue
                    return "动态工作流 call_tool 工具名无法静态确定", "<DYNAMIC_UNRESOLVED>", snippet
                tname = handoff_gate.AGY_TOOL_ALIASES.get(raw_name, raw_name)
                if tname not in (*FILE_MUTATION_TOOLS, "run_command", "run_workflow"):
                    continue
                for params in param_values:
                    if not isinstance(params, _AstDict) or params.uncertain:
                        if _AST_DEFINITION in bindings:
                            continue
                        return f"动态工作流 call_tool({raw_name}) 参数无法静态核验", "<DYNAMIC_UNRESOLVED>", snippet
                    fields = params.fields
                    if tname in FILE_MUTATION_TOOLS:
                        targets = _ast_dict_field(fields, FILE_PATH_KEYS)
                        if _bad(targets):
                            return f"动态工作流 call_tool({raw_name}) 修改项目外或动态路径", _format_ast_candidates(targets), snippet
                        continue
                    cwds = _ast_dict_field(fields, ("Cwd", "cwd"), bindings.get(_AST_CWD, base_cwd or ""))
                    if _bad(cwds):
                        return f"动态工作流 call_tool({raw_name}) 指定项目外或动态 Cwd", _format_ast_candidates(cwds), snippet
                    resolved_cwds = _ast_product(
                        [_ast_values(bindings.get(_AST_CWD, base_cwd or "")), cwds],
                        lambda parts: _ast_resolve_cwd(parts[1], parts[0]),
                    )
                    for cwd in resolved_cwds:
                        if not isinstance(cwd, str):
                            if _AST_DEFINITION in bindings:
                                continue
                            return "动态工作流工作目录无法静态确定", "<DYNAMIC_UNRESOLVED>", snippet
                        if tname == "run_command":
                            commands = _ast_dict_field(fields, ("CommandLine", "command_line", "command"))
                            for command in commands:
                                if not isinstance(command, str):
                                    return "动态工作流 run_command 命令无法静态确定", "<DYNAMIC_UNRESOLVED>", snippet
                                hit = _detect_command_external_mutation(command, cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp)
                                if hit:
                                    return f"动态工作流 call_tool(run_command): {hit[0]}", hit[1], hit[2]
                        else:
                            if depth >= 3:
                                return "动态工作流嵌套超出静态审查上限", "<DYNAMIC_UNRESOLVED>", snippet
                            for keys, is_path in ((("Script", "script"), False), (("ScriptPath", "scriptPath", "script_path"), True)):
                                scripts = _ast_dict_field(fields, keys, "")
                                for script in scripts:
                                    if not isinstance(script, str):
                                        return "嵌套动态工作流脚本无法静态确定", "<DYNAMIC_UNRESOLVED>", snippet
                                    hit = _detect_workflow_external_mutation(
                                        {"Cwd": cwd, "ScriptPath" if is_path else "Script": script}, cwd, cwd, [],
                                        allowed_norm_keys, allowed_resolved_keys, allow_os_temp, depth + 1,
                                    ) if script else None
                                    if hit:
                                        return hit
                            if not any(key in fields for key in ("Script", "script", "ScriptPath", "scriptPath", "script_path")):
                                return "嵌套动态工作流缺少可核验脚本", "<DYNAMIC_UNRESOLVED>", snippet

        elif fn_name == "agent":
            for kw in node.keywords:
                if kw.arg in ("cwd", "Cwd"):
                    ag_cwd = _eval_ast_path_candidates(kw.value, bindings)
                    if _bad(ag_cwd):
                        return "动态工作流 agent() 指定项目外工作目录", _format_ast_candidates(ag_cwd), snippet
                elif kw.arg is None:
                    for params in _eval_ast_path_candidates(kw.value, bindings):
                        if not isinstance(params, _AstDict) or params.uncertain:
                            if _AST_DEFINITION in bindings:
                                continue
                            return "动态工作流 agent() 解包参数无法静态核验", "<DYNAMIC_UNRESOLVED>", snippet
                        ag_cwd = _ast_dict_field(params.fields, ("cwd", "Cwd"), frozenset())
                        if _bad(ag_cwd):
                            return "动态工作流 agent() 指定项目外工作目录", _format_ast_candidates(ag_cwd), snippet
    return None


def _is_trusted_readonly_skill_root(target_path: Path) -> bool:
    gemini_home = Path.home() / ".gemini"
    skill_roots = (
        gemini_home / "antigravity" / "builtin" / "skills",
        gemini_home / "antigravity" / "skills",
        gemini_home / "antigravity" / "plugins",
        gemini_home / "config" / "skills",
        gemini_home / "config" / "plugins",
    )
    return any(_is_inside_dir(target_path, root) for root in skill_roots if root.exists())


def _detect_workflow_external_mutation(
    args: Dict[str, Any],
    payload_cwd: Optional[str],
    base_cwd: Optional[str],
    workspace_paths: List[str],
    allowed_norm_keys: List[str],
    allowed_resolved_keys: List[str],
    allow_os_temp: bool,
    depth: int = 0,
) -> Optional[Tuple[str, str, str]]:
    """在 run_workflow 启动前完成外层一次性 R4 路径与内联脚本预审"""
    raw_wf_cwd = str(args.get("Cwd") or args.get("cwd") or "").strip()
    if raw_wf_cwd and not _is_write_target_allowed(
        raw_wf_cwd, payload_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp
    ):
        return "在项目目录外指定动态工作流运行目录 (run_workflow Cwd)", raw_wf_cwd, f"Cwd={raw_wf_cwd}"

    script_src = str(args.get("Script") or args.get("script") or "")
    if script_src.strip():
        hit = _scan_workflow_python_code(script_src, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, depth)
        if hit:
            return hit

    raw_script_path = str(args.get("ScriptPath") or args.get("scriptPath") or args.get("script_path") or "").strip()
    if raw_script_path:
        try:
            resolved_sp = resolve_target_path(raw_script_path, base_cwd, workspace_paths)
            is_file = resolved_sp.is_file()
        except (OSError, ValueError, RuntimeError):
            return "无法定位动态工作流脚本 (run_workflow ScriptPath)", raw_script_path, f"ScriptPath={raw_script_path}"
        in_allowed = _is_write_target_allowed(
            raw_script_path, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp
        )
        in_skill_root = _is_trusted_readonly_skill_root(resolved_sp)
        if not in_allowed and not in_skill_root:
            return "加载并执行项目目录外的动态工作流脚本 (run_workflow ScriptPath)", raw_script_path, f"ScriptPath={raw_script_path}"
        if is_file:
            try:
                if resolved_sp.stat().st_size <= MAX_WORKFLOW_SCRIPT_BYTES:
                    file_src = resolved_sp.read_text(encoding="utf-8-sig")
                    sp_hit = _scan_workflow_python_code(
                        file_src, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, depth
                    )
                    if sp_hit:
                        return sp_hit
                else:
                    return "动态工作流脚本体积超出静态审查上限 (run_workflow ScriptPath)", raw_script_path, f"ScriptPath={raw_script_path}"
            except (OSError, UnicodeError):
                return "无法核实动态工作流脚本安全性 (run_workflow ScriptPath)", raw_script_path, f"ScriptPath={raw_script_path}"
        else:
            return "无法定位动态工作流普通脚本文件 (run_workflow ScriptPath)", raw_script_path, f"ScriptPath={raw_script_path}"
    return None


# ⑥ Shell 子句、写入路径与审批响应

def _compact_snippet(text: str, max_len: int = 140) -> str:
    flat = re.sub(r"\s+", " ", text.strip())
    return flat if len(flat) <= max_len else flat[: max_len - 3] + "..."


def _unwrap_shell_script_layer(cmd: str) -> Optional[str]:
    """若整条命令是 wsl/bash/powershell -c '<inner>' 包装，提取内层脚本以精确解析子句命令头"""
    m = re.match(
        r"^\s*(?:[A-Za-z_]\w*=\S+\s+)*"
        r"""(?:wsl(?:\.exe)?\b(?:\s+(?:-(?:d|u)|--(?:distribution|user|cd))\s+(?:"(?:\\.|[^"\\])*"|'(?:''|[^'])*'|\S+)|\s+(?:--|-e|--exec|-[A-Za-z]+))*\s+)?"""
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

    sanitized = (_COMMIT_MSG_RE.sub(r'\1"__COMMIT_MSG__"', command_line)
                 if "commit" in command_line.casefold() else command_line)

    # 先拆壳并隔离内联 Python，再扫描 Shell；Python 字符串中的命令只是数据。
    unwrapped = _unwrap_shell_script_layer(sanitized)
    if unwrapped:
        return _detect_command_external_mutation(
            unwrapped, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp,
            depth + 1, wsl_distribution,
        )
    # 先处理明确的目录切换；后续相对写入应以切换后的目录为基准。
    first_clauses = handoff_gate._split_shell_clauses(sanitized)
    if len(first_clauses) > 1:
        first = first_clauses[0]
        first_tokens = _safe_split_tokens(first)
        if first_tokens and first_tokens[0].casefold() in ("cd", "chdir", "set-location"):
            targets = _extract_clause_path_targets(first_tokens)
            if targets:
                targets[0] = _expand_host_vars(targets[0])
            if len(targets) == 1 and not targets[0].startswith(("$", "%")) and not _SHELL_REDIRECT_RE.search(first):
                end = sanitized.find(first) + len(first)
                separator = re.match(r"\s*(?:&&|;|\r?\n)\s*", sanitized[end:])
                if separator:
                    return _detect_command_external_mutation(
                        sanitized[end + separator.end():], _ast_resolve_cwd(targets[0], base_cwd),
                        allowed_norm_keys, allowed_resolved_keys, allow_os_temp, depth + 1, wsl_distribution,
                    )
    py_blocks, shell_masked = _extract_and_mask_inline_python(sanitized)
    for block in py_blocks:
        py_hit = _scan_inline_python_code(
            block, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, depth, wsl_distribution
        )
        if py_hit:
            return py_hit

    for inner in handoff_gate._extract_cmd_substitutions(shell_masked):
        hit = _detect_command_external_mutation(
            inner, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp,
            depth + 1, wsl_distribution,
        )
        if hit:
            return hit

    # 1. 专项拦截: git worktree add 指向项目目录外 (如 ../.codex-worktrees 或父目录)
    for m_wt in _GIT_WORKTREE_ADD_RE.finditer(shell_masked):
        if _pos_inside_quotes(shell_masked, m_wt.start()):
            continue
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
    for m_inst in _INSTALLER_INVOKE_RE.finditer(shell_masked):
        if _pos_inside_quotes(shell_masked, m_inst.start()):
            continue
        prefix_seg = shell_masked[m_inst.start():m_inst.start(1)]
        if re.search(r"(?:^|\s)-m\s+py_compile\b", prefix_seg, re.IGNORECASE):
            continue
        tail_args = m_inst.group(1) or ""
        arg_tokens = _safe_split_tokens(tail_args)
        value_flags = {"--app", "--target-dir", "--codex-home", "--codex-skills-dir", "--zotero-cmd",
                       "--codex-handoff-mode", "--codex-handoff-compactions"}
        options = {flag: [] for flag in value_flags}
        switches = set()
        idx = 0
        while idx < len(arg_tokens):
            token = arg_tokens[idx]
            flag, equal, value = token.partition("=")
            idx += 1
            if flag in value_flags:
                if not equal:
                    value = arg_tokens[idx] if idx < len(arg_tokens) else ""
                    idx += 1
                options[flag].append(value)
            else:
                switches.add(token.lower())
        if switches & {"--dry-run", "-dryrun", "--probe-apps"}:
            continue
        target_dirs = options["--target-dir"]
        skills_dirs = options["--codex-skills-dir"]
        codex_homes = options["--codex-home"]
        app = options["--app"][-1] if options["--app"] else "auto"
        complete = {
            "both": bool(target_dirs and (codex_homes or skills_dirs)),
            "antigravity": bool(target_dirs),
            "codex": bool(codex_homes or skills_dirs),
            "auto": bool(target_dirs or codex_homes or skills_dirs),
        }.get(app, False)
        if complete and "--no-zotero" in switches:
            if all(value and _is_write_target_allowed(
                value, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
            ) for value in target_dirs + skills_dirs + codex_homes):
                continue
        return (
            "执行全局配置同步安装 (将更新本机 ~/.gemini/config 与/或 ~/.codex)",
            "~/.gemini/config/plugins/user-guard, ~/.codex",
            _compact_snippet(m_inst.group(0)),
        )

    # 3. 检查 PowerShell .NET [IO.File] / [IO.Directory] 写删方法
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

    # 4. 检查外层 Shell 重定向 (> / >>) 的直接输出目标（忽略位于引号字面量内的符号）
    for m_red in _SHELL_REDIRECT_RE.finditer(shell_masked):
        if _pos_inside_quotes(shell_masked, m_red.start()):
            continue
        op, red_tok = m_red.group(1), m_red.group(2).strip().strip("\"'")
        if not _is_write_target_allowed(
            red_tok, base_cwd, allowed_norm_keys, allowed_resolved_keys, allow_os_temp, wsl_distribution
        ):
            return f"Shell 输出重定向写入 ({op})", red_tok, _compact_snippet(m_red.group(0))

    # 5. 按子句严格检查处于“命令头位置”的 Shell/PowerShell 写删移动复制命令
    clauses = handoff_gate._split_shell_clauses(shell_masked)
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


# ⑦ PreToolUse 调度与命令行主入口

def handle_pre_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    """处理 PreToolUse 门禁请求"""
    conversation_id = str(payload.get("conversationId") or "unknown_session").strip()
    workspace_paths = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
    transcript_path_str = payload.get("transcriptPath")

    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    raw_tool_name = str(tool_call.get("name") or tool_call.get("tool_name") or "").strip()
    tool_name = handoff_gate.AGY_TOOL_ALIASES.get(raw_tool_name, raw_tool_name)
    display_tool = raw_tool_name or tool_name
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}

    payload_cwd = payload.get("cwd") or (workspace_paths[0] if workspace_paths else None)
    command_cwd = (args.get("Cwd") or args.get("cwd")) if tool_name in ("run_command", "run_workflow") else None
    cwd = _ast_resolve_cwd(str(command_cwd), payload_cwd) if command_cwd else payload_cwd
    state_dir, logs_dir = get_data_dirs()

    boundary: Optional[Dict[str, Any]] = None
    # 分支 0: 项目目录外文件改动硬边界门禁 (R4: 跨表面文件修改工具 / run_command / run_workflow)
    if tool_name in (*FILE_MUTATION_TOOLS, "run_command", "run_workflow"):
        proj_keys, allowed_keys, allowed_res_keys, proj_display, base_cwd = _collect_boundary_context(
            payload, state_dir, logs_dir
        )
        if proj_keys:
            allow_os_temp = not any(_is_standard_os_temp_key(pk) for pk in proj_keys)
            if tool_name in FILE_MUTATION_TOOLS:
                write_arg = str(
                    args.get("Path")
                    or args.get("path")
                    or args.get("AbsolutePath")
                    or args.get("absolute_path")
                    or args.get("FilePath")
                    or args.get("TargetFile")
                    or args.get("target_file")
                    or args.get("file_path")
                    or args.get("NotebookPath")
                    or args.get("notebook_path")
                    or ""
                ).strip()
                if write_arg and not _is_write_target_allowed(
                    write_arg, base_cwd, allowed_keys, allowed_res_keys, allow_os_temp
                ):
                    if tool_name in EDIT_FILE_TOOLS:
                        t_lines = len(str(args.get("Target") or args.get("TargetContent") or "").splitlines()) or 1
                        r_lines = len(str(args.get("Replacement") or args.get("ReplacementContent") or "").splitlines())
                        act = f"修改项目外已有文件 ({display_tool}: 替换 {t_lines} 行 -> {r_lines} 行)"
                    elif args.get("Append"):
                        content_txt = str(args.get("Content") or args.get("CodeContent") or "")
                        act = f"向项目外文件追加内容 ({display_tool} Append=true, {len(content_txt)} 字符)"
                    else:
                        content_txt = str(args.get("Content") or args.get("CodeContent") or "")
                        act = f"创建或覆写项目外文件 ({display_tool}, {len(content_txt)} 字符)"
                    meta = args.get("ArtifactMetadata") if isinstance(args.get("ArtifactMetadata"), dict) else {}
                    summary_txt = str(meta.get("Summary") or "").splitlines()[0].strip() if meta.get("Summary") else ""
                    boundary = _build_boundary_violation_response(
                        display_tool, act, write_arg, proj_display, conversation_id, tool_summary=summary_txt
                    )
            elif tool_name == "run_workflow":
                wf_desc = str(args.get("Description") or args.get("description") or "").strip()
                wf_hit = _detect_workflow_external_mutation(
                    args,
                    str(payload.get("cwd") or proj_display.split(",")[0].strip()),
                    base_cwd,
                    workspace_paths,
                    allowed_keys,
                    allowed_res_keys,
                    allow_os_temp,
                )
                if wf_hit:
                    act_desc, ext_target, clause_snip = wf_hit
                    boundary = _build_boundary_violation_response(
                        display_tool,
                        act_desc,
                        ext_target,
                        proj_display,
                        conversation_id,
                        tool_summary=wf_desc,
                        snippet=clause_snip,
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
                        display_tool,
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


def main() -> None:
    payload: Dict[str, Any] = {}
    try:
        payload = user_words.read_hook_payload()
    except (OSError, ValueError) as e:
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

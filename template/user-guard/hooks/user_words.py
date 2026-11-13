"""
user_words.py - 反重力与 Codex 双端会话用户原话纯标准库解析与基础设施模块 (user-guard)

职责：
1. 提供全平台 UTF-8 标准流自愈、原子写盘与 PLUGIN_DATA 数据目录解析基础设施；
2. 解析 Antigravity 会话 transcript.jsonl，并在存在 truncated_fields 时以 O(N+M) 单次索引回退补全 transcript_full.jsonl；
3. 解析 Codex 会话 rollout-*.jsonl（含跨 sessions / archived_sessions 多分段接续合并、provenance 来源鉴别与 SHA-256 完整性校验）；
4. 安全提取用户原始输入与交互式选项回答，按序格式化并原子写入会话原话归档 Markdown 文档。
"""

import contextlib
import errno
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple


def ensure_utf8_stdio() -> None:
    """全平台强制标准流为 UTF-8 编码，消除 Windows GBK 管道及 POSIX C locale 编解码崩溃"""
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


ensure_utf8_stdio()


def resolve_data_dirs(hooks_dir: Path) -> Tuple[Path, Path]:
    """
    解析运行时状态目录与日志目录优先级：
    1. 显式环境变量 USER_GUARD_STATE_DIR / USER_GUARD_LOGS_DIR（测试沙箱隔离）；
    2. Antigravity 官方注入的持久化插件数据目录 PLUGIN_DATA（保持插件代码目录零污染）；
    3. 本地回退目录 hooks_dir/state 与 hooks_dir/logs。
    """
    plugin_data = os.environ.get("PLUGIN_DATA", "").strip()
    default_base = Path(plugin_data) if plugin_data else hooks_dir

    state_env = os.environ.get("USER_GUARD_STATE_DIR", "").strip()
    logs_env = os.environ.get("USER_GUARD_LOGS_DIR", "").strip()

    state_dir = Path(state_env) if state_env else (default_base / "state")
    logs_dir = Path(logs_env) if logs_env else (default_base / "logs")
    return state_dir, logs_dir


def resolve_compact_transcript_path(raw_path: Path) -> Path:
    """
    Antigravity 宿主传入 transcriptPath 时可能指向 transcript_full.jsonl；
    若同目录存在紧凑版 transcript.jsonl，优先定位至 transcript.jsonl，
    既严格对齐 4MB/6MB 遥测标定基准，又将增量扫描与原话索引体积降低约 3 倍（截断正文按需回查 transcript_full.jsonl）。
    """
    if raw_path.name == "transcript_full.jsonl":
        compact = raw_path.with_name("transcript.jsonl")
        if compact.exists() and compact.is_file():
            return compact
    return raw_path


def atomic_write_text(target_path: Path, content: str) -> None:
    """使用同目录临时文件 + fsync + os.replace 原子更新文件，防止并发或中断产生半截损坏文件"""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        for attempt in range(5):
            try:
                os.replace(tmp_path, target_path)
                break
            except PermissionError:
                if attempt >= 4:
                    raise
                time.sleep(0.01)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(target_path: Path, data: Dict[str, Any]) -> None:
    """原子写入 JSON 状态文件"""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(target_path, text)


def valid_session_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value))


@contextlib.contextmanager
def session_state_lock(state_dir: Path, session_id: str, timeout_sec: float = 1.5) -> Iterator[None]:
    """串行化同会话状态事务。锁文件作为稳定锚点，进程退出由内核释放锁。"""
    if not valid_session_id(session_id):
        raise ValueError("会话标识缺失或不合法")
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f".{session_id}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_sec
    token = uuid.uuid4().hex.encode("ascii")
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("未取得会话状态锁，本次未更新状态") from exc
                time.sleep(0.005)

        os.lseek(fd, 1, os.SEEK_SET)
        os.write(fd, token)
        try:
            yield
        finally:
            os.lseek(fd, 1, os.SEEK_SET)
            if os.read(fd, len(token)) == token:
                os.ftruncate(fd, 1)
    finally:
        os.close(fd)


# 系统自动注入的伪消息特征（子代理消息筛选时排除）
SYSTEM_NOISE_PATTERNS = (
    "<EPHEMERAL_MESSAGE>",
    "<CONVERSATION_SUMMARIES>",
    "<TASK_NOTIFICATION>",
    "The following system message was injected by a",
    "[Notice] All your subagents and background tasks have been stopped",
)

TOOL_TIMING_HEADER_RE = re.compile(
    r"^(?:(?:Created|Completed) At:\s*[^\r\n]+\r?\n)+",
    re.MULTILINE,
)


def _extract_outer_user_request(text: str) -> Optional[str]:
    """
    安全提取最外层 <USER_REQUEST>...</USER_REQUEST> 内容。
    只识别块首宿主封装，保留用户正文中间出现的同名标签。
    """
    open_tag = "<USER_REQUEST>"
    close_tag = "</USER_REQUEST>"
    start_idx = 0
    # 宿主可能在用户请求之前附加一个独立的 ephemeral 封装。
    ephemeral = "<EPHEMERAL_MESSAGE>"
    if text.startswith(ephemeral):
        end_ephemeral = text.find("</EPHEMERAL_MESSAGE>")
        if end_ephemeral >= 0:
            start_idx = end_ephemeral + len("</EPHEMERAL_MESSAGE>")
            start_idx += len(text[start_idx:]) - len(text[start_idx:].lstrip())
    if not text.startswith(open_tag, start_idx):
        return None

    body_start = start_idx + len(open_tag)
    end_idx = text.rfind(close_tag, body_start)
    while end_idx >= body_start:
        trailer = text[end_idx + len(close_tag):].lstrip()
        if not trailer or trailer.startswith(("<ADDITIONAL_METADATA>", "<USER_SETTINGS_CHANGE>")):
            return text[body_start:end_idx].strip()
        end_idx = text.rfind(close_tag, body_start, end_idx)
    return text[body_start:].strip()


def clean_user_content(raw_content: str, step_type: str = "USER_INPUT") -> str:
    """清洗并提取纯净的用户原始输入或父代理任务书"""
    if not isinstance(raw_content, str) or not raw_content:
        return ""

    text = raw_content.strip()

    if step_type == "USER_INPUT":
        extracted = _extract_outer_user_request(text)
        if extracted is not None:
            return extracted

        return text

    if step_type == "SYSTEM_MESSAGE":
        for noise in SYSTEM_NOISE_PATTERNS:
            if noise in text:
                return ""
        # 仅提取非 system 发送的跨代理任务消息（如父代理派发给子代理的指令）
        msg_match = re.search(r"\[Message\][^\n]*?\bsender=([^\s]+)[^\n]*?\bcontent=", text)
        if msg_match:
            sender = msg_match.group(1).strip().lower()
            if sender == "system":
                return ""
            content_start = msg_match.end()
            close_idx = text.rfind("</SYSTEM_MESSAGE>", content_start)
            body = text[content_start:close_idx] if close_idx != -1 else text[content_start:]
            return body.strip()
        return ""

    return ""


MARKDOWN_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
GOAL_START_RE = re.compile(r"^/goal(?:\s|$)", re.IGNORECASE)
GOAL_CANCEL_RE = re.compile(r"^(?:请)?(?:取消|停止|结束|关闭)\s*/?goal\b", re.IGNORECASE)


def agy_goal_command(text: str) -> Optional[bool]:
    if GOAL_CANCEL_RE.match(text):
        return False
    if GOAL_START_RE.match(text):
        return True
    return None


def markdown_unquoted_lines(text: str) -> Iterator[str]:
    """返回 Markdown 正文行，排除引用、围栏代码块和缩进代码块。"""
    fence = ""
    for line in text.splitlines():
        match = MARKDOWN_FENCE_RE.match(line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = ""
        elif match:
            fence = match[1]
        elif not line.startswith(("    ", "\t")) and not line.lstrip().startswith(">"):
            yield line.strip()
            continue
        yield ""


def has_goal_cancel_marker(text: str) -> bool:
    marker = "<!-- GOAL_CANCELLED -->"
    lines = text.rstrip().splitlines()
    return bool(lines and lines[-1] == marker and list(markdown_unquoted_lines(text.rstrip()))[-1] == marker)


def _load_truncated_contents_map(full_transcript_path: Path, needed_indices: Set[int]) -> Dict[int, str]:
    """
    单次线性扫描 transcript_full.jsonl，按需补全所有被截断步骤的完整 content。
    时间复杂度严格 O(M)，找齐所需步骤后立即提前退出，杜绝 O(N*M) 重复扫描。
    """
    resolved: Dict[int, str] = {}
    if not needed_indices or not full_transcript_path.exists():
        return resolved

    remaining = set(needed_indices)
    try:
        with open(full_transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not remaining:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    step = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(step, dict) or "content" in (step.get("truncated_fields") or []):
                    continue
                idx = step.get("step_index")
                if isinstance(idx, int) and idx in remaining:
                    content = step.get("content")
                    if isinstance(content, str):
                        resolved[idx] = content
                        remaining.discard(idx)
    except Exception:
        pass

    return resolved


def _ask_question_title(call: Dict[str, Any]) -> str:
    questions_titles: List[str] = []
    args = call.get("args") or call.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    qs = args.get("questions") if isinstance(args, dict) else None
    if isinstance(qs, str):
        try:
            qs = json.loads(qs)
        except ValueError:
            qs = None
    if isinstance(qs, list):
        for q in qs:
            if isinstance(q, dict) and isinstance(q.get("question"), str):
                questions_titles.append(q["question"].strip())
    return " / ".join(questions_titles) or "交互式选项提问"


def _tool_call_id(record: Dict[str, Any], *, is_call: bool = False) -> str:
    keys = ("tool_call_id", "call_id", "toolCallId", "callId")
    if is_call:
        keys += ("id",)
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clean_generic_tool_answer(raw_output: str) -> str:
    """清洗 ask_question 对应的 GENERIC 步骤输出"""
    if not raw_output:
        return ""
    cleaned = TOOL_TIMING_HEADER_RE.sub("", raw_output).strip()
    if not cleaned or cleaned.startswith("Encountered error in tool execution"):
        return ""
    return cleaned


def extract_agy_archive(
    transcript_path_str: Optional[str],
    include_subagent_msgs: bool = True,
) -> Dict[str, Any]:
    """提取原话和来源缺口，仅归档可关联的选项回答。"""
    result: Dict[str, Any] = {"words": [], "unresolved_steps": [], "unverified_answers": [], "issues": []}
    if not transcript_path_str:
        result["issues"].append("未提供 transcriptPath")
        return result

    transcript_path = resolve_compact_transcript_path(Path(transcript_path_str).expanduser())
    full_transcript_path = transcript_path.parent / "transcript_full.jsonl"

    raw_candidates: List[Dict[str, Any]] = []
    truncated_needed: Set[int] = set()
    pending_questions: Dict[str, Tuple[int, str]] = {}
    unidentified_questions: Set[int] = set()
    ambiguous_outputs: Dict[int, Set[int]] = {}

    try:
        with open(transcript_path, "rb") as f:
            for line_num, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    step = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    result["issues"].append(f"{transcript_path.name} 第 {line_num + 1} 行未能解析")
                    continue
                if not isinstance(step, dict):
                    result["issues"].append(f"{transcript_path.name} 第 {line_num + 1} 行缺少步骤对象")
                    continue

                step_type = step.get("type", "")
                source = step.get("source", "")
                step_index = step.get("step_index")
                if not isinstance(step_index, int):
                    step_index = line_num
                created_at = step.get("created_at", "")
                truncated_fields = step.get("truncated_fields") or []
                is_content_truncated = isinstance(truncated_fields, list) and "content" in truncated_fields

                if step_type == "PLANNER_RESPONSE":
                    calls = step.get("tool_calls")
                    if isinstance(calls, list):
                        for call in calls:
                            if not isinstance(call, dict) or call.get("name") != "ask_question":
                                continue
                            call_id = _tool_call_id(call, is_call=True)
                            if call_id and call_id not in pending_questions:
                                pending_questions[call_id] = (step_index, _ask_question_title(call))
                            else:
                                unidentified_questions.add(step_index)
                    continue

                if step_type in ("GENERIC", "TOOL_RESULT", "TOOL_RESPONSE"):
                    tool_result = step.get("tool_result")
                    tool_result = tool_result if isinstance(tool_result, dict) else step
                    call_id = _tool_call_id(tool_result) or _tool_call_id(step)
                    named_ask = (tool_result.get("tool_name") or tool_result.get("name")) == "ask_question"
                    question = pending_questions.pop(call_id, None)
                    if step.get("error") or tool_result.get("error"):
                        continue
                    if question is None:
                        waiting = unidentified_questions | {pos for pos, _ in pending_questions.values()}
                        if (not call_id and waiting) or named_ask:
                            ambiguous_outputs[step_index] = waiting if not named_ask else waiting | {step_index}
                        continue
                    q_index, q_title = question
                    for positions in ambiguous_outputs.values():
                        positions.discard(q_index)
                    if (tool_result.get("tool_name") or tool_result.get("name")) not in (None, "", "ask_question"):
                        ambiguous_outputs[step_index] = {q_index}
                        continue
                    raw_ans = step.get("content") or tool_result.get("output") or ""
                    if not isinstance(raw_ans, str):
                        raw_ans = ""
                    if is_content_truncated:
                        truncated_needed.add(step_index)
                    raw_candidates.append(
                        {
                            "step_index": step_index,
                            "created_at": created_at,
                            "step_type": "ASK_QUESTION_ANSWER",
                            "question_title": q_title,
                            "raw_content": raw_ans,
                            "needs_full": is_content_truncated,
                        }
                    )
                    continue

                is_explicit_user = step_type == "USER_INPUT" or source == "USER_EXPLICIT"
                is_subagent_msg = include_subagent_msgs and step_type == "SYSTEM_MESSAGE"

                if not (is_explicit_user or is_subagent_msg):
                    continue

                raw_content = step.get("content") or ""
                if not isinstance(raw_content, str):
                    result["issues"].append(f"步骤 {step_index} 缺少文本正文")
                    raw_content = ""
                norm_type = "USER_INPUT" if is_explicit_user else "SYSTEM_MESSAGE"
                if is_content_truncated:
                    truncated_needed.add(step_index)

                raw_candidates.append(
                    {
                        "step_index": step_index,
                        "created_at": created_at,
                        "step_type": norm_type,
                        "raw_content": raw_content,
                        "needs_full": is_content_truncated,
                    }
                )
    except OSError as exc:
        result["issues"].append(f"读取 {transcript_path.name} 失败（{exc}）")

    # 单次按需补全所有被截断的记录
    full_map = _load_truncated_contents_map(full_transcript_path, truncated_needed)
    unresolved_steps = sorted(truncated_needed.difference(full_map))

    results: List[Dict[str, Any]] = []
    turn_counter = 0

    for item in raw_candidates:
        s_idx = item["step_index"]
        raw_text = full_map.get(s_idx, item["raw_content"]) if item["needs_full"] else item["raw_content"]
        s_type = item["step_type"]

        if s_type == "ASK_QUESTION_ANSWER":
            ans_text = _clean_generic_tool_answer(raw_text)
            if not ans_text:
                continue
            q_title = item.get("question_title", "交互式选项提问")
            cleaned = f"【选项框回答 · {q_title}】\n{ans_text}"
            kind = "ask_question_answer"
        else:
            cleaned = clean_user_content(raw_text, s_type)
            if not cleaned:
                continue
            kind = "user_input" if s_type == "USER_INPUT" else "subagent_message"

        turn_counter += 1
        results.append(
            {
                "turn": turn_counter,
                "step_index": s_idx,
                "created_at": item["created_at"],
                "kind": kind,
                "content": cleaned,
                "truncated": s_idx in unresolved_steps,
            }
        )

    result.update(
        words=results,
        unresolved_steps=unresolved_steps,
        unverified_answers=sorted(index for index, questions in ambiguous_outputs.items() if questions),
    )
    return result


def extract_user_words_with_status(
    transcript_path_str: Optional[str],
    include_subagent_msgs: bool = True,
) -> Tuple[List[Dict[str, Any]], List[int]]:
    result = extract_agy_archive(transcript_path_str, include_subagent_msgs)
    return result["words"], result["unresolved_steps"]


def extract_user_words(
    transcript_path_str: Optional[str],
    include_subagent_msgs: bool = True,
) -> List[Dict[str, Any]]:
    """
    从 transcript.jsonl 提取会话全部历史用户原话（含被截断长指令还原与 ask_question 选项回答）。
    """
    words, _ = extract_user_words_with_status(transcript_path_str, include_subagent_msgs=include_subagent_msgs)
    return words


def format_user_words_markdown(conversation_id: str, words: List[Dict[str, Any]],
                               issues: Optional[List[str]] = None) -> str:
    """将提取出的用户原话格式化为持久留痕 Markdown 文档"""
    lines = [
        "# 会话用户原话归档",
        "",
        f"- **Conversation ID**: `{conversation_id}`",
        f"- **累计指令轮数**: {len(words)}",
    ]
    if issues:
        lines.extend(["- 归档状态：存在缺口，未核实完整性", *[f"  - {issue}" for issue in issues]])
    lines.extend(["", "---", ""])

    for w in words:
        time_str = f" ({w['created_at']})" if w.get("created_at") else ""
        kind_tag = ""
        if w.get("kind") == "ask_question_answer":
            kind_tag = " [选项框回答]"
        elif w.get("kind") == "subagent_message":
            kind_tag = " [父代理任务]"
        if w.get("truncated"):
            kind_tag += " [截断正文待补]"
        if w.get("provenance_verified") is False:
            kind_tag += " [用户角色，来源未核实]"
        lines.append(f"## 第 {w['turn']} 轮指令 [Step {w['step_index']}]{kind_tag}{time_str}")
        if w.get("attachments") and w.get("source_path"):
            lines.append(f"来源分段：`{w['source_path']}`")
        lines.append("")
        lines.append(w["content"])
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def save_user_words_markdown(state_dir: Path, conversation_id: str, words: List[Dict[str, Any]],
                             issues: Optional[List[str]] = None) -> Optional[Path]:
    """将用户原话列表原子保存至 state/<conversationId>_user_words.md"""
    if not words:
        return None
    out_file = state_dir / f"{conversation_id}_user_words.md"
    md_text = format_user_words_markdown(conversation_id, words, issues)
    atomic_write_text(out_file, md_text)
    return out_file


# ==============================================================================
# Codex Rollout 会话记录解析与归档 (供 --client codex 在 compact/resume 时调用)
# ==============================================================================

CODEX_META_HEAD_BYTES = 65536
CODEX_MAIN_SOURCES = {"cli", "vscode", "exec", "mcp", "desktop", "app_server"}
CODEX_SYSTEM_PROVENANCE = {"harness", "agents_md"}
CODEX_ATTACHMENTS = {"input_image": "图片", "input_audio": "音频", "input_file": "文件"}
CODEX_ARCHIVE_FORMAT = "user-guard-codex-v2"
CODEX_LEGACY_ARCHIVE_FORMAT = "user-guard-codex-v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def strip_win_extended_prefix(path_str: str) -> str:
    """剥离 Windows \\\\?\\ 扩展长度路径前缀，统一跨端路径比较与读取"""
    raw = str(path_str or "").strip()
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[8:]
    if raw.startswith("\\\\?\\"):
        return raw[4:]
    return raw


def read_codex_rollout_meta(rollout_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """仅读取 rollout 首行元数据，区分主会话与子代理会话"""
    try:
        with open(rollout_path, "rb") as f:
            first_line = f.readline(CODEX_META_HEAD_BYTES)
        if not first_line:
            return None, f"空会话文件: {rollout_path}"
        if not first_line.endswith(b"\n") and not first_line.rstrip().endswith(b"}"):
            return None, f"首行元数据未写完或超长: {rollout_path}"
        entry = json.loads(first_line)
        payload = entry.get("payload") if isinstance(entry, dict) and entry.get("type") == "session_meta" else None
        if not isinstance(payload, dict):
            return None, f"首条记录非 session_meta: {rollout_path}"
        sid = payload.get("id")
        cwd = payload.get("cwd") or ""
        source = payload.get("source")
        if not isinstance(sid, str) or not sid.strip():
            return None, f"session_meta 缺少有效 id: {rollout_path}"
        root_id = payload.get("session_id")
        thread_source = payload.get("thread_source")
        if (
            source in ("subagent", "internal")
            or (isinstance(source, dict) and ("subagent" in source or "internal" in source))
            or (isinstance(root_id, str) and root_id and root_id != sid)
            or payload.get("parent_thread_id")
            or thread_source in ("subagent", "guardian_review", "memory_consolidation")
        ):
            source_kind = "subagent"
        elif isinstance(source, str) and source.casefold() in CODEX_MAIN_SOURCES:
            source_kind = "main"
        else:
            source_kind = "unknown"
        return {"id": sid.strip(), "cwd": str(cwd), "source_kind": source_kind}, None
    except Exception as exc:
        return None, f"读取 Codex 会话元数据失败 ({rollout_path}): {exc}"


def codex_hook_session(
    payload: Dict[str, Any],
) -> Tuple[str, Optional[Path], Optional[str], bool]:
    """供两个钩子共用，返回会话标识、轨迹、未核实原因与子会话跳过标记。"""
    sid = str(payload.get("session_id") or "").strip()
    agent_id = payload.get("agent_id")
    if agent_id is not None and str(agent_id) != sid:
        return sid, None, None, True
    if not valid_session_id(sid):
        return sid, None, "hook input 缺少有效 session_id", False
    raw_path = strip_win_extended_prefix(str(payload.get("transcript_path") or ""))
    if not raw_path:
        return sid, None, "未提供 transcript_path，无法核实主会话身份", False
    transcript = Path(raw_path).expanduser()
    meta, error = read_codex_rollout_meta(transcript)
    if meta and meta["source_kind"] == "subagent":
        return sid, None, None, True
    if not meta:
        return sid, None, error or "主会话身份未核实", False
    if meta["id"] != sid:
        return sid, None, f"轨迹 session_id 不匹配（期望 {sid}，实际 {meta['id']}）", False
    if meta["source_kind"] != "main":
        return sid, None, "轨迹来源未知，主会话身份未核实", False
    return sid, transcript, None, False


def _scan_codex_home_rollouts(codex_root: Path) -> List[Path]:
    """扫描 <codex_root>/sessions/YYYY/MM/DD 与 archived_sessions 下的 rollout-*.jsonl"""
    candidates: List[Path] = []
    sessions_dir = codex_root / "sessions"
    if sessions_dir.is_dir():
        try:
            for y_dir in sessions_dir.iterdir():
                if not (y_dir.name.isdigit() and len(y_dir.name) == 4 and y_dir.is_dir()):
                    continue
                for m_dir in y_dir.iterdir():
                    if not (m_dir.name.isdigit() and len(m_dir.name) == 2 and m_dir.is_dir()):
                        continue
                    for d_dir in m_dir.iterdir():
                        if not (d_dir.name.isdigit() and len(d_dir.name) == 2 and d_dir.is_dir()):
                            continue
                        for f in d_dir.iterdir():
                            if f.name.startswith("rollout-") and f.name.endswith(".jsonl") and f.is_file():
                                candidates.append(f)
        except OSError:
            pass

    archived_dir = codex_root / "archived_sessions"
    if archived_dir.is_dir():
        try:
            for f in archived_dir.iterdir():
                if f.name.startswith("rollout-") and f.name.endswith(".jsonl") and f.is_file():
                    candidates.append(f)
        except OSError:
            pass

    return candidates


def _canonical_path_key(p: Path) -> str:
    try:
        s = str(p.resolve())
    except OSError:
        s = os.path.abspath(str(p))
    return s.casefold() if os.name == "nt" else s


def discover_codex_session_segments(
    transcript_path_str: str,
    session_id: Optional[str] = None,
) -> Tuple[List[Path], Optional[str], List[str]]:
    """发现同一 session_id 的全部主会话 rollout 分段并按时间序排列"""
    issues: List[str] = []
    cleaned_str = strip_win_extended_prefix(os.path.expanduser(str(transcript_path_str or "").strip()))
    if not cleaned_str:
        return [], session_id, ["未提供 transcript_path"]

    primary = Path(cleaned_str)
    if not primary.is_file():
        return [], session_id, [f"transcript_path 不存在: {cleaned_str}"]

    meta, err = read_codex_rollout_meta(primary)
    if err or not meta:
        return [], session_id, [err or f"无法识别元数据: {cleaned_str}"]
    if meta["source_kind"] == "subagent":
        return [], meta["id"], ["子代理 rollout 会话不执行主会话原话提取"]
    if meta["source_kind"] != "main":
        return [], session_id, [f"轨迹来源未知，无法核实主会话身份: {primary}"]
    if session_id and session_id.strip() and meta["id"] != session_id.strip():
        return [], session_id.strip(), [f"session_id 不匹配: 期望 {session_id.strip()}，实际 {meta['id']}"]

    sid = meta["id"]
    seen_keys = {_canonical_path_key(primary)}
    segments: List[Path] = [primary]

    # 向上寻找所属的 sessions 或 archived_sessions 父目录以定位 codex_root
    cur = primary.parent
    codex_root: Optional[Path] = None
    while cur != cur.parent:
        if cur.name in ("sessions", "archived_sessions"):
            codex_root = cur.parent
            break
        cur = cur.parent

    if codex_root is not None:
        for cand in _scan_codex_home_rollouts(codex_root):
            if sid not in cand.name:
                continue
            ckey = _canonical_path_key(cand)
            if ckey in seen_keys:
                continue
            c_meta, c_err = read_codex_rollout_meta(cand)
            if c_err:
                issues.append(c_err)
                continue
            if c_meta and c_meta["id"] == sid:
                if c_meta["source_kind"] == "main":
                    seen_keys.add(ckey)
                    segments.append(cand)
                elif c_meta["source_kind"] == "unknown":
                    issues.append(f"来源未核实的同 ID 分段: {cand}")

    segments.sort(key=lambda p: (p.name, str(p)))
    return segments, sid, issues


def _extract_codex_record(
    entry: Dict[str, Any],
    fallback_step: int,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """保留记录标识及内容项来源，供跨分段核对。"""
    if not isinstance(entry, dict) or entry.get("type") != "response_item":
        return None, None
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
        return None, None

    content = payload.get("content")
    meta_pass = payload.get("internal_chat_message_metadata_passthrough")
    raw_meta_items = meta_pass.get("content_item_metadata") if isinstance(meta_pass, dict) else None
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    origins = ["unknown"] * len(blocks)
    if isinstance(raw_meta_items, list) and len(raw_meta_items) == len(blocks):
        for index, item in enumerate(raw_meta_items):
            prov = item.get("provenance") if isinstance(item, dict) else None
            origin = prov.get("type") if isinstance(prov, dict) else None
            if origin == "user":
                origins[index] = "user"
            elif isinstance(origin, str) and origin in CODEX_SYSTEM_PROVENANCE:
                origins[index] = "system"

    rec_id: Optional[str] = None
    for cand_id in (payload.get("id"), entry.get("uuid")):
        if isinstance(cand_id, str) and cand_id.strip():
            rec_id = f"id:{cand_id.strip()}"
            break
    if rec_id is None and isinstance(entry.get("ordinal"), int) and isinstance(entry.get("timestamp"), str):
        identity_fields = [entry["ordinal"], entry["timestamp"], blocks]
        canonical_json = json.dumps(identity_fields, sort_keys=True, ensure_ascii=True)
        rec_id = "record:" + hashlib.sha256(canonical_json.encode("ascii")).hexdigest()
    if rec_id is None and origins and all(origin == "system" for origin in origins):
        return None, None

    return rec_id, {
        "step_index": entry.get("ordinal") if isinstance(entry.get("ordinal"), int) else fallback_step,
        "created_at": str(entry.get("timestamp") or ""),
        "blocks": blocks,
        "origins": origins,
        "content_signature": hashlib.sha256(
            json.dumps(blocks, ensure_ascii=True, sort_keys=True).encode("ascii")
        ).hexdigest(),
    }


def _codex_word_from_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    blocks = record["blocks"]
    kept_blocks: List[str] = []
    attachments: List[str] = []
    missing_content = not blocks
    provenance_verified = bool(blocks)
    for block, origin in zip(blocks, record["origins"]):
        if origin in ("system", "conflict"):
            continue
        if origin != "user":
            provenance_verified = False
        block_type = block.get("type") if isinstance(block, dict) else None
        if block_type in ("input_text", "text") and isinstance(block.get("text"), str):
            kept_blocks.append(block["text"])
        elif isinstance(block_type, str) and block_type in CODEX_ATTACHMENTS:
            label = CODEX_ATTACHMENTS[block_type]
            attachments.append(label)
            kept_blocks.append(f"【{label}附件，内容见原始轨迹】")
            if not any(block.get(key) for key in (
                "image_url", "file_id", "audio_url", "audio", "data", "file_data", "file_url",
            )):
                missing_content = True
        else:
            missing_content = True
    if missing_content:
        kept_blocks.append("【存在无法归档的输入内容，待核对原始轨迹】")
    if not kept_blocks:
        return None

    merged_text = "\n\n".join(kept_blocks)
    kind = "user_input"
    if provenance_verified and not missing_content and not attachments and re.fullmatch(
        r"<send_user_message_question_reply>[\s\S]+</send_user_message_question_reply>",
        merged_text.strip(),
    ):
        kind = "ask_question_answer"

    return {
        "step_index": record["step_index"],
        "created_at": record["created_at"],
        "source_path": record["source_path"],
        "kind": kind,
        "content": merged_text,
        "provenance_verified": provenance_verified,
        "attachments": attachments,
        "content_verified": not missing_content,
    }


def _merge_codex_record_origins(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Tuple[bool, bool]:
    """返回是否纠正了归属、是否遇到新的明确来源冲突。"""
    previous = existing["origins"]
    updated = list(previous)
    corrected = False
    conflict = False
    for index, (old, new) in enumerate(zip(previous, incoming["origins"])):
        if old == "unknown":
            updated[index] = new
            corrected |= new == "system"
        elif old != "conflict" and new not in ("unknown", old):
            updated[index] = "conflict"
            conflict = True
    if updated != previous:
        existing["origins"] = updated
        existing["source_path"] = incoming["source_path"]
        existing["step_index"] = incoming["step_index"]
        existing["created_at"] = incoming["created_at"]
    return corrected, conflict


def extract_codex_session_words(
    transcript_path_str: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """合并解析同一 Codex 主会话所有 rollout 分段的用户原话并生成完整性摘要"""
    segments, resolved_sid, issues = discover_codex_session_segments(transcript_path_str, session_id=session_id)
    if not segments:
        return {
            "session_id": resolved_sid or session_id or "unknown",
            "words": [],
            "segments": [],
            "sources": [],
            "issues": issues or ["未找到可读的 Codex 会话分段"],
            "complete": False,
            "provenance_corrected": False,
        }

    records: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    records_by_id: Dict[str, List[int]] = {}
    provenance_corrected = False

    for seg_path in segments:
        digest = hashlib.sha256()
        offset = 0
        bad_lines = 0
        line_counter = 0

        try:
            initial_size = seg_path.stat().st_size
            with open(seg_path, "rb") as f:
                for raw_line in f:
                    line_counter += 1
                    if not raw_line.strip():
                        offset += len(raw_line)
                        digest.update(raw_line)
                        continue
                    try:
                        entry = json.loads(raw_line)
                    except (ValueError, UnicodeDecodeError):
                        if not raw_line.endswith(b"\n"):
                            issues.append(f"末尾存在未写完记录: {seg_path.name}")
                            break
                        bad_lines += 1
                        offset += len(raw_line)
                        digest.update(raw_line)
                        continue
                    offset += len(raw_line)
                    digest.update(raw_line)
                    if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
                        bad_lines += 1
                        continue
                    rec_id, record = _extract_codex_record(entry, fallback_step=line_counter)
                    if record is None:
                        continue
                    record["source_path"] = str(seg_path)
                    if rec_id is not None:
                        indices = records_by_id.setdefault(rec_id, [])
                        matching = next((i for i in indices if records[i]["content_signature"] ==
                                         record["content_signature"]), None)
                        if matching is not None:
                            corrected, conflict = _merge_codex_record_origins(records[matching], record)
                            provenance_corrected |= corrected or conflict
                            if conflict:
                                issues.append(f"同一记录标识的内容项存在用户与系统来源冲突（{rec_id}），冲突项未归档")
                            continue
                        indices.append(len(records))
                    records.append(record)
            if bad_lines:
                issues.append(f"存在损坏 JSON 行 ({bad_lines} 行): {seg_path.name}")
            if offset != initial_size or seg_path.stat().st_size != initial_size:
                issues.append(f"读取时分段未稳定或不完整: {seg_path.name}")
            sources.append(
                {
                    "path": str(seg_path),
                    "complete_offset": offset,
                    "observed_size": initial_size,
                    "sha256": digest.hexdigest(),
                }
            )
        except OSError as exc:
            issues.append(f"读取分段失败 ({seg_path.name}): {exc}")

    rendered = [_codex_word_from_record(record) for record in records]
    words = [word for word in rendered if word is not None]
    for rec_id, indices in records_by_id.items():
        if len(indices) > 1:
            issues.append(f"同一记录标识的内容冲突（{rec_id}），冲突版本的来源待核对")
    unknown_source_rows = sum(not item["provenance_verified"] for item in words)
    incomplete_rows = sum(not item["content_verified"] for item in words)
    if unknown_source_rows:
        issues.append(f"{unknown_source_rows} 条用户角色记录的来源未核实")
    if incomplete_rows:
        issues.append(f"{incomplete_rows} 条记录含无法核实的输入内容")
    for index, item in enumerate(words, start=1):
        item["turn"] = index

    return {
        "session_id": resolved_sid or session_id or "unknown",
        "words": words,
        "segments": [str(p) for p in segments],
        "sources": sources,
        "issues": issues,
        "complete": len(issues) == 0,
        "provenance_corrected": provenance_corrected,
    }


def _read_codex_publication(md_path: Path, meta_path: Path, sid: str) -> Dict[str, Any]:
    saved: Dict[str, Any] = {
        "file": str(md_path), "available": False, "manifest_verified": False, "verified": False,
        "preserved": False, "count": None, "last_time": None, "sources": [],
    }
    try:
        body = md_path.read_bytes()
        saved["body_sha256"] = hashlib.sha256(body).hexdigest()
        body.decode("utf-8")
    except FileNotFoundError:
        if meta_path.is_file():
            saved["issue"] = "校验清单存在，归档正文缺失"
        return saved
    except (OSError, UnicodeDecodeError):
        saved["issue"] = "归档正文无法读取"
        return saved

    saved["available"] = True
    digest = saved["body_sha256"]
    try:
        manifest = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError
        sources = manifest.get("sources")
        count = manifest.get("count")
        last_time = manifest.get("last_time")
        archive_format = manifest.get("format")
        legacy = archive_format == CODEX_LEGACY_ARCHIVE_FORMAT
        complete = manifest.get("complete")
        if (
            archive_format not in (CODEX_ARCHIVE_FORMAT, CODEX_LEGACY_ARCHIVE_FORMAT) or manifest.get("id") != sid
            or manifest.get("file") != str(md_path) or not isinstance(manifest.get("sha256"), str)
            or not SHA256_RE.fullmatch(manifest["sha256"])
            or type(count) is not int or count < 0
            or (last_time is not None and not isinstance(last_time, str))
            or (not legacy and type(complete) is not bool)
            or not isinstance(sources, list) or not sources
        ):
            raise ValueError
        for source in sources:
            if (
                not isinstance(source, dict) or not isinstance(source.get("path"), str) or not source["path"]
                or not isinstance(source.get("sha256"), str) or not SHA256_RE.fullmatch(source["sha256"])
                or type(source.get("complete_offset")) is not int or type(source.get("observed_size")) is not int
                or source["complete_offset"] < 0 or source["observed_size"] < 0
                or (not legacy and complete and source["complete_offset"] != source["observed_size"])
                or (legacy and source["complete_offset"] == 0)
            ):
                raise ValueError
    except (ValueError, OSError, UnicodeDecodeError):
        saved["issue"] = "校验清单格式无效或无法读取，归档完整性未验证"
        return saved
    if manifest["sha256"] != digest:
        saved.update(issue="归档正文哈希与校验清单不匹配", previous_manifest_sources=sources)
        if legacy:
            saved["legacy_hash_mismatch"] = True
        return saved
    saved.update(manifest_verified=True, verified=not legacy and complete is True, count=count, last_time=last_time,
                 sources=sources, sha256=digest)
    if legacy:
        saved.update(legacy_manifest=True, issue="旧版归档正文哈希与清单一致，记录完整性未验证")
    elif not complete:
        saved["issue"] = "此归档保存的是上次可恢复的部分记录"
    return saved


def _missing_codex_source_coverage(previous: List[Dict[str, Any]], current: List[Dict[str, Any]]) -> List[str]:
    """先按原路径匹配，再核对已迁移或并入其它分段的原始字节前缀。"""
    indexed = {_canonical_path_key(Path(item["path"])): item for item in current}
    prefix_digests: Dict[Tuple[str, int], Optional[str]] = {}
    missing: List[str] = []
    for old in previous:
        key = _canonical_path_key(Path(old["path"]))
        same_path = indexed.get(key)
        candidates = [same_path] if same_path is not None else current
        length = old["complete_offset"]
        for candidate in candidates:
            if candidate["complete_offset"] < length:
                continue
            if candidate["complete_offset"] == length:
                digest = candidate["sha256"]
            else:
                cache_key = candidate["path"], length
                if cache_key not in prefix_digests:
                    digest_prefix = hashlib.sha256()
                    remaining = length
                    try:
                        with Path(candidate["path"]).open("rb") as stream:
                            while remaining:
                                data = stream.read(min(remaining, 1024 * 1024))
                                if not data:
                                    break
                                remaining -= len(data)
                                digest_prefix.update(data)
                    except OSError:
                        remaining = -1
                    prefix_digests[cache_key] = digest_prefix.hexdigest() if remaining == 0 else None
                digest = prefix_digests[cache_key]
            if digest == old["sha256"]:
                break
        else:
            missing.append(old["path"])
    return missing


def refresh_codex_session_archive(
    transcript_path_str: str,
    user_words_dir: Path,
    session_id: str,
    *,
    identity_error: Optional[str] = None,
) -> Dict[str, Any]:
    """串行发布原话与校验清单。发布中断时，按磁盘上实际可核验的一对文件判定。"""
    sid = session_id.strip()
    if not valid_session_id(sid):
        return {"complete": False, "words": [], "issues": [identity_error or "无效的 session_id"],
                "saved": {}, "meta_path": "", "segments": []}
    md_path = user_words_dir / f"{sid}.md"
    meta_path = user_words_dir / f"{sid}.json"
    if identity_error and not md_path.exists() and not meta_path.exists():
        return {"complete": False, "words": [], "issues": [identity_error], "saved": {},
                "meta_path": str(meta_path), "segments": []}
    with session_state_lock(user_words_dir, sid):
        previous = _read_codex_publication(md_path, meta_path, sid)
        parsed = (
            {"complete": False, "issues": [identity_error], "words": [], "sources": [], "segments": [],
             "provenance_corrected": False}
            if identity_error else extract_codex_session_words(transcript_path_str, session_id=sid)
        )
        words = parsed["words"]
        issues = list(parsed["issues"])
        complete = bool(parsed["complete"])
        saved = previous
        recorded_sources = previous.get("previous_manifest_sources") or previous["sources"]
        missing = _missing_codex_source_coverage(recorded_sources, parsed["sources"])
        if missing:
            complete = False
            issues.append("本次无法核对已有归档分段的来源覆盖：" + "、".join(missing))
        can_update_partial = parsed["provenance_corrected"] or (bool(words) and (not previous["available"] or (
            previous["manifest_verified"] and not previous["verified"] and len(words) >= previous["count"]
        )))
        md_text = format_user_words_markdown(sid, words, issues)
        md_sha = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
        body_sha = previous.get("body_sha256")
        can_restore_manifest = bool(body_sha and body_sha == md_sha and parsed["sources"])
        if previous.get("legacy_hash_mismatch"):
            can_restore_manifest = False
        unresolved_body = bool(body_sha and not previous["manifest_verified"] and not can_restore_manifest)
        if unresolved_body:
            complete = False
            issues.append("未能证明本次重建覆盖现有归档正文，已保留原正文")
        if previous.get("legacy_manifest"):
            should_publish = complete
        else:
            should_publish = complete or can_update_partial or can_restore_manifest
        if not missing and not unresolved_body and should_publish:
            try:
                manifest = {
                    "format": CODEX_ARCHIVE_FORMAT, "id": sid, "file": str(md_path), "count": len(words),
                    "last_time": words[-1]["created_at"] if words else None, "sources": parsed["sources"],
                    "sha256": md_sha, "complete": complete,
                }
                if body_sha != md_sha:
                    atomic_write_text(md_path, md_text)
                atomic_write_json(meta_path, manifest)
            except OSError as exc:
                complete = False
                issues.append(f"归档发布中断（{exc}）")
            saved = _read_codex_publication(md_path, meta_path, sid)
            if not saved["manifest_verified"]:
                complete = False
        if not complete and saved["verified"] and saved.get("sha256") == previous.get("sha256"):
            saved["preserved"] = True
        if saved.get("issue"):
            issues.append(saved["issue"])

    saved.pop("body_sha256", None)
    saved.pop("previous_manifest_sources", None)
    saved.pop("legacy_hash_mismatch", None)
    return {
        "complete": complete,
        "words": words,
        "issues": issues,
        "saved": saved,
        "meta_path": str(meta_path),
        "segments": parsed["segments"],
    }


MAX_LOG_BYTES = 2 * 1024 * 1024
TRIM_LOG_BYTES = 1 * 1024 * 1024


def current_iso_time() -> str:
    """返回带本机时区偏移的 ISO-8601 时间戳"""
    return datetime.now(timezone.utc).astimezone().isoformat()


def append_lifecycle_log(
    hooks_dir: Path,
    event_name: str,
    conversation_id: str,
    message: str,
    payload_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """向 logs/lifecycle.log 追加结构化 JSONL 日志，附带 2MB 自动滚动裁剪防无限膨胀"""
    try:
        _, logs_dir = resolve_data_dirs(hooks_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / "lifecycle.log"

        if log_file.exists() and log_file.stat().st_size > MAX_LOG_BYTES:
            raw = log_file.read_bytes()
            tail = raw[-TRIM_LOG_BYTES:]
            nl = tail.find(b"\n")
            if nl != -1:
                tail = tail[nl + 1 :]
            atomic_write_text(log_file, tail.decode("utf-8", errors="replace"))

        log_entry = {
            "timestamp": current_iso_time(),
            "event": event_name,
            "conversationId": conversation_id,
            "message": message,
            "summary": payload_summary or {},
        }
        with open(log_file, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

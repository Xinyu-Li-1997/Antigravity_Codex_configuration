"""
user_words.py - 反重力与 Codex 双端会话用户原话纯标准库解析与基础设施模块 (user-guard)

职责：
1. 提供全平台 UTF-8 标准流自愈、原子写盘与 PLUGIN_DATA 数据目录解析基础设施；
2. 解析 Antigravity 会话 transcript.jsonl，并在存在 truncated_fields 时以 O(N+M) 单次索引回退补全 transcript_full.jsonl；
3. 解析 Codex 会话 rollout-*.jsonl（含跨 sessions / archived_sessions 多分段接续合并、provenance 来源鉴别与 SHA-256 完整性校验）；
4. 安全提取用户原始输入与交互式选项回答，按序格式化并原子写入会话原话归档 Markdown 文档。
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


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


# 系统自动注入的伪消息特征（子代理消息筛选时排除）
SYSTEM_NOISE_PATTERNS = (
    "<EPHEMERAL_MESSAGE>",
    "<CONVERSATION_SUMMARIES>",
    "<TASK_NOTIFICATION>",
    "The following system message was injected by a",
    "[Notice] All your subagents and background tasks have been stopped",
)

METADATA_TAIL_MARKERS = (
    "\n<ADDITIONAL_METADATA>",
    "\r\n<ADDITIONAL_METADATA>",
    "\n<USER_SETTINGS_CHANGE>",
    "\r\n<USER_SETTINGS_CHANGE>",
)

TOOL_TIMING_HEADER_RE = re.compile(
    r"^(?:(?:Created|Completed) At:\s*[^\r\n]+\r?\n)+",
    re.MULTILINE,
)


def _extract_outer_user_request(text: str) -> Optional[str]:
    """
    安全提取最外层 <USER_REQUEST>...</USER_REQUEST> 内容。
    定位首个起始标签，并在尾部元数据块之前用 rfind 寻找真正的闭合标签，
    避免用户正文中粘贴包含 </USER_REQUEST> 字面量的代码时被提前截断。
    """
    open_tag = "<USER_REQUEST>"
    close_tag = "</USER_REQUEST>"
    start_idx = text.find(open_tag)
    if start_idx == -1:
        return None

    body_start = start_idx + len(open_tag)
    search_end = len(text)
    for marker in METADATA_TAIL_MARKERS:
        m_idx = text.find(marker, body_start)
        if m_idx != -1 and m_idx < search_end:
            search_end = m_idx

    end_idx = text.rfind(close_tag, body_start, search_end)
    if end_idx == -1:
        end_idx = text.rfind(close_tag, body_start)
    if end_idx == -1:
        return text[body_start:search_end].strip()

    return text[body_start:end_idx].strip()


def clean_user_content(raw_content: str, step_type: str = "USER_INPUT") -> str:
    """清洗并提取纯净的用户原始输入或父代理任务书"""
    if not raw_content:
        return ""

    text = raw_content.strip()

    if step_type == "USER_INPUT":
        extracted = _extract_outer_user_request(text)
        if extracted is not None:
            return extracted

        # 兼容未包裹 <USER_REQUEST> 的裸输入，剥离尾部元数据块
        cut_pos = len(text)
        for marker in METADATA_TAIL_MARKERS:
            idx = text.find(marker)
            if idx != -1 and idx < cut_pos:
                cut_pos = idx
        text = text[:cut_pos]
        text = re.sub(r"<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>", "", text)
        text = re.sub(r"<CONTEXT_SUMMARY>[\s\S]*?</CONTEXT_SUMMARY>", "", text)
        return text.strip()

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
                idx = step.get("step_index")
                if isinstance(idx, int) and idx in remaining:
                    content = step.get("content")
                    if isinstance(content, str):
                        resolved[idx] = content
                    remaining.discard(idx)
    except Exception:
        pass

    return resolved


def _extract_ask_question_summary(tool_calls: Any) -> Optional[str]:
    """检查 PLANNER_RESPONSE 是否调用了 ask_question，并返回精简问题标题"""
    if not isinstance(tool_calls, list):
        return None
    questions_titles: List[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict) or tc.get("name") != "ask_question":
            continue
        args = tc.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = None
        if isinstance(args, dict):
            qs = args.get("questions")
            if isinstance(qs, str):
                try:
                    qs = json.loads(qs)
                except Exception:
                    qs = None
            if isinstance(qs, list):
                for q in qs:
                    if isinstance(q, dict) and isinstance(q.get("question"), str):
                        questions_titles.append(q["question"].strip())
        return " / ".join(questions_titles) if questions_titles else "交互式选项提问"
    return None


def _clean_generic_tool_answer(raw_output: str) -> str:
    """清洗 ask_question 对应的 GENERIC 步骤输出"""
    if not raw_output:
        return ""
    cleaned = TOOL_TIMING_HEADER_RE.sub("", raw_output).strip()
    if not cleaned or cleaned.startswith("Encountered error in tool execution"):
        return ""
    return cleaned


def extract_user_words_with_status(
    transcript_path_str: Optional[str],
    include_subagent_msgs: bool = True,
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    提取会话全部历史用户原话，并同时返回未能在 transcript_full.jsonl 中补全的截断 step_index 列表。
    """
    if not transcript_path_str:
        return [], []

    transcript_path = resolve_compact_transcript_path(Path(transcript_path_str))
    if not transcript_path.exists() or not transcript_path.is_file():
        return [], []

    full_transcript_path = transcript_path.parent / "transcript_full.jsonl"

    raw_candidates: List[Dict[str, Any]] = []
    truncated_needed: Set[int] = set()
    pending_question_title: Optional[str] = None

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line_num, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    step = json.loads(line)
                except json.JSONDecodeError:
                    continue

                step_type = step.get("type", "")
                source = step.get("source", "")
                step_index = step.get("step_index", line_num)
                created_at = step.get("created_at", "")
                truncated_fields = step.get("truncated_fields") or []
                is_content_truncated = isinstance(truncated_fields, list) and "content" in truncated_fields

                # 1. 追踪模型发出的 ask_question 提问
                if step_type == "PLANNER_RESPONSE":
                    pending_question_title = _extract_ask_question_summary(step.get("tool_calls"))
                    continue

                # 2. 捕获紧随 ask_question 的用户选项回答
                if pending_question_title is not None and step_type == "GENERIC":
                    q_title = pending_question_title
                    pending_question_title = None
                    raw_ans = step.get("content", "") or ""
                    if is_content_truncated and isinstance(step_index, int):
                        truncated_needed.add(step_index)
                    raw_candidates.append(
                        {
                            "step_index": step_index,
                            "created_at": created_at,
                            "step_type": "ASK_QUESTION_ANSWER",
                            "question_title": q_title,
                            "raw_content": raw_ans,
                            "needs_full": is_content_truncated and isinstance(step_index, int),
                        }
                    )
                    continue
                else:
                    pending_question_title = None

                # 3. 捕获显式 USER_INPUT 或跨代理任务消息
                is_explicit_user = step_type == "USER_INPUT" or source == "USER_EXPLICIT"
                is_subagent_msg = include_subagent_msgs and step_type == "SYSTEM_MESSAGE"

                if not (is_explicit_user or is_subagent_msg):
                    continue

                raw_content = step.get("content", "") or ""
                norm_type = "USER_INPUT" if is_explicit_user else "SYSTEM_MESSAGE"
                if is_content_truncated and isinstance(step_index, int):
                    truncated_needed.add(step_index)

                raw_candidates.append(
                    {
                        "step_index": step_index,
                        "created_at": created_at,
                        "step_type": norm_type,
                        "raw_content": raw_content,
                        "needs_full": is_content_truncated and isinstance(step_index, int),
                    }
                )
    except Exception:
        return [], []

    # 单次按需补全所有被截断的记录
    full_map = _load_truncated_contents_map(full_transcript_path, truncated_needed)
    unresolved_steps = sorted(idx for idx in truncated_needed if idx not in full_map)

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
            }
        )

    return results, unresolved_steps


def extract_user_words(
    transcript_path_str: Optional[str],
    include_subagent_msgs: bool = True,
) -> List[Dict[str, Any]]:
    """
    从 transcript.jsonl 提取会话全部历史用户原话（含被截断长指令还原与 ask_question 选项回答）。
    """
    words, _ = extract_user_words_with_status(transcript_path_str, include_subagent_msgs=include_subagent_msgs)
    return words


def format_user_words_markdown(conversation_id: str, words: List[Dict[str, Any]]) -> str:
    """将提取出的用户原话格式化为持久留痕 Markdown 文档"""
    lines = [
        "# 会话用户原话全量留痕档案",
        "",
        f"- **Conversation ID**: `{conversation_id}`",
        f"- **累计指令轮数**: {len(words)}",
        "",
        "---",
        "",
    ]

    for w in words:
        time_str = f" ({w['created_at']})" if w.get("created_at") else ""
        kind_tag = ""
        if w.get("kind") == "ask_question_answer":
            kind_tag = " [选项框回答]"
        elif w.get("kind") == "subagent_message":
            kind_tag = " [父代理任务]"
        lines.append(f"## 第 {w['turn']} 轮指令 [Step {w['step_index']}]{kind_tag}{time_str}")
        lines.append("")
        lines.append(w["content"])
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def save_user_words_markdown(state_dir: Path, conversation_id: str, words: List[Dict[str, Any]]) -> Optional[Path]:
    """将用户原话列表原子保存至 state/<conversationId>_user_words.md"""
    if not words:
        return None
    out_file = state_dir / f"{conversation_id}_user_words.md"
    md_text = format_user_words_markdown(conversation_id, words)
    atomic_write_text(out_file, md_text)
    return out_file


# ==============================================================================
# Codex Rollout 会话记录解析与归档 (供 --client codex 在 compact/resume 时调用)
# ==============================================================================

CODEX_META_HEAD_BYTES = 65536
CODEX_IDE_TAG_RE = re.compile(r"<(ide_opened_file|ide_selection|ide_diagnostics)>")
CODEX_DROPPED_WRAPPER_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<codex_internal_context",
    "<external_codex_apps_open_page>",
    "<subagent_notification",
    "<system-reminder>",
    "<task-notifi",
    "<local-command-",
    "<command-",
    "Stop hook feedback",
    "Stop hook blocking error",
    "This session is being continued from a previous conversation",
    "[SYSTEM NOTIFICATION",
    "[Request interrupted by user",
)


def strip_win_extended_prefix(path_str: str) -> str:
    """剥离 Windows \\\\?\\ 扩展长度路径前缀，统一跨端路径比较与读取"""
    raw = str(path_str or "").strip()
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[8:]
    if raw.startswith("\\\\?\\"):
        return raw[4:]
    return raw


def _strip_leading_ide_tags(text: str) -> str:
    """仅剥离块首 IDE 自动附加上下文标签，保留用户正文中自行引用的同名标签"""
    rest = text.lstrip()
    while True:
        m = CODEX_IDE_TAG_RE.match(rest)
        if not m:
            return rest
        closing = f"</{m.group(1)}>"
        end = rest.find(closing, m.end())
        if end < 0:
            return ""
        rest = rest[end + len(closing) :].lstrip()


def read_codex_rollout_meta(rollout_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """仅读取 rollout 首行元数据，区分主会话与子代理会话"""
    try:
        with open(rollout_path, "rb") as f:
            first_line = f.readline(CODEX_META_HEAD_BYTES)
        if not first_line:
            return None, f"空会话文件: {rollout_path}"
        if not first_line.endswith(b"\n") and not first_line.rstrip().endswith(b"}"):
            return None, f"首行元数据未写完或超长: {rollout_path}"
        entry = json.loads(first_line.decode("utf-8", errors="replace"))
        payload = entry.get("payload") if isinstance(entry, dict) and entry.get("type") == "session_meta" else None
        if not isinstance(payload, dict):
            return None, f"首条记录非 session_meta: {rollout_path}"
        sid = payload.get("id")
        cwd = payload.get("cwd") or ""
        source = payload.get("source")
        if not isinstance(sid, str) or not sid.strip():
            return None, f"session_meta 缺少有效 id: {rollout_path}"
        if source == "subagent" or (isinstance(source, dict) and "subagent" in source):
            source_kind = "subagent"
        elif (isinstance(source, str) and source.strip()) or isinstance(source, dict):
            source_kind = "main"
        else:
            source_kind = "main"
        return {"id": sid.strip(), "cwd": str(cwd), "source_kind": source_kind}, None
    except Exception as exc:
        return None, f"读取 Codex 会话元数据失败 ({rollout_path}): {exc}"


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
    if meta["source_kind"] != "main":
        return [], meta["id"], ["子代理 rollout 会话不执行主会话原话提取"]
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
            if c_meta and c_meta["id"] == sid and c_meta["source_kind"] == "main":
                seen_keys.add(ckey)
                segments.append(cand)

    segments.sort(key=lambda p: (p.name, str(p)))
    return segments, sid, issues


def extract_codex_entry_words(
    entry: Dict[str, Any],
    fallback_step: int,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """从单条 Codex rollout response_item 记录中提取用户原话与去重标识"""
    if not isinstance(entry, dict) or entry.get("type") != "response_item":
        return None, None
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
        return None, None

    content = payload.get("content")
    meta_pass = payload.get("internal_chat_message_metadata_passthrough")
    raw_meta_items = meta_pass.get("content_item_metadata") if isinstance(meta_pass, dict) else None
    origins: List[Optional[str]] = []
    if isinstance(raw_meta_items, list):
        for item in raw_meta_items:
            prov = item.get("provenance") if isinstance(item, dict) else None
            origins.append(prov.get("type") if isinstance(prov, dict) else None)

    if origins and all(o in ("harness", "agents_md") for o in origins):
        return None, None

    kept_blocks: List[str] = []
    if isinstance(content, list):
        for idx, blk in enumerate(content):
            if not isinstance(blk, dict) or blk.get("type") not in ("input_text", "text"):
                continue
            raw_txt = blk.get("text")
            if not isinstance(raw_txt, str):
                continue
            origin = origins[idx] if len(origins) == len(content) else None
            if origin in ("harness", "agents_md"):
                continue
            if origin == "user":
                if raw_txt.strip():
                    kept_blocks.append(raw_txt.strip())
            else:
                stripped_ide = _strip_leading_ide_tags(raw_txt).strip()
                if stripped_ide and not stripped_ide.startswith(CODEX_DROPPED_WRAPPER_PREFIXES):
                    kept_blocks.append(stripped_ide)
    elif isinstance(content, str):
        if origins and all(o == "user" for o in origins):
            if content.strip():
                kept_blocks.append(content.strip())
        else:
            stripped_ide = _strip_leading_ide_tags(content).strip()
            if stripped_ide and not stripped_ide.startswith(CODEX_DROPPED_WRAPPER_PREFIXES):
                kept_blocks.append(stripped_ide)

    if not kept_blocks:
        return None, None

    merged_text = "\n\n".join(kept_blocks)
    kind = "ask_question_answer" if merged_text.lstrip().startswith("<send_user_message_question_reply>") else "user_input"

    rec_id: Optional[str] = None
    for cand_id in (payload.get("id"), entry.get("uuid")):
        if isinstance(cand_id, str) and cand_id.strip():
            rec_id = f"id:{cand_id.strip()}"
            break
    if rec_id is None and isinstance(entry.get("ordinal"), int) and isinstance(entry.get("timestamp"), str):
        canonical_json = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        rec_id = "record:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    step_idx = entry.get("ordinal") if isinstance(entry.get("ordinal"), int) else fallback_step
    created_at = str(entry.get("timestamp") or "")

    return rec_id, {
        "step_index": step_idx,
        "created_at": created_at,
        "kind": kind,
        "content": merged_text,
    }


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
        }

    words: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    seen_cross_segment_ids: Set[str] = set()

    for seg_path in segments:
        digest = hashlib.sha256()
        offset = 0
        bad_lines = 0
        seg_ids: Set[str] = set()
        line_counter = 0

        try:
            initial_size = seg_path.stat().st_size
            with open(seg_path, "rb") as f:
                for raw_line in f:
                    line_counter += 1
                    if not raw_line.endswith(b"\n") and not raw_line.rstrip().endswith(b"}"):
                        issues.append(f"末尾存在未写完记录: {seg_path.name}")
                        break
                    offset += len(raw_line)
                    digest.update(raw_line)
                    if not raw_line.strip():
                        continue
                    # 仅 response_item 的用户消息需要完整 JSON 解析，跳过海量工具输出行
                    if b"response_item" not in raw_line or b"message" not in raw_line:
                        continue
                    try:
                        entry = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except ValueError:
                        bad_lines += 1
                        continue
                    rec_id, item = extract_codex_entry_words(entry, fallback_step=line_counter)
                    if item is None:
                        continue
                    if rec_id is not None and rec_id in seen_cross_segment_ids:
                        continue
                    if rec_id is not None:
                        seg_ids.add(rec_id)
                    item["turn"] = len(words) + 1
                    words.append(item)
            seen_cross_segment_ids.update(seg_ids)
            if bad_lines:
                issues.append(f"存在损坏 JSON 行 ({bad_lines} 行): {seg_path.name}")
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

    return {
        "session_id": resolved_sid or session_id or "unknown",
        "words": words,
        "segments": [str(p) for p in segments],
        "sources": sources,
        "issues": issues,
        "complete": len(issues) == 0,
    }


def refresh_codex_session_archive(
    transcript_path_str: str,
    user_words_dir: Path,
    session_id: str,
) -> Dict[str, Any]:
    """
    刷新 Codex 会话的用户原话归档 (<user_words_dir>/<session_id>.md) 与校验元数据 (.json)。
    若本次读取遇到缺口而磁盘已有先前验证通过的完整文档，则保留旧文档并如实标注 preserved=True。
    """
    sid = (session_id or "unknown_session").strip()
    md_path = user_words_dir / f"{sid}.md"
    meta_path = user_words_dir / f"{sid}.json"

    prev_saved: Dict[str, Any] = {
        "file": str(md_path),
        "available": False,
        "preserved": False,
        "count": None,
        "last_time": None,
        "sources": [],
    }
    if md_path.is_file():
        try:
            prev_bytes = md_path.read_bytes()
            prev_bytes.decode("utf-8")
            prev_sha = hashlib.sha256(prev_bytes).hexdigest()
            prev_saved["available"] = True
            prev_saved["sha256"] = prev_sha
            if meta_path.is_file():
                meta_loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                # 兼容新版直接字段与旧版 publication 嵌套字段
                pub = meta_loaded.get("publication") if isinstance(meta_loaded, dict) else None
                cand_meta = pub if isinstance(pub, dict) else meta_loaded
                if isinstance(cand_meta, dict) and cand_meta.get("sha256") == prev_sha:
                    prev_saved["count"] = cand_meta.get("count")
                    prev_saved["last_time"] = cand_meta.get("last_time")
                    prev_saved["sources"] = cand_meta.get("sources") or []
        except Exception:
            pass

    parsed = extract_codex_session_words(transcript_path_str, session_id=sid)
    words = parsed["words"]
    issues = list(parsed["issues"])
    complete = bool(parsed["complete"])

    # 若提取完整（或尚无旧归档但已提取出有效原话），原子更新归档 Markdown 与元数据
    should_publish = complete or (bool(words) and not prev_saved["available"])
    if should_publish:
        try:
            md_text = format_user_words_markdown(sid, words)
            md_sha = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
            last_ts = words[-1]["created_at"] if words else None
            pub_record = {
                "format": "user-guard-codex-v1",
                "id": sid,
                "file": str(md_path),
                "count": len(words),
                "last_time": last_ts,
                "sources": parsed["sources"],
                "sha256": md_sha,
            }
            atomic_write_json(meta_path, pub_record)
            atomic_write_text(md_path, md_text)
            saved_info = {
                "file": str(md_path),
                "available": True,
                "preserved": False,
                "count": len(words),
                "last_time": last_ts,
                "sources": parsed["sources"],
                "sha256": md_sha,
            }
        except Exception as exc:
            complete = False
            issues.append(f"归档落盘失败: {exc}")
            saved_info = dict(prev_saved, preserved=bool(prev_saved["available"]))
    else:
        saved_info = dict(prev_saved, preserved=bool(prev_saved["available"]))

    return {
        "complete": complete,
        "words": words,
        "issues": issues,
        "saved": saved_info,
        "meta_path": str(meta_path),
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

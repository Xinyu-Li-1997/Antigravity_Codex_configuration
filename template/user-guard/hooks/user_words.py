"""
user_words.py - 反重力会话用户原话纯标准库解析与基础设施模块 (user-guard)

职责：
1. 提供全平台 UTF-8 标准流自愈、原子写盘与 PLUGIN_DATA 数据目录解析基础设施；
2. 解析 Antigravity 会话 transcript.jsonl，并在存在 truncated_fields 时以 O(N+M) 单次索引回退补全 transcript_full.jsonl；
3. 安全提取外层 <USER_REQUEST> 原话（防内部闭合标签提前截断）、交互式 ask_question 选项回答及可选的子代理父消息；
4. 按序格式化并原子写入会话原话归档文件 (<conversationId>_user_words.md)。
"""

import json
import os
import re
import sys
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


def atomic_write_text(target_path: Path, content: str) -> None:
    """使用同目录临时文件 + fsync + os.replace 原子更新文件，防止并发或中断产生半截损坏文件"""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, target_path)
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
        if isinstance(args, dict):
            qs = args.get("questions")
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


def extract_user_words(
    transcript_path_str: Optional[str],
    include_subagent_msgs: bool = True,
) -> List[Dict[str, Any]]:
    """
    从 transcript.jsonl 提取会话全部历史用户原话（含被截断长指令还原与 ask_question 选项回答）。

    返回结构列表:
    [
      {
        "turn": 1,
        "step_index": 0,
        "created_at": "2026-...",
        "kind": "user_input" | "ask_question_answer" | "subagent_message",
        "content": "纯净的用户输入文本"
      },
      ...
    ]
    """
    if not transcript_path_str:
        return []

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists() or not transcript_path.is_file():
        return []

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
        return []

    # 单次按需补全所有被截断的记录
    full_map = _load_truncated_contents_map(full_transcript_path, truncated_needed)

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

    return results


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

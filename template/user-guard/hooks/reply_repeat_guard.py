"""
reply_repeat_guard.py - 反重力 Stop 交接终审与防连续同质等待刷屏门禁 (user-guard)

正常模型停机时检查交接与重复等待。后台是否空闲和 Stop 钩子续跑状态独立判定。
1. 长会话 Stage-2 (drain/freeze) 交接终审（联动 handoff_gate.py）：
   - 核验是否已落盘非空交接文档或回复中已给出可粘贴【新对话提示词】；开启 /goal 时同时强制校验末尾是否附带 `<!-- GOAL_CANCELLED -->` 终止标记（每回合至多阻断 1 次）。
2. 日常防连续同质等待刷屏：
   - 优先定位紧凑版 transcript.jsonl，尾部高效读取末尾窗口（>256KB 时仅扫描末尾窗口，并在单回合超长时分块逆序溯源），按最近一条 USER_INPUT 或父代理消息定位当前回合；
   - 检测模型最新回复是否为短状态等待回复（<=300 字符、含后台运行等待词、不含向用户提问或等待用户拍板词）；
   - 严格执行相邻归一化文本同质比对（保留数字以区分真实迭代步数/残差进展！）：
     仅当同一回合连续 3 次输出同质无增量的短状态等待回复时，触发 1 次 decision="continue" 阻断提醒；
   - 同一用户回合至多触发一次续跑。
"""

# ① 常量、阈值与宿主依赖

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import handoff_gate  # noqa: E402
import user_words  # noqa: E402

user_words.ensure_utf8_stdio()

MAX_SHORT_REPLY_CHARS = 300
TAIL_SCAN_BYTES = 256 * 1024  # 256 KB
SIMILARITY_THRESHOLD = 0.80
CORE_SIMILARITY_THRESHOLD = 0.75

WAIT_KEYWORDS = (
    "仍在运行",
    "还在运行",
    "正在运行",
    "继续运行",
    "后台运行",
    "仍在计算",
    "正在计算",
    "继续等待",
    "等待完成",
    "等待结果",
    "等待后台",
    "耐心等待",
    "请稍候",
    "稍后汇报",
    "尚未完成",
    "还未完成",
    "still running",
    "waiting for",
)

WAIT_BOILERPLATE_TOKENS = sorted(
    set(WAIT_KEYWORDS)
    | {
        "请耐心",
        "in the background",
        "currently",
        "please",
        "wait",
        "still",
        "目前",
        "当前",
        "现在",
        "后台",
        "仍在",
        "还在",
        "正在",
        "继续",
        "依然",
        "仍然",
        "稍候",
        "等待",
        "运行中",
        "计算中",
        "请",
        "中",
        "的",
        "了",
        "呢",
        "吧",
    },
    key=len,
    reverse=True,
)

# 向用户提问或等待用户拍板裁决的标记（命中则豁免，绝不拦截正常的等待用户回复）
USER_DECISION_OR_QUESTION_MARKERS = (
    "?",
    "？",
    "请问",
    "是否需要",
    "是否继续",
    "请确认",
    "请告知",
    "由您决定",
    "由你决定",
    "请拍板",
    "等你",
    "等待你",
    "等待您",
    "用户裁决",
    "你的指示",
    "您的指示",
)

BLOCK_REASON_TEXT = (
    "【user-guard 防刷屏门禁阻断】当前已连续三次输出内容同质、无实质进展的短状态等待回复。"
    "请停止重复刷屏；后台任务未完成时请直接简明收尾并静默挂起等待系统 Reactive Wakeup 通知。"
)


# ② 目录、日志与防重复状态读写

def get_data_dirs() -> Tuple[Path, Path]:
    return user_words.resolve_data_dirs(HOOKS_DIR)


def get_current_iso_time() -> str:
    return user_words.current_iso_time()


def log_event(event_name: str, conversation_id: str, message: str) -> None:
    user_words.append_lifecycle_log(HOOKS_DIR, event_name, conversation_id, message)


def _load_repeat_state(state_file: Path, conversation_id: str, logs_dir: Optional[Path] = None) -> Dict[str, Any]:
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8-sig"))
            if (isinstance(data, dict) and data.get("version") == 2
                    and data.get("conversationId") == conversation_id and isinstance(data.get("turns"), dict)):
                for record in data["turns"].values():
                    if (not isinstance(record, dict)
                            or type(record.get("consecutiveShortWaitCount")) is not int
                            or record["consecutiveShortWaitCount"] < 0
                            or not isinstance(record.get("lastNormalizedReply"), str)
                            or not isinstance(record.get("blockedSignatures"), list)
                            or (record.get("lastStepIndex") is not None and type(record["lastStepIndex"]) is not int)):
                        raise ValueError("回合复读记录格式错误")
                return data
            raise ValueError("状态格式或会话身份不符")
        except (OSError, ValueError, RecursionError) as exc:
            if logs_dir is None:
                log_event("Stop:repeat_state_error", conversation_id, f"防重复状态读取失败（{exc}），保留原文件")
            else:
                user_words.append_jsonl_log(logs_dir / "lifecycle.log", {
                    "event": "Stop:repeat_state_error", "client": "claude", "session_id": conversation_id,
                    "message": f"防重复状态读取失败（{exc}），保留原文件",
                })
            raise
    return {"version": 2, "conversationId": conversation_id, "turns": {}}


def _save_repeat_state(state_file: Path, state: Dict[str, Any], conversation_id: str) -> None:
    state["version"] = 2
    state["conversationId"] = conversation_id
    state["updatedAt"] = get_current_iso_time()
    user_words.atomic_write_json(state_file, state)


# ③ 尾部窗口、逆序回合定位与截断回复回查

def _read_tail_steps_with_offset(transcript_path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """读取 transcript.jsonl 末尾窗口并解析步骤列表，同时返回窗口起始字节偏移"""
    try:
        start_offset = 0
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize > TAIL_SCAN_BYTES:
                seek_pos = fsize - TAIL_SCAN_BYTES
                f.seek(seek_pos - 1)
                at_line_start = f.read(1) == b"\n"
                raw = f.read(TAIL_SCAN_BYTES)
                if at_line_start:
                    start_offset = seek_pos
                else:
                    first_nl = raw.find(b"\n")
                    if first_nl != -1 and raw[first_nl + 1 :].strip():
                        start_offset = seek_pos + first_nl + 1
                        raw = raw[first_nl + 1 :]
                    else:
                        # 尾行可能独占整个窗口（包括仅有末尾换行的 JSONL）。
                        # 有界逆向寻找行首，避免丢弃超长的最终交接回复。
                        search_pos = seek_pos
                        lower_bound = max(0, fsize - user_words.MAX_HOOK_INPUT_CHARS)
                        while search_pos > lower_bound:
                            chunk_start = max(lower_bound, search_pos - TAIL_SCAN_BYTES)
                            f.seek(chunk_start)
                            prefix = f.read(search_pos - chunk_start)
                            last_nl = prefix.rfind(b"\n")
                            if last_nl != -1:
                                search_pos = chunk_start + last_nl + 1
                                break
                            search_pos = chunk_start
                        else:
                            if search_pos:
                                return [], fsize
                        start_offset = search_pos
                        f.seek(start_offset)
                        raw = f.read(fsize - start_offset)
            else:
                f.seek(0)
                raw = f.read(fsize)
    except OSError:
        return [], 0

    steps: List[Dict[str, Any]] = []
    # JSON 字符串允许含 U+2028 等字符，只能按真实换行字节分行。
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(user_words.decode_jsonl_line(line))
            if isinstance(obj, dict):
                steps.append(obj)
        except (ValueError, RecursionError):
            continue
    return steps, start_offset


def _read_tail_steps(transcript_path: Path) -> List[Dict[str, Any]]:
    """读取 transcript.jsonl 末尾窗口并解析步骤列表，避免大会话全量读盘"""
    steps, _ = _read_tail_steps_with_offset(transcript_path)
    return steps


def _is_turn_boundary_step(step: Dict[str, Any]) -> bool:
    """识别回合起点：人类显式 USER_INPUT 或跨代理派发的非 system 消息"""
    s_type = step.get("type", "")
    if s_type == "USER_INPUT" or step.get("source") == "USER_EXPLICIT":
        return True
    if s_type == "SYSTEM_MESSAGE":
        return bool(user_words.clean_user_content(step.get("content"), "SYSTEM_MESSAGE"))
    return False


def _find_turn_key_before_offset(transcript_path: Path, end_offset: int) -> str:
    """当单回合轨迹超过尾部 256KB 窗口时，向前分块逆序定位最近一次回合起点并返回 turn_<step>_<hash>"""
    if end_offset <= 0:
        return "turn_0_initial"
    try:
        pos = end_offset
        carry = b""
        with open(transcript_path, "rb") as f:
            while pos > 0:
                chunk_start = max(0, pos - TAIL_SCAN_BYTES)
                f.seek(chunk_start)
                chunk = f.read(pos - chunk_start)
                buf = chunk + carry
                lines = buf.split(b"\n")
                if chunk_start > 0:
                    carry = lines[0]
                    candidate_lines = lines[1:]
                else:
                    carry = b""
                    candidate_lines = lines
                for raw_line in reversed(candidate_lines):
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    if (
                        b'"USER_INPUT"' not in stripped
                        and b'"USER_EXPLICIT"' not in stripped
                        and b"[Message]" not in stripped
                    ):
                        continue
                    try:
                        st = json.loads(user_words.decode_jsonl_line(stripped))
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(st, dict) and _is_turn_boundary_step(st):
                        s_idx = st.get("step_index", 0)
                        c_hash = hashlib.sha256(str(st.get("content", "")).encode("utf-8")).hexdigest()[:12]
                        return f"turn_{s_idx}_{c_hash}"
                pos = chunk_start
    except OSError:
        pass
    return "turn_0_initial"


def extract_current_turn_latest_reply(transcript_path_str: Optional[str]) -> Tuple[str, Optional[int], str]:
    """
    提取当前回合键 turn_key、最新一条模型文本回复的 step_index 及其内容。
    """
    if not transcript_path_str:
        return "turn_0_initial", None, ""

    tp = user_words.resolve_compact_transcript_path(Path(transcript_path_str).expanduser())
    if not tp.exists() or not tp.is_file():
        return "turn_0_initial", None, ""

    steps, window_start_offset = _read_tail_steps_with_offset(tp)
    if not steps and window_start_offset <= 0:
        return "turn_0_initial", None, ""

    turn_key = "turn_0_initial"
    turn_start_pos = 0

    for i in range(len(steps) - 1, -1, -1):
        st = steps[i]
        if _is_turn_boundary_step(st):
            s_idx = st.get("step_index", i)
            c_hash = hashlib.sha256(str(st.get("content", "")).encode("utf-8")).hexdigest()[:12]
            turn_key = f"turn_{s_idx}_{c_hash}"
            turn_start_pos = i + 1
            break

    if turn_key == "turn_0_initial" and window_start_offset > 0:
        turn_key = _find_turn_key_before_offset(tp, window_start_offset)

    latest_step_idx: Optional[int] = None
    latest_text = ""
    latest_is_truncated = False

    for pos, st in enumerate(steps[turn_start_pos:], start=turn_start_pos):
        if st.get("type") == "PLANNER_RESPONSE" or st.get("source") == "MODEL":
            # 排除工具输出 GENERIC 步骤
            if st.get("type") == "GENERIC":
                continue
            content = st.get("content")
            if isinstance(content, str) and content.strip():
                raw_idx = st.get("step_index")
                latest_step_idx = int(raw_idx) if isinstance(raw_idx, int) else pos
                latest_text = content
                trunc_fields = st.get("truncated_fields") or []
                latest_is_truncated = isinstance(trunc_fields, list) and "content" in trunc_fields

    if latest_is_truncated and latest_step_idx is not None:
        full_map = user_words._load_truncated_contents_map(
            tp.parent / "transcript_full.jsonl",
            {latest_step_idx},
        )
        latest_text = full_map.get(latest_step_idx) or latest_text

    return turn_key, latest_step_idx, latest_text


# ④ 短等待识别、数值标记与相邻回复相似度

def is_short_status_reply(text: str) -> bool:
    """判定回复是否属于无提问的短状态等待回复"""
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_SHORT_REPLY_CHARS:
        return False

    low = stripped.lower()
    for qm in USER_DECISION_OR_QUESTION_MARKERS:
        if qm in low:
            return False

    return any(kw in low for kw in WAIT_KEYWORDS)


def normalize_reply_text(text: str) -> str:
    """归一化回复文本（去除标点与多余空白，但保留字母、汉字与数字以敏感识别迭代步数/残差变化）"""
    low = text.strip().lower()
    # 移除常见 ISO 时间戳如 2026-11-12T06:00:00 或 06:12:34，保留业务数值（步数、残差、百分比）
    low = re.sub(r"(?<!\d)\d{4}-\d{2}-\d{2}[t\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?(?![0-9a-z])", "", low)
    low = re.sub(r"(?<!\d)\d{2}:\d{2}:\d{2}(?!\d)", "", low)
    # 仅保留中英文字符、数字与科学计数法符号
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.+-]+", " ", low)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_number_tokens(norm_text: str) -> List[str]:
    """提取归一化文本中的数值标记（如迭代步数 100、残差 1e-5、进度 45）"""
    return re.findall(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", norm_text)


def _strip_wait_boilerplate(norm_text: str) -> str:
    """剥离通用等待套话词汇，提取短状态回复的核心任务主语"""
    s = norm_text
    for tok in WAIT_BOILERPLATE_TOKENS:
        s = s.replace(tok, " ")
    return re.sub(r"\s+", "", s).strip()


def are_replies_homogeneous(prev_norm: str, curr_norm: str) -> bool:
    """
    严格比对相邻两条短等待回复是否同质：
    1. 归一化文本完全一致 -> 同质；
    2. 若包含数值指标（如迭代步数、残差、完成度）且数值发生变化 -> 属于真实进展汇报，绝不判为复读；
    3. 数值序列相同（或均无数值）时：
       - 整句序列相似度 >= SIMILARITY_THRESHOLD，或
        - 剥离通用等待套话后的核心主语一致 / 高度相似 -> 判为同质等待复读。
    这里比较相邻短句的字符序列，不对长篇技术报告执行 N-gram 或段落去重。
    """
    if not prev_norm or not curr_norm:
        return False
    if prev_norm == curr_norm:
        return True

    prev_nums = _extract_number_tokens(prev_norm)
    curr_nums = _extract_number_tokens(curr_norm)
    if prev_nums != curr_nums:
        return False

    if difflib.SequenceMatcher(None, prev_norm, curr_norm).ratio() >= SIMILARITY_THRESHOLD:
        return True

    core_prev = _strip_wait_boilerplate(prev_norm)
    core_curr = _strip_wait_boilerplate(curr_norm)
    if core_prev == core_curr:
        return True
    if core_prev and core_curr:
        return difflib.SequenceMatcher(None, core_prev, core_curr).ratio() >= CORE_SIMILARITY_THRESHOLD
    return False


# ⑤ Stop 交接终审与每回合一次的防重复提醒

def _handle_stop_locked(payload: Dict[str, Any], state_dir: Path, conversation_id: str) -> Dict[str, Any]:
    turn_key, step_idx, latest_reply = extract_current_turn_latest_reply(payload.get("transcriptPath"))

    # 若当前会话处于长会话 Stage-2 (drain/freeze)，优先验收交接完整性与 /goal 停止标记（每回合至多阻断 1 次）
    last_input = user_words.clean_user_content(payload.get("lastUserInput"))
    goal_active = user_words.agy_goal_command(last_input)
    compaction_file = state_dir / f"{conversation_id}_compaction.json"
    try:
        compaction_state = json.loads(compaction_file.read_text(encoding="utf-8-sig"))
        if (isinstance(compaction_state, dict) and compaction_state.get("conversationId") == conversation_id
                and compaction_state.get("hasExplicitUserInput") is True):
            # 已按步骤对账的用户输入及后续取消标记优先于 Stop 中的旧首轮输入。
            goal_active = None
    except (OSError, ValueError, RecursionError):
        pass
    handoff_resp = handoff_gate.check_stop_handoff_locked(
        payload, state_dir, turn_key, latest_reply, goal_active=goal_active,
    )
    if handoff_resp is not None:
        return handoff_resp

    if not latest_reply:
        return {"decision": "allow"}

    state_file = state_dir / f"{conversation_id}_repeat_guard.json"
    state = _load_repeat_state(state_file, conversation_id)
    turns = state.setdefault("turns", {})
    turn_rec = turns.setdefault(
        turn_key,
        {
            "consecutiveShortWaitCount": 0,
            "lastNormalizedReply": "",
            "lastStepIndex": None,
            "blockedSignatures": [],
        },
    )

    # 同一 step_index 幂等保护（防止重复投递误累加）
    last_step = turn_rec.get("lastStepIndex")
    if step_idx is not None and isinstance(last_step, int) and step_idx <= last_step:
        return {"decision": "allow"}

    if not is_short_status_reply(latest_reply):
        turn_rec["consecutiveShortWaitCount"] = 0
        turn_rec["lastNormalizedReply"] = ""
        turn_rec["lastStepIndex"] = step_idx
        _save_repeat_state(state_file, state, conversation_id)
        return {"decision": "allow"}

    curr_norm = normalize_reply_text(latest_reply)
    prev_norm = str(turn_rec.get("lastNormalizedReply") or "")
    prev_count = int(turn_rec.get("consecutiveShortWaitCount", 0))

    if prev_count > 0 and are_replies_homogeneous(prev_norm, curr_norm):
        count = prev_count + 1
    else:
        # 首次出现短等待回复，或本条包含新的迭代步数/实质进展信息 -> 从 1 重新计数
        count = 1

    turn_rec["consecutiveShortWaitCount"] = count
    turn_rec["lastNormalizedReply"] = curr_norm
    turn_rec["lastStepIndex"] = step_idx

    blocked_sigs: List[str] = turn_rec.setdefault("blockedSignatures", [])
    sig = hashlib.sha256(curr_norm.encode("utf-8")).hexdigest()[:16]

    # fullyIdle=false 仍可能需要交接，也允许单次防重复提醒。它只表示存在后台任务。
    if count >= 3 and not blocked_sigs:
        blocked_sigs.append(sig)
        _save_repeat_state(state_file, state, conversation_id)
        log_event(
            "Stop:reply_repeat_guard",
            conversation_id,
            f"BLOCKED homogeneous short wait reply (count={count}) in {turn_key}",
        )
        return {
            "decision": "continue",
            "reason": BLOCK_REASON_TEXT,
        }

    _save_repeat_state(state_file, state, conversation_id)
    log_event(
        "Stop:reply_repeat_guard",
        conversation_id,
        f"ALLOWED short wait reply (count={count}/3) in {turn_key}",
    )
    return {"decision": "allow"}


def handle_stop_repeat_guard(payload: Dict[str, Any]) -> Dict[str, Any]:
    """处理 Stop 事件防复读刷屏检测"""
    conversation_id = str(payload.get("conversationId") or "unknown_session").strip()
    termination_reason = str(payload.get("terminationReason") or "").strip().casefold()

    # 1. 非正常无工具结束时直接放行
    if (termination_reason not in ("model_stop", "no_tool_call") or payload.get("error")
            or not user_words.valid_session_id(conversation_id)):
        return {"decision": "allow"}

    # 2. 官方防死循环协议规范：若当前回合已处于 Stop 钩子触发的续跑状态（兼容 App camelCase 与 CLI v1.5.6+ snake_case），立即放行
    if payload.get("continuationActive") is True or payload.get("continuation_active") is True:
        return {"decision": "allow"}

    state_dir, _ = get_data_dirs()
    try:
        with user_words.session_state_lock(state_dir, conversation_id):
            return _handle_stop_locked(payload, state_dir, conversation_id)
    except (OSError, ValueError) as exc:
        log_event("Stop:state_error", conversation_id, f"Stop 状态未更新（{exc}）")
        return {"decision": "allow"}


def _claude_reverse_entries(path: Path):
    """从末尾分块读取，保留行首字节偏移，回合超出尾部窗口时继续向前找。"""
    with path.open("rb") as stream:
        position = stream.seek(0, 2)
        carry = b""
        while position:
            start = max(0, position - TAIL_SCAN_BYTES)
            stream.seek(start)
            data = stream.read(position - start) + carry
            lines = data.split(b"\n")
            carry = lines.pop(0) if start else b""
            offset = start + len(carry) + 1 if start else 0
            located = []
            for line in lines:
                located.append((offset, line))
                offset += len(line) + 1
            for row_offset, raw in reversed(located):
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(user_words.decode_jsonl_line(raw))
                except (ValueError, RecursionError):
                    continue
                if isinstance(entry, dict):
                    yield row_offset, entry
            position = start


def _claude_turn_reply(payload: Dict[str, Any]) -> Tuple[str, Optional[int], str, str]:
    supplied = payload.get("last_assistant_message")
    latest_reply = supplied.strip() if isinstance(supplied, str) else ""
    _, transcript, _, skip = user_words.claude_hook_session(payload)
    if skip or transcript is None:
        return "turn_0_initial", None, latest_reply, ""
    step: Optional[int] = None
    message_id = ""
    turn_key = "turn_0_initial"
    pending_question: Optional[Tuple[int, Dict[str, Any], Set[str]]] = None
    try:
        for offset, entry in _claude_reverse_entries(transcript):
            if entry.get("isSidechain") is True or entry.get("isMeta") is True:
                continue
            if pending_question is not None and entry.get("type") == "assistant":
                cand_offset, cand_entry, cand_ids = pending_question
                q_ids = user_words._claude_question_ids_from_assistant(entry)
                if q_ids & cand_ids:
                    q_text, _ = user_words.claude_user_content(cand_entry, question_ids=q_ids)
                    if q_text:
                        identity = str(cand_entry.get("uuid") or cand_entry.get("id") or cand_offset)
                        turn_key = "turn_" + hashlib.sha256((identity + ":" + q_text).encode("utf-8")).hexdigest()[:24]
                        break
                msg = entry.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                tool_ids = {
                    str(block["id"]) for block in (content if isinstance(content, list) else [])
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
                }
                if cand_ids & tool_ids:
                    pending_question = None
            text, kind = user_words.claude_user_content(entry)
            if text and kind in ("user_input", "queued_command"):
                identity = str(entry.get("uuid") or entry.get("id") or offset)
                turn_key = "turn_" + hashlib.sha256((identity + ":" + text).encode("utf-8")).hexdigest()[:24]
                break
            if text and kind == "ask_question_answer":
                r_ids = user_words._claude_tool_result_ids(entry)
                if r_ids and pending_question is None:
                    pending_question = (offset, entry, r_ids)
                continue
            if entry.get("type") != "assistant" or step is not None:
                continue
            msg = entry.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                text = "\n".join(block["text"] for block in content if isinstance(block, dict)
                                 and block.get("type") == "text" and isinstance(block.get("text"), str))
            else:
                text = content if isinstance(content, str) else ""
            if text.strip():
                step = offset
                message_id = str(entry.get("uuid") or entry.get("id") or "")
                if not latest_reply:
                    latest_reply = text.strip()
        if step is None and latest_reply:
            # 宿主可能在助手记录刷盘前调用 Stop；同一快照的重复投递仍须幂等。
            step = transcript.stat().st_size
    except OSError:
        return turn_key, None, latest_reply, ""
    return turn_key, step, latest_reply, message_id


def handle_claude_stop_repeat_guard(payload: Dict[str, Any], state_dir: Optional[Path] = None,
                                   logs_dir: Optional[Path] = None) -> Dict[str, Any]:
    if payload.get("stop_hook_active") is True:
        return {}
    sid, _, _, skip = user_words.claude_hook_session(payload)
    if skip or not user_words.valid_session_id(sid):
        return {}
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    state_dir = Path(state_dir).expanduser() if state_dir is not None else home / "hooks" / "state"
    logs_dir = Path(logs_dir).expanduser() if logs_dir is not None else home / "hooks" / "logs"
    turn_key, step, reply, message_id = _claude_turn_reply(payload)
    if not reply:
        return {}
    try:
        with user_words.session_state_lock(state_dir, sid):
            state_file = state_dir / f"{sid}_repeat_guard.json"
            state = _load_repeat_state(state_file, sid, logs_dir)
            record = state["turns"].setdefault(turn_key, {
                "consecutiveShortWaitCount": 0, "lastNormalizedReply": "", "lastStepIndex": None,
                "blockedSignatures": [],
            })
            if ((step is not None and step == record["lastStepIndex"])
                    or (message_id and message_id == record.get("lastMessageId"))):
                return {}
            record.update(lastStepIndex=step, lastMessageId=message_id)
            if is_short_status_reply(reply):
                normalized = normalize_reply_text(reply)
                previous = record["lastNormalizedReply"]
                record["consecutiveShortWaitCount"] = (record["consecutiveShortWaitCount"] + 1
                    if are_replies_homogeneous(previous, normalized) else 1)
                record["lastNormalizedReply"] = normalized
            else:
                record.update(consecutiveShortWaitCount=0, lastNormalizedReply="")
            block = record["consecutiveShortWaitCount"] >= 3 and not record["blockedSignatures"]
            if block:
                record["blockedSignatures"].append(hashlib.sha256(record["lastNormalizedReply"].encode("utf-8")).hexdigest()[:16])
            _save_repeat_state(state_file, state, sid)
            if block:
                user_words.append_jsonl_log(logs_dir / "lifecycle.log", {
                    "event": "Stop:reply_repeat_guard", "client": "claude", "session_id": sid,
                    "turn_key": turn_key, "count": record["consecutiveShortWaitCount"],
                })
                return {"decision": "block", "reason": BLOCK_REASON_TEXT}
    except (OSError, ValueError) as exc:
        user_words.append_jsonl_log(logs_dir / "lifecycle.log", {
            "event": "Stop:state_error", "client": "claude", "session_id": sid, "message": str(exc),
        })
    return {}


# ⑥ 命令行主入口

def main() -> None:
    parser = argparse.ArgumentParser(description="Stop 短状态等待回复门禁")
    parser.add_argument("--client", choices=("antigravity", "claude"), default="antigravity")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--log-dir", default="")
    args, _ = parser.parse_known_args()
    payload: Dict[str, Any] = {}
    try:
        payload = user_words.read_hook_payload()
    except (OSError, ValueError) as e:
        if args.client == "claude":
            sys.stderr.write(f"Claude Stop JSON parse error: {e}\n")
        else:
            log_event("Stop:reply_repeat_guard", "unknown", f"Failed to parse stdin JSON: {e}")

    try:
        response = (handle_claude_stop_repeat_guard(payload, Path(args.state_dir) if args.state_dir else None,
                                                  Path(args.log_dir) if args.log_dir else None)
                    if args.client == "claude" else handle_stop_repeat_guard(payload))
    except Exception as e:
        stack_str = traceback.format_exc()
        if args.client == "claude":
            sys.stderr.write(f"Claude Stop: {e}\n")
            response = {}
        else:
            log_event("Stop:reply_repeat_guard", str(payload.get("conversationId", "unknown")), f"Exception: {e}\n{stack_str}")
            response = {"decision": "allow"}

    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

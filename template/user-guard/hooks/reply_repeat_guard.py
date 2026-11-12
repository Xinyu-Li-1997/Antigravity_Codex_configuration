"""
reply_repeat_guard.py - 反重力 Stop 防连续同质等待刷屏门禁 (user-guard)

核心职责（适配 Antigravity Stop 钩子契约）：
1. 仅在正常结束回合（terminationReason == "NO_TOOL_CALL"）且非强制续跑重入（continuationActive == False）时检查；
2. 尾部高效读取 transcript.jsonl（>256KB 时仅扫描末尾窗口），按最近一条 USER_INPUT 或父代理消息定位当前回合；
3. 检测模型最新回复是否为短状态等待回复（<=300 字符、含后台运行等待词、不含向用户提问或等待用户拍板词）；
4. 严格执行相邻归一化文本同质比对（保留数字以区分真实迭代步数/残差进展！）：
   - 仅当同一回合连续 3 次输出同质无增量的短状态等待回复时，触发 1 次 decision="continue" 阻断提醒；
   - 配合 continuationActive 检查与已拦截签名去重，100% 杜绝循环唤醒死锁。
"""

import difflib
import hashlib
import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import user_words  # noqa: E402

user_words.ensure_utf8_stdio()

MAX_SHORT_REPLY_CHARS = 300
TAIL_SCAN_BYTES = 256 * 1024  # 256 KB
SIMILARITY_THRESHOLD = 0.80
CORE_SIMILARITY_THRESHOLD = 0.75
MAX_LOG_BYTES = 2 * 1024 * 1024
TRIM_LOG_BYTES = 1 * 1024 * 1024

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


def get_data_dirs() -> Tuple[Path, Path]:
    return user_words.resolve_data_dirs(HOOKS_DIR)


def get_current_iso_time() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def log_event(event_name: str, conversation_id: str, message: str) -> None:
    try:
        _, logs_dir = get_data_dirs()
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / "lifecycle.log"
        if log_file.exists() and log_file.stat().st_size > MAX_LOG_BYTES:
            raw = log_file.read_bytes()
            tail = raw[-TRIM_LOG_BYTES:]
            nl = tail.find(b"\n")
            if nl != -1:
                tail = tail[nl + 1 :]
            user_words.atomic_write_text(log_file, tail.decode("utf-8", errors="replace"))
        entry = {
            "timestamp": get_current_iso_time(),
            "event": event_name,
            "conversationId": conversation_id,
            "message": message,
            "summary": {},
        }
        with open(log_file, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
    low = re.sub(r"\b\d{4}-\d{2}-\d{2}[t\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?\b", "", low)
    low = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "", low)
    # 仅保留中英文字符、数字与科学计数法符号
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.+-]+", " ", low)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_number_tokens(norm_text: str) -> List[str]:
    """提取归一化文本中的数值标记（如迭代步数 100、残差 1e-5、进度 45）"""
    return re.findall(r"\d+(?:\.\d+)?(?:e[+-]?\d+)?", norm_text)


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
    3. 数值集合相同（或均无数值）时：
       - 整句序列相似度 >= SIMILARITY_THRESHOLD，或
       - 剥离通用等待套话后的核心主语一致 / 高度相似 -> 判为同质等待复读。
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


def _read_tail_steps(transcript_path: Path) -> List[Dict[str, Any]]:
    """读取 transcript.jsonl 末尾窗口并解析步骤列表，避免大会话全量读盘"""
    try:
        fsize = transcript_path.stat().st_size
        with open(transcript_path, "rb") as f:
            if fsize > TAIL_SCAN_BYTES:
                f.seek(fsize - TAIL_SCAN_BYTES)
                raw = f.read()
                first_nl = raw.find(b"\n")
                if first_nl != -1:
                    raw = raw[first_nl + 1 :]
            else:
                raw = f.read()
    except OSError:
        return []

    steps: List[Dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                steps.append(obj)
        except json.JSONDecodeError:
            continue
    return steps


def _is_turn_boundary_step(step: Dict[str, Any]) -> bool:
    """识别回合起点：人类显式 USER_INPUT 或跨代理派发的非 system 消息"""
    s_type = step.get("type", "")
    if s_type == "USER_INPUT" or step.get("source") == "USER_EXPLICIT":
        return True
    if s_type == "SYSTEM_MESSAGE":
        content = str(step.get("content") or "")
        if "[Message]" in content and "sender=system" not in content:
            return True
    return False


def extract_current_turn_latest_reply(transcript_path_str: Optional[str]) -> Tuple[str, Optional[int], str]:
    """
    提取当前回合键 turn_key、最新一条模型文本回复的 step_index 及其内容。
    """
    if not transcript_path_str:
        return "turn_0_initial", None, ""

    tp = Path(transcript_path_str)
    if not tp.exists() or not tp.is_file():
        return "turn_0_initial", None, ""

    steps = _read_tail_steps(tp)
    if not steps:
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

    latest_step_idx: Optional[int] = None
    latest_text = ""

    for pos, st in enumerate(steps[turn_start_pos:], start=turn_start_pos):
        if st.get("type") == "PLANNER_RESPONSE" or st.get("source") == "MODEL":
            # 排除工具输出 GENERIC 步骤
            if st.get("type") == "GENERIC":
                continue
            content = st.get("content")
            if isinstance(content, str) and content.strip():
                raw_idx = st.get("step_index")
                latest_step_idx = int(raw_idx) if isinstance(raw_idx, int) else pos
                latest_text = content.strip()

    return turn_key, latest_step_idx, latest_text


def _load_repeat_state(state_file: Path, conversation_id: str) -> Dict[str, Any]:
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"version": 2, "conversationId": conversation_id, "turns": {}}


def _save_repeat_state(state_file: Path, state: Dict[str, Any], conversation_id: str) -> None:
    state["version"] = 2
    state["conversationId"] = conversation_id
    state["updatedAt"] = get_current_iso_time()
    user_words.atomic_write_json(state_file, state)


def handle_stop_repeat_guard(payload: Dict[str, Any]) -> Dict[str, Any]:
    """处理 Stop 事件防复读刷屏检测"""
    conversation_id = str(payload.get("conversationId") or "unknown_session").strip()
    termination_reason = str(payload.get("terminationReason") or "")

    # 1. 非正常无工具结束时直接放行
    if termination_reason != "NO_TOOL_CALL":
        return {"decision": "allow"}

    # 2. 官方防死循环契约：若当前回合已处于 Stop 钩子触发的续跑状态，立即放行，绝不循环唤醒
    if bool(payload.get("continuationActive", False)):
        return {"decision": "allow"}

    turn_key, step_idx, latest_reply = extract_current_turn_latest_reply(payload.get("transcriptPath"))
    if not latest_reply:
        return {"decision": "allow"}

    state_dir, _ = get_data_dirs()
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
    if step_idx is not None and turn_rec.get("lastStepIndex") == step_idx:
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

    if count >= 3 and sig not in blocked_sigs:
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


def main() -> None:
    input_text = sys.stdin.read().strip()
    payload: Dict[str, Any] = {}
    if input_text:
        try:
            payload = json.loads(input_text)
        except Exception as e:
            log_event("Stop:reply_repeat_guard", "unknown", f"Failed to parse stdin JSON: {e}")

    try:
        response = handle_stop_repeat_guard(payload)
    except Exception as e:
        stack_str = traceback.format_exc()
        log_event("Stop:reply_repeat_guard", str(payload.get("conversationId", "unknown")), f"Exception: {e}\n{stack_str}")
        response = {"decision": "allow"}

    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

"""
session_context_loader.py - 反重力上下文压缩原话回溯引擎 (user-guard)

挂载事件：
- SessionStart: 会话初次启动或进程冷重载时同步检查点游标（若存在未注入的压缩检查点则补注入 1 次）
- PreInvocation: 会话运行中每轮调用模型前，通过 O(Δbytes) 二进制增量游标检测新增 CHECKPOINT：
  * 日常未压缩对话：零全盘读取、零上下文注入；
  * 发生自动压缩或手动 /compact 后首轮：精确触发 1 次全量原话提取、落盘 _user_words.md 并注入索引与摘要；
  * 压缩后第 2、3...N 轮日常对话：因检查点步号已登记，零重复注入；
  * 再次发生新的压缩（产生更大 step_index 的 CHECKPOINT）：再次精确触发 1 次。
"""

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import user_words  # noqa: E402

user_words.ensure_utf8_stdio()

MAX_INLINE_TOTAL_CHARS = 8000
MAX_INLINE_ITEM_CHARS = 1200
MAX_LOG_BYTES = 2 * 1024 * 1024
TRIM_LOG_BYTES = 1 * 1024 * 1024


def get_data_dirs() -> Tuple[Path, Path]:
    return user_words.resolve_data_dirs(HOOKS_DIR)


def get_current_iso_time() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def log_event(
    event_name: str,
    conversation_id: str,
    message: str,
    payload_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """追加结构化日志到 logs/lifecycle.log，附带 2MB 自动滚动裁剪防无限膨胀"""
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

        log_entry = {
            "timestamp": get_current_iso_time(),
            "event": event_name,
            "conversationId": conversation_id,
            "message": message,
            "summary": payload_summary or {},
        }
        with open(log_file, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _default_compaction_state(conversation_id: str) -> Dict[str, Any]:
    return {
        "version": 1,
        "conversationId": conversation_id,
        "scannedByteOffset": 0,
        "latestCheckpointStep": -1,
        "lastInjectedCheckpointStep": -1,
        "explicitResumeInjected": False,
        "hasExplicitUserInput": False,
        "updatedAt": get_current_iso_time(),
    }


def _load_compaction_state(state_file: Path, conversation_id: str) -> Dict[str, Any]:
    if not state_file.exists():
        return _default_compaction_state(conversation_id)
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 1:
            return data
    except Exception:
        pass
    return _default_compaction_state(conversation_id)


def _scan_transcript_incrementally(
    transcript_path: Path,
    state: Dict[str, Any],
) -> bool:
    """
    从上次 scannedByteOffset 起以二进制模式仅扫描新增尾部字节，
    极速捕获新增的 CHECKPOINT 步骤与 USER_INPUT 标记。
    返回 state 是否发生变更。
    """
    try:
        file_size = transcript_path.stat().st_size
    except OSError:
        return False

    offset = int(state.get("scannedByteOffset", 0))
    changed = False

    # 若日志文件被截断或重建，从头重置扫描游标（保留已注入记忆仅在文件缩小时归零）
    if file_size < offset:
        offset = 0
        state["scannedByteOffset"] = 0
        state["latestCheckpointStep"] = -1
        state["lastInjectedCheckpointStep"] = -1
        state["hasExplicitUserInput"] = False
        changed = True

    if file_size == offset:
        return changed

    try:
        with open(transcript_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return changed

    if not chunk:
        return changed

    last_nl = chunk.rfind(b"\n")
    if last_nl != -1:
        processable = chunk[: last_nl + 1]
    elif chunk.rstrip().endswith(b"}"):
        # 兼容未以换行符结尾的完整单行测试样本
        processable = chunk
    else:
        return changed

    new_offset = offset + len(processable)
    text_block = processable.decode("utf-8", errors="replace")

    latest_cp = int(state.get("latestCheckpointStep", -1))
    has_user = bool(state.get("hasExplicitUserInput", False))

    for raw_line in text_block.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 子串快速预筛，避免对无关大体积工具输出行执行 json.loads
        might_be_cp = '"CHECKPOINT"' in line
        might_be_user = (not has_user) and ('"USER_INPUT"' in line or '"USER_EXPLICIT"' in line)
        if not (might_be_cp or might_be_user):
            continue

        try:
            step = json.loads(line)
        except json.JSONDecodeError:
            continue

        s_type = step.get("type", "")
        s_source = step.get("source", "")

        if s_type == "CHECKPOINT":
            cp_idx = step.get("step_index")
            cp_num = int(cp_idx) if isinstance(cp_idx, int) else max(latest_cp + 1, 0)
            if cp_num > latest_cp:
                latest_cp = cp_num

        if s_type == "USER_INPUT" or s_source == "USER_EXPLICIT":
            has_user = True

    state["scannedByteOffset"] = new_offset
    state["latestCheckpointStep"] = latest_cp
    state["hasExplicitUserInput"] = has_user
    state["updatedAt"] = get_current_iso_time()
    return True


def _truncate_single_item(content: str, turn: int, max_chars: int = MAX_INLINE_ITEM_CHARS) -> str:
    if len(content) <= max_chars:
        return content
    head_len = max(max_chars - 220, 200)
    tail_len = 120
    return (
        f"{content[:head_len].rstrip()}\n"
        f"...[第 {turn} 轮单条超长已省略中间部分，完整一字不差原文见归档文件]...\n"
        f"{content[-tail_len:].lstrip()}"
    )


def build_compaction_system_message(
    words: List[Dict[str, Any]],
    saved_md_path: Optional[Path],
    trigger_reason: str,
) -> str:
    """构造带预算上限保护与归档文件指针的原话回溯系统消息"""
    archive_str = str(saved_md_path) if saved_md_path else "未落盘"
    total_turns = len(words)

    header_lines = [
        f"【系统原话回溯】检测到会话经历上下文压缩或恢复交接（触发源：{trigger_reason}）。",
        f"- 本会话用户原话完整归档文件：`{archive_str}`（共 {total_turns} 条记录，范围：第 1～{total_turns} 轮）",
        "- 请严格遵照全局规则 R9 对照以下用户原始诉求与最新裁定，严禁遗忘或发生三类无声放大（若条目被截断省略，请调用 `read_file` 查阅上述归档文件）：",
        "",
    ]
    header_text = "\n".join(header_lines)

    formatted_blocks: List[str] = []
    for w in words:
        time_tag = f", {w['created_at']}" if w.get("created_at") else ""
        kind_label = " [选项框回答]" if w.get("kind") == "ask_question_answer" else ""
        body = _truncate_single_item(w["content"], w["turn"], MAX_INLINE_ITEM_CHARS)
        block = f"【第 {w['turn']} 轮指令】{kind_label} (Step {w['step_index']}{time_tag})\n{body}"
        formatted_blocks.append(block)

    joined_all = "\n\n".join(formatted_blocks)
    if len(header_text) + len(joined_all) <= MAX_INLINE_TOTAL_CHARS or len(formatted_blocks) <= 2:
        return header_text + joined_all

    # 超出总预算时：必保第 1 轮立项总纲 + 逆序尽可能多地保留最近定论轮次
    first_block = formatted_blocks[0]
    budget_left = MAX_INLINE_TOTAL_CHARS - len(header_text) - len(first_block) - 200
    tail_blocks: List[str] = []
    used = 0
    for blk in reversed(formatted_blocks[1:]):
        if used + len(blk) + 2 > budget_left and tail_blocks:
            break
        tail_blocks.append(blk)
        used += len(blk) + 2
    tail_blocks.reverse()

    omitted_count = len(formatted_blocks) - 1 - len(tail_blocks)
    if omitted_count > 0:
        end_omitted_turn = 1 + omitted_count
        omit_notice = (
            f"...（中间第 2～{end_omitted_turn} 轮共 {omitted_count} 条历史指令因上下文预算省略内联；"
            f"完整原文已保存于 `{archive_str}`，请按规则 R9 使用 `read_file` 核对）..."
        )
        return header_text + "\n\n".join([first_block, omit_notice] + tail_blocks)

    return header_text + joined_all


def handle_context_check(payload: Dict[str, Any], event_name: str = "PreInvocation") -> Dict[str, Any]:
    """
    统一处理 SessionStart 与 PreInvocation 事件：
    1. 日常无新增 CHECKPOINT：增量游标毫秒级返回 {"injectSteps": []}，零全盘扫描、零重复注入；
    2. 捕获新增 CHECKPOINT（或显式 compact/resume 信号）：提取全量用户原话落盘并精确注入 1 次。
    """
    conversation_id = str(payload.get("conversationId") or "unknown_session").strip()
    transcript_path_str = payload.get("transcriptPath")
    explicit_source = str(payload.get("source") or payload.get("eventSource") or "").strip().lower()

    if explicit_source in ("startup", "fresh", "new"):
        log_event(event_name, conversation_id, f"Explicit {explicit_source} start. Skipping injection.")
        return {"injectSteps": []}

    if not transcript_path_str:
        return {"injectSteps": []}

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists() or not transcript_path.is_file():
        return {"injectSteps": []}

    state_dir, _ = get_data_dirs()
    state_file = state_dir / f"{conversation_id}_compaction.json"
    state = _load_compaction_state(state_file, conversation_id)

    state_changed = _scan_transcript_incrementally(transcript_path, state)

    latest_cp = int(state.get("latestCheckpointStep", -1))
    last_injected_cp = int(state.get("lastInjectedCheckpointStep", -1))
    has_user_input = bool(state.get("hasExplicitUserInput", False))

    # 子代理会话（无显式人类 USER_INPUT）不执行主会话原话注入
    if not has_user_input:
        if state_changed:
            user_words.atomic_write_json(state_file, state)
        return {"injectSteps": []}

    trigger_reason: Optional[str] = None
    if latest_cp > last_injected_cp:
        trigger_reason = f"CHECKPOINT step #{latest_cp}"
    elif explicit_source in ("compact", "compaction"):
        trigger_reason = f"explicit {explicit_source}"
    elif explicit_source == "resume" and not state.get("explicitResumeInjected", False):
        trigger_reason = "explicit resume"

    if not trigger_reason:
        if state_changed:
            user_words.atomic_write_json(state_file, state)
        return {"injectSteps": []}

    # 确证发生新的上下文压缩或首次显式恢复：提取纯净用户原话（主会话不含子代理系统消息）
    words = user_words.extract_user_words(transcript_path_str, include_subagent_msgs=False)
    if not words:
        if latest_cp > last_injected_cp:
            state["lastInjectedCheckpointStep"] = latest_cp
        user_words.atomic_write_json(state_file, state)
        return {"injectSteps": []}

    saved_md_path = user_words.save_user_words_markdown(state_dir, conversation_id, words)
    system_msg = build_compaction_system_message(words, saved_md_path, trigger_reason)

    if latest_cp > last_injected_cp:
        state["lastInjectedCheckpointStep"] = latest_cp
    if explicit_source == "resume":
        state["explicitResumeInjected"] = True
    state["lastInjectedAt"] = get_current_iso_time()
    state["lastInjectedWordCount"] = len(words)
    user_words.atomic_write_json(state_file, state)

    log_event(
        event_name,
        conversation_id,
        f"Injected {len(words)} user words on {trigger_reason} (archived to {saved_md_path.name if saved_md_path else 'none'}).",
        {"wordCount": len(words), "latestCheckpointStep": latest_cp},
    )

    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": system_msg,
                }
            }
        ]
    }


# 保持向后兼容函数别名
def handle_session_start(payload: Dict[str, Any]) -> Dict[str, Any]:
    return handle_context_check(payload, event_name="SessionStart")


def handle_pre_invocation(payload: Dict[str, Any]) -> Dict[str, Any]:
    return handle_context_check(payload, event_name="PreInvocation")


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Compaction Context Loader Hook")
    parser.add_argument("--event", type=str, default="", help="Hook event name: SessionStart or PreInvocation")
    args, _ = parser.parse_known_args()

    input_text = sys.stdin.read().strip()
    payload: Dict[str, Any] = {}
    if input_text:
        try:
            payload = json.loads(input_text)
        except Exception as e:
            log_event(args.event or "Unknown", "unknown", f"Failed to parse stdin JSON: {e}")

    event_name = args.event or str(payload.get("hookEventName") or payload.get("eventName") or "PreInvocation")

    try:
        if event_name in ("SessionStart", "PreInvocation"):
            response = handle_context_check(payload, event_name=event_name)
        else:
            response = {}
    except Exception as e:
        stack_str = traceback.format_exc()
        log_event(event_name, str(payload.get("conversationId", "unknown")), f"Unhandled exception: {e}\n{stack_str}")
        response = {"injectSteps": []} if event_name in ("SessionStart", "PreInvocation") else {}

    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

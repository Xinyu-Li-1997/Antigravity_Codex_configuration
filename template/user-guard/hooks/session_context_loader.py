"""
session_context_loader.py - 反重力与 Codex 双端上下文压缩原话回溯引擎 (user-guard)

挂载事件：
- Antigravity 侧 (--client antigravity，默认)：
  * SessionStart: 会话初次启动或进程冷重载时同步检查点游标（若存在未注入的压缩检查点则补注入 1 次）
  * PreInvocation: 会话运行中每轮调用模型前，通过 O(Δbytes) 二进制增量游标检测新增 CHECKPOINT：
    日常未压缩对话零全盘读取、零上下文注入；发生压缩后首轮精确触发 1 次全量原话提取落盘并注入索引与摘要；
    同时增量维护压缩频次/间隔与多模态读图遥测，联动 handoff_gate.py 注入长会话预警 (advisory) 与强收口停机提醒 (drain/freeze)。
- Codex 侧 (--client codex)：
  * SessionStart (matcher: ^(compact|resume)$): 在 Codex 发生上下文压缩或恢复会话时，合并同 ID 主会话全部 rollout 分段，
    原子刷新 hooks/state/user-words/<session_id>.md 并通过 hookSpecificOutput.additionalContext 注入原话档案路径与核心指令摘要。
"""

import argparse
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
import user_words  # noqa: E402

user_words.ensure_utf8_stdio()

MAX_INLINE_TOTAL_CHARS = 8000
MAX_INLINE_ITEM_CHARS = 1200
MAX_LOG_BYTES = user_words.MAX_LOG_BYTES
TRIM_LOG_BYTES = user_words.TRIM_LOG_BYTES
MAX_TRANSCRIPT_TRACKED_IMAGES = 2000

STEP_INDEX_RE = re.compile(r'"step_index"\s*:\s*(\d+)')
GOAL_CMD_RE = re.compile(r"(?:^|\s)/goal(?:\s|$)", re.IGNORECASE)
GOAL_CANCEL_CMD_RE = re.compile(r"(?:取消|停止|结束|关闭)\s*/?goal\b", re.IGNORECASE)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
UI_BANNER_PHRASE = "this conversation grew too large, so the history before this point may be missing or incomplete"


def _decode_proto_str(raw: Any) -> str:
    """还原 protojson 参数里带外层引号与转义反斜杠的字符串值。"""
    s = str(raw or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            decoded = json.loads(s)
            if isinstance(decoded, str):
                s = decoded
        except Exception:
            s = s[1:-1]
    return s.strip().strip("\"'")


def get_data_dirs() -> Tuple[Path, Path]:
    return user_words.resolve_data_dirs(HOOKS_DIR)


def get_current_iso_time() -> str:
    return user_words.current_iso_time()


def log_event(
    event_name: str,
    conversation_id: str,
    message: str,
    payload_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """追加结构化日志到 logs/lifecycle.log，附带 2MB 自动滚动裁剪防无限膨胀"""
    user_words.append_lifecycle_log(HOOKS_DIR, event_name, conversation_id, message, payload_summary)


def _default_compaction_state(conversation_id: str) -> Dict[str, Any]:
    return {
        "version": 1,
        "telemetryVersion": 3,
        "conversationId": conversation_id,
        "scannedByteOffset": 0,
        "scannedTranscriptName": "",
        "latestCheckpointStep": -1,
        "lastInjectedCheckpointStep": -1,
        "explicitResumeInjected": False,
        "hasExplicitUserInput": False,
        "checkpointSteps": [],
        "mediaStepCount": 0,
        "transcriptImageSizes": {},
        "transcriptImageCallCount": 0,
        "latestStepIndex": -1,
        "latestUserStepIndex": -1,
        "latestUserInputHash": "",
        "latestUserInputPreview": "",
        "isGoalActive": False,
        "updatedAt": get_current_iso_time(),
    }


def _load_compaction_state(state_file: Path, conversation_id: str) -> Dict[str, Any]:
    default = _default_compaction_state(conversation_id)
    if not state_file.exists():
        return default
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 1:
            merged = dict(default)
            merged.update(data)
            # 从旧版游标平滑迁移时，保留已注入检查点记录并将偏移重置为 0 以补建/重标定遥测画像（绝不重复注入）
            if int(data.get("telemetryVersion", 1)) < 3:
                merged["telemetryVersion"] = 3
                merged["scannedByteOffset"] = 0
                merged["scannedTranscriptName"] = ""
                merged["checkpointSteps"] = []
                merged["mediaStepCount"] = 0
                merged["transcriptImageSizes"] = {}
                merged["transcriptImageCallCount"] = 0
                merged["latestStepIndex"] = -1
                merged["latestUserStepIndex"] = -1
                merged["latestUserInputHash"] = ""
                merged["latestUserInputPreview"] = ""
                merged["isGoalActive"] = False
            if not isinstance(merged.get("checkpointSteps"), list):
                merged["checkpointSteps"] = []
            if not isinstance(merged.get("transcriptImageSizes"), dict):
                merged["transcriptImageSizes"] = {}
            return merged
    except Exception:
        pass
    return default


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
    prev_name = str(state.get("scannedTranscriptName") or "")
    changed = False

    # 若切换了追踪文件名（例如从 transcript_full.jsonl 切换到紧凑版 transcript.jsonl），重置增量遥测但保留已注入游标
    if prev_name and prev_name != transcript_path.name:
        offset = 0
        state["scannedByteOffset"] = 0
        state["scannedTranscriptName"] = transcript_path.name
        state["checkpointSteps"] = []
        state["mediaStepCount"] = 0
        state["transcriptImageSizes"] = {}
        state["transcriptImageCallCount"] = 0
        state["latestStepIndex"] = -1
        state["latestUserStepIndex"] = -1
        state["latestUserInputHash"] = ""
        state["latestUserInputPreview"] = ""
        state["isGoalActive"] = False
        changed = True

    # 若日志文件被截断或重建，从头重置扫描游标（保留已注入记忆仅在文件缩小时归零）
    if file_size < offset:
        offset = 0
        state["scannedByteOffset"] = 0
        state["scannedTranscriptName"] = transcript_path.name
        state["latestCheckpointStep"] = -1
        state["lastInjectedCheckpointStep"] = -1
        state["hasExplicitUserInput"] = False
        state["checkpointSteps"] = []
        state["mediaStepCount"] = 0
        state["transcriptImageSizes"] = {}
        state["transcriptImageCallCount"] = 0
        state["latestStepIndex"] = -1
        state["latestUserStepIndex"] = -1
        state["latestUserInputHash"] = ""
        state["latestUserInputPreview"] = ""
        state["isGoalActive"] = False
        changed = True

    if file_size == offset:
        if state.get("scannedTranscriptName") != transcript_path.name:
            state["scannedTranscriptName"] = transcript_path.name
            changed = True
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
    cp_steps: List[int] = [int(x) for x in (state.get("checkpointSteps") or []) if isinstance(x, int)]
    cp_seen = set(cp_steps)
    media_step_count = int(state.get("mediaStepCount", 0))
    img_sizes: Dict[str, int] = dict(state.get("transcriptImageSizes") or {})
    img_call_count = int(state.get("transcriptImageCallCount", 0))
    latest_step = int(state.get("latestStepIndex", -1))
    latest_user_step = int(state.get("latestUserStepIndex", -1))
    latest_user_hash = str(state.get("latestUserInputHash") or "")
    latest_user_preview = str(state.get("latestUserInputPreview") or "")
    is_goal_active = bool(state.get("isGoalActive", False))
    full_contents_cache: Optional[Dict[int, Any]] = None

    def _stat_image_uri(raw_uri: str) -> None:
        if not raw_uri or len(img_sizes) >= MAX_TRANSCRIPT_TRACKED_IMAGES:
            return
        cleaned = _decode_proto_str(raw_uri)
        if cleaned.startswith("file://"):
            cleaned = cleaned[7:]
            if os.name == "nt" and cleaned.startswith("/") and len(cleaned) > 2 and cleaned[2] == ":":
                cleaned = cleaned[1:]
        suffix = Path(cleaned.replace("\\", "/")).suffix.lower()
        if suffix not in IMAGE_EXTS:
            return
        try:
            p = Path(cleaned)
            key_str = str(p.resolve()) if p.exists() else cleaned
        except OSError:
            key_str = cleaned
        key = key_str.casefold() if os.name == "nt" else key_str
        if key not in img_sizes:
            sz = 0
            try:
                p_obj = Path(cleaned)
                if p_obj.exists() and p_obj.is_file():
                    sz = int(p_obj.stat().st_size)
            except OSError:
                sz = 0
            img_sizes[key] = sz
        elif img_sizes[key] == 0:
            try:
                p_obj = Path(cleaned)
                if p_obj.exists() and p_obj.is_file():
                    img_sizes[key] = int(p_obj.stat().st_size)
            except OSError:
                pass

    parsed_steps: List[Dict[str, Any]] = []
    needed_full_steps: set = set()
    check_goal_trunc = is_goal_active or ("/goal" in text_block.lower())

    for raw_line in text_block.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 轻量预提取 step_index（跳过大行全文 json.loads）
        m_step = STEP_INDEX_RE.search(line[:300])
        if m_step:
            s_num = int(m_step.group(1))
            if s_num > latest_step:
                latest_step = s_num

        # 子串快速预筛，仅解析真正承载检查点/用户指令/图片读数/goal 取消/截断警告的行
        might_be_cp = '"CHECKPOINT"' in line
        might_be_user = '"USER_INPUT"' in line or '"USER_EXPLICIT"' in line
        might_be_media = '"media"' in line
        might_be_read_img = '"read_file"' in line
        might_be_goal_cancel = "GOAL_CANCELLED" in line
        might_be_banner = "conversation grew too large" in line.lower()
        might_be_trunc_goal = check_goal_trunc and '"PLANNER_RESPONSE"' in line and '"truncated_fields"' in line
        might_be_sys = '"SYSTEM_MESSAGE"' in line and might_be_banner
        if not (
            might_be_cp
            or might_be_user
            or might_be_media
            or might_be_read_img
            or might_be_goal_cancel
            or might_be_trunc_goal
            or might_be_sys
        ):
            continue

        try:
            step = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(step, dict):
            continue
        parsed_steps.append(step)

        s_type = step.get("type", "")
        s_source = step.get("source", "")
        step_idx = step.get("step_index")
        trunc_fields = step.get("truncated_fields") or []
        if isinstance(step_idx, int) and isinstance(trunc_fields, list) and "content" in trunc_fields:
            if s_type == "USER_INPUT" or s_source == "USER_EXPLICIT" or (s_type == "PLANNER_RESPONSE" and check_goal_trunc):
                needed_full_steps.add(step_idx)

    # 严格 O(N + M)：对本增量窗口全部截断步骤只执行 1 次完整轨迹线性补全
    if needed_full_steps:
        full_contents_cache = user_words._load_truncated_contents_map(
            transcript_path.parent / "transcript_full.jsonl",
            needed_full_steps,
        )
    else:
        full_contents_cache = {}

    for step in parsed_steps:
        s_type = step.get("type", "")
        s_source = step.get("source", "")
        step_idx = step.get("step_index")
        if isinstance(step_idx, int) and step_idx > latest_step:
            latest_step = step_idx

        if s_type == "CHECKPOINT":
            cp_num = int(step_idx) if isinstance(step_idx, int) else max(latest_cp + 1, 0)
            if cp_num > latest_cp:
                latest_cp = cp_num
            if cp_num not in cp_seen:
                cp_seen.add(cp_num)
                cp_steps.append(cp_num)

        if s_type == "USER_INPUT" or s_source == "USER_EXPLICIT":
            has_user = True
            raw_content = step.get("content", "")
            trunc_fields = step.get("truncated_fields") or []
            if "content" in trunc_fields and isinstance(step_idx, int) and step_idx in full_contents_cache:
                raw_content = full_contents_cache[step_idx]
            raw_has_banner = UI_BANNER_PHRASE in (raw_content or "").lower()
            cleaned_user = user_words.clean_user_content(raw_content)
            if cleaned_user:
                if isinstance(step_idx, int):
                    latest_user_step = step_idx
                if len(cleaned_user) > 4000:
                    preview = cleaned_user[:2500] + "\n...\n" + cleaned_user[-1500:]
                else:
                    preview = cleaned_user
                if raw_has_banner and UI_BANNER_PHRASE not in preview.lower():
                    preview = f"{preview}\n{UI_BANNER_PHRASE}"
                latest_user_preview = preview
                latest_user_hash = hashlib.sha256(cleaned_user.encode("utf-8")).hexdigest()[:16]
                if GOAL_CANCEL_CMD_RE.search(cleaned_user):
                    is_goal_active = False
                elif GOAL_CMD_RE.search(cleaned_user):
                    is_goal_active = True
            elif raw_has_banner and UI_BANNER_PHRASE not in latest_user_preview.lower():
                latest_user_preview = (latest_user_preview + f"\n{UI_BANNER_PHRASE}").strip()

        if s_type == "SYSTEM_MESSAGE":
            sys_content = str(step.get("content") or "")
            if (
                UI_BANNER_PHRASE in sys_content.lower()
                and "the following system message was injected by a" not in sys_content.lower()
                and UI_BANNER_PHRASE not in latest_user_preview.lower()
            ):
                latest_user_preview = (latest_user_preview + f"\n{UI_BANNER_PHRASE}").strip()

        if s_type == "PLANNER_RESPONSE":
            resp_content = step.get("content")
            has_goal_cancel = isinstance(resp_content, str) and "<!-- GOAL_CANCELLED -->" in resp_content
            if not has_goal_cancel and is_goal_active and "content" in (step.get("truncated_fields") or []) and isinstance(step_idx, int):
                full_resp = full_contents_cache.get(step_idx)
                has_goal_cancel = isinstance(full_resp, str) and "<!-- GOAL_CANCELLED -->" in full_resp
            if has_goal_cancel:
                is_goal_active = False

        media_list = step.get("media")
        if isinstance(media_list, list) and media_list:
            if s_type != "CHECKPOINT":
                media_step_count += 1
            for m_item in media_list:
                if isinstance(m_item, dict):
                    uri_val = str(m_item.get("uri") or m_item.get("path") or "")
                    _stat_image_uri(uri_val)

        tool_calls = step.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_name = str(tc.get("name") or tc.get("tool_name") or "").strip()
                if tc_name != "read_file":
                    continue
                tc_args = tc.get("args") if isinstance(tc.get("args"), dict) else tc.get("arguments")
                if isinstance(tc_args, str):
                    try:
                        tc_args = json.loads(tc_args)
                    except Exception:
                        tc_args = {}
                if isinstance(tc_args, dict):
                    f_path = _decode_proto_str(tc_args.get("Path") or tc_args.get("path") or "")
                    if Path(f_path.replace("\\", "/")).suffix.lower() in IMAGE_EXTS:
                        img_call_count += 1
                        _stat_image_uri(f_path)

    state["scannedByteOffset"] = new_offset
    state["scannedTranscriptName"] = transcript_path.name
    state["latestCheckpointStep"] = latest_cp
    state["hasExplicitUserInput"] = has_user
    state["checkpointSteps"] = sorted(cp_seen)
    state["mediaStepCount"] = media_step_count
    state["transcriptImageSizes"] = img_sizes
    state["transcriptImageCallCount"] = img_call_count
    state["latestStepIndex"] = latest_step
    state["latestUserStepIndex"] = latest_user_step
    state["latestUserInputHash"] = latest_user_hash
    state["latestUserInputPreview"] = latest_user_preview
    state["isGoalActive"] = is_goal_active
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

    transcript_path = user_words.resolve_compact_transcript_path(Path(transcript_path_str))
    if not transcript_path.exists() or not transcript_path.is_file():
        return {"injectSteps": []}

    state_dir, _ = get_data_dirs()
    state_file = state_dir / f"{conversation_id}_compaction.json"
    state = _load_compaction_state(state_file, conversation_id)

    state_changed = _scan_transcript_incrementally(transcript_path, state)

    latest_cp = int(state.get("latestCheckpointStep", -1))
    last_injected_cp = int(state.get("lastInjectedCheckpointStep", -1))
    has_user_input = bool(state.get("hasExplicitUserInput", False))

    # 子代理会话（无显式人类 USER_INPUT）不执行主会话原话注入与长会话交接门禁
    if not has_user_input:
        if state_changed:
            user_words.atomic_write_json(state_file, state)
        return {"injectSteps": []}

    # Antigravity 专属长会话“原子收口 + 交接停机”硬门禁评估（懒加载以保持 --client codex 零依赖隔离）
    import handoff_gate  # noqa: E402

    gate_res = handoff_gate.evaluate_pre_invocation(
        conversation_id=conversation_id,
        event_name=event_name,
        state_dir=state_dir,
        compaction_state=state,
        transcript_path=transcript_path,
    )

    trigger_reason: Optional[str] = None
    if latest_cp > last_injected_cp:
        trigger_reason = f"CHECKPOINT step #{latest_cp}"
    elif explicit_source in ("compact", "compaction"):
        trigger_reason = f"explicit {explicit_source}"
    elif explicit_source == "resume" and not state.get("explicitResumeInjected", False):
        trigger_reason = "explicit resume"

    injected_blocks: List[str] = []

    if trigger_reason:
        # 确证发生新的上下文压缩或首次显式恢复：提取纯净用户原话（主会话不含子代理系统消息）
        words, unresolved_steps = user_words.extract_user_words_with_status(
            str(transcript_path), include_subagent_msgs=False
        )
        if words:
            existing_archive = state_dir / f"{conversation_id}_user_words.md"
            if unresolved_steps and existing_archive.exists():
                saved_md_path: Optional[Path] = existing_archive
            else:
                saved_md_path = user_words.save_user_words_markdown(state_dir, conversation_id, words)
            system_msg = build_compaction_system_message(words, saved_md_path, trigger_reason)
            if unresolved_steps:
                system_msg += (
                    f"\n\n> [注] 以下步骤被截断但暂未在 `transcript_full.jsonl` 中找到完整正文：{unresolved_steps}"
                    "（已保留现有完整归档防止覆盖降级）。"
                )
            should_advance_cp = latest_cp > last_injected_cp
            if should_advance_cp and unresolved_steps and int(state.get("unresolvedRetryCp", -1)) != latest_cp:
                state["unresolvedRetryCp"] = latest_cp
                should_advance_cp = False
            elif should_advance_cp:
                state.pop("unresolvedRetryCp", None)
                state["lastInjectedCheckpointStep"] = latest_cp
            if explicit_source == "resume":
                state["explicitResumeInjected"] = True
            state["lastInjectedAt"] = get_current_iso_time()
            state["lastInjectedWordCount"] = len(words)
            state_changed = True
            injected_blocks.append(system_msg)
            log_event(
                event_name,
                conversation_id,
                f"Injected {len(words)} user words on {trigger_reason} (archived to {saved_md_path.name if saved_md_path else 'none'}, unresolved={len(unresolved_steps)}).",
                {"wordCount": len(words), "latestCheckpointStep": latest_cp, "unresolvedSteps": unresolved_steps},
            )
        elif latest_cp > last_injected_cp:
            state["lastInjectedCheckpointStep"] = latest_cp
            state_changed = True

    if gate_res.get("advisoryNotice") and not gate_res.get("mandateMessage"):
        injected_blocks.append(str(gate_res["advisoryNotice"]))
    if gate_res.get("mandateMessage"):
        injected_blocks.append(str(gate_res["mandateMessage"]))

    if state_changed:
        user_words.atomic_write_json(state_file, state)

    if not injected_blocks:
        return {"injectSteps": []}

    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": "\n\n---\n\n".join(injected_blocks),
                }
            }
        ]
    }


# 保持向后兼容函数别名
def handle_session_start(payload: Dict[str, Any]) -> Dict[str, Any]:
    return handle_context_check(payload, event_name="SessionStart")


def handle_pre_invocation(payload: Dict[str, Any]) -> Dict[str, Any]:
    return handle_context_check(payload, event_name="PreInvocation")


def _append_codex_log(log_path: Path, entry: Dict[str, Any]) -> None:
    """向 Codex 钩子日志文件写入仅含元数据的 JSONL 记录（附带 2MB 自动滚动裁剪）"""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
            raw = log_path.read_bytes()
            tail = raw[-TRIM_LOG_BYTES:]
            nl = tail.find(b"\n")
            if nl != -1:
                tail = tail[nl + 1 :]
            user_words.atomic_write_text(log_path, tail.decode("utf-8", errors="replace"))
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def handle_codex_session_start(
    payload: Dict[str, Any],
    state_dir_str: str = "",
    log_path_str: str = "",
) -> Dict[str, Any]:
    """
    处理 Codex SessionStart (compact / resume) 事件：
    提取同 ID 主会话 rollout 原话并落盘至 user-words/<session_id>.md，返回 hookSpecificOutput.additionalContext。
    """
    codex_home_env = os.environ.get("CODEX_HOME", "").strip()
    default_codex_home = Path(codex_home_env).expanduser() if codex_home_env else (Path.home() / ".codex")

    state_dir = Path(os.path.expanduser(state_dir_str)) if state_dir_str else (default_codex_home / "hooks" / "state" / "session-context")
    user_words_dir = (state_dir.parent / "user-words") if state_dir.name == "session-context" else (state_dir / "user-words")
    log_path = Path(os.path.expanduser(log_path_str)) if log_path_str else (default_codex_home / "hooks" / "logs" / "session-context.jsonl")

    source = str(payload.get("source") or "unknown").strip()
    session_id = str(payload.get("session_id") or "").strip()
    transcript_str = str(payload.get("transcript_path") or "").strip()
    cwd_str = str(payload.get("cwd") or os.getcwd())

    if not session_id:
        result: Dict[str, Any] = {
            "complete": False,
            "words": [],
            "issues": ["hook input 缺少有效 session_id"],
            "saved": {"file": "", "available": False, "preserved": False, "count": None, "last_time": None, "sources": []},
            "meta_path": "",
        }
    else:
        result = user_words.refresh_codex_session_archive(transcript_str, user_words_dir, session_id)

    saved = result.get("saved") or {}
    complete = bool(result.get("complete"))
    count = saved.get("count")
    words = result.get("words") or []

    lines: List[str] = [f"恢复来源：{source}"]
    if saved.get("available"):
        lines.append(f"本会话用户原话文件：{saved['file']}")
        if isinstance(count, int) and count > 0:
            scope = f"第 1～{count} 条"
        elif count == 0:
            scope = "0 条"
        else:
            scope = "尚未核实"
        lines.append(f"已保存记录范围：{scope}；最后一条时间：{saved.get('last_time') or '未记录'}")
        if saved.get("preserved"):
            lines.append("文件状态：保留上次已验证文档，本次因读取缺口未覆盖")
        sources = saved.get("sources") or []
        if sources and result.get("meta_path"):
            lines.append(f"源记录分段数：{len(sources)} 段；校验清单：{result['meta_path']}")
    else:
        lines.append("本会话用户原话文件：尚无已确认可读的归档文档")
        lines.append("已保存记录范围：尚未核实")

    lines.append("提取状态：成功（已合并同 ID 主会话记录）" if complete else "提取状态：存在缺口")
    if not complete and result.get("issues"):
        lines.append("缺口信息：" + json.dumps(result["issues"], ensure_ascii=False))
    lines.append("请严格遵照全局规则 R9：结合下方核心指令摘要与上述原话归档文件核对用户全部原始诉求，汇报现状后再继续执行。")

    header_block = "\n".join(lines)
    if words and saved.get("available"):
        inline_msg = build_compaction_system_message(words, Path(saved["file"]), f"Codex {source}")
        # 复用 build_compaction_system_message 的预算控制能力，确保总长受控
        if len(header_block) + len(inline_msg) + 2 <= MAX_INLINE_TOTAL_CHARS + 1200:
            full_context = header_block + "\n\n" + inline_msg
        else:
            full_context = inline_msg
    else:
        full_context = header_block

    _append_codex_log(
        log_path,
        {
            "ts": get_current_iso_time(),
            "event": "SessionStart",
            "session_id": session_id or "unknown",
            "cwd": cwd_str,
            "source": source,
            "pid": os.getpid(),
            "client": "codex",
            "outcome": "saved" if complete else "gap",
            "saved": saved,
            "issues": result.get("issues") or [],
        },
    )

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": full_context,
        }
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity & Codex Compaction Context Loader Hook")
    parser.add_argument("--client", type=str, default="antigravity", choices=("antigravity", "codex"), help="Host client type")
    parser.add_argument("--event", type=str, default="", help="Hook event name: SessionStart or PreInvocation")
    parser.add_argument("--state-dir", type=str, default="", help="Optional state directory override (used by Codex hook)")
    parser.add_argument("--log", type=str, default="", help="Optional JSONL log file path override (used by Codex hook)")
    args, _ = parser.parse_known_args()

    input_text = sys.stdin.read().strip()
    payload: Dict[str, Any] = {}
    if input_text:
        try:
            payload = json.loads(input_text)
        except Exception as e:
            if args.client == "codex":
                sys.stderr.write(f"session_context_loader JSON parse error: {e}\n")
                sys.exit(2)
            log_event(args.event or "Unknown", "unknown", f"Failed to parse stdin JSON: {e}")

    if args.client == "codex":
        try:
            response = handle_codex_session_start(payload, state_dir_str=args.state_dir, log_path_str=args.log)
        except Exception as e:
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": f"恢复来源：{payload.get('source', 'unknown')}\n提取状态：异常（{e}）",
                }
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return

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

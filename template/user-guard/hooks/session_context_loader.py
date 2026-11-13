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
import time
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
ARCHIVE_RETRY_INTERVAL_SEC = 30

STEP_INDEX_RE = re.compile(r'"step_index"\s*:\s*(\d+)')
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
        "telemetryVersion": 6,
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
        "transcriptImageResultCount": 0,
        "transcriptImageAttemptSizes": {},
        "hostBannerStep": -1,
        "latestStepIndex": -1,
        "latestUserStepIndex": -1,
        "latestUserInputHash": "",
        "latestUserInputPreview": "",
        "latestUserHandoffIntent": "",
        "latestHandoffStepIndex": -1,
        "latestHandoffInputHash": "",
        "latestUserBannerReport": False,
        "isGoalActive": False,
        "latestGoalStepIndex": -1,
        "pendingControlSteps": {},
        "controlFullSignature": [],
        "controlFullCheckedAt": 0.0,
        "updatedAt": get_current_iso_time(),
    }


def _load_compaction_state(state_file: Path, conversation_id: str) -> Dict[str, Any]:
    default = _default_compaction_state(conversation_id)
    if not state_file.exists():
        return default
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 1 and data.get("conversationId") == conversation_id:
            merged = dict(default)
            merged.update(data)
            if int(data.get("telemetryVersion", 1)) < default["telemetryVersion"]:
                for key in default.keys() - {"lastInjectedCheckpointStep", "explicitResumeInjected"}:
                    merged[key] = default[key]
            for key in ("checkpointSteps", "transcriptImageSizes", "transcriptImageAttemptSizes",
                        "scannedByteOffset", "latestCheckpointStep", "lastInjectedCheckpointStep",
                        "mediaStepCount", "transcriptImageCallCount", "transcriptImageResultCount",
                        "latestStepIndex", "latestUserStepIndex", "latestHandoffStepIndex", "latestHandoffInputHash",
                        "hostBannerStep", "latestGoalStepIndex",
                        "pendingControlSteps", "controlFullSignature", "controlFullCheckedAt"):
                if type(merged[key]) is not type(default[key]):
                    raise ValueError(f"字段 {key} 格式错误")
            return merged
        raise ValueError("会话状态格式或身份错误")
    except (ValueError, OSError, TypeError) as exc:
        log_event("Context:state_error", conversation_id, f"无法读取上下文状态（{exc}），从轨迹重建")
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
        file_stat = transcript_path.stat()
    except OSError:
        return False

    file_size = file_stat.st_size
    offset = int(state.get("scannedByteOffset", 0))
    prev_name = str(state.get("scannedTranscriptName") or "")
    prev_id = state.get("scannedTranscriptIdentity")
    identity = [file_stat.st_dev, file_stat.st_ino]
    changed = False

    if (prev_name and prev_name != transcript_path.name) or file_size < offset or (prev_id and prev_id != identity):
        previous_checkpoint = state["lastInjectedCheckpointStep"] if prev_name != transcript_path.name else -1
        state.update(_default_compaction_state(state["conversationId"]))
        state["lastInjectedCheckpointStep"] = previous_checkpoint
        state.pop("pendingArchive", None)
        offset = 0
        changed = True

    if file_size == offset:
        return _reconcile_agy_controls(transcript_path, state, []) or changed

    try:
        with open(transcript_path, "rb") as f:
            f.seek(offset)
            chunk = f.read(file_size - offset)
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
    attempt_sizes: Dict[str, int] = dict(state.get("transcriptImageAttemptSizes") or {})
    img_call_count = int(state.get("transcriptImageCallCount", 0))
    img_result_count = int(state.get("transcriptImageResultCount", 0))
    host_banner_step = int(state.get("hostBannerStep", -1))
    latest_step = int(state.get("latestStepIndex", -1))

    def _stat_image_uri(raw_uri: str, image_map: Dict[str, int], is_image_media: bool = False) -> None:
        if not raw_uri or len(image_map) >= MAX_TRANSCRIPT_TRACKED_IMAGES:
            return
        cleaned = _decode_proto_str(raw_uri)
        if cleaned.startswith("file://"):
            cleaned = cleaned[7:]
            if os.name == "nt" and cleaned.startswith("/") and len(cleaned) > 2 and cleaned[2] == ":":
                cleaned = cleaned[1:]
        suffix = Path(cleaned.replace("\\", "/")).suffix.lower()
        if suffix not in IMAGE_EXTS and not is_image_media:
            return
        try:
            p = Path(cleaned)
            key_str = str(p.resolve()) if p.exists() else cleaned
        except OSError:
            key_str = cleaned
        key = key_str.casefold() if os.name == "nt" else key_str
        if key not in image_map:
            sz = 0
            try:
                p_obj = Path(cleaned)
                if p_obj.exists() and p_obj.is_file():
                    sz = int(p_obj.stat().st_size)
            except OSError:
                sz = 0
            image_map[key] = sz
        elif image_map[key] == 0:
            try:
                p_obj = Path(cleaned)
                if p_obj.exists() and p_obj.is_file():
                    image_map[key] = int(p_obj.stat().st_size)
            except OSError:
                pass

    parsed_steps: List[Dict[str, Any]] = []

    for raw_line in text_block.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 轻量预提取 step_index（跳过大行全文 json.loads）
        m_step = STEP_INDEX_RE.search(line[:300]) or (STEP_INDEX_RE.search(line) if '"step_index"' in line else None)
        if m_step:
            s_num = int(m_step.group(1))
            if s_num > latest_step:
                latest_step = s_num

        # 子串快速预筛，仅解析真正承载检查点/用户指令/图片读数/goal 取消/截断警告的行
        might_be_cp = '"CHECKPOINT"' in line
        might_be_user = '"USER_INPUT"' in line or '"USER_EXPLICIT"' in line
        might_be_media = '"media"' in line
        might_be_read_img = '"read_file"' in line or '"view_file"' in line
        might_be_goal_cancel = "GOAL_CANCELLED" in line
        might_be_banner = "conversation grew too large" in line.lower()
        might_be_trunc_goal = '"PLANNER_RESPONSE"' in line and '"truncated_fields"' in line
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

        if s_type == "SYSTEM_MESSAGE" and s_source in ("SYSTEM", "SYSTEM_SDK"):
            text = str(step.get("content") or "").strip()
            for tag in ("SYSTEM_MESSAGE", "EPHEMERAL_MESSAGE"):
                if text.startswith(f"<{tag}>") and text.endswith(f"</{tag}>"):
                    text = text[len(tag) + 2:-(len(tag) + 3)].strip()
            if text.casefold().rstrip(".") == UI_BANNER_PHRASE and isinstance(step_idx, int):
                host_banner_step = max(host_banner_step, step_idx)

        media_list = step.get("media")
        if isinstance(media_list, list) and media_list and not step.get("error"):
            if s_type != "CHECKPOINT":
                media_step_count += 1
            has_image_result = False
            for m_item in media_list:
                if isinstance(m_item, dict):
                    uri_val = str(m_item.get("uri") or m_item.get("path") or "")
                    is_image = str(m_item.get("mime_type") or "").startswith("image/")
                    if s_type != "CHECKPOINT":
                        _stat_image_uri(uri_val, img_sizes, is_image)
                    has_image_result |= is_image or Path(uri_val).suffix.lower() in IMAGE_EXTS
            if has_image_result and s_type in ("GENERIC", "TOOL_RESULT", "TOOL_RESPONSE") and step.get("status") in (
                "DONE", "SUCCESS", "COMPLETED",
            ):
                img_result_count += 1

        tool_calls = step.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_name = str(tc.get("name") or tc.get("tool_name") or "").strip()
                if tc_name not in ("read_file", "view_file"):
                    continue
                tc_args = tc.get("args") or tc.get("arguments")
                if isinstance(tc_args, str):
                    try:
                        tc_args = json.loads(tc_args)
                    except Exception:
                        tc_args = {}
                if isinstance(tc_args, dict):
                    f_path = _decode_proto_str(tc_args.get("AbsolutePath") or tc_args.get("Path") or tc_args.get("path"))
                    if Path(f_path.replace("\\", "/")).suffix.lower() in IMAGE_EXTS:
                        img_call_count += 1
                        _stat_image_uri(f_path, attempt_sizes)

    state["scannedByteOffset"] = new_offset
    state["scannedTranscriptName"] = transcript_path.name
    state["scannedTranscriptIdentity"] = identity
    state["latestCheckpointStep"] = latest_cp
    state["hasExplicitUserInput"] = has_user
    state["checkpointSteps"] = sorted(cp_seen)
    state["mediaStepCount"] = media_step_count
    state["transcriptImageSizes"] = img_sizes
    state["transcriptImageCallCount"] = img_call_count
    state["transcriptImageResultCount"] = img_result_count
    state["transcriptImageAttemptSizes"] = attempt_sizes
    state["hostBannerStep"] = host_banner_step
    state["latestStepIndex"] = latest_step
    _reconcile_agy_controls(transcript_path, state, parsed_steps)
    state["updatedAt"] = get_current_iso_time()
    return True


def _reconcile_agy_controls(transcript_path: Path, state: Dict[str, Any], steps: List[Dict[str, Any]]) -> bool:
    """仅用完整控制事件更新权限。迟到全文按步骤顺序补入，较新已核实命令优先。"""
    pending = state["pendingControlSteps"]
    events: Dict[int, Tuple[str, Any]] = {}
    new_pending = False
    changed = False
    for step in steps:
        step_index = step.get("step_index")
        if not isinstance(step_index, int):
            continue
        if step.get("type") == "USER_INPUT" or step.get("source") == "USER_EXPLICIT":
            event_kind = "user"
        elif step.get("type") == "PLANNER_RESPONSE":
            event_kind = "reply"
        else:
            continue
        if "content" in (step.get("truncated_fields") or []):
            pending[str(step_index)] = event_kind
            new_pending = True
            if event_kind == "user" and step_index >= state["latestUserStepIndex"]:
                state.update(latestUserStepIndex=step_index, latestUserInputHash="", latestUserInputPreview="",
                             latestUserBannerReport=False)
        else:
            events[step_index] = (event_kind, step.get("content"))
            pending.pop(str(step_index), None)
    if pending:
        signature = _agy_source_signatures(transcript_path)[1]
        now = time.time()
        if (new_pending or signature != state["controlFullSignature"]
                or now - state["controlFullCheckedAt"] >= ARCHIVE_RETRY_INTERVAL_SEC):
            full = user_words._load_truncated_contents_map(
                transcript_path.parent / "transcript_full.jsonl", {int(index) for index in pending},
            )
            for index, content in full.items():
                events[index] = (pending.pop(str(index)), content)
            state.update(controlFullSignature=signature, controlFullCheckedAt=now)
            changed = True
    if not events:
        return changed

    import handoff_gate

    for index, (kind, content) in sorted(events.items()):
        if kind == "user":
            cleaned = user_words.clean_user_content(content)
            text_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
            if index >= state["latestUserStepIndex"]:
                preview = cleaned if len(cleaned) <= 4000 else cleaned[:2500] + "\n...\n" + cleaned[-1500:]
                state.update(latestUserStepIndex=index, latestUserInputPreview=preview,
                             latestUserInputHash=text_hash,
                             latestUserBannerReport=handoff_gate.agy_banner_report(cleaned))
            intent = handoff_gate.agy_handoff_intent(cleaned)
            if intent and index >= state["latestHandoffStepIndex"]:
                state.update(latestUserHandoffIntent=intent, latestHandoffStepIndex=index,
                             latestHandoffInputHash=text_hash)
            command = user_words.agy_goal_command(cleaned)
        else:
            command = False if isinstance(content, str) and user_words.has_goal_cancel_marker(content) else None
        if command is not None and index >= state["latestGoalStepIndex"]:
            state.update(latestGoalStepIndex=index, isGoalActive=command)
    return True


def _truncate_single_item(content: str, turn: int, max_chars: int = MAX_INLINE_ITEM_CHARS) -> str:
    if len(content) <= max_chars:
        return content
    head_len = max(max_chars - 220, 200)
    tail_len = 120
    return (
        f"{content[:head_len].rstrip()}\n"
        f"...[第 {turn} 条记录的中间部分省略]...\n"
        f"{content[-tail_len:].lstrip()}"
    )


def build_compaction_system_message(
    words: List[Dict[str, Any]],
    saved_md_path: Optional[Path],
    trigger_reason: str,
    *,
    archive_status: str = "已保存可提取记录",
    issues: Optional[List[str]] = None,
    source_paths: Optional[List[str]] = None,
    header: str = "",
) -> str:
    """构造带预算上限保护与归档文件指针的原话回溯系统消息"""
    archive_str = str(saved_md_path) if saved_md_path else "未落盘"
    total_turns = len(words)

    header_lines = [header] if header else [
        f"【系统原话回溯】会话经过压缩或恢复（触发源：{trigger_reason}）。",
        f"归档位置：{archive_str}",
        f"归档状态：{archive_status}。本次提取 {total_turns} 条记录。",
    ]
    if source_paths:
        header_lines.append("原始轨迹：" + "、".join(source_paths[:2]))
        if len(source_paths) > 2:
            header_lines.append(f"另有 {len(source_paths) - 2} 个分段，参见校验清单。")
    if issues:
        detail = "、".join(issues)
        header_lines.append(f"缺口信息：{detail[:1100]}" + ("…（其余缺口见日志或归档）" if len(detail) > 1100 else ""))
    header_lines.append("请按全局规则 R9 核对原始轨迹、归档状态及用户最新裁定，汇报现状后继续。")
    header_text = "\n".join(header_lines) + "\n\n"

    formatted_blocks: List[str] = []
    for w in words:
        time_tag = f", {w['created_at']}" if w.get("created_at") else ""
        kind_label = " [选项框回答]" if w.get("kind") == "ask_question_answer" else ""
        if w.get("truncated"):
            kind_label += " [截断正文待补]"
        if w.get("provenance_verified") is False:
            kind_label += " [用户角色，来源未核实]"
        body = _truncate_single_item(w["content"], w["turn"], MAX_INLINE_ITEM_CHARS)
        if w.get("attachments") and w.get("source_path"):
            body += f"\n附件来源：{w['source_path']}"
        block = f"【第 {w['turn']} 轮指令】{kind_label} (Step {w['step_index']}{time_tag})\n{body}"
        formatted_blocks.append(block)

    joined_all = "\n\n".join(formatted_blocks)
    if len(header_text) + len(joined_all) <= MAX_INLINE_TOTAL_CHARS or not formatted_blocks:
        return header_text + joined_all

    # 超出总预算时：必保第 1 轮立项总纲 + 逆序尽可能多地保留最近定论轮次
    first_block = formatted_blocks[0]
    budget_left = MAX_INLINE_TOTAL_CHARS - len(header_text) - len(first_block) - 180
    tail_blocks: List[str] = []
    used = 0
    for blk in reversed(formatted_blocks[1:]):
        if used + len(blk) + 2 > budget_left:
            break
        tail_blocks.append(blk)
        used += len(blk) + 2
    tail_blocks.reverse()

    omitted_count = len(formatted_blocks) - 1 - len(tail_blocks)
    if omitted_count > 0:
        end_omitted_turn = 1 + omitted_count
        omit_notice = (
            f"...（中间第 2～{end_omitted_turn} 轮共 {omitted_count} 条历史指令因上下文预算省略内联；"
            "请按 R9 查阅轨迹和已保存的归档）..."
        )
        return header_text + "\n\n".join([first_block, omit_notice] + tail_blocks)

    return header_text + joined_all


def _agy_source_signatures(transcript_path: Path) -> List[List[int]]:
    signatures: List[List[int]] = []
    for path in (transcript_path, transcript_path.parent / "transcript_full.jsonl"):
        try:
            stat = path.stat()
            signatures.append([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])
        except OSError:
            signatures.append([])
    return signatures


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

    if not transcript_path_str or not user_words.valid_session_id(conversation_id):
        return {"injectSteps": []}

    transcript_path = user_words.resolve_compact_transcript_path(Path(transcript_path_str).expanduser())
    if not transcript_path.exists() or not transcript_path.is_file():
        return {"injectSteps": []}

    state_dir, _ = get_data_dirs()
    try:
        with user_words.session_state_lock(state_dir, conversation_id):
            return _handle_context_locked(conversation_id, event_name, explicit_source, transcript_path, state_dir)
    except (TimeoutError, OSError) as exc:
        log_event(event_name, conversation_id, f"上下文状态未更新（{exc}）")
        return {"injectSteps": []}


def _handle_context_locked(conversation_id: str, event_name: str, explicit_source: str,
                           transcript_path: Path, state_dir: Path) -> Dict[str, Any]:
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

    try:
        gate_res = handoff_gate.evaluate_pre_invocation_locked(
            conversation_id=conversation_id,
            event_name=event_name,
            state_dir=state_dir,
            compaction_state=state,
            transcript_path=transcript_path,
        )
    except (ValueError, TypeError, OSError) as exc:
        log_event(f"{event_name}:handoff_gate_error", conversation_id, f"交接门禁状态异常，继续原话恢复（{exc}）")
        gate_res = {"injectMessage": None, "phase": "normal", "stateChanged": False}

    trigger_reason: Optional[str] = None
    pending = state.get("pendingArchive")
    pending = pending if isinstance(pending, dict) else {}
    signatures = _agy_source_signatures(transcript_path)
    if latest_cp > last_injected_cp:
        trigger_reason = f"CHECKPOINT step #{latest_cp}"
    elif explicit_source in ("compact", "compaction") and state.get("lastExplicitCompact") != signatures[0]:
        trigger_reason = f"explicit {explicit_source}"
    elif explicit_source == "resume" and not state.get("explicitResumeInjected", False):
        trigger_reason = "explicit resume"
    elif pending:
        last_scan = pending.get("checkedAt", 0)
        last_scan = last_scan if isinstance(last_scan, (float, int)) else 0
        old_signatures = pending.get("sources")
        full_changed = isinstance(old_signatures, list) and len(old_signatures) == 2 and old_signatures[1] != signatures[1]
        if full_changed or time.time() - last_scan >= ARCHIVE_RETRY_INTERVAL_SEC:
            trigger_reason = "原话归档缺口复核"

    injected_blocks: List[str] = []

    if trigger_reason:
        # 确证发生新的上下文压缩或首次显式恢复：提取纯净用户原话（主会话不含子代理系统消息）
        archive = user_words.extract_agy_archive(str(transcript_path), include_subagent_msgs=False)
        words = archive["words"]
        unresolved_steps = archive["unresolved_steps"]
        unverified_answers = archive["unverified_answers"]
        issues = list(archive["issues"])
        if unresolved_steps:
            issues.append(f"以下步骤的截断正文待补：{unresolved_steps}")
        if unverified_answers:
            issues.append(f"以下步骤的选项回答来源未核实：{unverified_answers}")
        if _agy_source_signatures(transcript_path) != signatures:
            issues.append("提取时轨迹发生变化，待再次核对")
        if not words:
            issues.append("未提取到可归档的用户输入")

        existing = state_dir / f"{conversation_id}_user_words.md"
        previous_issues = pending.get("issues")
        update_notice = trigger_reason != "原话归档缺口复核" or issues != previous_issues
        previous_was_complete = state.get("archiveComplete") is True
        if issues and existing.is_file() and previous_was_complete:
            saved_md_path = existing
            archive_status = "保留已有文件，本次提取存在缺口"
        else:
            saved_md_path = (user_words.save_user_words_markdown(state_dir, conversation_id, words, issues)
                             if update_notice or not existing.is_file() else existing)
            archive_status = "已保存部分记录" if issues else "本次可见轨迹中的原话已归档"
            state["archiveComplete"] = bool(saved_md_path) and not issues
        if issues:
            state["pendingArchive"] = {"sources": signatures, "checkedAt": time.time(), "issues": issues}
        else:
            state.pop("pendingArchive", None)
        if latest_cp > last_injected_cp:
            state["lastInjectedCheckpointStep"] = latest_cp
        if explicit_source == "resume":
            state["explicitResumeInjected"] = True
        if explicit_source in ("compact", "compaction"):
            state["lastExplicitCompact"] = signatures[0]
        state_changed = True
        if update_notice:
            injected_blocks.append(build_compaction_system_message(
                words, saved_md_path, trigger_reason, archive_status=archive_status, issues=issues,
                source_paths=[str(transcript_path), str(transcript_path.parent / "transcript_full.jsonl")],
            ))
            state["lastInjectedAt"] = get_current_iso_time()
            state["lastInjectedWordCount"] = len(words)
        log_event(
            event_name, conversation_id, f"原话归档检查：{trigger_reason}（{archive_status}）",
            {"wordCount": len(words), "latestCheckpointStep": latest_cp, "unresolvedSteps": unresolved_steps,
             "unverifiedAnswers": unverified_answers, "issues": issues},
        )

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
    session_id, transcript, identity_error, skip = user_words.codex_hook_session(payload)
    if skip:
        return {}
    cwd_str = str(payload.get("cwd") or os.getcwd())

    result = user_words.refresh_codex_session_archive(
        str(transcript) if transcript else "", user_words_dir, session_id, identity_error=identity_error,
    )

    saved = result.get("saved") or {}
    complete = bool(result.get("complete"))
    count = saved.get("count")
    words = result.get("words") or []

    lines: List[str] = [f"恢复来源：{source}",
                        "提取状态：成功（已合并可见的同 ID 主会话记录）" if complete else "提取状态：存在缺口"]
    if saved.get("available"):
        lines.append(f"本会话用户原话文件：{saved['file']}")
        if isinstance(count, int) and count > 0:
            scope = f"第 1～{count} 条"
        elif count == 0:
            scope = "0 条"
        else:
            scope = "尚未核实"
        lines.append(f"已保存记录范围：{scope}。最后一条时间：{saved.get('last_time') or '未记录'}。")
        if saved.get("preserved"):
            lines.append("文件状态：保留上次已验证文档，本次因读取缺口未覆盖")
        elif saved.get("verified"):
            lines.append("文件状态：归档正文哈希与清单一致，本次可见记录已归档")
        elif saved.get("legacy_manifest"):
            lines.append("文件状态：旧版归档正文哈希与清单一致，完整性未验证")
        elif saved.get("manifest_verified"):
            lines.append("文件状态：归档正文哈希与清单一致，记录范围存在缺口")
        else:
            lines.append("文件状态：正文可读，完整性未验证")
        sources = saved.get("sources") or []
        if sources:
            lines.append(f"已保存记录来源：{len(sources)} 段。校验清单：{result['meta_path']}")
    else:
        lines.append("本会话用户原话文件：尚无已确认可读的归档文档")
        lines.append("已保存记录范围：尚未核实")

    source_paths = result["segments"] or [item["path"] for item in saved.get("sources") or []]
    full_context = build_compaction_system_message(
        words, Path(saved["file"]) if saved.get("available") else None, f"Codex {source}",
        header="\n".join(lines), issues=result.get("issues"), source_paths=source_paths,
    )

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

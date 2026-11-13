"""
handoff_gate.py - 长会话交接策略与 Antigravity / Codex 宿主适配 (user-guard)

Antigravity 适配保留原有遥测与门禁；Codex 独立入口默认仅观测和提醒，
仅在显式 enforce 模式及显式压缩次数策略或用户交接指令下进入排空。
以下检查点、图片和 /goal 规则只用于 Antigravity：
1. 结合增量会话遥测（压缩检查点数、近期检查点间隔坍塌、transcript 体积、多模态读图数/字节数）、
   UI 截断告警与显式 R8 用户暂缓指令，维护四态状态机：
   normal -> advisory -> drain -> freeze（或 R8 暂缓 overridden）。
2. Stage-1 (advisory)：纯提醒不阻断，在 PreInvocation 提醒主控注意上下文预算与大体积读图消耗。
3. Stage-2 (drain -> freeze)：
   - 严禁新开委派（invoke_subagent / define_subagent / swarm / schedule / browser_* / cae_harness 派工 / git push）；
   - drain 排空窗口（默认 4 次模型调用 + 8 次写/运行动作）内允许完成正在进行中的原子收口与本地校验；
   - freeze 冻结期仅放行：非图片只读核对、后台任务/子代理查询管理（manage_task / manage_subagents）、交接文档编写与本地 git add/commit；
   - Stop 门禁验收交接完整性（并在开启 /goal 时强制校验 <!-- GOAL_CANCELLED --> 标记），每回合至多阻断 1 次。
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, ContextManager, Dict, List, Optional, Tuple

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
import user_words  # noqa: E402

user_words.ensure_utf8_stdio()

# 阶段预算默认值
DEFAULT_DRAIN_INVOCATIONS = 4
DEFAULT_DRAIN_ACTIONS = 8

# 遥测阈值是项目收口策略的启发式参数，不能据此推断模型的上下文容量。
ADVISORY_CP_THRESHOLD = 11
ADVISORY_TRANSCRIPT_BYTES = 4 * 1024 * 1024
ADVISORY_THRASH_MIN_CP = 6
ADVISORY_THRASH_AVG_INTERVAL = 90.0
ADVISORY_MEDIA_STEPS = 60
ADVISORY_IMAGE_CALLS = 35
ADVISORY_IMAGE_BYTES = 50 * 1024 * 1024

DRAIN_CP_THRESHOLD = 16
DRAIN_THRASH_MIN_CP = 10
DRAIN_THRASH_AVG_INTERVAL = 75.0
DRAIN_TRANSCRIPT_BYTES = 6 * 1024 * 1024
DRAIN_MEDIA_STEPS = 160
DRAIN_MEDIA_MIN_CP = 6
DRAIN_IMAGE_CALLS = 65
DRAIN_IMAGE_BYTES = 100 * 1024 * 1024

# 用户直接报告 UI 告警时，另检查现有轨迹是否达到最低遥测门槛。
UI_BANNER_PHRASE = "this conversation grew too large, so the history before this point may be missing or incomplete"
BANNER_PLAUSIBLE_CP = 2
BANNER_PLAUSIBLE_STEP = 120
BANNER_PLAUSIBLE_TRANSCRIPT_BYTES = 256 * 1024
BANNER_PLAUSIBLE_MEDIA_STEPS = 20
BANNER_PLAUSIBLE_IMAGE_CALLS = 20
BANNER_PLAUSIBLE_IMAGE_BYTES = 30 * 1024 * 1024

OVERRIDE_SNOOZE_CPS = 3
OVERRIDE_SNOOZE_STEPS = 120
MAX_TRACKED_IMAGES = 2000

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

OVERRIDE_COMMAND = (
    r"(?:暂不交接|先不交接|先别交接|不用交接|不要交接|取消交接|解除交接|暂缓交接|跳过交接|"
    r"先继续(?:当前|完成|执行)|继续当前对话|不用开新对话|先别停|继续往下做)"
)
AGY_OVERRIDE_REQUEST_RE = re.compile(
    r"(?:请(?:你)?)?(?:" + OVERRIDE_COMMAND + r"|继续当前对话(?:做完|完成)这一批)"
    r"(?:[，,、]\s*(?:请(?:你)?)?(?:" + OVERRIDE_COMMAND + r"|继续当前对话(?:做完|完成)这一批))*[。.!！]*",
)
AGY_HANDOFF_REQUEST_RE = re.compile(
    r"(?:/handoff|(?:请(?:你)?)?(?:立即)?(?:处理好交接|(?:整理|开始|准备|完成|做好|立即|执行)交接)"
    r"(?:并|后)?(?:停机|停止|结束当前对话)?)[。.!！]*",
)
AGY_BANNER_REPORT_RE = re.compile(
    r"(?:我(?:刚才|刚刚)?(?:看到|收到)|(?:界面|宿主|客户端|对话)(?:刚才|刚刚)?(?:显示|出现|提示))"
    r"(?:了)?(?:界面|系统|截断|上下文|会话|UI|\s)*(?:告警|提示|报错)?\s*[:：]\s*[“\"‘']?"
    + re.escape(UI_BANNER_PHRASE) + r"[.。]?[”\"’']?[.。!！]*",
    re.IGNORECASE,
)

# 交接产物的识别正则
HANDOFF_FILENAME_RE = re.compile(
    r"(?:handoff|handover|resume|next[_-]?prompt|new[_-]?conversation|交接|接续|提示词|停机|^state\.md$)",
    re.IGNORECASE,
)

# Stop 回复校验中提示词标记
PROMPT_MARKER_RE = re.compile(
    r"(?:新对话提示词|下一对话提示词|接续提示词|交接提示词|新会话提示词|next\s*prompt|new\s*conversation\s*prompt|"
    r"新开对话.*?提示词|复现与接续提示词)",
    re.IGNORECASE,
)

DELEGATION_TOOLS = {"invoke_subagent", "define_subagent", "swarm", "schedule"}
AGY_TOOL_ALIASES = {
    "view_file": "read_file",
    "write_to_file": "write_file",
    "replace_file_content": "edit_file",
    "multi_replace_file_content": "edit_file",
}
AGY_INSPECTION_TOOLS = {"list_dir", "list_directory", "view_file_outline", "view_code_item", "grep_search",
                        "find_by_name", "search_files", "manage_task", "manage_subagents", "ask_question", "finish"}
MEDIA_TOOLS = {"generate_image", "imagegen", "view_image", "open_image"}

READONLY_BINARIES = {
    "ls",
    "dir",
    "pwd",
    "echo",
    "printf",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "fd",
    "which",
    "where",
    "type",
    "stat",
    "get-content",
    "gc",
    "get-childitem",
    "gci",
    "select-string",
    "sls",
    "get-location",
    "test-path",
    "git",
}

GIT_READONLY_SUBCMDS = {"status", "diff", "log", "show", "rev-parse", "ls-files", "describe", "shortlog"}
GIT_LOCAL_COMMIT_SUBCMDS = {"add", "commit"}
SHELL_WRAPPERS = {"bash", "sh", "zsh", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "env"}
SUBSYSTEM_WRAPPERS = {"wsl", "wsl.exe", "ssh", "ssh.exe", "docker", "docker.exe", "podman"}


def default_gate_state(conversation_id: str) -> Dict[str, Any]:
    """生成初始长会话交接门禁状态"""
    return {
        "version": 1,
        "gateSchemaVersion": 3,
        "conversationId": conversation_id,
        "phase": "normal",
        "triggers": [],
        "bannerDetectedAtStep": None,
        "bannerTriggerTurnHash": "",
        "bannerTriggerHostStep": -1,
        "enteredDrainAtStep": None,
        "enteredFreezeAtStep": None,
        "drainInvocationsRemaining": DEFAULT_DRAIN_INVOCATIONS,
        "drainActionsRemaining": DEFAULT_DRAIN_ACTIONS,
        "handoffFilesTouched": [],
        "handoffFileBaselines": {},
        "imageReads": {},
        "imageReadCallCount": 0,
        "isGoalActive": False,
        "overriddenTurnHash": "",
        "handoffRequestTurnHash": "",
        "overrideUntilCheckpoint": -1,
        "overrideUntilStep": -1,
        "stopGateBlockedTurnKeys": [],
        "lastAdvisoryCp": -1,
        "lastAdvisoryImageMilestone": 0,
        "lastAdvisoryTriggers": [],
        "updatedAt": user_words.current_iso_time(),
    }


def _gate_state_path(state_dir: Path, conversation_id: str) -> Path:
    return state_dir / f"{conversation_id}_handoff_gate.json"


def load_gate_state(state_dir: Path, conversation_id: str) -> Dict[str, Any]:
    """安全加载或初始化会话级交接门禁状态"""
    state_file = _gate_state_path(state_dir, conversation_id)
    default = default_gate_state(conversation_id)
    if not state_file.exists():
        return default
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        if (not isinstance(raw, dict) or raw.get("conversationId") != conversation_id
                or raw.get("version") != 1):
            raise ValueError("门禁状态格式或身份错误")
        merged = dict(default)
        merged.update(raw)
        merged["gateSchemaVersion"] = int(raw.get("gateSchemaVersion", 1))
        for key in ("triggers", "handoffFilesTouched", "handoffFileBaselines", "imageReads", "stopGateBlockedTurnKeys",
                    "lastAdvisoryTriggers", "drainInvocationsRemaining", "drainActionsRemaining",
                    "imageReadCallCount", "overrideUntilCheckpoint", "overrideUntilStep", "bannerTriggerHostStep"):
            if type(merged[key]) is not type(default[key]):
                raise ValueError(f"门禁状态字段 {key} 格式错误")
        if merged["phase"] not in ("normal", "advisory", "drain", "freeze", "overridden"):
            raise ValueError("未知的门禁阶段")
        return merged
    except (TypeError, ValueError, OSError) as exc:
        user_words.append_lifecycle_log(HOOKS_DIR, "HandoffGate:state_error", conversation_id,
                                        f"门禁状态读取失败（{exc}），保留原文件")
        raise


def save_gate_state(state_dir: Path, conversation_id: str, gate_state: Dict[str, Any]) -> None:
    gate_state["updatedAt"] = user_words.current_iso_time()
    user_words.atomic_write_json(_gate_state_path(state_dir, conversation_id), gate_state)


def consume_drain_action(gate_state: Dict[str, Any]) -> bool:
    """共用阶段策略：完成一次获准的收尾动作后扣预算，用尽即冻结。"""
    remaining = max(0, int(gate_state.get("drainActionsRemaining", 0)) - 1)
    gate_state["drainActionsRemaining"] = remaining
    if remaining == 0:
        gate_state["phase"] = "freeze"
    return remaining == 0


def _gate_state_lock(state_dir: Path, conversation_id: str, timeout_sec: float = 1.5,
                     strict: bool = False) -> ContextManager[None]:
    """保留既有测试和调用入口，两种模式均须持锁才可进入事务。"""
    return user_words.session_state_lock(state_dir, conversation_id, timeout_sec)


def is_image_path(path_str: str) -> bool:
    if not path_str:
        return False
    cleaned = path_str.strip().strip("\"'")
    return Path(cleaned.replace("\\", "/")).suffix.lower() in IMAGE_EXTENSIONS


def _resolve_candidate_path(
    raw_path: str,
    cwd: Optional[str] = None,
    workspace_paths: Optional[List[str]] = None,
) -> Path:
    cleaned = os.path.expanduser(raw_path.strip().strip("\"'"))
    p = Path(cleaned)
    if p.is_absolute():
        return p
    bases: List[Path] = []
    if cwd and str(cwd).strip():
        bases.append(Path(str(cwd).strip()))
    if workspace_paths:
        for wp in workspace_paths:
            if wp and str(wp).strip():
                bases.append(Path(str(wp).strip()))
    for base in bases:
        cand = base / p
        if cand.exists():
            return cand
    if bases:
        return bases[0] / p
    return p


def _path_key(p: Path) -> str:
    try:
        resolved = str(p.resolve())
    except OSError:
        resolved = str(p)
    return resolved.casefold() if os.name == "nt" else resolved


def record_image_read(
    gate_state: Dict[str, Any],
    raw_path: str,
    cwd: Optional[str] = None,
    workspace_paths: Optional[List[str]] = None,
    increment_calls: bool = True,
) -> Tuple[int, int]:
    """登记 PreToolUse 图片请求，文件大小用于估算可能增加的图片负载。"""
    if increment_calls:
        gate_state["imageReadCallCount"] = int(gate_state.get("imageReadCallCount", 0)) + 1

    img_map: Dict[str, int] = gate_state.setdefault("imageReads", {})
    cleaned = raw_path.strip().strip("\"'")
    if cleaned.startswith("file://"):
        cleaned = cleaned[7:]
        if os.name == "nt" and cleaned.startswith("/") and len(cleaned) > 2 and cleaned[2] == ":":
            cleaned = cleaned[1:]
    target = _resolve_candidate_path(cleaned, cwd=cwd, workspace_paths=workspace_paths)
    key = _path_key(target)

    if key not in img_map and len(img_map) < MAX_TRACKED_IMAGES:
        fsize = 0
        try:
            if target.exists() and target.is_file():
                fsize = int(target.stat().st_size)
        except OSError:
            fsize = 0
        img_map[key] = fsize

    return len(img_map), sum(int(v) for v in img_map.values() if isinstance(v, (int, float)))


def _merge_telemetry(
    compaction_state: Dict[str, Any],
    gate_state: Dict[str, Any],
    transcript_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """整合 transcript 增量遥测与实时 PreToolUse 图片读数"""
    cp_steps_raw = compaction_state.get("checkpointSteps") or []
    cp_steps = sorted({int(x) for x in cp_steps_raw if isinstance(x, int) and x >= 0})
    cp_count = len(cp_steps)

    intervals = [cp_steps[i] - cp_steps[i - 1] for i in range(1, len(cp_steps)) if cp_steps[i] > cp_steps[i - 1]]
    recent_avg_interval: Optional[float] = None
    if len(intervals) >= 3:
        tail3 = intervals[-3:]
        recent_avg_interval = sum(tail3) / len(tail3)

    transcript_bytes = 0
    if transcript_path is not None:
        transcript_path = user_words.resolve_compact_transcript_path(transcript_path)
        if transcript_path.exists():
            try:
                transcript_bytes = int(transcript_path.stat().st_size)
            except OSError:
                transcript_bytes = 0

    recorded_imgs = compaction_state.get("transcriptImageSizes") or {}
    img_map: Dict[str, int] = {}
    for source in (recorded_imgs, compaction_state.get("transcriptImageAttemptSizes") or {}, gate_state["imageReads"]):
        for key, size in source.items():
            if isinstance(size, int) and size >= 0 and (key in img_map or len(img_map) < MAX_TRACKED_IMAGES):
                img_map[key] = max(size, img_map.get(key, 0))

    comp_img_calls = int(compaction_state.get("transcriptImageCallCount", 0))
    gate_img_calls = int(gate_state.get("imageReadCallCount", 0))
    result_count = int(compaction_state.get("transcriptImageResultCount", 0))
    image_attempt_count = max(comp_img_calls, gate_img_calls)
    image_call_count = max(image_attempt_count, result_count, len(img_map))
    total_image_bytes = sum(img_map.values())
    media_step_count = int(compaction_state.get("mediaStepCount", 0))

    latest_step = max(
        int(compaction_state.get("latestStepIndex", -1)),
        cp_steps[-1] if cp_steps else -1,
    )

    return {
        "cpCount": cp_count,
        "latestCp": cp_steps[-1] if cp_steps else -1,
        "recentAvgInterval": recent_avg_interval,
        "transcriptBytes": transcript_bytes,
        "mediaStepCount": media_step_count,
        "uniqueImageCount": len(img_map),
        "imageCallCount": image_call_count,
        "imageAttemptCount": image_attempt_count,
        "imageResultCount": result_count,
        "recordedImageBytes": sum(v for v in recorded_imgs.values() if isinstance(v, int) and v >= 0),
        "totalImageBytes": total_image_bytes,
        "latestStep": latest_step,
        "latestUserInputHash": str(compaction_state.get("latestUserInputHash") or ""),
        "latestUserInputPreview": str(compaction_state.get("latestUserInputPreview") or ""),
        "latestUserHandoffIntent": compaction_state.get("latestUserHandoffIntent"),
        "latestHandoffStepIndex": compaction_state.get("latestHandoffStepIndex"),
        "latestHandoffInputHash": compaction_state.get("latestHandoffInputHash"),
        "latestUserBannerReport": compaction_state.get("latestUserBannerReport"),
        "latestUserStepIndex": int(compaction_state.get("latestUserStepIndex", -1)),
        "hostBannerStep": int(compaction_state.get("hostBannerStep", -1)),
        "isGoalActive": bool(compaction_state.get("isGoalActive", False)),
    }


def agy_handoff_intent(user_text: str) -> str:
    """识别独立指令句，按最后一条直接指令处理，忽略引用与代码。"""
    intent = ""
    for line in user_words.markdown_unquoted_lines(user_text):
        if AGY_OVERRIDE_REQUEST_RE.fullmatch(line):
            intent = "override"
        elif AGY_HANDOFF_REQUEST_RE.fullmatch(line):
            intent = "handoff"
    return intent


def agy_banner_report(user_text: str) -> bool:
    return any(AGY_BANNER_REPORT_RE.fullmatch(line) for line in user_words.markdown_unquoted_lines(user_text))


def _evaluate_triggers(tel: Dict[str, Any], user_text: str) -> Tuple[List[str], List[str]]:
    """计算当前满足的 Stage-1 (advisory) 与 Stage-2 (drain) 触发条件列表"""
    advisory_hits: List[str] = []
    stage2_hits: List[str] = []

    cp_count = tel["cpCount"]
    avg_int = tel["recentAvgInterval"]
    tp_bytes = tel["transcriptBytes"]
    media_steps = tel["mediaStepCount"]
    img_calls = tel["imageCallCount"]
    img_bytes = tel["totalImageBytes"]
    latest_step = tel["latestStep"]

    # Stage 1 (零阻断温控预警)
    if cp_count >= ADVISORY_CP_THRESHOLD:
        advisory_hits.append("cp_warning")
    if tp_bytes >= ADVISORY_TRANSCRIPT_BYTES:
        advisory_hits.append("transcript_warning")
    if cp_count >= ADVISORY_THRASH_MIN_CP and avg_int is not None and avg_int <= ADVISORY_THRASH_AVG_INTERVAL:
        advisory_hits.append("interval_warning")
    if (cp_count >= ADVISORY_THRASH_MIN_CP and media_steps >= ADVISORY_MEDIA_STEPS) or img_calls >= ADVISORY_IMAGE_CALLS:
        advisory_hits.append("media_warning")
    if img_bytes >= ADVISORY_IMAGE_BYTES:
        advisory_hits.append("image_bytes_warning")

    # Stage 2 (强收口硬门禁)
    user_report = tel.get("latestUserBannerReport")
    if user_report is None:
        user_report = agy_banner_report(user_text)
    if user_report:
        banner_plausible = (
            cp_count >= BANNER_PLAUSIBLE_CP
            or latest_step >= BANNER_PLAUSIBLE_STEP
            or tp_bytes >= BANNER_PLAUSIBLE_TRANSCRIPT_BYTES
            or media_steps >= BANNER_PLAUSIBLE_MEDIA_STEPS
            or img_calls >= BANNER_PLAUSIBLE_IMAGE_CALLS
            or img_bytes >= BANNER_PLAUSIBLE_IMAGE_BYTES
        )
        if banner_plausible:
            stage2_hits.append("user_banner_report")

    if cp_count >= DRAIN_CP_THRESHOLD:
        stage2_hits.append("cp_count")
    if cp_count >= DRAIN_THRASH_MIN_CP and avg_int is not None and avg_int <= DRAIN_THRASH_AVG_INTERVAL:
        stage2_hits.append("cp_thrashing")
    if tp_bytes >= DRAIN_TRANSCRIPT_BYTES:
        stage2_hits.append("transcript_bytes")
    if img_bytes >= DRAIN_IMAGE_BYTES:
        stage2_hits.append("media_bytes")
    if (media_steps >= DRAIN_MEDIA_STEPS and cp_count >= DRAIN_MEDIA_MIN_CP) or img_calls >= DRAIN_IMAGE_CALLS:
        stage2_hits.append("media_flood")

    return advisory_hits, stage2_hits


def _format_telemetry_brief(tel: Dict[str, Any]) -> str:
    mb_tp = tel["transcriptBytes"] / (1024 * 1024)
    mb_img = tel["totalImageBytes"] / (1024 * 1024)
    avg_txt = f"{tel['recentAvgInterval']:.0f}" if tel["recentAvgInterval"] is not None else "N/A"
    return (
        f"压缩={tel['cpCount']}次, 近期均值间隔={avg_txt}步, "
        f"transcript={mb_tp:.2f}MB，读图尝试={tel['imageAttemptCount']}次，"
        f"轨迹图片结果={tel['imageResultCount']}步，图片文件估算={mb_img:.1f}MB"
    )


def _build_advisory_message(tel: Dict[str, Any], triggers: List[str]) -> str:
    extra = ""
    if "media_warning" in triggers or "image_bytes_warning" in triggers:
        extra = "；检测到批量读图偏高，请改用已提取文本/脚本核对，避免无谓大图耗尽上下文。"
    return (
        f"【长会话提醒 · Stage-1 advisory】当前会话达到启发式收口预警阈值（{_format_telemetry_brief(tel)}）。"
        f"建议在完成本原子单元并提交本地 Git 后适时整理交接；若非必要请勿新开长耗时探索{extra}"
    )


def _build_drain_message(gate_state: Dict[str, Any], tel: Dict[str, Any]) -> str:
    goal_line = (
        "\n- 注意：检测到当前会话开启了 `/goal`，最终交接输出末尾必须附带 `<!-- GOAL_CANCELLED -->` 以终止循环！"
        if gate_state.get("isGoalActive")
        else ""
    )
    return (
        f"【长会话强收口门禁 · Stage-2 drain】触发源：{', '.join(gate_state.get('triggers') or ['pressure'])} "
        f"（{_format_telemetry_brief(tel)}）。\n"
        f"- 当前进入原子收口排空期（剩余模型调用 {gate_state['drainInvocationsRemaining']} 次 / 写或运行动作 {gate_state['drainActionsRemaining']} 次）：\n"
        "  1. 禁止新开 subagent/swarm/schedule/browser/cae_harness 派工、禁止读图及 `git push`；\n"
        "  2. 仅完成手头未闭环的局部收尾与本地验收，`git add`/`git commit`；\n"
        "  3. 全量整理当前已验证进展、未尽事项与可直接粘贴的【新对话提示词】写入交接文档或在终局回复给出，随后立即停机。"
        + goal_line
    )


def _build_freeze_message(gate_state: Dict[str, Any], tel: Dict[str, Any]) -> str:
    goal_line = " 并在末尾附带 `<!-- GOAL_CANCELLED -->`" if gate_state.get("isGoalActive") else ""
    return (
        f"【长会话强收口冻结 · Stage-2 freeze】触发源：{', '.join(gate_state.get('triggers') or ['pressure'])} "
        f"（{_format_telemetry_brief(tel)}）。\n"
        "- 排空预算已耗尽！当前仅允许：① 非图片文件只读核对 / `manage_task` / `manage_subagents`；② 编写交接档案；③ 本地 `git add`/`git commit`。\n"
        f"- 请立即整理已实测状态、未决问题与【新对话提示词】{goal_line}，直接收尾停机交由新对话接续。"
    )


def _refresh_phase_from_telemetry(
    gate_state: Dict[str, Any],
    tel: Dict[str, Any],
    user_text: str,
    conversation_id: str,
    decrement_invocation: bool = False,
) -> bool:
    """
    根据最新遥测与用户指令刷新门禁状态机，返回 gate_state 是否发生变化。
    """
    changed = False
    prev_phase = str(gate_state.get("phase") or "normal")
    turn_hash = tel.get("latestUserInputHash") or ""
    u_step_idx = int(tel.get("latestUserStepIndex", -1))
    turn_token = f"{u_step_idx}:{turn_hash}" if (turn_hash and u_step_idx >= 0) else turn_hash
    intent = tel.get("latestUserHandoffIntent")
    if intent is None:
        intent = agy_handoff_intent(user_text)
    control_hash = tel.get("latestHandoffInputHash")
    control_step = tel.get("latestHandoffStepIndex")
    if control_step is None:
        control_token, control_hash = turn_token, turn_hash
    else:
        control_token = f"{control_step}:{control_hash}" if control_hash else ""
    new_request = intent == "handoff" and control_token and gate_state.get("handoffRequestTurnHash") != control_token
    host_banner_step = int(tel.get("hostBannerStep", -1))
    host_banner_is_new = host_banner_step > int(gate_state.get("bannerTriggerHostStep", -1))
    advisory_hits, stage2_hits = _evaluate_triggers(tel, user_text)

    # 重新核对旧版单一体积或匿名告警触发，保留其他已登记的触发依据。
    if int(gate_state.get("gateSchemaVersion", 1)) < 3:
        gate_state["gateSchemaVersion"] = 3
        gate_state["handoffFilesTouched"] = []
        changed = True
        prev_triggers = set(gate_state.get("triggers") or [])
        if (prev_phase in ("advisory", "drain", "freeze") and prev_triggers
                and prev_triggers <= {"transcript_warning", "transcript_bytes", "banner"}):
            if not stage2_hits and not host_banner_is_new:
                prev_phase = "advisory" if advisory_hits else "normal"
                gate_state["phase"] = prev_phase
                gate_state["triggers"] = list(advisory_hits)
                gate_state["drainInvocationsRemaining"] = DEFAULT_DRAIN_INVOCATIONS
                gate_state["drainActionsRemaining"] = DEFAULT_DRAIN_ACTIONS
                gate_state["enteredDrainAtStep"] = None
                gate_state["enteredFreezeAtStep"] = None
                user_words.append_lifecycle_log(
                    HOOKS_DIR,
                    "HandoffGate:recalibrate",
                    conversation_id,
                    f"Recalibrated compact transcript size -> phase={prev_phase} ({_format_telemetry_brief(tel)})",
                )

    if bool(tel.get("isGoalActive")) != bool(gate_state.get("isGoalActive")):
        gate_state["isGoalActive"] = bool(tel.get("isGoalActive"))
        changed = True

    # 1. 检查显式 R8 暂缓指令（绑定步骤号+内容哈希，防止同轮重复消费且允许后续轮次再次输入相同暂缓词）
    if control_token and gate_state.get("overriddenTurnHash") not in (control_token, control_hash) and intent == "override":
        gate_state["phase"] = "overridden"
        gate_state["overriddenTurnHash"] = control_token
        gate_state["overrideUntilCheckpoint"] = int(tel["cpCount"]) + OVERRIDE_SNOOZE_CPS
        gate_state["overrideUntilStep"] = int(tel["latestStep"]) + OVERRIDE_SNOOZE_STEPS
        gate_state["drainInvocationsRemaining"] = DEFAULT_DRAIN_INVOCATIONS
        gate_state["drainActionsRemaining"] = DEFAULT_DRAIN_ACTIONS
        gate_state["bannerTriggerHostStep"] = max(gate_state["bannerTriggerHostStep"], host_banner_step)
        user_words.append_lifecycle_log(
            HOOKS_DIR,
            "HandoffGate:override",
            conversation_id,
            f"R8 explicit user override accepted until cp>={gate_state['overrideUntilCheckpoint']} step>={gate_state['overrideUntilStep']}",
        )
        return True

    # 2. 暂缓期内，新交接指令或新宿主告警优先。
    if prev_phase == "overridden":
        snooze_expired = (
            int(tel["cpCount"]) >= int(gate_state.get("overrideUntilCheckpoint", -1))
            or int(tel["latestStep"]) >= int(gate_state.get("overrideUntilStep", -1))
        )
        new_banner_in_fresh_turn = (
            turn_token
            and gate_state.get("overriddenTurnHash") not in (turn_token, turn_hash)
            and gate_state.get("bannerTriggerTurnHash") not in (turn_token, turn_hash)
            and "user_banner_report" in stage2_hits
        )
        if not (snooze_expired or new_banner_in_fresh_turn or new_request or host_banner_is_new):
            return changed
        # 宽限到期或新报错出现，允许重新进入阈值判定
        gate_state["phase"] = "normal"
        prev_phase = "normal"
        changed = True

    if new_request:
        gate_state["handoffRequestTurnHash"] = control_token
        stage2_hits.append("user_request")
        changed = True
    if host_banner_is_new:
        gate_state["bannerTriggerHostStep"] = host_banner_step
        stage2_hits.append("host_banner")
        changed = True

    # 3. 阶段跃迁与单调冻结演进
    if prev_phase == "freeze":
        for tr in stage2_hits:
            if tr not in gate_state["triggers"]:
                gate_state["triggers"].append(tr)
                changed = True
        return changed

    if prev_phase == "drain":
        for tr in stage2_hits:
            if tr not in gate_state["triggers"]:
                gate_state["triggers"].append(tr)
                changed = True
        if decrement_invocation:
            rem = max(0, int(gate_state.get("drainInvocationsRemaining", 0)) - 1)
            gate_state["drainInvocationsRemaining"] = rem
            changed = True
            if rem <= 0 or int(gate_state.get("drainActionsRemaining", 0)) <= 0:
                gate_state["phase"] = "freeze"
                gate_state["enteredFreezeAtStep"] = tel["latestStep"]
                user_words.append_lifecycle_log(
                    HOOKS_DIR,
                    "HandoffGate:freeze",
                    conversation_id,
                    f"Transitioned drain -> freeze at step={tel['latestStep']} (invocation budget exhausted)",
                )
        return changed

    # 当前为 normal 或 advisory：判定是否升级到 drain 或 advisory
    if stage2_hits:
        gate_state["phase"] = "drain"
        gate_state["triggers"] = list(dict.fromkeys(stage2_hits))
        gate_state["enteredDrainAtStep"] = tel["latestStep"]
        gate_state["handoffFilesTouched"] = []
        gate_state["handoffFileBaselines"] = {}
        gate_state["drainInvocationsRemaining"] = DEFAULT_DRAIN_INVOCATIONS
        gate_state["drainActionsRemaining"] = DEFAULT_DRAIN_ACTIONS
        if "user_banner_report" in stage2_hits or "host_banner" in stage2_hits:
            gate_state["bannerDetectedAtStep"] = tel["latestStep"]
            gate_state["bannerTriggerTurnHash"] = turn_token
        user_words.append_lifecycle_log(
            HOOKS_DIR,
            "HandoffGate:drain",
            conversation_id,
            f"Entered Stage-2 drain (triggers={gate_state['triggers']}, {_format_telemetry_brief(tel)})",
        )
        return True

    if advisory_hits and prev_phase == "normal":
        gate_state["phase"] = "advisory"
        gate_state["triggers"] = list(dict.fromkeys(advisory_hits))
        user_words.append_lifecycle_log(
            HOOKS_DIR,
            "HandoffGate:advisory",
            conversation_id,
            f"Entered Stage-1 advisory (triggers={gate_state['triggers']}, {_format_telemetry_brief(tel)})",
        )
        return True

    if advisory_hits and prev_phase == "advisory":
        for tr in advisory_hits:
            if tr not in gate_state["triggers"]:
                gate_state["triggers"].append(tr)
                changed = True

    return changed


def evaluate_pre_invocation_locked(
    conversation_id: str,
    event_name: str,
    state_dir: Path,
    compaction_state: Dict[str, Any],
    transcript_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """评估遥测并更新门禁。调用方在同一会话锁内提交压缩状态。"""
    gate_state = load_gate_state(state_dir, conversation_id)
    tel = _merge_telemetry(compaction_state, gate_state, transcript_path=transcript_path)
    user_text = tel["latestUserInputPreview"]

    is_pre_inv = event_name == "PreInvocation"
    changed = _refresh_phase_from_telemetry(
        gate_state,
        tel,
        user_text,
        conversation_id,
        decrement_invocation=is_pre_inv,
    )

    phase = str(gate_state.get("phase") or "normal")
    advisory_notice: Optional[str] = None
    mandate_message: Optional[str] = None

    if phase == "advisory":
        cp_now = int(tel["cpCount"])
        img_milestone = int(tel["imageCallCount"]) // 25
        curr_triggers = list(gate_state.get("triggers") or [])
        prev_adv_triggers = set(gate_state.get("lastAdvisoryTriggers") or [])
        if (
            cp_now > int(gate_state.get("lastAdvisoryCp", -1))
            or img_milestone > int(gate_state.get("lastAdvisoryImageMilestone", 0))
            or bool(set(curr_triggers) - prev_adv_triggers)
        ):
            gate_state["lastAdvisoryCp"] = cp_now
            gate_state["lastAdvisoryImageMilestone"] = img_milestone
            gate_state["lastAdvisoryTriggers"] = curr_triggers
            advisory_notice = _build_advisory_message(tel, curr_triggers)
            changed = True
    elif phase == "drain":
        mandate_message = _build_drain_message(gate_state, tel)
    elif phase == "freeze":
        mandate_message = _build_freeze_message(gate_state, tel)

    if changed:
        save_gate_state(state_dir, conversation_id, gate_state)

    return {
        "phase": phase,
        "advisoryNotice": advisory_notice,
        "mandateMessage": mandate_message,
        "telemetry": tel,
    }


def _is_handoff_target(
    raw_path: str,
    conversation_id: str,
    cwd: Optional[str],
    workspace_paths: Optional[List[str]],
) -> bool:
    if not raw_path or not raw_path.strip():
        return False
    target = _resolve_candidate_path(raw_path, cwd=cwd, workspace_paths=workspace_paths)
    norm = str(target).replace("\\", "/")
    norm_low = norm.lower()
    name = target.name

    # 允许 Antigravity 当前对话 brain Artifact 目录写交接文档（排除系统文件与媒体上传目录）
    if conversation_id and f"/brain/{conversation_id.lower()}/" in norm_low:
        if "/.system_generated/" in norm_low or "/.user_uploaded/" in norm_low:
            return False
        if name.endswith("_user_words.md"):
            return False
        if target.suffix.lower() in {".md", ".txt", ".json"}:
            return True

    # 项目目录内交接文件仅放行纯文本文档后缀（严禁将 src/resume_solver.py 或 handoff/solver.cpp 等源码误判为交接文档）
    if target.suffix.lower() not in {".md", ".markdown", ".txt", ".rst"}:
        return False

    if "/handoff/" in norm_low or norm_low.endswith("/handoff"):
        return True
    if HANDOFF_FILENAME_RE.search(name):
        return True
    return False


def _split_shell_clauses(cmd_str: str) -> List[str]:
    """按顶层分隔符 (; / && / || / | / 换行) 拆分 Shell 子句，尊重引号嵌套"""
    clauses: List[str] = []
    buf: List[str] = []
    quote_ch: Optional[str] = None
    i = 0
    n = len(cmd_str)
    while i < n:
        ch = cmd_str[i]
        if quote_ch:
            buf.append(ch)
            if ch == "\\" and quote_ch == '"' and i + 1 < n:
                buf.append(cmd_str[i + 1])
                i += 2
                continue
            if ch == quote_ch:
                quote_ch = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote_ch = ch
            buf.append(ch)
            i += 1
            continue

        if ch in ("\n", ";"):
            seg = "".join(buf).strip()
            if seg:
                clauses.append(seg)
            buf = []
            i += 1
            continue

        if ch in ("&", "|"):
            if i + 1 < n and cmd_str[i + 1] == ch:
                seg = "".join(buf).strip()
                if seg:
                    clauses.append(seg)
                buf = []
                i += 2
                continue
            if ch == "|":
                seg = "".join(buf).strip()
                if seg:
                    clauses.append(seg)
                buf = []
                i += 1
                continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        clauses.append(tail)
    return clauses


def _safe_split_clause_tokens(clause: str) -> List[str]:
    try:
        return shlex.split(clause, posix=(os.name != "nt"))
    except ValueError:
        return clause.split()


def _strip_token_quotes(tok: str) -> str:
    s = tok.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        inner = s[1:-1]
        return inner.replace('\\"', '"') if s[0] == '"' else inner.replace("''", "'")
    return s


def _unwrap_clause_or_shell(tokens: List[str]) -> Tuple[List[str], Optional[str]]:
    """剥离前导 env/timeout/wsl/& 等包装；若遇到 bash -lc / powershell -Command 等壳包装则返回内层脚本供递归分类。"""
    idx = 0
    n = len(tokens)
    while idx < n:
        head_raw = _strip_token_quotes(tokens[idx])
        head_name = Path(head_raw.replace("\\", "/")).name.lower()
        if head_raw == "&" or head_name in ("sudo", "command", "env"):
            idx += 1
            continue
        if "=" in head_raw and not head_raw.startswith("-") and "/" not in head_raw and "\\" not in head_raw:
            idx += 1
            continue
        if head_name in ("timeout", "timeout.exe"):
            idx += 1
            while idx < n and _strip_token_quotes(tokens[idx]).startswith("-"):
                flag_t = _strip_token_quotes(tokens[idx])
                idx += 1
                if flag_t in ("-k", "--kill-after", "-s", "--signal") and idx < n:
                    idx += 1
            if idx < n:
                idx += 1
            continue
        if head_name in SUBSYSTEM_WRAPPERS:
            idx += 1
            while idx < n and _strip_token_quotes(tokens[idx]).startswith("-"):
                flag = _strip_token_quotes(tokens[idx])
                idx += 1
                if flag in ("--", "-e", "--exec"):
                    break
                if flag in ("-d", "--distribution", "-u", "--user", "--cd") and idx < n:
                    idx += 1
            continue
        if head_name in SHELL_WRAPPERS:
            idx += 1
            while idx < n:
                fl = _strip_token_quotes(tokens[idx]).lower()
                if fl in ("-executionpolicy", "-ep", "-windowstyle", "-w", "-inputformat", "-outputformat") and idx + 1 < n:
                    idx += 2
                    continue
                if fl in ("-c", "/c", "-command") or (
                    head_name in ("bash", "sh", "zsh") and re.fullmatch(r"-[a-z]*c", fl)
                ):
                    idx += 1
                    if idx >= n:
                        return [], ""
                    if n - idx == 1:
                        return [], _strip_token_quotes(tokens[idx])
                    rest = [_strip_token_quotes(tokens[idx])] + list(tokens[idx + 1 :])
                    return [], " ".join(rest)
                if fl.startswith("-") or (head_name in ("cmd", "cmd.exe") and fl.startswith("/")):
                    idx += 1
                    continue
                break
            continue
        break
    return tokens[idx:], None


def _unwrap_clause_tokens(tokens: List[str]) -> List[str]:
    unwrapped, inner = _unwrap_clause_or_shell(tokens)
    return _safe_split_clause_tokens(inner) if inner is not None else unwrapped


def _git_subcommand(tokens: List[str]) -> Tuple[Optional[str], List[str]]:
    """跳过 git 全局选项（如 -C dir, --no-pager）提取真实子命令名"""
    i = 1
    n = len(tokens)
    value_opts = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    while i < n:
        tok = _strip_token_quotes(tokens[i])
        if tok in value_opts:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok.lower(), [_strip_token_quotes(t) for t in tokens[i + 1 :]]
    return None, []


def _strip_quoted_segments(clause: str) -> str:
    """屏蔽引号内字符串文本，防止 commit 信息中的 `>` / `>>` 等字符触发重定向误判"""
    out: List[str] = []
    quote_ch: Optional[str] = None
    i = 0
    n = len(clause)
    while i < n:
        ch = clause[i]
        if quote_ch:
            if ch == "\\" and quote_ch == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote_ch:
                quote_ch = None
            out.append(" ")
            i += 1
            continue
        if ch in ("'", '"'):
            quote_ch = ch
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_cmd_substitutions(clause: str) -> List[str]:
    """提取非单引号区间的 $(...) 与成对反引号命令替换表达式，防 echo "$(git push)" 类隐蔽执行绕过"""
    subs: List[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(clause)
    while i < n:
        ch = clause[i]
        if ch == "\\" and not in_single and i + 1 < n:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not in_single:
            if ch == "$" and i + 1 < n and clause[i + 1] == "(":
                depth = 1
                j = i + 2
                s_q = False
                d_q = False
                while j < n and depth > 0:
                    c2 = clause[j]
                    if c2 == "\\" and not s_q and j + 1 < n:
                        j += 2
                        continue
                    if c2 == "'" and not d_q:
                        s_q = not s_q
                    elif c2 == '"' and not s_q:
                        d_q = not d_q
                    elif not s_q and not d_q:
                        if c2 == "(":
                            depth += 1
                        elif c2 == ")":
                            depth -= 1
                    j += 1
                if depth == 0:
                    inner = clause[i + 2 : j - 1].strip()
                    if inner:
                        subs.append(inner)
                    i = j
                    continue
            if ch == "`" and i + 1 < n:
                j = clause.find("`", i + 1)
                if j != -1:
                    inner = clause[i + 1 : j].strip()
                    if inner and not inner.startswith(("r", "n", "t", "0")):
                        subs.append(inner)
                    i = j + 1
                    continue
        i += 1
    return subs


def _classify_run_command(cmd_str: str, is_daemon: bool = False, _depth: int = 0) -> str:
    """
    将 run_command 分类为：
    - blocked_always: cae_harness 新派工、git push、IsDaemon 常驻进程
    - readonly_inspect: 全部子句为纯只读核对（含 git status/diff/log/show 等）
    - git_local_commit: 含 git add/commit 且无外部写操作与 push
    - mutating_other: 其他会改动/运行测试/脚本的命令
    """
    if is_daemon:
        return "blocked_always"
    if not cmd_str or not cmd_str.strip():
        return "mutating_other"
    if _depth > 3:
        return "mutating_other"

    has_git_commit_or_add = False
    has_mutating = False

    clauses = _split_shell_clauses(cmd_str)
    for clause in clauses:
        for sub_expr in _extract_cmd_substitutions(clause):
            sub_expr_kind = _classify_run_command(sub_expr, is_daemon=False, _depth=_depth + 1)
            if sub_expr_kind == "blocked_always":
                return "blocked_always"
            if sub_expr_kind == "mutating_other":
                has_mutating = True
            elif sub_expr_kind == "git_local_commit":
                has_git_commit_or_add = True

        unquoted = _strip_quoted_segments(clause)
        stripped_redirs = re.sub(r"\d?>&1|\d?>\s*/dev/null", "", unquoted)
        has_redir = bool(re.search(r"(?:>>|>|\bout-file\b|\bset-content\b|\badd-content\b|\btee\b)", stripped_redirs, re.IGNORECASE))

        tokens, inner_script = _unwrap_clause_or_shell(_safe_split_clause_tokens(clause))
        if inner_script is not None:
            sub_kind = _classify_run_command(inner_script, is_daemon=False, _depth=_depth + 1)
            if sub_kind == "blocked_always":
                return "blocked_always"
            if sub_kind == "mutating_other" or has_redir:
                has_mutating = True
                continue
            if sub_kind == "git_local_commit":
                has_git_commit_or_add = True
                continue
            continue

        if not tokens:
            continue

        raw_head = _strip_token_quotes(tokens[0])
        head_file = Path(raw_head.replace("\\", "/")).name.lower()
        head = head_file[:-4] if head_file.endswith(".exe") else head_file
        if any(is_image_path(_strip_token_quotes(argument)) for argument in tokens[1:]) and head in (
            "cat", "type", "get-content", "gc", "head", "tail", "xxd", "display", "open", "start",
        ):
            return "blocked_always"

        if head_file == "cae_harness.py":
            sub_tokens = [_strip_token_quotes(st) for st in tokens[1:]]
            if (
                not has_redir
                and any(st in ("probe", "--help", "-h", "status") for st in sub_tokens)
                and not any(st in ("task", "audit", "gate") for st in sub_tokens)
            ):
                continue
            return "blocked_always"

        if head == "git":
            subcmd, subargs = _git_subcommand(tokens)
            if subcmd == "push":
                return "blocked_always"
            if subcmd in GIT_READONLY_SUBCMDS and not has_redir:
                continue
            if subcmd == "branch" and not any(f in subargs for f in ("-d", "-D", "-m", "-M", "-c", "-C")) and not has_redir:
                continue
            if subcmd in GIT_LOCAL_COMMIT_SUBCMDS and not has_redir:
                has_git_commit_or_add = True
                continue
            has_mutating = True
            continue

        if head in READONLY_BINARIES and not has_redir:
            find_unsafe = ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")
            if head == "find" and any(_strip_token_quotes(a) in find_unsafe for a in tokens[1:]):
                has_mutating = True
            else:
                continue

        if head == "py" or head.startswith("python"):
            py_args = tokens[1:]
            idx_arg = 0
            saw_inline_or_mod = False
            while idx_arg < len(py_args):
                tok_arg = _strip_token_quotes(py_args[idx_arg])
                if tok_arg in ("-c", "-m"):
                    saw_inline_or_mod = True
                    break
                if tok_arg in ("-X", "-W", "-Q", "--check-hash-based-pycs"):
                    idx_arg += 2
                    continue
                if tok_arg.startswith("-"):
                    idx_arg += 1
                    continue
                break
            if saw_inline_or_mod:
                if re.search(r"cae_harness", clause, re.IGNORECASE):
                    return "blocked_always"
                has_mutating = True
                continue
            if idx_arg < len(py_args):
                script_name = Path(_strip_token_quotes(py_args[idx_arg]).replace("\\", "/")).name.lower()
                if script_name == "cae_harness.py":
                    sub_tokens = [_strip_token_quotes(st) for st in py_args[idx_arg + 1 :]]
                    if (
                        not has_redir
                        and any(st in ("probe", "--help", "-h", "status") for st in sub_tokens)
                        and not any(st in ("task", "audit", "gate") for st in sub_tokens)
                    ):
                        continue
                    return "blocked_always"

        has_mutating = True

    if has_mutating:
        return "mutating_other"
    if has_git_commit_or_add:
        return "git_local_commit"
    return "readonly_inspect"


def check_pre_tool_use_locked(payload: Dict[str, Any], state_dir: Path,
                              *, boundary_blocked: bool = False) -> Optional[Dict[str, Any]]:
    """在同一会话锁中检查阶段并更新状态。"""
    conversation_id = str(payload.get("conversationId") or "").strip()
    if not conversation_id:
        return None

    compaction_state_file = state_dir / f"{conversation_id}_compaction.json"
    compaction_state = {}
    if compaction_state_file.is_file():
        try:
            compaction_state = json.loads(compaction_state_file.read_text(encoding="utf-8"))
            if not isinstance(compaction_state, dict) or compaction_state.get("conversationId") != conversation_id:
                raise ValueError("压缩状态格式或身份错误")
        except (ValueError, OSError) as exc:
            if not _gate_state_path(state_dir, conversation_id).exists():
                raise
            user_words.append_lifecycle_log(HOOKS_DIR, "PreToolUse:state_error", conversation_id,
                                            f"读取压缩状态失败（{exc}），沿用现有门禁阶段")
            compaction_state = {}
    if not compaction_state.get("hasExplicitUserInput", False) and not _gate_state_path(state_dir, conversation_id).exists():
        return None

    gate_state = load_gate_state(state_dir, conversation_id)
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    raw_tool_name = str(tool_call.get("name") or tool_call.get("tool_name") or "").strip()
    tool_name = AGY_TOOL_ALIASES.get(raw_tool_name, raw_tool_name)
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    cwd = args.get("Cwd") if tool_name == "run_command" else None
    cwd = cwd or payload.get("cwd")
    workspace_paths = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
    transcript_str = payload.get("transcriptPath")
    transcript_path = user_words.resolve_compact_transcript_path(Path(transcript_str).expanduser()) if transcript_str else None

    # 对 read_file 图片读取实时累计计数并检查是否当场突破 Stage-2
    is_image_read = False
    if tool_name == "read_file":
        file_arg = str(args.get("AbsolutePath") or args.get("Path") or args.get("path") or "").strip()
        if is_image_path(file_arg):
            is_image_read = True
            record_image_read(gate_state, file_arg, cwd=cwd, workspace_paths=workspace_paths, increment_calls=True)

    tel = _merge_telemetry(compaction_state, gate_state, transcript_path=transcript_path)
    if not compaction_state:
        tel["isGoalActive"] = gate_state["isGoalActive"]
    changed = _refresh_phase_from_telemetry(
        gate_state,
        tel,
        tel["latestUserInputPreview"],
        conversation_id,
        decrement_invocation=False,
    )

    phase = str(gate_state.get("phase") or "normal")
    if phase not in ("drain", "freeze"):
        if changed or is_image_read:
            save_gate_state(state_dir, conversation_id, gate_state)
        return None

    # 以下为 Stage-2 (drain / freeze) 精确工具门禁
    # 1. 绝不允许的派工与常驻/外推工具（drain 与 freeze 均阻断）
    if tool_name in DELEGATION_TOOLS or tool_name in MEDIA_TOOLS or tool_name.startswith("browser_"):
        save_gate_state(state_dir, conversation_id, gate_state)
        return {
            "decision": "deny",
            "reason": (
                f"【user-guard 长会话交接门禁 ({phase})】当前会话已进入交接停机收口期（{_format_telemetry_brief(tel)}），"
                    f"收口期禁用 `{tool_name}`。"
                "请仅完成本地原子收口并整理交接文档与新对话提示词后立即停机。"
            ),
        }

    # 2. manage_task / manage_subagents：允许查看或收尾后台任务（不扣减动作预算）
    if tool_name in AGY_INSPECTION_TOOLS:
        save_gate_state(state_dir, conversation_id, gate_state)
        return None

    # 3. read_file：禁止读图（防耗尽剩余配额），放行文本核对
    if tool_name == "read_file":
        save_gate_state(state_dir, conversation_id, gate_state)
        if is_image_read:
            return {
                "decision": "deny",
                "reason": (
                    f"【user-guard 长会话交接门禁 ({phase})】当前图片遥测（{_format_telemetry_brief(tel)}）。"
                    "收口期内禁止继续调用 `read_file` 看图，请改用文本摘要/脚本输出核对后完成交接。"
                ),
            }
        return None

    # 4. write_file / edit_file：交接文档始终放行；非交接修改在 drain 扣减预算，在 freeze 阻断
    if tool_name in ("write_file", "edit_file"):
        write_arg = str(args.get("Path") or args.get("path") or args.get("TargetFile") or args.get("file_path") or "").strip()
        if _is_handoff_target(write_arg, conversation_id, cwd, workspace_paths):
            if boundary_blocked:
                if changed:
                    save_gate_state(state_dir, conversation_id, gate_state)
                return None
            resolved_target = str(_resolve_candidate_path(write_arg, cwd=cwd, workspace_paths=workspace_paths))
            _record_handoff_baseline(gate_state, Path(resolved_target))
            save_gate_state(state_dir, conversation_id, gate_state)
            return None

        if phase == "freeze":
            save_gate_state(state_dir, conversation_id, gate_state)
            return {
                "decision": "deny",
                "reason": (
                    "【user-guard 长会话交接门禁 (freeze)】排空预算已用尽，当前已进入冻结期，禁止修改业务源码或非交接文件！"
                    "请仅编写交接文件、执行本地 `git add`/`git commit` 或直接输出交接总结与新对话提示词后停机。"
                ),
            }

        if boundary_blocked:
            if changed:
                save_gate_state(state_dir, conversation_id, gate_state)
            return None
        # drain 阶段扣减动作预算
        rem_actions = int(gate_state.get("drainActionsRemaining", 0))
        if rem_actions <= 0:
            gate_state["phase"] = "freeze"
            gate_state["enteredFreezeAtStep"] = tel["latestStep"]
            save_gate_state(state_dir, conversation_id, gate_state)
            return {
                "decision": "deny",
                "reason": (
                    "【user-guard 长会话交接门禁 (drain -> freeze)】排空动作预算（8 次）已用完，已转入冻结期！"
                    "禁止继续修改业务文件，请整理交接提示词并停机。"
                ),
            }
        if consume_drain_action(gate_state):
            gate_state["enteredFreezeAtStep"] = tel["latestStep"]
        save_gate_state(state_dir, conversation_id, gate_state)
        return None

    # 5. send_message：仅允许在 drain 消耗 1 次动作通知收尾，freeze 冻结阻断
    if tool_name == "send_message":
        if phase == "freeze" or int(gate_state.get("drainActionsRemaining", 0)) <= 0:
            gate_state["phase"] = "freeze"
            save_gate_state(state_dir, conversation_id, gate_state)
            return {
                "decision": "deny",
                "reason": "【user-guard 长会话交接门禁 (freeze)】已冻结，禁止再通过 `send_message` 唤醒或继续驱动子代理。请直接整理交接并停机。",
            }
        if consume_drain_action(gate_state):
            gate_state["enteredFreezeAtStep"] = tel["latestStep"]
        save_gate_state(state_dir, conversation_id, gate_state)
        return None

    # 6. run_command：分类管控（始终禁派工/push，始终放行只读与本地 git add/commit）
    if tool_name == "run_command":
        cmd_str = str(args.get("CommandLine") or args.get("command_line") or args.get("command") or "")
        is_daemon = bool(args.get("IsDaemon", False))
        cmd_kind = _classify_run_command(cmd_str, is_daemon=is_daemon)

        if cmd_kind == "blocked_always":
            save_gate_state(state_dir, conversation_id, gate_state)
            return {
                "decision": "deny",
                "reason": (
                    f"【user-guard 长会话交接门禁 ({phase})】收口期禁用派工、推送、读图和常驻进程。"
                    "仅允许本地只读核对、运行本地验收与本地 `git add`/`git commit`。"
                ),
            }

        if cmd_kind in ("readonly_inspect", "git_local_commit"):
            save_gate_state(state_dir, conversation_id, gate_state)
            return None

        if phase == "freeze":
            save_gate_state(state_dir, conversation_id, gate_state)
            return {
                "decision": "deny",
                "reason": (
                    "【user-guard 长会话交接门禁 (freeze)】已进入冻结期，禁止运行新的构建/批量脚本或修改命令！"
                    "当前仅允许只读检查命令（`git status/diff/log`）与本地 `git add`/`git commit`。"
                ),
            }

        if boundary_blocked:
            if changed:
                save_gate_state(state_dir, conversation_id, gate_state)
            return None
        rem_actions = int(gate_state.get("drainActionsRemaining", 0))
        if rem_actions <= 0:
            gate_state["phase"] = "freeze"
            gate_state["enteredFreezeAtStep"] = tel["latestStep"]
            save_gate_state(state_dir, conversation_id, gate_state)
            return {
                "decision": "deny",
                "reason": "【user-guard 长会话交接门禁 (drain -> freeze)】排空运行预算已耗尽，禁止再启动新命令，请完成交接停机。",
            }
        if consume_drain_action(gate_state):
            gate_state["enteredFreezeAtStep"] = tel["latestStep"]
        save_gate_state(state_dir, conversation_id, gate_state)
        return None

    if phase == "freeze" or int(gate_state["drainActionsRemaining"]) <= 0:
        gate_state["phase"] = "freeze"
        save_gate_state(state_dir, conversation_id, gate_state)
        return {"decision": "deny", "reason": "【user-guard 冻结】当前工具缺少可核实的只读或交接用途。"}
    if consume_drain_action(gate_state):
        gate_state["enteredFreezeAtStep"] = tel["latestStep"]
    save_gate_state(state_dir, conversation_id, gate_state)
    return None


MAX_HANDOFF_DOCUMENT_BYTES = 1024 * 1024


def _handoff_document_snapshot(path: Path, *, include_text: bool = False) -> Optional[Dict[str, Any]]:
    try:
        resolved = str(path.resolve())
        if not path.exists():
            return {"path": resolved, "sha256": ""}
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            data = stream.read(MAX_HANDOFF_DOCUMENT_BYTES + 1)
        if len(data) > MAX_HANDOFF_DOCUMENT_BYTES:
            return None
        snapshot = {"path": resolved, "sha256": hashlib.sha256(data).hexdigest()}
        if include_text:
            snapshot["text"] = data.decode("utf-8-sig")
        return snapshot
    except (OSError, UnicodeDecodeError, RuntimeError):
        return None


def _record_handoff_baseline(state: Dict[str, Any], path: Path) -> None:
    baselines = state.setdefault("handoffFileBaselines", {})
    key = str(path.absolute())
    if key not in baselines:
        snapshot = _handoff_document_snapshot(path)
        if snapshot is not None:
            baselines[key] = snapshot


def _has_valid_handoff_file(gate_state: Dict[str, Any]) -> bool:
    for raw_path, baseline in gate_state.get("handoffFileBaselines", {}).items():
        current = _handoff_document_snapshot(Path(raw_path), include_text=True)
        if (isinstance(baseline, dict) and current and current.get("path") == baseline.get("path")
                and current.get("sha256") != baseline.get("sha256")
                and _handoff_structure_present(current.get("text") or "")):
            return True
    return False


def _reply_has_handoff_prompt(reply_text: str) -> bool:
    lines = reply_text.strip().splitlines()
    plain_lines = list(user_words.markdown_unquoted_lines(reply_text.strip()))
    for idx, line in enumerate(plain_lines):
        heading, *body = re.split(r"[:：]", line, maxsplit=1)
        if not PROMPT_MARKER_RE.fullmatch(heading.strip("#*【】[] ")):
            continue
        if body and len(body[0].strip()) >= 15:
            return True
        for pos in range(idx + 1, len(lines)):
            if not lines[pos].strip():
                continue
            fence = user_words.MARKDOWN_FENCE_RE.match(lines[pos])
            if fence:
                content = []
                for body_line in lines[pos + 1:]:
                    ending = user_words.MARKDOWN_FENCE_RE.match(body_line)
                    if ending and ending[1][0] == fence[1][0] and len(ending[1]) >= len(fence[1]) and not ending[2].strip():
                        return len("\n".join(content).strip()) >= 15
                    content.append(body_line)
                return False
            return len(plain_lines[pos]) >= 15 and not lines[pos].lstrip().startswith(("#", ">"))
    return False


def _handoff_structure_present(text: str) -> bool:
    """核对交接文档结构，内容是否属实仍需对照源证据。"""
    prose = "\n".join(user_words.markdown_unquoted_lines(text))
    return (_reply_has_handoff_prompt(text)
            and bool(re.search(r"已验证|已完成|进展|verified|completed", prose, re.IGNORECASE))
            and bool(re.search(r"未完成|待办|下一步|剩余|remaining|next steps", prose, re.IGNORECASE))
            and bool(re.search(r"git|提交|工作区|workspace", prose, re.IGNORECASE)))


def check_stop_handoff_locked(
    payload: Dict[str, Any],
    state_dir: Path,
    turn_key: str,
    latest_reply: str,
    *,
    goal_active: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """检查当前交接结构，每个用户回合最多要求补正一次。"""
    conversation_id = str(payload.get("conversationId") or "").strip()
    if not conversation_id:
        return None

    gate_state = load_gate_state(state_dir, conversation_id)
    phase = str(gate_state.get("phase") or "normal")
    if phase not in ("drain", "freeze"):
        return None

    if turn_key == "turn_0_initial":
        comp_file = state_dir / f"{conversation_id}_compaction.json"
        if comp_file.exists():
            try:
                comp = json.loads(comp_file.read_text(encoding="utf-8"))
                if isinstance(comp, dict):
                    u_step = int(comp.get("latestUserStepIndex", -1))
                    u_hash = str(comp.get("latestUserInputHash") or "")
                    if u_step >= 0 and u_hash:
                        turn_key = f"turn_{u_step}_{u_hash[:12]}"
            except Exception:
                pass

    blocked_keys: List[str] = gate_state.setdefault("stopGateBlockedTurnKeys", [])
    if turn_key in blocked_keys:
        return {"decision": "allow"}

    missing_parts: List[str] = []
    has_file = _has_valid_handoff_file(gate_state)
    has_prompt_inline = _handoff_structure_present(latest_reply)
    if not has_file and not has_prompt_inline:
        missing_parts.append(
            "尚未落盘非空交接文档且最终回复中缺少可直接复制粘贴到新对话的完整【接续提示词】（含已验证进度、Git 状态与未尽事项）"
        )

    requires_cancel = bool(gate_state.get("isGoalActive")) if goal_active is None else goal_active
    if requires_cancel and not user_words.has_goal_cancel_marker(latest_reply):
        missing_parts.append(
            "检测到当前会话运行了 `/goal` 自主循环，回复末尾必须附带 `<!-- GOAL_CANCELLED -->` 才能安全停机"
        )

    if not missing_parts:
        user_words.append_lifecycle_log(
            HOOKS_DIR,
            "Stop:handoff_gate",
            conversation_id,
            f"ALLOWED clean handoff stop in phase={phase} ({turn_key})",
        )
        return {"decision": "allow"}

    blocked_keys.append(turn_key)
    save_gate_state(state_dir, conversation_id, gate_state)
    user_words.append_lifecycle_log(
        HOOKS_DIR,
        "Stop:handoff_gate",
        conversation_id,
        f"BLOCKED incomplete handoff in phase={phase} ({turn_key}): {'; '.join(missing_parts)}",
    )
    return {
        "decision": "continue",
        "reason": (
            "【user-guard 交接停机门禁验收未通过】"
            + "；".join(missing_parts)
            + "。请补全上述交接要素后直接输出停机回复。"
        ),
    }


# Codex 使用官方事件字段；不读取 Antigravity 的检查点、图片体积或目标终止标记。
CODEX_EVENTS = {"SessionStart", "PostCompact", "UserPromptSubmit", "PreToolUse", "Stop"}
CODEX_DRAIN_ACTIONS = 4  # 显式严格模式的收尾动作预算，不表示模型容量。
CODEX_HANDOFF_REQUEST_RE = re.compile(
    r"^(?:/handoff|(?:请)?(?:立即)?(?:整理|准备|完成|做好|开始)交接(?:并|后)(?:停机|停止|结束当前对话))[。！!\s]*$",
    re.IGNORECASE,
)
CODEX_OVERRIDE_REQUEST_RE = re.compile(
    r"^(?:请)?" + OVERRIDE_COMMAND
    + r"(?:[，,；;、\s]+(?:请)?" + OVERRIDE_COMMAND + r")*[。.!！\s]*$",
    re.IGNORECASE,
)
CODEX_READ_TOOLS = {
    "read_file", "list_dir", "list_directory", "search_files", "get_goal",
    "list_agents", "wait_agent", "interrupt_agent", "wait", "request_user_input",
    "request_user_input_async", "list_mcp_resources", "read_mcp_resource",
}
CODEX_DELEGATION_TOOLS = {"spawn_agent", "agent", "followup_task", "send_message", "create_thread"}


def _codex_notice(phase: str, count: int, mode: str) -> str:
    prefix = f"【Codex 长会话交接：{phase}】已登记的压缩次数下界为 {count}。模式 {mode}。"
    if phase == "overridden":
        return prefix + "已按用户明确指令继续；宽限到其后第 3 个新压缩事件，再按已配置策略评估。"
    if phase in ("drain", "freeze"):
        return (prefix + "请收尾当前小步，核对用户原话与最新裁定，整理已验证进展、Git/工作区状态、"
                "未完成事项及可复制的新对话提示词。结束当前回合不等于取消目标循环或终止后台任务；"
                "须如实登记其状态，不得把未完成目标标成完成。"
                + ("当前仅提醒，不阻断工具。" if mode == "advisory" else "暂停新派工和推送；冻结后仅允许核对与交接。"))
    return prefix + "先核对压缩恢复的原话与当前状态，再继续工作；本提醒不表示上下文已失效。"


def _codex_context(event: str, message: str) -> Dict[str, Any]:
    if event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}
    return {"systemMessage": message}


def _scan_codex_compactions(transcript: Path, ledger: Dict[str, Any]) -> Optional[str]:
    """按轨迹文件身份与游标对账，保留可见记录及可确认的回合关联。"""
    path_key = _path_key(transcript)
    records = ledger["records"]
    try:
        with transcript.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            identity = [stat.st_dev, stat.st_ino]
            cursor = ledger["files"].get(path_key, {})
            offset = cursor.get("offset", 0)
            if type(offset) is not int or offset < 0:
                raise ValueError("压缩轨迹游标格式错误")
            stream.seek(max(0, offset - 128))
            anchor = hashlib.sha256(stream.read(min(128, offset))).hexdigest()
            if (cursor.get("identity") != identity or offset > stat.st_size or anchor != cursor.get("anchor")):
                cursor = {"identity": identity, "activeTurn": "", "pendingRecord": "", "occurrences": {}}
                offset = 0
            stream.seek(offset)
            while stream.tell() < stat.st_size:
                row_offset = stream.tell()
                raw = stream.readline(stat.st_size - row_offset)
                if not raw.endswith(b"\n"):
                    stream.seek(row_offset)
                    break
                pending = cursor["pendingRecord"]
                cursor["pendingRecord"] = ""
                if not any(token in raw for token in (b'"compacted"', b'"turn_context"', b'"task_started"',
                                                      b'"turn_started"', b'"task_complete"', b'"turn_complete"',
                                                      b'"turn_aborted"')):
                    continue
                try:
                    entry = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    cursor["unreadable"] = True
                    continue
                if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
                    continue
                kind = entry.get("type")
                fields = entry["payload"]
                turn = fields.get("turn_id")
                turn = turn if isinstance(turn, str) else ""
                if kind == "event_msg":
                    if fields.get("type") in ("task_started", "turn_started"):
                        cursor["activeTurn"] = turn
                    elif fields.get("type") in ("task_complete", "turn_complete", "turn_aborted"):
                        cursor["activeTurn"] = ""
                elif kind == "turn_context" and turn:
                    if pending:
                        records[pending].update(turn=turn, turnSource="following_turn_context")
                    if not cursor["activeTurn"]:
                        cursor["activeTurn"] = turn
                elif kind == "compacted":
                    window_id = fields.get("window_id")
                    if isinstance(window_id, str) and window_id:
                        key = f"window:{window_id}"
                    else:
                        # 没有宿主记录 ID 时，区分同一轨迹内相同内容的不同位置，镜像分段按出现顺序去重。
                        digest = hashlib.sha256(raw.strip()).hexdigest()
                        occurrence = cursor["occurrences"].get(digest, 0) + 1
                        cursor["occurrences"][digest] = occurrence
                        key = f"record:{digest}:{occurrence}"
                    active_turn = cursor["activeTurn"]
                    rec = records.setdefault(key, {"path": str(transcript), "offset": row_offset, "turn": "",
                                                   "turnSource": "unknown"})
                    if turn:
                        rec.update(turn=turn, turnSource="record")
                    else:
                        if active_turn and not rec["turn"]:
                            rec.update(turn=active_turn, turnSource="active_turn")
                        if rec["turnSource"] != "record":
                            cursor["pendingRecord"] = key

            offset = stream.tell()
            stream.seek(max(0, offset - 128))
            anchor = hashlib.sha256(stream.read(min(128, offset))).hexdigest()
            cursor.update(offset=offset, anchor=anchor)
            ledger["files"][path_key] = cursor
            if cursor.get("unreadable"):
                return "轨迹中存在不可解析的相关记录"
            if offset < stat.st_size:
                return "轨迹末尾记录待写完"
    except OSError as exc:
        return f"压缩轨迹读取失败（{exc}）"
    return None


def _reconcile_codex_compactions(payload: Dict[str, Any], transcript: Path, state: Dict[str, Any]) -> None:
    ledger = state.get("compactionLedger")
    if ledger is None:
        ledger = {"files": {}, "records": {}, "observations": {}}
        for key in state.get("compactionKeys", []):
            if isinstance(key, str) and key.startswith("turn:"):
                turn, _, trigger = key[5:].rpartition(":")
                if turn:
                    ledger["observations"][key] = {"turn": turn, "trigger": trigger, "source": "legacy_hook"}
        state["compactionLedger"] = ledger
        state.pop("compactionKeys", None)
    if not isinstance(ledger, dict) or any(not isinstance(ledger.get(key), dict)
                                          for key in ("files", "records", "observations")):
        raise ValueError("压缩对账状态格式错误，保留原文件")

    paths = [transcript]
    issues: List[str] = []
    if payload["hook_event_name"] == "SessionStart":
        segments, _, issues = user_words.discover_codex_session_segments(str(transcript), state["conversationId"])
        paths = segments or paths
    for path in paths:
        issue = _scan_codex_compactions(path, ledger)
        if issue:
            issues.append(issue)
    if payload["hook_event_name"] == "PostCompact":
        turn = payload.get("turn_id")
        if isinstance(turn, str) and turn:
            trigger = payload.get("trigger")
            trigger = trigger if trigger in ("manual", "auto") else "unknown"
            ledger["observations"].setdefault(f"turn:{turn}:{trigger}", {"turn": turn, "trigger": trigger,
                                                                          "source": "post_compact"})
        else:
            issues.append("PostCompact 未提供回合标识，无法按回合登记")

    verified_turns = set()
    unassigned = 0
    for record in ledger["records"].values():
        if record.get("turn") and record.get("turnSource") == "record":
            verified_turns.add(record["turn"])
        else:
            unassigned += 1
    observed_turns = {observation["turn"] for observation in ledger["observations"].values()}
    unmatched = observed_turns.difference(verified_turns)
    # 邻接记录只提供归属线索。它可能来自上一回合或恢复快照，不能证明与 hook 事件相互独立。
    extra = max(0, len(unmatched) - unassigned)
    record_count = len(ledger["records"])
    if unmatched and unassigned:
        issues.append("部分压缩记录未提供自身回合标识，与 PostCompact 观察可能重合")
    state["compactionCount"] = record_count + extra
    state["compactionRecordCount"] = record_count
    state["compactionFallbackCount"] = extra
    state["compactionIssues"] = issues
    state["compactionIdentity"] = ("mixed" if record_count and extra else "rollout_record" if record_count
                                  else "turn_fallback" if extra else "unknown")


def _codex_handoff_patch_paths(command: str, cwd: str) -> List[Path]:
    """获取项目内的交接补丁目标，移动源和目标均须通过路径核验。"""
    edits = re.findall(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$", command, re.MULTILINE)
    if not edits or not cwd:
        return []
    try:
        base = Path(cwd).resolve()
        targets: List[Path] = []
        for kind, raw in edits:
            path = Path(raw.strip())
            path = path if path.is_absolute() else base / path
            resolved = path.resolve()
            if (not path.is_relative_to(base) or not resolved.is_relative_to(base)
                    or resolved.suffix.lower() not in (".md", ".txt", ".rst")
                    or not HANDOFF_FILENAME_RE.search(resolved.name)):
                return []
            if kind != "Delete File":
                targets.append(path)
        return targets
    except (OSError, RuntimeError):
        return []


def _codex_tool_kind(payload: Dict[str, Any]) -> Tuple[str, List[Path]]:
    raw_name = str(payload.get("tool_name") or "")
    name = re.split(r"[.:]|__", raw_name)[-1].lower()
    raw_input = payload.get("tool_input")
    args = raw_input if isinstance(raw_input, dict) else {}
    if name in CODEX_DELEGATION_TOOLS or name in ("view_image", "imagegen"):
        return "blocked_always", []
    if name in CODEX_READ_TOOLS:
        return "readonly_inspect", []
    if name in ("bash", "exec_command", "run_command"):
        return _classify_run_command(str(args.get("command") or args.get("cmd") or args.get("CommandLine") or "")), []
    if name in ("apply_patch", "edit", "write"):
        command = str(args.get("command") or args.get("patch") or args.get("patch_text") or
                      (raw_input if isinstance(raw_input, str) else ""))
        targets = _codex_handoff_patch_paths(command, str(payload.get("cwd") or ""))
        if targets:
            return "handoff_document", targets
    # 未识别工具不赋予只读豁免；严格模式 drain 中计入预算，freeze 中拒绝。
    return "mutating_other", []


def handle_codex_handoff(payload: Dict[str, Any], state_dir: Path, mode: str = "advisory",
                         compact_limit: int = 0) -> Dict[str, Any]:
    """Codex 原生协议入口。advisory 永不阻断；enforce 只采用显式策略。"""
    event = str(payload.get("hook_event_name") or "")
    if event not in CODEX_EVENTS or mode == "off":
        return {}
    if mode not in ("advisory", "enforce") or isinstance(compact_limit, bool) or compact_limit < 0:
        raise ValueError("无效的 Codex 交接策略")
    sid, transcript, identity_error, skip = user_words.codex_hook_session(payload)
    if skip:
        return {}
    if transcript is None:
        return {"systemMessage": f"Codex 长会话门禁未更新。{identity_error}。"}
    state_dir = Path(state_dir)
    with user_words.session_state_lock(state_dir, sid):
        state_path = _gate_state_path(state_dir, sid)
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or state.get("client") != "codex" or state.get("version") != 1
                    or state.get("conversationId") != sid or not isinstance(state.get("observedEvents"), list)
                    or state.get("phase") not in ("normal", "advisory", "drain", "freeze", "overridden")):
                raise ValueError("Codex 门禁状态格式不兼容，保留原文件")
        else:
            state = {"client": "codex", "version": 1, "conversationId": sid, "phase": "normal",
                     "compactionKeys": [], "compactionCount": 0, "observedEvents": [],
                     "drainActionsRemaining": CODEX_DRAIN_ACTIONS, "overrideUntilCount": -1,
                     "lastUserEvent": "", "stopCorrectionRequested": False, "handoffRecorded": False}
        if event not in state["observedEvents"]:
            state["observedEvents"].append(event)
        state.update(mode=mode, compactLimit=compact_limit, lastModel=str(payload.get("model") or "unknown"))
        _reconcile_codex_compactions(payload, transcript, state)
        if state["phase"] == "normal" and (event == "PostCompact" or state["compactionCount"]):
            state["phase"] = "advisory"
        count = state["compactionCount"]
        if state["phase"] == "overridden" and count >= state["overrideUntilCount"]:
            state["phase"] = "advisory"
        enter_drain = (mode == "enforce" and compact_limit > 0 and count >= compact_limit
                       and state["phase"] in ("normal", "advisory"))
        user_event_is_new = False
        prompt = str(payload.get("prompt") or "").strip() if event == "UserPromptSubmit" else ""
        if prompt and not payload.get("stop_hook_active"):
            turn = str(payload.get("turn_id") or "")
            user_key = turn + ":" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            user_event_is_new = user_key != state["lastUserEvent"]
            if user_event_is_new:
                state["lastUserEvent"] = user_key
                # 只处理短而明确的用户指令，引用长文中的相同短语不改变门禁。
                if len(prompt) <= 160 and CODEX_OVERRIDE_REQUEST_RE.fullmatch(prompt):
                    state.update(phase="overridden", overrideUntilCount=count + OVERRIDE_SNOOZE_CPS,
                                 stopCorrectionRequested=False, handoffRecorded=False, toolDecisions={},
                                 handoffFileBaselines={})
                    enter_drain = False
                elif CODEX_HANDOFF_REQUEST_RE.fullmatch(prompt):
                    enter_drain = state["phase"] not in ("drain", "freeze")
        if enter_drain:
            state.update(phase="drain", drainActionsRemaining=CODEX_DRAIN_ACTIONS,
                         stopCorrectionRequested=False, handoffRecorded=False, toolDecisions={},
                         handoffFileBaselines={})
        phase = state["phase"]
        notice = _codex_notice(phase, count, mode)
        if state["compactionIssues"]:
            notice += " 压缩轨迹对账存在缺口，次数按当前可见证据保守计算。"
        response: Dict[str, Any] = {}
        if event in ("PostCompact", "SessionStart"):
            if event == "PostCompact" or payload.get("source") == "compact" or phase in ("drain", "freeze", "overridden"):
                response = _codex_context(event, notice)
        elif event == "UserPromptSubmit" and user_event_is_new and (phase in ("drain", "freeze", "overridden")):
            response = _codex_context(event, notice)
        elif event == "PreToolUse" and mode == "enforce" and phase in ("drain", "freeze"):
            tool_id = str(payload.get("tool_use_id") or "")
            previous = state.setdefault("toolDecisions", {})
            if tool_id and tool_id in previous:
                save_gate_state(state_dir, sid, state)
                return previous[tool_id]
            kind, targets = _codex_tool_kind(payload)
            if kind == "blocked_always" or (phase == "freeze" and kind == "mutating_other"):
                response = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                            "permissionDecisionReason": notice}}
            elif kind == "mutating_other":
                consume_drain_action(state)
                response = _codex_context(event, notice)
            elif kind == "handoff_document":
                for target in targets:
                    _record_handoff_baseline(state, target)
            if tool_id:
                previous[tool_id] = response
                state["toolDecisions"] = dict(list(previous.items())[-512:])
        elif event == "Stop" and mode == "enforce" and phase in ("drain", "freeze"):
            reply = str(payload.get("last_assistant_message") or "")
            if _handoff_structure_present(reply) or _has_valid_handoff_file(state):
                state.update(phase="freeze", handoffRecorded=True)
                response = {"continue": False, "stopReason": "已记录交接结构，结束当前回合；目标与后台任务状态须由用户/总控另行核对。"}
            elif payload.get("stop_hook_active") or state["stopCorrectionRequested"]:
                state["phase"] = "freeze"
                response = {"continue": False, "stopReason": "交接要素仍不完整，已停止本轮自动补正以防循环；请人工核对未完成事项。",
                            "systemMessage": "交接未通过结构检查，不能报告为已完成。"}
            else:
                state["stopCorrectionRequested"] = True
                response = {"decision": "block", "reason": notice + "请补齐交接后结束；本次仅补正一次。"}
        save_gate_state(state_dir, sid, state)
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex 长会话交接事件适配；反重力使用既有模块接口")
    parser.add_argument("--client", choices=("codex",), required=True)
    parser.add_argument("--mode", choices=("advisory", "enforce", "off"), default="advisory")
    parser.add_argument("--compact-limit", type=int, default=0)
    parser.add_argument("--state-dir", default=str(HOOKS_DIR / "state" / "handoff"))
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("事件必须为 JSON 对象")
        response = handle_codex_handoff(payload, Path(args.state_dir), args.mode, args.compact_limit)
    except Exception as exc:
        # 状态/协议异常时不伪装已受保护，不在失败的钩子中无限继续。
        sys.stderr.write(f"Codex handoff hook: {exc}\n")
        response = {"systemMessage": "Codex 长会话门禁本次处理失败，未更新保护状态；请检查钩子错误。"}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

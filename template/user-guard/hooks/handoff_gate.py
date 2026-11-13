"""
handoff_gate.py - 反重力专有长会话两阶段「原子收口 + 交接停机」硬门禁引擎 (user-guard)

职责（仅 Antigravity 侧启用，Codex 侧不依赖本模块）：
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

import contextlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
import user_words  # noqa: E402

user_words.ensure_utf8_stdio()

# 阶段预算默认值
DEFAULT_DRAIN_INVOCATIONS = 4
DEFAULT_DRAIN_ACTIONS = 8

# 遥测阈值（基于多组真实长会话与图片洪泛轨迹标定）
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

# UI Banner 文本与 Turn-1 合理性最低门槛（防纯提问贴报错文本时误锁死）
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

# 显式 R8 暂缓 / 继续指令（明确排除“处理好交接”类要求执行交接的指令）
OVERRIDE_POSITIVE_RE = re.compile(
    r"(?:暂不交接|先不交接|先别交接|不用交接|不要交接|取消交接|解除交接|暂缓交接|跳过交接|"
    r"先继续(?:当前|完成|执行)|继续当前对话|不用开新对话|先别停|继续往下做)",
    re.IGNORECASE,
)
OVERRIDE_NEGATIVE_RE = re.compile(
    r"(?:处理好交接|做好交接|整理交接|开始交接|准备交接|立即交接|完成交接)",
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
        "gateSchemaVersion": 2,
        "conversationId": conversation_id,
        "phase": "normal",
        "triggers": [],
        "bannerDetectedAtStep": None,
        "bannerTriggerTurnHash": "",
        "enteredDrainAtStep": None,
        "enteredFreezeAtStep": None,
        "drainInvocationsRemaining": DEFAULT_DRAIN_INVOCATIONS,
        "drainActionsRemaining": DEFAULT_DRAIN_ACTIONS,
        "handoffFilesTouched": [],
        "imageReads": {},
        "imageReadCallCount": 0,
        "isGoalActive": False,
        "overriddenTurnHash": "",
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
        if not isinstance(raw, dict):
            return default
        merged = dict(default)
        merged.update(raw)
        merged["gateSchemaVersion"] = int(raw.get("gateSchemaVersion", 1))
        if not isinstance(merged.get("triggers"), list):
            merged["triggers"] = []
        if not isinstance(merged.get("handoffFilesTouched"), list):
            merged["handoffFilesTouched"] = []
        if not isinstance(merged.get("imageReads"), dict):
            merged["imageReads"] = {}
        if not isinstance(merged.get("stopGateBlockedTurnKeys"), list):
            merged["stopGateBlockedTurnKeys"] = []
        if not isinstance(merged.get("lastAdvisoryTriggers"), list):
            merged["lastAdvisoryTriggers"] = []
        return merged
    except Exception:
        return default


def save_gate_state(state_dir: Path, conversation_id: str, gate_state: Dict[str, Any]) -> None:
    gate_state["updatedAt"] = user_words.current_iso_time()
    user_words.atomic_write_json(_gate_state_path(state_dir, conversation_id), gate_state)


@contextlib.contextmanager
def _gate_state_lock(state_dir: Path, conversation_id: str, timeout_sec: float = 1.5) -> Iterator[None]:
    """轻量会话级独占文件锁，防止并发 PreToolUse / PreInvocation / Stop 发生状态读-改-写覆盖丢失"""
    state_dir.mkdir(parents=True, exist_ok=True)
    safe_cid = re.sub(r"[^A-Za-z0-9_.-]+", "_", conversation_id) or "unknown"
    lock_path = state_dir / f".{safe_cid}_handoff_gate.lock"
    acquired = False
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 10.0:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.005)
        except OSError:
            break
    try:
        yield
    finally:
        if acquired:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


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
    """登记单次图片读取并统计去重后的总图片数与磁盘字节总量"""
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

    # 将 transcript 中扫到的新增图片路径同步入 gate_state["imageReads"]
    scanned_imgs = compaction_state.get("transcriptImageSizes")
    img_map: Dict[str, int] = gate_state.setdefault("imageReads", {})
    if isinstance(scanned_imgs, dict):
        for k, sz in scanned_imgs.items():
            if len(img_map) >= MAX_TRACKED_IMAGES:
                break
            if k not in img_map or (int(sz or 0) > int(img_map.get(k, 0))):
                img_map[k] = int(sz or 0)

    comp_img_calls = int(compaction_state.get("transcriptImageCallCount", 0))
    gate_img_calls = int(gate_state.get("imageReadCallCount", 0))
    image_call_count = max(comp_img_calls, gate_img_calls, len(img_map))
    total_image_bytes = sum(int(v) for v in img_map.values() if isinstance(v, (int, float)))
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
        "totalImageBytes": total_image_bytes,
        "latestStep": latest_step,
        "latestUserInputHash": str(compaction_state.get("latestUserInputHash") or ""),
        "latestUserInputPreview": str(compaction_state.get("latestUserInputPreview") or ""),
        "latestUserStepIndex": int(compaction_state.get("latestUserStepIndex", -1)),
        "isGoalActive": bool(compaction_state.get("isGoalActive", False)),
    }


def _has_user_override(user_text: str) -> bool:
    if not user_text:
        return False
    if OVERRIDE_NEGATIVE_RE.search(user_text) and not re.search(
        r"(?:暂不交接|先不交接|先别交接|不用交接|不要交接|取消交接|解除交接|暂缓交接|跳过交接)",
        user_text,
    ):
        return False
    return bool(OVERRIDE_POSITIVE_RE.search(user_text))


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
    has_banner = UI_BANNER_PHRASE in (user_text or "").lower()
    if has_banner:
        banner_plausible = (
            cp_count >= BANNER_PLAUSIBLE_CP
            or latest_step >= BANNER_PLAUSIBLE_STEP
            or tp_bytes >= BANNER_PLAUSIBLE_TRANSCRIPT_BYTES
            or media_steps >= BANNER_PLAUSIBLE_MEDIA_STEPS
            or img_calls >= BANNER_PLAUSIBLE_IMAGE_CALLS
            or img_bytes >= BANNER_PLAUSIBLE_IMAGE_BYTES
        )
        if banner_plausible:
            stage2_hits.append("banner")

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
        f"transcript={mb_tp:.2f}MB, 读图={tel['imageCallCount']}次/{mb_img:.1f}MB"
    )


def _build_advisory_message(tel: Dict[str, Any], triggers: List[str]) -> str:
    extra = ""
    if "media_warning" in triggers or "image_bytes_warning" in triggers:
        extra = "；检测到批量读图偏高，请改用已提取文本/脚本核对，避免无谓大图耗尽上下文。"
    return (
        f"【长会话容量预警 · Stage-1 advisory】当前会话遥测（{_format_telemetry_brief(tel)}）已接近上下文上限。"
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

    # 从 v1 门禁状态升级时：若此前仅因误测 transcript_full.jsonl 体积触发 advisory/drain/freeze，按紧凑版重新标定自愈
    if int(gate_state.get("gateSchemaVersion", 1)) < 2:
        gate_state["gateSchemaVersion"] = 2
        changed = True
        prev_triggers = set(gate_state.get("triggers") or [])
        if prev_phase in ("advisory", "drain", "freeze") and prev_triggers and prev_triggers <= {"transcript_warning", "transcript_bytes"}:
            recal_adv, recal_s2 = _evaluate_triggers(tel, user_text)
            if not recal_s2:
                if recal_adv:
                    prev_phase = "advisory"
                    gate_state["phase"] = "advisory"
                    gate_state["triggers"] = list(dict.fromkeys(recal_adv))
                else:
                    prev_phase = "normal"
                    gate_state["phase"] = "normal"
                    gate_state["triggers"] = []
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

    turn_hash = tel.get("latestUserInputHash") or ""
    u_step_idx = int(tel.get("latestUserStepIndex", -1))
    turn_token = f"{u_step_idx}:{turn_hash}" if (turn_hash and u_step_idx >= 0) else turn_hash

    # 1. 检查显式 R8 暂缓指令（绑定步骤号+内容哈希，防止同轮重复消费且允许后续轮次再次输入相同暂缓词）
    if turn_token and gate_state.get("overriddenTurnHash") not in (turn_token, turn_hash) and _has_user_override(user_text):
        gate_state["phase"] = "overridden"
        gate_state["overriddenTurnHash"] = turn_token
        gate_state["overrideUntilCheckpoint"] = int(tel["cpCount"]) + OVERRIDE_SNOOZE_CPS
        gate_state["overrideUntilStep"] = int(tel["latestStep"]) + OVERRIDE_SNOOZE_STEPS
        gate_state["drainInvocationsRemaining"] = DEFAULT_DRAIN_INVOCATIONS
        gate_state["drainActionsRemaining"] = DEFAULT_DRAIN_ACTIONS
        user_words.append_lifecycle_log(
            HOOKS_DIR,
            "HandoffGate:override",
            conversation_id,
            f"R8 explicit user override accepted until cp>={gate_state['overrideUntilCheckpoint']} step>={gate_state['overrideUntilStep']}",
        )
        return True

    # 2. 若当前处于 R8 overridden 宽限期，检查宽限窗口是否已过期（或遇到新的 banner 报错）
    if prev_phase == "overridden":
        snooze_expired = (
            int(tel["cpCount"]) >= int(gate_state.get("overrideUntilCheckpoint", -1))
            or int(tel["latestStep"]) >= int(gate_state.get("overrideUntilStep", -1))
        )
        new_banner_in_fresh_turn = (
            turn_token
            and gate_state.get("overriddenTurnHash") not in (turn_token, turn_hash)
            and gate_state.get("bannerTriggerTurnHash") not in (turn_token, turn_hash)
            and UI_BANNER_PHRASE in (user_text or "").lower()
        )
        if not snooze_expired and not new_banner_in_fresh_turn:
            return changed
        # 宽限到期或新报错出现，允许重新进入阈值判定
        gate_state["phase"] = "normal"
        prev_phase = "normal"
        changed = True

    advisory_hits, stage2_hits = _evaluate_triggers(tel, user_text)

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
        gate_state["drainInvocationsRemaining"] = DEFAULT_DRAIN_INVOCATIONS
        gate_state["drainActionsRemaining"] = DEFAULT_DRAIN_ACTIONS
        if "banner" in stage2_hits:
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


def evaluate_pre_invocation(
    conversation_id: str,
    event_name: str,
    state_dir: Path,
    compaction_state: Dict[str, Any],
    transcript_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    供 session_context_loader.py (Antigravity SessionStart / PreInvocation) 调用：
    评估会话压力并在 advisory/drain/freeze 时生成附加系统提示。
    """
    try:
        with _gate_state_lock(state_dir, conversation_id):
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
    except Exception:
        return {
            "phase": "normal",
            "advisoryNotice": None,
            "mandateMessage": None,
            "telemetry": {},
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
        return "readonly_inspect"
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


def check_pre_tool_use(payload: Dict[str, Any], state_dir: Path) -> Optional[Dict[str, Any]]:
    """
    供 status_guard.py 在 R4 边界检验之后调用的 PreToolUse 交接硬门禁。
    返回 None 表示当前门禁放行（继续执行后续防轮询检查）；返回 dict 则直接阻断。
    """
    try:
        conversation_id = str(payload.get("conversationId") or "").strip()
        if not conversation_id:
            return None

        compaction_state_file = state_dir / f"{conversation_id}_compaction.json"
        if not compaction_state_file.exists() and not _gate_state_path(state_dir, conversation_id).exists():
            # 无主会话记录且尚未建门禁状态时直接放行，零干扰子代理或空跑测试
            return None

        try:
            compaction_state = json.loads(compaction_state_file.read_text(encoding="utf-8"))
        except Exception:
            compaction_state = {}
        if not compaction_state.get("hasExplicitUserInput", False):
            return None

        with _gate_state_lock(state_dir, conversation_id):
            gate_state = load_gate_state(state_dir, conversation_id)
            tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
            tool_name = str(tool_call.get("name") or tool_call.get("tool_name") or "").strip()
            args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
            cwd = payload.get("cwd")
            workspace_paths = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
            transcript_str = payload.get("transcriptPath")
            transcript_path = user_words.resolve_compact_transcript_path(Path(transcript_str)) if transcript_str else None

            # 对 read_file 图片读取实时累计计数并检查是否当场突破 Stage-2
            is_image_read = False
            if tool_name == "read_file":
                file_arg = str(args.get("Path") or args.get("path") or "").strip()
                if is_image_path(file_arg):
                    is_image_read = True
                    record_image_read(gate_state, file_arg, cwd=cwd, workspace_paths=workspace_paths, increment_calls=True)

            tel = _merge_telemetry(compaction_state, gate_state, transcript_path=transcript_path)
            _refresh_phase_from_telemetry(
                gate_state,
                tel,
                tel["latestUserInputPreview"],
                conversation_id,
                decrement_invocation=False,
            )

            phase = str(gate_state.get("phase") or "normal")
            if phase not in ("drain", "freeze"):
                if is_image_read:
                    save_gate_state(state_dir, conversation_id, gate_state)
                return None

            # 以下为 Stage-2 (drain / freeze) 精确工具门禁
            # 1. 绝不允许的派工与常驻/外推工具（drain 与 freeze 均阻断）
            if tool_name in DELEGATION_TOOLS or tool_name.startswith("browser_"):
                save_gate_state(state_dir, conversation_id, gate_state)
                return {
                    "decision": "deny",
                    "reason": (
                        f"【user-guard 长会话交接门禁 ({phase})】当前会话已进入交接停机收口期（{_format_telemetry_brief(tel)}），"
                        f"严禁再调用 `{tool_name}` 新开子代理/集群/定时器/浏览器任务！"
                        "请仅完成本地原子收口并整理交接文档与新对话提示词后立即停机。"
                    ),
                }

            # 2. manage_task / manage_subagents：允许查看或收尾后台任务（不扣减动作预算）
            if tool_name in ("manage_task", "manage_subagents"):
                save_gate_state(state_dir, conversation_id, gate_state)
                return None

            # 3. read_file：禁止读图（防耗尽剩余配额），放行文本核对
            if tool_name == "read_file":
                save_gate_state(state_dir, conversation_id, gate_state)
                if is_image_read:
                    return {
                        "decision": "deny",
                        "reason": (
                            f"【user-guard 长会话交接门禁 ({phase})】已累计读取 {tel['imageCallCount']} 次图片"
                            f"（{tel['totalImageBytes'] / (1024 * 1024):.1f} MB），触发多模态上下文保护！"
                            "收口期内禁止继续调用 `read_file` 看图，请改用文本摘要/脚本输出核对后完成交接。"
                        ),
                    }
                return None

            # 4. write_file / edit_file：交接文档始终放行；非交接修改在 drain 扣减预算，在 freeze 阻断
            if tool_name in ("write_file", "edit_file"):
                write_arg = str(args.get("Path") or args.get("path") or args.get("TargetFile") or args.get("file_path") or "").strip()
                if _is_handoff_target(write_arg, conversation_id, cwd, workspace_paths):
                    resolved_target = str(_resolve_candidate_path(write_arg, cwd=cwd, workspace_paths=workspace_paths))
                    if resolved_target not in gate_state["handoffFilesTouched"]:
                        gate_state["handoffFilesTouched"].append(resolved_target)
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
                gate_state["drainActionsRemaining"] = rem_actions - 1
                if gate_state["drainActionsRemaining"] == 0:
                    gate_state["phase"] = "freeze"
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
                gate_state["drainActionsRemaining"] -= 1
                if gate_state["drainActionsRemaining"] == 0:
                    gate_state["phase"] = "freeze"
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
                            f"【user-guard 长会话交接门禁 ({phase})】收口期内严禁启动跨端派工（`cae_harness.py`）、`git push` 或常驻守护进程！"
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

                rem_actions = int(gate_state.get("drainActionsRemaining", 0))
                if rem_actions <= 0:
                    gate_state["phase"] = "freeze"
                    gate_state["enteredFreezeAtStep"] = tel["latestStep"]
                    save_gate_state(state_dir, conversation_id, gate_state)
                    return {
                        "decision": "deny",
                        "reason": "【user-guard 长会话交接门禁 (drain -> freeze)】排空运行预算已耗尽，禁止再启动新命令，请完成交接停机。",
                    }
                gate_state["drainActionsRemaining"] = rem_actions - 1
                if gate_state["drainActionsRemaining"] == 0:
                    gate_state["phase"] = "freeze"
                    gate_state["enteredFreezeAtStep"] = tel["latestStep"]
                save_gate_state(state_dir, conversation_id, gate_state)
                return None

            save_gate_state(state_dir, conversation_id, gate_state)
            return None
    except Exception:
        return None


def _has_valid_handoff_file(gate_state: Dict[str, Any]) -> bool:
    for p_str in gate_state.get("handoffFilesTouched") or []:
        try:
            p = Path(str(p_str))
            if p.exists() and p.is_file() and p.stat().st_size >= 80:
                return True
        except OSError:
            continue
    return False


def _reply_has_handoff_prompt(reply_text: str) -> bool:
    stripped = (reply_text or "").strip()
    if len(stripped) < 60:
        return False
    low = stripped.lower()
    has_cue = bool(PROMPT_MARKER_RE.search(stripped)) or any(
        cue in low for cue in ("交接", "接续", "新对话", "新会话", "handoff", "handover", "next prompt", "new conversation")
    )
    code_blocks = re.findall(r"```(?:[^\n]*\n)?([\s\S]{15,}?)```", stripped)
    if code_blocks and has_cue:
        return True
    if PROMPT_MARKER_RE.search(stripped) and len(stripped) >= 80:
        return True
    return False


def check_stop_handoff(
    payload: Dict[str, Any],
    state_dir: Path,
    turn_key: str,
    latest_reply: str,
) -> Optional[Dict[str, Any]]:
    """
    供 reply_repeat_guard.py 在 Stop（NO_TOOL_CALL 且非 continuationActive）时调用：
    在 drain/freeze 阶段核验是否已完成全量交接（提示词 + /goal 终止标记）。
    每回合最多阻断 1 次，绝不死循环。
    """
    try:
        conversation_id = str(payload.get("conversationId") or "").strip()
        if not conversation_id:
            return None

        with _gate_state_lock(state_dir, conversation_id):
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
                return None

            missing_parts: List[str] = []
            has_file = _has_valid_handoff_file(gate_state)
            has_prompt_inline = _reply_has_handoff_prompt(latest_reply)
            if not has_file and not has_prompt_inline:
                missing_parts.append(
                    "尚未落盘非空交接文档且最终回复中缺少可直接复制粘贴到新对话的完整【接续提示词】（含已验证进度、Git 状态与未尽事项）"
                )

            if gate_state.get("isGoalActive") and "<!-- GOAL_CANCELLED -->" not in (latest_reply or ""):
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
                return None

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
    except Exception:
        return None

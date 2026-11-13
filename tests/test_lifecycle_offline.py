"""
test_lifecycle_offline.py - 上下文压缩原话回溯引擎离线单元测试套件

覆盖验证：
1. 新会话与常规未压缩多轮对话：SessionStart 与 PreInvocation 零注入、零生成归档；
2. 会话内自动压缩或手动 /compact 写入 CHECKPOINT 后：下一轮 PreInvocation 精确触发 1 次提取落盘与注入；
3. 压缩后第 2、3 轮日常对话：检查点步号幂等去重，零重复注入；
4. 二次压缩（新 CHECKPOINT 步号递增）：再次精确触发 1 次；
5. 冷重载 SessionStart 与 PreInvocation 协同幂等（绝不双倍注入）；
6. user_words 边界：嵌套 </USER_REQUEST> 不截断、O(N+M) 截断补全、ask_question 选项框回答提取；
7. 超长历史原话字符预算保护（保留首轮总纲与最近定论，中间省略并指向归档文件）；
8. 子代理会话自动豁免主会话注入，且 PLUGIN_DATA 环境变量隔离生效。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = Path(os.environ.get("USER_GUARD_INSTALLED_DIR", str(REPO_ROOT / "template" / "user-guard"))) / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import session_context_loader  # noqa: E402
import user_words  # noqa: E402


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


class TestLifecycleCompactionEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self.tmp_obj.name)
        self.plugin_data = self.tmp_root / "plugin_data"
        self.old_plugin_data = os.environ.get("PLUGIN_DATA")
        self.old_state = os.environ.get("USER_GUARD_STATE_DIR")
        self.old_logs = os.environ.get("USER_GUARD_LOGS_DIR")

        os.environ["PLUGIN_DATA"] = str(self.plugin_data)
        os.environ.pop("USER_GUARD_STATE_DIR", None)
        os.environ.pop("USER_GUARD_LOGS_DIR", None)

    def tearDown(self) -> None:
        if self.old_plugin_data is None:
            os.environ.pop("PLUGIN_DATA", None)
        else:
            os.environ["PLUGIN_DATA"] = self.old_plugin_data
        if self.old_state is not None:
            os.environ["USER_GUARD_STATE_DIR"] = self.old_state
        if self.old_logs is not None:
            os.environ["USER_GUARD_LOGS_DIR"] = self.old_logs
        self.tmp_obj.cleanup()

    def test_01_normal_session_zero_injection_across_turns(self) -> None:
        """新会话启动及多轮日常未压缩对话，绝不触发原话回读注入"""
        conv_id = "conv-normal-001"
        tp = self.tmp_root / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"

        # 1. 空会话启动 SessionStart
        r0 = session_context_loader.handle_session_start({"conversationId": conv_id, "transcriptPath": str(tp)})
        self.assertEqual(r0.get("injectSteps"), [])

        # 2. 连续 3 轮普通对话 PreInvocation（无 CHECKPOINT）
        for turn in range(3):
            _append_jsonl(
                tp,
                {
                    "step_index": turn * 2,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": f"<USER_REQUEST>\n日常开发指令第 {turn + 1} 轮\n</USER_REQUEST>",
                },
            )
            res = session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(tp)})
            self.assertEqual(res.get("injectSteps"), [])
            _append_jsonl(
                tp,
                {"step_index": turn * 2 + 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "收到并已完成。"},
            )

        archive_md = self.plugin_data / "state" / f"{conv_id}_user_words.md"
        self.assertFalse(archive_md.exists(), "未压缩日常会话不应执行全盘提取与归档写入")

    def test_02_compaction_triggers_exactly_once_and_second_compaction_retriggers(self) -> None:
        """发生自动压缩或 /compact 后首轮精确注入 1 次，后续日常轮 0 注入，二次压缩再触发 1 次"""
        conv_id = "conv-compact-002"
        tp = self.tmp_root / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"

        _append_jsonl(
            tp,
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-12T01:00:00Z",
                "content": "<USER_REQUEST>\n实现高阶守恒律数值求解器（如 FVM / DG / DGSEM / WENO），严禁改动网格接口。\n</USER_REQUEST>",
            },
        )
        _append_jsonl(tp, {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "好的。"})

        # 第一轮未压缩，0 注入
        self.assertEqual(
            session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(tp)})["injectSteps"],
            [],
        )

        # 触发第一次上下文压缩 CHECKPOINT (step 10) + 新一轮用户提问 (step 11)
        _append_jsonl(tp, {"step_index": 10, "source": "SYSTEM", "type": "CHECKPOINT", "content": "# Resuming from a compaction"})
        _append_jsonl(
            tp,
            {
                "step_index": 11,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-12T02:00:00Z",
                "content": "<USER_REQUEST>\n压缩后继续核对通量边界条件\n</USER_REQUEST>",
            },
        )

        # 压缩后首次 PreInvocation -> 必须精确触发注入！
        r_after_cp1 = session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(tp)})
        steps1 = r_after_cp1.get("injectSteps", [])
        self.assertEqual(len(steps1), 1)
        msg1 = steps1[0]["systemMessage"]["systemMessage"]
        self.assertIn("CHECKPOINT step #10", msg1)
        self.assertIn("严禁改动网格接口", msg1)
        self.assertIn("压缩后继续核对通量边界条件", msg1)

        archive_md = self.plugin_data / "state" / f"{conv_id}_user_words.md"
        self.assertTrue(archive_md.exists())
        self.assertIn(str(archive_md), msg1)

        # 压缩后的第 2 轮、第 3 轮日常对话 -> 必须严格 0 重复注入！
        _append_jsonl(
            tp,
            {"step_index": 13, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>\n再跑一下收敛阶\n</USER_REQUEST>"},
        )
        r_turn2 = session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(tp)})
        self.assertEqual(r_turn2.get("injectSteps"), [])

        # 即使此时窗口重启触发 SessionStart，因 CHECKPOINT #10 已注入过，依然幂等 0 注入！
        r_reload = session_context_loader.handle_session_start({"conversationId": conv_id, "transcriptPath": str(tp)})
        self.assertEqual(r_reload.get("injectSteps"), [])

        # 发生第二次上下文压缩 CHECKPOINT (step 30 > 10) -> 必须再次精确触发 1 次！
        _append_jsonl(tp, {"step_index": 30, "source": "SYSTEM", "type": "CHECKPOINT", "content": "# Second compaction"})
        _append_jsonl(
            tp,
            {"step_index": 31, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>\n出最终验收报告\n</USER_REQUEST>"},
        )
        r_after_cp2 = session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(tp)})
        steps2 = r_after_cp2.get("injectSteps", [])
        self.assertEqual(len(steps2), 1)
        msg2 = steps2[0]["systemMessage"]["systemMessage"]
        self.assertIn("CHECKPOINT step #30", msg2)
        self.assertIn("出最终验收报告", msg2)
        self.assertIn("再跑一下收敛阶", msg2)

    def test_03_nested_tags_truncated_full_lookup_and_ask_question(self) -> None:
        """验证嵌套 </USER_REQUEST> 不截断、O(N+M) 截断补全及 ask_question 选项回答提取"""
        log_dir = self.tmp_root / "logs_parse"
        tp = log_dir / "transcript.jsonl"
        full_tp = log_dir / "transcript_full.jsonl"

        # 1. 含内部 </USER_REQUEST> 代码片段的用户原话
        nested_prompt = (
            "<USER_REQUEST>\n"
            "请检查下面这段正则示例：\n"
            "`<USER_REQUEST>示例文本</USER_REQUEST>`\n"
            "真正的结尾要求在这里：保留高精度浮点输出！\n"
            "</USER_REQUEST>\n"
            "<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-12.\n</ADDITIONAL_METADATA>"
        )
        _append_jsonl(tp, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": nested_prompt})

        # 2. 被截断的超长用户指令（需从 transcript_full.jsonl 补全）
        full_long_body = "【核心数值边界约束】" + ("高阶通量重构细节_" * 400) + "【末尾锚点_不可丢失】"
        _append_jsonl(
            tp,
            {
                "step_index": 2,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\n【核心数值...<truncated>",
                "truncated_fields": ["content"],
            },
        )
        _append_jsonl(
            full_tp,
            {
                "step_index": 2,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": f"<USER_REQUEST>\n{full_long_body}\n</USER_REQUEST>",
            },
        )

        # 3. 模型发起 ask_question 提问，紧随 GENERIC 用户选项框回答
        _append_jsonl(
            tp,
            {
                "step_index": 5,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {
                        "name": "ask_question",
                        "args": {"questions": [{"question": "选择时间推进格式", "options": ["SSP-RK3", "ADER"]}]},
                    }
                ],
            },
        )
        _append_jsonl(
            tp,
            {
                "step_index": 6,
                "source": "MODEL",
                "type": "GENERIC",
                "content": "Created At: 2026-11-12T06:00:00+08:00\nCompleted At: 2026-11-12T06:00:05+08:00\nUser selected: SSP-RK3",
            },
        )

        words = user_words.extract_user_words(str(tp))
        self.assertEqual(len(words), 3)
        self.assertIn("真正的结尾要求在这里：保留高精度浮点输出！", words[0]["content"])
        self.assertIn("`<USER_REQUEST>示例文本</USER_REQUEST>`", words[0]["content"])
        self.assertIn("【末尾锚点_不可丢失】", words[1]["content"])
        self.assertEqual(words[2]["kind"], "ask_question_answer")
        self.assertIn("选择时间推进格式", words[2]["content"])
        self.assertIn("User selected: SSP-RK3", words[2]["content"])

    def test_04_budget_cap_preserves_first_and_latest_turns(self) -> None:
        """超长多轮历史原话触发注入时，严格遵守字符上限并保留首轮总纲与最新定论"""
        words = []
        for i in range(1, 21):
            words.append(
                {
                    "turn": i,
                    "step_index": i * 2,
                    "created_at": "2026-11-12T00:00:00Z",
                    "kind": "user_input",
                    "content": f"第 {i} 轮指令核心头_" + ("长篇技术参数说明_" * 200) + f"_第 {i} 轮尾部定论",
                }
            )
        archive_path = self.plugin_data / "state" / "budget_test_user_words.md"
        msg = session_context_loader.build_compaction_system_message(words, archive_path, "CHECKPOINT step #99")

        self.assertLessEqual(len(msg), session_context_loader.MAX_INLINE_TOTAL_CHARS + 300)
        self.assertIn("第 1 轮指令核心头_", msg)
        self.assertIn("第 20 轮尾部定论", msg)
        self.assertIn("因上下文预算省略内联", msg)
        self.assertIn(str(archive_path), msg)

    def test_05_subagent_session_skips_main_injection(self) -> None:
        """无 USER_INPUT 的纯子代理会话在自动压缩时不注入主会话回溯消息"""
        conv_id = "subagent-readonly-005"
        tp = self.tmp_root / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"

        _append_jsonl(
            tp,
            {
                "step_index": 0,
                "source": "SYSTEM_SDK",
                "type": "SYSTEM_MESSAGE",
                "content": "<SYSTEM_MESSAGE>\n[Message] sender=parent-conv content=请只读审查 status_guard.py 第 1~100 行\n</SYSTEM_MESSAGE>",
            },
        )
        _append_jsonl(tp, {"step_index": 20, "source": "SYSTEM", "type": "CHECKPOINT", "content": "# Compacted"})

        res = session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(tp)})
        self.assertEqual(res.get("injectSteps"), [])

        # 但直接调用 extract_user_words(include_subagent_msgs=True) 可正确提取父代理任务书
        sub_words = user_words.extract_user_words(str(tp), include_subagent_msgs=True)
        self.assertEqual(len(sub_words), 1)
        self.assertEqual(sub_words[0]["kind"], "subagent_message")
        self.assertIn("请只读审查 status_guard.py", sub_words[0]["content"])

    def test_06_codex_multisegment_rollout_extraction_and_session_start_hook(self) -> None:
        """验证 Codex 模式跨归档与活动分段合并、provenance 过滤、选项框识别及缺口保底"""
        sid = "01a1c238-test-codex-session"
        codex_root = self.tmp_root / ".codex"
        seg1 = codex_root / "archived_sessions" / f"rollout-2026-11-09T01-00-00-{sid}.jsonl"
        seg2 = codex_root / "sessions" / "2026" / "11" / "10" / f"rollout-2026-11-10T05-00-00-{sid}_cont.jsonl"
        seg_sub = codex_root / "sessions" / "2026" / "11" / "10" / f"rollout-2026-11-10T06-00-00-{sid}_sub.jsonl"

        _append_jsonl(seg1, {"type": "session_meta", "payload": {"id": sid, "cwd": "/tmp/proj", "source": "cli"}})
        # 系统自动注入的 AGENTS.md 与环境块应被过滤
        _append_jsonl(
            seg1,
            {
                "type": "response_item",
                "ordinal": 1,
                "timestamp": "2026-11-09T01:00:01Z",
                "payload": {
                    "id": "sys-msg-1",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /tmp/proj"}],
                    "internal_chat_message_metadata_passthrough": {
                        "content_item_metadata": [{"provenance": {"type": "agents_md"}}]
                    },
                },
            },
        )
        # 人类真实输入（即使正文引用了 # AGENTS.md 字面量，因 provenance=user 必须完整保留）
        shared_first_msg = {
            "type": "response_item",
            "ordinal": 2,
            "timestamp": "2026-11-09T01:01:00Z",
            "payload": {
                "id": "usr-msg-1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "首轮立项：检查 # AGENTS.md instructions 与 DGSEM 通量守恒"}],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_metadata": [{"provenance": {"type": "user"}}]
                },
            },
        }
        _append_jsonl(seg1, shared_first_msg)

        # 第二段续跑包含第一段镜像记录（应按 id 去重）+ 两次人类打字“继续”（不同 id 均须保留）+ 选项框回答
        _append_jsonl(seg2, {"type": "session_meta", "payload": {"id": sid, "cwd": "/tmp/proj", "source": "desktop"}})
        _append_jsonl(seg2, shared_first_msg)
        _append_jsonl(
            seg2,
            {
                "type": "response_item",
                "ordinal": 3,
                "timestamp": "2026-11-10T05:01:00Z",
                "payload": {"id": "usr-msg-2", "type": "message", "role": "user", "content": "<environment_context>\nauto\n</environment_context>"},
            },
        )
        _append_jsonl(
            seg2,
            {
                "type": "response_item",
                "ordinal": 4,
                "timestamp": "2026-11-10T05:02:00Z",
                "payload": {"id": "usr-msg-3", "type": "message", "role": "user", "content": "继续"},
            },
        )
        _append_jsonl(
            seg2,
            {
                "type": "response_item",
                "ordinal": 5,
                "timestamp": "2026-11-10T05:03:00Z",
                "payload": {"id": "usr-msg-4", "type": "message", "role": "user", "content": "继续"},
            },
        )
        _append_jsonl(
            seg2,
            {
                "type": "response_item",
                "ordinal": 6,
                "timestamp": "2026-11-10T05:04:00Z",
                "payload": {
                    "id": "usr-msg-5",
                    "type": "message",
                    "role": "user",
                    "content": "<send_user_message_question_reply>采用熵稳定通量</send_user_message_question_reply>",
                },
            },
        )

        # 子代理分段不应混入主会话
        _append_jsonl(seg_sub, {"type": "session_meta", "payload": {"id": sid, "cwd": "/tmp/proj", "source": "subagent"}})
        _append_jsonl(
            seg_sub,
            {
                "type": "response_item",
                "ordinal": 1,
                "timestamp": "2026-11-10T06:00:01Z",
                "payload": {"id": "sub-1", "type": "message", "role": "user", "content": "子代理内部提示词"},
            },
        )

        state_dir = codex_root / "hooks" / "state" / "session-context"
        log_file = codex_root / "hooks" / "logs" / "session-context.jsonl"
        out = session_context_loader.handle_codex_session_start(
            {
                "session_id": sid,
                "transcript_path": str(seg2),
                "cwd": "/tmp/proj",
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            state_dir_str=str(state_dir),
            log_path_str=str(log_file),
        )
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "SessionStart")
        ctx = spec["additionalContext"]
        self.assertIn("提取状态：成功", ctx)
        self.assertIn("第 1～4 条", ctx)
        self.assertIn("DGSEM 通量守恒", ctx)
        self.assertIn("采用熵稳定通量", ctx)
        self.assertNotIn("子代理内部提示词", ctx)

        md_file = codex_root / "hooks" / "state" / "user-words" / f"{sid}.md"
        self.assertTrue(md_file.is_file())
        md_text = md_file.read_text(encoding="utf-8")
        self.assertEqual(md_text.count("\n继续\n"), 2, "两次真实打字『继续』均须保留")
        self.assertTrue(log_file.is_file())
        self.assertNotIn("DGSEM 通量守恒", log_file.read_text(encoding="utf-8"), "运行日志仅记元数据，不落盘对话正文")

        # 当 transcript_path 缺失触发缺口时，必须保留上次已验证的归档文档
        gap_out = session_context_loader.handle_codex_session_start(
            {"session_id": sid, "transcript_path": "", "source": "resume"},
            state_dir_str=str(state_dir),
            log_path_str=str(log_file),
        )
        gap_ctx = gap_out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("提取状态：存在缺口", gap_ctx)
        self.assertIn("保留上次已验证文档", gap_ctx)
        self.assertEqual(md_file.read_text(encoding="utf-8"), md_text)

    def test_07_handoff_gate_telemetry_advisory_drain_and_override(self) -> None:
        """验证长会话遥测建档、Turn-1 防误伤、Stage-1 预警、检查点坍塌 drain 及 R8 显式暂缓"""
        import handoff_gate

        # 1) Turn-1 提问引用英文 UI Banner 严禁误触发 Stage-2
        conv_turn1 = "ag-handoff-turn1"
        tp_t1 = self.tmp_root / "t1.jsonl"
        banner = "This conversation grew too large, so the history before this point may be missing or incomplete."
        _append_jsonl(tp_t1, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": f"<USER_REQUEST>\n{banner}\n请分析原因\n</USER_REQUEST>"})
        out_t1 = session_context_loader.handle_pre_invocation({"conversationId": conv_turn1, "transcriptPath": str(tp_t1)})
        self.assertEqual(out_t1["injectSteps"], [], "新会话首轮仅引用 Banner 不得误触发")

        # 2) 旧版 v1 state 平滑回填遥测，不重复注入已注入检查点，且健康会话保持仅 Stage-1 advisory
        conv_healthy = "ag-handoff-healthy"
        tp_ok = self.tmp_root / "healthy.jsonl"
        _append_jsonl(tp_ok, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "开始长期研究"})
        step_cursor = 140
        for _ in range(11):
            _append_jsonl(tp_ok, {"step_index": step_cursor, "source": "SYSTEM", "type": "CHECKPOINT"})
            step_cursor += 140
        out_ok = session_context_loader.handle_pre_invocation({"conversationId": conv_healthy, "transcriptPath": str(tp_ok)})
        self.assertEqual(len(out_ok["injectSteps"]), 1)
        msg_ok = out_ok["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Stage-1 advisory", msg_ok)
        self.assertNotIn("Stage-2 drain", msg_ok)
        gate_ok = handoff_gate.load_gate_state(self.plugin_data / "state", conv_healthy)
        self.assertEqual(gate_ok["phase"], "advisory")

        # 3) 检查点间隔坍塌（cp >= 10 且尾 3 段间隔 <= 75）：触发 Stage-2 drain
        conv_thrash = "ag-handoff-thrash"
        tp_th = self.tmp_root / "thrash.jsonl"
        _append_jsonl(tp_th, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "/goal\n完成批量校准，不要擅自停止"})
        s = 100
        for i in range(10):
            _append_jsonl(tp_th, {"step_index": s, "source": "SYSTEM", "type": "CHECKPOINT"})
            s += 150 if i < 6 else 50
        out_th = session_context_loader.handle_pre_invocation({"conversationId": conv_thrash, "transcriptPath": str(tp_th)})
        msg_th = out_th["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Stage-2 drain", msg_th)
        self.assertIn("GOAL_CANCELLED", msg_th)

        # 4) 用户发送 "请你处理好交接" 不得误触发 R8 暂缓；发送 "先别交接，继续当前对话" 则触发 overridden 宽限
        _append_jsonl(tp_th, {"step_index": s + 5, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "请你处理好交接"})
        session_context_loader.handle_pre_invocation({"conversationId": conv_thrash, "transcriptPath": str(tp_th)})
        self.assertEqual(handoff_gate.load_gate_state(self.plugin_data / "state", conv_thrash)["phase"], "drain")

        _append_jsonl(tp_th, {"step_index": s + 10, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "先别交接，继续当前对话做完这一批"})
        out_ovr = session_context_loader.handle_pre_invocation({"conversationId": conv_thrash, "transcriptPath": str(tp_th)})
        self.assertEqual(handoff_gate.load_gate_state(self.plugin_data / "state", conv_thrash)["phase"], "overridden")
        self.assertEqual(out_ovr["injectSteps"], [])

        # 5) 真实 protojson 双重引号路径图片读取计数、外层 <EPHEMERAL_MESSAGE> UI Banner 及超 4000 字符末尾 R8 暂缓识别
        conv_proto = "ag-handoff-proto"
        tp_pr = self.tmp_root / "proto.jsonl"
        img_file = self.tmp_root / "plot_01.png"
        img_file.write_bytes(b"PNG" + b"\x00" * 2048)
        _append_jsonl(
            tp_pr,
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": f"<EPHEMERAL_MESSAGE>\n{banner}\n</EPHEMERAL_MESSAGE>\n<USER_REQUEST>\n检查图片轨迹\n</USER_REQUEST>",
            },
        )
        _append_jsonl(tp_pr, {"step_index": 120, "source": "SYSTEM", "type": "CHECKPOINT"})
        _append_jsonl(
            tp_pr,
            {
                "step_index": 125,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "read_file", "args": {"Path": json.dumps(str(img_file))}}],
            },
        )
        out_pr = session_context_loader.handle_pre_invocation({"conversationId": conv_proto, "transcriptPath": str(tp_pr)})
        self.assertEqual(handoff_gate.load_gate_state(self.plugin_data / "state", conv_proto)["phase"], "drain")
        self.assertEqual(len(out_pr["injectSteps"]), 1)
        gate_pr = handoff_gate.load_gate_state(self.plugin_data / "state", conv_proto)
        self.assertEqual(gate_pr["imageReads"].get(str(img_file.resolve()).casefold() if os.name == "nt" else str(img_file.resolve())), 2051)

        long_tail_r8 = ("补充长指令背景说明。\n" * 400) + "暂不交接，继续当前对话"
        _append_jsonl(tp_pr, {"step_index": 130, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": long_tail_r8})
        session_context_loader.handle_pre_invocation({"conversationId": conv_proto, "transcriptPath": str(tp_pr)})
        self.assertEqual(handoff_gate.load_gate_state(self.plugin_data / "state", conv_proto)["phase"], "overridden")

    def test_11_compact_transcript_resolution_and_legacy_gate_recalibration(self) -> None:
        """传入 transcript_full.jsonl 时优先度量紧凑版 transcript.jsonl，兼顾截断原文还原与旧误报自愈"""
        import handoff_gate

        conv_id = "ag-compact-resolve"
        log_dir = self.tmp_root / "brain" / conv_id / ".system_generated" / "logs"
        compact_tp = log_dir / "transcript.jsonl"
        full_tp = log_dir / "transcript_full.jsonl"

        full_user_text = "<USER_REQUEST>\n重要原始需求：保持数值守恒\n</USER_REQUEST>"
        _append_jsonl(
            compact_tp,
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\n重要原始<truncated 12 bytes>",
                "truncated_fields": ["content"],
            },
        )
        _append_jsonl(compact_tp, {"step_index": 10, "source": "SYSTEM", "type": "CHECKPOINT"})

        _append_jsonl(
            full_tp,
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": full_user_text,
            },
        )
        pad_block = "X" * (650 * 1024)
        for idx in range(1, 11):
            _append_jsonl(full_tp, {"step_index": idx, "source": "SYSTEM", "type": "GENERIC", "content": pad_block})

        self.assertGreater(full_tp.stat().st_size, 6 * 1024 * 1024)
        self.assertLess(compact_tp.stat().st_size, 1024 * 1024)

        out = session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(full_tp)})
        self.assertEqual(handoff_gate.load_gate_state(self.plugin_data / "state", conv_id)["phase"], "normal")
        self.assertEqual(len(out["injectSteps"]), 1)
        self.assertIn("重要原始需求：保持数值守恒", out["injectSteps"][0]["systemMessage"]["systemMessage"])
        comp_saved = json.loads((self.plugin_data / "state" / f"{conv_id}_compaction.json").read_text(encoding="utf-8"))
        self.assertEqual(comp_saved.get("scannedTranscriptName"), "transcript.jsonl")

        # 模拟从旧版 telemetryVersion=2 + gateSchemaVersion=1（因 transcript_full 体积误进入 drain）平滑升级
        conv_legacy = "ag-compact-legacy"
        state_dir = self.plugin_data / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f"{conv_legacy}_compaction.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "telemetryVersion": 2,
                    "conversationId": conv_legacy,
                    "scannedByteOffset": full_tp.stat().st_size,
                    "latestCheckpointStep": 10,
                    "lastInjectedCheckpointStep": 10,
                    "hasExplicitUserInput": True,
                }
            ),
            encoding="utf-8",
        )
        (state_dir / f"{conv_legacy}_handoff_gate.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "conversationId": conv_legacy,
                    "phase": "drain",
                    "triggers": ["transcript_bytes"],
                }
            ),
            encoding="utf-8",
        )
        out_leg = session_context_loader.handle_pre_invocation({"conversationId": conv_legacy, "transcriptPath": str(full_tp)})
        self.assertEqual(out_leg["injectSteps"], [], "升级重标定时不得重复注入已注入过的 CHECKPOINT")
        gate_leg = handoff_gate.load_gate_state(state_dir, conv_legacy)
        self.assertEqual(gate_leg["phase"], "normal")
        self.assertEqual(gate_leg["gateSchemaVersion"], 2)

        # 取消 /goal 与宽限期届满后重复发送相同文本 R8 暂缓指令必须生效
        conv_r8 = "ag-cancel-repeat-r8"
        tp_r8 = self.tmp_root / f"{conv_r8}.jsonl"
        _append_jsonl(tp_r8, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "/goal\n开始执行"})
        s = 100
        for _ in range(16):
            _append_jsonl(tp_r8, {"step_index": s, "source": "SYSTEM", "type": "CHECKPOINT"})
            s += 100
        session_context_loader.handle_pre_invocation({"conversationId": conv_r8, "transcriptPath": str(tp_r8)})
        self.assertTrue(handoff_gate.load_gate_state(state_dir, conv_r8)["isGoalActive"])

        _append_jsonl(tp_r8, {"step_index": s + 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "取消 /goal\n先别交接，继续当前对话"})
        session_context_loader.handle_pre_invocation({"conversationId": conv_r8, "transcriptPath": str(tp_r8)})
        g_r8 = handoff_gate.load_gate_state(state_dir, conv_r8)
        self.assertFalse(g_r8["isGoalActive"], "显式取消 /goal 必须关闭 isGoalActive")
        self.assertEqual(g_r8["phase"], "overridden")

        for k in range(3):
            s += 150
            _append_jsonl(tp_r8, {"step_index": s, "source": "SYSTEM", "type": "CHECKPOINT"})
        session_context_loader.handle_pre_invocation({"conversationId": conv_r8, "transcriptPath": str(tp_r8)})
        self.assertEqual(handoff_gate.load_gate_state(state_dir, conv_r8)["phase"], "drain")

        _append_jsonl(tp_r8, {"step_index": s + 5, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "取消 /goal\n先别交接，继续当前对话"})
        session_context_loader.handle_pre_invocation({"conversationId": conv_r8, "transcriptPath": str(tp_r8)})
        self.assertEqual(handoff_gate.load_gate_state(state_dir, conv_r8)["phase"], "overridden")


if __name__ == "__main__":
    unittest.main(verbosity=2)


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

import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

        # 3. 使用宿主调用标识关联选项结果，不能把中间的其它工具输出当成回答
        _append_jsonl(
            tp,
            {
                "step_index": 5,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {
                        "name": "ask_question",
                        "id": "question-5",
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
                "content": "不相关输出",
                "tool_call_id": "other-6",
            },
        )
        _append_jsonl(
            tp,
            {
                "step_index": 7,
                "source": "MODEL",
                "type": "GENERIC",
                "tool_call_id": "question-5",
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
                "payload": {
                    "id": "usr-msg-2", "type": "message", "role": "user", "content": "<environment_context>\nauto\n</environment_context>",
                    "internal_chat_message_metadata_passthrough": {
                        "content_item_metadata": [{"provenance": {"type": "harness"}}],
                    },
                },
            },
        )
        _append_jsonl(
            seg2,
            {
                "type": "response_item",
                "ordinal": 4,
                "timestamp": "2026-11-10T05:02:00Z",
                "payload": {
                    "id": "usr-msg-3", "type": "message", "role": "user", "content": "继续",
                    "internal_chat_message_metadata_passthrough": {
                        "content_item_metadata": [{"provenance": {"type": "user"}}],
                    },
                },
            },
        )
        _append_jsonl(
            seg2,
            {
                "type": "response_item",
                "ordinal": 5,
                "timestamp": "2026-11-10T05:03:00Z",
                "payload": {
                    "id": "usr-msg-4", "type": "message", "role": "user", "content": "继续",
                    "internal_chat_message_metadata_passthrough": {
                        "content_item_metadata": [{"provenance": {"type": "user"}}],
                    },
                },
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
                    "internal_chat_message_metadata_passthrough": {
                        "content_item_metadata": [{"provenance": {"type": "user"}}],
                    },
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

    def test_codex_long_gap_preserves_provenance_and_reports_unverified_manifest(self) -> None:
        sid = "codex-long-gap"
        transcript = self.tmp_root / "rollout-long.jsonl"
        state_dir = self.tmp_root / "session-context"
        log = self.tmp_root / "hook-log.jsonl"
        _append_jsonl(transcript, {"type": "session_meta", "payload": {"id": sid, "source": "vscode"}})

        def add_user(index, body, origin="user"):
            metadata = {"content_item_metadata": [{"provenance": {"type": origin}}]} if origin else {}
            _append_jsonl(transcript, {"type": "response_item", "ordinal": index, "payload": {
                "type": "message", "role": "user", "id": f"user-{index}", "content": body,
                "internal_chat_message_metadata_passthrough": metadata,
            }})

        add_user(1, "# AGENTS.md instructions\n真实首轮立项")
        for index in range(2, 23):
            add_user(index, "中间工程约束。" * 250)
        add_user(23, "最新裁定保留。")
        payload = {"session_id": sid, "transcript_path": str(transcript), "source": "compact"}

        def render():
            return session_context_loader.handle_codex_session_start(
                payload, str(state_dir), str(log),
            )["hookSpecificOutput"]["additionalContext"]

        self.assertIn("提取状态：成功", render())
        md = self.tmp_root / "user-words" / f"{sid}.md"
        md.write_text("可读，但和清单错配\n", encoding="utf-8")
        add_user(24, "<environment_context>\n字面量输入候选\n</environment_context>", origin=None)
        with transcript.open("ab") as stream:
            stream.write(b'{"type":"response_item","payload":')
        context = render()
        self.assertLessEqual(len(context), session_context_loader.MAX_INLINE_TOTAL_CHARS)
        self.assertIn("提取状态：存在缺口", context)
        self.assertIn("正文可读，完整性未验证", context)
        self.assertIn("原始轨迹：" + str(transcript), context)
        self.assertIn("缺口信息", context)
        self.assertIn("真实首轮立项", context)
        self.assertIn("最新裁定保留", context)
        self.assertIn("字面量输入候选", context)
        self.assertIn("用户角色，来源未核实", context)
        self.assertNotIn("保留上次已验证文档", context)
        self.assertNotIn("完整归档", context)

    def test_codex_archive_publication_failure_is_detectable_and_recoverable(self) -> None:
        sid = "codex-publication"
        transcript = self.tmp_root / "rollout.jsonl"
        user_dir = self.tmp_root / "user-words"
        _append_jsonl(transcript, {"type": "session_meta", "payload": {"id": sid, "source": "cli"}})
        first = user_words.refresh_codex_session_archive(str(transcript), user_dir, sid)
        self.assertTrue(first["saved"]["verified"])
        _append_jsonl(transcript, {"type": "response_item", "payload": {"type": "message", "role": "user",
                      "content": "并发归档记录", "internal_chat_message_metadata_passthrough": {
                          "content_item_metadata": [{"provenance": {"type": "user"}}],
                      }}})
        with mock.patch.object(user_words, "atomic_write_json", side_effect=OSError("写清单被中断")):
            interrupted = user_words.refresh_codex_session_archive(str(transcript), user_dir, sid)
        self.assertFalse(interrupted["complete"])
        self.assertTrue(interrupted["saved"]["available"])
        self.assertFalse(interrupted["saved"]["verified"])
        self.assertFalse(interrupted["saved"]["preserved"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: user_words.refresh_codex_session_archive(str(transcript), user_dir, sid), range(8)))
        self.assertTrue(all(item["complete"] and item["saved"]["verified"] for item in results))
        md = user_dir / f"{sid}.md"
        manifest = user_dir / f"{sid}.json"
        published = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(published["sha256"], hashlib.sha256(md.read_bytes()).hexdigest())
        manifest.write_text('{"publication":{"sha256":"' + published["sha256"] + '"}}', encoding="utf-8")
        gap = user_words.refresh_codex_session_archive("", user_dir, sid)
        self.assertTrue(gap["saved"]["available"])
        self.assertFalse(gap["saved"]["verified"])
        self.assertFalse(gap["saved"]["preserved"])

    def test_codex_legacy_manifest_migration_requires_matching_body_and_source_coverage(self) -> None:
        for scenario in ("complete", "missing_segment", "bad_body_hash"):
            with self.subTest(scenario=scenario):
                sid = f"codex-v1-{scenario}"
                older = self.tmp_root / ".codex" / "archived_sessions" / f"rollout-2026-01-01-{sid}.jsonl"
                active = self.tmp_root / ".codex" / "sessions/2026/01/02" / f"rollout-2026-01-02-{sid}.jsonl"
                user_dir = self.tmp_root / "user-words"
                user_dir.mkdir(exist_ok=True)
                md = user_dir / f"{sid}.md"
                manifest = user_dir / f"{sid}.json"
                for segment in (older, active):
                    _append_jsonl(segment, {"type": "session_meta", "payload": {"id": sid, "source": "cli"}})

                def user_message(segment, key, body):
                    _append_jsonl(segment, {"type": "response_item", "payload": {
                        "type": "message", "role": "user", "id": key, "content": body,
                        "internal_chat_message_metadata_passthrough": {
                            "content_item_metadata": [{"provenance": {"type": "user"}}],
                        },
                    }})

                user_message(older, "old", "旧档案中的第一条原话")
                parsed = user_words.extract_codex_session_words(str(active), sid)
                v1_body = user_words.format_user_words_markdown(sid, parsed["words"]).replace(
                    "# 会话用户原话归档", "# 会话用户原话全量留痕档案", 1,
                )
                user_words.atomic_write_text(md, v1_body)
                user_words.atomic_write_json(manifest, {
                    "format": "user-guard-codex-v1", "id": sid, "file": str(md), "count": 1,
                    "last_time": "", "sources": parsed["sources"],
                    "sha256": "0" * 64 if scenario == "bad_body_hash" else hashlib.sha256(md.read_bytes()).hexdigest(),
                })
                old_md, old_manifest = md.read_bytes(), manifest.read_bytes()
                unavailable = user_words.refresh_codex_session_archive("", user_dir, sid)
                self.assertFalse(unavailable["saved"]["verified"])
                self.assertFalse(unavailable["saved"]["preserved"])
                if scenario != "bad_body_hash":
                    self.assertTrue(unavailable["saved"]["manifest_verified"])
                    self.assertTrue(unavailable["saved"]["legacy_manifest"])
                    self.assertEqual(unavailable["saved"]["sources"], parsed["sources"])
                user_message(active, "new", "升级期间新增的用户原话")
                if scenario == "missing_segment":
                    older.rename(older.with_suffix(".unavailable"))
                result = user_words.refresh_codex_session_archive(str(active), user_dir, sid)
                if scenario == "complete":
                    self.assertTrue(result["complete"])
                    self.assertTrue(result["saved"]["verified"])
                    self.assertEqual(result["saved"]["count"], 2)
                    self.assertIn("升级期间新增的用户原话", md.read_text(encoding="utf-8"))
                    self.assertEqual(json.loads(manifest.read_bytes())["format"], "user-guard-codex-v2")
                else:
                    self.assertFalse(result["complete"])
                    self.assertFalse(result["saved"]["verified"])
                    self.assertFalse(result["saved"]["preserved"])
                    self.assertEqual(md.read_bytes(), old_md)
                    self.assertEqual(manifest.read_bytes(), old_manifest)
                    if scenario == "missing_segment":
                        self.assertTrue(any(str(older) in issue for issue in result["issues"]))
                    else:
                        self.assertTrue(any("正文哈希与校验清单不匹配" in issue for issue in result["issues"]))

    def test_codex_interrupted_publication_cannot_hide_a_missing_segment(self) -> None:
        sid = "codex-interrupted-missing-segment"
        older = self.tmp_root / ".codex" / "archived_sessions" / f"rollout-2026-01-01-{sid}.jsonl"
        active = self.tmp_root / ".codex" / "sessions/2026/01/02" / f"rollout-2026-01-02-{sid}.jsonl"
        user_dir = self.tmp_root / "user-words"
        md = user_dir / f"{sid}.md"
        manifest = user_dir / f"{sid}.json"
        for segment in (older, active):
            _append_jsonl(segment, {"type": "session_meta", "payload": {"id": sid, "source": "cli"}})

        def user_message(segment, key, body):
            _append_jsonl(segment, {"type": "response_item", "payload": {
                "type": "message", "role": "user", "id": key, "content": body,
                "internal_chat_message_metadata_passthrough": {
                    "content_item_metadata": [{"provenance": {"type": "user"}}],
                },
            }})

        user_message(older, "older", "早期分段独有原话")
        self.assertTrue(user_words.refresh_codex_session_archive(str(active), user_dir, sid)["complete"])
        old_manifest = manifest.read_bytes()
        user_message(active, "new", "后续分段新增原话")
        with mock.patch.object(user_words, "atomic_write_json", side_effect=OSError("写清单被中断")):
            self.assertFalse(user_words.refresh_codex_session_archive(str(active), user_dir, sid)["complete"])
        pending_body = md.read_bytes()
        self.assertIn("早期分段独有原话", pending_body.decode())
        self.assertIn("后续分段新增原话", pending_body.decode())
        unavailable = older.with_suffix(".unavailable")
        older.rename(unavailable)
        failed = user_words.refresh_codex_session_archive(str(active), user_dir, sid)
        self.assertFalse(failed["complete"])
        self.assertFalse(failed["saved"]["verified"])
        self.assertFalse(failed["saved"]["preserved"])
        self.assertTrue(any(str(older) in issue for issue in failed["issues"]))
        self.assertEqual(md.read_bytes(), pending_body)
        self.assertEqual(manifest.read_bytes(), old_manifest)

        unavailable.rename(older)
        with mock.patch.object(user_words, "atomic_write_text", wraps=user_words.atomic_write_text) as writes:
            restored = user_words.refresh_codex_session_archive(str(active), user_dir, sid)
        self.assertTrue(restored["saved"]["verified"])
        self.assertEqual(md.read_bytes(), pending_body)
        self.assertEqual([call.args[0] for call in writes.call_args_list], [manifest])

    def test_codex_later_system_provenance_corrects_and_reports_conflicts(self) -> None:
        for first_origin, second_origin in ((None, "harness"), ("user", "agents_md"), ("harness", "user")):
            with self.subTest(first=first_origin, later=second_origin):
                sid = f"codex-source-{first_origin}-{second_origin}"
                older = self.tmp_root / ".codex" / "archived_sessions" / f"rollout-2026-01-01-{sid}.jsonl"
                active = self.tmp_root / ".codex" / "sessions/2026/01/02" / f"rollout-2026-01-02-{sid}.jsonl"
                user_dir = self.tmp_root / "user-words"
                for segment in (older, active):
                    _append_jsonl(segment, {"type": "session_meta", "payload": {"id": sid, "source": "cli"}})

                def message(segment, origin):
                    metadata = {"content_item_metadata": [{"provenance": {"type": origin}}]} if origin else {}
                    _append_jsonl(segment, {"type": "response_item", "payload": {
                        "type": "message", "role": "user", "id": "shared-input", "content": "来源待判定的内容",
                        "internal_chat_message_metadata_passthrough": metadata,
                    }})

                message(older, first_origin)
                user_words.refresh_codex_session_archive(str(active), user_dir, sid)
                message(active, second_origin)
                result = user_words.refresh_codex_session_archive(str(active), user_dir, sid)
                self.assertEqual(result["words"], [])
                self.assertEqual(result["complete"], first_origin is None)
                self.assertEqual(any("用户与系统来源冲突" in issue for issue in result["issues"]), first_origin is not None)
                self.assertTrue(result["saved"]["manifest_verified"])
                self.assertNotIn("来源待判定的内容", (user_dir / f"{sid}.md").read_text(encoding="utf-8"))

    def test_codex_attachment_and_cross_segment_provenance_upgrade(self) -> None:
        sid = "codex-attachment-history"
        first = self.tmp_root / ".codex" / "archived_sessions" / f"rollout-2026-01-01-{sid}.jsonl"
        second = self.tmp_root / ".codex" / "sessions/2026/01/02" / f"rollout-2026-01-02-{sid}.jsonl"
        for transcript in (first, second):
            _append_jsonl(transcript, {"type": "session_meta", "payload": {"id": sid, "source": "cli"}})
        attachment = {"type": "input_image", "file_id": "fixture-image"}
        _append_jsonl(first, {"type": "response_item", "payload": {
            "type": "message", "role": "user", "id": "image-1", "content": [attachment],
        }})
        _append_jsonl(second, {"type": "response_item", "payload": {
            "type": "message", "role": "user", "id": "image-1", "content": [attachment],
            "internal_chat_message_metadata_passthrough": {"content_item_metadata": [
                {"provenance": {"type": "user"}},
            ]},
        }})
        user_dir = self.tmp_root / "user-words"
        result = user_words.refresh_codex_session_archive(str(second), user_dir, sid)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["words"]), 1)
        self.assertTrue(result["words"][0]["provenance_verified"])
        self.assertEqual(result["words"][0]["source_path"], str(second))
        body = (user_dir / f"{sid}.md").read_text(encoding="utf-8")
        self.assertIn("图片附件", body)
        self.assertIn(str(second), body)
        self.assertNotIn("fixture-image", body)

        _append_jsonl(second, {"type": "response_item", "payload": {
            "type": "message", "role": "user", "id": "image-1",
            "content": [{"type": "input_image", "file_id": "another-image"}],
            "internal_chat_message_metadata_passthrough": {"content_item_metadata": [
                {"provenance": {"type": "user"}},
            ]},
        }})
        conflict = user_words.extract_codex_session_words(str(second), sid)
        self.assertFalse(conflict["complete"])
        self.assertEqual(len(conflict["words"]), 2)
        self.assertTrue(any("内容冲突" in issue for issue in conflict["issues"]))

    def test_codex_archive_updates_partial_history_and_preserves_missing_segment(self) -> None:
        sid = "codex-archive-coverage"
        older = self.tmp_root / ".codex" / "archived_sessions" / f"rollout-2026-01-01-{sid}.jsonl"
        active = self.tmp_root / ".codex" / "sessions/2026/01/02" / f"rollout-2026-01-02-{sid}.jsonl"
        user_dir = self.tmp_root / "user-words"
        archive = user_dir / f"{sid}.md"
        for segment in (older, active):
            _append_jsonl(segment, {"type": "session_meta", "payload": {"id": sid, "source": "cli"}})

        def user_message(segment, key, body, origin=None):
            metadata = {"content_item_metadata": [{"provenance": {"type": origin}}]} if origin else {}
            _append_jsonl(segment, {"type": "response_item", "payload": {
                "type": "message", "role": "user", "id": key, "content": body,
                "internal_chat_message_metadata_passthrough": metadata,
            }})

        user_message(older, "initial", "早期原话，初次缺少来源标记")
        self.assertFalse(user_words.refresh_codex_session_archive(str(active), user_dir, sid)["complete"])
        user_message(active, "later", "本轮新增且来源已核实", "user")
        partial = user_words.refresh_codex_session_archive(str(active), user_dir, sid)
        self.assertFalse(partial["complete"])
        self.assertTrue(partial["saved"]["manifest_verified"])
        self.assertIn("本轮新增且来源已核实", archive.read_text(encoding="utf-8"))
        user_message(active, "initial", "早期原话，初次缺少来源标记", "user")
        complete = user_words.refresh_codex_session_archive(str(active), user_dir, sid)
        self.assertTrue(complete["complete"])
        before = archive.read_bytes()
        older.rename(older.with_suffix(".unavailable"))
        gap = user_words.refresh_codex_session_archive(str(active), user_dir, sid)
        self.assertFalse(gap["complete"])
        self.assertTrue(gap["saved"]["preserved"])
        self.assertTrue(any(str(older) in issue for issue in gap["issues"]))
        self.assertEqual(archive.read_bytes(), before)

    def test_codex_gate_and_archive_use_independent_lock_namespaces(self) -> None:
        import handoff_gate

        sid = "codex-independent-locks"
        transcript = self.tmp_root / "rollout.jsonl"
        _append_jsonl(transcript, {"type": "session_meta", "payload": {"id": sid, "source": "cli"}})
        state = self.tmp_root / "state"
        with user_words.session_state_lock(state / "user-words", sid):
            response = handoff_gate.handle_codex_handoff(
                {"session_id": sid, "hook_event_name": "SessionStart", "source": "compact",
                 "transcript_path": str(transcript)}, state / "handoff",
            )
            self.assertIn("hookSpecificOutput", response)
        with user_words.session_state_lock(state / "handoff", sid):
            self.assertTrue(user_words.refresh_codex_session_archive(str(transcript), state / "user-words", sid)["complete"])

    def test_implicit_question_result_remains_unverified_and_full_log_repairs_pending_checkpoint(self) -> None:
        cid = "agy-archive-gap"
        transcript = self.tmp_root / "transcript.jsonl"
        full = self.tmp_root / "transcript_full.jsonl"
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 0, "content": "<USER_REQUEST>\n正文待补",
                                  "truncated_fields": ["content"]})
        _append_jsonl(transcript, {"type": "PLANNER_RESPONSE", "step_index": 1, "tool_calls": [{"name": "ask_question"}]})
        _append_jsonl(transcript, {"type": "GENERIC", "step_index": 2, "content": "User selected: 未核对候选"})
        _append_jsonl(transcript, {"type": "CHECKPOINT", "step_index": 3})
        payload = {"conversationId": cid, "transcriptPath": str(transcript)}
        first = session_context_loader.handle_pre_invocation(payload)
        context = first["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("截断正文待补", context)
        self.assertIn("选项回答来源未核实", context)
        self.assertNotIn("未核对候选", context)
        for _ in range(4):
            self.assertEqual(session_context_loader.handle_pre_invocation(payload)["injectSteps"], [])
        _append_jsonl(full, {"type": "USER_INPUT", "step_index": 0, "content": "<USER_REQUEST>\n已补齐正文\n</USER_REQUEST>"})
        updated = session_context_loader.handle_pre_invocation(payload)["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("已补齐正文", updated)
        self.assertNotIn("截断正文待补", updated)
        self.assertIn("选项回答来源未核实", updated)
        archive = self.plugin_data / "state" / f"{cid}_user_words.md"
        self.assertIn("已补齐正文", archive.read_text(encoding="utf-8"))
        self.assertNotIn("未核对候选", archive.read_text(encoding="utf-8"))

    def test_agy_late_full_control_updates_latest_turn_and_goal(self) -> None:
        import handoff_gate

        cid = "agy-late-control"
        state_dir = self.plugin_data / "state"
        transcript = self.tmp_root / "transcript.jsonl"
        full = self.tmp_root / "transcript_full.jsonl"
        payload = {"conversationId": cid, "transcriptPath": str(transcript)}
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 0, "content": "请整理交接并停机"})
        session_context_loader.handle_pre_invocation(payload)
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 1, "content": "先别交接",
                                  "truncated_fields": ["content"]})
        session_context_loader.handle_pre_invocation(payload)
        self.assertEqual(handoff_gate.load_gate_state(state_dir, cid)["phase"], "drain")
        _append_jsonl(full, {"type": "USER_INPUT", "step_index": 1, "content": "/goal\n先别交接"})
        session_context_loader.handle_pre_invocation(payload)
        gate = handoff_gate.load_gate_state(state_dir, cid)
        self.assertEqual(gate["phase"], "overridden")
        self.assertTrue(gate["isGoalActive"])

        _append_jsonl(transcript, {"type": "PLANNER_RESPONSE", "step_index": 2,
                                  "content": "<!-- GOAL_CANCELLED -->", "truncated_fields": ["content"]})
        session_context_loader.handle_pre_invocation(payload)
        self.assertTrue(handoff_gate.load_gate_state(state_dir, cid)["isGoalActive"])
        _append_jsonl(full, {"type": "PLANNER_RESPONSE", "step_index": 2,
                             "content": "已收尾。\n<!-- GOAL_CANCELLED -->"})
        session_context_loader.handle_pre_invocation(payload)
        self.assertFalse(handoff_gate.load_gate_state(state_dir, cid)["isGoalActive"])

        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 3, "content": "/goal",
                                  "truncated_fields": ["content"]})
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 4, "content": "取消 /goal\n请立即交接"})
        session_context_loader.handle_pre_invocation(payload)
        _append_jsonl(full, {"type": "USER_INPUT", "step_index": 3, "content": "/goal\n先别交接"})
        session_context_loader.handle_pre_invocation(payload)
        gate = handoff_gate.load_gate_state(state_dir, cid)
        self.assertEqual(gate["phase"], "drain")
        self.assertFalse(gate["isGoalActive"])

    def test_agy_late_override_survives_newer_ordinary_user_message(self) -> None:
        import handoff_gate

        cid = "agy-late-override"
        state_dir = self.plugin_data / "state"
        transcript = self.tmp_root / "transcript.jsonl"
        full = self.tmp_root / "transcript_full.jsonl"
        payload = {"conversationId": cid, "transcriptPath": str(transcript)}
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 0, "content": "请整理交接并停机"})
        session_context_loader.handle_pre_invocation(payload)
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 1, "content": "暂缓",
                                  "truncated_fields": ["content"]})
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 2, "content": "查看进度"})
        session_context_loader.handle_pre_invocation(payload)
        self.assertEqual(handoff_gate.load_gate_state(state_dir, cid)["phase"], "drain")
        _append_jsonl(full, {"type": "USER_INPUT", "step_index": 1, "content": "暂缓交接"})
        session_context_loader.handle_pre_invocation(payload)
        gate = handoff_gate.load_gate_state(state_dir, cid)
        self.assertEqual(gate["phase"], "overridden")
        self.assertTrue(gate["overriddenTurnHash"].startswith("1:"))
        compaction = json.loads((state_dir / f"{cid}_compaction.json").read_text(encoding="utf-8"))
        self.assertEqual(compaction["latestUserStepIndex"], 2)
        self.assertEqual(compaction["latestHandoffStepIndex"], 1)

        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 3, "content": "暂缓",
                                  "truncated_fields": ["content"]})
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 4, "content": "请整理交接并停机"})
        session_context_loader.handle_pre_invocation(payload)
        _append_jsonl(full, {"type": "USER_INPUT", "step_index": 3, "content": "暂缓交接"})
        session_context_loader.handle_pre_invocation(payload)
        self.assertEqual(handoff_gate.load_gate_state(state_dir, cid)["phase"], "drain")

    def test_mature_conversation_ignores_quoted_banner_and_yields_to_explicit_handoff(self) -> None:
        import handoff_gate

        cid = "agy-quoted-banner"
        transcript = self.tmp_root / "transcript.jsonl"
        state_dir = self.plugin_data / "state"
        banner = handoff_gate.UI_BANNER_PHRASE
        payload = {"conversationId": cid, "transcriptPath": str(transcript)}

        def add(step_index, step_type, content="", source="USER_EXPLICIT"):
            _append_jsonl(transcript, {"step_index": step_index, "type": step_type, "source": source, "content": content})

        add(0, "USER_INPUT", "核对轨迹")
        add(160, "CHECKPOINT", source="SYSTEM")
        add(320, "CHECKPOINT", source="SYSTEM")
        add(321, "USER_INPUT", f"历史引用如下。\n> {banner}\n> 先别交接\n请分析历史问题。")
        add(322, "SYSTEM_MESSAGE", f"<SYSTEM_MESSAGE>\nThe following system message was injected by a hook:\n{banner}\n</SYSTEM_MESSAGE>", "SYSTEM_SDK")
        session_context_loader.handle_pre_invocation(payload)
        self.assertEqual(handoff_gate.load_gate_state(state_dir, cid)["phase"], "normal")
        add(323, "USER_INPUT", "先别交接，继续当前对话")
        session_context_loader.handle_pre_invocation(payload)
        self.assertEqual(handoff_gate.load_gate_state(state_dir, cid)["phase"], "overridden")
        add(324, "USER_INPUT", "~~~text\n请你开始交接\n~~~\n请逐句分析。")
        session_context_loader.handle_pre_invocation(payload)
        self.assertEqual(handoff_gate.load_gate_state(state_dir, cid)["phase"], "overridden")
        add(325, "USER_INPUT", "请你开始交接")
        session_context_loader.handle_pre_invocation(payload)
        self.assertEqual(handoff_gate.load_gate_state(state_dir, cid)["phase"], "drain")
        self.assertIn("user_request", handoff_gate.load_gate_state(state_dir, cid)["triggers"])

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

        # 5) 受信系统记录、图片请求与轨迹结果，以及超 4000 字符末尾的独立暂缓指令
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
                "content": "<USER_REQUEST>\n检查图片轨迹\n</USER_REQUEST>",
            },
        )
        _append_jsonl(tp_pr, {"step_index": 120, "source": "SYSTEM", "type": "CHECKPOINT"})
        _append_jsonl(tp_pr, {"step_index": 121, "source": "SYSTEM_SDK", "type": "SYSTEM_MESSAGE",
                             "content": f"<SYSTEM_MESSAGE>\n<EPHEMERAL_MESSAGE>\n{banner}\n</EPHEMERAL_MESSAGE>\n</SYSTEM_MESSAGE>"})
        _append_jsonl(
            tp_pr,
            {
                "step_index": 125,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "read_file", "args": {"Path": json.dumps(str(img_file))}}],
            },
        )
        _append_jsonl(tp_pr, {"step_index": 126, "source": "MODEL", "type": "GENERIC", "status": "DONE",
                             "media": [{"uri": str(img_file), "mime_type": "image/png"}]})
        out_pr = session_context_loader.handle_pre_invocation({"conversationId": conv_proto, "transcriptPath": str(tp_pr)})
        self.assertEqual(handoff_gate.load_gate_state(self.plugin_data / "state", conv_proto)["phase"], "drain")
        self.assertEqual(len(out_pr["injectSteps"]), 1)
        comp_pr = json.loads((self.plugin_data / "state" / f"{conv_proto}_compaction.json").read_text(encoding="utf-8"))
        self.assertEqual(comp_pr["transcriptImageCallCount"], 1)
        self.assertEqual(comp_pr["transcriptImageResultCount"], 1)
        image_key = str(img_file.resolve()).casefold() if os.name == "nt" else str(img_file.resolve())
        self.assertEqual(comp_pr["transcriptImageAttemptSizes"][image_key], 2051)
        self.assertEqual(comp_pr["transcriptImageSizes"][image_key], 2051)

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
        self.assertEqual(gate_leg["gateSchemaVersion"], 3)

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


class TestCodexHandoffAdapter(unittest.TestCase):
    """Codex 独立协议、保守默认值和严格模式的可观察状态转换。"""

    def setUp(self):
        import handoff_gate
        self.gate = handoff_gate
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-handoff-test-")
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.transcript = self.root / "rollout.jsonl"
        self.sid = "codex-handoff-main"
        _append_jsonl(self.transcript, {"type": "session_meta", "payload": {
            "id": self.sid, "cwd": str(self.root), "source": "cli"}})

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, event, mode="advisory", limit=0, **extra):
        payload = {"session_id": self.sid, "transcript_path": str(self.transcript),
                   "cwd": str(self.root), "hook_event_name": event, "turn_id": "turn-1", **extra}
        return self.gate.handle_codex_handoff(payload, self.state_dir, mode, limit)

    def state(self):
        return json.loads((self.state_dir / (self.sid + "_handoff_gate.json")).read_text(encoding="utf-8"))

    def test_advisory_never_blocks_and_compaction_events_are_not_double_counted(self):
        first = self.call("PostCompact", trigger="auto")
        self.assertIn("Codex 长会话", first["systemMessage"])
        self.call("PostCompact", trigger="auto")
        start = self.call("SessionStart", source="compact")
        self.assertIn("additionalContext", start["hookSpecificOutput"])
        self.assertEqual(self.state()["compactionCount"], 1)
        for index in range(2, 21):
            self.call("PostCompact", turn_id=f"turn-{index}", trigger="auto")
        self.assertEqual(self.call("PreToolUse", tool_name="spawn_agent", tool_input={}), {})
        self.assertEqual(self.call("Stop", last_assistant_message="尚未交接"), {})
        self.assertEqual(self.state()["phase"], "advisory")

    def test_strict_without_explicit_limit_does_not_invent_capacity_threshold(self):
        for index in range(5):
            self.call("PostCompact", mode="enforce", turn_id=f"turn-{index}")
        self.assertEqual(self.call("PreToolUse", mode="enforce", tool_name="spawn_agent"), {})
        self.assertEqual(self.state()["phase"], "advisory")

    def test_record_identity_counts_two_compactions_in_one_turn(self):
        for stamp in ("2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"):
            _append_jsonl(self.transcript, {"type": "compacted", "timestamp": stamp, "payload": {"message": "summary"}})
            self.call("PostCompact", mode="enforce", limit=2)
            self.call("PostCompact", mode="enforce", limit=2)
        self.assertEqual(self.state()["compactionCount"], 2)
        self.assertEqual(self.state()["compactionIdentity"], "rollout_record")
        self.assertEqual(self.state()["phase"], "drain")

    def test_strict_budget_idempotence_and_tool_protocol(self):
        self.call("PostCompact", mode="enforce", limit=1)
        denied = self.call("PreToolUse", mode="enforce", limit=1, tool_name="spawn_agent")
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        mcp_denied = self.call("PreToolUse", mode="enforce", limit=1, tool_name="mcp__codex_app__create_thread")
        self.assertEqual(mcp_denied["hookSpecificOutput"]["permissionDecision"], "deny")
        push = self.call("PreToolUse", mode="enforce", limit=1, tool_name="Bash",
                         tool_input={"command": 'echo "$(git push origin main)"'})
        self.assertEqual(push["hookSpecificOutput"]["permissionDecision"], "deny")
        for index in range(self.gate.CODEX_DRAIN_ACTIONS):
            args = {"tool_name": "Bash", "tool_input": {"command": "python -B verify.py"}, "tool_use_id": str(index)}
            first = self.call("PreToolUse", mode="enforce", limit=1, **args)
            self.assertEqual(first, self.call("PreToolUse", mode="enforce", limit=1, **args))
        self.assertEqual(self.state()["phase"], "freeze")
        self.assertEqual(self.state()["drainActionsRemaining"], 0)
        blocked = self.call("PreToolUse", mode="enforce", limit=1, tool_name="Bash", tool_input={"command": "python build.py"})
        self.assertEqual(blocked["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.call("PreToolUse", mode="enforce", limit=1, tool_name="Bash", tool_input={"command": "git status --short"}), {})
        self.assertEqual(self.call("PreToolUse", mode="enforce", limit=1, tool_name="get_goal"), {})

    def test_frozen_patch_exemption_is_only_for_in_project_handoff_documents(self):
        self.call("UserPromptSubmit", mode="enforce", prompt="请整理交接并停机")
        state = self.state()
        state["phase"] = "freeze"
        self.gate.save_gate_state(self.state_dir, self.sid, state)
        good = "*** Begin Patch\n*** Add File: docs/handoff.md\n+handoff\n*** End Patch"
        self.assertEqual(self.call("PreToolUse", mode="enforce", tool_name="apply_patch", tool_input={"command": good}), {})
        for path in ("src/handoff.py", "../handoff.md", "docs/ordinary.md"):
            patch = good.replace("docs/handoff.md", path)
            result = self.call("PreToolUse", mode="enforce", tool_name="apply_patch", tool_input={"command": patch})
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        mixed = good.replace("*** End Patch", "*** Update File: solver.py\n@@\n-old\n+new\n*** End Patch")
        result = self.call("PreToolUse", mode="enforce", tool_name="apply_patch", tool_input={"command": mixed})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stop_correction_is_bounded_and_never_claims_goal_cancelled(self):
        self.call("UserPromptSubmit", mode="enforce", prompt="请整理交接并停机")
        first = self.call("Stop", mode="enforce", last_assistant_message="尚未整理")
        self.assertEqual(first["decision"], "block")
        second = self.call("Stop", mode="enforce", turn_id="continuation-turn", last_assistant_message="仍未整理")
        self.assertFalse(second["continue"])
        self.assertFalse(self.state()["handoffRecorded"])
        reply = ("已验证：现有离线测试通过。Git 工作区保留尚未提交的改动。未完成：真实宿主的事件验收。\n"
                 "新对话提示词\n```text\n请读用户原话和当前源码，先核对工作区，继续未完成的真实宿主验收；后台任务尚待核对。\n```")
        final = self.call("Stop", mode="enforce", last_assistant_message=reply)
        self.assertFalse(final["continue"])
        self.assertTrue(self.state()["handoffRecorded"])
        self.assertNotIn("GOAL_CANCELLED", json.dumps(final))

    def test_codex_stop_accepts_only_changed_current_handoff_document(self):
        self.call("UserPromptSubmit", mode="enforce", prompt="请整理交接并停机")
        path = self.root / "HANDOFF.md"
        document = (
            "已验证：已完成第一批核对。\nGit 工作区：保留当前修改。\n未完成：第二批验收。\n"
            "## 新对话提示词\n请读取用户最新裁定，从第二批的完整验收开始。\n"
        )
        path.write_text(document, encoding="utf-8")
        patch = "*** Begin Patch\n*** Update File: HANDOFF.md\n@@\n+本轮增加证据。\n*** End Patch"
        self.call("PreToolUse", mode="enforce", tool_name="apply_patch", tool_input=patch, tool_use_id="proposal")
        self.assertEqual(self.call("Stop", mode="enforce", last_assistant_message="文件已写")["decision"], "block")
        path.write_text(document + "本轮增加证据。\n", encoding="utf-8")
        result = self.call("Stop", mode="enforce", last_assistant_message="文件已写")
        self.assertFalse(result["continue"])
        self.assertTrue(self.state()["handoffRecorded"])

        self.call("UserPromptSubmit", mode="enforce", turn_id="next", prompt="暂不交接")
        self.call("UserPromptSubmit", mode="enforce", turn_id="new-handoff", prompt="请整理交接并停机")
        self.assertEqual(self.call("Stop", mode="enforce", last_assistant_message="仍用前一次文档")["decision"], "block")

    def test_codex_stop_rejects_candidate_redirected_outside_project(self):
        import subprocess

        project = self.root / "project"
        project.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        outside_file = outside / "handoff.md"
        outside_file.write_text(
            "已验证：第一批通过。\nGit 工作区：当前干净。\n未完成：第二批。\n"
            "## 新对话提示词\n请先核对工作区，再完成第二批验收。\n", encoding="utf-8",
        )
        original = project / "docs"
        original.mkdir()
        self.call("UserPromptSubmit", mode="enforce", prompt="请整理交接并停机", cwd=str(project))
        patch = "*** Begin Patch\n*** Add File: docs/handoff.md\n+内容\n*** End Patch"
        self.call("PreToolUse", mode="enforce", tool_name="apply_patch", tool_input=patch, cwd=str(project))
        original.rmdir()
        try:
            original.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "New-Item -ItemType Junction -Path $env:UG_LINK -Target $env:UG_TARGET | Out-Null"],
                env={**os.environ, "UG_LINK": str(original), "UG_TARGET": str(outside)},
                capture_output=True, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        rejected = self.call("Stop", mode="enforce", cwd=str(project), last_assistant_message="文件已写")
        self.assertEqual(rejected["decision"], "block")
        self.assertFalse(self.state()["handoffRecorded"])

    def test_user_override_is_per_turn_and_does_not_trigger_from_quoted_history(self):
        self.call("PostCompact", mode="enforce", limit=1)
        self.call("UserPromptSubmit", mode="enforce", limit=1,
                  prompt='历史材料：“先别交接，继续当前对话”。请分析这句话。')
        self.assertEqual(self.state()["phase"], "drain")
        self.call("UserPromptSubmit", mode="enforce", limit=1, prompt="先别交接，继续当前对话")
        self.assertEqual(self.state()["phase"], "overridden")
        for index in range(2, 5):
            self.call("PostCompact", mode="enforce", limit=1, turn_id=f"turn-{index}")
        self.assertEqual(self.state()["phase"], "drain")
        self.call("UserPromptSubmit", mode="enforce", limit=1, turn_id="turn-5", prompt="先别交接，继续当前对话")
        self.assertEqual(self.state()["phase"], "overridden")
        self.call("UserPromptSubmit", mode="enforce", limit=1, turn_id="turn-6", prompt="历史材料：请整理交接并停机。这是以前的原话，请分析。")
        self.assertEqual(self.state()["phase"], "overridden")

    def test_subagent_missing_identity_and_malformed_state_do_not_contaminate_parent(self):
        self.assertEqual(self.call("PostCompact", agent_id="child-agent"), {})
        self.assertFalse(self.state_dir.exists())
        self.assertIn("systemMessage", self.call("PostCompact", session_id="../escape"))
        self.assertFalse(self.state_dir.exists())
        self.assertIn("systemMessage", self.call("PostCompact", transcript_path=""))
        self.assertFalse(self.state_dir.exists())
        self.call("PostCompact")
        state_path = self.state_dir / (self.sid + "_handoff_gate.json")
        state_path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.call("PostCompact")
        self.assertEqual(state_path.read_text(encoding="utf-8"), "{bad")

    def test_concurrent_compactions_and_strict_lock_timeout(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.call("PostCompact", turn_id=f"parallel-{n}"), range(32)))
        self.assertEqual(self.state()["compactionCount"], 32)
        anchor = self.state_dir / f".{self.sid}.lock"
        inode = anchor.stat().st_ino
        holder = (
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import user_words\n"
            "with user_words.session_state_lock(Path(sys.argv[2]), sys.argv[3]):\n"
            "    print('locked', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        with subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-c", holder, str(HOOKS_DIR), str(self.state_dir), self.sid],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as proc:
            try:
                self.assertEqual(proc.stdout.readline().strip(), "locked")
                os.utime(anchor, (1, 1))
                for strict in (True, False):
                    with self.assertRaises(TimeoutError):
                        with self.gate._gate_state_lock(self.state_dir, self.sid, timeout_sec=0.03, strict=strict):
                            self.fail("未取得锁时不得进入状态更新")
                self.assertEqual(anchor.stat().st_ino, inode)
                proc.kill()
            finally:
                proc.communicate(timeout=5)
        with self.gate._gate_state_lock(self.state_dir, self.sid, strict=True):
            # 替换 token 模拟迟到清理，离开旧上下文时不得清除不属于它的记录。
            with anchor.open("r+b") as stream:
                stream.seek(1)
                stream.write(b"x" * 32)
        self.assertEqual(anchor.read_bytes()[1:], b"x" * 32)
        with self.gate._gate_state_lock(self.state_dir, self.sid, strict=True):
            self.assertEqual(anchor.stat().st_ino, inode)
        self.assertEqual(anchor.read_bytes()[1:], b"")

    def test_compaction_reconciles_delayed_large_and_multiple_records(self):
        self.call("PostCompact", turn_id="delayed", trigger="auto")
        self.assertEqual(self.state()["compactionFallbackCount"], 1)
        _append_jsonl(self.transcript, {"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "delayed",
        }})
        _append_jsonl(self.transcript, {"type": "compacted", "ordinal": 3, "payload": {
            "window_id": "window-one", "replacement_history": ["x" * (300 * 1024)],
        }})
        _append_jsonl(self.transcript, {"type": "turn_context", "payload": {"turn_id": "delayed"}})
        self.call("UserPromptSubmit", turn_id="next")
        repaired = self.state()
        self.assertEqual(repaired["compactionCount"], 1)
        self.assertEqual(repaired["compactionRecordCount"], 1)
        self.assertEqual(repaired["compactionFallbackCount"], 0)
        self.assertEqual(repaired["compactionLedger"]["records"]["window:window-one"]["turnSource"], "following_turn_context")
        for index in (2, 3):
            _append_jsonl(self.transcript, {"type": "compacted", "payload": {"window_id": f"window-{index}"}})
        _append_jsonl(self.transcript, {"type": "response_item", "payload": {"output": "x" * (300 * 1024)}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.call("PostCompact", turn_id="delayed", trigger="auto"), range(8)))
        self.assertEqual(self.state()["compactionCount"], 3)
        self.assertEqual(self.state()["compactionRecordCount"], 3)
        self.assertEqual(self.state()["compactionFallbackCount"], 0)

    def test_delayed_turn_context_cannot_prematurely_trigger_drain(self):
        _append_jsonl(self.transcript, {"type": "turn_context", "payload": {"turn_id": "previous-turn"}})
        _append_jsonl(self.transcript, {"type": "compacted", "payload": {"window_id": "window-one"}})
        self.call("PostCompact", mode="enforce", limit=2, turn_id="current-turn", trigger="auto")
        early = self.state()
        self.assertEqual(early["compactionCount"], 1)
        self.assertEqual(early["phase"], "advisory")
        self.assertTrue(early["compactionIssues"])
        _append_jsonl(self.transcript, {"type": "turn_context", "payload": {"turn_id": "current-turn"}})
        self.call("SessionStart", mode="enforce", limit=2, source="compact")
        repaired = self.state()
        self.assertEqual(repaired["compactionCount"], 1)
        self.assertEqual(repaired["phase"], "advisory")
        self.assertEqual(repaired["compactionLedger"]["records"]["window:window-one"]["turn"], "current-turn")
        _append_jsonl(self.transcript, {"type": "compacted", "payload": {"window_id": "window-two"}})
        self.call("PostCompact", mode="enforce", limit=2, turn_id="current-turn", trigger="auto")
        self.assertEqual(self.state()["compactionCount"], 2)
        self.assertEqual(self.state()["phase"], "drain")

    def test_unassigned_compaction_does_not_duplicate_fallback(self):
        self.call("PostCompact", turn_id="undetermined", trigger="auto")
        row = b'{"type":"compacted","payload":{"window_id":"known-record"}}'
        with self.transcript.open("ab") as stream:
            stream.write(row[:-1])
        self.call("SessionStart", source="compact")
        self.assertEqual(self.state()["compactionRecordCount"], 0)
        self.assertTrue(self.state()["compactionIssues"])
        with self.transcript.open("ab") as stream:
            stream.write(row[-1:] + b"\n")
        self.call("SessionStart", source="compact")
        self.assertEqual(self.state()["compactionCount"], 1)
        self.assertEqual(self.state()["compactionRecordCount"], 1)
        self.assertEqual(self.state()["compactionFallbackCount"], 0)
        self.assertTrue(self.state()["compactionIssues"])

    def test_canonical_identity_is_shared_by_archive_and_gate(self):
        state_dir = self.root / "session-context"
        log = self.root / "archive.log"
        payload = {"session_id": self.sid, "transcript_path": str(self.transcript), "source": "resume"}
        self.assertEqual(session_context_loader.handle_codex_session_start(
            {**payload, "agent_id": "another-agent"}, str(state_dir), str(log),
        ), {})
        self.assertFalse(log.exists())
        self.transcript.write_text("", encoding="utf-8")
        _append_jsonl(self.transcript, {"type": "session_meta", "payload": {"id": self.sid, "source": "unknown"}})
        self.assertIn("来源未知", self.call("PostCompact")["systemMessage"])
        context = session_context_loader.handle_codex_session_start(payload, str(state_dir), str(log))
        self.assertIn("来源未知", context["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((self.root / "user-words").exists())

    def test_corrupted_agy_handoff_gate_json_does_not_abort_compaction_recovery(self):
        plugin_data = self.root / "agy-plugin-data"
        state_dir = plugin_data / "state"
        state_dir.mkdir(parents=True)
        cid = "agy-corrupt-gate"
        (state_dir / f"{cid}_handoff_gate.json").write_text("{corrupted", encoding="utf-8")
        tp = self.root / "agy-brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        _append_jsonl(tp, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>\n保留数值通量守恒\n</USER_REQUEST>"})
        _append_jsonl(tp, {"step_index": 12, "source": "SYSTEM", "type": "CHECKPOINT"})
        with unittest.mock.patch.dict(os.environ, {"PLUGIN_DATA": str(plugin_data)}):
            out = session_context_loader.handle_pre_invocation({"conversationId": cid, "transcriptPath": str(tp)})
        self.assertEqual(len(out["injectSteps"]), 1)
        self.assertIn("保留数值通量守恒", out["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertTrue((state_dir / f"{cid}_user_words.md").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)


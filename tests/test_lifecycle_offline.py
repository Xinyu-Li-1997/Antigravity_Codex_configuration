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


if __name__ == "__main__":
    unittest.main(verbosity=2)


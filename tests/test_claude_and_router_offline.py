"""Claude 生命周期、模型路由与 CC Switch 管理的纯离线回归。

临时文件仅写入仓库内的测试沙箱；HTTP、DNS、代理和上游响应均使用本地夹具。
"""

import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout, redirect_stderr
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = Path(os.environ.get("USER_GUARD_INSTALLED_DIR", str(REPO_ROOT / "template/user-guard"))) / "hooks"
sys.path.insert(0, str(HOOKS_DIR))
import handoff_gate
import reply_repeat_guard
import session_context_loader
import status_guard
import user_words


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = _load_module("claude_router_offline", REPO_ROOT / "template/claude/router/router.py")
admin = _load_module("ccswitch_admin_offline", REPO_ROOT / "template/claude/router/ccswitch_admin.py")
installer = _load_module("claude_installer_offline", REPO_ROOT / "install.py")


def _append_jsonl(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _user(text, **fields):
    return {"type": "user", "message": {"role": "user", "content": text},
            "timestamp": "2026-11-14T01:00:00Z", **fields}


HANDOFF_TEXT = (
    "已验证：离线回归通过。\nGit 工作区：仍有待提交改动。\n未完成：真实宿主接入验收。\n"
    "【新对话提示词】\n请先核对当前工作区差异与用户最新裁定，再继续完成真实宿主接入验收。\n"
)


class OfflineSandbox(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-offline-", dir=REPO_ROOT / "tests")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        self.claude_home = self.home / ".claude"
        self.state = self.claude_home / "hooks/state"
        self.logs = self.claude_home / "hooks/logs"
        self.sid = "claude_sess_alpha"
        self.transcript = self.claude_home / "projects/work" / f"{self.sid}.jsonl"
        self.payload = {"session_id": self.sid, "transcript_path": str(self.transcript), "cwd": str(self.project)}
        patch = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude_home), "HOME": str(self.home), "USERPROFILE": str(self.home),
            "PLUGIN_DATA": str(self.root / "plugin-data"), "USER_GUARD_STATE_DIR": str(self.state),
            "USER_GUARD_LOGS_DIR": str(self.logs), "CAE_HARNESS_DEPTH": "0", "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(self.root), "TMP": str(self.root), "TMPDIR": str(self.root),
        })
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(tempfile, "tempdir", str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        router._key_cache.clear()
        router._proxy_cache = None

    def cli(self, script, payload, *args):
        raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(HOOKS_DIR / script), "--client", "claude", *map(str, args)],
            input=raw, capture_output=True, text=True, encoding="utf-8", cwd=self.project, timeout=20,
        )

    def tool(self, name, **args):
        return {**self.payload, "hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args}

    def guard(self, name, **args):
        return status_guard.handle_claude_pre_tool_use(self.tool(name, **args), self.state, self.logs)

    def archive(self):
        return user_words.archive_claude_session_words(self.payload, self.state / "session-context")

    def gate(self, event, mode="enforce", limit=2, **fields):
        return handoff_gate.handle_claude_handoff(
            {**self.payload, "hook_event_name": event, **fields}, self.state / "handoff", mode, limit,
        )

    def gate_state(self):
        return json.loads((self.state / "handoff" / f"{self.sid}_handoff_gate.json").read_text(encoding="utf-8"))

    def boundaries(self, count=2):
        for index in range(count):
            _append_jsonl(self.transcript, {"type": "system", "subtype": "compact_boundary",
                                           "uuid": f"boundary_{index}"})

    def database(self, version=26):
        path = self.root / "cc-switch.db"
        with closing(sqlite3.connect(path)) as con:
            con.executescript("""
                CREATE TABLE providers (
                    id TEXT PRIMARY KEY, app_type TEXT, name TEXT, settings_config TEXT,
                    is_current INTEGER DEFAULT 0, sort_index INTEGER, created_at INTEGER,
                    notes TEXT, meta TEXT, category TEXT, in_failover_queue INTEGER DEFAULT 0,
                    cost_multiplier TEXT DEFAULT '1.0', website_url TEXT, icon TEXT, icon_color TEXT,
                    limit_daily_usd REAL, limit_monthly_usd REAL, provider_type TEXT
                );
                CREATE TABLE prompts (id TEXT PRIMARY KEY, app_type TEXT, name TEXT, enabled INTEGER, content TEXT);
            """)
            con.execute(f"PRAGMA user_version={int(version)}")
            official = {"env": {"ANTHROPIC_AUTH_TOKEN": "tok_official_test", "ANTHROPIC_MODEL": "official-model",
                                "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "ENABLE_TOOL_SEARCH": "false"}}
            con.execute("INSERT INTO providers (id,app_type,name,settings_config,is_current,sort_index,created_at) "
                        "VALUES (?,?,?,?,?,?,?)", ("claude-official", "claude", "Official", json.dumps(official), 1, 1, 1))
            con.execute("INSERT INTO providers (id,app_type,name,settings_config,is_current) VALUES (?,?,?,?,?)",
                        ("codex_official", "codex", "Other", "{}", 1))
            con.commit()
        return path

    def settings(self):
        path = self.claude_home / "settings.json"
        user_words.atomic_write_json(path, {"hooks": {"Stop": [{"hooks": []}]}, "effortLevel": "xhigh",
                                           "env": {"PERSONAL_SETTING": "keep"}})
        return path

    def cards(self, db):
        with closing(sqlite3.connect(db)) as con:
            return {name: json.loads(raw) for name, raw in con.execute(
                "SELECT name, settings_config FROM providers WHERE app_type='claude'")}

    def replace_card(self, db, name, config):
        with closing(sqlite3.connect(db)) as con:
            con.execute("UPDATE providers SET settings_config=? WHERE name=? AND app_type='claude'",
                        (json.dumps(config), name))
            con.commit()


class TestClaudeArchive(OfflineSandbox):
    def test_identity_validation_and_missing_transcript(self):
        self.assertEqual(user_words.claude_hook_session({"session_id": "../escape"}),
                         ("", None, "缺少合法 session_id", False))
        self.assertEqual(user_words.claude_hook_session(self.payload),
                         (self.sid, None, "未找到 Claude 会话轨迹文件", False))
        self.assertFalse(self.archive()["complete"])

    def test_discovery_uses_latest_main_transcript_only(self):
        _append_jsonl(self.transcript, _user("较早记录"))
        later = self.claude_home / "projects/other" / f"{self.sid}.jsonl"
        _append_jsonl(later, _user("较新记录"))
        os.utime(self.transcript, ns=(1_000_000_000, 1_000_000_000))
        os.utime(later, ns=(2_000_000_000, 2_000_000_000))
        _append_jsonl(later.parent / "subagents" / f"{self.sid}.jsonl", _user("子代理记录"))
        self.assertEqual(user_words.claude_hook_session({"session_id": self.sid})[1], later)
        self.assertIsNone(user_words.claude_hook_session({**self.payload, "transcript_path": str(self.root / "missing.jsonl")})[1])

    def test_plain_text_blocks_timestamp_and_injected_noise(self):
        _append_jsonl(self.transcript, _user("<ide_opened_file>source.py</ide_opened_file>请检查守恒残差。"))
        _append_jsonl(self.transcript, _user([
            {"type": "text", "text": "<ide_selection>无关选区</ide_selection>请保留容差。"},
            {"type": "text", "text": "<system-reminder>自动提示</system-reminder><ide_diagnostics>诊断</ide_diagnostics>"},
        ]))
        result = user_words.extract_claude_user_words(self.transcript)
        self.assertTrue(result["complete"])
        self.assertEqual([word["content"] for word in result["words"]], ["请检查守恒残差。", "请保留容差。"])
        self.assertEqual(result["words"][0]["created_at"], "2026-11-14T01:00:00Z")

    def test_sidechain_meta_tool_output_and_local_echo_are_filtered(self):
        for entry in [
            _user("子代理消息", isSidechain=True), _user("系统注入", isMeta=True),
            _user([{"type": "tool_result", "content": "工具输出"}]),
            _user("<local-command-stdout>回显</local-command-stdout>"),
            _user("<local-command-caveat>本地警告</local-command-caveat>"),
            _user("<ide_opened_file>只有注入内容</ide_opened_file>"),
            _user("真实指令"),
        ]:
            _append_jsonl(self.transcript, entry)
        self.assertEqual([word["content"] for word in self.archive()["words"]], ["真实指令"])

    def test_slash_commands_are_preserved_with_arguments(self):
        _append_jsonl(self.transcript, _user("<command-name>/review</command-name><command-args>solver.py</command-args>"))
        self.assertEqual(self.archive()["words"][0]["content"], "/review solver.py")

    def test_question_answers_from_entry_and_result_blocks(self):
        for qid in ("toolu_q1", "toolu_q2", "toolu_q3"):
            _append_jsonl(self.transcript, {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": qid, "name": "AskUserQuestion", "input": {"questions": []}}]}})
        _append_jsonl(self.transcript, {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_read_1", "name": "Read", "input": {"file_path": "fixture.json"}}]}})
        _append_jsonl(self.transcript, _user([{"type": "tool_result", "tool_use_id": "toolu_q1", "content": "选择已提交"}],
                                           toolUseResult={"answers": {"格式": "DGSEM", "检查": ["守恒", "收敛阶"]}}))
        _append_jsonl(self.transcript, _user([{"type": "tool_result", "tool_use_id": "toolu_q2",
                                             "content": json.dumps({"answers": {"下一步": "运行回归"}})}]))
        _append_jsonl(self.transcript, _user([{"type": "tool_result", "tool_use_id": "toolu_q3", "content": [
            {"type": "text", "text": 'User has answered your questions: "方案"="保留现状". You can now continue with the user\'s answers in mind.'}]}]))
        _append_jsonl(self.transcript, _user([{"type": "tool_result", "tool_use_id": "toolu_read_1",
                                             "content": json.dumps({"answers": {"伪造": "不应归档"}})}],
                                           toolUseResult={"answers": {"伪造": "不应归档"}}))
        result = self.archive()
        self.assertEqual(len(result["words"]), 3)
        self.assertTrue(all(word["kind"] == "ask_question_answer" for word in result["words"]))
        self.assertIn("检查：守恒、收敛阶", result["words"][0]["content"])
        self.assertIn("下一步：运行回归", result["words"][1]["content"])
        self.assertIn("保留现状", result["words"][2]["content"])

    def test_queued_human_input_deduplicates_only_consumed_copy(self):
        _append_jsonl(self.transcript, {"type": "attachment", "attachment": {
            "type": "queued_command", "origin": {"kind": "human"}, "prompt": "检查接口"}})
        _append_jsonl(self.transcript, _user("检查接口"))
        _append_jsonl(self.transcript, _user("检查接口"))
        _append_jsonl(self.transcript, {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "未消费指令"}})
        _append_jsonl(self.transcript, {"type": "attachment", "attachment": {
            "type": "queued_command", "origin": {"kind": "agent"}, "prompt": "自动任务"}})
        self.assertEqual([word["content"] for word in self.archive()["words"]], ["检查接口", "检查接口", "未消费指令"])

    def test_archive_directory_matches_codex_rule_and_manifest_digest(self):
        _append_jsonl(self.transcript, _user("原话正文"))
        for directory in (self.state / "session-context", self.state, self.root / "custom-state"):
            with self.subTest(directory=directory.name):
                result = user_words.archive_claude_session_words(self.payload, directory)
                expected_base = directory.parent if directory.name == "session-context" else directory
                path = expected_base / "user-words" / f"{self.sid}.md"
                self.assertEqual(Path(result["saved"]["file"]), path)
                manifest = json.loads(Path(result["meta_path"]).read_text(encoding="utf-8"))
                self.assertTrue(str(result["meta_path"]).endswith(".meta.json"))
                self.assertEqual(manifest["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertTrue(result["saved"]["verified"])

    def test_bad_json_and_utf8_preserve_verified_archive(self):
        _append_jsonl(self.transcript, _user("已验证的用户裁定"))
        initial = self.archive()
        md = Path(initial["saved"]["file"])
        meta = Path(initial["meta_path"])
        original = md.read_bytes(), meta.read_bytes()
        with self.transcript.open("ab") as stream:
            stream.write(b'broken-json\n\xff\n{"type":')
        result = self.archive()
        self.assertFalse(result["complete"])
        self.assertTrue(result["issues"])
        self.assertTrue(result["saved"]["preserved"])
        self.assertEqual((md.read_bytes(), meta.read_bytes()), original)

    def test_missing_or_rewritten_source_preserves_verified_history(self):
        _append_jsonl(self.transcript, _user("第一轮约束"))
        _append_jsonl(self.transcript, _user("第二轮约束"))
        initial = self.archive()
        path = Path(initial["saved"]["file"])
        original = path.read_bytes()
        self.transcript.write_bytes(b"")
        _append_jsonl(self.transcript, _user("压缩后仅可见新输入"))
        self.assertTrue(self.archive()["saved"]["preserved"])
        self.transcript.unlink()
        self.assertTrue(self.archive()["saved"]["preserved"])
        self.assertEqual(path.read_bytes(), original)

    def test_bom_unicode_separator_and_nonobject_corruption(self):
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_bytes(b"\xef\xbb\xbf" + json.dumps(_user("正文\u2028保留"), ensure_ascii=False).encode() + b"\n[]\n")
        result = user_words.extract_claude_user_words(self.transcript)
        self.assertEqual(result["words"][0]["content"], "正文\u2028保留")
        self.assertFalse(result["complete"])

    def test_session_start_function_and_cli_return_native_json_and_log(self):
        _append_jsonl(self.transcript, _user("用中文核对离散格式。"))
        log = self.logs / "context.jsonl"
        direct = session_context_loader.handle_claude_session_start(self.payload, str(self.state / "session-context"), str(log))
        proc = self.cli("session_context_loader.py", {**self.payload, "source": "compact"},
                        "--state-dir", self.state / "session-context", "--log", log)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        response = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(response["hookEventName"], "SessionStart")
        self.assertIn("恢复来源：Claude compact", response["additionalContext"])
        self.assertIn(str(self.state / "user-words" / f"{self.sid}.md"), response["additionalContext"])
        self.assertIn("SHA-256", direct["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["client"], "claude")

    def test_context_budget_keeps_initial_and_recent_instructions(self):
        for index in range(20):
            _append_jsonl(self.transcript, _user(f"裁定_{index}：" + "必须核对原始证据。" * 250))
        result = session_context_loader.handle_claude_session_start(self.payload, str(self.state / "session-context"))
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), 8000)
        self.assertIn("裁定_0：", text)
        self.assertIn("裁定_19：", text)
        result = session_context_loader.handle_claude_session_start({**self.payload, "source": "s" * 12000}, str(self.state))
        self.assertLessEqual(len(result["hookSpecificOutput"]["additionalContext"]), 8000)

    def test_loader_bad_stdin_exits_two_and_runtime_error_degrades(self):
        for raw in ("{", "[]"):
            proc = self.cli("session_context_loader.py", raw, "--state-dir", self.state)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("JSON parse error", proc.stderr)
            self.assertEqual(proc.stdout, "")
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["session_context_loader.py", "--client", "claude"]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.payload))), \
                mock.patch.object(sys, "stdout", output), \
                mock.patch.object(user_words, "archive_claude_session_words", side_effect=OSError("测试读取失败")):
            session_context_loader.main()
        self.assertIn("提取状态：异常", json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"])


class TestClaudeStatusGuard(OfflineSandbox):
    def test_project_writes_edits_notebook_and_commands_are_allowed(self):
        for name, args in (
            ("Write", {"file_path": str(self.project / "solver.py")}),
            ("Edit", {"file_path": "solver.py"}), ("MultiEdit", {"path": "solver.py"}),
            ("NotebookEdit", {"notebook_path": "analysis.ipynb"}),
            ("Bash", {"command": "echo ok > output.txt"}),
            ("PowerShell", {"command": "Set-Content -LiteralPath solver.py -Value ok"}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.guard(name, **args), {})

    def test_only_claude_runtime_directories_are_exempt(self):
        for name in ("projects", "todos", "session-env", "statsig", "telemetry", "hooks/state", "hooks/logs"):
            with self.subTest(name=name):
                self.assertEqual(self.guard("Write", file_path=str(self.claude_home / name / "record.json")), {})
        for name in ("CLAUDE.md", "settings.json", "settings.local.json", "agents/reader.md", "hooks/status_guard.py", "router/router.py"):
            with self.subTest(name=name):
                response = self.guard("Write", file_path=str(self.claude_home / name))["hookSpecificOutput"]
                self.assertEqual(response["permissionDecision"], "ask")
                self.assertIn("项目外目标路径", response["permissionDecisionReason"])

    def test_external_mutation_asks_interactively_and_denies_all_headless_depths(self):
        outside = str(self.root / "business/result.txt")
        for depth, expected in (("", "ask"), ("0", "ask"), ("1", "deny"), ("2", "deny")):
            with self.subTest(depth=depth), mock.patch.dict(os.environ, {"CAE_HARNESS_DEPTH": depth}):
                self.assertEqual(self.guard("Write", file_path=outside)["hookSpecificOutput"]["permissionDecision"], expected)

    def test_inline_python_ast_worktree_and_installer_external_writes(self):
        commands = [
            "python -c \"from pathlib import Path; root=Path('../business'); (root/'out.txt').write_text('x')\"",
            "git worktree add ../business -b branch_unit",
            "python -B install.py --yes",
        ]
        for command in commands:
            with self.subTest(command=command):
                response = self.guard("Bash", command=command, description="更新测试产物")["hookSpecificOutput"]
                self.assertEqual(response["permissionDecision"], "ask")
                self.assertIn("触发语句", response["permissionDecisionReason"])
                self.assertIn("更新测试产物", response["permissionDecisionReason"])

    def test_read_same_log_denied_but_pagination_and_appends_allowed(self):
        path = self.project / "solver.log"
        path.write_bytes(b"step 1\n")
        args = {"file_path": str(path), "offset": 1, "limit": 20}
        self.assertEqual(self.guard("Read", **args), {})
        result = self.guard("Read", **args)["hookSpecificOutput"]
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertNotIn("Reactive Wakeup", result["permissionDecisionReason"])
        self.assertNotIn("反重力", result["permissionDecisionReason"])
        self.assertEqual(self.guard("Read", **{**args, "offset": 21}), {})
        self.assertEqual(self.guard("Read", **{**args, "limit": 40}), {})
        with path.open("ab") as stream:
            stream.write(b"step 2\n")
        self.assertEqual(self.guard("Read", **args), {})

    def test_grep_queries_include_all_parameters_and_directory_evidence(self):
        (self.project / "solver.log").write_bytes(b"residual=0.01\n")
        args = {"path": str(self.project), "glob": "*.log", "pattern": "residual", "output_mode": "content", "head_limit": 10, "offset": 0}
        self.assertEqual(self.guard("Grep", **args), {})
        self.assertEqual(self.guard("Grep", **args)["hookSpecificOutput"]["permissionDecision"], "deny")
        for key, value in (("pattern", "step"), ("head_limit", 20), ("output_mode", "files_with_matches"), ("offset", 10), ("glob", "solver*.log")):
            with self.subTest(key=key):
                self.assertEqual(self.guard("Grep", **{**args, key: value}), {})
        (self.project / "other.log").write_bytes(b"new evidence\n")
        self.assertEqual(self.guard("Grep", **args), {})

    def test_bash_tail_and_powershell_log_queries_track_changes(self):
        path = self.project / "solver.log"
        path.write_bytes(b"step 1\n")
        for name, command in (("Bash", "tail -n 20 solver.log"), ("PowerShell", "Get-Content solver.log -Tail 20")):
            with self.subTest(name=name):
                self.assertEqual(self.guard(name, command=command), {})
                self.assertEqual(self.guard(name, command=command)["hookSpecificOutput"]["permissionDecision"], "deny")
                with path.open("ab") as stream:
                    stream.write(b"next step\n")
                self.assertEqual(self.guard(name, command=command), {})

    def test_source_files_and_own_runtime_logs_do_not_poll_block(self):
        for path in (self.project / "solver.py", self.logs / "lifecycle.log"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"content\n")
            for _ in range(3):
                self.assertEqual(self.guard("Read", file_path=str(path)), {})

    def test_cli_native_ask_deny_and_allowed_json(self):
        payload = self.tool("Write", file_path=str(self.claude_home / "CLAUDE.md"))
        proc = self.cli("status_guard.py", payload, "--state-dir", self.state, "--log-dir", self.logs, "--unused-flag")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        with mock.patch.dict(os.environ, {"CAE_HARNESS_DEPTH": "1"}):
            proc = self.cli("status_guard.py", payload, "--state-dir", self.state, "--log-dir", self.logs)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        proc = self.cli("status_guard.py", self.tool("Edit", file_path="solver.py"), "--state-dir", self.state, "--log-dir", self.logs)
        self.assertEqual(json.loads(proc.stdout), {})


class TestClaudeReplyGuard(OfflineSandbox):
    def reply(self, text, **fields):
        return reply_repeat_guard.handle_claude_stop_repeat_guard(
            {"session_id": self.sid, "last_assistant_message": text, **fields}, self.state, self.logs,
        )

    def assistant(self, text, identity):
        _append_jsonl(self.transcript, {"type": "assistant", "uuid": identity,
                                       "message": {"content": [{"type": "text", "text": text}]}})

    def test_long_replies_and_questions_are_allowed(self):
        for text in ("任务仍在运行，" + "守恒残差与参考解已逐项核对。" * 40, "仍在运行，是否继续等待？"):
            for _ in range(4):
                self.assertEqual(self.reply(text), {})

    def test_changing_steps_residuals_and_task_subjects_are_allowed(self):
        for step in range(5):
            self.assertEqual(self.reply(f"仍在计算，第 {step} 步，残差 {1 / (step + 1):.3e}。"), {})
        for text in ("求解器仍在运行。", "回归测试仍在运行。", "安装流程仍在运行。"):
            self.assertEqual(self.reply(text), {})

    def test_three_homogeneous_wait_replies_block_once_without_transcript(self):
        replies = ["后台任务仍在运行，请稍候。", "后台任务仍在运行，继续等待。", "后台任务仍在运行，请稍候。"]
        self.assertEqual(self.reply(replies[0]), {})
        self.assertEqual(self.reply(replies[1]), {})
        self.assertEqual(self.reply(replies[2]), {"decision": "block", "reason": reply_repeat_guard.BLOCK_REASON_TEXT})
        self.assertEqual(self.reply(replies[2]), {})

    def test_stop_hook_active_and_invalid_session_do_no_state_work(self):
        with mock.patch.object(user_words, "claude_hook_session") as resolver:
            self.assertEqual(self.reply("仍在运行。", stop_hook_active=True), {})
            resolver.assert_not_called()
        self.assertEqual(self.reply("仍在运行。", session_id="../invalid"), {})
        self.assertFalse(self.state.exists())

    def test_transcript_message_idempotency_and_tool_result_filtering(self):
        _append_jsonl(self.transcript, _user("继续检查", uuid="user_alpha"))
        for index in range(3):
            self.assistant("后台任务仍在运行。", f"assistant_{index}")
            _append_jsonl(self.transcript, _user([{"type": "tool_result", "content": "工具结果"}]))
            result = self.reply("后台任务仍在运行。", transcript_path=str(self.transcript))
            self.assertEqual(result.get("decision"), "block" if index == 2 else None)
            for _ in range(2):
                self.assertEqual(self.reply("后台任务仍在运行。", transcript_path=str(self.transcript)), {})

    def test_new_user_turn_resets_single_block_budget(self):
        for turn in range(2):
            _append_jsonl(self.transcript, _user("同样的用户正文", uuid=f"user_{turn}"))
            for index in range(3):
                self.assistant("仍在计算，等待结果。", f"assistant_{turn}_{index}")
                result = reply_repeat_guard.handle_claude_stop_repeat_guard(self.payload, self.state, self.logs)
                self.assertEqual(result.get("decision"), "block" if index == 2 else None)

    def test_tail_scan_crosses_large_turn_and_ignores_meta_sidechain(self):
        _append_jsonl(self.transcript, _user("主回合", uuid="main_user"))
        _append_jsonl(self.transcript, {"type": "progress", "content": "x" * (reply_repeat_guard.TAIL_SCAN_BYTES + 200)})
        for index in range(3):
            _append_jsonl(self.transcript, _user("不是用户回合", isMeta=True))
            _append_jsonl(self.transcript, _user("子代理回合", isSidechain=True))
            self.assistant("后台任务仍在运行。", f"main_assistant_{index}")
            result = reply_repeat_guard.handle_claude_stop_repeat_guard(self.payload, self.state, self.logs)
            self.assertEqual(result.get("decision"), "block" if index == 2 else None)

    def test_cli_uses_transcript_reply_and_active_stop_never_blocks(self):
        _append_jsonl(self.transcript, _user("运行回归", uuid="cli_user"))
        for index in range(3):
            self.assistant("后台任务仍在运行。", f"cli_assistant_{index}")
            proc = self.cli("reply_repeat_guard.py", self.payload, "--state-dir", self.state, "--log-dir", self.logs)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout).get("decision"), "block" if index == 2 else None)
        proc = self.cli("reply_repeat_guard.py", {**self.payload, "stop_hook_active": True}, "--state-dir", self.state, "--log-dir", self.logs)
        self.assertEqual(json.loads(proc.stdout), {})


class TestClaudeHandoff(OfflineSandbox):
    def test_advisory_never_denies_and_off_has_no_side_effects(self):
        self.assertEqual(self.gate("PreToolUse", mode="off", tool_name="Agent"), {})
        self.assertFalse(self.state.exists())
        self.boundaries(4)
        self.gate("SessionStart", mode="advisory", source="compact")
        self.assertEqual(self.gate("PreToolUse", mode="advisory", tool_name="Agent"), {})
        self.assertEqual(self.gate("Stop", mode="advisory", last_assistant_message="仍在运行"), {})
        self.assertEqual(self.gate_state()["phase"], "advisory")
        self.assertEqual(self.gate_state()["client"], "claude")

    def test_postcompact_protocol_and_duplicate_boundary_observations(self):
        self.boundaries(1)
        response = self.gate("PostCompact", mode="advisory")
        self.assertIn("systemMessage", response)
        self.assertNotIn("hookSpecificOutput", response)
        self.gate("SessionStart", mode="advisory", source="compact")
        self.gate("PostCompact", mode="advisory")
        self.assertEqual(self.gate_state()["compactionCount"], 1)

    def test_event_before_boundary_is_merged_and_microcompact_counted(self):
        _append_jsonl(self.transcript, _user("继续"))
        self.gate("PostCompact", mode="advisory")
        self.gate("SessionStart", mode="advisory", source="compact")
        self.assertEqual(self.gate_state()["compactionCount"], 1)
        self.boundaries(1)
        self.gate("PreToolUse", mode="advisory", tool_name="Read")
        self.assertEqual(self.gate_state()["compactionCount"], 1)
        _append_jsonl(self.transcript, {"type": "system", "subtype": "microcompact_boundary", "uuid": "micro_alpha"})
        self.gate("PostCompact", mode="advisory")
        self.assertEqual(self.gate_state()["compactionCount"], 2)

    def test_enforce_drain_freeze_and_mutating_tool_budget(self):
        self.boundaries()
        response = self.gate("SessionStart", source="compact")["hookSpecificOutput"]
        self.assertEqual(response["hookEventName"], "SessionStart")
        self.assertEqual(self.gate_state()["phase"], "drain")
        for index in range(handoff_gate.CODEX_DRAIN_ACTIONS):
            self.gate("PreToolUse", tool_name="Write", tool_input={"file_path": "solver.py"}, tool_use_id=f"write_{index}")
        self.assertEqual(self.gate_state()["phase"], "freeze")
        response = self.gate("PreToolUse", tool_name="Write", tool_input={"file_path": "solver.py"})
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_agent_push_and_harness_dispatch_are_blocked(self):
        self.boundaries()
        for name, args in (("Agent", {}), ("Task", {}), ("Subagent", {}),
                           ("Bash", {"command": "git push origin main"}),
                           ("PowerShell", {"command": "python -B cae_harness.py task --task work"})):
            with self.subTest(name=name):
                result = self.gate("PreToolUse", tool_name=name, tool_input=args)["hookSpecificOutput"]
                self.assertEqual(result["permissionDecision"], "deny")
        self.assertEqual(self.gate_state()["drainActionsRemaining"], handoff_gate.CODEX_DRAIN_ACTIONS)

    def test_readonly_local_git_and_handoff_document_are_exempt(self):
        self.boundaries()
        self.gate("SessionStart", source="compact")
        for name, args in (("Read", {}), ("AskUserQuestion", {}), ("TodoWrite", {}),
                           ("Bash", {"command": "git status --short"}), ("Bash", {"command": "git add . && git commit -m unit"}),
                           ("Write", {"file_path": "HANDOFF.md"}), ("Bash", {"command": "echo progress > handoff.txt"})):
            self.assertEqual(self.gate("PreToolUse", tool_name=name, tool_input=args), {})
        self.assertEqual(self.gate_state()["drainActionsRemaining"], handoff_gate.CODEX_DRAIN_ACTIONS)
        (self.project / "HANDOFF.md").write_text(HANDOFF_TEXT, encoding="utf-8", newline="\n")
        self.assertEqual(self.gate("Stop"), {})
        self.assertTrue(self.gate_state()["handoffRecorded"])

    def test_handoff_path_escape_code_suffix_and_mixed_shell_not_exempt(self):
        for name, args in (("Write", {"file_path": "../handoff.md"}), ("Edit", {"file_path": "resume_solver.py"}),
                           ("Bash", {"command": "echo ready > handoff.md; python solver.py"})):
            self.assertEqual(handoff_gate._claude_tool_kind(self.tool(name, **args))[0], "mutating_other")

    def test_user_override_grace_and_quoted_text_do_not_conflict(self):
        self.boundaries()
        self.gate("SessionStart", source="compact")
        self.gate("UserPromptSubmit", prompt="文档引用：暂缓交接", turn_id="quoted_turn")
        self.assertEqual(self.gate_state()["phase"], "drain")
        response = self.gate("UserPromptSubmit", prompt="暂缓交接", turn_id="override_turn")
        self.assertIn("additionalContext", response["hookSpecificOutput"])
        self.assertEqual(self.gate_state()["phase"], "overridden")
        self.assertEqual(self.gate("PreToolUse", tool_name="Agent"), {})
        for index in range(3):
            _append_jsonl(self.transcript, {"type": "system", "subtype": "compact_boundary", "uuid": f"grace_{index}"})
        self.gate("PostCompact")
        self.assertEqual(self.gate_state()["phase"], "drain")

    def test_stop_requests_correction_once_and_never_loops(self):
        self.boundaries()
        self.assertEqual(self.gate("Stop", last_assistant_message="尚未交接")["decision"], "block")
        self.assertTrue(self.gate_state()["stopCorrectionRequested"])
        result = self.gate("Stop", last_assistant_message="尚未交接")
        self.assertNotIn("decision", result)
        self.assertNotIn("continue", result)
        self.assertEqual(self.gate_state()["phase"], "freeze")
        result = self.gate("Stop", last_assistant_message="尚未交接", stop_hook_active=True)
        self.assertNotIn("decision", result)

    def test_valid_reply_freezes_and_tool_delivery_is_idempotent(self):
        self.boundaries()
        self.gate("PreToolUse", tool_name="Bash", tool_input={"command": "python solver.py"}, tool_use_id="run_alpha")
        before = self.gate_state()["drainActionsRemaining"]
        self.gate("PreToolUse", tool_name="Bash", tool_input={"command": "python solver.py"}, tool_use_id="run_alpha")
        self.assertEqual(self.gate_state()["drainActionsRemaining"], before)
        self.assertEqual(self.gate("Stop", last_assistant_message=HANDOFF_TEXT), {})
        self.assertEqual(self.gate_state()["phase"], "freeze")

    def test_cli_postcompact_uses_standard_output_and_unknown_events_skip(self):
        self.boundaries()
        proc = self.cli("handoff_gate.py", {**self.payload, "hook_event_name": "PostCompact"},
                        "--state-dir", self.state / "handoff", "--mode", "advisory", "--compact-limit", 0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(set(json.loads(proc.stdout)), {"systemMessage"})
        self.assertEqual(self.gate("UnrecognizedEvent"), {})


class TestClaudeRouter(OfflineSandbox):
    def handler(self, data=None):
        handler = object.__new__(router.Handler)
        cfg = copy.deepcopy(router.DEFAULTS)
        cfg.update(ccswitch_db=str(self.root / "cards.db"), log=str(self.logs / "router.log"),
                   default_key_from_card="Kimi", kimi_effort_map={"xhigh": "max"})
        handler.server = SimpleNamespace(cfg=cfg)
        handler.path = "/v1/messages?stream=true"
        handler.command = "POST"
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Authorization"] = "Bearer incoming_unit"
        handler.headers["x-api-key"] = "incoming_unit"
        body = json.dumps(data or {}).encode()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_error = mock.Mock()
        return handler

    def upstream(self, chunks=None):
        response = mock.Mock(status=200, reason="OK")
        response.getheaders.return_value = [("Content-Type", "text/event-stream"), ("Transfer-Encoding", "chunked"),
                                            ("Connection", "X-Private"), ("X-Private", "strip"), ("X-Trace", "keep")]
        response.read1.side_effect = chunks or [b"data: first\n\n", b"data: second\n\n", b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        return connection

    def test_default_config_path_and_loading_are_isolated(self):
        with mock.patch.object(router, "HERE", str(self.root)):
            platform_path = self.root / ("router.windows.json" if os.name == "nt" else "router.wsl.json")
            platform_path.write_bytes(b'\xef\xbb\xbf{"port":8250}')
            self.assertEqual(Path(router.default_config_path()), platform_path)
            primary = self.root / "router.json"
            primary.write_bytes(b'{"port":8251}')
            self.assertEqual(Path(router.default_config_path()), primary)
            cfg = router.load_config(str(platform_path))
            self.assertEqual(cfg["port"], 8250)
            cfg["routes"].clear()
            self.assertTrue(router.DEFAULTS["routes"])
            primary.write_bytes(b'{"follow_windows_proxy":"yes"}')
            with self.assertRaises(ValueError):
                router.load_config(str(primary))

    def test_windows_manual_proxy_scheme_selection_and_pac(self):
        value = {"enabled": True, "server": "http=127.0.0.1:8080;https=127.0.0.1:8443", "override": "", "pac": ""}
        with mock.patch.object(router, "_windows_proxy_settings", return_value=value), \
                mock.patch.object(router, "_probe_proxy_port") as probe:
            self.assertEqual(router.windows_proxy_for("https://example.org").port, 8443)
            self.assertEqual(router.windows_proxy_for("http://example.org").port, 8080)
            probe.assert_not_called()
            value["pac"] = "https://example.org/proxy.pac"
            self.assertIsNone(router.windows_proxy_for("https://example.org"))

    def test_disabled_proxy_requires_loopback_fake_ip_and_live_port(self):
        value = {"enabled": False, "server": "127.0.0.1:8080", "override": "", "pac": ""}
        with mock.patch.object(router, "_windows_proxy_settings", return_value=value), \
                mock.patch.object(router, "_probe_proxy_port", return_value=True) as probe:
            self.assertEqual(router.windows_proxy_for("https://198.18.0.14").hostname, "127.0.0.1")
            probe.assert_called_once_with("127.0.0.1", 8080)
            self.assertIsNone(router.windows_proxy_for("https://192.0.2.10"))
            probe.return_value = False
            self.assertIsNone(router.windows_proxy_for("https://198.18.0.14"))
            value["server"] = "192.0.2.10:8080"
            probe.return_value = True
            self.assertIsNone(router.windows_proxy_for("https://198.18.0.14"))

    def test_proxy_bypass_local_wildcards_and_ipv6(self):
        for host in ("printer", "solver.local", "::1", "2001:db8::1"):
            self.assertTrue(router.proxy_bypassed(host, "<local>;*.local;[::1];[2001:db8::1]"))
        self.assertFalse(router.proxy_bypassed("example.org", "<local>;*.local"))
        with mock.patch.object(router, "_windows_proxy_settings", return_value={
            "enabled": True, "server": "[::1]:8080", "override": "*.local", "pac": "",
        }):
            self.assertIsNone(router.windows_proxy_for("https://solver.local"))
            self.assertEqual(router.windows_proxy_for("https://example.org").hostname, "::1")

    def test_fake_ip_dns_and_port_probe_never_contact_external_network(self):
        with mock.patch.object(router.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.14", 0)),
        ]):
            self.assertTrue(router._resolves_to_fake_ip("unit.example"))
        with mock.patch.object(router.socket, "getaddrinfo", side_effect=OSError):
            self.assertFalse(router._resolves_to_fake_ip("unit.example"))
        with mock.patch.object(router.socket, "create_connection") as connect:
            self.assertTrue(router._probe_proxy_port("127.0.0.1", 8080))
            connect.return_value.close.assert_called_once()
        with mock.patch.object(router.socket, "create_connection", side_effect=OSError):
            self.assertFalse(router._probe_proxy_port("127.0.0.1", 8080))

    def test_proxy_registry_lookup_and_five_second_cache(self):
        with mock.patch.object(router.shutil, "which", return_value="powershell.exe"), \
                mock.patch.object(router.subprocess, "run", return_value=SimpleNamespace(stdout=b'{"enabled":true,"server":"127.0.0.1:8080"}')):
            self.assertTrue(router._read_windows_proxy_settings()["enabled"])
        clock = [0.0]
        with mock.patch.object(router.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(router, "_read_windows_proxy_settings", return_value={"enabled": False}) as read:
            router._windows_proxy_settings()
            clock[0] = 4.0
            router._windows_proxy_settings()
            self.assertEqual(read.call_count, 1)
            clock[0] = 6.0
            router._windows_proxy_settings()
            self.assertEqual(read.call_count, 2)

    def test_invalid_proxy_endpoints_and_cached_errors(self):
        for server in ("socks5://127.0.0.1:1080", "http://alice:token@127.0.0.1:8080", "127.0.0.1:0", "http://127.0.0.1:8080/path"):
            with self.subTest(server=server), mock.patch.object(router, "_windows_proxy_settings", return_value={
                "enabled": True, "server": server, "override": "", "pac": "",
            }):
                with self.assertRaises(RuntimeError):
                    router.windows_proxy_for("https://example.org")
        with mock.patch.object(router, "_read_windows_proxy_settings", side_effect=RuntimeError("lookup failed")) as read:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "lookup failed"):
                    router._windows_proxy_settings()
            self.assertEqual(read.call_count, 1)

    def test_end_to_end_headers_remove_connection_nominated_fields(self):
        result = router.end_to_end_headers([("Connection", "X-Remove, Keep-Alive"), ("X-Remove", "secret"),
                                            ("Content-Length", "42"), ("Transfer-Encoding", "chunked"), ("X-Trace", "keep")])
        self.assertEqual(result, [("X-Trace", "keep")])

    def test_card_key_readonly_uri_sixty_second_cache_and_expiry(self):
        db = self.database()
        admin.prepare_cards(db, 8240)
        self.replace_card(db, "Kimi", {"env": {"ANTHROPIC_AUTH_TOKEN": "tok_kimi_unit_test"}})
        cfg = {"ccswitch_db": str(db)}
        clock = [0.0]
        connect = sqlite3.connect
        with mock.patch.object(router.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(router.sqlite3, "connect", wraps=connect) as opened:
            self.assertEqual(router.card_key(cfg, "Kimi"), "tok_kimi_unit_test")
            self.assertIn("?mode=ro", opened.call_args.args[0])
            self.assertTrue(opened.call_args.kwargs["uri"])
            clock[0] = 59.0
            self.assertEqual(router.card_key(cfg, "Kimi"), "tok_kimi_unit_test")
            self.assertEqual(opened.call_count, 1)
            clock[0] = 61.0
            router.card_key(cfg, "Kimi")
            self.assertEqual(opened.call_count, 2)

    def test_card_key_rejects_placeholders_invalid_json_and_missing_cards(self):
        db = self.database()
        cfg = {"ccswitch_db": str(db)}
        for token in ("PASTE_KIMI_API_KEY_HERE", "via-router", "", "line\nbreak", "carriage\rreturn", 42):
            self.replace_card(db, "Official", {"env": {"ANTHROPIC_AUTH_TOKEN": token}})
            with self.subTest(token=token), self.assertRaises(RuntimeError):
                router.card_key(cfg, "Official")
        with self.assertRaisesRegex(RuntimeError, "not found"):
            router.card_key(cfg, "missing")
        with closing(sqlite3.connect(db)) as con:
            con.execute("UPDATE providers SET settings_config='not-json' WHERE name='Official'")
            con.commit()
        with self.assertRaisesRegex(RuntimeError, "invalid settings"):
            router.card_key(cfg, "Official")

    def test_health_endpoint_uses_no_upstream(self):
        handler = self.handler()
        handler.path = "/__router/health"
        with mock.patch.object(router.http.client, "HTTPSConnection") as connection:
            handler.do_GET()
            connection.assert_not_called()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"ok": True, "port": 8240})

    def test_kimi_default_route_maps_effort_replaces_auth_and_relays_chunks(self):
        handler = self.handler({"model": "k3.1-pro[1m]", "output_config": {"effort": "xhigh"}, "stream": True})
        connection = self.upstream()
        with mock.patch.object(router.http.client, "HTTPSConnection", return_value=connection) as opened, \
                mock.patch.object(router, "card_key", return_value="tok_kimi_unit_test") as key:
            handler.do_POST()
        opened.assert_called_once_with("api.kimi.com", timeout=900)
        key.assert_called_once_with(handler.server.cfg, "Kimi")
        request = connection.request.call_args
        self.assertEqual(request.args, ("POST", "/coding/v1/messages?stream=true"))
        self.assertEqual(json.loads(request.kwargs["body"])["output_config"]["effort"], "max")
        self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer tok_kimi_unit_test")
        self.assertEqual(handler.wfile.getvalue(), b"data: first\n\ndata: second\n\n")
        headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertNotIn("Transfer-Encoding", headers)
        self.assertNotIn("X-Private", headers)
        self.assertEqual(headers["X-Trace"], "keep")
        connection.close.assert_called_once()
        log = (self.logs / "router.log").read_text(encoding="utf-8")
        self.assertNotIn("tok_kimi_unit_test", log)
        self.assertEqual(json.loads(log)["bytes"], len(handler.wfile.getvalue()))

    def test_deepseek_prefix_strips_context_suffix_and_forces_max(self):
        handler = self.handler({"model": "deepseek-flash[1M]", "output_config": {"effort": "low", "format": "json"}})
        connection = self.upstream()
        with mock.patch.object(router.http.client, "HTTPSConnection", return_value=connection) as opened, \
                mock.patch.object(router, "card_key", return_value="tok_ds_unit_test") as key:
            handler.do_POST()
        opened.assert_called_once_with("api.deepseek.com", timeout=900)
        key.assert_called_once_with(handler.server.cfg, "DeepSeek")
        data = json.loads(connection.request.call_args.kwargs["body"])
        self.assertEqual(data["model"], "deepseek-flash")
        self.assertEqual(data["output_config"], {"effort": "max", "format": "json"})

    def test_proxy_tunnel_and_lookup_failure_direct_fallback(self):
        handler = self.handler({"model": "kimi"})
        handler.server.cfg["follow_windows_proxy"] = True
        connection = self.upstream()
        with mock.patch.object(router.http.client, "HTTPSConnection", return_value=connection) as opened, \
                mock.patch.object(router, "card_key", return_value="tok_kimi_unit_test"), \
                mock.patch.object(router, "windows_proxy_for", return_value=router.urlsplit("http://127.0.0.1:8080")):
            handler.do_POST()
        opened.assert_called_once_with("127.0.0.1", 8080, timeout=900)
        connection.set_tunnel.assert_called_once_with("api.kimi.com", 443)
        handler = self.handler({"model": "kimi"})
        handler.server.cfg["follow_windows_proxy"] = True
        with mock.patch.object(router.http.client, "HTTPSConnection", return_value=self.upstream()) as opened, \
                mock.patch.object(router, "card_key", return_value="tok_kimi_unit_test"), \
                mock.patch.object(router, "windows_proxy_for", side_effect=RuntimeError("lookup failed")):
            handler.do_POST()
        opened.assert_called_once_with("api.kimi.com", timeout=900)

    def test_bad_model_length_missing_key_and_stream_failure(self):
        handler = self.handler({"model": 123})
        handler.do_POST()
        self.assertEqual(handler.send_error.call_args.args[0], 400)
        handler = self.handler()
        handler.headers.replace_header("Content-Length", "-1")
        handler.do_POST()
        self.assertEqual(handler.send_error.call_args.args[0], 400)
        handler = self.handler({"model": "kimi"})
        with mock.patch.object(router, "card_key", side_effect=RuntimeError("no usable key")):
            handler.do_POST()
        self.assertEqual(handler.send_error.call_args.args[0], 502)
        handler = self.handler({"model": "kimi"})
        connection = self.upstream([b"data: partial\n\n", OSError("upstream closed")])
        with mock.patch.object(router.http.client, "HTTPSConnection", return_value=connection), \
                mock.patch.object(router, "card_key", return_value="tok_kimi_unit_test"):
            handler.do_POST()
        handler.send_error.assert_not_called()
        self.assertEqual(handler.wfile.getvalue(), b"data: partial\n\n")
        connection.close.assert_called_once()


class TestCCSwitchAdmin(OfflineSandbox):
    def test_schema_supported_newer_warns_and_older_rejected(self):
        db = self.database()
        for version, warns in ((26, False), (27, True)):
            with closing(sqlite3.connect(db)) as con:
                con.execute(f"PRAGMA user_version={version}")
                con.commit()
            report = admin.inspect_status(db)
            self.assertEqual(report["schema_version"], version)
            self.assertEqual(bool(report["warnings"]), warns)
        with closing(sqlite3.connect(db)) as con:
            con.execute("PRAGMA user_version=22")
            con.commit()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            admin.prepare_cards(db, 8240)

    def test_prepare_cards_insert_update_preserve_real_keys_and_effort_control(self):
        db = self.database()
        first = admin.prepare_cards(db, 8240)
        self.assertTrue(Path(first["backup"]).is_file())
        self.assertEqual([card["action"] for card in first["cards"]], ["insert"] * 3)
        for name, key in (("Kimi", "tok_kimi_unit_test"), ("DeepSeek", "tok_ds_unit_test")):
            self.replace_card(db, name, {"env": {"ANTHROPIC_API_KEY": key, "CLAUDE_CODE_EFFORT_LEVEL": "low"}})
        second = admin.prepare_cards(db, 8241)
        self.assertEqual([card["action"] for card in second["cards"]], ["update"] * 3)
        cards = self.cards(db)
        self.assertEqual(cards["Kimi"]["env"]["ANTHROPIC_AUTH_TOKEN"], "tok_kimi_unit_test")
        self.assertEqual(cards["DeepSeek"]["env"]["ANTHROPIC_AUTH_TOKEN"], "tok_ds_unit_test")
        self.assertEqual(cards["K3.1主+DS子代理"]["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8241")
        for name in ("Kimi", "DeepSeek", "K3.1主+DS子代理"):
            self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", cards[name]["env"])
        self.assertNotIn("tok_kimi_unit_test", json.dumps(second))

    def test_prepare_dry_run_and_duplicate_failure_do_not_modify_database(self):
        db = self.database()
        original = db.read_bytes()
        result = admin.prepare_cards(db, 8240, dry_run=True)
        self.assertIsNone(result["backup"])
        self.assertEqual(db.read_bytes(), original)
        admin.prepare_cards(db, 8240)
        with closing(sqlite3.connect(db)) as con:
            con.execute("INSERT INTO providers (id,app_type,name,settings_config) VALUES ('duplicate_kimi','claude','Kimi','{}')")
            con.commit()
        before = self.cards(db)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            admin.prepare_cards(db, 8240)
        self.assertEqual(self.cards(db), before)

    def test_projection_preserves_hooks_effort_and_clears_floor_exclusive_residue(self):
        original = {
            "hooks": {"SessionStart": [{"hooks": []}]}, "effortLevel": "xhigh", "model": "obsolete",
            "apiKeyHelper": "old-helper", "env": {"ANTHROPIC_BASE_URL": "https://example.org", "AWS_REGION": "unit",
                "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "CLAUDE_CODE_DISABLE_THINKING": "1",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "131072", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": 372000,
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "983616", "PERSONAL_SETTING": "keep"},
        }
        previous = {"env": {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "CLAUDE_CODE_DISABLE_THINKING": "0"}}
        target = {"env": {"ANTHROPIC_MODEL": "deepseek-flash", "ANTHROPIC_AUTH_TOKEN": "tok_ds_unit_test"}}
        snapshot = copy.deepcopy(original)
        result = admin.project_claude_settings(original, previous, target)
        self.assertEqual(original, snapshot)
        self.assertEqual(result["hooks"], original["hooks"])
        self.assertEqual(result["effortLevel"], "xhigh")
        for key in ("model", "apiKeyHelper"):
            self.assertNotIn(key, result)
        for key in ("AWS_REGION", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", *admin.RESIDUE_ENV):
            self.assertNotIn(key, result["env"])
        self.assertEqual(result["env"]["CLAUDE_CODE_DISABLE_THINKING"], "1")
        self.assertEqual(result["env"]["PERSONAL_SETTING"], "keep")

    def test_switch_card_preserves_user_settings_and_other_app_current(self):
        db, settings = self.database(), self.settings()
        admin.prepare_cards(db, 8240)
        result = admin.switch_card(db, settings, "DeepSeek")
        self.assertEqual(result["previous"], "Official")
        self.assertTrue(Path(result["settings_backup"]).is_file())
        updated = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(updated["hooks"], {"Stop": [{"hooks": []}]})
        self.assertEqual(updated["effortLevel"], "xhigh")
        self.assertEqual(updated["env"]["PERSONAL_SETTING"], "keep")
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", updated["env"])
        with closing(sqlite3.connect(db)) as con:
            current = con.execute("SELECT app_type,name FROM providers WHERE is_current=1 ORDER BY app_type").fetchall()
        self.assertEqual(current, [("claude", "DeepSeek"), ("codex", "Other")])

    def test_live_proxy_attached_and_pending_refuse_switch_without_writes(self):
        db, settings = self.database(), self.settings()
        admin.prepare_cards(db, 8240)
        for live in ({"mode": "proxy"}, {"attached": True}, {"pending": {"card": "Kimi"}}):
            user_words.atomic_write_json(self.root / "live-state.json", {"apps": {"claude": live}})
            before = db.read_bytes(), settings.read_bytes()
            with self.subTest(live=live), self.assertRaisesRegex(RuntimeError, "direct mode"):
                admin.switch_card(db, settings, "Kimi")
            self.assertEqual((db.read_bytes(), settings.read_bytes()), before)

    def test_settings_write_failure_rolls_back_database(self):
        db, settings = self.database(), self.settings()
        admin.prepare_cards(db, 8240)
        original = settings.read_bytes()
        with mock.patch.object(admin, "atomic_write_json", side_effect=OSError("write failed")), \
                self.assertRaisesRegex(OSError, "write failed"):
            admin.switch_card(db, settings, "Kimi")
        self.assertEqual(settings.read_bytes(), original)
        self.assertEqual(admin.inspect_status(db)["current_claude_card"]["name"], "Official")

    def test_commit_failure_after_publication_restores_settings_and_database(self):
        db, settings = self.database(), self.settings()
        admin.prepare_cards(db, 8240)
        original = settings.read_bytes()
        real_connect = sqlite3.connect

        class FailCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("commit failed")

        def connect(path, *args, **kwargs):
            if isinstance(path, str) and "?mode=rw" in path:
                kwargs["factory"] = FailCommit
            return real_connect(path, *args, **kwargs)

        with mock.patch.object(admin.sqlite3, "connect", side_effect=connect), \
                self.assertRaisesRegex(sqlite3.OperationalError, "commit failed"):
            admin.switch_card(db, settings, "Kimi")
        self.assertEqual(settings.read_bytes(), original)
        self.assertEqual(admin.inspect_status(db)["current_claude_card"]["name"], "Official")

    def test_switch_dry_run_preserves_database_and_settings(self):
        db, settings = self.database(), self.settings()
        admin.prepare_cards(db, 8240)
        before = db.read_bytes(), settings.read_bytes()
        result = admin.switch_card(db, settings, "Kimi", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual((db.read_bytes(), settings.read_bytes()), before)

    def test_disable_prompts_backs_up_original_and_only_selected_app(self):
        db = self.database()
        with closing(sqlite3.connect(db)) as con:
            con.executemany("INSERT INTO prompts VALUES (?,?,?,?,?)", [
                ("prompt_a", "claude", "Old rules", 1, "old claude content"),
                ("prompt_b", "codex", "Other rules", 1, "other content"),
                ("prompt_c", "claude", "Disabled rules", 0, "keep disabled"),
            ])
            con.commit()
        result = admin.disable_prompts(db, app_types=["claude"])
        self.assertEqual([row["id"] for row in result["disabled"]], ["prompt_a"])
        with closing(sqlite3.connect(result["backup"])) as con:
            self.assertEqual(con.execute("SELECT enabled FROM prompts WHERE id='prompt_a'").fetchone(), (1,))
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute("SELECT enabled,content FROM prompts WHERE id='prompt_a'").fetchone(), (0, "old claude content"))
            self.assertEqual(con.execute("SELECT enabled FROM prompts WHERE id='prompt_b'").fetchone(), (1,))

    def test_disable_prompts_zero_enabled_means_zero_writes_and_no_backup(self):
        db = self.database()
        before = db.read_bytes(), db.stat().st_mtime_ns
        with mock.patch.object(admin, "backup_sqlite_database") as backup:
            result = admin.disable_prompts(db)
            backup.assert_not_called()
        self.assertEqual(result["disabled"], [])
        self.assertIsNone(result["backup"])
        self.assertEqual((db.read_bytes(), db.stat().st_mtime_ns), before)

    def test_verify_alignment_reports_prompts_effort_lock_and_mismatch(self):
        db, settings = self.database(), self.settings()
        admin.prepare_cards(db, 8240)
        admin.switch_card(db, settings, "Kimi")
        okay, report = admin.verify_alignment(db, settings)
        self.assertTrue(okay, report["errors"])
        data = json.loads(settings.read_text(encoding="utf-8"))
        data["env"].update(CLAUDE_CODE_EFFORT_LEVEL="low", ANTHROPIC_MODEL="mismatched")
        user_words.atomic_write_json(settings, data)
        with closing(sqlite3.connect(db)) as con:
            con.execute("INSERT INTO prompts VALUES ('prompt_a','claude','Old rules',1,'old content')")
            con.commit()
        okay, report = admin.verify_alignment(db, settings)
        self.assertFalse(okay)
        self.assertEqual(len(report["errors"]), 3)

    def test_admin_cli_prepare_switch_disable_and_verify(self):
        db, settings = self.database(), self.settings()
        script = REPO_ROOT / "template/claude/router/ccswitch_admin.py"
        for command in (["prepare-cards", "--port", "8240"], ["switch-card", "Kimi"], ["disable-prompts"], ["verify"]):
            proc = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(script), "--db", str(db), "--settings", str(settings), *command],
                capture_output=True, text=True, encoding="utf-8", cwd=self.project, timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertIsInstance(result, dict)
        self.assertTrue(result["ok"])


class TestClaudeInstallerAndUpgradeAudit(OfflineSandbox):
    def setUp(self):
        super().setUp()
        # 独立源码与安装目标互为平级，满足安装器的源码目录重叠检查。
        self.source = self.root / "source"
        self.source.mkdir()
        shutil.copy2(REPO_ROOT / "install.py", self.source / "install.py")
        shutil.copytree(REPO_ROOT / "template", self.source / "template",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        self.plugin = self.root / "target/.gemini/config/plugins/user-guard"
        self.codex = self.root / "target/codex"
        self.default_codex = self.home / ".codex"
        patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.default_codex),
                                            "APPDATA": str(self.home / "AppData/Roaming"),
                                            "LOCALAPPDATA": str(self.home / "AppData/Local")})
        patch.start()
        self.addCleanup(patch.stop)

    def install(self, *args, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(self.source / "install.py"), *map(str, args),
             "--no-zotero", "--skip-test"], cwd=self.source, capture_output=True,
            text=True, encoding="utf-8", timeout=40,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def all_args(self):
        return ["--app", "all", "--target-dir", self.plugin, "--codex-home", self.codex,
                "--claude-home", self.claude_home, "--with-cc-router"]

    def snapshot(self):
        # 同时核对目录，防止只读探测留下没有缓存文件的空目录。
        return {path.relative_to(self.root).as_posix():
                (path.read_bytes() if path.is_file() else None, path.stat().st_mtime_ns)
                for path in self.root.rglob("*")}

    def prompts(self, db):
        with closing(sqlite3.connect(db)) as con:
            con.executemany("INSERT INTO prompts VALUES (?,?,?,?,?)", [
                ("claude_old", "claude", "Old Claude", 1, "old claude rules"),
                ("codex_old", "codex", "Old Codex", 2, "old codex rules"),
            ])
            con.commit()

    def test_claude_upgrade_migrates_hooks_preserves_user_assets_and_is_idempotent(self):
        db = self.database()
        self.prompts(db)
        args = ["--app", "claude", "--claude-home", self.claude_home, "--with-cc-router",
                "--ccswitch-db", db, "--disable-ccswitch-prompts"]
        before = self.snapshot()
        self.install(*args, "--dry-run")
        self.assertEqual(self.snapshot(), before)
        self.install(*args)
        self.assertTrue((self.claude_home / "CLAUDE.md").is_file())
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute("SELECT enabled FROM prompts ORDER BY id").fetchall(), [(0,), (0,)])
        backups = list(self.root.glob("cc-switch.db.bak-before-disable-prompts-*"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as con:
            self.assertEqual(con.execute("SELECT enabled FROM prompts ORDER BY id").fetchall(), [(1,), (2,)])
        for name in ("reader", "auditor", "collaborator"):
            self.assertEqual((self.claude_home / f"agents/readonly-{name}.md").read_bytes(),
                             (REPO_ROOT / f"template/claude/agents/readonly-{name}.md").read_bytes())
        for name in installer.CLAUDE_HOOK_SCRIPTS:
            self.assertEqual((self.claude_home / "hooks" / name).read_bytes(),
                             (REPO_ROOT / "template/user-guard/hooks" / name).read_bytes())
        for path in (REPO_ROOT / "template/claude/router").iterdir():
            if path.is_file():
                target = self.claude_home / "router" / path.name
                self.assertEqual(target.read_bytes(), path.read_bytes())
                if os.name != "nt" and target.suffix in {".sh", ".py"}:
                    self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        platform_file = "router.windows.json" if os.name == "nt" else "router.wsl.json"
        self.assertEqual((self.claude_home / "router/router.json").read_bytes(),
                         (REPO_ROOT / "template/claude/router" / platform_file).read_bytes())

        custom_tail = "## 工具与本机环境\r\n- 保留用户自定义编译器。\r\n"
        (self.claude_home / "CLAUDE.md").write_bytes(("# 旧规则\r\n\r\n" + custom_tail).encode())
        user_words.atomic_write_json(self.claude_home / "router/router.json", {"port": 8250, "personal": True})
        custom_router = (self.claude_home / "router/router.json").read_bytes()
        (self.claude_home / "agents/custom.md").write_bytes(b"custom agent\n")
        for relative in ("hooks/state/history.md", "hooks/logs/history.log", "hooks/__pycache__/old.pyc",
                         "hooks/background_status.py", "hooks/receipts_stop.py", "hooks/read_delegation_guard.py"):
            path = self.claude_home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"keep runtime history or remove retired code\n")
        loader = {"type": "command", "command": sys.executable, "args": ["-I", "-B",
                  str(self.claude_home / "hooks/session_context_loader.py"), "--user-only"]}
        status = {"type": "command", "command": sys.executable, "args": ["-B", str(self.claude_home / "hooks/status_guard.py")]}
        third_party = {"type": "command", "command": "personal-hook", "args": ["keep", "all"], "timeout": 7}
        external = {"type": "command", "command": sys.executable,
                    "args": [str(self.root / "third-party/hooks/session_context_loader.py"), "--user-only"]}
        env = {"ANTHROPIC_MODEL": "personal-model", "ANTHROPIC_AUTH_TOKEN": "tok_installer_private",
               "CLAUDE_CODE_EFFORT_LEVEL": "high", "PERSONAL_SETTING": "keep"}
        legacy = {"env": env, "effortLevel": "high", "permissions": {"allow": ["Read"]}, "hooks": {
            "SessionStart": [{"matcher": "startup", "hooks": [copy.deepcopy(loader)]}],
            "Stop": [{"hooks": [copy.deepcopy(loader), third_party, external]}],
            "PreToolUse": [{"matcher": "Bash|PowerShell|Read|Grep", "hooks": [status]}],
        }}
        user_words.atomic_write_json(self.claude_home / "settings.json", legacy)
        self.install(*args)
        updated = json.loads((self.claude_home / "settings.json").read_bytes())
        self.assertEqual(updated["env"], env)
        self.assertEqual(updated["effortLevel"], "high")
        self.assertEqual(updated["permissions"], legacy["permissions"])
        self.assertTrue((self.claude_home / "CLAUDE.md").read_bytes().endswith(custom_tail.encode()))
        self.assertNotIn(b"\n", (self.claude_home / "CLAUDE.md").read_bytes().replace(b"\r\n", b""))
        self.assertEqual((self.claude_home / "router/router.json").read_bytes(), custom_router)
        self.assertEqual((self.claude_home / "agents/custom.md").read_bytes(), b"custom agent\n")
        for name in (*installer.CLAUDE_RETIRED_SCRIPTS, "__pycache__"):
            self.assertFalse((self.claude_home / "hooks" / name).exists())
        for relative in ("hooks/state/history.md", "hooks/logs/history.log"):
            self.assertEqual((self.claude_home / relative).read_bytes(), b"keep runtime history or remove retired code\n")
        stop_handlers = [handler for group in updated["hooks"]["Stop"] for handler in group["hooks"]]
        self.assertIn(third_party, stop_handlers)
        self.assertIn(external, stop_handlers)
        for event, groups in updated["hooks"].items():
            for group in groups:
                for handler in group["hooks"]:
                    name = installer._claude_managed_script(handler, self.claude_home)
                    if name:
                        self.assertNotIn("args", handler)
                        tokens = installer._claude_hook_tokens(handler)
                        self.assertEqual(tokens[1:5], installer.HOOK_PYTHON_FLAGS)
                        self.assertTrue(installer._same_nonlink_path(Path(tokens[0]), Path(sys.executable)))
                        if event == "Stop":
                            self.assertNotEqual(name, "session_context_loader.py")
        self.assertNotIn("Bash|PowerShell|Read|Grep", [group.get("matcher") for group in updated["hooks"]["PreToolUse"]])
        installed_loader = next(handler for group in updated["hooks"]["SessionStart"] for handler in group["hooks"]
                                if "session_context_loader.py" in handler["command"])
        live = subprocess.run(installer._claude_hook_tokens(installed_loader), cwd=self.project,
                              input=json.dumps({**self.payload, "source": "compact"}), capture_output=True,
                              text=True, encoding="utf-8", timeout=20)
        self.assertEqual(live.returncode, 0, live.stderr)
        self.assertEqual(json.loads(live.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")
        before = self.snapshot()
        self.install(*args)
        self.assertEqual(self.snapshot(), before)

    def test_app_modes_and_custom_directory_safety(self):
        for args in (
            ["--app", "all", "--claude-home", self.claude_home],
            ["--app", "all", "--target-dir", self.plugin, "--claude-home", self.claude_home],
            ["--app", "claude", "--target-dir", self.plugin],
            ["--app", "claude", "--codex-home", self.codex],
            ["--app", "both", "--claude-home", self.claude_home],
            ["--app", "codex", "--claude-home", self.claude_home],
            ["--app", "antigravity", "--claude-home", self.claude_home],
            ["--app", "claude", "--claude-home", self.claude_home, "--claude-handoff-compactions", "-1"],
        ):
            with self.subTest(args=args):
                self.install(*args, code=2)
        self.assertFalse(self.default_codex.exists())
        self.assertFalse(self.claude_home.exists())
        self.install(*self.all_args())
        for path in (self.plugin / "plugin.json", self.codex / "AGENTS.md", self.claude_home / "CLAUDE.md"):
            self.assertTrue(path.is_file())
        before = self.snapshot()
        overlapping = self.all_args()
        overlapping[overlapping.index("--claude-home") + 1] = self.codex / "nested"
        self.install(*overlapping, code=2)
        self.assertEqual(self.snapshot(), before)

    def test_sandbox_prompt_guard_and_model_hook_opt_outs(self):
        host_db = self.home / ".cc-switch/cc-switch.db"
        host_db.parent.mkdir(parents=True)
        shutil.copy2(self.database(), host_db)
        self.prompts(host_db)
        before = host_db.read_bytes(), host_db.stat().st_mtime_ns
        args = ["--app", "claude", "--claude-home", self.claude_home, "--disable-ccswitch-prompts",
                "--no-claude-hooks", "--no-claude-rules", "--no-claude-agents", "--no-model-config"]
        self.install(*args)
        self.assertEqual((host_db.read_bytes(), host_db.stat().st_mtime_ns), before)
        self.assertFalse(self.claude_home.exists())
        self.assertEqual(list(host_db.parent.iterdir()), [host_db])

    def test_handoff_policy_persists_and_model_defaults_are_explicit(self):
        args = ["--app", "claude", "--claude-home", self.claude_home]
        self.install(*args, "--claude-handoff-mode", "enforce", "--claude-handoff-compactions", "3")
        path = self.claude_home / "settings.json"
        initial = path.read_bytes(), path.stat().st_mtime_ns
        self.install(*args)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), initial)
        self.assertIn(b"--mode enforce --compact-limit 3", path.read_bytes())
        self.install(*args, "--claude-handoff-mode", "off")
        self.assertFalse((self.claude_home / "hooks/handoff_gate.py").exists())
        initial = self.snapshot()
        self.install(*args)
        self.assertEqual(self.snapshot(), initial)
        doc = json.loads(path.read_bytes())
        doc.update(effortLevel="medium", env={"CLAUDE_CODE_EFFORT_LEVEL": "low", "ANTHROPIC_AUTH_TOKEN": "tok_preserved"})
        user_words.atomic_write_json(path, doc)
        self.install(*args, "--sync-model-defaults")
        updated = json.loads(path.read_bytes())
        self.assertEqual(updated["effortLevel"], "xhigh")
        self.assertEqual(updated["env"], {"ANTHROPIC_AUTH_TOKEN": "tok_preserved"})

    def test_upgrade_audit_reports_drift_models_prompts_and_converges_after_sync(self):
        db = self.database()
        self.prompts(db)
        self.codex.mkdir(parents=True)
        cache = {"models": [
            {"slug": "gpt-6.0-sol", "priority": 0, "max_context_window": 400000,
             "supported_reasoning_levels": [{"effort": "high"}]},
            {"slug": "gpt-6.1-sol", "priority": 9, "max_context_window": 872000,
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "max"}, {"effort": "ultra", "description": "delegation"}]},
            {"slug": "gpt-6-astra", "max_context_window": 872000, "supported_efforts": ["high", "max"]},
        ]}
        user_words.atomic_write_json(self.codex / "models_cache.json", cache)
        (self.codex / "config.toml").write_bytes(b'model = "gpt-6.0-sol"\nmodel_reasoning_effort = "high"\nmodel_context_window = 400000 # retain comment\n')
        self.install(*self.all_args())
        admin.prepare_cards(db, admin.default_router_port())
        admin.switch_card(db, self.claude_home / "settings.json", "Kimi")
        for path in (self.plugin / "rules/AGENTS.md", self.codex / "AGENTS.md", self.claude_home / "CLAUDE.md"):
            path.write_bytes(b"rule drift\n")
        self.install("--app", "claude", "--claude-home", self.claude_home, "--no-claude-rules")
        audit_args = ["--audit-upgrade", "--target-dir", self.plugin, "--codex-home", self.codex,
                      "--claude-home", self.claude_home, "--ccswitch-db", db]
        before = self.snapshot()
        report = json.loads(self.install(*audit_args).stdout)
        self.assertEqual(self.snapshot(), before)
        for name in ("antigravity", "codex", "claude"):
            self.assertTrue(report[name]["drift_files"])
        self.assertTrue(report["codex"]["upgrade_recommended"])
        self.assertEqual(report["codex"]["latest_sol_slug"], "gpt-6.1-sol")
        self.assertEqual(report["codex"]["latest_astra_slug"], "gpt-6-astra")
        self.assertEqual(report["codex"]["declared_max_context_window"], 872000)
        self.assertEqual(report["codex"]["supported_efforts"], ["high", "max"])
        self.assertEqual(len(report["cc_switch"]["enabled_prompts"]), 2)
        self.assertIn("sync_codex_model_defaults", report["summary"]["recommended_actions"])
        self.assertIn("disable_ccswitch_prompts", report["summary"]["recommended_actions"])
        self.install(*self.all_args(), "--sync-model-defaults", "--ccswitch-db", db, "--disable-ccswitch-prompts")
        self.assertIn(b"872000 # retain comment", (self.codex / "config.toml").read_bytes())
        before = self.snapshot()
        report = json.loads(self.install(*audit_args).stdout)
        self.assertEqual(self.snapshot(), before)
        for name in ("antigravity", "codex", "claude"):
            self.assertEqual(report[name]["drift_files"], [])
        self.assertTrue(report["codex"]["model_aligned"])
        self.assertTrue(report["claude"]["hooks_aligned"])
        self.assertEqual(report["cc_switch"]["enabled_prompts"], [])
        self.assertTrue(report["summary"]["all_aligned"])

    def test_audit_redacts_credentials_and_probes_router_only_when_selected(self):
        db = self.database()
        self.install(*self.all_args())
        settings = self.claude_home / "settings.json"
        admin.prepare_cards(db, admin.default_router_port())
        admin.switch_card(db, settings, "Kimi")
        doc = json.loads(settings.read_bytes())
        doc["env"]["ANTHROPIC_AUTH_TOKEN"] = "tok_audit_private"
        user_words.atomic_write_json(settings, doc)
        detection = {name: {"native_installed": False} for name in ("antigravity", "codex", "claude", "cc_switch")}
        loaded_admin = installer._load_ccswitch_admin(REPO_ROOT)
        before = self.snapshot()
        with mock.patch.object(installer, "_load_ccswitch_admin", return_value=loaded_admin), \
                mock.patch.object(loaded_admin.urllib.request, "urlopen") as health:
            report = installer.audit_upgrade_status(self.source, self.plugin, self.codex, self.claude_home,
                                                     db, detection=detection, is_sandbox_run=True)
            health.assert_not_called()
        text = json.dumps(report)
        self.assertNotIn("tok_audit_private", text)
        self.assertNotIn("tok_official_test", text)
        self.assertNotIn(str(self.home), text)
        self.assertEqual(self.snapshot(), before)
        admin.switch_card(db, settings, "K3.1主+DS子代理")
        response = mock.MagicMock(status=200)
        response.read.return_value = json.dumps({"ok": True, "port": admin.default_router_port()}).encode()
        response.__enter__.return_value = response
        with mock.patch.object(installer, "_load_ccswitch_admin", return_value=loaded_admin), \
                mock.patch.object(loaded_admin.urllib.request, "urlopen", return_value=response) as health:
            report = installer.audit_upgrade_status(self.source, self.plugin, self.codex, self.claude_home,
                                                     db, detection=detection, is_sandbox_run=True)
            health.assert_called_once()
            self.assertTrue(report["cc_switch"]["router_health"]["ok"])
        self.assertEqual(len([card for card in report["cc_switch"]["cards"] if card["name"] in ("Kimi", "DeepSeek", "K3.1主+DS子代理")]), 3)

    def test_planning_and_probe_preserve_legacy_contracts(self):
        detection = {name: {"native_installed": False, "interop_available": False, "interop_status": "not_detected"}
                     for name in ("antigravity", "codex", "claude")}
        for mode, expected in (("all", (True, True, True)), ("both", (True, True, False)),
                               ("claude", (False, False, True)), ("codex", (False, True, False)),
                               ("antigravity", (True, False, False))):
            self.assertEqual(installer.resolve_multi_app_plan(mode, False, False, False, detection, False, True, False), expected)
        self.assertEqual(installer.resolve_app_plan("both", False, False, detection, False, True, False), (True, True))
        self.assertEqual(installer.resolve_multi_app_plan("auto", False, False, True, {}, False, True, False), (False, False, True))
        detection["claude"]["native_installed"] = True
        with redirect_stdout(io.StringIO()):
            self.assertEqual(installer.resolve_multi_app_plan("auto", False, False, False, detection, False, True, False), (False, False, True))
        detection["antigravity"]["native_installed"] = True
        self.assertEqual(installer.resolve_multi_app_plan("auto", False, False, False, detection, False, True, False), (True, False, True))
        detection["claude"]["native_installed"] = False
        detection["antigravity"]["native_installed"] = False
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            installer.resolve_multi_app_plan("auto", False, False, False, detection, False, True, False)
        self.assertEqual(stopped.exception.code, 2)
        with mock.patch.object(installer.Path, "home", return_value=self.home), \
                mock.patch.object(installer, "_native_cli", return_value=None), \
                mock.patch.object(installer, "_first_executable", return_value=None), \
                mock.patch.object(installer, "_find_windows_codex_package", return_value=(None, None)), \
                mock.patch.object(installer, "_probe_windows_wsl", return_value={"apps": {}, "status": "unknown"}), \
                mock.patch.object(installer, "_is_wsl", return_value=False), \
                mock.patch.object(installer, "detect_zotero_mcp_executable", return_value=None):
            probe = installer.detect_installed_apps()
        self.assertEqual(probe["recommended_app_mode"], "none")
        self.assertEqual(probe["claude"]["target_claude_home"], str(self.claude_home))
        self.assertFalse(probe["cc_switch"]["native_installed"])

    def test_malformed_claude_settings_abort_before_any_client_write(self):
        self.plugin.mkdir(parents=True)
        (self.plugin / "keep.txt").write_bytes(b"preserve before preflight\n")
        self.claude_home.mkdir(parents=True)
        path = self.claude_home / "settings.json"
        for content in (b"{broken", b'{"hooks":[]}', b'{"hooks":{"Stop":[{"hooks":{}}]}}',
                        b'{"env":[]}', b'{"hooks":{},"hooks":{}}'):
            with self.subTest(content=content):
                path.write_bytes(content)
                before = self.snapshot()
                self.install(*self.all_args(), code=2)
                self.assertEqual(self.snapshot(), before)

    def test_probe_reports_all_native_combinations_and_wsl_extensions(self):
        available = set()
        with mock.patch.object(installer.Path, "home", return_value=self.home), \
                mock.patch.object(installer, "_native_cli", side_effect=lambda name, home: str(home / name) if name in available else None), \
                mock.patch.object(installer, "_windows_app_binaries", return_value=(None, None)), \
                mock.patch.object(installer, "_first_executable", return_value=None), \
                mock.patch.object(installer, "_find_windows_codex_package", return_value=(None, None)), \
                mock.patch.object(installer, "_probe_windows_wsl", return_value={"apps": {}, "status": "checked"}), \
                mock.patch.object(installer, "_is_wsl", return_value=False), \
                mock.patch.object(installer, "detect_zotero_mcp_executable", return_value=None):
            for clients, mode in (((), "none"), (("claude",), "claude"), (("codex",), "codex"),
                                   (("agy",), "antigravity"), (("agy", "codex"), "both"),
                                   (("agy", "claude"), "all"), (("agy", "codex", "claude"), "all")):
                available.clear()
                available.update(clients)
                self.assertEqual(installer.detect_installed_apps()["recommended_app_mode"], mode)
        output = "agy\t/opt/bin/agy\ttrue\ncodex\t/opt/bin/codex\ttrue\nclaude\t/opt/bin/claude\ttrue\ncc-switch\t\ttrue\n"
        with mock.patch.object(installer.shutil, "which", return_value="wsl.exe"), \
                mock.patch.object(installer, "_wsl_distro_list", side_effect=[["running"], ["running"]]), \
                mock.patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=output)):
            result = installer._probe_windows_wsl()
        self.assertEqual(result["status"], "checked")
        self.assertEqual(result["apps"]["claude"][0]["binary"], "/opt/bin/claude")
        self.assertTrue(result["apps"]["cc-switch"][0]["config_dir_exists"])

    def test_windows_package_probe_reads_registry_without_spawning_subprocess(self):
        package_old = self.root / "codex-package-old"
        package_new = self.root / "codex-package-new"
        for pkg in (package_old, package_new):
            (pkg / "app/resources").mkdir(parents=True)
            (pkg / "app/resources/codex.exe").write_bytes(b"cli fixture\n")
            (pkg / "app/Codex.exe").write_bytes(b"desktop fixture\n")

        entries = [
            ("OpenAI.CodexPrimaryRuntime.v26_26.9999.0.0_x64__abc", str(package_old)),
            ("OpenAI.Codex_26.1100.1.0_x64__abc", str(package_old)),
            ("OpenAI.Codex_26.1111.2228.0_x64__abc", str(package_new)),
        ]

        class FakeKey:
            def __init__(self, sub=None):
                self.sub = sub

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        fake_winreg = SimpleNamespace(
            HKEY_CURRENT_USER=1,
            OpenKey=lambda root_or_key, path: FakeKey(None if root_or_key == 1 else path),
            QueryInfoKey=lambda key: (len(entries), 0, 0),
            EnumKey=lambda key, idx: entries[idx][0],
            QueryValueEx=lambda key, name: (dict(entries)[key.sub], 1),
        )
        before = self.snapshot()
        with mock.patch.object(installer, "_is_windows", return_value=True), \
                mock.patch.dict(sys.modules, {"winreg": fake_winreg}), \
                mock.patch.object(installer.subprocess, "run", side_effect=AssertionError("must not spawn subprocess")):
            self.assertEqual(
                installer._find_windows_codex_package(),
                (str(package_new / "app/resources/codex.exe"), str(package_new / "app/Codex.exe")),
            )
        self.assertEqual(self.snapshot(), before)

    @unittest.skipUnless(os.name == "nt", "Windows 应用包探测实测")
    def test_windows_package_probe_does_not_write_powershell_cache(self):
        before = self.snapshot()
        installer._find_windows_codex_package()
        self.assertEqual(self.snapshot(), before)

    def test_audit_reads_carbon_catalog_and_reports_unreadable_configs_without_writes(self):
        self.install(*self.all_args())
        gemini = self.plugin.parent.parent.parent
        user_words.atomic_write_json(gemini / "antigravity-cli/settings.json", {"model": "Claude 4.6"})
        user_words.atomic_write_json(gemini / "antigravity-cli/cache/cae_models_cache.json", {"models": [
            {"slug": "gemini-4-carbon-high", "max_context_window": 1000000},
            {"slug": "gemini-4-carbon-medium"}, {"slug": "gemini-4-carbon-ultra"},
            {"slug": "claude-4.6"}, {"slug": "gpt-oss-120b"},
        ]})
        (self.claude_home / "settings.json").write_bytes(b"{unreadable-settings")
        (self.codex / "config.toml").write_bytes(b"model = [invalid")
        detection = {name: {"native_installed": False} for name in ("antigravity", "codex", "claude", "cc_switch")}
        before = self.snapshot()
        with mock.patch.object(installer, "_is_working_python_cmd", side_effect=AssertionError("audit must not probe hook interpreters")):
            report = installer.audit_upgrade_status(self.source, self.plugin, self.codex, self.claude_home,
                                                     self.root / "missing.db", detection=detection, is_sandbox_run=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report["antigravity"]["carbon_supported_efforts"], ["medium", "high"])
        self.assertNotIn("gemini-4-carbon-ultra", report["antigravity"]["recommended_carbon_variants"])
        self.assertIn("claude-4.6", report["antigravity"]["retiring_models"])
        self.assertIn("gpt-oss-120b", report["antigravity"]["retiring_models"])
        self.assertFalse(report["claude"]["hooks_aligned"])
        self.assertIn("repair_unreadable_configs", report["summary"]["recommended_actions"])
        self.assertNotIn(str(self.home), json.dumps(report))
        self.assertTrue(all(not key.startswith(str(self.codex)) for key in report["read_errors"]))

    def test_status_guard_installer_flags_and_claude_subagent_skips(self):
        project = self.project / "repo"
        project.mkdir()
        base = {"conversationId": "claude-inst-guard", "cwd": str(project), "workspacePaths": [str(project)]}
        safe = (
            "python -B install.py --audit-upgrade",
            "python -B install.py --app claude --claude-home inside --no-zotero",
            "python -B install.py --app all --target-dir inside/p --codex-home inside/cx --claude-home inside/cl --no-zotero",
            "python -B install.py --app claude --claude-home inside --ccswitch-db inside/cc.db --disable-ccswitch-prompts --no-zotero",
        )
        bad = (
            "python -B install.py --app claude",
            "python -B install.py --app all --target-dir inside/p --codex-home inside/cx --no-zotero",
            "python -B install.py --app both --target-dir inside/p --codex-home inside/cx --claude-home inside/cl --no-zotero",
            "python -B install.py --app claude --claude-home inside --ccswitch-db /outside/cc.db --disable-ccswitch-prompts --no-zotero",
            "python -B install.py --claude-home --audit-upgrade",
        )
        for cmd, expected in [(s, "allow") for s in safe] + [(b, "force_ask") for b in bad]:
            with self.subTest(command=cmd):
                res = status_guard.handle_pre_tool_use({**base, "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
                self.assertEqual(res["decision"], expected)

        sub_transcript = self.claude_home / "projects/work/subagents/agent-1.jsonl"
        _append_jsonl(sub_transcript, _user("子代理支线消息"))
        for sub_payload in (
            {**self.payload, "agent_id": "sub_01"},
            {**self.payload, "transcript_path": str(sub_transcript)},
        ):
            self.assertEqual(session_context_loader.handle_claude_session_start({**sub_payload, "source": "compact"}), {})
            self.assertEqual(handoff_gate.handle_claude_handoff({**sub_payload, "hook_event_name": "PostCompact"}, self.state / "handoff", "enforce", 1), {})
            self.assertEqual(reply_repeat_guard.handle_claude_stop_repeat_guard({**sub_payload, "last_assistant_message": "等待中..."}), {})

    def test_unverified_body_preserved_and_compaction_override_edges(self):
        user_words_dir = self.state / "user-words"
        md_path = user_words_dir / f"{self.sid}.md"
        user_words.atomic_write_text(md_path, "# 既有未附清单的重要原话正文\n")
        _append_jsonl(self.transcript, _user("新会话片段"))
        res = user_words.archive_claude_session_words(self.payload, self.state)
        self.assertFalse(res["complete"])
        self.assertEqual(md_path.read_text(encoding="utf-8"), "# 既有未附清单的重要原话正文\n")

        gate_dir = self.state / "handoff-edge"
        handoff_gate.handle_claude_handoff({**self.payload, "hook_event_name": "PostCompact"}, gate_dir, "enforce", 1)
        _append_jsonl(self.transcript, _user("压缩后首条普通消息"))
        handoff_gate.handle_claude_handoff({**self.payload, "hook_event_name": "SessionStart", "source": "compact"}, gate_dir, "enforce", 1)
        gate_state = json.loads((gate_dir / f"{self.sid}_handoff_gate.json").read_text(encoding="utf-8"))
        self.assertEqual(gate_state["compactionCount"], 1)

        handoff_gate.handle_claude_handoff({**self.payload, "hook_event_name": "UserPromptSubmit", "prompt": "暂缓交接"}, gate_dir, "enforce", 1)
        for idx in range(3):
            _append_jsonl(self.transcript, {"type": "system", "subtype": "compact_boundary", "uuid": f"cb_{idx}"})
        handoff_gate.handle_claude_handoff({**self.payload, "hook_event_name": "PostCompact"}, gate_dir, "enforce", 1)
        _append_jsonl(self.transcript, _user("继续工作后再次暂缓"))
        handoff_gate.handle_claude_handoff({**self.payload, "hook_event_name": "UserPromptSubmit", "prompt": "暂缓交接"}, gate_dir, "enforce", 1)
        gate_state = json.loads((gate_dir / f"{self.sid}_handoff_gate.json").read_text(encoding="utf-8"))
        self.assertEqual(gate_state["phase"], "overridden")

    def test_third_party_wrapper_hook_preserved_and_single_client_audit_aligned(self):
        self.claude_home.mkdir(parents=True)
        wrapper = f'python3 /opt/custom/wrapper.py "{(self.claude_home / "hooks/status_guard.py").as_posix()}"'
        user_words.atomic_write_json(self.claude_home / "settings.json", {
            "hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": wrapper}]}]},
        })
        self.install("--app", "claude", "--claude-home", self.claude_home)
        merged = json.loads((self.claude_home / "settings.json").read_text(encoding="utf-8"))
        commands = [h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertIn(wrapper, commands)

        report = json.loads(self.install("--audit-upgrade", "--app", "claude", "--claude-home", self.claude_home).stdout)
        self.assertEqual(report["claude"]["drift_files"], [])
        self.assertTrue(report["summary"]["all_aligned"], report["summary"])

    def test_duplicate_cards_verify_alignment_and_loopback_http_router_relay(self):
        import threading
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        db = self.database()
        settings = self.claude_home / "settings.json"
        user_words.atomic_write_json(settings, {"hooks": {}, "effortLevel": "xhigh"})
        admin.prepare_cards(db, 8240)
        admin.disable_prompts(db)
        admin.switch_card(db, settings, "Kimi")
        ok, _ = admin.verify_alignment(db, settings)
        self.assertTrue(ok)

        with closing(sqlite3.connect(db)) as con:
            con.execute("UPDATE providers SET is_current = 0 WHERE app_type = 'claude'")
            con.commit()
        ok, status = admin.verify_alignment(db, settings)
        self.assertFalse(ok)
        self.assertTrue(any("no active Claude provider card" in err for err in status["errors"]))

        admin.switch_card(db, settings, "Kimi")
        with closing(sqlite3.connect(db)) as con:
            con.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, is_current, sort_index) VALUES (?,?,?,?,?,?)",
                ("dup_kimi", "claude", "Kimi", json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "tok_dup"}}), 0, 9),
            )
            con.commit()
        router._key_cache.clear()
        with self.assertRaisesRegex(RuntimeError, "multiple cc-switch cards"):
            router.card_key({"ccswitch_db": str(db)}, "Kimi")
        with closing(sqlite3.connect(db)) as con:
            con.execute("DELETE FROM providers WHERE id = 'dup_kimi'")
            ds_cfg = json.loads(con.execute("SELECT settings_config FROM providers WHERE app_type='claude' AND name='DeepSeek'").fetchone()[0])
            ds_cfg["env"]["ANTHROPIC_AUTH_TOKEN"] = "tok_ds_loopback"
            con.execute("UPDATE providers SET settings_config = ? WHERE app_type='claude' AND name='DeepSeek'", (json.dumps(ds_cfg),))
            con.commit()
        router._key_cache.clear()

        received = {}

        class UpstreamStub(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                received["path"] = self.path
                received["auth"] = self.headers.get("Authorization") or self.headers.get("x-api-key")
                received["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
                payload = json.dumps({"id": "msg_stub", "type": "message", "model": received["body"]["model"]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamStub)
        self.addCleanup(upstream.server_close)
        up_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        up_thread.start()
        self.addCleanup(upstream.shutdown)
        up_port = upstream.server_address[1]

        cfg_path = self.root / "router_loopback.json"
        user_words.atomic_write_json(cfg_path, {
            "listen": "127.0.0.1",
            "port": 8240,
            "default_upstream": f"http://127.0.0.1:{up_port}/coding",
            "routes": [{
                "prefix": "deepseek",
                "upstream": f"http://127.0.0.1:{up_port}/anthropic",
                "key_from_card": "DeepSeek",
                "force_effort": "max",
            }],
            "kimi_effort_map": {"xhigh": "max"},
            "ccswitch_db": str(db),
            "log": str(self.root / "router.log"),
            "timeout_s": 10,
            "default_key_from_card": "Kimi",
            "follow_windows_proxy": False,
        })
        cfg = router.load_config(str(cfg_path))
        relay = ThreadingHTTPServer(("127.0.0.1", 0), router.Handler)
        relay.daemon_threads = True
        relay.cfg = cfg
        self.addCleanup(relay.server_close)
        relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
        relay_thread.start()
        self.addCleanup(relay.shutdown)
        relay_port = relay.server_address[1]

        with urllib.request.urlopen(f"http://127.0.0.1:{relay_port}/__router/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(health["ok"])

        req = urllib.request.Request(
            f"http://127.0.0.1:{relay_port}/v1/messages",
            data=json.dumps({"model": "deepseek-flash[1M]", "messages": [{"role": "user", "content": "ping"}]}).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-api-key": "via-router"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(out["model"], "deepseek-flash")
        self.assertEqual(received["path"], "/anthropic/v1/messages")
        self.assertEqual(received["body"]["output_config"]["effort"], "max")


if __name__ == "__main__":
    unittest.main()

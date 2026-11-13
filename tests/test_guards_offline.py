"""
test_guards_offline.py - 防空转门禁 (status_guard) 与防刷屏门禁 (reply_repeat_guard) 离线单元测试套件

覆盖验证：
1. status_guard 对未变化日志文件的同参重复读取精准 deny，文件更新后自动恢复 allow；
2. 【防翻页误杀】长输出文件（如 .system_generated/steps/17/output.txt 或长 .log）分段翻页读取（不同 StartLine/EndLine/ByteOffset）100% allow；
3. 【防变参检索误杀】run_command 先 grep 再 tail 同一日志（或含 powershell/bash 包装器）按命令签名区分，变参 allow、同参重复 deny；
4. 【防 manage_task 永久锁死】manage_task(Action="status") 联动 .system_generated/tasks/<TaskId>.log，日志新增输出后立即恢复 allow；
5. 【防 WSL 死锁与业务文件误伤】wsl 查看子系统内部不可达路径不锁死；普通配置 status.json / test_result.json 及插件自身状态目录绝不误拦截；
6. reply_repeat_guard 连续 3 次同质短等待回复精准 continue 拦截，且重入 continuationActive=True 时 100% allow 熔断防死循环；
7. 【防误杀真实进展】连续 3 次包含不同迭代步数/残差数值的短回复、或含等待用户指示/提问的回复绝不误拦截。
"""

import concurrent.futures
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = Path(os.environ.get("USER_GUARD_INSTALLED_DIR", str(REPO_ROOT / "template" / "user-guard"))) / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import reply_repeat_guard  # noqa: E402
import status_guard  # noqa: E402


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


class TestGuardsOffline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self.tmp_obj.name)
        self.plugin_data = self.tmp_root / "plugin_data"
        self.old_plugin_data = os.environ.get("PLUGIN_DATA")
        self.old_harness_depth = os.environ.pop("CAE_HARNESS_DEPTH", None)
        os.environ["PLUGIN_DATA"] = str(self.plugin_data)

    def tearDown(self) -> None:
        if self.old_plugin_data is None:
            os.environ.pop("PLUGIN_DATA", None)
        else:
            os.environ["PLUGIN_DATA"] = self.old_plugin_data
        if self.old_harness_depth is None:
            os.environ.pop("CAE_HARNESS_DEPTH", None)
        else:
            os.environ["CAE_HARNESS_DEPTH"] = self.old_harness_depth
        self.tmp_obj.cleanup()

    def test_01_status_guard_unchanged_deny_and_updated_allow(self) -> None:
        """未变动日志文件同参重复读取被 deny，文件产生增量输出后恢复 allow"""
        conv_id = "guard-001"
        log_file = self.tmp_root / "cfd_solver.log"
        log_file.write_text("Iter 1: residual = 1.0e-1\n", encoding="utf-8")

        payload = {
            "conversationId": conv_id,
            "cwd": str(self.tmp_root),
            "toolCall": {"name": "read_file", "args": {"Path": str(log_file), "StartLine": 1, "EndLine": 100}},
        }

        self.assertEqual(status_guard.handle_pre_tool_use(payload)["decision"], "allow")
        r_dup = status_guard.handle_pre_tool_use(payload)
        self.assertEqual(r_dup["decision"], "deny")
        self.assertIn("Reactive Wakeup", r_dup["reason"])

        # 算例追加新输出后自动恢复放行
        time.sleep(0.01)
        log_file.write_text("Iter 1: residual = 1.0e-1\nIter 2: residual = 3.2e-4\n", encoding="utf-8")
        self.assertEqual(status_guard.handle_pre_tool_use(payload)["decision"], "allow")

    def test_02_status_guard_allows_pagination_on_large_output(self) -> None:
        """分段翻页读取 .system_generated/steps/17/output.txt 或大日志文件绝不误杀"""
        conv_id = "guard-pagination-002"
        step_out = self.tmp_root / ".system_generated" / "steps" / "17" / "output.txt"
        step_out.parent.mkdir(parents=True, exist_ok=True)
        step_out.write_text("Line\n" * 2000, encoding="utf-8")

        p_page1 = {
            "conversationId": conv_id,
            "toolCall": {"name": "read_file", "args": {"Path": str(step_out), "StartLine": 1, "EndLine": 800}},
        }
        p_page2 = {
            "conversationId": conv_id,
            "toolCall": {"name": "read_file", "args": {"Path": str(step_out), "StartLine": 801, "EndLine": 1600}},
        }
        p_page3_offset = {
            "conversationId": conv_id,
            "toolCall": {
                "name": "read_file",
                "args": {"Path": str(step_out), "StartLine": 801, "EndLine": 1600, "ByteOffset": 46080},
            },
        }

        self.assertEqual(status_guard.handle_pre_tool_use(p_page1)["decision"], "allow")
        self.assertEqual(status_guard.handle_pre_tool_use(p_page2)["decision"], "allow", "翻读第 2 页必须放行！")
        self.assertEqual(status_guard.handle_pre_tool_use(p_page3_offset)["decision"], "allow", "ByteOffset 续读必须放行！")
        # 原地重复读第 1 页则精准拦截
        self.assertEqual(status_guard.handle_pre_tool_use(p_page1)["decision"], "deny")

    def test_03_status_guard_run_command_params_and_shell_wrappers(self) -> None:
        """run_command 支持变参放行（先 grep 后 tail）与包装器剥离同参拦截"""
        conv_id = "guard-cmd-003"
        out_file = self.tmp_root / "output.txt"
        out_file.write_text("INFO start\nERROR divergence at cell 42\n", encoding="utf-8")

        p_grep = {
            "conversationId": conv_id,
            "cwd": str(self.tmp_root),
            "toolCall": {"name": "run_command", "args": {"CommandLine": "grep ERROR output.txt"}},
        }
        p_tail = {
            "conversationId": conv_id,
            "cwd": str(self.tmp_root),
            "toolCall": {"name": "run_command", "args": {"CommandLine": 'bash -c "tail -n 20 output.txt"'}},
        }

        self.assertEqual(status_guard.handle_pre_tool_use(p_grep)["decision"], "allow")
        self.assertEqual(status_guard.handle_pre_tool_use(p_tail)["decision"], "allow", "变参切换命令必须放行")
        self.assertEqual(status_guard.handle_pre_tool_use(p_tail)["decision"], "deny", "完全相同的包装命令重复轮询必须阻断")

    def test_04_manage_task_status_tracks_real_task_log_updates(self) -> None:
        """manage_task(Action='status') 联动后台任务日志指纹，日志更新后立即恢复放行，不永久锁死"""
        conv_id = "guard-task-004"
        sg_dir = self.tmp_root / "brain" / conv_id / ".system_generated"
        tp = sg_dir / "logs" / "transcript.jsonl"
        task_log = sg_dir / "tasks" / "task-323.log"
        tp.parent.mkdir(parents=True, exist_ok=True)
        task_log.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text("", encoding="utf-8")
        task_log.write_text("Step 10/100...\n", encoding="utf-8")

        payload = {
            "conversationId": conv_id,
            "transcriptPath": str(tp),
            "toolCall": {"name": "manage_task", "args": {"Action": "status", "TaskId": "task-323"}},
        }

        self.assertEqual(status_guard.handle_pre_tool_use(payload)["decision"], "allow")
        self.assertEqual(status_guard.handle_pre_tool_use(payload)["decision"], "deny", "任务日志未变时应拦截空转轮询")

        # 后台算例向 task-323.log 写入新进展 -> 必须自动放行！
        time.sleep(0.01)
        task_log.write_text("Step 10/100...\nStep 80/100...\n", encoding="utf-8")
        self.assertEqual(
            status_guard.handle_pre_tool_use(payload)["decision"],
            "allow",
            "后台任务日志更新后 manage_task(status) 必须自动放行，严禁永久封死！",
        )

    def test_05_wsl_unreachable_paths_and_normal_json_files_not_blocked(self) -> None:
        """WSL 子系统不可达路径不锁死，普通代码配置 status.json / test_result.json 不误伤"""
        conv_id = "guard-safe-005"

        # 1. WSL 内部路径在宿主不可见，连续查询不应被 non_existent 锁死
        wsl_payload = {
            "conversationId": conv_id,
            "cwd": str(self.tmp_root),
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": 'wsl bash -c "tail -n 20 /nonexistent_wsl_sim_dir/run.log"'},
            },
        }
        self.assertEqual(status_guard.handle_pre_tool_use(wsl_payload)["decision"], "allow")
        self.assertEqual(status_guard.handle_pre_tool_use(wsl_payload)["decision"], "allow")

        # 2. 普通项目文件 http_status.json / test_result_schema.json 连续读取完全放行
        for fname in ("http_status.json", "test_result_schema.json", "solver_core.f90"):
            f = self.tmp_root / fname
            f.write_text('{"ok": true}\n', encoding="utf-8")
            p = {"conversationId": conv_id, "toolCall": {"name": "read_file", "args": {"Path": str(f)}}}
            self.assertEqual(status_guard.handle_pre_tool_use(p)["decision"], "allow")
            self.assertEqual(status_guard.handle_pre_tool_use(p)["decision"], "allow", f"普通文件 {fname} 绝不应被拦截")

    def test_06_repeat_guard_blocks_homogeneous_and_respects_continuation_active(self) -> None:
        """连续 3 次同质短等待回复触发拦截，且重入 continuationActive=True 时立即熔断放行防死循环"""
        conv_id = "repeat-006"
        tp = self.tmp_root / "transcript.jsonl"
        _append_jsonl(tp, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "跑算例"})

        replies = [
            "后台算例仍在运行中，请稍候。",
            "后台算例仍在运行中，请耐心等待。",
            "后台算例还在运行中，请稍候。",
        ]
        decisions = []
        for idx, text in enumerate(replies, start=1):
            _append_jsonl(tp, {"step_index": idx, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": text})
            res = reply_repeat_guard.handle_stop_repeat_guard(
                {
                    "conversationId": conv_id,
                    "terminationReason": "NO_TOOL_CALL",
                    "continuationActive": False,
                    "transcriptPath": str(tp),
                }
            )
            decisions.append(res["decision"])

        self.assertEqual(decisions, ["allow", "allow", "continue"])

        # 紧接着宿主携带 continuationActive=True 重入 Stop -> 必须无条件放行防无限唤醒！
        _append_jsonl(tp, {"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "后台仍在运行，现挂起等待通知。"})
        res_reentry = reply_repeat_guard.handle_stop_repeat_guard(
            {
                "conversationId": conv_id,
                "terminationReason": "NO_TOOL_CALL",
                "continuationActive": True,
                "transcriptPath": str(tp),
            }
        )
        self.assertEqual(res_reentry["decision"], "allow")

    def test_07_repeat_guard_allows_distinct_numerical_progress_and_user_questions(self) -> None:
        """包含不同迭代步数/残差的真实进度汇报，以及等待用户裁决的回复，绝不误判为刷屏"""
        conv_id = "repeat-progress-007"
        tp = self.tmp_root / "transcript_prog.jsonl"
        _append_jsonl(tp, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "监控收敛"})

        progress_replies = [
            "算例正在计算：已完成 100 步，当前残差 1.2e-2，继续等待完成。",
            "算例正在计算：已完成 500 步，当前残差 4.5e-5，继续等待完成。",
            "算例正在计算：已完成 900 步，当前残差 8.1e-8，继续等待完成。",
            "网格切换方案已就绪，后台尚未完成合并，等待您的指示。",
        ]
        for idx, text in enumerate(progress_replies, start=1):
            _append_jsonl(tp, {"step_index": idx, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": text})
            res = reply_repeat_guard.handle_stop_repeat_guard(
                {
                    "conversationId": conv_id,
                    "terminationReason": "NO_TOOL_CALL",
                    "continuationActive": False,
                    "transcriptPath": str(tp),
                }
            )
            self.assertEqual(res["decision"], "allow", f"第 {idx} 条真实进展或用户请示不应被误拦截")

        # 补充验证：无数值但属于不同子任务主语的短状态汇报，绝不误判为同质复读
        conv_id_distinct = "repeat-distinct-subject-007b"
        tp_distinct = self.tmp_root / "transcript_distinct.jsonl"
        _append_jsonl(tp_distinct, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "全流程运行"})
        distinct_subject_replies = [
            "前处理非结构网格划分仍在运行中，请稍候。",
            "三维可压缩流场求解器正在计算，请耐心等待。",
            "后处理流线与涡量场导出还在运行中，请稍候。",
        ]
        for idx, text in enumerate(distinct_subject_replies, start=1):
            _append_jsonl(tp_distinct, {"step_index": idx, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": text})
            res = reply_repeat_guard.handle_stop_repeat_guard(
                {
                    "conversationId": conv_id_distinct,
                    "terminationReason": "NO_TOOL_CALL",
                    "continuationActive": False,
                    "transcriptPath": str(tp_distinct),
                }
            )
            self.assertEqual(res["decision"], "allow", f"不同子任务主语第 {idx} 条状态汇报不应被误拦截")

    def test_08_workspace_boundary_allows_project_artifacts_and_cross_env_paths(self) -> None:
        """项目目录内写入、跨端 D:/ <-> /mnt/d/ 同项目等价路径、会话专属制品与标准临时目录 100% 放行"""
        conv_id = "boundary-allow-008"
        proj = self.tmp_root / "myproj"
        proj.mkdir()
        art_dir = self.tmp_root / "brain" / conv_id
        tp = art_dir / ".system_generated" / "logs" / "transcript.jsonl"

        base_payload = {
            "conversationId": conv_id,
            "cwd": str(proj),
            "workspacePaths": [str(proj)],
            "artifactDirectoryPath": str(art_dir),
            "transcriptPath": str(tp),
        }

        for target_p in (
            str(proj / "src" / "solver.py"),
            "docs/report.md",
            str(art_dir / "scratch" / "calc.py"),
            str(self.plugin_data / "state" / "note.json"),
        ):
            res = status_guard.handle_pre_tool_use(
                {**base_payload, "toolCall": {"name": "write_file", "args": {"Path": target_p}}}
            )
            self.assertEqual(res["decision"], "allow", f"允许范围内的路径 {target_p} 必须放行")

        # 验证非临时目录项目支持跨端等价路径、系统临时目录、子代理/宿主对话记忆及 ~/.cache 正常放行
        cross_payload = {
            "conversationId": conv_id,
            "cwd": r"X:\Projects\myproj",
            "workspacePaths": [r"X:\Projects\myproj"],
        }
        for allowed_runtime_path in (
            "/mnt/x/Projects/myproj/src/a.py",
            str(Path(tempfile.gettempdir()) / "cae_stage_check.json"),
            str(Path.home() / ".gemini/antigravity/brain/subagent-uuid-001/scratch/note.md"),
            str(Path.home() / ".gemini/antigravity/plugin_data/user-guard/state/conv_user_words.md"),
            str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "memories/proj_memo.json"),
            str(Path.home() / ".cache/solver-venv/pip-wheel.json"),
        ):
            self.assertEqual(
                status_guard.handle_pre_tool_use(
                    {**cross_payload, "toolCall": {"name": "write_file", "args": {"Path": allowed_runtime_path}}}
                )["decision"],
                "allow",
                f"临时目录、对话记忆与工具缓存路径应当放行: {allowed_runtime_path}",
            )

    def test_09_workspace_boundary_blocks_external_mutations_worktrees_and_link_escapes(self) -> None:
        """项目目录外业务写删、外部 .codex-worktrees、全局核心配置篡改与软链接逃逸：交互清晰 force_ask、无头子任务硬 deny"""
        import subprocess as _sp

        conv_id = "boundary-block-009"
        proj = self.tmp_root / "myproj"
        outside = self.tmp_root / "outside_dir"
        proj.mkdir()
        outside.mkdir()

        base_payload = {
            "conversationId": conv_id,
            "cwd": str(proj),
            "workspacePaths": [str(proj)],
        }

        # 1) 交互会话：外部业务目录与全局核心配置文件触发 force_ask，且弹窗清晰展示动作与目标路径
        r_ext_write = status_guard.handle_pre_tool_use(
            {
                **base_payload,
                "toolCall": {
                    "name": "write_file",
                    "args": {"Path": str(self.tmp_root / "myproj_bak" / "a.py"), "Content": "x = 1\n"},
                },
            }
        )
        self.assertEqual(r_ext_write["decision"], "force_ask")
        self.assertIn("具体写删动作：", r_ext_write["reason"])
        self.assertIn("项目外目标路径：", r_ext_write["reason"])

        for protected_cfg in (
            "/home/alice/.codex/config.toml",
            "/home/alice/.codex/AGENTS.md",
            "/home/alice/.gemini/config/settings.json",
        ):
            r_ext_cfg = status_guard.handle_pre_tool_use(
                {**base_payload, "toolCall": {"name": "edit_file", "args": {"Path": protected_cfg, "Target": "a", "Replacement": "b"}}}
            )
            self.assertEqual(r_ext_cfg["decision"], "force_ask", f"全局核心配置必须受保护: {protected_cfg}")

        # 2) 交互会话：外部 git worktree add / 外部删除（含 wsl 直调）/ 非沙箱全局 install.py 均返回含意图说明的 force_ask
        win_proj_payload = {
            "conversationId": conv_id,
            "cwd": r"X:\Projects\myproj",
            "workspacePaths": [r"X:\Projects\myproj"],
        }
        for bad_cmd in (
            'git worktree add "../.codex-worktrees/myproj/branch-a"',
            r'Remove-Item -Force "X:\Projects\.codex-worktrees"',
            'wsl.exe -d Ubuntu -- rm -rf /mnt/x/Projects/.codex-worktrees',
            'python -B install.py',
            'python -B install.py --app codex',
            r'python -B "X:\Projects\myproj\install.py"',
        ):
            r_cmd = status_guard.handle_pre_tool_use(
                {
                    **win_proj_payload,
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": bad_cmd, "toolSummary": "清理或同步项目外目标测试"},
                    },
                }
            )
            self.assertEqual(r_cmd["decision"], "force_ask", f"越界命令必须触发 force_ask: {bad_cmd}")
            self.assertIn("当前任务意图：清理或同步项目外目标测试", r_cmd["reason"])
            self.assertIn("具体写删动作：", r_cmd["reason"])
            self.assertIn("触发语句：", r_cmd["reason"])

        # 3) 安全对照：--dry-run、项目内沙箱安装、只读查询外部目录、含外部路径字面量的 git commit 均放行
        for safe_cmd in (
            "python -B install.py --dry-run",
            "python -B install.py --probe-apps",
            r'python -B install.py --target-dir "X:\Projects\myproj\.sandbox\plugin" --no-zotero',
            r'python -B install.py --app codex --codex-home "X:\Projects\myproj\.sandbox\codex" --no-zotero',
            r'Get-Item -Force "X:\Projects\.codex-worktrees"',
            r'git commit -m "fix: block X:\Projects\.codex-worktrees and ../.codex-worktrees"',
        ):
            r_ok = status_guard.handle_pre_tool_use(
                {**win_proj_payload, "toolCall": {"name": "run_command", "args": {"CommandLine": safe_cmd}}}
            )
            self.assertEqual(r_ok["decision"], "allow", f"安全命令不应被误拦截: {safe_cmd}")

        # 4) 跨端无头子任务（CAE_HARNESS_DEPTH=1）：越界操作一律硬 deny
        old_depth = os.environ.get("CAE_HARNESS_DEPTH")
        os.environ["CAE_HARNESS_DEPTH"] = "1"
        try:
            r_deny_w = status_guard.handle_pre_tool_use(
                {**base_payload, "toolCall": {"name": "write_file", "args": {"Path": str(outside / "x.txt")}}}
            )
            r_deny_wt = status_guard.handle_pre_tool_use(
                {
                    **win_proj_payload,
                    "toolCall": {"name": "run_command", "args": {"CommandLine": 'git worktree add "../.codex-worktrees/wt"'}},
                }
            )
            self.assertEqual(r_deny_w["decision"], "deny")
            self.assertEqual(r_deny_wt["decision"], "deny")
        finally:
            if old_depth is None:
                os.environ.pop("CAE_HARNESS_DEPTH", None)
            else:
                os.environ["CAE_HARNESS_DEPTH"] = old_depth

        # 5) 软链接 / 目录联接逃逸拦截
        escape_link = proj / "escape_link"
        link_created = False
        try:
            escape_link.symlink_to(outside, target_is_directory=True)
            link_created = True
        except OSError:
            if os.name == "nt":
                env = os.environ.copy()
                env["UG_LINK"] = str(escape_link)
                env["UG_TARGET"] = str(outside)
                p = _sp.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "New-Item -ItemType Junction -Path $env:UG_LINK -Target $env:UG_TARGET | Out-Null",
                    ],
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
                link_created = p.returncode == 0
        if link_created:
            r_link = status_guard.handle_pre_tool_use(
                {**base_payload, "toolCall": {"name": "write_file", "args": {"Path": str(escape_link / "escaped.txt")}}}
            )
            self.assertEqual(r_link["decision"], "force_ask", "通过项目内链接写向项目外目录必须被拦截")

    def test_10_real_world_readonly_python_and_wsl_venv_never_false_positive(self) -> None:
        """真实场景回归：含 .md 字面量、子代理 brain 只读检查、WSL ~/.cache 解释器与数组 .copy() 零误报；真实外部写必拦截"""
        # 场景 A: 项目内 python3 -c 检查 .md 文件名列表并只读打开子代理 brain/<sub-id>/transcript.jsonl
        linux_proj_payload = {
            "conversationId": "parent-conv-010a",
            "cwd": "/home/alice/cfd_proj",
            "workspacePaths": ["/home/alice/cfd_proj"],
        }
        cmd_subagent_check = (
            "python3 -c '\n"
            "import sys\n"
            'mods = ["AGENTS.md", "docs/README.md", "docs/handover/START-HERE.en.md"]\n'
            'with open("/home/alice/.gemini/antigravity/brain/subagent-g5/.system_generated/logs/transcript.jsonl", "r", encoding="utf-8") as f:\n'
            "    print(len(f.readlines()), len(mods))\n"
            "'"
        )
        res_a = status_guard.handle_pre_tool_use(
            {
                **linux_proj_payload,
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": cmd_subagent_check, "toolSummary": "Check wording hits and subagent state"},
                },
            }
        )
        self.assertEqual(res_a["decision"], "allow", "只读检查 .md 列表与子代理轨迹严禁误报")

        # 场景 B: Windows 经 wsl.exe -e bash -lc 调用 $HOME/.cache/.../python 执行含 .copy()、.open('rb') 与 > 比较的只读后处理脚本
        win_cfd_payload = {
            "conversationId": "parent-conv-010b",
            "cwd": r"x:/Projects/cfd_post/case_data 数据处理",
            "workspacePaths": [r"x:/Projects/cfd_post/case_data 数据处理"],
        }
        cmd_wsl_venv_readonly = (
            "wsl.exe -e bash -lc '$HOME/.cache/step1-venv/bin/python -B -c \"\n"
            "from pathlib import Path\n"
            "arr = [1, 2, 3].copy()\n"
            'p = Path(\\"data/simbf.bin\\")\n'
            'if len(arr) > 0 and p.exists():\n'
            '    with p.open(\\"rb\\") as st:\n'
            "        print(len(st.read()))\n"
            "\"'"
        )
        res_b = status_guard.handle_pre_tool_use(
            {
                **win_cfd_payload,
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": cmd_wsl_venv_readonly, "toolSummary": "Inspect profile sample stats"},
                },
            }
        )
        self.assertEqual(res_b["decision"], "allow", "WSL 缓存 venv 只读脚本（含 .copy() 与 > 比较）严禁误报")

        # 对照场景 C: 同样通过 WSL venv python -c，但脚本内真正向项目外业务目录 open(..., 'w') 写文件 -> 必须精准拦截并说明原因
        cmd_wsl_venv_real_write = (
            "wsl.exe -e bash -lc '$HOME/.cache/step1-venv/bin/python -B -c \""
            'open(\\"/mnt/x/Projects/outside_garbage.txt\\", \\"w\\").write(\\"leak\\")'
            "\"'"
        )
        res_c = status_guard.handle_pre_tool_use(
            {
                **win_cfd_payload,
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": cmd_wsl_venv_real_write, "toolSummary": "Export debug dump"},
                },
            }
        )
        self.assertEqual(res_c["decision"], "force_ask", "Python 内联真实写向项目外业务盘必须拦截")
        self.assertIn("当前任务意图：Export debug dump", res_c["reason"])
        self.assertIn("Python 写入/修改文件", res_c["reason"])
        self.assertIn("/mnt/x/Projects/outside_garbage.txt", res_c["reason"])

    def test_11_handoff_gate_image_flood_drain_freeze_and_stop_goal_verification(self) -> None:
        """验证单回合读图过载自动进 drain/freeze、派工/读图禁令、R4 优先级、本地 commit 及 Stop /goal 终审"""
        import session_context_loader

        conv_id = "guard-handoff-011"
        ws_dir = self.tmp_root / "workspace"
        brain_dir = self.tmp_root / ".gemini" / "antigravity" / "brain" / conv_id
        ws_dir.mkdir(parents=True)
        brain_dir.mkdir(parents=True)
        tp = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
        _append_jsonl(tp, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "/goal\n逐批执行并核图"})
        session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(tp)})

        # 构造 4 张合计 >100MB 的模拟 PNG 并在单回合内连续 read_file，第 4 张跨过 100MB 阈值当场进 drain 并阻断读图
        for i in range(4):
            img = ws_dir / f"page_{i}.png"
            with open(img, "wb") as f:
                f.seek(26 * 1024 * 1024 - 1)
                f.write(b"\0")
            res = status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "transcriptPath": str(tp),
                    "toolCall": {"name": "read_file", "args": {"Path": str(img)}},
                }
            )
            if i < 3:
                self.assertEqual(res["decision"], "allow")
            else:
                self.assertEqual(res["decision"], "deny")
                self.assertIn("禁止继续调用 `read_file` 看图", res["reason"])

        # 非图片文本 read_file 仍放行
        txt_file = ws_dir / "summary.txt"
        txt_file.write_text("ok", encoding="utf-8")
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "read_file", "args": {"Path": str(txt_file)}},
                }
            )["decision"],
            "allow",
        )

        # drain 期禁止新开子代理 / swarm / browser / cae_harness 派工 / git push
        for tool_nm in ("invoke_subagent", "swarm", "schedule", "browser_click"):
            r_blk = status_guard.handle_pre_tool_use(
                {"conversationId": conv_id, "cwd": str(ws_dir), "toolCall": {"name": tool_nm, "args": {}}}
            )
            self.assertEqual(r_blk["decision"], "deny", f"{tool_nm} 必须在 drain 期被阻断")

        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "python cae_harness.py task --kind code"}},
                }
            )["decision"],
            "deny",
        )
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
                }
            )["decision"],
            "deny",
        )
        # drain 期禁止多子句串联偷跑 cae_harness 派工、& git push 与 bash -lc 包装 push
        for sneaky_cmd in (
            "python cae_harness.py probe && python cae_harness.py task --kind review",
            "& git push origin main",
            'bash -lc "git status && git push origin main"',
        ):
            self.assertEqual(
                status_guard.handle_pre_tool_use(
                    {
                        "conversationId": conv_id,
                        "cwd": str(ws_dir),
                        "workspacePaths": [str(ws_dir)],
                        "toolCall": {"name": "run_command", "args": {"CommandLine": sneaky_cmd}},
                    }
                )["decision"],
                "deny",
                f"drain 期必须硬阻断变体派工/推送命令: {sneaky_cmd}",
            )

        # 含引号内 '>' 的 git commit -m "fix > ok" 须识别为本地 commit 放行（且不消耗业务修改预算）
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": 'git add -A && git commit -m "chore: > ok"'}},
                }
            )["decision"],
            "allow",
        )

        # drain 中普通项目外请求仍走 R4 审批
        outside_path = self.tmp_root / "outside_dir" / "bad.txt"
        res_r4 = status_guard.handle_pre_tool_use(
            {
                "conversationId": conv_id,
                "cwd": str(ws_dir),
                "workspacePaths": [str(ws_dir)],
                "toolCall": {"name": "write_file", "args": {"Path": str(outside_path), "Content": "x"}},
            }
        )
        self.assertEqual(res_r4["decision"], "force_ask")

        # 消耗 8 次 drain 业务写动作进入 freeze；freeze 后普通源码 write_file 阻断，但交接文档写入放行
        for k in range(8):
            self.assertEqual(
                status_guard.handle_pre_tool_use(
                    {
                        "conversationId": conv_id,
                        "cwd": str(ws_dir),
                        "workspacePaths": [str(ws_dir)],
                        "toolCall": {"name": "write_file", "args": {"Path": str(ws_dir / f"src_{k}.py"), "Content": "pass"}},
                    }
                )["decision"],
                "allow",
            )
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "write_file", "args": {"Path": str(ws_dir / "src_8.py"), "Content": "pass"}},
                }
            )["decision"],
            "deny",
        )

        # freeze 阶段放行带引号路径的 cae_harness.py probe，阻断带引号路径的 cae_harness.py task 派工
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": 'python -B "d:/repo/cae_harness.py" probe'}},
                }
            )["decision"],
            "allow",
        )
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": 'python -B "d:/repo/cae_harness.py" task --kind review'}},
                }
            )["decision"],
            "deny",
        )
        # freeze 阶段放行只读 find 检索，拦截带删除副作用的 find -delete
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": 'find . -maxdepth 2 -name "*.py"'}},
                }
            )["decision"],
            "allow",
        )
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": 'find . -name "*.tmp" -delete'}},
                }
            )["decision"],
            "deny",
        )
        # freeze 阶段放行 git 操作 cae_harness.py 文件及 wsl bash -lc 只读 git status（不得被全局正则误杀）
        for safe_freeze_cmd in (
            "git diff template/cae_harness.py",
            'git add template/cae_harness.py && git commit -m "fix cae_harness.py"',
            'wsl -d Ubuntu-24.04 -- bash -lc "git status"',
        ):
            self.assertEqual(
                status_guard.handle_pre_tool_use(
                    {
                        "conversationId": conv_id,
                        "cwd": str(ws_dir),
                        "workspacePaths": [str(ws_dir)],
                        "toolCall": {"name": "run_command", "args": {"CommandLine": safe_freeze_cmd}},
                    }
                )["decision"],
                "allow",
                f"freeze 期应放行合法本地 Git/shell 命令: {safe_freeze_cmd}",
            )

        handoff_doc = ws_dir / "handoff_next.md"
        self.assertEqual(
            status_guard.handle_pre_tool_use(
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "write_file", "args": {"Path": str(handoff_doc), "Content": "# Handoff"}},
                }
            )["decision"],
            "allow",
        )
        handoff_doc.write_text(
            "# 本会话交接档案\n\n已验证：已核对前三批。\nGit 工作区：保留当前结果。\n"
            "未完成：第 4 批待运行。\n\n## 新对话提示词\n请读取本文件和用户裁定，核对前三批结果后运行第 4 批。\n",
            encoding="utf-8",
        )

        # Stop 门禁：已落盘交接档但 /goal 未带 <!-- GOAL_CANCELLED --> 时先拦截 1 次；同回合续跑必放行
        _append_jsonl(tp, {"step_index": 20, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "已写入交接文档，停机等待新对话。"})
        stop_incomplete = reply_repeat_guard.handle_stop_repeat_guard(
            {"conversationId": conv_id, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tp)}
        )
        self.assertEqual(stop_incomplete["decision"], "continue")
        self.assertIn("GOAL_CANCELLED", stop_incomplete["reason"])

        # 同回合 continuationActive=True 必放行；新回合带全交接内容 + <!-- GOAL_CANCELLED --> 也放行
        self.assertEqual(
            reply_repeat_guard.handle_stop_repeat_guard(
                {"conversationId": conv_id, "terminationReason": "NO_TOOL_CALL", "continuationActive": True, "transcriptPath": str(tp)}
            )["decision"],
            "allow",
        )
        _append_jsonl(tp, {"step_index": 21, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "补充停机"})
        _append_jsonl(
            tp,
            {
                "step_index": 22,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "已核验并整理交接。\n```markdown\n继续下一批\n```\n<!-- GOAL_CANCELLED -->",
            },
        )
        self.assertEqual(
            reply_repeat_guard.handle_stop_repeat_guard(
                {"conversationId": conv_id, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tp)}
            )["decision"],
            "allow",
        )

        # 验证 Stop 门禁面对带 truncated_fields: ["content"] 的截断交接回复时，能回溯 transcript_full.jsonl 放行
        _append_jsonl(tp, {"step_index": 23, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "/goal\n再核一次截断回复"})
        full_tp = tp.parent / "transcript_full.jsonl"
        full_reply = (
            "已验证：前三批通过本地核对。\nGit 工作区：保留当前结果。\n未完成：第 4 批。\n"
            "## 新对话提示词\n```markdown\n请核对前三批并完成第 4 批的验收。\n```\n<!-- GOAL_CANCELLED -->"
        )
        _append_jsonl(
            tp,
            {
                "step_index": 24,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "完整收口交接已完成。\n<truncated 128 bytes>",
                "truncated_fields": ["content"],
            },
        )
        _append_jsonl(full_tp, {"step_index": 24, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": full_reply})
        self.assertEqual(
            reply_repeat_guard.handle_stop_repeat_guard(
                {"conversationId": conv_id, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tp)}
            )["decision"],
            "allow",
        )

    def test_12_long_turn_over_256kb_preserves_exact_turn_key(self) -> None:
        """单回合产出超过 256KB 时，extract_current_turn_latest_reply 仍向前溯源锁定真实回合键"""
        tp = self.tmp_root / "long_turn.jsonl"
        _append_jsonl(tp, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "第 1 轮"})
        _append_jsonl(tp, {"step_index": 10, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "第 2 轮真实起点"})
        expected_key, _, _ = reply_repeat_guard.extract_current_turn_latest_reply(str(tp))
        self.assertTrue(expected_key.startswith("turn_10_"))

        chunk_payload = "data_" + ("A" * 40000)
        for k in range(11, 20):
            _append_jsonl(tp, {"step_index": k, "source": "SYSTEM", "type": "GENERIC", "content": chunk_payload})
        _append_jsonl(tp, {"step_index": 20, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "阶段汇报已完成。"})

        self.assertGreater(tp.stat().st_size, reply_repeat_guard.TAIL_SCAN_BYTES)
        actual_key, step_idx, latest_txt = reply_repeat_guard.extract_current_turn_latest_reply(str(tp))
        self.assertEqual(actual_key, expected_key)
        self.assertEqual(step_idx, 20)
        self.assertEqual(latest_txt, "阶段汇报已完成。")

    def test_13_ast_anchored_path_cmd_subst_and_handoff_false_positive_guards(self) -> None:
        """覆盖 Python AST 绝对右操作数拼接、$(git push) 替换、源码假交接与普通代码回答误放行防护"""
        import handoff_gate
        import session_context_loader

        conv_id = "guard-edge-013"
        ws_dir = self.tmp_root / "ws13"
        ws_dir.mkdir(parents=True)
        outside_abs = (self.tmp_root / "outside13" / "leak.txt").as_posix()
        tp = self.tmp_root / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        _append_jsonl(tp, {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "开始"})
        for k in range(16):
            _append_jsonl(tp, {"step_index": (k + 1) * 100, "source": "SYSTEM", "type": "CHECKPOINT"})
        session_context_loader.handle_pre_invocation({"conversationId": conv_id, "transcriptPath": str(tp)})

        state_dir = self.plugin_data / "state"
        gs = handoff_gate.load_gate_state(state_dir, conv_id)
        gs["phase"] = "freeze"
        handoff_gate.save_gate_state(state_dir, conv_id, gs)

        # 1) 同一 AST 越界在常态要求审批，在冻结阶段必须直接阻断。
        ast_payload = {
                "conversationId": conv_id,
                "cwd": str(ws_dir),
                "workspacePaths": [str(ws_dir)],
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": f'python -c "from pathlib import Path; (Path(\'src\') / \'{outside_abs}\').write_text(\'x\')"'},
                },
        }
        self.assertEqual(status_guard.handle_pre_tool_use(ast_payload)["decision"], "deny")
        gs["phase"] = "overridden"
        gs["overrideUntilCheckpoint"] = 100
        gs["overrideUntilStep"] = 10000
        handoff_gate.save_gate_state(state_dir, conv_id, gs)
        self.assertEqual(status_guard.handle_pre_tool_use(ast_payload)["decision"], "force_ask")
        gs["phase"] = "freeze"
        handoff_gate.save_gate_state(state_dir, conv_id, gs)

        # 2) echo "$(git push origin main)" 在 freeze/drain 期必须阻断
        res_sub = status_guard.handle_pre_tool_use(
            {
                "conversationId": conv_id,
                "cwd": str(ws_dir),
                "workspacePaths": [str(ws_dir)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": 'echo "$(git push origin main)"'}},
            }
        )
        self.assertEqual(res_sub["decision"], "deny")

        # 3) src/resume_solver.py 与 handoff/solver.cpp 在 freeze 期不得作为交接文档放行
        for bad_src in (ws_dir / "src" / "resume_solver.py", ws_dir / "handoff" / "solver.cpp"):
            self.assertEqual(
                status_guard.handle_pre_tool_use(
                    {
                        "conversationId": conv_id,
                        "cwd": str(ws_dir),
                        "workspacePaths": [str(ws_dir)],
                        "toolCall": {"name": "write_file", "args": {"Path": str(bad_src), "Content": "pass"}},
                    }
                )["decision"],
                "deny",
            )

        # 4) 普通排错回答附带代码块不得误通过 Stop 交接验收
        _append_jsonl(
            tp,
            {
                "step_index": 2000,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "定位到计算错误，示例代码如下：\n```python\nx = 1 + 2\nprint(x)\n```\n已检查完毕。",
            },
        )
        res_stop = reply_repeat_guard.handle_stop_repeat_guard(
            {"conversationId": conv_id, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tp)}
        )
        self.assertEqual(res_stop["decision"], "continue")

    def test_native_stop_model_reason_and_concurrent_correction(self) -> None:
        import handoff_gate
        import session_context_loader

        cid = "agy-native-model-stop"
        transcript = self.tmp_root / "transcript.jsonl"
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 0, "content": "请整理交接并停机"})
        payload = {"conversationId": cid, "transcriptPath": str(transcript), "lastUserInput": "请整理交接并停机",
                   "executionNum": 1, "fullyIdle": False, "error": "", "terminationReason": "model_stop"}
        session_context_loader.handle_pre_invocation(payload)
        _append_jsonl(transcript, {"type": "PLANNER_RESPONSE", "source": "MODEL", "step_index": 1, "content": "尚未写好"})
        for changes in ({"terminationReason": "error"}, {"terminationReason": "max_steps_exceeded"},
                        {"error": "runtime error"}, {"continuationActive": True}):
            self.assertEqual(reply_repeat_guard.handle_stop_repeat_guard({**payload, **changes})["decision"], "allow")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: reply_repeat_guard.handle_stop_repeat_guard(payload), range(8)))
        self.assertEqual([reply["decision"] for reply in replies].count("continue"), 1)
        state_dir = self.plugin_data / "state"
        state = handoff_gate.load_gate_state(state_dir, cid)
        self.assertEqual(len(state["stopGateBlockedTurnKeys"]), 1)
        state_file = state_dir / f"{cid}_handoff_gate.json"
        state_file.write_text("{bad", encoding="utf-8")
        self.assertEqual(reply_repeat_guard.handle_stop_repeat_guard(payload)["decision"], "allow")
        self.assertEqual(state_file.read_text(encoding="utf-8"), "{bad")
        log = self.plugin_data / "logs" / "lifecycle.log"
        self.assertIn("Stop 状态未更新", log.read_text(encoding="utf-8"))

    def test_stop_requires_changed_handoff_document_and_unquoted_goal_marker(self) -> None:
        import session_context_loader

        cid = "agy-stop-handoff-file"
        project = self.tmp_root / "project"
        project.mkdir()
        handoff = project / "handoff.md"
        transcript = self.tmp_root / "transcript.jsonl"
        document = (
            "已验证：已完成一轮。\nGit 工作区：当前仍有修改。\n未完成：下一轮验收。\n"
            "## 新对话提示词\n请从当前工作区继续未完成的数值验收并保存原始记录。\n"
        )
        handoff.write_text(document, encoding="utf-8")
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 0, "content": "/goal\n请整理交接并停机"})
        payload = {"conversationId": cid, "transcriptPath": str(transcript), "cwd": str(project),
                   "workspacePaths": [str(project)], "terminationReason": "model_stop", "fullyIdle": True}
        session_context_loader.handle_pre_invocation(payload)
        approved = status_guard.handle_pre_tool_use({
            **payload, "toolCall": {"name": "write_to_file", "args": {"TargetFile": str(handoff), "CodeContent": document}},
        })
        self.assertEqual(approved["decision"], "allow")
        _append_jsonl(transcript, {"type": "PLANNER_RESPONSE", "source": "MODEL", "step_index": 1,
                                  "content": "已整理。\n<!-- GOAL_CANCELLED -->"})
        no_change = reply_repeat_guard.handle_stop_repeat_guard(payload)
        self.assertEqual(no_change["decision"], "continue")
        self.assertIn("尚未落盘", no_change["reason"])
        handoff.write_text(document + "\n本轮新核对：保留最近一次日志。\n", encoding="utf-8")
        for index, content in enumerate((
            "    <!-- GOAL_CANCELLED -->",
            "文档已更新。\n> <!-- GOAL_CANCELLED -->",
            "文档已更新。\n~~~html\n<!-- GOAL_CANCELLED -->",
            "文档已更新。\n<!-- GOAL_CANCELLED -->\n附注。",
            "文档已更新。\n<!-- GOAL_CANCELLED -->",
        )):
            _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": index * 2 + 2, "content": "/goal\n继续交接验收"})
            _append_jsonl(transcript, {"type": "PLANNER_RESPONSE", "source": "MODEL", "step_index": index * 2 + 3,
                                      "content": content})
            result = reply_repeat_guard.handle_stop_repeat_guard(payload)
            if index == 4:
                self.assertEqual(result["decision"], "allow")
            else:
                self.assertEqual(result["decision"], "continue")
                self.assertIn("GOAL_CANCELLED", result["reason"])
                self.assertNotIn("尚未落盘", result["reason"])

    def test_standard_temp_does_not_allow_business_name_or_link_escape(self) -> None:
        from unittest import mock

        root = self.tmp_root
        project = root / "workspace"
        runtime_temp = root / "actual-temp"
        other = root / "outside"
        for path in (project, runtime_temp, other):
            path.mkdir()
        payload = {"conversationId": "temp-boundary", "cwd": str(project), "workspacePaths": [str(project)]}

        def write(target):
            return status_guard.handle_pre_tool_use({
                **payload, "toolCall": {"name": "write_to_file", "args": {"TargetFile": str(target), "CodeContent": "x"}},
            })["decision"]

        with mock.patch.object(status_guard.tempfile, "gettempdir", return_value=str(runtime_temp)):
            self.assertEqual(write(runtime_temp / "probe.txt"), "allow")
            self.assertEqual(write(other / "AppData/Local/Temp/probe.txt"), "force_ask")
            self.assertEqual(write(other / "Users/not-the-current-user/.cache/probe.txt"), "force_ask")
            link = runtime_temp / "escape"
            try:
                link.symlink_to(other, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                import subprocess

                proc = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "New-Item -ItemType Junction -Path $env:UG_LINK -Target $env:UG_TARGET | Out-Null"],
                    env={**os.environ, "UG_LINK": str(link), "UG_TARGET": str(other)},
                    capture_output=True, timeout=10,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(write(link / "probe.txt"), "force_ask")

    @unittest.skipUnless(os.name == "nt", "仅 Windows 钩子负责识别显式 WSL 调用")
    def test_wsl_temp_exception_tracks_execution_scope_and_physical_probe(self) -> None:
        from unittest import mock

        project = self.tmp_root / "workspace"
        project.mkdir()
        payload = {"conversationId": "wsl-temp-boundary", "cwd": str(project), "workspacePaths": [str(project)]}

        def check(command):
            return status_guard.handle_pre_tool_use({
                **payload, "toolCall": {"name": "run_command", "args": {"CommandLine": command}},
            })["decision"]

        runtime_root = self.tmp_root / "runtime-temp"
        runtime_root.mkdir()
        with mock.patch.object(status_guard.tempfile, "gettempdir", return_value=str(runtime_root)), \
                mock.patch.object(status_guard.subprocess, "run") as run:
            def probe(args, **kwargs):
                self.assertEqual(args[1:3], ["-d", "Ubuntu-24.04"])
                self.assertEqual(args[3:8], ["--exec", "/usr/bin/python3", "-B", "-I", "-c"])
                self.assertNotIn("/tmp/nested/out.txt", args)
                targets = json.loads(kwargs["input"])
                if targets == ["/tmp/nested/out.txt"]:
                    return mock.Mock(returncode=0, stdout=json.dumps(targets).encode())
                return mock.Mock(returncode=0, stdout=b"[]")

            run.side_effect = probe
            self.assertEqual(check("wsl -d Ubuntu-24.04 --exec touch /tmp/nested/out.txt"), "allow")
            self.assertEqual(check("wsl.exe -d Ubuntu-24.04 -- bash -lc 'printf x > /tmp/nested/out.txt'"), "allow")
            self.assertEqual(check("wsl -d Ubuntu-24.04 --exec touch /tmp/link-out/out.txt"), "force_ask")
            self.assertEqual(check("wsl -d Ubuntu-24.04 --exec touch /tmp/../etc/out.txt"), "force_ask")
            previous = run.call_count
            self.assertEqual(check("wsl -d Ubuntu-24.04 --exec touch /tmp/nested/out.txt > /tmp/host-output"), "force_ask")
            self.assertEqual(check("wsl -d Ubuntu-24.04 --exec touch /var/tmp/out.txt"), "force_ask")
            self.assertEqual(check("touch /tmp/nested/out.txt"), "force_ask")
            self.assertEqual(run.call_count, previous)

    def test_freeze_overrides_external_approval_and_recognizes_documented_tools(self) -> None:
        import handoff_gate
        import session_context_loader

        cid = "agy-freeze-r4"
        project = self.tmp_root / "project"
        project.mkdir()
        transcript = self.tmp_root / "transcript.jsonl"
        _append_jsonl(transcript, {"type": "USER_INPUT", "step_index": 0, "content": "请整理交接并停机"})
        payload = {"conversationId": cid, "transcriptPath": str(transcript), "cwd": str(project),
                   "workspacePaths": [str(project)]}
        session_context_loader.handle_pre_invocation(payload)
        def call(name, args):
            return status_guard.handle_pre_tool_use({**payload, "toolCall": {"name": name, "args": args}})
        outside = self.tmp_root / "outside" / "solver.py"
        self.assertEqual(call("write_to_file", {"TargetFile": str(outside)})["decision"], "force_ask")
        state_dir = self.plugin_data / "state"
        state = handoff_gate.load_gate_state(state_dir, cid)
        state["phase"] = "freeze"
        handoff_gate.save_gate_state(state_dir, cid, state)
        for name, args in (
            ("replace_file_content", {"TargetFile": str(outside)}),
            ("multi_replace_file_content", {"TargetFile": str(outside)}),
            ("view_file", {"AbsolutePath": str(project / "plot.png")}),
            ("run_command", {"CommandLine": "cat plot.png"}),
            ("generate_image", {}),
            ("unknown_tool", {}),
        ):
            self.assertEqual(call(name, args)["decision"], "deny", name)
        self.assertEqual(call("view_file", {"AbsolutePath": str(project / "readme.md")})["decision"], "allow")
        self.assertEqual(call("finish", {})["decision"], "allow")
        self.assertEqual(call("write_to_file", {"TargetFile": str(outside.with_name("handoff.md"))})["decision"], "force_ask")

    def test_14_sed_script_quoted_redirect_ast_home_handoff_heading_and_missing_full_tp(self) -> None:
        import handoff_gate

        project = self.tmp_root / "proj14"
        project.mkdir()
        (project / "local.txt").write_text("foo\nbar\n", encoding="utf-8")
        base_payload = {"conversationId": "guard-14", "cwd": str(project), "workspacePaths": [str(project)]}

        def run_cmd(cmd: str) -> str:
            return status_guard.handle_pre_tool_use(
                {**base_payload, "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
            )["decision"]

        self.assertEqual(run_cmd("sed -i '/^foo/d' local.txt"), "allow")
        self.assertEqual(run_cmd('rg "cmd > /var/log/a.log" local.txt'), "allow")
        self.assertEqual(
            run_cmd('python -c "from pathlib import Path; (Path.home() / \'.codex/config.toml\').write_text(\'x\')"'),
            "force_ask",
        )
        self.assertIsNone(
            status_guard._eval_ast_path_expr(
                status_guard.ast.parse("unknown_base / '.codex'", mode="eval").body, {}
            )
        )

        for valid_reply in (
            "已验证：完成回归。\nGit 工作区：干净。\n未完成：待推送。\n### 4. 新对话提示词\n```text\n请读取当前仓库状态与测试记录，继续完成远端推送验收。\n```",
            "已验证：完成回归。\nGit 工作区：干净。\n未完成：待推送。\n新对话提示词\n请复制以下内容到新会话：\n```markdown\n请读取当前仓库状态与测试记录，继续完成远端推送验收。\n```",
        ):
            self.assertTrue(handoff_gate._handoff_structure_present(valid_reply), valid_reply)

        cid = "guard-missing-full"
        tp = self.tmp_root / "missing_full" / "transcript.jsonl"
        _append_jsonl(tp, {"type": "USER_INPUT", "step_index": 0, "content": "请整理交接并停机"})
        import session_context_loader
        session_context_loader.handle_pre_invocation({"conversationId": cid, "transcriptPath": str(tp)})
        compact_handoff = (
            "已验证：通过本地测试。\nGit 工作区：保留当前修改。\n未完成：跨端补测。\n"
            "## 新对话提示词\n```text\n请读取当前仓库状态与测试记录，继续跨端补测并核对回执。\n```"
        )
        _append_jsonl(
            tp,
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "step_index": 1,
             "content": compact_handoff, "truncated_fields": ["content"]},
        )
        res = reply_repeat_guard.handle_stop_repeat_guard(
            {"conversationId": cid, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tp)}
        )
        self.assertEqual(res["decision"], "allow")


if __name__ == "__main__":
    unittest.main(verbosity=2)

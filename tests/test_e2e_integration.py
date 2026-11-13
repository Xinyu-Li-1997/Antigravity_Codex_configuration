"""
test_e2e_integration.py - 全系统端到端集成验证、隐私脱敏审计与沙箱安装测试套件

覆盖验证：
1. 插件清单、规则 (rules/AGENTS.md)、钩子编排 (hooks.json) 与只读子代理静态合规校验；
2. 全仓库源码、测试与文档 100% 隐私脱敏校验（零个人用户名、电脑型号与显卡标识硬编码）；
3. 真实 CLI 子进程级联管道联调（UTF-8 中文符号无损传输、PreInvocation 增量压缩注入、PreToolUse 门禁、Stop 门禁）；
4. install.py 镜像同步安装器沙箱测试（自动清除旧版脏文件与废弃 *_stop_gate.json、保护有效原话归档、零 __pycache__ 污染）。
"""

import contextlib
import io
import json
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "template" / "user-guard"
ACTIVE_PLUGIN_DIR = Path(os.environ.get("USER_GUARD_INSTALLED_DIR", str(TEMPLATE_DIR)))
INSTALLER_SPEC = importlib.util.spec_from_file_location("user_guard_installer", REPO_ROOT / "install.py")
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(INSTALLER)


def _build_subprocess_cmd(hook_cmd: str) -> list:
    """将 hooks.json 中的命令安全替换为当前测试 Python 解释器执行，跨 Windows/Linux/macOS 通用"""
    tokens = [t.strip('"') for t in shlex.split(hook_cmd, posix=(os.name != "nt"))]
    # 找到第一个 .py 脚本位置，保留 -X utf8 -I -B 及脚本参数
    script_idx = next((i for i, t in enumerate(tokens) if t.endswith(".py")), 1)
    return [sys.executable, "-X", "utf8", "-I", "-B"] + tokens[script_idx:]


def _create_directory_link(link: Path, target: Path) -> None:
    """Windows 无 symlink 权限时使用同一临时目录内的目录联接。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        env = os.environ.copy()
        env["USER_GUARD_TEST_LINK"] = str(link)
        env["USER_GUARD_TEST_TARGET"] = str(target)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction "
             "-Path $env:USER_GUARD_TEST_LINK -Target $env:USER_GUARD_TEST_TARGET | Out-Null"],
            env=env, capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            raise OSError("当前系统无法建立测试目录链接或联接")


class TestEndToEndIntegration(unittest.TestCase):
    def test_01_plugin_manifest_rules_and_agents_compliance(self) -> None:
        """验证插件结构严格符合 Antigravity 官方 Native Plugin 规范"""
        plugin_json = json.loads((ACTIVE_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        allowed_keys = {
            "name",
            "description",
            "logo",
            "suggestedPrompts",
            "displayName",
            "version",
            "author",
            "homepage",
            "repository",
            "disabled",
        }
        self.assertEqual(plugin_json.get("name"), "user-guard")
        self.assertTrue(set(plugin_json.keys()).issubset(allowed_keys), "plugin.json 不可包含未定义顶层字段")

        hooks_json = json.loads((ACTIVE_PLUGIN_DIR / "hooks.json").read_text(encoding="utf-8"))
        lifecycle = hooks_json.get("user-guard-lifecycle", {})
        self.assertIn("SessionStart", lifecycle)
        self.assertIn("PreInvocation", lifecycle)
        self.assertIn("PreToolUse", lifecycle)
        self.assertIn("Stop", lifecycle)
        self.assertEqual(len(lifecycle["Stop"]), 1, "Stop 阶段仅挂载防刷屏守卫，不挂载生硬日常拦截")

        rules_file = ACTIVE_PLUGIN_DIR / "rules" / "AGENTS.md"
        self.assertTrue(rules_file.exists(), "应使用官方推荐的 rules/AGENTS.md")
        rules_text = rules_file.read_text(encoding="utf-8")
        self.assertFalse(rules_text.startswith("---"), "rules/AGENTS.md 按官方规范应为无 frontmatter 的纯 Markdown")
        for r_code in ("R0.", "R1.", "R2.", "R3.", "R3.1.", "R3.6.", "R4.", "R5.", "R6.", "R7.", "R8.", "R9.", "R10.", "R11."):
            self.assertIn(r_code, rules_text)
        self.assertIn("CAE", rules_text)

        for agent_name in ("readonly-auditor.md", "readonly-reader.md", "readonly-collaborator.md"):
            agent_path = ACTIVE_PLUGIN_DIR / "agents" / agent_name
            self.assertTrue(agent_path.exists())
            agent_text = agent_path.read_text(encoding="utf-8")
            self.assertIn(
                "tools: [read_file, search_web, finish]",
                agent_text,
                "只读代理须精确限定为只读工具及 agy --json-schema 结构化收尾工具 finish",
            )
            self.assertIn("subagent: true", agent_text)
            self.assertIn("mainAgent: true", agent_text, "agy CLI --agent 需要 mainAgent: true 方可识别")
            self.assertIn("hidden: true", agent_text)

        skill_md = ACTIVE_PLUGIN_DIR / "skills" / "cae-harness" / "SKILL.md"
        skill_script = ACTIVE_PLUGIN_DIR / "skills" / "cae-harness" / "scripts" / "cae_harness.py"
        self.assertTrue(skill_md.exists(), "应包含渐进式加载按需技能 cae-harness/SKILL.md")
        self.assertTrue(skill_script.exists(), "应包含确定性总控脚本 cae_harness.py")
        skill_text = skill_md.read_text(encoding="utf-8")
        self.assertTrue(skill_text.startswith("---\nname: cae-harness"), "SKILL.md 必须具备合规 YAML frontmatter")
        self.assertIn("scripts/cae_harness.py", skill_text)

        license_file = REPO_ROOT / "LICENSE"
        self.assertTrue(license_file.is_file(), "开源仓库根目录必须包含标准 LICENSE 许可证文件")
        license_text = license_file.read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)

        root_agents_md = REPO_ROOT / "AGENTS.md"
        self.assertTrue(root_agents_md.is_file(), "仓库根目录须包含供双端共读自举配置的 AGENTS.md")
        root_agents_text = root_agents_md.read_text(encoding="utf-8")
        self.assertIn("--probe-apps", root_agents_text)
        self.assertIn("--app codex", root_agents_text)
        self.assertIn("--app antigravity", root_agents_text)

        codex_tpl = REPO_ROOT / "template" / "codex"
        self.assertTrue((codex_tpl / "hooks.json").is_file())
        for toml_name in ("readonly-auditor.toml", "readonly-reader.toml", "readonly-collaborator.toml"):
            toml_path = codex_tpl / "agents" / toml_name
            self.assertTrue(toml_path.is_file(), f"缺少 Codex 只读子代理模板: {toml_name}")
            toml_text = toml_path.read_text(encoding="utf-8")
            self.assertIn('name = "readonly-', toml_text)
            self.assertIn("严禁修改任何文件", toml_text)

    def test_02_zero_personal_info_leakage_across_repo(self) -> None:
        """全仓库源码、配置、测试与文档 100% 脱敏审计（零硬编码个人主目录、主机路径或 API 密钥）"""
        allowed_placeholders = {
            "...", "<user>", "user", "username", "yourname", "example",
            "alice", "bob", "public", "default", "test", "mock",
        }
        hardcoded_home_patterns = (
            re.compile(r"[A-Za-z]:[/\\]{1,2}Users[/\\]{1,2}([^/\\\'\"\s`<>]+)"),
            re.compile(r"(?<![A-Za-z0-9_])[/\\]{1,2}home[/\\]{1,2}([^/\\\'\"\s`<>]+)"),
            re.compile(r"(?<![A-Za-z0-9_])[/\\]{1,2}Users[/\\]{1,2}([^/\\\'\"\s`<>]+)"),
        )
        secret_patterns = (
            re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
            re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        )

        # 动态获取当前机器真实主目录与当前本地克隆路径（任何真实机器路径绝不可硬编码进仓库）
        home_variants = {str(Path.home()), Path.home().as_posix(), str(REPO_ROOT), REPO_ROOT.as_posix()}

        checked_count = 0
        for p in REPO_ROOT.rglob("*"):
            if not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
                continue
            if p.suffix.lower() in {".py", ".md", ".json", ".toml", ".yaml", ".yml", ".ps1", ".sh", ".gitignore", ".gitattributes"} or p.name in {".gitignore", ".gitattributes", "LICENSE"}:
                # 排除测试文件自身对正则字面量的定义误匹配
                content = p.read_text(encoding="utf-8")
                rel_name = str(p.relative_to(REPO_ROOT))
                checked_count += 1

                for hv in home_variants:
                    if len(hv) >= 6:
                        self.assertNotIn(
                            hv,
                            content,
                            f"文件 {rel_name} 中检测到当前机器主目录路径硬编码: {hv}",
                        )

                if p.name != "test_e2e_integration.py":
                    for hp in hardcoded_home_patterns:
                        for m in hp.finditer(content):
                            captured = m.group(1).strip().lower()
                            if captured and captured not in allowed_placeholders and not captured.startswith("$"):
                                self.fail(f"文件 {rel_name} 中检测到未脱敏的用户路径片段: {m.group(0)}")

                for sp in secret_patterns:
                    self.assertIsNone(sp.search(content), f"文件 {rel_name} 中检测到疑似密钥或私钥字面量")

        self.assertGreaterEqual(checked_count, 16)

    def test_03_full_cli_subprocess_utf8_pipeline(self) -> None:
        """通过真实操作系统子进程管道运行全部 4 个生命周期事件，验证 UTF-8 稳定性与协同契约"""
        hooks_cfg = json.loads((ACTIVE_PLUGIN_DIR / "hooks.json").read_text(encoding="utf-8"))["user-guard-lifecycle"]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plugin_data = tmp_path / "plugin_data"
            conv_id = "e2e-subprocess-conv"
            tp = tmp_path / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
            tp.parent.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env["PLUGIN_DATA"] = str(plugin_data)
            env["PYTHONDONTWRITEBYTECODE"] = "1"

            def run_hook(cmd_str: str, payload: dict) -> dict:
                cmd = _build_subprocess_cmd(cmd_str)
                proc = subprocess.run(
                    cmd,
                    cwd=str(ACTIVE_PLUGIN_DIR),
                    input=json.dumps(payload, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=env,
                    timeout=15,
                )
                self.assertEqual(proc.returncode, 0, f"Hook 执行失败 stderr={proc.stderr}")
                return json.loads(proc.stdout.strip())

            # 1) 写入首轮原话，未压缩时 PreInvocation 零注入
            tp.write_text(
                json.dumps(
                    {
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>\n【立项要求】保持通量守恒，输出中文技术报告。\n</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            pre_cmd = hooks_cfg["PreInvocation"][0]["command"]
            out1 = run_hook(pre_cmd, {"conversationId": conv_id, "transcriptPath": str(tp), "hookEventName": "PreInvocation"})
            self.assertEqual(out1["injectSteps"], [])

            # 2) 写入 CHECKPOINT 后，PreInvocation 精确注入 1 次，二次调用 0 注入
            with open(tp, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"step_index": 5, "source": "SYSTEM", "type": "CHECKPOINT"}) + "\n")
            out2 = run_hook(pre_cmd, {"conversationId": conv_id, "transcriptPath": str(tp), "hookEventName": "PreInvocation"})
            self.assertEqual(len(out2["injectSteps"]), 1)
            self.assertIn("【立项要求】保持通量守恒", out2["injectSteps"][0]["systemMessage"]["systemMessage"])

            out3 = run_hook(pre_cmd, {"conversationId": conv_id, "transcriptPath": str(tp), "hookEventName": "PreInvocation"})
            self.assertEqual(out3["injectSteps"], [])

            # 3) PreToolUse status_guard 子进程测试
            ptu_cmd = hooks_cfg["PreToolUse"][0]["hooks"][0]["command"]
            sim_log = tmp_path / "sim.log"
            sim_log.write_text("residual=1e-3\n", encoding="utf-8")
            p_read = {
                "conversationId": conv_id,
                "cwd": str(tmp_path),
                "toolCall": {"name": "read_file", "args": {"Path": str(sim_log), "StartLine": 1, "EndLine": 50}},
            }
            self.assertEqual(run_hook(ptu_cmd, p_read)["decision"], "allow")
            self.assertEqual(run_hook(ptu_cmd, p_read)["decision"], "deny")

            # 3b) PreToolUse 项目目录硬边界子进程测试（越界写入返回 force_ask）
            proj_sub = tmp_path / "workspace_proj"
            proj_sub.mkdir()
            p_ext = {
                "conversationId": conv_id,
                "cwd": str(proj_sub),
                "workspacePaths": [str(proj_sub)],
                "toolCall": {"name": "write_file", "args": {"Path": str(tmp_path / ".codex-worktrees" / "leak.txt")}},
            }
            out_ext = run_hook(ptu_cmd, p_ext)
            self.assertEqual(out_ext["decision"], "force_ask")
            self.assertIn("项目目录外", out_ext["reason"])

            # 4) Stop reply_repeat_guard 子进程日常回复秒放行测试
            stop_cmd = hooks_cfg["Stop"][0]["command"]
            with open(tp, "a", encoding="utf-8", newline="\n") as f:
                f.write(
                    json.dumps({"step_index": 6, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "已完成通量守恒校验。"}, ensure_ascii=False)
                    + "\n"
                )
            out_stop = run_hook(stop_cmd, {"conversationId": conv_id, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tp)})
            self.assertEqual(out_stop["decision"], "allow")

    def test_04_installer_mirror_sync_cleans_stale_files_without_pycache(self) -> None:
        """验证 install.py 镜像同步自动清除陈旧文件与废弃 *_stop_gate.json，保留原话归档且零 .pyc 残留"""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_plugin = Path(tmp) / "plugins" / "user-guard"
            # 预置模拟的历史遗留脏文件
            (sandbox_plugin / "rules").mkdir(parents=True)
            (sandbox_plugin / "hooks" / "__pycache__").mkdir(parents=True)
            (sandbox_plugin / "hooks" / "state").mkdir(parents=True)

            stale_rules = sandbox_plugin / "rules" / "global_rules.md"
            stale_mcp = sandbox_plugin / "mcp_config.json"
            stale_test = sandbox_plugin / "hooks" / "test_old.py"
            stale_pyc = sandbox_plugin / "hooks" / "__pycache__" / "old.cpython-313.pyc"
            stale_stop_gate = sandbox_plugin / "hooks" / "state" / "abc_stop_gate.json"
            valid_words_md = sandbox_plugin / "hooks" / "state" / "abc_user_words.md"

            for sf in (stale_rules, stale_mcp, stale_test, stale_pyc, stale_stop_gate):
                sf.write_text("stale", encoding="utf-8")
            valid_words_md.write_text("# 历史有效原话归档", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(REPO_ROOT / "install.py"),
                    "--target-dir",
                    str(sandbox_plugin),
                    "--skip-test",
                    "--no-zotero",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, f"install.py 失败: {proc.stderr}\n{proc.stdout}")

            # 断言脏文件已全部清除，新文件已就位，历史原话归档完好无损
            self.assertFalse(stale_rules.exists())
            self.assertFalse(stale_mcp.exists())
            self.assertFalse(stale_test.exists())
            self.assertFalse(stale_pyc.exists())
            self.assertFalse(stale_stop_gate.exists())
            self.assertTrue((sandbox_plugin / "rules" / "AGENTS.md").exists())
            self.assertTrue((sandbox_plugin / "README.md").exists())
            self.assertTrue(valid_words_md.exists())
            self.assertEqual(list(sandbox_plugin.rglob("__pycache__")), [])

            # 直接执行安装后生成的平台原生 hooks.json 命令，验证解释器探测真实可用
            installed_hooks = json.loads((sandbox_plugin / "hooks.json").read_text(encoding="utf-8"))
            raw_hook_cmd = installed_hooks["user-guard-lifecycle"]["SessionStart"][0]["command"]
            hook_env = os.environ.copy()
            hook_env["PLUGIN_DATA"] = str(Path(tmp) / "plugin_data")
            hook_proc = subprocess.run(
                shlex.split(raw_hook_cmd, posix=(os.name != "nt")),
                cwd=str(sandbox_plugin),
                input=json.dumps({"conversationId": "installed-cmd-check", "hookEventName": "SessionStart"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=hook_env,
                timeout=15,
            )
            self.assertEqual(hook_proc.returncode, 0, f"原生安装命令执行失败: {hook_proc.stderr}")
            self.assertEqual(json.loads(hook_proc.stdout.strip())["injectSteps"], [])

    def test_05_dry_run_preserves_source_and_all_install_targets(self) -> None:
        """预演保留源树缓存、目标陈旧文件与其他技能，且不创建新目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            shutil.copy2(REPO_ROOT / "install.py", source / "install.py")
            shutil.copytree(TEMPLATE_DIR, source / "template" / "user-guard")
            source_cache = source / "__pycache__" / "existing.pyc"
            source_cache.parent.mkdir()
            source_cache.write_bytes(b"retain source cache")
            plugin = root / "plugin target"
            plugin.mkdir()
            (plugin / "old.txt").write_text("retain during dry run", encoding="utf-8")
            codex_root = root / "codex"
            skills = codex_root / "skills"
            (skills / ".system").mkdir(parents=True)
            (skills / ".system" / "existing.txt").write_text("retain system skill", encoding="utf-8")

            def snapshot() -> dict:
                return {
                    entry.relative_to(root).as_posix(): entry.read_bytes() if entry.is_file() else None
                    for entry in root.rglob("*")
                }

            before = snapshot()
            result = subprocess.run(
                [sys.executable, "-B", str(source / "install.py"), "--target-dir", str(plugin),
                 "--codex-skills-dir", str(skills), "--dry-run", "--no-zotero"],
                capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(), before)

    def test_06_explicit_dual_install_preserves_other_codex_skills(self) -> None:
        """同源安装两个目标，仅镜像 Codex 的 cae-harness 子目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / "插件 空间" / "user-guard"
            skills = root / "codex" / "skills"
            peer = skills / ".system" / "keep.txt"
            peer.parent.mkdir(parents=True)
            peer.write_bytes(b"existing system skill")
            stale = skills / "cae-harness" / "old.txt"
            stale.parent.mkdir()
            stale.write_bytes(b"old skill")
            result = subprocess.run(
                [sys.executable, "-B", str(REPO_ROOT / "install.py"), "--target-dir", str(plugin),
                 "--codex-skills-dir", str(skills), "--skip-test", "--no-zotero"],
                capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(peer.read_bytes(), b"existing system skill")
            self.assertFalse(stale.exists())
            for rel in (Path("SKILL.md"), Path("scripts") / "cae_harness.py"):
                source_bytes = (TEMPLATE_DIR / "skills" / "cae-harness" / rel).read_bytes()
                self.assertEqual((skills / "cae-harness" / rel).read_bytes(), source_bytes)
                self.assertEqual((plugin / "skills" / "cae-harness" / rel).read_bytes(), source_bytes)

    def test_07_custom_plugin_target_does_not_write_default_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_root = root / "configured-codex"
            codex_root.mkdir()
            marker = codex_root / "config.toml"
            marker.write_bytes(b"retain configuration")
            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_root)
            result = subprocess.run(
                [sys.executable, "-B", str(REPO_ROOT / "install.py"), "--target-dir",
                 str(root / "plugin"), "--skip-test", "--no-zotero"],
                env=env, capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(marker.read_bytes(), b"retain configuration")
            self.assertEqual(sorted(entry.name for entry in codex_root.iterdir()), ["config.toml"])

    def test_08_codex_target_discovery_respects_home_and_switches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_root = root / "user"
            user_root.mkdir()
            custom = root / "configured-codex"
            custom.mkdir()
            with mock.patch.object(INSTALLER.Path, "home", return_value=user_root):
                with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
                    self.assertIsNone(INSTALLER.codex_skill_target("", False, False))
                    (user_root / ".codex").mkdir()
                    self.assertEqual(INSTALLER.codex_skill_target("", False, False),
                                     user_root / ".codex" / "skills" / "cae-harness")
                with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom)}):
                    self.assertEqual(INSTALLER.codex_skill_target("", False, False),
                                     custom / "skills" / "cae-harness")
                    self.assertIsNone(INSTALLER.codex_skill_target("", True, False))
                    self.assertIsNone(INSTALLER.codex_skill_target("", False, True))

    def test_09_rejects_source_overlap_and_linked_install_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repository"
            repo.mkdir()
            target = root / "custom installation"
            target.mkdir()
            external_dir = root / "external"
            external_dir.mkdir()
            external = external_dir / "keep.txt"
            external.write_bytes(b"outside target")
            for invalid in (repo, repo / "nested", repo.parent, Path.home()):
                with self.subTest(target=invalid):
                    with self.assertRaises(ValueError):
                        INSTALLER.validate_install_target(invalid, repo)
            self.assertEqual(INSTALLER.validate_install_target(target, repo), target.resolve())
            link = target / "linked-directory"
            try:
                _create_directory_link(link, external_dir)
            except OSError:
                self.skipTest("当前系统没有创建符号链接的权限")
            with self.assertRaises(ValueError):
                INSTALLER.validate_install_target(target, repo)
            self.assertEqual(external.read_bytes(), b"outside target")

    def test_10_rejects_ancestor_links_into_production_install_targets(self) -> None:
        """临时目标的祖先链接不能把隔离安装重定向到默认生产目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repository"
            repo.mkdir()
            user_root = root / "fake-user"
            codex_root = root / "configured-codex"
            production_targets = [
                user_root / ".gemini" / "config" / "plugins" / "user-guard",
                user_root / ".codex" / "skills" / "cae-harness",
                codex_root / "skills" / "cae-harness",
            ]
            with mock.patch.object(INSTALLER.Path, "home", return_value=user_root):
                with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_root)}):
                    for index, production in enumerate(production_targets):
                        production.mkdir(parents=True)
                        marker = production / "keep.txt"
                        marker.write_bytes(b"active installation")
                        alias = root / f"temporary-{index}"
                        try:
                            _create_directory_link(alias, production.parent)
                        except OSError:
                            self.skipTest("当前系统没有创建符号链接的权限")
                        with self.subTest(target=production):
                            with self.assertRaisesRegex(ValueError, "祖先链接"):
                                INSTALLER.validate_install_target(alias / production.name, repo)
                            self.assertEqual(INSTALLER.validate_install_target(production, repo), production.resolve())
                            self.assertEqual(marker.read_bytes(), b"active installation")

                    custom = root / "custom installation"
                    custom.mkdir()
                    alias = root / "custom alias"
                    _create_directory_link(alias, custom)
                    self.assertEqual(
                        INSTALLER.validate_install_target(alias / "new-plugin", repo),
                        custom.resolve() / "new-plugin",
                    )

    def test_11_custom_sibling_target_never_mutates_default_mcp_config(self) -> None:
        """显式安装至 ~/.gemini/config/plugins/<staging> 时禁止修改真实 mcp_config.json。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            real_cfg = fake_home / ".gemini" / "config"
            real_cfg.mkdir(parents=True)
            mcp_cfg = real_cfg / "mcp_config.json"
            mcp_cfg.write_text('{"mcpServers":{}}\n', encoding="utf-8")
            fake_zotero = root / "zotero-mcp"
            fake_zotero.write_text("stub", encoding="utf-8")
            staging = real_cfg / "plugins" / "user-guard-staging"
            with mock.patch.object(INSTALLER.Path, "home", return_value=fake_home):
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "install.py",
                        "--target-dir",
                        str(staging),
                        "--zotero-cmd",
                        str(fake_zotero),
                        "--skip-test",
                        "--no-codex-skill",
                    ],
                ):
                    with (
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        INSTALLER.main()
            self.assertEqual(mcp_cfg.read_text(encoding="utf-8"), '{"mcpServers":{}}\n')

    def test_12_installer_frontier_max_model_sync(self) -> None:
        """验证安装器动态选取首族最高挡模型且不破坏 config.toml 下游表结构。"""
        label = INSTALLER._select_agy_frontier_max_label(
            [
                {"slug": "alpha-hydrogen-xhigh", "display_name": "Alpha Hydrogen XHigh"},
                {"slug": "alpha-pro-orchestrator", "display_name": "Orchestrator (Core)"},
                {"slug": "alpha-core-medium", "display_name": "Alpha Core (Medium)"},
                {"slug": "alpha-core-high", "display_name": "Alpha Core (High)"},
                {"slug": "alpha-core-ultra", "display_name": "Alpha Core (Ultra)"},
                {"slug": "alpha-core-low", "display_name": "Alpha Core (Low)"},
                {"slug": "alpha-lite-high", "display_name": "Alpha Lite (High)"},
            ]
        )
        self.assertEqual(label, "Alpha Core (High)")

        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                'model = "older-model"\nmodel_reasoning_effort = "ultra"\n\n[projects."demo"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            (codex_dir / "models_cache.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "frontier-top",
                                "priority": 1,
                                "visibility": "list",
                                "supported_reasoning_levels": [
                                    {"effort": "low", "description": "Fast"},
                                    {"effort": "high", "description": "Greater reasoning"},
                                    {"effort": "max", "description": "Maximum reasoning depth"},
                                    {"effort": "ultra", "description": "Maximum reasoning with automatic task delegation"},
                                ],
                            },
                            {
                                "slug": "older-model",
                                "priority": 2,
                                "supported_reasoning_levels": [{"effort": "medium"}],
                            },
                            {"slug": "missing-metadata", "priority": 3, "supported_reasoning_levels": None},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            cfg = codex_dir / "config.toml"
            initial = cfg.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_frontier_max(codex_dir)
            self.assertEqual(cfg.read_bytes(), initial)

            for slug in ("custom-unknown", "missing-metadata"):
                with self.subTest(slug=slug):
                    cfg.write_text(f'model = "{slug}"\n', encoding="utf-8")
                    original = cfg.read_bytes()
                    with contextlib.redirect_stdout(io.StringIO()) as report:
                        INSTALLER._sync_codex_frontier_max(codex_dir)
                    self.assertEqual(cfg.read_bytes(), original)
                    self.assertNotIn("已写入", report.getvalue())

            cfg.write_text('"model" = "older-model" # 用户模型\nmodel_reasoning_effort = "" # 待补齐\n', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_frontier_max(codex_dir)
            self.assertEqual(cfg.read_text(encoding="utf-8"),
                             '"model" = "older-model" # 用户模型\nmodel_reasoning_effort = "medium" # 待补齐\n')

            cfg.write_bytes(initial)
            cmd = [sys.executable, "-B", "-X", "utf8", str(REPO_ROOT / "install.py"), "--app", "codex",
                   "--codex-home", str(codex_dir), "--skip-test", "--no-zotero"]
            result = subprocess.run(cmd + ["--sync-model-defaults", "--no-model-config"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(cfg.read_bytes(), initial)
            cfg.write_text('model = "older-model"\n', encoding="utf-8")
            result = subprocess.run(cmd + ["--no-model-config"], capture_output=True,
                                    text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("model_reasoning_effort", cfg.read_text(encoding="utf-8"))
            cfg.write_bytes(initial)
            result = subprocess.run(cmd + ["--sync-model-defaults"], capture_output=True,
                                    text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = cfg.read_text(encoding="utf-8")
            self.assertIn('model = "frontier-top"', updated)
            self.assertIn('model_reasoning_effort = "max"', updated)
            self.assertNotIn('model_reasoning_effort = "ultra"', updated)
            self.assertIn('[projects."demo"]', updated)

            # 无 delegation/max 的模型列表应正确选出最高非委派挡位 high
            (codex_dir / "models_cache.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "frontier-high-only",
                                "priority": 1,
                                "visibility": "list",
                                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_frontier_max(codex_dir, overwrite=True)
            updated_high = (codex_dir / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "frontier-high-only"', updated_high)
            self.assertIn('model_reasoning_effort = "high"', updated_high)
            cfg.unlink()
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_frontier_max(codex_dir)
            self.assertIn('model = "frontier-high-only"', cfg.read_text(encoding="utf-8"))
            self.assertIn('model_reasoning_effort = "high"', cfg.read_text(encoding="utf-8"))

    def test_12_agy_default_model_preservation_and_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gemini_root = Path(tmp) / ".gemini"
            cli_dir = gemini_root / "antigravity-cli"
            cli_dir.mkdir(parents=True)
            settings = cli_dir / "settings.json"
            settings.write_bytes(b'{"model":"Account custom (Ultra)","other":true}\n')
            source = settings.read_bytes()
            models = [{"slug": "alpha-core-high", "display_name": "Alpha Core (High)"},
                      {"slug": "alpha-core-ultra", "display_name": "Alpha Core (Ultra)"}]
            with mock.patch.object(INSTALLER.shutil, "which", return_value=None), \
                    mock.patch.object(INSTALLER.Path, "home", return_value=Path(tmp)), \
                    mock.patch.object(INSTALLER, "_load_wsl_windows_agy_cache", return_value=models), \
                    contextlib.redirect_stdout(io.StringIO()):
                INSTALLER.configure_agy_frontier_max_default(gemini_root)
                self.assertEqual(settings.read_bytes(), source)
                INSTALLER.configure_agy_frontier_max_default(gemini_root, overwrite=True)
                self.assertEqual(json.loads(settings.read_bytes()), {"model": "Alpha Core (High)", "other": True})
                settings.unlink()
                desktop_settings = gemini_root / "config" / "config.json"
                desktop_settings.parent.mkdir()
                desktop_settings.write_bytes(b'{"userSettings":{"model":"Orchestrator (Core)"}}\n')
                INSTALLER.configure_agy_frontier_max_default(gemini_root)
                self.assertFalse(settings.exists())
                desktop_settings.unlink()
                INSTALLER.configure_agy_frontier_max_default(gemini_root)
                self.assertEqual(json.loads(settings.read_bytes())["model"], "Alpha Core (High)")

    def test_13_installer_codex_workspace_boundary_sync(self) -> None:
        """验证安装器将 Codex 沙箱收紧为 workspace-write + on-request + network_access=true，幂等且保留下游表。"""
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            cfg = codex_dir / "config.toml"
            cfg.write_text(
                'sandbox_mode = "danger-full-access"\n'
                'model = "frontier-top"\n\n'
                '[projects."demo"]\n'
                'trust_level = "trusted"\n\n'
                '[hooks.state."session_start"]\n'
                'trusted_hash = "abc123"\n',
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_workspace_boundary(codex_dir, dry_run=True)
            self.assertIn('sandbox_mode = "danger-full-access"', cfg.read_text(encoding="utf-8"), "dry-run 不应改写文件")

            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_workspace_boundary(codex_dir, dry_run=False)
            first_pass = cfg.read_text(encoding="utf-8")
            self.assertNotIn("danger-full-access", first_pass)
            self.assertIn('sandbox_mode = "workspace-write"', first_pass)
            self.assertIn('approval_policy = "on-request"', first_pass)
            self.assertIn("[sandbox_workspace_write]\nnetwork_access = true", first_pass)
            self.assertIn('[projects."demo"]', first_pass)
            self.assertIn('trusted_hash = "abc123"', first_pass)

            # 二次调用严格幂等
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_workspace_boundary(codex_dir, dry_run=False)
            self.assertEqual(cfg.read_text(encoding="utf-8"), first_pass)

    def test_14_installer_codex_global_rules_sync_preserves_custom_env_section(self) -> None:
        """验证安装器同步 R0-R9 全局核心总纲至 ~/.codex/AGENTS.md 时，原封不动保留用户末尾自定义的本机环境段。"""
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            codex_agents = codex_dir / "AGENTS.md"
            custom_env_tail = (
                "## 工具与本机\n"
                "R10. 自定义子系统内存隔离上限配置说明。\n"
                "R11. 自定义本地开发工作站环境与默认编辑器说明。"
            )
            legacy_content = "# 用户规则（旧版）\n\n## 表达与背景\nR0. 旧版精简规则。\n\n" + custom_env_tail
            codex_agents.write_text(legacy_content, encoding="utf-8")

            tpl_rules = TEMPLATE_DIR / "rules" / "AGENTS.md"
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_global_rules(tpl_rules, codex_dir, dry_run=True)
            self.assertEqual(codex_agents.read_text(encoding="utf-8"), legacy_content, "dry-run 不应修改 AGENTS.md")

            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_global_rules(tpl_rules, codex_dir, dry_run=False)
            synced = codex_agents.read_text(encoding="utf-8")
            self.assertIn("R3.1. 并行子代理与双向协作编排", synced)
            self.assertIn("R3.6. 验收红线与诚实披露", synced)
            self.assertTrue(synced.endswith("\n\n" + custom_env_tail + "\n"))
            self.assertNotIn("## 工具与运行环境", synced)

            # 二次调用严格幂等
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_global_rules(tpl_rules, codex_dir, dry_run=False)
            self.assertEqual(codex_agents.read_text(encoding="utf-8"), synced)

    def test_15_standalone_codex_full_sandbox_install_and_hook_subprocess(self) -> None:
        """验证单 Codex 模式 (--app codex --codex-home) 全量独立部署、Zotero 合并、信任哈希保持及钩子子进程运行"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "standalone_codex_home"
            fake_zotero = root / "zotero-mcp-stub"
            fake_zotero.write_text("stub", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(REPO_ROOT / "install.py"),
                    "--app",
                    "codex",
                    "--codex-home",
                    str(codex_home),
                    "--zotero-cmd",
                    str(fake_zotero),
                    "--skip-test",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            # 1) 验证全套 Codex 独立工作流资产已生成，且未生成反重力目录
            self.assertFalse((root / "plugins").exists())
            self.assertTrue((codex_home / "AGENTS.md").is_file())
            for name in ("readonly-reader.toml", "readonly-auditor.toml", "readonly-collaborator.toml"):
                self.assertTrue((codex_home / "agents" / name).is_file())
            self.assertTrue((codex_home / "hooks" / "session_context_loader.py").is_file())
            self.assertTrue((codex_home / "hooks" / "user_words.py").is_file())
            self.assertTrue((codex_home / "skills" / "cae-harness" / "SKILL.md").is_file())

            cfg_text = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('sandbox_mode = "workspace-write"', cfg_text)
            self.assertIn('approval_policy = "on-request"', cfg_text)
            self.assertIn('model_reasoning_summary = "auto"', cfg_text)
            self.assertIn("network_access = true", cfg_text)
            self.assertIn("HOOKS = false", cfg_text)
            self.assertIn("[mcp_servers.zotero]", cfg_text)

            # 2) 二次运行必须保持已有有效 hooks.json 字节完全不变（保护 Codex trusted_hash）
            hooks_json_path = codex_home / "hooks.json"
            first_hooks_bytes = hooks_json_path.read_bytes()
            proc2 = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(REPO_ROOT / "install.py"),
                    "--app",
                    "codex",
                    "--codex-home",
                    str(codex_home),
                    "--no-zotero",
                    "--skip-test",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
            )
            self.assertEqual(proc2.returncode, 0, proc2.stderr + proc2.stdout)
            self.assertEqual(hooks_json_path.read_bytes(), first_hooks_bytes)

            # 3) 真实子进程调用生成的 Codex hooks.json 恢复命令
            hooks_doc = json.loads(hooks_json_path.read_text(encoding="utf-8"))
            raw_cmd = hooks_doc["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            sid = "standalone-codex-e2e-001"
            rollout = codex_home / "sessions" / "2026" / "11" / "13" / f"rollout-2026-11-13T00-00-00-{sid}.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(root), "source": "cli"}})
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "ordinal": 1,
                        "timestamp": "2026-11-13T00:00:05Z",
                        "payload": {"id": "u1", "type": "message", "role": "user", "content": "单 Codex 独立工作流原话测试"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            hook_proc = subprocess.run(
                _build_subprocess_cmd(raw_cmd),
                input=json.dumps(
                    {
                        "session_id": sid,
                        "transcript_path": str(rollout),
                        "cwd": str(root),
                        "hook_event_name": "SessionStart",
                        "source": "compact",
                    },
                    ensure_ascii=False,
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            self.assertEqual(hook_proc.returncode, 0, hook_proc.stderr)
            out_json = json.loads(hook_proc.stdout.strip())
            self.assertEqual(out_json["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("单 Codex 独立工作流原话测试", out_json["hookSpecificOutput"]["additionalContext"])
            self.assertTrue((codex_home / "hooks" / "state" / "user-words" / f"{sid}.md").is_file())

    def test_16_single_app_detection_prompt_and_standalone_modes(self) -> None:
        """验证仅安装单客户端时的交互提示、拒绝安全退出、确认单独安装及跨子系统互操作免打扰"""
        only_codex_det = {
            "antigravity": {"native_installed": False, "interop_available": False},
            "codex": {"native_installed": True, "interop_available": False},
        }
        # 用户选择 'n'（先去安装反重力）-> 干净退出码 0
        with mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                INSTALLER.resolve_app_plan("auto", False, False, only_codex_det, interactive=True, auto_yes=False, dry_run=False)
            self.assertEqual(cm.exception.code, 0)

        # 用户选择 'y'（不装反重力，仅配单 Codex）-> (False, True)
        with mock.patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
            plan_cx = INSTALLER.resolve_app_plan("auto", False, False, only_codex_det, interactive=True, auto_yes=False, dry_run=False)
        self.assertEqual(plan_cx, (False, True))

        # 仅安装反重力且 --yes -> (True, False)
        only_ag_det = {
            "antigravity": {"native_installed": True, "interop_available": False},
            "codex": {"native_installed": False, "interop_available": False},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            plan_ag = INSTALLER.resolve_app_plan("auto", False, False, only_ag_det, interactive=True, auto_yes=True, dry_run=False)
        self.assertEqual(plan_ag, (True, False))

        # WSL 原生仅装反重力、但 Windows 侧已装 Codex (interop_available=True) -> 免打扰直接返回 (True, False)
        wsl_interop_det = {
            "antigravity": {"native_installed": True, "interop_available": False},
            "codex": {"native_installed": False, "interop_available": True},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            plan_wsl = INSTALLER.resolve_app_plan("auto", False, False, wsl_interop_det, interactive=True, auto_yes=False, dry_run=False)
        self.assertEqual(plan_wsl, (True, False))

    def test_17_handoff_gate_subprocess_pipeline_and_codex_isolation(self) -> None:
        """分别执行反重力门禁和独立 Codex 提示钩子的子进程。"""
        hooks_cfg = json.loads((ACTIVE_PLUGIN_DIR / "hooks.json").read_text(encoding="utf-8"))["user-guard-lifecycle"]
        pre_cmd = hooks_cfg["PreInvocation"][0]["command"]
        ptu_cmd = hooks_cfg["PreToolUse"][0]["hooks"][0]["command"]
        stop_cmd = hooks_cfg["Stop"][0]["command"]
        self.assertIn("invoke_subagent", hooks_cfg["PreToolUse"][0]["matcher"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_data = root / "plugin_data"
            ws_dir = root / "proj"
            brain_dir = root / "brain" / "e2e-handoff-conv"
            ws_dir.mkdir(parents=True)
            tp = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
            tp.parent.mkdir(parents=True)

            env = {key: value for key, value in os.environ.items() if not key.startswith("USER_GUARD_")}
            env["PLUGIN_DATA"] = str(plugin_data)

            def run_sub(raw_cmd: str, payload_dict: dict) -> dict:
                proc = subprocess.run(
                    _build_subprocess_cmd(raw_cmd),
                    input=json.dumps(payload_dict, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=str(ACTIVE_PLUGIN_DIR),
                    env=env,
                    timeout=15,
                )
                self.assertEqual(proc.returncode, 0, f"{raw_cmd} 失败: {proc.stderr}")
                return json.loads(proc.stdout.strip())

            # 写入人类首条含 /goal 指令与 16 次 CHECKPOINT，直接激发 Stage-2 drain
            lines = [json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "/goal\n执行端到端长会话验证"}, ensure_ascii=False)]
            for idx in range(1, 17):
                lines.append(json.dumps({"step_index": idx * 100, "source": "SYSTEM", "type": "CHECKPOINT"}))
            tp.write_text("\n".join(lines) + "\n", encoding="utf-8")

            conv_id = "e2e-handoff-conv"
            out_pre = run_sub(pre_cmd, {"conversationId": conv_id, "transcriptPath": str(tp), "hookEventName": "PreInvocation"})
            self.assertEqual(len(out_pre["injectSteps"]), 1)
            self.assertIn("Stage-2 drain", out_pre["injectSteps"][0]["systemMessage"]["systemMessage"])

            # 子进程调用 invoke_subagent 必被阻断；写交接档放行
            out_subagent = run_sub(ptu_cmd, {"conversationId": conv_id, "cwd": str(ws_dir), "workspacePaths": [str(ws_dir)], "toolCall": {"name": "invoke_subagent", "args": {}}})
            self.assertEqual(out_subagent["decision"], "deny")

            out_handoff = run_sub(
                ptu_cmd,
                {
                    "conversationId": conv_id,
                    "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)],
                    "toolCall": {"name": "write_file", "args": {"Path": str(ws_dir / "HANDOFF_NEXT.md"), "Content": "# handoff"}},
                },
            )
            self.assertEqual(out_handoff["decision"], "allow")

            # Stop 子进程验收缺失 prompt / GOAL_CANCELLED 时阻断，补全后放行
            with open(tp, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"step_index": 1605, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "准备收尾"}, ensure_ascii=False) + "\n")
            out_stop_bad = run_sub(stop_cmd, {"conversationId": conv_id, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tp)})
            self.assertEqual(out_stop_bad["decision"], "continue")

            with open(tp, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"step_index": 1606, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "补齐交接"}, ensure_ascii=False) + "\n")
                f.write(
                    json.dumps(
                        {
                            "step_index": 1607,
                            "source": "MODEL",
                            "type": "PLANNER_RESPONSE",
                            "content": "交接已完成。\n\n### 新对话提示词\n```markdown\n请基于 HANDOFF_NEXT.md 接续验证并提交。\n```\n<!-- GOAL_CANCELLED -->",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            out_stop_ok = run_sub(stop_cmd, {"conversationId": conv_id, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tp)})
            self.assertEqual(out_stop_ok["decision"], "allow")

            # 运行单 Codex 安装生成的默认提示钩子。
            codex_only_home = root / "codex_only"
            proc_cx = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(REPO_ROOT / "install.py"),
                    "--app",
                    "codex",
                    "--codex-home",
                    str(codex_only_home),
                    "--no-zotero",
                    "--skip-test",
                    "--yes",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(proc_cx.returncode, 0, proc_cx.stderr)
            self.assertTrue((codex_only_home / "hooks" / "handoff_gate.py").is_file())
            self.assertTrue((codex_only_home / "hooks" / "session_context_loader.py").is_file())
            self.assertFalse((codex_only_home / "hooks" / "status_guard.py").exists())
            self.assertFalse((codex_only_home / "hooks" / "reply_repeat_guard.py").exists())
            codex_hooks = json.loads((codex_only_home / "hooks.json").read_text(encoding="utf-8"))["hooks"]
            self.assertEqual(set(codex_hooks), {"SessionStart", "PostCompact", "UserPromptSubmit", "PreToolUse", "Stop"})
            sid = "isolated-codex-handoff"
            rollout = codex_only_home / f"rollout-{sid}.jsonl"
            rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli"}}) + "\n", encoding="utf-8")
            codex_env = {key: value for key, value in env.items() if key != "PLUGIN_DATA"}
            for event, extra, output_key in (
                ("PostCompact", {"turn_id": "turn-1", "trigger": "auto"}, "systemMessage"),
                ("SessionStart", {"source": "compact"}, "hookSpecificOutput"),
            ):
                handler = next(h for h in codex_hooks[event][0]["hooks"] if "handoff_gate.py" in h["command"])
                self.assertIn("--mode advisory --compact-limit 0", handler["command"])
                result = subprocess.run(
                    _build_subprocess_cmd(handler["command"]),
                    input=json.dumps({"hook_event_name": event, "session_id": sid, "cwd": str(root),
                                      "transcript_path": str(rollout), **extra}),
                    capture_output=True, text=True, encoding="utf-8", cwd=codex_only_home, env=codex_env, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                outcome = json.loads(result.stdout)
                self.assertNotIn("decision", outcome)
                self.assertIn(output_key, outcome)
                self.assertIn("Codex 长会话", json.dumps(outcome, ensure_ascii=False))
            self.assertTrue(any((codex_only_home / "hooks" / "state" / "handoff").glob("*.json")))
            self.assertFalse((plugin_data / "state" / "handoff" / f"{sid}.json").exists())

    def test_18_codex_hook_upgrade_preserves_handlers_and_rejects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_root = Path(tmp) / "codex with spaces"
            codex_root.mkdir()
            hooks_path = codex_root / "hooks.json"
            loader_path = codex_root / "hooks" / "session_context_loader.py"
            loader_path.parent.mkdir()
            loader_path.write_bytes((TEMPLATE_DIR / "hooks" / loader_path.name).read_bytes())
            state_dir = (loader_path.parent / "state" / "session-context").as_posix()
            valid = {"type": "command", "command": f'"{sys.executable}" -B "{loader_path.as_posix()}" --client codex --state-dir "{state_dir}"',
                     "timeout": 33, "statusMessage": "保留已有设置"}
            sibling = {"type": "command", "command": "custom-session-start"}
            other_group = {"matcher": "startup", "hooks": [{"type": "command", "command": "custom-startup"}]}
            other_event = [{"hooks": [{"type": "command", "command": "custom-notification"}]}]
            other_gate = {"type": "command", "command": f'"{sys.executable}" "{(Path(tmp) / "custom" / "handoff_gate.py").as_posix()}"'}
            shared_path_gate = {"type": "command", "command": (
                f'"{sys.executable}" -B "{(loader_path.parent / "handoff_gate.py").as_posix()}" '
                f'--client codex --mode enforce --compact-limit 1 --state-dir "{(loader_path.parent / "state" / "handoff").as_posix()}"'
            )}
            legacy = {"hooks": {"SessionStart": [{"matcher": "^(compact|resume)$", "hooks": [valid, sibling]}, other_group],
                                "Notification": other_event, "Stop": [{"hooks": [other_gate, shared_path_gate]}]}}
            hooks_path.write_text(json.dumps(legacy), encoding="utf-8")
            cmd = [sys.executable, "-B", "-X", "utf8", str(REPO_ROOT / "install.py"), "--app", "codex",
                   "--codex-home", str(codex_root), "--no-zotero", "--skip-test"]

            def install(*options):
                return subprocess.run(cmd + list(options), capture_output=True, text=True, encoding="utf-8", timeout=20)

            result = install()
            self.assertEqual(result.returncode, 0, result.stderr)
            upgraded_bytes = hooks_path.read_bytes()
            upgraded = json.loads(upgraded_bytes)["hooks"]
            self.assertEqual(upgraded["SessionStart"][0]["hooks"][:2], [valid, sibling])
            self.assertEqual(upgraded["SessionStart"][1], other_group)
            self.assertEqual(upgraded["Notification"], other_event)
            self.assertEqual(upgraded["Stop"][0]["hooks"][:2], [other_gate, shared_path_gate])
            self.assertTrue({"PostCompact", "UserPromptSubmit", "PreToolUse"} <= set(upgraded))
            result = install()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(hooks_path.read_bytes(), upgraded_bytes)

            result = install("--codex-handoff-mode", "enforce", "--codex-handoff-compactions", "2")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"--mode enforce --compact-limit 2", hooks_path.read_bytes())
            result = install("--codex-handoff-mode", "off")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(hooks_path.read_bytes()), legacy)

            for malformed in (b"{bad json", b'{"hooks":{"SessionStart":{}}}', b'{"hooks":{},"hooks":{}}'):
                with self.subTest(malformed=malformed):
                    hooks_path.write_bytes(malformed)
                    result = install()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("无法安全合并", result.stderr)
                    self.assertEqual(hooks_path.read_bytes(), malformed)


    def test_20_codex_loader_upgrade_checks_old_python_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex"
            root.mkdir()
            loader = (root / "hooks" / "session_context_loader.py").as_posix()
            old = {"type": "command", "command": f'"{sys.executable}" -B "{loader}" --client codex',
                   "timeout": 33, "statusMessage": "custom status"}
            sibling = {"type": "command", "command": "custom-hook"}
            path = root / "hooks.json"
            path.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "^(compact|resume)$", "hooks": [old, sibling]}]}}), encoding="utf-8")
            with mock.patch.object(INSTALLER, "_is_working_python_cmd", return_value=False) as check:
                plan = INSTALLER._plan_codex_hooks(path, REPO_ROOT / "template/codex/hooks.json", "python -X utf8 -I -B", "advisory", 0)
            check.assert_called_once_with([sys.executable, "-B"])
            handlers = plan[1]["hooks"]["SessionStart"][0]["hooks"]
            self.assertNotEqual(handlers[0]["command"], old["command"])
            self.assertEqual(handlers[0]["timeout"], 33)
            self.assertEqual(handlers[0]["statusMessage"], "custom status")
            self.assertEqual(handlers[1], sibling)

    @unittest.skipIf(os.name == "nt", "POSIX Shell 路径转义")
    def test_21_posix_codex_hook_special_path_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex$cash`tick' space"
            root.mkdir()
            path = root / "hooks.json"
            template = REPO_ROOT / "template/codex/hooks.json"
            cmd = "python3 -X utf8 -I -B"
            first = INSTALLER._plan_codex_hooks(path, template, cmd, "advisory", 0)
            path.write_text(json.dumps(first[1]), encoding="utf-8")
            self.assertIsNone(INSTALLER._plan_codex_hooks(path, template, cmd, "advisory", 0))
            disabled = INSTALLER._plan_codex_hooks(path, template, cmd, "off", 0)
            remaining = [handler for groups in disabled[1]["hooks"].values() for group in groups for handler in group["hooks"]]
            self.assertEqual(len(remaining), 1)
            self.assertIn("session_context_loader.py", remaining[0]["command"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

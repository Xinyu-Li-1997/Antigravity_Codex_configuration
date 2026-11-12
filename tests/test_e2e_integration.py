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
    tokens = shlex.split(hook_cmd, posix=(os.name != "nt"))
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
                {"slug": "alpha-core-medium", "display_name": "Alpha Core (Medium)"},
                {"slug": "alpha-core-high", "display_name": "Alpha Core (High)"},
                {"slug": "alpha-core-low", "display_name": "Alpha Core (Low)"},
                {"slug": "alpha-lite-high", "display_name": "Alpha Lite (High)"},
            ]
        )
        self.assertEqual(label, "Alpha Core (High)")

        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                'model = "older-model"\nmodel_reasoning_effort = "medium"\n\n[projects."demo"]\ntrust_level = "trusted"\n',
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
                                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_frontier_max(codex_dir, dry_run=False)
            updated = (codex_dir / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "frontier-top"', updated)
            self.assertIn('model_reasoning_effort = "ultra"', updated)
            self.assertIn('[projects."demo"]', updated)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

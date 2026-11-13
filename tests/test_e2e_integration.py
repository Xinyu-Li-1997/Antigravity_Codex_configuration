"""
test_e2e_integration.py - 全系统端到端集成验证、隐私脱敏审计与沙箱安装测试套件

覆盖验证：
1. 插件清单、规则 (rules/AGENTS.md)、钩子编排 (hooks.json) 与只读子代理静态合规校验；
2. 仓库文本中的主目录、硬件型号和凭据字面量模式扫描；
3. 真实 CLI 子进程级联管道联调（UTF-8 中文符号无损传输、PreInvocation 增量压缩注入、PreToolUse 门禁、Stop 门禁）；
4. install.py 镜像同步安装器沙箱测试（自动清除旧版脏文件与废弃 *_stop_gate.json、保护有效原话归档、零 __pycache__ 污染）。
"""

import contextlib
import datetime
import getpass
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


ALLOWED_PLACEHOLDERS = {
    "...", "<user>", "user", "username", "yourname", "example", "alice", "bob", "charlie",
    "public", "default", "test", "mock", "fixture", "guest", "sample", "runner", "developer",
}
HARDCODED_HOME_PATTERNS = (
    re.compile(r"(?:[A-Za-z]:|/mnt/[A-Za-z])[/\\]{1,2}users[/\\]{1,2}([^/\\\'\"\s`<>]+)", re.I),
    re.compile(r"[/\\]{1,2}(?:home|users)[/\\]{1,2}([^/\\\'\"\s`<>]+)", re.I),
)
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|hf_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(?:access_token|refresh_token|id_token|api[_-]?key|client_secret)['\"]?\s*[:=]\s*['\"][A-Za-z0-9_.-]{24,}['\"]", re.I),
)
HARDWARE_PATTERNS = (
    re.compile(r"\b(?:NVIDIA\s+)?(?:GeForce\s+)?(?:RTX|GTX)\s+(?:PRO\s+)?[A-Z]?\d{3,4}\s*(?:Ti|Super|Ada)?\b", re.I),
    re.compile(r"\b(?:NVIDIA\s+)?(?:[AHV](?:100|200)|L40[S])\b", re.I),
    re.compile(r"\b(?:Radeon\s+)?RX\s+[5679]\d{3}\s*(?:XT|XTX)?\b", re.I),
    re.compile(r"\b(?:HP\s+)?OMEN\s+\d{2}[A-Za-z]\b", re.I),
    re.compile(r"\b(?:Intel\s+)?(?:Core\s+)?i[3579]-\d{4,5}[A-Za-z]{0,3}\b", re.I),
    re.compile(r"\b(?:Intel\s+)?Core\s+Ultra\s+[3579]\s+\d{3}[A-Za-z]{0,3}\b", re.I),
    re.compile(r"\b(?:Intel\s+)?Xeon(?:\s+(?:Platinum|Gold|Silver|Bronze|Max|W|D|E))?\s+(?:[A-Z]\d[- ]?)?\d{3,5}[A-Za-z+]*\b", re.I),
    re.compile(r"\b(?:AMD\s+)?(?:Ryzen(?:\s+Threadripper)?|Threadripper|EPYC)\s+(?:(?:[3579]|PRO)\s+)*[A-Za-z]?\d{3,5}[A-Za-z0-9]*\b", re.I),
    re.compile(r"\b(?:Apple\s+)?M[1-4]\s+(?:Pro|Max|Ultra)\b", re.I),
)
UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def _path_spelling_variants(paths):
    variants = set()
    for path in paths:
        posix = str(path).replace("\\", "/")
        spellings = {posix, posix.replace("/", "\\")}
        if re.match(r"^[A-Za-z]:/", posix):
            spellings.add("/mnt/" + posix[0].lower() + posix[2:])
        match = re.match(r"^/mnt/([A-Za-z])/(.*)$", posix)
        if match:
            windows = match[1] + ":/" + match[2]
            spellings.update((windows, windows.replace("/", "\\")))
        for spelling in spellings:
            variants.add(spelling.casefold())
            variants.add(spelling.replace("\\", "\\\\").casefold())
    return variants


def _sensitive_findings(content, home_variants=(), user_name="", hostname=""):
    """纯函数：扫描所有文本，测试夹具与规则定义使用同一套判据。"""
    findings = []
    low = content.casefold()
    if any(len(path) >= 6 and path.casefold() in low for path in home_variants):
        findings.append("本机路径")
    for label, value in (("用户名", user_name), ("主机名", hostname)):
        if len(value) >= 3 and value.casefold() not in ALLOWED_PLACEHOLDERS and value.casefold() in low:
            findings.append(label)
    for pattern in HARDCODED_HOME_PATTERNS:
        for match in pattern.finditer(content):
            captured = match[1].strip().casefold()
            if re.search(r"[\[\](){}+*?\\]", captured):
                continue  # 只跳过正则定义自身，不豁免任何文件。
            if captured and captured not in ALLOWED_PLACEHOLDERS and not captured.startswith("$"):
                findings.append("未脱敏主目录")
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        findings.append("凭据")
    if any(pattern.search(content) for pattern in HARDWARE_PATTERNS):
        findings.append("硬件型号")
    if UUID_PATTERN.search(content):
        findings.append("会话标识")
    for match in re.finditer(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", content):
        if match[1].lower() not in ("example.com", "example.org", "example.net", "example.invalid"):
            findings.append("邮箱")
    if re.search(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b", content):
        findings.append("内网地址")
    return findings


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
    def setUp(self) -> None:
        self._old_harness_depth = os.environ.pop("CAE_HARNESS_DEPTH", None)

    def tearDown(self) -> None:
        if self._old_harness_depth is None:
            os.environ.pop("CAE_HARNESS_DEPTH", None)
        else:
            os.environ["CAE_HARNESS_DEPTH"] = self._old_harness_depth

    def test_01_plugin_manifest_rules_and_agents_compliance(self) -> None:
        """验证插件结构严格符合 Antigravity 官方 Native Plugin 规范"""
        plugin_json = json.loads((ACTIVE_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        allowed_keys = {
            "$schema",
            "name",
            "description",
            "logo",
            "license",
            "suggestedPrompts",
            "displayName",
            "version",
            "author",
            "homepage",
            "repository",
            "disabled",
        }
        self.assertEqual(plugin_json.get("name"), "user-guard")
        self.assertEqual(plugin_json.get("$schema"), "https://antigravity.google/schemas/v1/plugin.json")
        self.assertEqual(plugin_json.get("license"), "Apache-2.0")
        self.assertEqual(plugin_json.get("logo"), "assets/logo.svg")
        self.assertTrue((ACTIVE_PLUGIN_DIR / "assets" / "logo.svg").is_file(), "plugin.json 声明的 logo.svg 必须存在")
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
        self.assertLess(
            len(rules_file.read_bytes()),
            16000,
            "rules/AGENTS.md UTF-8 字节数须显著低于 Antigravity 24,000 字节规则截断上限",
        )

        for agent_name in ("readonly-auditor.md", "readonly-reader.md", "readonly-collaborator.md"):
            agent_path = ACTIVE_PLUGIN_DIR / "agents" / agent_name
            self.assertTrue(agent_path.exists())
            agent_text = agent_path.read_text(encoding="utf-8")
            self.assertIn(
                "tools: [read_file, search_web, send_message, finish]",
                agent_text,
                "只读代理须精确限定为只读工具、父代理回传工具 send_message 及 agy --json-schema 收尾工具 finish",
            )
            self.assertIn("subagent: true", agent_text)
            self.assertIn("mainAgent: true", agent_text, "agy CLI --agent 需要 mainAgent: true 方可识别")
            self.assertNotIn("hidden: true", agent_text, "App v2.32.0 下 hidden: true 会将子代理从 <subagents> 目录隐藏")

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

        changelog_file = REPO_ROOT / "CHANGELOG.md"
        self.assertTrue(changelog_file.is_file(), "仓库根目录须维护规范的 CHANGELOG.md 更新日志")
        changelog_text = changelog_file.read_text(encoding="utf-8")
        self.assertIn(f"[{plugin_json.get('version')}]", changelog_text, "CHANGELOG.md 须记录当前 plugin.json 版本号")

        root_agents_md = REPO_ROOT / "AGENTS.md"
        self.assertTrue(root_agents_md.is_file(), "仓库根目录须包含供双端共读自举配置的 AGENTS.md")
        self.assertLess(
            len(root_agents_md.read_bytes()),
            16000,
            "根目录 AGENTS.md UTF-8 字节数须显著低于 Antigravity 24,000 字节规则截断上限",
        )
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

    def test_02_repository_personal_path_hardware_and_credential_patterns(self) -> None:
        """扫描仓库文本中与个人路径、具体硬件型号和常见凭据匹配的内容。"""
        home_variants = _path_spelling_variants((Path.home(), REPO_ROOT))
        user_name = getpass.getuser()
        hostname = os.environ.get("COMPUTERNAME", "")
        checked_count = 0
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            rel_name = path.relative_to(REPO_ROOT).as_posix()
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                self.fail(f"non-UTF8 file: {rel_name}")
            checked_count += 1
            findings = _sensitive_findings(content, home_variants, user_name, hostname)
            self.assertEqual(findings, [], f"{rel_name}: {', '.join(findings)}")
        self.assertGreaterEqual(checked_count, 16)

    def test_sanitization_helpers_positive_and_negative_examples(self):
        private_user = "private" + "person"
        homes = (
            "C:" + "\\Users\\" + private_user,
            "c:/" + "users/" + private_user,
            "/mnt/c/" + "Users/" + private_user,
            "\\\\wsl.localhost\\Ubuntu" + "\\home\\" + private_user,
            "/Users/" + private_user,
            "/home/" + private_user,
        )
        models = (
            "Core " + "i9-" + "14900K", "i5-" + "12400", "Core " + "Ultra " + "9 " + "285K",
            "Xeon " + "Gold " + "6430", "Xeon " + "E5-" + "2690",
            "AMD " + "Ryzen " + "9 " + "9950X", "Threadripper " + "PRO " + "7995WX", "EPYC " + "9654",
            "Apple " + "M4 " + "Max", "RTX " + "5090", "GTX " + "1080", "RTX " + "A6000",
            "A" + "100", "H" + "100", "H" + "200", "V" + "100", "L" + "40S",
            "Radeon RX " + "7900 " + "XTX", "OMEN " + "16L",
        )
        credentials = (
            "sk-" + "a" * 32, "AIza" + "a" * 34, "ghp_" + "a" * 32,
            "github_pat_" + "a" * 42, "AKIA" + "A" * 16,
            "hf_" + "a" * 30, "glpat-" + "a" * 30,
            "-----BEGIN " + "PRIVATE KEY-----",
        ) + tuple('"' + key + '": "' + "a" * 32 + '"' for key in (
            "api_key", "api-key", "access_token", "refresh_token", "id_token", "client_secret"))
        for text in homes + models + credentials:
            with self.subTest(text=text):
                self.assertTrue(_sensitive_findings(text))
        for text in (
            "/home/alice/work", "C:/Users/bob/work", "/home/charlie/work", "/Users/<user>/work",
            "CPU GPU Intel Core AMD Ryzen Threadripper Xeon EPYC NVIDIA RTX GTX",
            '"api_key": "<placeholder>"', "refresh_token = None", "user@example.com",
            "https://github.com/Xinyu-Li-1997/Antigravity_Codex_configuration", "Apache License 2.0",
            HARDCODED_HOME_PATTERNS[0].pattern, HARDCODED_HOME_PATTERNS[1].pattern,
        ):
            with self.subTest(text=text):
                self.assertEqual(_sensitive_findings(text), [])
        for path in _path_spelling_variants(("Q:/Private/Checkout",)):
            self.assertTrue(_sensitive_findings(path.upper(), home_variants=(path,)))
        self.assertTrue(_sensitive_findings(private_user, user_name=private_user.upper()))
        self.assertTrue(_sensitive_findings("private" + "host", hostname="PRIVATE" + "HOST"))
        uuid = "-".join(("a" * 8, "b" * 4, "4" + "c" * 3, "8" + "d" * 3, "e" * 12))
        self.assertTrue(_sensitive_findings(uuid))
        self.assertTrue(_sensitive_findings("private" + "@" + "private" + ".invalid"))
        self.assertTrue(_sensitive_findings("192." + "168." + "42." + "7"))

    def test_three_install_modes_ship_complete_artifacts_and_runnable_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for mode in ("codex", "antigravity", "both"):
                with self.subTest(mode=mode):
                    sandbox = root / mode
                    plugin, codex = sandbox / "plugins" / "user-guard", sandbox / "codex"
                    command = [sys.executable, "-B", "-X", "utf8", str(REPO_ROOT / "install.py"),
                               "--app", mode, "--skip-test", "--no-zotero", "--yes"]
                    if mode != "codex":
                        command += ["--target-dir", str(plugin)]
                    if mode != "antigravity":
                        command += ["--codex-home", str(codex)]
                    env = os.environ.copy()
                    for key in ("USER_GUARD_STATE_DIR", "USER_GUARD_LOGS_DIR", "USER_GUARD_INSTALLED_DIR", "USER_GUARD_INSTALLED_CODEX_DIR", "PLUGIN_DATA"):
                        env.pop(key, None)
                    env.update(PYTHONDONTWRITEBYTECODE="1", PLUGIN_DATA=str(sandbox / "data"), CODEX_HOME=str(codex))
                    proc = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    harnesses = []
                    if mode != "codex":
                        for relative in ("plugin.json", "assets/logo.svg", "hooks/status_guard.py", "hooks/handoff_gate.py"):
                            self.assertTrue((plugin / relative).is_file(), relative)
                        harnesses.append(plugin / "skills" / "cae-harness" / "scripts" / "cae_harness.py")
                        hooks = json.loads((plugin / "hooks.json").read_text(encoding="utf-8"))["user-guard-lifecycle"]
                        command = _build_subprocess_cmd(hooks["PreToolUse"][0]["hooks"][0]["command"])
                        proc = subprocess.run(command, cwd=plugin, env=env, input=json.dumps({"conversationId": "installed-hook", "toolCall": {"name": "read_file", "args": {}}}), capture_output=True, text=True, encoding="utf-8", timeout=15)
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertEqual(json.loads(proc.stdout)["decision"], "allow")
                    if mode != "antigravity":
                        for relative in ("hooks.json", "hooks/user_words.py", "hooks/handoff_gate.py", "hooks/session_context_loader.py"):
                            self.assertTrue((codex / relative).is_file(), relative)
                        harnesses.append(codex / "skills" / "cae-harness" / "scripts" / "cae_harness.py")
                        proc = subprocess.run([sys.executable, "-B", "-X", "utf8", str(codex / "hooks" / "handoff_gate.py"), "--client", "codex"], env=env, input=json.dumps({"hook_event_name": "PreToolUse"}), capture_output=True, text=True, encoding="utf-8", timeout=15)
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertIsInstance(json.loads(proc.stdout), dict)
                    for script in harnesses:
                        self.assertTrue(script.is_file())
                        proc = subprocess.run([sys.executable, "-B", "-X", "utf8", str(script), "--help"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertIn("assess", proc.stdout)

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
            first_snapshot = {file.relative_to(sandbox_plugin): (file.read_bytes(), file.stat().st_mtime_ns)
                              for file in sandbox_plugin.rglob("*") if file.is_file()}
            repeated = subprocess.run(proc.args, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual({file.relative_to(sandbox_plugin): (file.read_bytes(), file.stat().st_mtime_ns)
                              for file in sandbox_plugin.rglob("*") if file.is_file()}, first_snapshot)

    def test_05_dry_run_preserves_source_and_all_install_targets(self) -> None:
        """预演保留源树缓存、目标陈旧文件与其他技能，且不创建新目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            shutil.copy2(REPO_ROOT / "install.py", source / "install.py")
            shutil.copy2(REPO_ROOT / "CHANGELOG.md", source / "CHANGELOG.md")
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
                    with self.assertRaisesRegex(ValueError, "祖先链接"):
                        INSTALLER.validate_install_target(alias / "new-plugin", repo)
                    self.assertEqual(list(custom.iterdir()), [])

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
            cache = cli_dir / "cache" / "cae_models_cache.json"
            with mock.patch.object(INSTALLER, "_native_cli", return_value="native-agy") as discover, \
                    mock.patch.object(INSTALLER.subprocess, "run", return_value=subprocess.CompletedProcess(
                        ["native-agy", "models"], 0, stdout="".join(f'{item["slug"]}\t{item["display_name"]}\n' for item in models)
                    )) as query, \
                    contextlib.redirect_stdout(io.StringIO()):
                INSTALLER.configure_agy_frontier_max_default(gemini_root)
                self.assertEqual(settings.read_bytes(), source)
                discover.assert_not_called()
                query.assert_not_called()
                self.assertFalse(cache.exists())
                INSTALLER.configure_agy_frontier_max_default(gemini_root, dry_run=True, overwrite=True)
                discover.assert_not_called()
                query.assert_not_called()
                self.assertFalse(cache.exists())
                self.assertEqual(settings.read_bytes(), source)
                INSTALLER.configure_agy_frontier_max_default(gemini_root, overwrite=True)
                query.assert_called_once()
                self.assertEqual(json.loads(settings.read_bytes()), {"model": "Alpha Core (High)", "other": True})
                snapshot = json.loads(cache.read_bytes())
                self.assertEqual(snapshot["source"], "agy_models_cli")
                self.assertEqual(snapshot["models"], models)
                self.assertEqual(datetime.datetime.fromisoformat(snapshot["fetched_at"]).utcoffset(), datetime.timedelta(0))
                cache_before = (cache.read_bytes(), cache.stat().st_mtime_ns)
                settings.unlink()
                desktop_settings = gemini_root / "config" / "config.json"
                desktop_settings.parent.mkdir()
                desktop_settings.write_bytes(b'{"userSettings":{"model":"Orchestrator (Core)"}}\n')
                INSTALLER.configure_agy_frontier_max_default(gemini_root)
                self.assertFalse(settings.exists())
                self.assertEqual((cache.read_bytes(), cache.stat().st_mtime_ns), cache_before)
                query.assert_called_once()
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
        stop_cmd = hooks_cfg["Stop"][0]["command"]

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

            def dispatch_pre_tool(name: str, args: dict) -> dict:
                commands = [
                    handler["command"] for group in hooks_cfg["PreToolUse"]
                    if re.search(group["matcher"], name)
                    for handler in group["hooks"]
                ]
                self.assertEqual(len(commands), 1, f"{name} 应由已注册的 PreToolUse 处理")
                return run_sub(commands[0], {
                    "conversationId": "e2e-handoff-conv", "cwd": str(ws_dir),
                    "workspacePaths": [str(ws_dir)], "toolCall": {"name": name, "args": args},
                })

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
            out_subagent = dispatch_pre_tool("invoke_subagent", {})
            self.assertEqual(out_subagent["decision"], "deny")

            out_handoff = dispatch_pre_tool(
                "write_to_file", {"TargetFile": str(ws_dir / "HANDOFF_NEXT.md"), "CodeContent": "# handoff"},
            )
            self.assertEqual(out_handoff["decision"], "allow")
            gate_path = next(plugin_data.rglob(f"{conv_id}_handoff_gate.json"))
            gate_state = json.loads(gate_path.read_bytes())
            gate_state["phase"] = "freeze"
            gate_path.write_text(json.dumps(gate_state), encoding="utf-8")
            self.assertEqual(dispatch_pre_tool("new_tool_without_guard_alias", {})["decision"], "deny")
            self.assertEqual(dispatch_pre_tool("finish", {})["decision"], "allow")

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
                            "content": (
                                "已验证安装钩子可运行。Git 工作区已核对。未完成真实宿主验收。\n\n"
                                "### 新对话提示词\n```markdown\n请先读取用户原话，再核对当前工作区并完成宿主验收。\n"
                                "```\n<!-- GOAL_CANCELLED -->"
                            ),
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
            legacy = {"description": "用户原有说明\n第二行", "hooks": {"SessionStart": [{"matcher": "^(compact|resume)$", "hooks": [valid, sibling]}, other_group],
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
            enforce_bytes = hooks_path.read_bytes()
            self.assertIn(b"--mode enforce --compact-limit 2", enforce_bytes)
            result = install()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(hooks_path.read_bytes(), enforce_bytes)
            migrated = json.loads(enforce_bytes)
            migrated["description"] = legacy["description"]
            hooks_path.write_text(json.dumps(migrated), encoding="utf-8")
            result = install()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(hooks_path.read_bytes(), enforce_bytes)
            result = install("--codex-handoff-mode", "off")
            self.assertEqual(result.returncode, 0, result.stderr)
            disabled_bytes = hooks_path.read_bytes()
            self.assertEqual(json.loads(disabled_bytes), {
                **legacy, "description": legacy["description"] + "\n[user-guard codex-handoff v1 mode=off compact_limit=2]",
            })
            result = install()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(hooks_path.read_bytes(), disabled_bytes)
            result = install("--codex-handoff-mode", "advisory")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"--mode advisory --compact-limit 2", hooks_path.read_bytes())

            for malformed in (b"{bad json", b'{"hooks":{"SessionStart":{}}}', b'{"hooks":{},"hooks":{}}'):
                with self.subTest(malformed=malformed):
                    hooks_path.write_bytes(malformed)
                    result = install()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("无法安全合并", result.stderr)
                    self.assertEqual(hooks_path.read_bytes(), malformed)


    def test_20_codex_loader_upgrade_probes_only_trusted_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex"
            root.mkdir()
            loader = (root / "hooks" / "session_context_loader.py").as_posix()
            old = {"type": "command", "command": f'"{sys.executable}" -B "{loader}" --client codex',
                   "timeout": 33, "statusMessage": "custom status"}
            sibling = {"type": "command", "command": "custom-hook"}
            untrusted_dir = Path(tmp) / "untrusted"
            untrusted_dir.mkdir()
            untrusted = untrusted_dir / Path(sys.executable).name
            untrusted.write_bytes(b"not an interpreter")
            custom_program = {"type": "command", "command": f'"{untrusted}" -B "{loader}" --client codex'}
            custom_flags = {"type": "command", "command": f'"{sys.executable}" -c "raise SystemExit(0)" "{loader}" --client codex'}
            path = root / "hooks.json"
            path.write_text(json.dumps({"hooks": {"SessionStart": [
                {"matcher": "^(compact|resume)$", "hooks": [old, sibling, custom_program, custom_flags]},
            ]}}), encoding="utf-8")
            before = path.read_bytes()
            with mock.patch.object(INSTALLER.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as check, \
                    contextlib.redirect_stderr(io.StringIO()) as diagnostics:
                plan = INSTALLER._plan_codex_hooks(path, REPO_ROOT / "template/codex/hooks.json", "python -X utf8 -I -B", "advisory", 0)
            self.assertEqual(check.call_count, 1)
            invoked = check.call_args.args[0]
            self.assertEqual(Path(invoked[0]), Path(sys.executable))
            self.assertEqual(invoked[1:], ["-X", "utf8", "-I", "-B", "-c",
                                          "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"])
            self.assertIn("自定义脚本命令", diagnostics.getvalue())
            self.assertEqual(path.read_bytes(), before)
            handlers = json.loads(plan.change.content)["hooks"]["SessionStart"][0]["hooks"]
            self.assertNotEqual(handlers[0]["command"], old["command"])
            self.assertEqual(handlers[0]["timeout"], 33)
            self.assertEqual(handlers[0]["statusMessage"], "custom status")
            self.assertEqual(handlers[1:4], [sibling, custom_program, custom_flags])

    @unittest.skipUnless(os.name == "nt", "Windows PATH 的应用执行别名")
    def test_20_shadowed_windows_python_loader_uses_working_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alias = str(root / "WindowsApps" / "python.exe")
            python = str(root / "python" / "python.exe")
            launcher = str(root / "py.exe")
            candidates = [("python.exe", python, []), ("py.exe", launcher, ["-3"])]
            resolved = {"python.exe": alias, "py.exe": launcher}
            script = (root / "hooks" / "session_context_loader.py").as_posix()
            old = {"type": "command", "command": f'python.exe -I -B "{script}" --client codex',
                   "statusMessage": "已有状态提示"}
            hooks_path = root / "hooks.json"
            hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [
                {"matcher": "^(compact|resume)$", "hooks": [old]},
            ]}}), encoding="utf-8")
            original = hooks_path.read_bytes()
            template = REPO_ROOT / "template/codex/hooks.json"
            with mock.patch.object(INSTALLER, "_hook_python_candidates", return_value=candidates), \
                    mock.patch.object(INSTALLER.shutil, "which", side_effect=resolved.get), \
                    mock.patch.object(INSTALLER.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as probe:
                cmd = INSTALLER.detect_hook_python_command()
                plan = INSTALLER._plan_codex_hooks(hooks_path, template, cmd)
            self.assertEqual(cmd, "py.exe -3 -X utf8 -I -B")
            probe.assert_called_once()
            self.assertEqual(probe.call_args.args[0][:2], [launcher, "-3"])
            self.assertEqual(hooks_path.read_bytes(), original)
            loader = json.loads(plan.change.content)["hooks"]["SessionStart"][0]["hooks"][0]
            self.assertTrue(loader["command"].startswith(cmd + " "))
            self.assertEqual(loader["statusMessage"], old["statusMessage"])

    @unittest.skipIf(os.name == "nt", "POSIX Shell 路径转义")
    def test_21_posix_codex_hook_special_path_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex$cash`tick' space"
            root.mkdir()
            path = root / "hooks.json"
            template = REPO_ROOT / "template/codex/hooks.json"
            interpreter = root / 'python$cash`tick" space'
            interpreter.symlink_to(sys.executable)
            with mock.patch.object(INSTALLER.sys, "executable", str(interpreter)), \
                    mock.patch.object(INSTALLER.shutil, "which", return_value=None):
                cmd = INSTALLER.detect_hook_python_command()
                first = INSTALLER._plan_codex_hooks(path, template, cmd)
                path.write_bytes(first.change.content)
                self.assertIsNone(INSTALLER._plan_codex_hooks(path, template, cmd).change)
                scripts = root / "hooks"
                scripts.mkdir()
                for name in ("session_context_loader.py", "user_words.py"):
                    shutil.copy2(TEMPLATE_DIR / "hooks" / name, scripts / name)
                installed = json.loads(first.change.content)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
                payload = {"hook_event_name": "SessionStart", "session_id": "fallback-round-trip", "source": "startup"}
                result = subprocess.run(["/bin/sh", "-c", installed], input=json.dumps(payload), cwd=root,
                                        capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")
                self.assertTrue((root / "hooks" / "logs" / "session-context.jsonl").is_file())
                disabled = INSTALLER._plan_codex_hooks(path, template, cmd, "off", 0)
            hooks = json.loads(disabled.change.content)["hooks"]
            remaining = [handler for groups in hooks.values() for group in groups for handler in group["hooks"]]
            self.assertEqual(len(remaining), 1)
            self.assertIn("session_context_loader.py", remaining[0]["command"])

    def test_22_codex_toml_preflight_preserves_quoted_keys_comments_and_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = root / "codex"
            codex.mkdir()
            config = codex / "config.toml"
            config.write_bytes(
                b'"sandbox_mode" = "danger-full-access" # keep sandbox comment\r\n'
                b'"approval_policy" = "never" # keep approval\r\n'
                b'"model" = "account-choice" # keep model\r\n'
                b"'model_reasoning_effort' = 'high' # keep effort\r\n"
                b'"model_reasoning_summary" = "detailed"\r\n'
                b'["sandbox_workspace_write"] # sandbox header\r\n'
                b'"network_access" = false # keep network\r\n'
                b'["features"] # features header\r\n'
                b'"memories" = true # keep opt in\r\n'
                b'[memories] # memory header\r\n'
                b'"generate_memories" = true\r\n'
                b'["desktop"."external-agent-import-sync-item-types"] # imports\r\n'
                b"'HOOKS' = true # keep import\r\n"
                b'[hooks.state."handler#1"] # preserve trust\r\n'
                b'"trusted_hash" = "sha256:example"\r\n'
                b'["mcp_servers"."zotero"] # remote transport\r\n'
                b'"url" = "https://example.org/mcp" # keep remote URL\r\n'
                b'"bearer_token_env_var" = "ZOTERO_TOKEN"\r\n'
            )
            before = config.read_bytes()
            zotero = root / "zotero-mcp-stub"
            zotero.write_bytes(b"stub")
            cmd = [sys.executable, "-B", str(REPO_ROOT / "install.py"), "--app", "codex", "--codex-home", str(codex),
                   "--skip-test", "--zotero-cmd", str(zotero)]
            dry = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((codex / "hooks.json").exists())
            run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            updated = config.read_bytes()
            self.assertIn(b'"sandbox_mode" = "workspace-write" # keep sandbox comment\r\n', updated)
            for line in before.splitlines(keepends=True)[1:]:
                self.assertIn(line, updated)
            parsed = INSTALLER._parse_toml(updated.decode("utf-8"), config)
            self.assertEqual(parsed["features"], {"memories": True, "chronicle": False})
            self.assertEqual(parsed["approval_policy"], "never")
            self.assertEqual(parsed["sandbox_workspace_write"], {"network_access": False})
            self.assertEqual(parsed["hooks"]["state"]["handler#1"]["trusted_hash"], "sha256:example")
            self.assertEqual(parsed["mcp_servers"]["zotero"], {
                "url": "https://example.org/mcp", "bearer_token_env_var": "ZOTERO_TOKEN",
            })
            mtime = config.stat().st_mtime_ns
            run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((config.read_bytes(), config.stat().st_mtime_ns), (updated, mtime))

            # 在没有 tomllib 的解释器分支重做同一编辑，保守拒绝不支持的复杂值。
            config.write_bytes(before)
            with mock.patch.object(INSTALLER, "tomllib", None), contextlib.redirect_stdout(io.StringIO()):
                planned = INSTALLER._plan_codex_config(codex, include_sandbox=True, include_model=True,
                                                       overwrite_model=False, zotero_exe=str(zotero))
                self.assertEqual(planned.content, updated)
                complex_config = b'description = """\n[features]\nmultiline\n"""\n'
                config.write_bytes(complex_config)
                with self.assertRaisesRegex(ValueError, r"Python 3\.11\+"):
                    INSTALLER._plan_codex_config(codex, include_sandbox=True, include_model=False,
                                                overwrite_model=False, zotero_exe=None)
                self.assertEqual(config.read_bytes(), complex_config)

    def test_23_config_errors_abort_both_targets_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / "agy-config" / "plugins" / "user-guard"
            mcp = plugin.parent.parent / "mcp_config.json"
            codex = root / "codex"
            plugin.mkdir(parents=True)
            codex.mkdir()
            config = codex / "config.toml"
            hooks_path = codex / "hooks.json"
            (plugin / "old.txt").write_bytes(b"must not be cleaned")
            zotero = root / "zotero-mcp-stub"
            zotero.write_bytes(b"stub")
            cmd = [sys.executable, "-B", str(REPO_ROOT / "install.py"), "--app", "both", "--target-dir", str(plugin),
                   "--codex-home", str(codex), "--skip-test", "--zotero-cmd", str(zotero)]

            def snapshot() -> dict:
                return {entry.relative_to(root): (entry.read_bytes(), entry.stat().st_mtime_ns) if entry.is_file() else None
                        for entry in root.rglob("*")}

            cases = [(config, data) for data in (b"invalid TOML", b'a = 1\na = 2\n', b"[features\n")]
            cases += [(mcp, data) for data in (
                b"", b"{bad json", b"[]", b'{"mcpServers": []}', b'{"mcpServers": {"zotero": false}}',
                b'{"mcpServers": {"zotero": {}, "zotero": {}}}', b'{"mcpServers": {"other": NaN}}',
            )]
            cases += [(hooks_path, data) for data in (
                br'{"description":"\ud800","hooks":{}}',
                b'{"hooks":{"Notification":[{"hooks":[{"type":"command","command":"custom","custom":1e999}]}]}}',
            )]
            for path, content in cases:
                with self.subTest(path=path.name, content=content):
                    mcp.write_bytes(b'{"mcpServers": {}}\n')
                    config.write_bytes(b'model = "example"\n')
                    hooks_path.write_bytes(b'{"hooks":{}}\n')
                    path.write_bytes(content)
                    before = snapshot()
                    for options in (["--dry-run"], []):
                        result = subprocess.run(cmd + options, capture_output=True, text=True, encoding="utf-8", timeout=20)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn(path.name, result.stderr)
                        self.assertEqual(snapshot(), before)

    def test_24_early_link_rejection_leaves_all_install_targets_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / "agy-config" / "plugins" / "user-guard"
            plugin.mkdir(parents=True)
            (plugin / "old.txt").write_bytes(b"retain")
            external = root / "external"
            external.mkdir()
            marker = external / "keep.txt"
            marker.write_bytes(b"external sentinel")
            zotero = root / "zotero-mcp-stub"
            zotero.write_bytes(b"stub")
            probe = root / "link-probe"
            try:
                _create_directory_link(probe, external)
            except OSError:
                self.skipTest("当前系统没有创建目录链接或联接的权限")
            cmd = [sys.executable, "-B", str(REPO_ROOT / "install.py"), "--app", "both", "--target-dir", str(plugin),
                   "--skip-test", "--zotero-cmd", str(zotero)]

            def reject(codex: Path, link: Path) -> None:
                result = subprocess.run(cmd + ["--codex-home", str(codex)], capture_output=True, text=True,
                                        encoding="utf-8", timeout=20)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(link.name, result.stderr)
                self.assertEqual(list(external.iterdir()), [marker])
                self.assertEqual(marker.read_bytes(), b"external sentinel")
                self.assertEqual(list(plugin.iterdir()), [plugin / "old.txt"])
                self.assertEqual((plugin / "old.txt").read_bytes(), b"retain")
                self.assertFalse((codex / "hooks.json").exists())
                self.assertFalse((codex / "config.toml").exists())
                self.assertFalse((codex / "AGENTS.md").exists())

            for name in ("agents", "hooks", "skills"):
                with self.subTest(name=name):
                    codex = root / ("codex-" + name)
                    codex.mkdir()
                    link = codex / name
                    _create_directory_link(link, external)
                    reject(codex, link)
                    self.assertEqual(list(codex.iterdir()), [link])
            reject(probe / "codex", probe)
            mcp = plugin.parent.parent / "mcp_config.json"
            _create_directory_link(mcp, external)
            unused_codex = root / "unused-codex"
            reject(unused_codex, mcp)
            self.assertFalse(unused_codex.exists())

            for name in ("AGENTS.md", "config.toml", "hooks.json", "agents/readonly-reader.toml", "hooks/user_words.py"):
                with self.subTest(file=name):
                    codex = root / ("files-" + name.replace("/", "-"))
                    target = codex / name
                    target.parent.mkdir(parents=True)
                    # 硬链接在 Windows 无需符号链接特权，验证直接文件别名同样拒绝。
                    try:
                        os.link(marker, target)
                    except OSError:
                        self.skipTest("当前文件系统不支持硬链接")
                    result = subprocess.run(cmd + ["--codex-home", str(codex), "--no-zotero"], capture_output=True,
                                            text=True, encoding="utf-8", timeout=20)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(target.name, result.stderr)
                    self.assertEqual(target.read_bytes(), b"external sentinel")
                    self.assertEqual(marker.read_bytes(), b"external sentinel")
                    self.assertEqual(list(plugin.iterdir()), [plugin / "old.txt"])

    def test_25_agy_zotero_preserves_existing_remote_and_stdio_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / "agy-config" / "plugins" / "user-guard"
            config = plugin.parent.parent / "mcp_config.json"
            config.parent.mkdir()
            zotero = root / "zotero-mcp-stub"
            zotero.write_bytes(b"stub")
            cmd = [sys.executable, "-B", str(REPO_ROOT / "install.py"), "--app", "antigravity",
                   "--target-dir", str(plugin), "--skip-test", "--zotero-cmd", str(zotero)]
            for data in (
                b'{"mcpServers":{"zotero":{"serverUrl":"https://example.org/mcp","headers":{"X-Example":"yes"}}}}\n',
                b'{"mcpServers":{"zotero":{"command":"host-managed-zotero","args":["serve"],"env":{"CUSTOM":"true"}}}}\n',
            ):
                with self.subTest(data=data):
                    config.write_bytes(data)
                    mtime = config.stat().st_mtime_ns
                    run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=20)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual((config.read_bytes(), config.stat().st_mtime_ns), (data, mtime))
            config.write_bytes(b'{"mcpServers":{"other":{"serverUrl":"https://example.org/mcp"}},"custom":true}\n')
            run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            merged = json.loads(config.read_bytes())
            self.assertTrue(merged["custom"])
            self.assertEqual(merged["mcpServers"]["other"], {"serverUrl": "https://example.org/mcp"})
            self.assertEqual(merged["mcpServers"]["zotero"]["command"], str(zotero))

    def test_26_executable_evidence_does_not_treat_config_as_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            (home / ".gemini" / "config").mkdir(parents=True)
            wsl = {"status": "unknown", "installed_distros": ["stopped"], "running_distros": [], "apps": {}}
            with mock.patch.object(INSTALLER.Path, "home", return_value=home), \
                    mock.patch.object(INSTALLER, "_native_cli", return_value=None), \
                    mock.patch.object(INSTALLER, "_first_executable", return_value=None), \
                    mock.patch.object(INSTALLER, "_find_windows_codex_package", return_value=(None, None)), \
                    mock.patch.object(INSTALLER, "_is_wsl", return_value=False), \
                    mock.patch.object(INSTALLER, "_probe_windows_wsl", return_value=wsl), \
                    mock.patch.object(INSTALLER, "detect_zotero_mcp_executable", return_value=None), \
                    mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
                result = INSTALLER.detect_installed_apps()
                self.assertEqual(result["recommended_app_mode"], "none")
                for name in ("antigravity", "codex"):
                    self.assertFalse(result[name]["native_installed"])
                    self.assertEqual(result[name]["native_status"], "config_only")
                    self.assertEqual(result[name]["authentication_status"], "not_checked")
                    self.assertFalse(result[name]["interop_available"])

            with mock.patch.object(INSTALLER.shutil, "which", return_value="wsl.exe"), \
                    mock.patch.object(INSTALLER, "_wsl_distro_list", side_effect=[["running", "stopped"], ["running"]]), \
                    mock.patch.object(INSTALLER.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout="agy\t/opt/bin/agy\ttrue\ncodex\t\ttrue\n",
                    )) as probe:
                result = INSTALLER._probe_windows_wsl()
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["installed_distros"], ["running", "stopped"])
                self.assertEqual(result["apps"]["agy"][0]["binary"], "/opt/bin/agy")
                self.assertIsNone(result["apps"]["codex"][0]["binary"])
                probe.assert_called_once()
                self.assertEqual(probe.call_args.args[0][:4], ["wsl.exe", "--distribution", "running", "--exec"])

    def test_26_wsl_windows_profile_requires_successful_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = r"C:\Users\example"
            result = subprocess.CompletedProcess
            cases = (
                ([result([], 1, stdout=profile)], None),
                ([result([], 0, stdout="")], None),
                ([result([], 0, stdout=r"Users\example")], None),
                ([result([], 0, stdout=profile), result([], 1, stdout=str(root))], None),
                ([result([], 0, stdout=profile), result([], 0, stdout="\n")], None),
                ([result([], 0, stdout=profile), result([], 0, stdout=".")], None),
                ([result([], 0, stdout=profile), result([], 0, stdout=str(root / "missing"))], None),
                ([result([], 0, stdout=profile), result([], 0, stdout=str(root))], root),
            )
            executables = {"cmd.exe": str(root / "cmd.exe"), "wslpath": str(root / "wslpath")}
            for commands, expected in cases:
                with self.subTest(commands=commands), \
                        mock.patch.object(INSTALLER, "_is_wsl", return_value=True), \
                        mock.patch.object(INSTALLER.shutil, "which", side_effect=executables.get), \
                        mock.patch.object(INSTALLER.subprocess, "run", side_effect=commands) as run:
                    self.assertEqual(INSTALLER._probe_wsl_windows_profile_dir(), expected)
                    self.assertEqual(run.call_count, len(commands))

    def test_28_installer_regression_does_not_inherit_runtime_state_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / "agy" / "plugins" / "user-guard"
            codex = root / "codex"
            state, logs = root / "external-state", root / "external-logs"
            state.mkdir()
            logs.mkdir()
            for path in (state, logs):
                (path / "keep").write_bytes(b"runtime sentinel")
            argv = ["install.py", "--app", "both", "--target-dir", str(plugin), "--codex-home", str(codex), "--no-zotero"]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {"USER_GUARD_STATE_DIR": str(state), "USER_GUARD_LOGS_DIR": str(logs)}), \
                    mock.patch.object(INSTALLER, "detect_hook_python_command", return_value="python -X utf8 -I -B"), \
                    mock.patch.object(INSTALLER.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                INSTALLER.main()
                self.assertEqual(os.environ["USER_GUARD_STATE_DIR"], str(state))
                self.assertEqual(os.environ["USER_GUARD_LOGS_DIR"], str(logs))
            run.assert_called_once()
            self.assertIn("unittest", run.call_args.args[0])
            env = run.call_args.kwargs["env"]
            self.assertNotIn("USER_GUARD_STATE_DIR", env)
            self.assertNotIn("USER_GUARD_LOGS_DIR", env)
            self.assertEqual(Path(env["USER_GUARD_INSTALLED_DIR"]), plugin.resolve())
            self.assertEqual(Path(env["USER_GUARD_INSTALLED_CODEX_DIR"]), codex.resolve())
            for path in (state, logs):
                self.assertEqual(list(path.iterdir()), [path / "keep"])
                self.assertEqual((path / "keep").read_bytes(), b"runtime sentinel")

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "需要 POSIX Bash")
    def test_27_shell_probe_stdout_is_json_and_old_python_does_not_block_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "python3"
            old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            old.chmod(0o755)
            (root / "python").symlink_to(sys.executable)
            env = os.environ.copy()
            env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                ["bash", str(REPO_ROOT / "install.sh"), "--probe-apps"], env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recommended_app_mode", json.loads(result.stdout))
            self.assertIn("启动", result.stderr)

    def test_29_empty_mcp_no_zotero_rules_upgrade_dryrun_sync_and_both_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_root = root / ".gemini" / "config"
            cfg_root.mkdir(parents=True)
            (cfg_root / "mcp_config.json").write_bytes(b"")
            self.assertIsNone(INSTALLER._plan_user_zotero(cfg_root, None))
            with self.assertRaises(ValueError):
                INSTALLER._plan_user_zotero(cfg_root, str(root / "zotero-mcp"))

            codex_dir = root / "codex_rules"
            codex_dir.mkdir()
            codex_agents = codex_dir / "AGENTS.md"
            codex_agents.write_text(
                "# 用户规则（全局生效）\n- 旧规则\n\n## 工具与运行环境\n- **R10. 旧版环境说明**：旧内容\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER._sync_codex_global_rules(TEMPLATE_DIR / "rules" / "AGENTS.md", codex_dir, dry_run=False)
            upgraded = codex_agents.read_text(encoding="utf-8")
            self.assertIn("## 工具与运行环境", upgraded)
            self.assertNotIn("旧版环境说明", upgraded)
            self.assertIn("R11.", upgraded)

            plugin_dir = root / "plugins" / "user-guard"
            with contextlib.redirect_stdout(io.StringIO()):
                INSTALLER.mirror_sync_plugin(TEMPLATE_DIR, plugin_dir, dry_run=False)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                INSTALLER.mirror_sync_plugin(TEMPLATE_DIR, plugin_dir, dry_run=True)
            self.assertNotIn("[SYNC]", buf.getvalue())

            bad_argv = ["install.py", "--app", "both", "--target-dir", str(plugin_dir), "--no-zotero", "--dry-run"]
            with mock.patch.object(sys, "argv", bad_argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                INSTALLER.main()
            self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

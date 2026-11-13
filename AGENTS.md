# 仓库维护与双端自举配置指引（Codex ＆ Antigravity 共读）

本仓库（`Antigravity_Codex_configuration`）是 OpenAI Codex 与 Google Antigravity（反重力）统一的工程／科研工作流与双向协作配置源。无论用户使用 **Codex** 还是 **反重力** 打开本仓库，当前客户端均可读取本文件为自身及对侧完成探测、配置、升级与交叉验收。

## 一、 核心原则：独立工作流与跨端协作解耦

1. **单 Codex 独立生效（`--app codex`）**：部署 `~/.codex/AGENTS.md`（保留末尾自定义 `## 工具与本机`）、只读子代理（`agents/readonly-*.toml`）、压缩／恢复原话回溯钩子（`hooks.json` + `hooks/`）、项目边界沙箱与单模型最高挡基线（`config.toml`，默认跳过套娃委派 `ultra`）及可选 `zotero-mcp`。
2. **单反重力独立生效（`--app antigravity`）**：部署 `~/.gemini/config/plugins/user-guard` 全套原生插件（全局规则、四类生命周期事件守护钩子——含压缩原话恢复 `session_context_loader.py`、项目目录硬边界与防空转 `status_guard.py`、防刷屏与终止检验 `reply_repeat_guard.py` 及反重力专有两阶段长会话交接停机门禁 `handoff_gate.py`、以及三个只读子代理）、CLI 单模型最高挡基线及可选 `zotero-mcp`。
3. **双端完整生效与双向协作（`--app both` 或自动检测双端）**：同时部署上述两套独立工作流，并启用双端共享的 `cae-harness` 跨平台派工与验收技能（支持 Windows 与 WSL 原生及跨子系统互操作回退；前端一级交互总控优先尊重用户所选纯旗舰或 `Orchestrator` 混动模型，后台二级并发流独立执行专长自适应与显式／模糊选路）。

## 二、 AI 引导本机配置标准流程（双向自举）

当用户要求“帮我配置本机”“更新双端配置”或“检查安装状态”时，请严格按以下顺序执行：

1. **只读探测本机客户端状态（零写入）**：
   运行 `python -B install.py --probe-apps`（Linux／WSL／macOS 用 `python3`），读取返回的 JSON，核实本侧原生及跨子系统（WSL <-> Windows）是否已安装反重力与 Codex，以及是否检测到 `zotero-mcp`。
2. **单客户端缺失时的必做询问**：
   - 若探测显示本机**仅安装了其中一个 APP**（例如仅有 Codex 未装反重力，或仅有反重力未装 Codex，且跨子系统一侧也未安装另一方）：
     先向用户如实说明检测结果，并询问用户：**① 是否需要先去安装另一个客户端以启用双端协作**，还是 **② 不安装另一个，直接单独配置当前已安装客户端的完整独立工作流**。
   - 若本侧仅装其一但跨子系统对侧已装另一方（如 WSL 装了反重力、Windows 装了 Codex）：直接配置本侧已装客户端，并提示对侧配置可在对侧终端同步运行一次安装脚本。
3. **预演并按 R4 明确征求用户批准**：
   先运行 `python -B install.py --dry-run --yes`（或附带用户选定的 `--app codex|antigravity|both`）核对变更计划；向用户清晰列出即将写入的宿主机全局路径（`~/.codex` 与／或 `~/.gemini/config`），确认保留已有的 `## 工具与本机` 硬件说明与运行态配置，待用户明确同意后再执行正式写入（`python -B install.py --yes ...`）。
4. **双端交叉验证与发布边界**：
   修改本仓库代码后，必须先在临时沙箱目录完成单 Codex、单反重力与双端三套隔离安装及全量离线回归测试（`python -B -X utf8 -m unittest discover -s tests -p "test_*.py" -v`），且确保仓库内零个人路径、零硬件隐私与零非官方接口引用，经用户确认后再提交或推送远端。

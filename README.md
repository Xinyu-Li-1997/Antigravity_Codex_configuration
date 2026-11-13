# 反重力（Antigravity）与 Codex 统一工作流及双向协作配置

本仓库（`Antigravity_Codex_configuration`）将 **Google Antigravity（反重力）独立工作流**、**OpenAI Codex 独立工作流** 与 **双端跨平台协作（`cae-harness`）** 统一收纳在同一个仓库中维护。全部脚本仅依赖 Python `3.9+` 标准库，零第三方依赖。

### 核心设计：独立工作流与跨端协作解耦

协作是一回事，单客户端自身的工作流通用合理是另一回事。本仓库同时支持三种部署形态，且**支持两边自己给自己配、互相给对方配**：

1. **单 Codex 独立模式（`--app codex`）**：本机只装了 Codex 时独立生效——完整配置 `~/.codex/AGENTS.md` 全局规则（无损保留本机硬件段）、3 个只读子代理（`agents/readonly-*.toml`）、压缩／恢复用户原话回溯钩子（`hooks.json` + `hooks/`）、项目目录边界沙箱与防污染基线（`config.toml`）、单模型最高挡基线（默认跳过套娃委派 `ultra`）及可选本地 `zotero-mcp`。
2. **单反重力独立模式（`--app antigravity`）**：本机只装了反重力时独立生效——完整部署 `~/.gemini/config/plugins/user-guard` 原生插件（全局规则、四类生命周期守护钩子：原话压缩恢复、项目硬边界、防空转刷新及反重力专有两阶段长会话交接停机门禁 `handoff_gate.py`，以及 3 个严格只读子代理）、CLI 单模型最高挡及可选本地 `zotero-mcp`。
3. **双端完整协作模式（默认自动识别双端）**：同时配齐上述两套独立工作流，并启用双端共享的 `cae-harness` 双向派工与验收技能（支持 Windows 与 WSL 原生及跨子系统自动桥接）。

> **单客户端智能提示**：如果本机只安装了其中一个 APP（例如只有 Codex 没装反重力，或只有反重力没装 Codex），无论是直接运行安装脚本还是让 AI 读仓库自举配置，都会先明确提示你**“要不要先去安装另一个客户端以启用双端协作”**；选择不安装，即自动单独配好当前已安装的客户端，零报错、不向未安装一侧生成多余目录。

---

## 一、 快速配置（支持 AI 自举互配与命令行一键安装）

```sh
git clone https://github.com/Xinyu-Li-1997/Antigravity_Codex_configuration.git
cd Antigravity_Codex_configuration
```

### 方式 A：直接让 Codex 或反重力自己读仓库配本机（推荐）

用 **Codex** 或 **反重力** 任意一方打开克隆下来的仓库目录（两者都会自动读取仓库根目录的 [AGENTS.md](AGENTS.md)），直接发送一句话：

> *“帮我检查并配置好本机工作流。”*

当前打开的客户端会自动执行只读探测（`python -B install.py --probe-apps`）：
- 若双端均已安装：列出即将更新的 `~/.codex` 与 `~/.gemini/config` 路径，经你确认后**一次性把自身和另一方全部配好**（即 Codex 能配自己和反重力，反重力也能配自己和 Codex）；
- 若仅安装了当前这一个客户端：主动询问你是否要先安装另一个客户端，不安装就单独把当前客户端配齐。

### 方式 B：终端命令行一键安装

- **Windows（PowerShell）**：
  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
  ```
- **Linux / macOS / WSL**：
  ```sh
  bash install.sh
  ```

常用可选参数（可直接跟在 `install.ps1`、`install.sh` 或 `python -B install.py` 后）：
- `--app auto|both|codex|antigravity`：默认 `auto` 自动识别；也可显式指定只配单 Codex（`--app codex`）或只配单反重力（`--app antigravity`）；
- `-y` / `--yes`：非交互自动确认（仅检测到单端时直接单独配置已安装的一端）；
- `--probe-apps`：纯只读输出本机双端与 `zotero-mcp` 检测状态 JSON；
- `--dry-run`：只读预演变更计划，不修改任何磁盘文件。

若同时使用 **Windows + WSL**，在两侧终端进入同一个仓库目录各运行一次对应脚本即可（例如 Windows 原生装了双端、WSL 仅装了反重力时，WSL 侧会自动同步反重力并通过跨子系统互操作桥接 Windows 侧 Codex，不会在 WSL 乱建多余的 `.codex` 目录）。

---

## 二、 单端工作流与双端协作机制一览

### 1. 两边各自完备的独立工作流

| 能力维度 | 反重力侧（`~/.gemini/config/plugins/user-guard`） | Codex 侧（`~/.codex`） |
| --- | --- | --- |
| 全局工程与科研规则 | `rules/AGENTS.md`（`R0～R11`） | `AGENTS.md`（自动同步 `R0～R9` 核心总纲，完整保留末尾自定义 `## 工具与本机` 硬件与内存说明） |
| 防上下文压缩失忆 | `SessionStart` + `PreInvocation` 增量检查点游标，压缩后首轮自动落盘 `_user_words.md` 并注入 | `SessionStart`（匹配 `compact\|resume`）合并同会话全部 `rollout-*.jsonl` 分段，落盘 `hooks/state/user-words/<id>.md` 并注入（已有有效 `hooks.json` 保持原信任哈希不变） |
| 三个严格只读子代理 | `agents/readonly-{reader,auditor,collaborator}.md`（仅开放 `read_file`、`search_web`） | `agents/readonly-{reader,auditor,collaborator}.toml`（仅限只读读取、检索与列目录，禁写禁构建） |
| 项目目录硬边界约束 | `PreToolUse`（`status_guard.py`）AST + Shell 命令头配对拦截越界写删，交互弹窗四要素请示、后台硬阻断；兼防日志原地空转轮询 | `config.toml` 收紧 `sandbox_mode = "workspace-write"`、`approval_policy = "on-request"`、`network_access = true`，并默认关闭外部钩子自动导入（`HOOKS = false`） |
| 防同质废话、长会话交接停机与默认挡位 | `Stop`（`reply_repeat_guard.py`）拦截无进展重复等待刷屏；`handoff_gate.py` 提供反重力专有两阶段“原子收口＋交接停机”硬门禁（防长会话历史截断与多模态读图坍塌，禁新开派工并验收 `/goal` 停止标记）；CLI 默认对齐单模型最高挡 | 默认对齐旗舰单模型纯思考最高挡（跳过 `ultra` 套娃自动委派）与 `model_reasoning_summary = "auto"`，关闭未核实跨会话自动记忆污染 |
| 本地文献库接入（可选） | 自动探测本机 `zotero-mcp` 并无损写入 `mcp_config.json`（未安装则静默跳过） | 自动探测本机 `zotero-mcp` 并无损写入 `config.toml` 的 `[mcp_servers.zotero]`（未安装则静默跳过） |

### 2. 双端安装时的跨平台自动协作（`cae-harness`）

当反重力与 Codex 双端均可用时，日常直接在主控对话框正常聊天即可，无需手敲脚本命令：

| 项目侧重 | 主控端 | 另一方自动承担的协作职责 |
| --- | --- | --- |
| 代码工程 | Codex | 反重力参与方向校正、技术表述润色和独立验收 |
| 科研主题 | 反重力 | Codex 承担其中部分高质量编码和数学推导工作 |

1. **两级模型架构与专长分级选路（一级总控不冲突 + 二级自适应／显式／模糊选路）**：前端交互式主会话（一级总控）完全支持用户直接选用纯旗舰深思模型或 `Orchestrator` 混动自动路由器作为优先总控；后台并发流与跨端任务（二级执行层，`cae-harness`）在不冲突的前提下按专长动态自适应选路（自动区分深度推理旗舰、大上下文主力与含 `flash`/`hydrogen`/`lite`/`mini` 标签的同代轻量高速家族，排除混动路由器与长周期稀缺配额池误抢占），同时支持 `--engine` 与 `--tier` 的显式精确指定、模糊别名直达及任务描述显式点名模型（内置 CFD 物理名词防误触保护）：数理推导（`math`）与路线攻坚（`direction`）拉满**单模型纯思考最高挡**（`max`／`xhigh`，后台默认禁开套娃派生 `ultra`）；常规编码／审计／`gate` 首轮用主力 `high` 挡（复杂格式／根因或重试轮升至 `max`）；短润色用 `medium` 挡；表面核查用轻量高速 `low` 挡。
2. **额度不足自动熔断，交由你二选一**：任一端触发额度上限时立即停止重试（返回 `quota_exhausted`，退出码 `5`）并记入本侧冷却缓存；冷却期内再次触发跨端调用前自动拦截，提示你选择 **① 先不发起（由当前主控在本侧独立完成）** 或 **② 充值／恢复后加 `--retry-quota` 重试**。
3. **无痕会话隔离**：调用 Codex 固定启用 `codex exec --ephemeral`，调用反重力走独立无头存储 `~/.gemini/antigravity-cli`，绝不污染两侧桌面聊天侧边栏与项目树。

---

## 三、 仓库目录结构

| 目录／文件 | 作用（白话说明） |
| --- | --- |
| `AGENTS.md` | **双端自举引导文件**：Codex 与反重力打开本仓库时自动共读，指导客户端探测环境、提示单端用户并配置自身与对侧 |
| `install.py`、`install.ps1`、`install.sh` | 跨平台统一安装器，支持单 Codex、单反重力、双端协作安装及沙箱隔离验证 |
| `template/user-guard/` | 反重力原生插件模板，同时存放双端共用的 `rules/AGENTS.md`、双模压缩恢复钩子（`hooks/session_context_loader.py`、`user_words.py`）与共享技能 `skills/cae-harness/` |
| `template/codex/` | Codex 独立工作流声明式模板（`hooks.json` 与 `agents/readonly-{reader,auditor,collaborator}.toml`） |
| `docs/` | [协作实践指南](docs/反重力全域工程化最佳实践指南.md)与[生命周期钩子底层规范](docs/架构设计与生命周期钩子规范.md) |
| `tests/` | 覆盖单 Codex、单反重力、双端安装、双模钩子、跨端编排与全库脱敏合规的离线回归测试 |

---

## 四、 后续软件与模型升级维护（任意一端引导更新）

仓库源码零硬编码模型名与个人路径。后续软件或模型升级时，用 **反重力或 Codex 任意一方**打开本仓库目录直接对话即可：

1. **新模型或新思考挡位上线（无需改代码）**：发送 *“帮我刷新探测最新模型列表，并把本机默认最高挡同步好”*，AI 会执行 `probe --discover-models` 并运行安装器完成更新。
2. **客户端底层协议大改版**：发送 *“客户端升级了，帮我对照最新 CLI 与钩子规范做兼容性巡检，跑通沙箱测试后同步本机”*。建议重大改动在双端各自执行一次交叉验证后再推送发布新版本。

---

## 五、 沙箱验证与调试命令参考

修改仓库源码后，推荐先在临时目录验证单端与双端沙箱安装及全量测试，通过后再更新本机生产目录（完整协作参数见[共享技能说明](template/user-guard/skills/cae-harness/SKILL.md)）：

```sh
# 1. 只读探测本机客户端状态与安装预演
python -B install.py --probe-apps
python -B install.py --dry-run --yes

# 2. 临时沙箱隔离验证（单 Codex / 单反重力 / 双端）
python -B install.py --app codex --codex-home "<临时目录>/codex_home" --no-zotero
python -B install.py --app antigravity --target-dir "<临时目录>/plugins/user-guard" --no-zotero
python -B install.py --app both --target-dir "<临时目录>/plugins/user-guard" --codex-home "<临时目录>/codex_home" --no-zotero

# 3. 探测跨端 CLI 与模型挡位（加 --discover-models 主动刷新模型目录）
python -B template/user-guard/skills/cae-harness/scripts/cae_harness.py probe

# 4. 运行全量无字节码（-B）离线回归测试
python -B -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

---

## 六、 安全边界与开源许可

- **安全使用边界**：授权代码写入子任务（`task --kind code` 与 `gate`）由宿主工作区沙箱（`codex -s workspace-write`／`agy --sandbox`）与 `PreToolUse` 硬边界钩子双重防护（越界操作硬阻断），并在执行前后核对工作区文件快照；建议始终在受 Git 管理的项目工作区中使用。
- **外部工具与合规声明**：本仓库仅包含原创 Python 标准库脚本与声明式配置文档，仅通过操作系统标准命令行子进程调用用户自行从官方渠道安装并完成官方账号登录的本地客户端（`agy`、`codex` 及可选本地文献工具 `zotero-mcp`）；不包含、不链接亦不分发任何第三方专有程序或认证凭据，不接入任何非官方第三方接口，亦不包含任何绕过官方认证、权限或计费限制的机制。
- **开源许可证**：本仓库采用 [Apache License 2.0](LICENSE) 开源许可（`Copyright 2026 Xinyu-Li-1997`）。软件按“原样（AS IS）”提供，不附带任何明示或暗示担保；任何修改版分发须按许可证第 4(b) 条在变更文件中保留显著修改声明。

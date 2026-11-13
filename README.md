# 反重力（Antigravity）与 Codex 统一工作流及双向协作配置

本仓库（`Antigravity_Codex_configuration`）将 **Google Antigravity（反重力）独立工作流**、**OpenAI Codex 独立工作流** 与 **双端跨平台协作（`cae-harness`）** 统一收纳在同一个仓库中维护。全部脚本仅依赖 Python `3.9+` 标准库，零第三方依赖。

### 部署方式

1. **单 Codex（`--app codex`）**部署全局规则、三个只读子代理、原话恢复与长会话交接钩子、项目沙箱配置和可选的本地 `zotero-mcp`。
2. **单反重力（`--app antigravity`）**部署 `user-guard` 原生插件，包含全局规则、生命周期钩子、三个只读子代理和可选的本地 `zotero-mcp`。
3. **双端协作（`--app both` 或自动检测双端）**部署两套独立工作流，并通过共享技能 `cae-harness` 运行跨端 CLI 任务与验收。

自动探测只找到一个客户端时，交互安装会询问是否先安装另一端。可以直接配置已安装的一端。

---

## 一、 快速配置（支持 AI 自举互配与命令行一键安装）

```sh
git clone https://github.com/Xinyu-Li-1997/Antigravity_Codex_configuration.git
cd Antigravity_Codex_configuration
```

### 方式 A：直接让 Codex 或反重力自己读仓库配本机（推荐）

用 **Codex** 或 **反重力** 任意一方打开克隆下来的仓库目录（两者都会自动读取仓库根目录的 [AGENTS.md](AGENTS.md)），直接发送一句话：

> *“帮我检查并配置好本机工作流。”*

当前客户端先运行只读探测 `python -B install.py --probe-apps`，再按部署范围预演并列出将写入的全局配置路径。确认后执行安装。若只探测到一端，按上面的单端流程处理。

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
- `--sync-model-defaults`：显式同步模型和推理挡位的默认值。默认安装只补缺失项，保留已有模型和挡位。
- `--no-model-config`：跳过模型配置。与 `--sync-model-defaults` 互斥。
- `--codex-handoff-mode advisory|enforce|off`：默认 `advisory`，记录观测并按条件提醒。启用 `enforce` 前，先明确触发策略并在目标宿主上受控验证。`off` 移除本仓库管理的 Codex 交接处理器，保留原话恢复。
- `--codex-handoff-compactions N`：配置非负整数压缩次数阈值，默认 `0`。设为 `0` 时没有按次数触发的强停策略。

同时使用 Windows 与 WSL 时，在各自终端用同一仓库版本预演，按探测到的本侧客户端部署。跨子系统调用的可用方向取决于 CLI 路径、认证状态与项目路径。

---

## 二、 单端工作流与双端协作机制一览

### 1. 两端各自的独立工作流

| 能力维度 | 反重力侧（`~/.gemini/config/plugins/user-guard`） | Codex 侧（`~/.codex`） |
| --- | --- | --- |
| 全局工程与科研规则 | `rules/AGENTS.md`（`R0～R11`） | `AGENTS.md` 同步 `R0～R9`，并保留已有 `## 工具与本机`。没有本机段时使用模板的通用运行环境说明 |
| 压缩原话恢复 | `SessionStart` 与 `PreInvocation` 检查压缩轨迹，生成 `_user_words.md` 并注入恢复提示 | `SessionStart`（`compact\|resume`）合并可读的同会话 rollout 分段，生成 `hooks/state/user-words/<id>.md` 并注入恢复提示 |
| 三个严格只读子代理 | `agents/readonly-{reader,auditor,collaborator}.md`（仅开放 `read_file`、`search_web`） | `agents/readonly-{reader,auditor,collaborator}.toml`（仅限只读读取、检索与列目录，禁写禁构建） |
| 项目目录约束 | `PreToolUse`（`status_guard.py`）检查可识别的越界写删和重复日志轮询 | `config.toml` 配置工作区写入沙箱，并补齐缺失的审批策略和外部钩子导入默认值；已有个性化键按安装器规则保留 |
| 长会话交接 | `handoff_gate.py` 根据反重力轨迹提醒或进入 `drain`／`freeze`，`Stop` 校验交接材料 | `handoff_gate.py --client codex` 默认记录观测和提醒，严格模式按显式策略限制工具调用和结束当前回合 |
| 本地文献库接入（可选） | 自动探测本机 `zotero-mcp` 并无损写入 `mcp_config.json`（未安装则静默跳过） | 自动探测本机 `zotero-mcp` 并无损写入 `config.toml` 的 `[mcp_servers.zotero]`（未安装则静默跳过） |

### 2. 双端安装时的跨平台自动协作（`cae-harness`）

双端已安装且认证可用时，由当前总控按任务发起独立的 CLI 协作。

| 项目侧重 | 主控端 | 独立协作职责 |
| --- | --- | --- |
| 代码工程 | Codex | 反重力参与方向校正、技术表述润色和独立验收 |
| 科研主题 | 反重力 | Codex 承担其中部分高质量编码和数学推导工作 |

1. **模型与挡位**。前端主会话由用户选定模型与挡位。后台 CLI 任务由 `cae-harness` 按任务类型选路，支持 `--engine` 和 `--tier` 显式覆盖。数理推导和路线攻坚采用可用的单模型最高挡（如 `max` 或 `xhigh`），默认避开 `ultra` 的二次派生。常规编码与审查采用 `high`，短篇润色采用 `medium`，表面核查采用 `low`。Codex 原生 `spawn_agent` 由总控按当前可用模型和挡位显式调用。
2. **额度不足自动熔断，交由你二选一**：任一端触发额度上限时立即停止重试（返回 `quota_exhausted`，退出码 `5`）并记入本侧冷却缓存；冷却期内再次触发跨端调用前自动拦截，提示你选择 **① 先不发起（由当前主控在本侧独立完成）** 或 **② 充值／恢复后加 `--retry-quota` 重试**。
3. **独立 CLI 会话**。跨端调用使用 `codex exec --ephemeral` 或反重力无头 CLI 存储。派工任务单应包含所需背景、工作目录、授权范围和验收要求。

---

## 三、 仓库目录结构

| 目录／文件 | 作用（白话说明） |
| --- | --- |
| `AGENTS.md` | **双端自举引导文件**：Codex 与反重力打开本仓库时自动共读，指导客户端探测环境、提示单端用户并配置自身与对侧 |
| `install.py`、`install.ps1`、`install.sh` | 跨平台统一安装器，支持单 Codex、单反重力、双端协作安装及沙箱隔离验证 |
| `template/user-guard/` | 反重力原生插件模板，包含双端共用的规则、原话恢复与交接脚本，以及共享技能 `skills/cae-harness/` |
| `template/codex/` | Codex 独立工作流声明式模板（`hooks.json` 与 `agents/readonly-{reader,auditor,collaborator}.toml`） |
| `docs/` | [协作实践指南](docs/反重力全域工程化最佳实践指南.md)与[生命周期钩子底层规范](docs/架构设计与生命周期钩子规范.md) |
| `tests/` | 覆盖单 Codex、单反重力、双端安装、双模钩子、跨端编排与全库脱敏合规的离线回归测试 |

---

## 四、 后续软件与模型升级维护（任意一端引导更新）

客户端或模型目录更新时，可先用共享技能评估当前环境，再决定是否同步默认值。

1. `assess` 输出两侧 CLI 与模型目录来源、时效、已声明能力和场景选路建议。能力声明、推理挡位建议与当前账号的真实可用性分别核对，未实测项标为未验证。
2. 默认安装保留用户已有的模型和挡位。确需修改时用 `--sync-model-defaults` 预演并核对变更。宿主协议升级后，分别记录离线回归和实际宿主验证的版本、事件、结果与未验证项。

---

## 五、 沙箱验证与调试命令参考

`v1.5.0` 的 Windows Codex 提醒、低阈值自动压缩事件链、跨进程续接及隔离严格模式已完成真实会话验收；适用范围与证据层级见[实机验收记录](docs/架构设计与生命周期钩子规范.md)。生产默认仍为 `advisory`。

修改仓库源码后，须先在临时目录验证单端与双端沙箱安装及全量测试；向项目外全局生产目录部署前，须按 R4 列出拟写入路径并获用户批准（完整协作参数见[共享技能说明](template/user-guard/skills/cae-harness/SKILL.md)）：

```sh
# 1. 只读探测本机客户端状态与安装预演
python -B install.py --probe-apps
python -B install.py --dry-run --yes

# 2. 临时沙箱隔离验证（单 Codex / 单反重力 / 双端）
python -B install.py --app codex --codex-home "<临时目录>/codex_home" --no-zotero
python -B install.py --app antigravity --target-dir "<临时目录>/plugins/user-guard" --no-zotero
python -B install.py --app both --target-dir "<临时目录>/plugins/user-guard" --codex-home "<临时目录>/codex_home" --no-zotero

# 3. 探测跨端 CLI 并评估模型能力与挡位
python -B template/user-guard/skills/cae-harness/scripts/cae_harness.py probe
python -B template/user-guard/skills/cae-harness/scripts/cae_harness.py assess --caller codex

# 4. 运行全量无字节码（-B）离线回归测试
python -B -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

---

## 六、 安全边界与开源许可

- **写入约束**。代码子任务（`task --kind code` 与 `gate`）显式设置宿主沙箱，并在执行前后核对工作区变更。反重力 `PreToolUse` 对脚本能够识别的操作补充检查，隔离范围由宿主沙箱和操作系统权限决定。
- **外部工具与合规声明**：本仓库仅包含原创 Python 标准库脚本与声明式配置文档，仅通过操作系统标准命令行子进程调用用户自行从官方渠道安装并完成官方账号登录的本地客户端（`agy`、`codex` 及可选本地文献工具 `zotero-mcp`）；不包含、不链接亦不分发任何第三方专有程序或认证凭据，不接入任何非官方第三方接口，亦不包含任何绕过官方认证、权限或计费限制的机制。
- **开源许可证**：本仓库采用 [Apache License 2.0](LICENSE) 开源许可（`Copyright 2026 Xinyu-Li-1997`）。软件按“原样（AS IS）”提供，不附带任何明示或暗示担保；任何修改版分发须按许可证第 4(b) 条在变更文件中保留显著修改声明。

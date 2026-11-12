# Codex 与反重力双向协作及 `user-guard` 插件

本仓库提供反重力（Google Antigravity）与 OpenAI Codex 共用的工程／科研全局规则、防失忆与防越界生命周期钩子、只读审查子代理，以及跨端双向协作技能 `cae-harness`。全部脚本仅依赖 Python 3.9+ 标准库，零第三方依赖。

**日常使用方式**：完成一次安装后，**平时无需手动敲命令行脚本**。代码工程项目直接在 Codex 对话框里聊，科研探索／数理推导项目直接在反重力对话框里聊；两边会自动加载全局规则，遇到需要跨端独立审查、方向校正、润色、数学推导或带验收命令的代码闭环时，当前主控会在后台自动调用另一方，核实证据后把结果汇总回你正在使用的主对话框。

---

## 一、 协作分工与日常运行机制

| 项目侧重 | 主控端 | 另一方自动承担的协作职责 |
| --- | --- | --- |
| 代码工程 | Codex | 反重力参与方向校正、技术表述润色和独立验收 |
| 科研主题 | 反重力 | Codex 承担其中部分高质量编码和数学推导工作 |

1. **模型因地制宜按专长选路，思考挡位按任务分级**：`cae-harness` 仅在已登录的 `agy` 与 `codex` 之间调度（不依赖第三方 API Key 或外部 SDK）。默认策略（`--tier auto`）在运行期动态解析两侧模型元数据：数理推导（`math`）与路线攻坚（`direction`）自动匹配深度推理专长模型，大窗口代码实现（`code`）、全量审计（`audit`）与门禁闭环（`gate`）匹配大上下文工程主力模型，表面快速核查匹配轻量高速模型，并自动保护宿主内长周期稀缺配额池模型不被后台消耗。思考挡位同步分级：攻坚任务与门禁排错重试轮采用**单模型纯思考最高挡**（`max`／`xhigh`，默认禁开子任务内部套娃派生挡位 `ultra`），常规编码／常规审查／长文润色采用 `high` 挡，短篇纯文字润色采用 `medium` 挡，表面核查或显式省额度指令采用 `low` 挡，并在回执中透明披露。
2. **额度不足自动熔断，交由你二选一**：某一方触发用量或额度上限时，脚本立即停止重试（返回 `quota_exhausted`，退出码 `5`），并将恢复参考时间记入本侧缓存。冷却期内再次触发跨端调用前会自动拦截，由当前主控提示你选择：**① 先不发起跨端调用（直接由当前主控在本侧完成）**，或 **② 充值／额度恢复后加 `--retry-quota` 重试**。
3. **无痕隔离，不弄乱桌面项目树与聊天侧边栏**：跨端调用 Codex 时固定启用 `codex exec --ephemeral` 无痕会话，不写入侧边栏历史或新增项目条目；跨端调用反重力时走 `agy` CLI 独立存储（`~/.gemini/antigravity-cli`），不向反重力桌面端／IDE 聊天栏塞入机器子任务。
4. **双侧项目目录硬边界（防业务盘乱生垃圾文件）**：未经你明确下达指令批准，反重力与 Codex 均严禁在当前项目根目录之外的业务目录（如父目录 `../.codex-worktrees`、其他项目文件夹）或全局核心配置中创建、修改、移动或删除文件；系统标准临时目录、与项目关联的对话记忆／子代理轨迹／草稿目录及 `~/.cache` 工具缓存正常放行。一旦真需改动外部路径，交互会话会弹窗分行写清**当前任务意图、具体写删动作、项目外目标路径与触发语句**请你批准，后台无头子任务越界则直接硬阻断。

---

## 二、 快速安装与双环境同步

### 1. 前置环境
- Python `3.9+`（Windows 为 `python`，Linux／macOS／WSL 为 `python3`）；
- 已安装并登录反重力客户端（含 `agy` CLI），以及按需安装并登录的 Codex 客户端（含 `codex` CLI）。

### 2. 一键正式安装

```sh
git clone https://github.com/Xinyu-Li-1997/Antigravity_Codex_configuration.git
cd Antigravity_Codex_configuration
```

- **Windows（PowerShell）**：
  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
  ```
- **Linux / macOS / WSL**：
  ```sh
  bash install.sh
  ```

若同时使用 **Windows + WSL**，在两侧终端进入同一个已验收提交的仓库目录各运行一次对应脚本即可。

> **Codex 全局规则自动无损同步**：安装器除部署反重力全套插件（含反重力侧 `rules/AGENTS.md`）与 Codex 的 `cae-harness` 技能外，还会自动将核心总纲（`R0～R9`）同步至 `~/.codex/AGENTS.md`，并**完整保留**你在文件末尾自定义的 `## 工具与本机`（`R10`／`R11` 内存与硬件说明）段落，确保反重力与 Codex 双端始终遵循同一份最新总纲（`--no-codex-rules` 可跳过）。

### 3. 安装项说明与沙箱预演

| 安装项 | 默认位置与行为 |
| --- | --- |
| 反重力插件 | 本侧 `~/.gemini/config/plugins/user-guard`（自动清理陈旧文件与字节码，保留历史原话归档） |
| Codex 共享技能与全局总纲 | 检测到 Codex 配置目录时，同步技能至 `$CODEX_HOME/skills/cae-harness` 或 `~/.codex/skills/cae-harness`，并无损同步 `~/.codex/AGENTS.md` 核心总纲（`--no-codex-skill`／`--no-codex-rules` 可跳过） |
| 双端主窗口默认最高挡对齐 | 自动探测本侧反重力与 Codex 当前旗舰模型最高挡并写入主界面默认配置（`--no-model-config` 可跳过） |
| Codex 沙箱边界收紧 | 自动将 `~/.codex/config.toml` 收紧为 `sandbox_mode = "workspace-write"`、`approval_policy = "on-request"` 并保留出站网络（`--no-sandbox-config` 可跳过） |
| 可选 Zotero MCP | 本机已安装 `zotero-mcp` 时自动接入本地文献配置（`--no-zotero` 可跳过） |

修改本仓库源码后准备验证时，可先用预演或临时沙箱目录测试，通过后再更新本机生产目录：

```sh
python -B install.py --dry-run
python -B install.py --target-dir "<临时目录>/plugins/user-guard" --codex-skills-dir "<临时目录>/codex/skills" --no-zotero
```

WSL 环境优先调用本侧已登录的原生 CLI；当 WSL 侧未安装原生 `codex` 或原生 `agy` 未登录时，脚本均会自动通过跨子系统进程树监督回退调用 Windows 侧已登录的 `codex.exe`／`agy.exe`：`/mnt/<盘符>` 挂载目录直接支持只读与授权代码写入，WSL 纯 Linux 路径自动通过系统临时目录只读镜像支持审查与推导（代码写入任务需置于 `/mnt/<盘符>` 目录或在 WSL 使用已登录的原生 CLI）。

---

## 三、 仓库结构与四大守护模块说明

| 目录／文件 | 作用（白话说明） |
| --- | --- |
| `template/user-guard/plugin.json`、`hooks.json` | 插件清单与四类生命周期钩子注册表 |
| `template/user-guard/rules/AGENTS.md` | `R0～R11` 全局工程与科研规则（精简原则、中文规范术语、双向分工、项目硬边界、防无声放大、压缩对齐） |
| `hooks/session_context_loader.py`、`user_words.py` | **防压缩失忆**：仅在长对话发生自动压缩或 `/compact` 后首轮触发 1 次，从磁盘找回本会话历轮用户原话并注入上下文 |
| `hooks/status_guard.py` | **防越界乱改 + 防原地空转轮询**：① 拦截未经批准的项目外业务盘写删与全局配置改动（放行临时目录、对话记忆与缓存）；② 算例日志或后台任务无新输出时拦截原地重复查询，有新输出或翻页读取自动放行 |
| `hooks/reply_repeat_guard.py` | **防废话刷屏**：拦截连续同质的“仍在运行中，请稍候”短回复（汇报真实迭代步数／残差进展正常放行） |
| `template/user-guard/agents/` | 三个严格只读角色：原文纯摘录（`readonly-reader`）、行号证据核验（`readonly-auditor`）、方向／润色／数学推导协作（`readonly-collaborator`） |
| `template/user-guard/skills/cae-harness/` | 双端共享编排技能与脚本，支持环境探测（`probe`）、双路盲审（`audit`）、独立协作（`task`）与测试门禁闭环（`gate`） |
| `docs/` | [协作实践指南](docs/反重力全域工程化最佳实践指南.md)与[生命周期钩子底层规范](docs/架构设计与生命周期钩子规范.md) |
| `tests/` | 生命周期、门禁、跨端编排、安装同步与全库脱敏的离线自动化回归测试 |

---

## 四、 后续软件与模型升级维护（反重力引导更新）

仓库源码对模型名、挡位与客户端路径均采用运行期动态探测（零硬编码），后续两家软件或模型升级时按以下三类情况处理即可：

1. **两家发布新旗舰模型或新推理挡位（无需改代码）**
   用反重力打开本仓库目录，直接发送：
   > *“两家模型更新了，帮我刷新探测最新模型列表，并把本机（Windows／WSL）的默认最高挡配置同步好。”*

   反重力会执行 `probe --discover-models` 刷新模型目录缓存，并运行安装器（经弹窗确认后）把最新旗舰最高挡写入本机默认配置。

2. **反重力或 Codex 客户端日常小版本自动升级（无需操作）**
   用户级插件目录与 Codex 技能目录独立于客户端程序安装目录；桌面端升级更换二进制子目录后，脚本会自动按最新修改时间定位新版可执行文件。

3. **客户端发生底层大改版（如 CLI 参数、钩子协议或沙箱配置格式变更）**
   用反重力打开本仓库目录，直接发送：
   > *“反重力／Codex 升级了，帮我对照最新 CLI 与官方插件规范做一次兼容性巡检，更新仓库、跑通测试、同步本机并推送远端。”*

   反重力会结合内置官方规范技能与本机新版 CLI 实测结果定位变更，一站式完成源码更新、全量回归测试、经你确认后同步本机环境及 Git 远端推送。

---

## 五、 调试与验收命令参考

以下 CLI 命令主要由当前主控 AI 自动按需调用，也可在手动调试或独立复核时从仓库根目录直接执行（完整参数见[共享技能说明](template/user-guard/skills/cae-harness/SKILL.md)）：

```sh
# 探测双端 CLI、模型挡位、估算预算与额度冷却状态（加 --discover-models 主动刷新模型列表）
python -B template/user-guard/skills/cae-harness/scripts/cae_harness.py probe

# 跨端派发独立任务预演（review / direction / polish / math 为只读模式，code 为授权范围写模式）
python -B template/user-guard/skills/cae-harness/scripts/cae_harness.py task --caller codex --engine agy:default --kind direction --target "<项目目录>" --task-file "<UTF-8任务单>" --dry-run
python -B template/user-guard/skills/cae-harness/scripts/cae_harness.py task --caller antigravity --engine codex:default --kind math --target "<项目目录>" --task-file "<UTF-8任务单>"

# 运行全量无字节码（-B）离线回归测试
python -B -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

---

## 六、 安全边界与开源许可

- **安全使用边界**：授权代码写入子任务（`task --kind code` 与 `gate`）在无头 CLI 进程中运行时，由宿主工作区沙箱（`codex -s workspace-write`／`agy --sandbox`）与本插件 `PreToolUse` 硬边界钩子双重兜底（越界操作硬阻断），并在执行前后采集文件快照核对变更范围；建议始终在受 Git 管理的独立项目工作区中使用。
- **外部工具与合规声明**：本仓库仅包含原创 Python 标准库脚本与声明式配置文档，仅通过操作系统标准命令行子进程调用用户自行从官方渠道安装并完成官方账号登录的本地客户端（`agy`、`codex` 及可选本地文献工具 `zotero-mcp`）；不包含、不链接亦不分发任何第三方专有程序或认证凭据，不接入任何非官方第三方接口，亦不包含任何绕过官方认证、权限或计费限制的机制。
- **开源许可证**：本仓库采用 [Apache License 2.0](LICENSE) 开源许可（`Copyright 2026 Xinyu-Li-1997`）。软件按“原样（AS IS）”提供，不附带任何明示或暗示担保；任何修改版分发须按许可证第 4(b) 条在变更文件中保留显著修改声明。

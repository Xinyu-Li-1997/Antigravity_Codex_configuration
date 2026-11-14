# 仓库维护、多端自举与升级回归指引（Antigravity、Codex ＆ Claude Code 共读）

本仓库（`Antigravity_Codex_configuration`）单点维护 Google Antigravity（反重力）、OpenAI Codex、Anthropic Claude Code 与 CC Switch 共用的工程与科学计算规则、生命周期钩子、严格只读子代理、本地模型分流路由及跨端协作技能（仅依赖 Python 3.11+ 标准库，面向 Windows 与 WSL2／Linux 主力环境）。无论用户在反重力、Codex 还是 Claude Code 中打开本仓库，当前 AI 助手均可按以下规范完成本机探测、部署升级与全栈回归验收。

## 一、核心原则：单端独立闭环、多端按专长协作与反过度工程

1. **单反重力部署（`--app antigravity`）**：安装 `~/.gemini/config/plugins/user-guard` 原生插件，一次配齐 `R0–R11` 全局规则、压缩后用户原话自动恢复、项目目录硬边界与算例日志防空转门禁、空等刷屏拦截、长会话收口状态机、三个默认 `inherit` 跟随主对话的只读子代理及共享技能，可选接入 `zotero-mcp`。
2. **单 Codex 部署（`--app codex`）**：同步 `~/.codex/AGENTS.md`（自动保留末尾自定义的 `## 工具与本机` 段落）、三个只读子代理（`sandbox_mode = "read-only"`）、压缩／恢复原话归档与长会话交接钩子（默认 `advisory` 提醒模式）、`workspace-write` 工作区沙箱基线及可选 `zotero-mcp`。
3. **单 Claude Code 与 CC Switch 协同部署（`--app claude`）**：同步 `~/.claude/CLAUDE.md`（自动保留末尾自定义的 `## 工具与...` 段落）、三个只读子代理（`tools: Read, Grep, Glob`）、原话回溯／`R4` 目录硬边界 AST 守卫／防空转轮询／防重复刷屏／长会话交接钩子（无损合并 `settings.json` 并自动迁移旧版拆分式钩子，默认补齐 `effortLevel: "xhigh"`）；附带 `--with-cc-router` 时同步 `~/.claude/router/`（Kimi 主模型 + DeepSeek 子代理 HTTP 分流守护进程 `router.py`、CC Switch SQLite 卡片无损投影管理 CLI `ccswitch_admin.py` 及 WSLg 防白屏启动／`.deb` 校验升级脚本）；附带 `--disable-ccswitch-prompts` 时在线备份并禁用 `cc-switch.db` 中已启用的陈旧提示词，防止反向覆盖全局规则。
4. **双端／三端协同部署（`--app both`、`--app all` 或自动检测 `auto`）**：同时部署对应客户端独立工作流与共享的 `cae-harness` 技能：
   - **主会话与原生子代理**：各客户端前端保留用户自选的主模型与挡位（反重力日常科研与求解器综合研发推荐单模型 `Gemini 4 Carbon (High)` 作总控；主会话及派生的原生 `inherit` 子代理各自拥有独立大上下文窗口，不受 `cae-harness` 影响）。
   - **硬核工作流优先交由 Codex 端攻坚与本侧回退**：凡涉及偏微分方程推导、高阶数值格式分析（如 DGSEM／WENO 离散、雅可比矩阵、守恒性与稳定性证明）、核心求解器编码、深层边界审查及门禁验收时，**优先通过 `cae-harness` 发起 Codex 无头工作流**——默认选用专长模型（`Astra`／最新代 `Sol`）+ 单模型纯思考最高挡 `max` + 声明最大上下文窗口，由反重力总控负责最终逐行验收（同端 `run_workflow` 仅用于纯只读结构化盘点）；仅在用户要求降挡或省 token／省额度时切换分级省流；附带 `--fallback-on-quota` 时，若 Codex 在预检或启动期额度耗尽且未改动工作区文件，自动熔断并切回反重力本侧匹配模型完成。
   - **反过度工程与阶段收口全量把关**：全仓严格执行 `R3.3` 反过度工程原则，只维护目标环境（Python 3.11+、Windows 与 WSL2／Linux）真实需要的精简代码，严禁为未要求的老旧版本或外围平台堆砌回退解析器与假想兼容层。里程碑收口或发版前对全量文件分工闭环——由 Codex 无头工作流负责代码回归检查、Debug 排错、死代码清理与门禁验证；由反重力负责文档润色、清淤精简与隐私凭据脱敏，经总控逐行复核代码差异与运行证据后收口。

## 二、AI 引导本机配置标准流程（四步自举）

当用户提出“帮我配置本机”“升级配置”或“检查安装状态”时，严格按以下四步执行：

1. **只读探测客户端与跨子系统环境（零写入）**：
   运行 `python -B install.py --probe-apps` 与 `python -B install.py --audit-upgrade`（WSL2／Linux 用 `python3`），读取输出 JSON，核实当前系统及跨子系统对侧（Windows `<->` WSL）是否已安装反重力、Codex、Claude Code、CC Switch 与 `zotero-mcp`，并查清规则／钩子漂移与模型配置对齐情况。
2. **仅安装单端时先请用户拍板**：
   - 若本侧与跨子系统对侧**一共只装了其中一个客户端**，如实告知探测结果，请用户二选一：**① 先安装另一端客户端后再配多端协作**，或 **② 直接为当前已安装的客户端部署独立工作流**。
   - 若本侧与 WSL／Windows 对侧分别装了不同客户端，先按预演流程配好当前侧，再给出对侧终端的同步安装命令。
3. **先空跑预演，经 R4 明确批准后再落盘**：
   先运行 `python -B install.py --dry-run --yes`（可附用户选定的 `--app`、`--with-cc-router`、`--disable-ccswitch-prompts`）生成变更计划。向用户列清拟写入或更新的宿主机全局路径、既有模型与自定义 `## 工具与...` 段落保留情况、CC Switch 数据库原子备份策略及钩子合并方案，获得明确同意后再执行正式安装。注意：前端默认模型仅补缺失项（`--sync-model-defaults` 显式覆盖，`--no-model-config` 完全跳过，二者互斥）；Codex 与 Claude Code 交接默认 `advisory` 且压缩阈值 `0`，启用 `enforce` 前须明确策略并在宿主实测。
4. **源码改动后的全量回归、清淤脱敏与沙箱隔离验证**：
   修改本仓库任何代码、规则或模板后，先完成 Codex 代码回归 Debug 优化与反重力文档润色清淤脱敏，再在系统标准临时目录跑通五模式沙箱隔离安装（`codex`、`antigravity`、`claude`、`both`、`all`）及全量离线回归测试（`python -B -X utf8 -m unittest discover -s tests -p "test_*.py" -v`），确认不含任何个人用户名路径、具体硬件型号、内网地址或密钥凭据后，征得用户同意再提交推送或同步生产环境。

## 三、客户端、CC Switch 与大模型升级标准适配回归流程（触发式六步闭环）

当用户提出**“根据各自升级做适配与回归测试”**（或要求针对客户端、CC Switch、大模型更新执行例行回归）时，AI 助手自动按以下六步标准流水线端到端闭环完成：

1. **全栈版本与大模型目录只读体检**：
   依次运行 `python -B install.py --audit-upgrade`、`python -B template/user-guard/skills/cae-harness/scripts/cae_harness.py probe --discover-models` 与 `assess`（若启用了 WSL，同步只读检查 WSL 侧版本与状态），完整采集 Antigravity、Codex、Claude Code、CC Switch 的最新版本号、SQLite `PRAGMA user_version` Schema、生命周期钩子协议，以及各端最新大模型目录（主力模型代际如 `Astra`/`Sol`/`Carbon`、Kimi/DeepSeek 卡片模型 ID、声明最大上下文窗口、可调思考挡位 `max`/`xhigh`/`high` 及已退役模型下线状态）。
2. **最新大模型能力匹配与协议差异适配**：
   针对体检发现的模型代际演进、窗口扩容、思考挡位调整、客户端钩子字段变更或 CC Switch 表结构更新，精准修订 `install.py`、`cae_harness.py`、`template/claude/router/` 及对应配置模板；严格遵守 `R3.3`，清理过时模型映射，不为废弃旧版堆砌兼容分支。
3. **Codex 无头代码审查、Debug 与门禁验证**：
   通过 `cae-harness`（选用最新代 `Sol` + 单模型最高纯思考挡 `max` + 声明最大上下文窗口）对全部改动源码开展边界审查、死代码清理与带 `--verify-cmd` 的自动化门禁闭环。
4. **全模式沙箱隔离安装与离线单元测试**：
   在系统标准临时目录跑通 `codex`、`antigravity`、`claude --with-cc-router`、`both`、`all --with-cc-router` 五种沙箱安装、沙箱 `--audit-upgrade` 零漂移复核与全量 `unittest` 套件，严守 `R3.6` 绝不弱化测试断言。
5. **技术文档润色、清淤精简与公开脱敏审计**：
   同步更新 `CHANGELOG.md` 与文档中的模型能力矩阵和架构说明（保持 `plugin.json` 单一版本真源与纯 ASCII 文件路径，落实 `R1/R2` 规范书面语与 `R5` 零无声放大），派发只读审计子代理核对行号依据，并通过全仓脱敏扫描（零个人路径、零主机／用户名、零硬件型号、零内网 IP、零密钥泄露）。
6. **宿主机双侧空跑预演与生产切流收口**：
   输出 `--dry-run --yes` 变更清单（含 Windows 与 WSL 配置目录、`cc-switch.db` 在线备份与本地分流路由平滑重启方案），获用户按 `R4` 批准后同步生产环境，最后复跑 `--audit-upgrade` 与路由健康检查确认零漂移收口。

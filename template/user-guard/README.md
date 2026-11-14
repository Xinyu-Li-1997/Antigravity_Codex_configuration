# `user-guard` 工程与科学计算协作插件

`user-guard` 是本仓库维护的核心模板目录：既作为 Google Antigravity（反重力）的原生插件直接安装到 `~/.gemini/config/plugins/user-guard`，同时也是 OpenAI Codex 与 Anthropic Claude Code 全局规则、生命周期守护钩子及跨端协作技能 `cae-harness` 的唯一同源实现（Codex 与 Claude Code 专属的事件挂载模板、子代理定义及本地分流路由分别位于同级的 `../codex/` 与 `../claude/`）。

## 目录结构与组件职责

| 路径 | 组件职责 |
| --- | --- |
| `plugin.json` | 反重力原生插件元信息清单（遵循官方 v1 Schema，作为全仓唯一版本号真源） |
| `assets/logo.svg` | 插件矢量图标，满足 `agy plugin validate --strict` 零提示规范 |
| `hooks.json` | 反重力生命周期钩子注册表，挂载 `SessionStart`、`PreInvocation`、`PreToolUse` 与 `Stop` 四类同步命令事件 |
| `rules/AGENTS.md` | 三端精炼共用的 `R0–R11` 全局规则：文件精简、中文与 CAE/CFD 规范术语、多端选模配窗与额度回退、阶段收口全量把关、反过度工程（`R3.3`）、项目目录硬边界与公开脱敏（`R4`）、防无声放大、验收红线、压缩交接对齐及 CC Switch 提示词隔离 |
| `hooks/user_words.py` | 三端共享底层库：UTF-8 标准流重配置、跨平台文件锁、原子写盘、反重力 JSONL 截断回查、Codex 跨分段 `rollout` 合并校验及 Claude Code 项目会话轨迹原话提取 |
| `hooks/session_context_loader.py` | 压缩与续接恢复入口：反重力侧按字节游标增量检测 `CHECKPOINT` 并注入用户原话摘要；Codex 与 Claude Code 侧在 `SessionStart`（`^(compact\|resume)$`）时归档并注入历史原话 |
| `hooks/status_guard.py` | `PreToolUse` 守卫：支持反重力与 Claude Code（`--client claude`）协议，静态解析表面文件写改工具、Shell 命令与动态工作流 AST，拦截未授权的项目外写删（交互会话弹窗请示，无头子任务直接拒绝），并用日志复合指纹阻断无进展重复轮询 |
| `hooks/reply_repeat_guard.py` | `Stop` 守卫：在反重力与 Claude Code 侧识别含迭代步数、残差等真实数值推进的正常汇报，拦截连续同质的空等刷屏回复（反重力侧内部联动收口终审，Claude Code 侧与 `handoff_gate.py` 同组挂载） |
| `hooks/handoff_gate.py` | 长会话收口状态机：反重力侧按压缩频次、轨迹体积与读图负载执行 `normal -> advisory -> drain -> freeze` 迁移（支持 R8 暂缓宽限）；Codex 与 Claude Code 侧默认运行 `advisory` 提醒模式（可选 `enforce`） |
| `agents/readonly-reader.md` | 反重力只读通读子代理（默认 `inherit` 跟随主对话模型与独立窗口），负责长文献、大源码与算例日志的忠实摘录和行号定位 |
| `agents/readonly-auditor.md` | 反重力只读审查子代理（默认 `inherit`），负责逐行核对公式符号、边界条件、数值容差与运行证据 |
| `agents/readonly-collaborator.md` | 反重力只读协作子代理（默认 `inherit`），负责数学与离散格式推导、方案论证及技术文档润色 |
| `skills/cae-harness/` | 跨端 CLI 协作技能（`SKILL.md` 与 `scripts/cae_harness.py`），提供模型目录评估（`probe`／`assess`）、独立任务委派（`task`）、大输入审计（`audit`）与命令门禁验收（`gate`） |

## 选模、配窗与额度回退要点

1. **原生子代理与 `cae-harness` 互不干扰**：反重力通过 `invoke_subagent` 启动的三个 `readonly-*` 子代理不设硬编码 `model` 字段，始终 `inherit` 跟随当前前端主会话的模型、挡位与独立大上下文窗口（Claude Code 侧三个 `readonly-*` 同样配置 `model: inherit`，在接入本地 `router.py` 分流时按子代理模型映射转发）；`cae-harness` 仅管理无头 CLI 子进程，独立读取运行侧模型目录选模配窗，不受前端主控模型切换影响。
2. **默认满血旗舰 + 按需分级省流**：`cae-harness` 默认选用旗舰专长模型（Codex 数学推导与路线论证选 `Astra`，编码、审查、审计与门禁选最新代 `Sol`；反重力 CLI 选 `Carbon`），启用单模型纯思考最高挡（Codex `max`、反重力 `Carbon` `high`；纯文字润色 `polish` 短篇 `medium`、长篇 `high`；默认跳过二次派生的 `ultra` 挡）及所选模型声明最大上下文窗口（当前 Codex 目录为 `872,000` token）。仅当用户明确或模糊提出降挡、省额度、省 token 时，才切换为分级省流模式。
3. **额度耗尽自动回退**：调用时附带 `--fallback-on-quota`，若对侧在预检或启动阶段触发额度熔断（`quota_exhausted`）且尚未改动工作区文件，自动切回发起方本侧匹配模型与最高可用挡位完成任务。

命令行细则见 [`cae-harness` 技能说明](skills/cae-harness/SKILL.md)，完整安装、全栈升级体检与架构文档见[仓库总 README](../../README.md)与[架构设计及生命周期钩子规范](../../docs/architecture-and-hooks.md)。

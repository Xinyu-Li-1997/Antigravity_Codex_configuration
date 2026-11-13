# `user-guard` 工程与科研协作插件

本插件为 Google Antigravity（反重力）提供全局工程与科研规则、四类生命周期守护钩子、三个只读子代理，以及与 OpenAI Codex 共用的 `cae-harness` 跨端协作技能。

| 组件 | 作用说明 |
| --- | --- |
| `rules/AGENTS.md` | `R0–R11` 全局规则：精简文件、中文规范表达、主控分工与额度熔断回退、项目目录硬边界、防无声放大与压缩交接对齐 |
| `hooks/session_context_loader.py`、`hooks/user_words.py` | 压缩或续接后从本地会话轨迹提取并归档用户原话（反重力增量扫描检查点，Codex 合并同会话 rollout 分段） |
| `hooks/status_guard.py` | 拦截未授权的项目外文件写删与无进展重复日志轮询（交互主会话弹窗请示，无头子任务直接拒绝） |
| `hooks/reply_repeat_guard.py` | 拦截连续同质的空等刷屏回复（含真实步数／残差更新的正常放行），并在长会话收口 `Stop` 阶段核验交接提示词与 `/goal` 结束标记 |
| `hooks/handoff_gate.py` | 长会话收口状态机：反重力按检查点、轨迹体积与读图负载进入预警、排空或冻结（支持 R8 显式暂缓）；Codex 默认采用 `advisory` 提醒模式 |
| `agents/readonly-reader.md` | 只读长文档通读与带行号原文摘录 |
| `agents/readonly-auditor.md` | 只读审查与验收，逐条核对文件路径、行号、公式与数值依据 |
| `agents/readonly-collaborator.md` | 只读协作，承担方向分析、技术润色与数学推导，通过结构化回执返回正文与证据 |
| `skills/cae-harness/` | 双端 CLI 能力评估（`assess`）、独立任务派发（`task`）、双路／分片审计（`audit`）与命令门禁验收（`gate`） |

日常科研与综合工程默认以反重力作为主控，遇到数学公式推导、高阶数值格式分析与核心代码实现时，按需通过 `cae-harness` 调用 Codex（自动匹配 `Astra` 或最新代 `Sol`，启用单模型最高推理挡位 `max` 与模型声明最大窗口）；若 Codex 触发额度上限，立即熔断并可通过 `--fallback-on-quota` 自动切回反重力本侧匹配的模型与挡位继续完成。具体用法见 [cae-harness 技能说明](skills/cae-harness/SKILL.md)。

仓库根目录的安装脚本支持单反重力、单 Codex 与双端一键部署（Windows 用 `install.ps1`，Linux／macOS／WSL 用 `install.sh`），默认保留用户已选模型和挡位。完整说明见[仓库总 README](../../README.md)。

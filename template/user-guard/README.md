# `user-guard` 工程与科研协作插件

本插件为反重力（Google Antigravity）提供全局工程与科研规则、四类生命周期守护钩子、三个严格只读子代理，以及与 OpenAI Codex 共用的 `cae-harness` 双向协作技能。

| 组件 | 作用说明 |
| --- | --- |
| `rules/AGENTS.md` | `R0～R11` 全局规则：文件最少原则、中文规范专业表达、双向主控分工、项目目录硬边界、防无声放大与压缩接续对齐 |
| `hooks/session_context_loader.py`、`hooks/user_words.py` | 压缩或恢复后从可读会话记录生成用户原话归档。反重力增量扫描检查点，Codex 合并同会话 rollout 分段 |
| `hooks/status_guard.py` | 检查可识别的项目外写删动作及重复日志轮询。反重力交互会话可发出审批提示，无头子任务可拒绝工具调用。权限边界由宿主沙箱和操作系统实施 |
| `hooks/reply_repeat_guard.py` | **防废话刷屏 + 交接停机终审**：拦截连续同质的短等待回复（放行含真实数值／步数进展的汇报），并在反重力长会话进入收口期时在 `Stop` 端校验交接提示词与 `/goal` 停止标记 |
| `hooks/handoff_gate.py` | 两端共用交接原则，各自适配宿主事件。反重力按检查点、轨迹大小、读图负载与历史截断提示进入提醒、排空或冻结，支持 R8 显式暂缓。Codex 入口为 `--client codex`，默认仅记录观测和提醒 |
| `agents/readonly-reader.md` | 只读长文档通读与带行号原文纯摘录 |
| `agents/readonly-auditor.md` | 只读审计与验收，逐条核对文件路径、行号、公式与数值证据 |
| `agents/readonly-collaborator.md` | 只读跨端协作，承担方向分析、技术润色与数学推导，正文通过结构化回执交付 |
| `skills/cae-harness/` | 两端 CLI 能力评估（`assess`）、双路审计（`audit`）、独立任务（`task`）与指定命令验收（`gate`） |

偏代码工程由 Codex 总控，反重力参与方向校正、技术表述润色和独立验收。偏科研主题由反重力总控，Codex 承担其中部分高质量编码和数学工作。前端遵循用户显式选择的模型和挡位。`cae-harness` 根据任务选择独立 CLI 子任务的模型和挡位。Codex 原生 `spawn_agent` 由总控显式调用。跨端调用遇到额度不足时停止重试，按回执选择在本侧完成或在额度恢复后使用 `--retry-quota`。调用方式和模型评估见 [cae-harness 技能说明](skills/cae-harness/SKILL.md)。

仓库根目录的安装器支持单反重力、单 Codex 和双端部署。Windows 使用 `install.ps1`，Linux／macOS／WSL 使用 `install.sh`。默认保留已有模型与挡位，只补缺失项。部署参数、升级和验证流程见[仓库说明](../../README.md)。

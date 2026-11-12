# `user-guard` 工程与科研协作插件

本插件为反重力（Google Antigravity）提供全局工程与科研规则、四类生命周期守护钩子、三个严格只读子代理，以及与 OpenAI Codex 共用的 `cae-harness` 双向协作技能。

| 组件 | 作用说明 |
| --- | --- |
| `rules/AGENTS.md` | `R0～R11` 全局规则：文件最少原则、中文规范专业表达、双向主控分工、项目目录硬边界、防无声放大与压缩接续对齐 |
| `hooks/session_context_loader.py`、`hooks/user_words.py` | **双端防压缩失忆**：同时支持反重力（`PreInvocation` 增量检查点游标）与 Codex（`--client codex` 多分段 rollout 合并），长对话压缩或恢复后自动从磁盘找回历轮用户原话并注入 |
| `hooks/status_guard.py` | **项目硬边界 + 防空转轮询**：① 拦截未经批准的项目外业务盘写删与全局配置篡改（自动放行临时目录、对话记忆／子代理轨迹与 `.cache` 缓存；交互会话按四要素清晰弹窗请示，无头子任务硬阻断）；② 按复合指纹拦截无变化的日志／后台任务原地重复查询 |
| `hooks/reply_repeat_guard.py` | **防废话刷屏**：拦截连续同质的短等待回复，放行含真实数值／步数进展的汇报 |
| `agents/readonly-reader.md` | 只读长文档通读与带行号原文纯摘录 |
| `agents/readonly-auditor.md` | 只读审计与验收，逐条核对文件路径、行号、公式与数值证据 |
| `agents/readonly-collaborator.md` | 只读跨端协作，承担方向分析、技术润色与数学推导，正文通过结构化回执交付 |
| `skills/cae-harness/` | Codex 与反重力双向派工、大上下文双路审计（`audit`）、独立任务（`task`）与指定命令验收闭环（`gate`） |

**日常运行方式**：偏代码工程由 Codex 总控，反重力参与方向校正、润色与独立验收；偏科研主题由反重力总控，Codex 承担其中部分高质量编码与数学工作。跨端调用限定在已登录的 `agy` 与 `codex` 之间在 Windows／WSL 双端按需自动发起，遵循**模型因地制宜按专长选路、思考挡位按任务性质分级**（数理推导／路线攻坚匹配深度推理专长模型并拉满单模型纯思考最高挡 `max`／`xhigh`，禁开套娃派生 `ultra`；常规编码／审查用大窗口主力 `high` 挡；短润色用 `medium` 挡；表面核查用轻量高速 `low` 挡；自动保护长周期稀缺配额池）；遇到对侧额度不足时立即熔断并记入冷却缓存，后续发起前自动提示用户选择“先不发起（本侧完成）”或充值后 `--retry-quota` 重试。子任务全程采用无痕／独立无头会话，不污染两侧桌面项目树或聊天侧边栏。参数细节见 [cae-harness 技能说明](skills/cae-harness/SKILL.md)。

**安装与后续升级**：由仓库根目录安装脚本按检测到的客户端支持**单反重力独立模式、单 Codex 独立模式或双端协作模式**部署（Windows 运行 `install.ps1`，Linux／macOS／WSL 运行 `install.sh`）。后续两家模型或客户端升级时，用反重力或 Codex 任意一方打开配置仓库即可引导完成模型探测刷新、兼容性巡检与本机同步。

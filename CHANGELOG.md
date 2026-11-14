# 更新日志（Changelog）

记录 `Antigravity_Codex_configuration`（含 `user-guard` 原生插件与 `cae-harness` 双向协作编排器）各版本的功能演进、宿主协议适配与安全加固。

---

## [1.7.5] - 2026-11-14

完成仓库工程标准化完善、门面英文架构导引与跨平台自动化回归门禁建设（保持中文单源提示词内核零膨胀）：

- **文档路径纯 ASCII 标准化与双向链接补齐（`docs/workflow-guide.md`、`docs/architecture-and-hooks.md`、`README.md`、`template/user-guard/README.md`）**：将 `docs/` 下两份中文命名文档规范重命名为小写 ASCII 短横线路径，彻底消除跨平台 Git 八进制路径转义（`core.quotepath`）、URL 百分号编码与旧版文件名语义脱节问题，并同步更新全仓引用链接。
- **门面英文架构导引与语言作用边界声明（`README.md`）**：在不拆分冗余双语模板、不增加运行期常驻上下文开销的前提下，于根 `README.md` 顶部增设 `English Overview`，简明交代面向 AI-CAE／CFD／FEA／PDE 求解器研发的核心守护机制、三套部署模式及机器契约（英文）与内置交互规则（简体中文）的真实边界。
- **版本号单点真源、行尾规约与脚本提示口径统一（`template/user-guard/plugin.json`、`template/user-guard/README.md`、`install.ps1`、`.gitattributes`）**：移除插件子目录 `README.md` 中易遗漏的静态版本号硬编码，统一以 `plugin.json` 为全仓唯一版本真源；统一 `install.ps1` 错误输出语种并保持 PowerShell 5.1 所需的 UTF-8 BOM + `CRLF` 编码；补齐 `.gitattributes` 对 `.toml`、`.yml`、`.yaml`、`.svg` 的 `LF` 行尾约束。
- **跨平台 CI 自动回归门禁与仓库规范单测（`.github/workflows/ci.yml`、`tests/test_e2e_integration.py`）**：新增最小权限 GitHub Actions 工作流（覆盖 `ubuntu-latest` 与 `windows-latest`、Python `3.11`／`3.13` 三模式沙箱安装与全量离线测试），并在端到端测试中新增受控文件纯 ASCII 路径、Markdown 本地相对链接目标存在性及插件文档无重复静态版本号硬编码校验。

---

## [1.7.4] - 2026-11-14

落实反过度工程（`R3.3`）与全局规则精简去冗，统一 `Python 3.11+` 标准库基线并清理老版本与外围平台兼容包袱：

- **全局规则瘦身提纯与反过度工程红线（`template/user-guard/rules/AGENTS.md`、根目录 `AGENTS.md`、双端 `readonly-*` 子代理）**：将双端常驻加载的全局规则篇幅精简逾三分之一（去除与 `cae-harness/SKILL.md` 重复的 CLI 参数罗列）；在 `R3.3` 明确写入反过度工程约束（严禁为未要求的老旧版本、外围平台或假想场景编写兼容分支、回退解析器与多余抽象）；收紧 `R3.6` 披露边界（仅标注目标环境与核心验证链路的未实测项，严禁拿无关外围平台凑免责尾巴）；将原生 Shell／UTF-8 编码与公开仓库脱敏红线并入 `R4` 核心区，消除 Codex 启用自定义 `## 工具与本机` 尾段时漏掉脱敏与编码规则的盲区。
- **统一 `Python 3.11+` 基线并移除老版本与外围平台冗余代码（`install.py`、`install.ps1`、`install.sh`、`cae_harness.py`、`status_guard.py`）**：直接采用标准库 `tomllib`、`ast.Match` 与原生完整 ISO 时间戳解析，删除 `install.py` 与 `cae_harness.py` 中近 200 行针对 `Python 3.9/3.10` 的手写正则 TOML 回退解析器及 `Python 3.9` AST 兼容垫片；移除未使用的 `macOS` `/private/*` 根链接与 `.app` 探测分支，聚焦 Windows 与 WSL2／Linux 主力环境。
- **Codex 钩子升级参数一致性修复（`install.py`）**：升级已登记的 `session_context_loader.py` 托管钩子时，在保留既有可用解释器路径的前提下，自动将旧版遗留的 `-I -B` 启动参数补齐为 `-X utf8 -I -B`，保持二次运行严格幂等与信任哈希稳定。

---

## [1.7.3] - 2026-11-14

完成双端全量文件回归检查、边界 Debug 修复、性能优化与全仓文档润色清淤脱敏，并将阶段收口双端把关制度化写入核心规范：

- **阶段收口双端全量把关写入核心规范（`R3.1.4` 与根目录 `AGENTS.md`）**：明确双端协作项目在里程碑收口、合并或发版前按全量文件分工闭环——由 Codex 无头工作流承担全仓代码回归检查、Debug 排错、死代码／性能优化与验证门禁测试；由反重力承担全仓文档书面语润色、冗余清淤精简与隐私凭据脱敏把关，经总控逐行复核差异与运行证据后收口。
- **AST 边界守卫 Python 3.9 兼容与关键字／解包参数全覆盖（`status_guard.py`）**：将 `ast.Match` 改为 `getattr` 动态兼容保护，消除 Python 3.9 下 `AttributeError` 导致的静默放行旁路；补齐 `Path.rename`／`replace`、`os.remove`／`unlink`／`mkdir`／`makedirs`／`rmdir`／`removedirs`／`rename`／`replace`、`shutil.copy*`／`move`／`rmtree`、`np.save*`、`open(file=..., mode=...)` 及 `run_workflow` 中 `agent(**kwargs)` 的显式关键字参数与字典解包路径核验，兼容 UTF-8 BOM 工作流脚本并清理冗余未用函数；新增 `ast.parse(..., feature_version=(3, 9))` 全仓语法门禁测试。
- **长会话交接子壳分组拦截与压缩／恢复游标加固（`handoff_gate.py`、`session_context_loader.py`、`user_words.py`、`reply_repeat_guard.py`）**：`_split_shell_clauses` 支持切分引号外的 `( ... )` 子壳分组（保留 `$(...)` 命令替换与函数调用内嵌括号），阻断收口期 `(git push ...)` 绕过；修复 Codex `compacted` 与 `turn_context` 间夹杂普通事件行时 `pendingRecord` 提前清空、新用户回合未重置 `stopCorrectionRequested` 及降回 `advisory` 后的状态残留；补齐压缩游标元数据 Schema 与空文件／新 inode 重写自愈；修复子代理任务书提及系统标签字面量被误滤、中文紧邻 `/goal` 取消与时间戳归一化、Windows 文件锁显式解锁及超 `256 KiB` 交接尾行逆向完整读取。
- **编排器防空门禁、特殊文件防阻塞与 `verification_executed` 显式标记（`cae_harness.py`）**：拦截空串或纯空白 `--verify-cmd`，在 `gate` 与 `code` 任务回执中显式标注 `verification_executed`（无 `--verify-cmd` 的编码任务标注 `false` 并附未验证提示）；`snapshot_files` 与 `collect_target_files` 合并为单次 `lstat` 并拦截／跳过 FIFO、socket 等非常规特殊文件防打开挂死；确保 `assess` 纯只读零删除历史额度事件文件，`audit` 事后快照异常时保留已完成审计结果；`_windows_env` 校验变量名并优先走 `powershell.exe` 安全读取。
- **安装器跨平台兼容性加固与全仓清淤脱敏（`install.py`、`install.ps1`）**：`install.ps1` 改用标准流句柄直接继承与 `CommandLineToArgvW` 参数转义，兼顾无换行交互提示即时显示与 PowerShell 5.1 原生 `stderr`／退出码无损透传；`install.py` 区分 Windows 云盘重解析点与目录联接、放行 POSIX 系统根级 `/var`／`/tmp`／`/etc -> /private/*` 标准别名、支持引号包裹的 Windows `PATH` 项与含 `#` 字符串的回退 TOML 数组解析，并在插件升级时自动清理过时空目录；清理 `README.md` 残留对话措辞，全仓回归测试由 160 项扩充至 220 项。

---

## [1.7.2] - 2026-11-14

全面适配 Google Antigravity App `v2.32.0` 与 CLI `v1.5.8`，补齐动态工作流边界审查，并明确双端协作中优先交由 Codex 端无头工作流攻坚的分工规范：

- **双端按专长分工（硬核工作流优先交由 Codex 端攻坚）**：双端均可用时，凡涉及偏微分方程推导、高阶数值格式分析、核心求解器编码、严苛边界／脱敏审查及带验证命令的门禁闭环，优先通过 `cae-harness` 发起 Codex 无头工作流（默认启用 `Astra`／最新代 `Sol` 单模型最高思考挡 `max` 与 `872,000` token 声明窗口，配合 `workspace-write` 工程沙箱）；反重力主会话（推荐 `Carbon High`）负责全局统筹、多文献综合、同端纯只读盘点与最终逐行验收。
- **只读子代理兼容桌面端 `v2.32.0` 与 CLI `v1.5.8`**：移除 `readonly-reader`、`readonly-auditor`、`readonly-collaborator` 中会导致桌面端 `<subagents>` 目录隐藏该代理的非公开字段 `hidden: true`，保留 `subagent: true` 与 `mainAgent: true`；工具白名单统一为 `[read_file, search_web, send_message, finish]`，兼顾桌面端子代理消息回传与 CLI `--json-schema` 结构化收尾。
- **`run_workflow` 动态工作流与跨表面写改工具纳入 `R4` 目录硬边界**：`status_guard.py` 补齐 `write_to_file`、`replace_file_content`、`multi_replace_file_content`、`sed_file`、`notebook_edit` 五类文件修改工具及 `AbsolutePath`／`FilePath` 等路径字段识别（审批提示保留原始工具名），并将 `view_file` 纳入未变化日志防空转门禁；新增 `run_workflow` 启动前预审，静态检查外层 `Cwd`、`ScriptPath` 脚本文件（拦截缺失、超长、不可读或语法错误脚本）以及内联 `Script` 中的 Python 写删、`call_tool(...)` 与 `agent(..., cwd=...)` 越界调用。
- **内联 Python 与工作流 AST 静态路径追踪加固**：按源码真实求值顺序追踪变量候选路径、海象表达式（`:=`）、元组／列表解包、`with ... as` 绑定、f-string 拼接、`if/try/for/while` 分支合并、函数调用点实参重审及 `os.chdir` 目录切换；凡遇到无法静态证明安全的动态路径、非常量 `open` 模式、动态工具名或含未知解包（`**kwargs`）的工具参数，均触发审批或拦截；安装脚本调用检查覆盖重复传入的目标目录参数。
- **长会话收口门禁、冻结期 Git 分支保护与 CLI `Stop` 续跑兼容**：`handoff_gate.py` 在收口期禁止新发起 `run_workflow`，将 `post_message`、`swarm_wait` 计入 `drain` 期收尾预算并在 `freeze` 期阻断，补齐 `read_board`、`read_url_content`、`list_resources`、`read_resource` 只读放行及 `view_file` 读图限流；双端 `freeze` 期对 `git branch` 改用纯查询参数白名单，阻断 `--unset-upstream`、修改上游跟踪、建删改移分支等写操作；`reply_repeat_guard.py` 同时识别桌面端 `continuationActive` 与 CLI `v1.5.6+` 的 `continuation_active` 续跑标记。
- **`agy` CLI 超时识别与错误诊断防掩盖**：`cae_harness.py` 识别 `agy --print-timeout` 退出码 `3`、`[agy] print timeout after ...` 及 `AGY_ERROR` 超时行并统一归类为 `timeout`（外层进程预留 `+15s` 收尾宽限），拦截 `INVALID`／`WAITING`／`RUNNING` 非正常终态；即使子进程返回码为 `0` 或输出流中同时带有 `result` 事件，只要检出明确错误诊断仍判定失败，并在错误分类前剥离正常业务回执正文，避免报告内容提及“限流／超时”字样误触发额度冷却。
- **官方 v1 插件清单 `--strict` 满分校验与全文件脱敏回归**：`plugin.json` 补齐 `$schema`、`license` 与极简矢量图标 `assets/logo.svg`（通过 `agy plugin validate --strict` 零警告零提示校验）；脱敏回归扫描全仓所有普通文件并拒绝未知非 UTF-8 二进制文件，覆盖跨平台路径变体、用户名、主机名、会话 UUID、非示例邮箱、内网 IP、常见密钥凭据与具体 CPU/GPU 型号，并新增单端／双端三模式沙箱安装与钩子运行回归测试。

---

## [1.7.1] - 2026-11-14

- **默认满血最高思考挡位与按需分级省流**：`cae-harness` 在 `math`、`direction`、`code`、`review`、`audit`、`gate` 任务中默认启用所选旗舰专长模型（Codex `Astra`／最新代 `Sol`、反重力 `Carbon`）的单模型纯思考最高挡（`max`／`high`）及目录声明最大窗口（当前 Codex 目录为 `872,000` token），默认跳过会自动二次派生子代理的 `ultra` 挡；仅当用户明确或模糊提出降挡、省额度、省 token 时切换为分级省流模式。
- **安装器、钩子与编排器加固**：完善跨平台路径与符号链接／目录联接校验、Codex `hooks.json` 升级冲突检测、状态锁超时保护，并完成面向 AI-CAE 研发场景的全仓中文规范文档重构。

---

## [1.7.0] - 2026-11-14

- **额度冷却熔断与 `--fallback-on-quota` 本侧自动回退**：对侧返回配额耗尽时按执行侧（`windows`／`posix`）记录冷却截止时间并阻止盲目重试；开启 `--fallback-on-quota` 且工作区尚无文件改动时，自动切回发起方本侧匹配旗舰模型与最高可用挡位继续完成任务。
- **原生 `inherit` 子代理与跨端 CLI 选模解耦**：移除反重力原生只读子代理中的硬编码模型字段，使其默认跟随前端主会话模型与挡位，与 `cae-harness` 后台独立选模彻底解耦。

---

## [1.6.0] - 2026-11-13

- **五层上下文能力边界与受控实测基线**：完成并记录 Codex `GPT-6.1-Sol`（累积 `808,892` token 零压缩、6/6 口令全中）与反重力 CLI `1.5.8`（`241,625` token 全中、`290,028` token 触发自动检查点压缩）受控压力实测，建立五层上下文窗口管理规范。
- **WSL2 `<->` Windows 跨子系统互操作验证**：补齐 WSL 原生环境安装、路径映射与跨子系统只读镜像／共享盘读写实测验证。

---

## [1.5.0] - 2026-11-13

- **Codex 生命周期交接钩子与 `assess` 能力评估**：新增 Codex 侧 `SessionStart`／`UserPromptSubmit`／`PreToolUse`／`PostCompact`／`Stop` 原话增量归档与长会话交接提醒钩子（默认 `advisory` 模式），并在 `cae-harness` 中新增零写入 `assess` 选路与目录时效评估子命令。

---

## [1.4.0] - 2026-11-13

- **反重力两阶段长会话交接停机门禁（`handoff_gate.py`）**：基于紧凑轨迹遥测构建 `normal -> advisory -> drain -> freeze` 状态机，支持 R8 用户显式暂缓宽限窗口、`/goal` 终止标记核验与交接四要素完整性检查。
- **双层非套娃模型路由**：区分深度数理推理模型与工程编码主力模型，建立单模型纯思考挡位匹配逻辑。

---

## [1.3.0-rc1] - 2026-11-13

- **单端独立闭环与双端自举配置统一**：重构 `install.py` 支持 `--probe-apps`、`--app codex|antigravity|both|auto`、`--dry-run`、TOML 表结构无损合并及 `~/.codex/AGENTS.md` 末尾自定义环境段保留，确立仓库根目录 `AGENTS.md` 四步双向自举流程。

---

## [1.2.0] - 2026-11-13

- **初始开源发布**：发布 `user-guard` 反重力原生插件、`R0–R11` 工程与科学计算全局规则、压缩后用户原话自动回溯（`session_context_loader.py`／`user_words.py`）、R4 项目目录硬边界与防空转／防刷屏守卫（`status_guard.py`／`reply_repeat_guard.py`）、三个严格只读子代理及 `cae-harness` 跨端协作技能。

# `user-guard` 架构设计与生命周期钩子规范

本文面向需要了解底层实现、维护代码或排查边界行为的开发者，把“生命周期钩子（Lifecycle Hooks）”还原为直白的工程机制，并完整记录全仓代码模块分区、双端事件协议、状态机算法及受控实机测试数据。日常上手与协作流见[仓库总 README](../README.md)和[协作实践指南](workflow-guide.md)。

---

## 一、什么是生命周期钩子？全仓代码结构长什么样？

所谓“生命周期钩子”，并不是常驻后台的黑盒服务，而是**AI 客户端在“启动会话、发送请求前、调用读写／命令行工具前、结束当前回复前”这几个固定节点，自动同步调用的一组本地 Python 标准库脚本**。宿主把当前事件信息以单行 JSON 写入脚本标准输入（`stdin`），脚本在几毫秒内完成检查后，向标准输出（`stdout`）打印一行 JSON 告诉宿主“放行、弹窗请示用户、拒绝执行，还是顺带补入一段上下文提示”。

全仓核心代码按自顶向下顺序划分为三大块，不含任何第三方依赖：

### 1. 跨平台安装器 `install.py`（六大章节）

| 章节 | 职责范围 |
| --- | --- |
| 一、常量、路径与平台识别 | 定义托管文件清单、平台路径规则与版本常量 |
| 二、路径安全、原子读写与 TOML / JSON 配置处理 | 软链接／目录联接越界防护、基于标准库 `tomllib` 的保留注释无损键值编辑、原子替换写盘 |
| 三、双端客户端、WSL 互操作与 Python / Zotero 探测 | 零写入探测本侧与跨子系统（Windows `<->` WSL）客户端状态、可用 Python 3.11+ 解释器及 `zotero-mcp` |
| 四、反重力插件镜像、MCP 与默认模型同步 | 幂等同步 `~/.gemini/config/plugins/user-guard`、清理历史遗留脏文件并按需保留用户已选模型 |
| 五、Codex 规则、只读子代理、钩子与沙箱配置同步 | 同步 `~/.codex/AGENTS.md`（保留自定义 `## 工具与本机` 尾段）、`agents/*.toml`、`hooks.json` 与 `workspace-write` 沙箱基线 |
| 六、CLI 参数、预演与主安装流程 | 处理 `--probe-apps`、`--dry-run`、`--app` 等命令行入口与端到端自检 |

### 2. 生命周期守护脚本 `template/user-guard/hooks/*.py`（5 个模块）

| 脚本文件 | 自顶向下代码分区 | 核心职责 |
| --- | --- | --- |
| `user_words.py`（6 节） | ① 常量与阈值 -> ② UTF-8 标准流、数据目录与原子状态事务 -> ③ 用户正文清洗与 Markdown 归档格式 -> ④ Antigravity JSONL 解析与截断正文回查 -> ⑤ Codex 路径、主会话身份与分段发现 -> ⑥ Codex 来源合并、SHA-256 完整性校验与归档发布 | 双端共用的底层基础设施与用户原话提取归档库 |
| `session_context_loader.py`（7 节） | ① 常量 -> ② 目录与压缩状态读写 -> ③ 增量轨迹解析、遥测与迟到控制指令对账 -> ④ 原话内联摘要与字符预算 -> ⑤ Antigravity 恢复流程 -> ⑥ Codex 恢复协议适配 -> ⑦ 命令行主入口 | 上下文压缩或续接后，自动回溯并注入用户历史原话与归档路径 |
| `status_guard.py`（7 节） | ① 常量 -> ② 路径与轮询状态读写 -> ③ 证据查询解析、文件复合指纹与防空转检测 -> ④ 跨系统路径归一化与目录边界 -> ⑤ 内联 Python / 动态工作流提取与 AST 写入目标分析 -> ⑥ Shell 子句解包、写入路径与审批响应 -> ⑦ `PreToolUse` 调度与主入口 | 拦截项目目录外未授权写删，阻断对未变化算例日志的原地空转轮询 |
| `reply_repeat_guard.py`（6 节） | ① 常量 -> ② 防重复状态读写 -> ③ 尾部窗口、逆序回合定位与截断回复回查 -> ④ 短等待识别、数值标记与相邻回复相似度 -> ⑤ `Stop` 交接终审与每回合一次防重复提醒 -> ⑥ 命令行主入口 | 放行含迭代步数／残差推进的正常汇报，拦截纯空等刷屏并联动长会话收口终审 |
| `handoff_gate.py`（7 节） | ① 常量与阈值 -> ② 会话状态、路径与图片请求登记 -> ③ Shell 分句、包装器与命令副作用分类 -> ④ 交接文档快照、基线与四要素结构校验 -> ⑤ Antigravity 遥测、阶段迁移与工具门禁 -> ⑥ Codex 压缩事件对账与独立协议适配 -> ⑦ 命令行主入口 | 长会话分级预警、局部排空收尾（`drain`）、冻结保护（`freeze`）与结构化交接校验 |

### 3. 跨端无头协作编排器 `cae_harness.py`（七大章节）

| 章节 | 职责范围 |
| --- | --- |
| ① 全局常量、异常与回执 JSON Schema | 统一定义退出码、任务类型与结构化回执契约 |
| ② 基础配置、跨平台路径与 WSL 互操作工具 | 处理 Windows `<->` WSL 路径映射、纯 Linux 路径只读临时镜像与环境隔离 |
| ③ 模型目录、语义线索、挡位策略与执行画像 | 独立解析双端模型目录，落实“默认满血旗舰 + `max` 最高挡 + 最大声明窗口，按需才切分级省流” |
| ④ 额度冷却持久化、熔断门禁与回退资格 | 按运行侧记录 `quota_exhausted` 冷却期，发包前预检拦截，并在工作区零改动时执行 `--fallback-on-quota` 本侧接管 |
| ⑤ 进程树监督、JSON 流解析、App-Server RPC 与无头执行 | 管理 `codex exec` 与 `agy` 无头子进程生命周期、超时强杀进程树与流式输出解析 |
| ⑥ 工作区快照、分片审计规划与双盲归并 | 单次 `lstat` 对比任务前后文件哈希（拦截／跳过 FIFO、socket 等非常规特殊文件）防止 `--write-scope` 越界写入，按输入预算编排 `single-pass` 或 `sharded` 审计 |
| ⑦ 物理门禁循环与五大 CLI 子命令入口 | 拦截空白验收命令，驱动 `gate` 多轮排错验证闭环并显式标注 `verification_executed`，暴露 `probe`、`assess`（纯只读零写删）、`task`、`audit`、`gate` 五个命令 |

---

## 二、双端事件挂载与运行态目录隔离

### 1. 反重力侧插件协议（`template/user-guard/hooks.json`）

反重力从插件根目录的 `hooks.json` 读取同步命令配置，通过 `stdin` 传入 **camelCase** JSON，读取 `stdout` 单行 JSON 响应（协议说明见[反重力钩子文档](https://antigravity.google/docs/hooks)）。宿主运行时自动注入两个环境变量：
- `PLUGIN_ROOT`：插件只读代码目录（如 `~/.gemini/config/plugins/user-guard`）；
- `PLUGIN_DATA`：插件专属运行数据目录（如 `~/.gemini/antigravity/plugin_data/user-guard`）。

所有会话游标状态（`state/`）与诊断日志（`logs/`）均只写入 `PLUGIN_DATA`，保持插件代码目录干净无污染。

```json
{
  "user-guard-lifecycle": {
    "SessionStart": [
      {
        "type": "command",
        "command": "python3 -X utf8 -I -B ./hooks/session_context_loader.py --event SessionStart",
        "timeout": 30
      }
    ],
    "PreInvocation": [
      {
        "type": "command",
        "command": "python3 -X utf8 -I -B ./hooks/session_context_loader.py --event PreInvocation",
        "timeout": 30
      }
    ],
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 -X utf8 -I -B ./hooks/status_guard.py",
            "timeout": 15
          }
        ]
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "python3 -X utf8 -I -B ./hooks/reply_repeat_guard.py",
        "timeout": 15
      }
    ]
  }
}
```

### 2. Codex 侧钩子协议（`template/codex/hooks.json`）

安装器将原话恢复与交接脚本同步至 `~/.codex/hooks/`，并在 `~/.codex/hooks.json` 中挂载五类生命周期事件（协议说明见[Codex 钩子文档](https://learn.chatgpt.com/docs/hooks)）：`SessionStart`（匹配 `^(compact|resume)$`）、`PostCompact`、`UserPromptSubmit`、`PreToolUse` 与 `Stop`。二次升级时，若托管钩子实质配置未变，安装器保持原 `hooks.json` 字节完全不变，避免破坏客户端信任哈希。

> **跨平台 Python 解释器自动重写**：`install.py` 部署时会自动检测当前系统 `PATH` 中可用的 Python 3.11+（Windows 探测 `python.exe` 或 `py.exe -3`，WSL2／Linux 优先探测 `python3`），将模板中的命令前缀重写为本机有效命令，并将旧版遗留的 `-I -B` 启动参数统一升级至 `-X utf8 -I -B`；若未找到合规解释器则直接中止报错，绝不写入失效钩子。

---

## 三、四大核心守护机制的工程细节

### 1. 压缩与续接后的用户原话无损回溯（`session_context_loader.py` + `user_words.py`）

长对话触发上下文压缩后，早期聊天记录会被折叠为简短摘要，极易丢失初始边界条件、容差判据或中途纠正过的符号约定。

- **反重力增量字节游标（`<convId>_compaction.json`）**：
  - 每次 `PreInvocation` 优先定位紧凑轨迹文件 `transcript.jsonl`，只从上次记录的字节偏移量 `scannedByteOffset` 向后扫描新增字节（$O(\Delta\text{bytes})$ 开销），只在遇到带 `truncated_fields` 的用户步骤时按步骤号回查同目录 `transcript_full.jsonl` 补全原文。
  - **常规未压缩轮次**：仅更新字节游标，直接返回 `{"injectSteps": []}`，零额外上下文开销。
  - **检测到新 `"type": "CHECKPOINT"` 后的首轮**（`latestCheckpointStep > lastInjectedCheckpointStep` 且存在真实用户输入 `hasExplicitUserInput`）：提取全部 `<USER_REQUEST>...</USER_REQUEST>` 与可关联的多选回答，原子写入 `<PLUGIN_DATA>/state/<convId>_user_words.md`，并在预算上限内（总长 `8,000` 字符、单条 `1,200` 字符；超长时优先保留首轮立项与最近若干轮指令）构造带归档文件路径的 `systemMessage` 注入模型上下文，提示按 `R9` 核对。即使交接状态文件意外损坏，异常也会被独立捕获记入日志，绝不阻断原话恢复主流程。
  - **显式恢复与缺口自动补齐**：即便轨迹中尚未写入新 `CHECKPOINT`，只要收到新的显式 `compact`／`compaction` 触发信号或首次恢复会话（`resume`），同样会执行原话回溯；若前次因完整轨迹尚未落盘留下待补全标记（`pendingArchive`），脚本会在完整轨迹文件更新或间隔满 `30s` 后自动重试补齐归档。
- **Codex 跨分段 Rollout 合并归档（`--client codex`）**：
  - 同一逻辑会话在多次恢复或归档后可能横跨 `sessions/` 与 `archived_sessions/` 下的多个 `rollout-*.jsonl` 分段。脚本先校验 `session_meta`、会话 ID 与子代理标记（过滤后台子代理会话），再按消息角色与来源（provenance）剥离系统自动注入的 `# AGENTS.md instructions` 等环境块，完整保留人类真实输入（即便用户在正文中讨论了 `AGENTS.md` 字面量也不会误删）。
  - 在文件锁保护下成对写入 Markdown 原话归档与 `user-guard-codex-v2` SHA-256 校验清单；历史分段缺失或哈希不符时，保留已恢复内容并在归档中明确标注缺口。

### 2. 项目目录硬边界与算例日志防空转门禁（`status_guard.py`）

#### （1）项目外写删静态拦截（落实 `R4`）
脚本从 `workspacePaths`（缺省回退 `cwd`）解析当前项目根目录，审查已登记的 7 类跨宿主表面文件写删工具（`write_file`、`edit_file`、`write_to_file`、`replace_file_content`、`multi_replace_file_content`、`sed_file`、`notebook_edit`）、`run_command` 命令及 `v2.32.0+` `run_workflow` 动态工作流：
- **合法放行白名单**：当前项目根目录（自动等价映射 Windows `<盘符>:/...` 与 WSL `/mnt/<盘符>/...`）、经真实物理路径解析校验的操作系统标准临时目录（`%TEMP%`、`/tmp`，防软链接越界逃逸）、宿主会话记忆／轨迹／制品目录及标准 `.cache` 缓存区。
- **Python AST、工作流预审与 Shell 子句解包分析**：对 `python -c`、heredoc 内联脚本及 `run_workflow` 的 `Script`／`ScriptPath` 使用 Python 3.11+ 标准库 `ast` 按源码求值顺序识别写删目标与 `call_tool`／`agent` 越界路径（同时覆盖位置参数、显式关键字参数如 `dst=`／`target=`／`file=` 及 `**kwargs` 字典解包，涵盖 `Path.home()`、`open(..., 'w'|'a'|'x'|'+')`、`.write_text`、`os.remove`、`os.rename`、`shutil.copy*`／`move`／`rmtree`、`np.save*` 等），并校验 `run_workflow` 的外层 `Cwd`；对 Shell 命令先剥离 `wsl`、`bash -lc`、`powershell -Command` 等外层包装，再按子句识别输出重定向、`git worktree add`、安装脚本及 `sed -i`／`perl -i` 目标文件（自动忽略引号内的 `>` 符号与脚本表达式参数，避免误伤只读检索）。
- **动态路径与边界兜底**：AST 扫描按求值顺序追踪 `os.chdir` 相对目录切换、条件分支的所有候选路径，并在每次函数调用处结合实参重新核验；凡遇到运行期动态拼接路径、无法静态确认为只读的显式 `open` 模式、动态工具名或含未知解包（`**kwargs`）的写改参数，一律按潜在越界提请审批（无头子任务直接拦截）。对 `run_workflow` 的 `ScriptPath`，若文件缺失、不是普通文件、不可读、超过 `1 MiB` 或存在语法错误，同样不予静默放行；扫描 `install.py` 调用时会逐一核查所有重复传入的目标目录参数。
- **交互与无头分级响应**：前端交互主会话返回 `{"decision": "force_ask", "reason": "..."}` 弹窗请用户审批；`cae-harness` 启动的无头子进程（携带环境变量 `CAE_HARNESS_DEPTH=1`）对越界写删直接返回 `{"decision": "deny", "reason": "..."}`。

#### （2）算例日志复合指纹防空转轮询
仅对算例与后台任务输出文件启用指纹比对——包括 `.log`、`.out`、`.err`、`.output` 后缀文件，`.system_generated/tasks|steps/` 目录下的宿主任务日志，以及严格按整文件名匹配的典型输出文件（如 `journal*.jsonl`、`output.<ext>`、`task_output*`、`result(s)[.txt|.json|.jsonl|.dat|.csv]`、`task.json|.txt|.status`、`wf_*.json`，主文件名中不含额外点号分段，防止误伤 `status.json` 或 `test_result.json` 等业务文件）。普通源码与常规配置文件不受限制，可随时反复精读：
- 改变查询行号范围或切换命令方式（如先 `grep` 定位再 `tail` 查看末尾）正常放行；只有以相同参数重复读取且文件内容分毫未变时才拒绝空转。
- Windows 宿主机通过 `wsl`、`ssh` 或 `docker` 读取宿主机无法直接访问的子系统／容器内部路径时自动放行。
- 超大算例输出采用头尾分块哈希避免阻塞 I/O：
  $$\text{Fingerprint} = \begin{cases} (\text{mtime\_ns},\ \text{size},\ \text{SHA256}_{\text{full}}), & \text{size} \le 4\ \text{MiB} \\ (\text{mtime\_ns},\ \text{size},\ \text{SHA256}_{\text{head 1MiB} \parallel \text{tail 1MiB}}), & \text{size} > 4\ \text{MiB} \end{cases}$$

### 3. 防空等刷屏与数值迭代进展放行守卫（`reply_repeat_guard.py`）

- **防死循环双保险**：若 `continuationActive == true` 或 CLI `v1.5.6+` 的 `continuation_active == true`（当前已处于钩子触发的续跑轮），或终止原因属于运行错误，无条件返回 `allow`；同一用户回合最多提醒 1 次。
- **识别真实数值推进**：从 `transcript.jsonl` 尾部 `256 KiB` 窗口逆序定位最新回合回复（单条超长交接尾行或超长回合自动有界向前追溯完整行首）。对字数 `<= 300`、包含等待词且不含请示提问的短回复剥离时间戳（兼容紧邻汉字的无空格时间戳）并提取保留数字的归一化特征：只要迭代步数、残差量级或推进时间等数字发生了真实变化（如从 `第 100 步，残差 1.2e-3` 更新为 `第 500 步，残差 4.8e-6`），立即判定为有效进展正常放行；仅在连续 3 次回复同质且毫无数值推进时返回 `continue` 提醒静默等待后台任务完成通知。

### 4. 双端长会话收口与交接状态机（`handoff_gate.py`）

当单次会话经历十余次压缩或累积了数兆字节轨迹与大量图片后，上下文信噪比显著下降，继续派发新任务极易出错。

- **反重力四态状态机（`normal -> advisory -> drain -> freeze`）**：
  1. **Stage-1 预警（`advisory`，仅提醒不拦截）**：压缩达 `11` 次、紧凑轨迹达 `4 MiB`、压缩 `>= 6` 次且近 3 段平均间隔 `<= 90` 步、读图达 `35` 次／`50 MiB`，或压缩 `>= 6` 次且媒体步数达 `60`。
  2. **Stage-2 排空收尾（`drain`）**：收到宿主真实历史截断信号、用户明确下达交接指令、压缩 `>= 16` 次、压缩 `>= 10` 次且近 3 段平均间隔 `<= 75` 步、紧凑轨迹 `>= 6 MiB`、读图 `>= 65` 次／`100 MiB`，或压缩 `>= 6` 次且媒体步数 `>= 160`（若仅在用户消息中检出疑似转述的界面截断提示词，还须同时满足至少一项长会话遥测指标——如压缩 `>= 2` 次、步骤号 `>= 120`、轨迹 `>= 256 KiB` 或读图／媒体步数达标——以防新会话讨论截断机制时误触发收口）。
  3. **收口门禁与宽限规则**：进入 `drain` 后立即禁止新派生子代理、`run_workflow` 动态工作流、新启动 `cae_harness.py`、`git push`（含 `( ... )` 子壳分组与嵌套包装命令）、后台守护进程与图片读取；保留最多 `4` 次模型调用与 `8` 次局部收尾动作预算，耗尽后转入 `freeze`。进入 `freeze` 后，仅放行只读核查、项目根目录内的交接文档、当前会话制品目录（`brain/<convId>/` 下的普通 `.md/.txt/.json` 交接稿，不含内部系统目录、上传目录与原话归档）、本地 `git add`/`git commit` 及后台任务查询；`send_message`、`post_message` 与 `swarm_wait` 在 `drain` 期每次消耗 1 次收尾预算，进入 `freeze` 后一律阻断。若用户按 `R8` 明确要求“先别交接，继续当前对话”，则进入 `overridden` 宽限期（延后 `3` 次压缩与 `120` 步）。`Stop` 阶段会自动核验本轮是否已更新结构完整的交接文档，或在最终回复中写齐交接四要素（已验证进展、Git 状态、未完成事项，以及正文不少于 `15` 个字符的【新对话提示词】——支持围栏代码块、标题冒号同行或紧随标题的独立非引用段落）；若启用了 `/goal`，回复末行须单独输出 `<!-- GOAL_CANCELLED -->`。
- **Codex 适配策略（`--client codex`）**：
  - 默认运行 `advisory` 模式（压缩阈值 `0`），仅统计压缩次数并注入提醒，不阻断工具执行或回合结束。
  - 显式启用 `enforce` 且达到设定阈值或收到明确交接指令时，给予 `4` 次局部收尾动作排空预算后转入 `freeze`（仅放行只读命令、项目内交接文档 `apply_patch` 与本地提交），并在 `Stop` 阶段做最多 1 次交接要素补正校验后安全停机。

---

## 四、跨平台 UTF-8 管道与并发文件锁规约

1. **标准输入输出全程 UTF-8 编码**：所有钩子统一带 `-X utf8 -I -B` 启动（隔离用户目录外部包干扰且不生成 `.pyc` 缓存），入口处显式调用 `reconfigure(encoding="utf-8", errors="replace")`，彻底避免 Windows 默认代码页乱码与 Linux 最小容器环境 `LANG=C` 下的中文编码异常。
2. **原子写盘与独立文件锁域**：所有状态 JSON 与 Markdown 归档均先写入同目录临时文件，执行 `flush()` + `os.fsync()` 后通过 `os.replace()` 原子覆盖（针对 Windows 文件句柄瞬态占用内置短退避重试）。多进程并发采用操作系统原生文件锁（Windows `msvcrt`／POSIX `fcntl`），且 Codex 原话归档与交接状态采用独立锁文件，互不阻塞。

---

## 五、受控实机验收记录（2026-11-13）

以下数据均在独立临时目录中完成受控实测，原始日志与回执留存本地，公开文档仅记录脱敏后的定量结果。

### 1. 双端生命周期钩子与跨端无头执行实测

- **Codex CLI `0.167.0-alpha.4.1`（Windows）**：
  - 默认 `advisory` 模式下五类事件全部真实触发，手动压缩与低阈值自动压缩计数准确，跨进程 `resume` 不重复累加；
  - 隔离进程开启 `enforce` 模式后，准确拦截 `drain` 期远端推送与 `freeze` 期普通源码补丁，正常放行 `4` 次局部收尾动作与项目内交接文档 `apply_patch`，`Stop` 阶段完整覆盖“补正一次后放行”与“未补齐安全停机”两条路径。
- **反重力 CLI `1.5.8`（Windows 与 WSL `Ubuntu-24.04`）**：
  - 四类生命周期事件（`SessionStart`、`PreInvocation`、`PreToolUse`、`Stop`）在双平台均原生触发；明确交接指令进入 `drain`、完成四要素交接后 `Stop` 正常放行；
  - WSL 原生 CLI 完成登录并复用交互环境网络配置后，`cae-harness` 只读审查与 `Hydrogen (High)` 代码写入任务均以纯 Linux 原生链路（`side=posix`、`windows_interop=false`）一次通过固定体积三单元周期通量差的 9 组精确分数验收（耗时 `49.64s`）；
  - `Orchestrator (Carbon)` 首次跨端调用因上游流式 `unexpected EOF` 自动重试叠加默认 `300s` 外层时限超时，将 `--timeout` 调至 `900s` 后在新临时目录以 `342.07s` 一次通过同一通量守恒验收。

### 2. 上下文窗口容量与自动检查点压缩边界压力测试

测试采用合成随机口令语料，禁止模型调用任何工具或读取磁盘归档，由总控逐一核对分布在首、中、尾的 6 处随机值：

| 测试对象与场景 | 实测输入规模与宿主行为 | 提取命中率与工程结论 |
| --- | --- | --- |
| Codex `GPT-6.1-Sol`（分轮累积，同轮提取） | 单次请求输入达 `808,892` token（占宿主 `828,400` 有效窗口的 **97.65%**），未触发压缩、无工具调用 | **6/6 全部精确命中**，证实 `872,000` 声明最大窗口下的超长上下文同轮检索真实可用 |
| Codex 单条消息 RPC 上限探针 | 单条 `3,444,360` 字符消息被 `turn/start` 拒绝，服务端返回上限 `1,048,576` 字符 | 属客户端单次 RPC 载荷限制；超长源码或日志应让模型在工作目录内按文件读取或分轮送入 |
| 反重力 CLI `1.5.8` `Hydrogen`（单条超大输入探针） | 单条 44～66 万字节消息在轨迹与模型回复中出现入口截断标记 | CLI 无头单条提示词不宜过长，大体积材料应走文件读取或分块送入 |
| 反重力 CLI `1.5.8` `Hydrogen`（分块累积与检查点触发） | 每轮约 13 万 UTF-8 字节无截断：累积至 `241,625` token 时未压缩，**6/6 全部精确命中**；继续追加至 `290,028` token 时触发自动检查点压缩，压缩后输入降至 `14,063` token | 自动检查点压缩后早期散落的细节无法单凭压缩摘要还原（0/6），直接验证了 **`user-guard` 在检查点后从磁盘归档恢复用户原话**以及**把大体积源码分流给独立窗口原生子代理或 Codex `872k` 窗口**的工程必要性 |

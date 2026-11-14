---
name: cae-harness
description: >-
  Delegate bounded engineering and research tasks between Codex and Antigravity.
  Use for independent cross-platform reviews, large code audit planning, or
  delegated code changes with an explicit verification command. Assess local
  CLI and model metadata before choosing a backend for an independent task.
---

# 反重力与 Codex 双向协作技能（`cae-harness`）

日常科研与数值求解器开发**默认由反重力总控**（负责全局统筹、多篇文献对照、跨模块梳理、方向把关、中文技术写作与最终验收）；双端均可用时，凡涉及**偏微分方程推导、高阶数值格式分析（如 DGSEM／WENO 通量、雅可比矩阵与守恒性证明）、核心求解器编码、深层边界审查及带验证命令的门禁闭环**，**优先通过本技能发起 Codex 无头工作流攻坚**（发挥其深度推理、高保持率长上下文与工程沙箱优势）；在阶段收口或发版前，按全量文件由 Codex 完成代码回归检查、Debug 排错与优化闭环，由反重力完成文档润色、冗余清淤与隐私脱敏把关。若用户指定纯代码项目由 Codex 总控，反重力则配合承担方案复核、技术润色与独立验收。所有跨端调用结果均返回发起方总控，经亲自核对原文件行号与运行证据后再整合交付。

## 职责边界：Codex 跨端攻坚优先与本侧原生工具解耦

本技能仅管理通过官方 `codex` 与 `agy` CLI 启动的无头独立子进程：
1. **Codex 跨端攻坚优先，不干预本侧原生只读工具**：硬核推导、核心编码、深度审查与 `--verify-cmd` 门禁闭环优先通过本技能交由 Codex 端执行；反重力主会话通过 `invoke_subagent` 派生的 `readonly-reader`、`readonly-auditor`、`readonly-collaborator` 始终采用默认 `inherit`（直接继承当前前端主会话模型与挡位并各自拥有独立大上下文窗口），而反重力原生 `run_workflow` 仅用于同端 10 个以上同构模块或算例目录的轻量纯只读盘点（启动前由 `status_guard.py` 执行 R4 边界预审）。
2. **不受前端主控模型切换影响**：无论用户在反重力或 Codex 前端界面选了哪个模型，`cae-harness` 始终独立读取实际运行侧的模型目录，按任务性质匹配专长模型、思考挡位与最大上下文窗口。

## 选模、最高思考挡位与最长上下文规则

1. **默认只选主力旗舰专长模型（不擅自降级轻量模型）**：
   - **调用 Codex**（`--engine codex:default`，或显式传 `codex:astra`、`codex:sol`）：
     - 数学公式推导（`math`）、方法与路线论证（`direction`）及含高复杂度数理线索的审查，自动优选深度推理旗舰 **`Astra`**（如 `gpt-6-astra`）；
     - 核心代码实现（`code`）、常规代码审查（`review`）、大输入分片／双盲审计（`audit`）与命令门禁验收（`gate`），自动优选最新代工程旗舰 **`Sol`**（如 `gpt-6.1-sol`，多代并存时自动解析最新代）。
   - **调用反重力 CLI**（`--engine agy:default`，或显式传 `agy:carbon` 等）：默认选当前最高代主力单模型 **`Carbon`**（如 `gemini-4-carbon-high`）。
   - 自动选路默认排除固定复合模式（如 `Orchestrator`）、自动路由与长周期稀缺配额第三方模型；确需使用时须通过 `--engine` 显式指定。
2. **默认满血最高挡位 + 按需分级省流（默认禁套娃 `ultra`）**：
   - **默认满血模式**：除纯文字润色（`polish` 短篇用 `medium`、长篇用 `high`，防止过度发挥造成 R5 无声放大）外，所有 `math`、`direction`、`code`、`review`、`audit`、`gate` 任务默认一律启用所选旗舰模型的**单模型纯思考最高挡**（如 Codex `max`、反重力 `Carbon` `high`）。后台默认跳过会自动二次派生子代理的 `ultra` 挡（除非显式传 `--tier ultra`）。
   - **按需省流模式**：仅当显式传 `--tier`、配置 `CAE_DEFAULT_TIER`，或用户在任务描述中明确／模糊提出降挡、省额度、省 token（如“省点 token”“降档”“省额度”“轻量快速”）时，才切换为分级自适应——常规编码／审查降为 `high` 挡（门禁首轮失败时第 2 轮仍可升至 `max` 排错），表面拼写／格式微查启用轻量高速模型（如 `GPT-6-Luna`、`Gemini 4 Hydrogen`）配 `low` 挡。
3. **自动下发模型声明最大上下文窗口**：
   - 脚本读取最终选定模型在运行侧目录中的 `max_context_window`（如当前 Codex 目录声明的 `872,000` token），向 CLI 显式下发最大窗口参数；若目录无最大值声明则采用该模型默认窗口。若目标模型窗口未知且宿主存在不匹配的全局窗口覆盖，脚本会直接拦截并提示刷新模型目录，防止误套其他模型的窗口上限。

## 子命令实战指南

编排脚本位于 [scripts/cae_harness.py](scripts/cae_harness.py)。以下命令假设当前工作目录为技能目录（Windows 用 `python`，WSL2／Linux 用 `python3`）：

### 1. 环境探测（`probe`）与选路评估（`assess`）

```sh
python -B scripts/cae_harness.py probe
python -B scripts/cae_harness.py assess --caller antigravity
python -B scripts/cae_harness.py task --help
```

- `probe`：只读检查双端 CLI 可执行路径、登录态配置与本地模型缓存；附带 `--discover-models` 时调用 `agy models` 刷新反重力模型目录缓存（遇到官方模型代际切换，如 `2026-11-18` 下线 `Claude 4.6`／`GPT-OSS-120b` 并切换至 `Claude 5.5` 与 `Gemini 4` 系列后，可执行一次 `probe --discover-models` 同步本地目录缓存）。
- `assess`：零写入输出双端可用性、模型目录来源与采集时间、声明窗口与支持挡位、以及各任务类型的推荐执行画像。

### 2. 独立任务派发（`task`）

无头 CLI 子进程无法读取桌面聊天窗口上下文，派工任务单（`--task-file` 或 `--task`）须一次写清：**① 物理／数学假设、符号约定与最新裁定；② Git 基线与 `--target` 项目根目录；③ 必读源码或文献列表；④ 允许修改的文件白名单（`--write-scope`）；⑤ 交付格式或验收命令（`--verify-cmd`）**。

```sh
# 反重力调用 Codex 推导高阶离散格式／雅可比矩阵（自动选 Astra + max 最高挡 + 872k 窗口；额度耗尽时自动回退反重力本侧）
python -B scripts/cae_harness.py task --caller antigravity --engine codex:default --kind math --tier max --fallback-on-quota --target "<项目目录或单文件>" --task-file "<UTF-8任务单.md>"

# 反重力调用 Codex Sol 实现求解器模块并同步运行守恒律／收敛阶验收测试
python -B scripts/cae_harness.py task --caller antigravity --engine codex:sol --kind code --tier max --fallback-on-quota --target "<项目目录>" --task-file "<UTF-8任务单.md>" --write-scope "src/*.py" --verify-cmd "python -B -m unittest discover -s tests"

# Codex 调用反重力润色技术文档（先 --dry-run 预演执行画像）
python -B scripts/cae_harness.py task --caller codex --engine agy:default --kind polish --tier medium --target "<项目目录>" --task-file "<UTF-8任务单.md>" --dry-run
```

| `--kind` 类型 | 读写权限与结构化回执内容 |
| --- | --- |
| `math` | 只读读取输入材料，在回执中交付控制方程假设、完整推导步骤、适用范围与未验证项 |
| `direction` | 只读开展方案与数值格式相容性论证，返回依据对比与待拍板项 |
| `review` | 只读逐行审查代码或公式，返回问题定位、精确行号与原文证据 |
| `polish` | 只读读取原稿，在回执 `content` 中直接交付去套话、防无声放大的润色稿 |
| `code` | 仅在 `--write-scope` 授权范围内修改文件，推荐附 `--verify-cmd` 执行自动化验收（回执显式标注 `verification_executed`） |

写入范围必须位于 `--target` 目录之内（若 `--target` 指向单个文件且未传 `--write-scope`，默认仅授权修改该文件）。任务前后自动对比全目录快照（非常规特殊文件如 FIFO／socket 直接拒绝防阻塞），任何白名单外的写入或删除均判定为 `scope_violation`。`--verify-cmd` 严禁传入空串或纯空白字符；若 `code` 任务未传 `--verify-cmd`，回执顶层会显式置 `"verification_executed": false` 并附未验证提示，提醒总控跑完验证后再验收。调用 `Astra (max)` 执行 `math`／`direction` 任务时，输入应聚焦预提炼的控制方程或架构边界（必要时将 `--timeout` 设为 `900–1200`）；多文件常规代码对账优先选用 `review`（自动路由至最新代 `Sol`）。

### 3. 大型求解器审计（`audit`）与物理／测试门禁（`gate`）

```sh
# 双端交叉／自动分片审计大型代码库
python -B scripts/cae_harness.py audit --caller antigravity --target "<项目目录>" --left codex:default --right agy:default --strategy auto --fallback-on-quota

# 编码 + 验证命令闭环门禁（失败时在限定轮次内附报错日志排错，严禁放宽容差）
python -B scripts/cae_harness.py gate --caller antigravity --worker codex:sol --tier max --fallback-on-quota --target "<项目目录>" --task-file "<UTF-8任务单.md>" --write-scope "src/*.py" --verify-cmd "<验收命令>"
```

- `audit`：扣除系统提示词、文件清单与 JSON Schema 开销后，在预算内采用单次全量审查（`single-pass`），超预算时按目录内聚性自动分片（`sharded`），并核对计划文件、自报已读文件与未读原因（即使事后快照遇到权限异常，仍保留已完成的审计单元与发现明细并标注 `scope_verified: false`）。横跨多个源文件的强耦合算子（如 DGSEM 体积分、界面数值通量与弯曲网格离散度量恒等式）须由总控做跨片联合核对。
- `gate`：编码落盘后自动在宿主机运行非空 `--verify-cmd`（回执顶层及每轮历史均记录 `verification_executed`，验收命令自身生成的临时测试产物单独归入 `verification_changed_files`）；未通过时在 `--max-rounds` 轮次内携带真实报错继续修复，严禁放宽 `rtol`／`atol`、弱化守恒判据或删改测试断言。

## 额度熔断回退与跨平台运行边界

1. **额度熔断与 `--fallback-on-quota` 自动接管**：
   - 对侧返回用量／配额耗尽时，脚本立即熔断同批次后续请求，返回 `quota_exhausted`（退出码 `5`）并按执行侧（`windows`／`posix`）持久化冷却截止时间；冷却期内新请求在发包前直接拦截（`preflight_blocked: true`）。
   - **推荐附带 `--fallback-on-quota`**：若对侧在预检或启动阶段触发额度熔断，且工作区文件尚无任何改动，脚本自动切回发起方本侧（如反重力 `agy:default`）匹配的旗舰模型、最高可用挡位与最大可用上下文完成任务，并在回执 `quota_fallback` 字段记录切换原因与冷却时间。若中途已改动过工作区文件，绝不自动重跑写入，原样返回熔断状态交总控裁处。
   - 未开启 `--fallback-on-quota` 时，由总控向用户说明冷却状态，请用户选择 **① 改由本侧接管完成**，或 **② 额度恢复后加 `--retry-quota` 重试**。
2. **Windows 与 WSL 会话隔离及沙箱边界**：
   - 反重力调 Codex 采用 `codex exec --ephemeral`，Codex 调反重力采用独立无头目录 `~/.gemini/antigravity-cli`，均不污染桌面客户端的聊天历史列表。
   - **Windows 沙箱反向调用提示**：反重力主会话直接调 `codex` 可正常访问宿主机 `.codex` 目录；但若从 Windows 版 Codex 的受限沙箱（如 `workspace-write`／AppContainer）内部反向调 `agy.exe`，受限令牌会阻止 `agy.exe` 写入 `~/.gemini/antigravity-cli` 运行锁导致无响应超时——此时须在放行 `~/.gemini` 写权限的宿主通道中执行，并将 `--timeout` 设为 `300` 秒以上。
   - **WSL 互操作**：WSL 优先调用本侧已登录的原生 Linux CLI；未安装或未登录时可自动回退调用 Windows 侧 CLI（`/mnt/<盘符>` 共享盘支持直接读写；WSL 纯 Linux 路径支持自动建立临时镜像完成只读审查，代码写入任务须置于共享盘或使用已登录的 WSL 原生 CLI）。

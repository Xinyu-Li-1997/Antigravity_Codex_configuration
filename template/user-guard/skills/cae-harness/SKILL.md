---
name: cae-harness
description: >-
  Delegate bounded engineering and research tasks between Codex and Antigravity.
  Use for independent cross-platform reviews, large code audit planning, or
  delegated code changes with an explicit verification command. Assess local
  CLI and model metadata before choosing a backend for an independent task.
---

# 反重力与 Codex 双向协作技能（`cae-harness`）

日常科研与综合工程**默认以反重力总控**（主会话与原生只读子代理各自拥有独立大上下文窗口，负责全局推进、文献与架构梳理、方向把控和文字表达），遇到**数学公式推导、高阶数值格式分析与核心代码实现／验收**时，按需通过本技能调用 Codex 攻坚。若用户指定纯代码项目由 Codex 总控，反重力则配合承担方向复核、技术表述润色与独立验收。所有跨端任务结果均交回发起方总控，核实原始文件与运行证据后再整合交付。

本技能只管理通过 `codex` 与 `agy` 启动的独立无头 CLI 子任务，不改写前端交互主会话的模型与挡位，也不接管宿主原生的 `invoke_subagent` 或 `spawn_agent`。

## 选模、最高思考挡位与最长上下文规则

- **因地制宜匹配专长模型**：
  - 调用 **Codex**（`--engine codex:default`，或显式指定 `codex:astra`、`codex:sol`）：数学推导（`math`）、路线攻坚（`direction`）及高复杂度数理审查自动优选深度推理模型 **`Astra`**（如 `gpt-6-astra`）；代码实现（`code`）、大输入审计（`audit`）与门禁验收（`gate`）自动优选最新代工程主力 **`Sol`**（如 `gpt-6.1-sol`，多代并存时自动解析到最新代）。
  - 调用**反重力**（`--engine agy:default`，或显式指定 `agy:carbon` 等）：深度推理与核心工程优先选当前最高代主力单模型（如 `gemini-4-carbon-high`），表面快速检查选轻量高速模型（如 `Hydrogen` 低挡）。
  - 自动选路默认排除固定复合模式（如 `Orchestrator`）、自动路由与长周期稀缺配额第三方模型；只有通过 `--engine` 或任务单明确点名时才启用。
- **单模型最高思考挡位（防套娃裂变）**：
  - `math`、`direction`、含数学公式／守恒律／高阶格式／雅可比／牛顿法／并发根因的任务，以及 `gate` 验收失败后的排错轮，自动选用**该模型支持的单模型纯思考最高挡**（如 Codex 的 `max`、反重力 Carbon 的 `high` 或 Hydrogen 的 `xhigh`）。需要强制在普通代码任务上也拉满推理时，直接传 `--tier max`。
  - 后台默认跳过会自动二次派生子代理的 `ultra` 挡；确需使用时必须显式传 `--tier ultra`。常规编码、审查与长文润色默认用 `high`，短篇润色用 `medium`，表面格式／拼写检查用 `low`。
- **按所选模型自动拉满声明上下文窗口**：
  - 脚本根据实际运行侧和最终选定的模型读取其元数据，优先向 CLI 传入该模型自身声明的 `max_context_window`（如当前 Codex 目录声明的 `872,000` token），无最大值声明时使用该模型默认窗口；若目标模型窗口未知且宿主存在不匹配的全局窗口覆盖，脚本会直接拦截并提示刷新目录，避免误借其他模型的上限。

## 调用入口与能力探测

脚本位于 [scripts/cae_harness.py](scripts/cae_harness.py)。以下示例假设当前位于技能目录（Windows 用 `python`，WSL／Linux／macOS 用 `python3`）：

```sh
python -B scripts/cae_harness.py probe
python -B scripts/cae_harness.py assess --caller antigravity
python -B scripts/cae_harness.py task --help
```

- `probe`：默认只读检查 CLI 路径、本地配置与模型缓存；加 `--discover-models` 会调用 `agy models` 刷新反重力模型目录缓存。
- `assess`：只读输出双端可用性、模型目录来源与时效、声明窗口与挡位、各任务类型的推荐选路（不写任何配置或缓存）。

## 独立任务派发（`task`）

通过 `--caller antigravity` 或 `--caller codex` 指明发起方。无头子任务看不到桌面聊天历史，任务单必须写清用户最新定论、背景依据、工作目录、允许改动的文件范围与验收标准。

```sh
# 反重力调用 Codex 推导数学公式／离散格式（自动选 Astra + max 最高挡 + 872k 最大窗口，Codex 额度不足时自动切回反重力本侧完成）
python -B scripts/cae_harness.py task --caller antigravity --engine codex:default --kind math --tier max --fallback-on-quota --target "<项目目录或单文件>" --task-file "<UTF-8任务单>"

# 反重力调用 Codex Sol 用最高挡与最大窗口实现核心数值代码并跑验收命令
python -B scripts/cae_harness.py task --caller antigravity --engine codex:sol --kind code --tier max --fallback-on-quota --target "<项目目录>" --task-file "<UTF-8任务单>" --write-scope "src/*.py" --verify-cmd "python -B -m unittest discover -s tests"

# Codex 调用反重力做技术润色或方向复核（预演）
python -B scripts/cae_harness.py task --caller codex --engine agy:default --kind polish --tier medium --target "<项目目录>" --task-file "<UTF-8任务单>" --dry-run
```

| 任务类型 | 权限与交付方式 |
| --- | --- |
| `review` | 只读审查，在结构化回执中返回问题发现与行号依据 |
| `direction` | 只读方案与方法论证，返回物理／数值依据和待裁决事项 |
| `polish` | 只读读取原稿，在回执 `content` 中直接交付润色后文本 |
| `math` | 只读读取材料，在回执中交付完整推导步骤、前置假设与未验证项 |
| `code` | 在 `--write-scope` 授权范围内修改代码，可附 `--verify-cmd` 同步验收 |

写入任务必须落在 `--target` 项目目录内（单文件 `--target` 未传 `--write-scope` 时默认仅授权该文件），脚本执行前后会自动快照比对工作区文件，任何越界改动都会标记为 `scope_violation`。

## 大输入审计（`audit`）与门禁闭环（`gate`）

```sh
python -B scripts/cae_harness.py audit --caller antigravity --target "<项目目录>" --left codex:default --right agy:default --strategy auto --fallback-on-quota
python -B scripts/cae_harness.py gate --caller antigravity --worker codex:sol --tier max --fallback-on-quota --target "<项目目录>" --task "<修改任务>" --write-scope "src/*.py" --verify-cmd "<验收命令>"
```

- `audit`：根据最终选定模型的输入预算（扣除提示词、清单与 JSON Schema 开销并留余量）自动安排单次通读（`single-pass`）或按模块分片审查（`sharded`），分别核对计划文件、自报已读文件与未读原因。跨分片的强耦合机制（如 DGSEM 体积分、界面数值通量与度量恒等式）须由总控联合复核。
- `gate`：编码完成后自动执行 `--verify-cmd`（验收命令自身产生的产物单独记入 `verification_changed_files`），未通过时在 `--max-rounds` 限定轮次内附带报错日志升级排错；严禁放宽数值容差或删改断言。

## 额度熔断、本侧自动接管与跨平台注意事项

1. **额度熔断与 `--fallback-on-quota` 无缝回退**：
   - 当某一方返回用量／额度耗尽时，脚本立即熔断同批次后续调用，返回 `quota_exhausted`（退出码 `5`），并把冷却截止时间写入本侧缓存；冷却期内再次请求会在发包前直接拦截（`preflight_blocked: true`）。
   - **推荐在反重力调 Codex 时附带 `--fallback-on-quota`**：一旦 Codex 在预检或启动阶段触发额度熔断，且工作区尚无任何文件改动，脚本会在记录 Codex 冷却状态的同时，**自动切回发起方本侧（`agy:default`）匹配的模型、最高可用挡位与最大可用上下文**完成任务，并在回执 `quota_fallback` 字段写明切换原因与冷却信息（若执行中途已改过文件，则绝不自动重跑写入，原样返回熔断状态交总控处理）。
   - 未开启 `--fallback-on-quota` 时，总控应向用户说明冷却情况，由用户选择**① 改由本侧直接完成**，或 **② 充值／额度恢复后加 `--retry-quota` 重试**。
2. **Windows 与 WSL 运行边界**：
   - 反重力调 Codex 使用 `codex exec --ephemeral`，Codex 调反重力使用 `agy` 独立无头存储（`~/.gemini/antigravity-cli`），均不污染桌面侧边栏会话列表。
   - **Windows 沙箱提示**：反重力主会话直接调 `codex` 可正常访问宿主机 `.codex` 目录；反之若从 Windows 版 Codex 的受限沙箱（如 `workspace-write`／AppContainer）反向调 `agy.exe`，受限令牌会拦截 `agy.exe` 写入 `~/.gemini/antigravity-cli` 状态锁导致卡死超时——此时须在宿主提权／放行 `~/.gemini` 写权限下运行，并保持 `--timeout` 不少于 `300` 秒。
   - **WSL 互操作**：WSL 优先调用本侧已登录原生 CLI；未装或未登录时可自动回退调用 Windows 侧 CLI（`/mnt/<盘符>` 共享盘支持读写，纯 Linux 路径支持自动临时镜像只读审查，代码写入需走共享盘或 WSL 原生 CLI）。

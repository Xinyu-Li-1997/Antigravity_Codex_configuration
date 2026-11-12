---
name: cae-harness
description: >-
  Delegate bounded engineering and research tasks between Codex and Antigravity.
  Use for independent cross-platform reviews, large code audit planning, or
  delegated code changes with an explicit verification command. Supports review,
  direction, polish, math, and code tasks.
---

# Codex 与反重力双向协作

偏代码工程由 Codex 总控，反重力参与方向校正、技术表述润色和独立验收。偏科研主题由反重力总控，Codex 承担其中部分高质量编码和数学工作。任务结果交回实际发起方，由当前总控核实证据、整合成果和推进交付。

本技能的后端为 `agy` 与 `codex`。默认策略（`--tier auto`）在运行期动态解析对侧模型元数据，实行**“模型因地制宜按专长选路、思考挡位按任务性质分级”**：数理推导（`math`）与路线攻坚（`direction`）优先调度深度推理专长模型并启用**单模型纯思考最高挡**（`max`／`xhigh`，后台无头子任务默认跳过内部二次派生委派挡位 `ultra` 以防套娃裂变）；大窗口代码实现（`code`）、全量审计（`audit`）与门禁闭环（`gate`）调度大上下文工程主力模型（常规任务采用 `high` 挡，含高阶数值格式／守恒性／并发根因特征或门禁失败排错重试轮自动升至 `max` 挡）；短篇纯文字润色（`polish`）采用 `medium` 挡（长篇 `high` 挡）；单文件拼写／注释表面核查或显式省额度指令调度轻量高速模型（`low` 挡）；宿主内长周期稀缺配额池的内置第三方模型默认不纳入后台自动消耗，仅可通过 `--engine <后端>:<模型>` 或 `--tier ultra|max|xhigh|high|medium|low` 显式指定。跨平台调用启动独立进程，任务单应提供最新用户决定、输入文件、工作目录、允许改动范围和验收要求。`codex` 子任务启用 `--ephemeral` 无持久会话模式，`agy` 子任务写入独立的无头 CLI 存储而不污染桌面端或 IDE 侧边栏对话框及项目树；完整结构化回执（含实际选定模型、推理挡位与选挡依据）统一归还发起方主对话。

## 调用入口

脚本位于 [scripts/cae_harness.py](scripts/cae_harness.py)。下列示例从本技能目录运行。Windows 使用 `python`，WSL／Linux 使用 `python3`。

```sh
python -B scripts/cae_harness.py probe
python -B scripts/cae_harness.py task --help
```

`probe` 默认读取程序路径和本侧配置。`probe --discover-models` 会执行模型列表查询，按需要使用。程序发现、配置或缓存元数据、认证状态和实际调用验证分别判断。使用预算字段时同时核对其来源和验证状态。

## 独立任务

先按用户已确定的分工填写 `--caller codex` 或 `--caller antigravity`。`auto` 可读取 `CAE_HARNESS_CALLER`，识别失败时应显式填写发起方和后端。

```sh
python -B scripts/cae_harness.py task --caller codex --engine agy:default --kind direction --target "<项目目录>" --task-file "<UTF-8任务单>" --dry-run
python -B scripts/cae_harness.py task --caller antigravity --engine codex:default --kind math --target "<项目目录>" --task-file "<UTF-8任务单>"
```

| 任务类型 | 交付与权限 |
| --- | --- |
| `review` | 只读核查，返回发现和证据 |
| `direction` | 只读分析方法与方案，返回依据和待决定事项 |
| `polish` | 只读输入材料，在回执正文中提供修订文本 |
| `math` | 只读输入材料，在回执正文中提供推导、假设及未验证事项 |
| `code` | 在授权范围实现代码，可同时执行指定验收命令 |

反重力的一般只读任务使用 `readonly-collaborator`，专门的源码审计使用 `readonly-auditor`。使用这些角色前，核对运行侧 `user-guard` 插件已经包含对应角色。

写入任务明确列出 `--write-scope`，可重复指定。子任务一律受项目目录硬边界约束（`codex exec` 强制启用 `-s workspace-write -c approval_policy="never"`，`agy` 启用 `--sandbox` 并联动 `PreToolUse` 钩子硬阻断越界写入），严禁在 `--target` 目标项目目录之外的业务目录或全局配置中创建、修改或删除任何文件与目录（含严禁在父目录或外部目录创建 `.codex-worktrees` / `git worktree`；系统标准临时目录与运行时记忆缓存除外）。范围检查同时比较执行前后的工作区文件状态，越界会报告失败；异常后的文件差异由总控查阅处理，脚本保留实际变更供核对。

```sh
python -B scripts/cae_harness.py task --caller antigravity --engine codex:default --kind code --target "<项目目录>" --task-file "<UTF-8任务单>" --write-scope "src/*.py" --verify-cmd "python -B -m unittest discover -s tests"
```

## 大输入审计与验收循环

```sh
python -B scripts/cae_harness.py audit --caller antigravity --target "<项目目录>" --left codex:default --right agy:default --strategy auto --dry-run
python -B scripts/cae_harness.py gate --caller antigravity --worker codex:default --target "<项目目录>" --task "<修改任务>" --write-scope "src/*.py" --verify-cmd "<验收命令>"
```

`single-pass` 表示按估算预算把指定文件安排在一个任务中。旧 `whole-1m` 参数作为兼容别名处理。容量元数据、输入估算和分片安排分别记录，实际完整阅读及压缩行为需由执行证据确认。预算可用 `--codex-input-budget` 和 `--agy-input-budget` 调整。

读取计划与回执中的文件清单、跳过原因和跨分片限制。审计回执分别列出计划文件、子任务自报已读文件与未读原因，遗漏会返回部分完成。自报阅读范围仍需总控核对证据。强耦合模块需要联合判断时，由总控保留跨模块问题和依赖，避免把独立分片的结论扩大到未覆盖的组合。

`gate` 使用用户指定的命令验收。模型写入在执行验收命令前核对范围，验收命令产生的文件单独记录在 `verification_changed_files`。数值容差、守恒量、稳定性和收敛性判据应来自当前任务定义。

## 进度与结果

标准错误流提供生命周期事件，标准输出返回最终 JSON。父会话保留本次进程句柄以等待结果或取消本次任务，按回执状态处理成功、部分完成、无效输出、额度耗尽（`quota_exhausted`，退出码 `5`）、超时和失败。当某一方触发额度上限时，脚本会立即熔断同批次后续重试并将恢复参考时间记入本侧运行缓存；冷却期内再次触发调用时会在发起前直接拦截（`preflight_blocked: true`），此时当前总控必须明确提示用户并交由用户二选一：**① 先不发起跨端调用（改由当前总控在本侧直接完成）**，或 **② 已充值／额度恢复后附带 `--retry-quota` 重新发起**（成功后自动清除冷却记录）。审查结果由总控核对源文件或原始记录后采纳。

安装与 Windows／WSL 同步由仓库安装器负责。先在临时目标目录验证同一提交，再在当前任务合适的间隙更新用户目录。安装后的技能入口及后端程序由各侧运行环境解析。WSL 优先调用本侧已登录原生 CLI；当 WSL 缺少原生 `codex` 或原生 `agy` 未登录时，自动回退调用 Windows 侧已登录的 `codex.exe`／`agy.exe`：`/mnt/<盘符>` 共享目录支持只读与授权写入任务，WSL 纯 Linux 文件系统路径自动通过 Windows `%TEMP%` 临时只读镜像支持审计与只读协作，代码写入任务需在 `/mnt/<盘符>` 目录执行或在 WSL 登录原生 CLI。

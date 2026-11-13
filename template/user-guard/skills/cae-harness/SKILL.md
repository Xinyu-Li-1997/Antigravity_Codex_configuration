---
name: cae-harness
description: >-
  Delegate bounded engineering and research tasks between Codex and Antigravity.
  Use for independent cross-platform reviews, large code audit planning, or
  delegated code changes with an explicit verification command. Assess local
  CLI and model metadata before choosing a backend for an independent task.
---

# Codex 与反重力双向协作

偏代码工程由 Codex 总控，反重力参与方向校正、技术表述润色和独立验收。偏科研主题由反重力总控，Codex 承担其中部分高质量编码和数学工作。任务结果交回实际发起方，由当前总控核实证据、整合成果和推进交付。

前端交互遵循用户显式选定的模型和挡位。技能只负责通过 `agy` 和 `codex` 发起的 CLI 子任务。Codex 原生 `spawn_agent` 由总控按当前可用模型和挡位显式调度。

- 数理推导（`math`）、路线分析（`direction`）及复杂数值格式、守恒性或并发根因复核，优先使用适合深度推理的模型和可用的单模型最高挡（如 `max` 或 `xhigh`）。后台默认跳过用于二次自动派生的 `ultra`。
- 大窗口编码（`code`）、全量审计（`audit`）与指定命令验收（`gate`）优先选择适合大上下文的工程模型。常规编码、审查和长篇技术润色采用 `high`，短篇纯文字润色采用 `medium`，表面核查可选轻量模型和 `low`。
- 自动策略为 `--tier auto` 和 `<后端>:default`。默认排除固定复合模式、自动路由配置与占用长周期稀缺配额的内置第三方模型。可用 `--engine <后端>:<目录中的模型名或别名>` 和 `--tier ultra|max|xhigh|high|medium|low` 显式指定。任务单中的明确模型选择指令也可参与匹配，单纯讨论名称不构成选择；`ultra` 仍须使用结构化参数授权。

反重力 Orchestrator 是同一 agent 内的固定规划／实施复合模式，显式选择时不附加 `--effort`；Codex `ultra` 是自动委派模式，须显式用 `--tier ultra` 授权，任务正文仅讨论该词不构成授权。两者与本侧原生子代理、跨端 CLI 进程的关系见[总 README](https://github.com/Xinyu-Li-1997/Antigravity_Codex_configuration)。

任务单应提供用户最新定论、输入材料、工作目录、允许改动范围与验收要求。Codex 子任务使用 `--ephemeral`，反重力子任务使用独立的无头 CLI 存储。回执含选定模型、推理挡位和选挡依据，交回实际发起方。

## 调用入口

脚本位于 [scripts/cae_harness.py](scripts/cae_harness.py)。下列示例从本技能目录运行。Windows 使用 `python`，WSL／Linux 使用 `python3`。

```sh
python -B scripts/cae_harness.py probe
python -B scripts/cae_harness.py assess --caller codex
python -B scripts/cae_harness.py task --help
```

`probe` 默认仅读取程序路径、本侧配置与模型缓存；`probe --discover-models` 调用 `agy models` 查询反重力模型目录并刷新缓存。`assess` 只读输出 CLI 发现情况、目录来源与采集时效、声明的上下文容量与挡位、未知项及场景选路建议（加 `--discover-models` 仅用于本次评估，不写配置或缓存），不凭缓存宣称模型实际性能。认证、执行性能和任务适用性以受控运行的证据为准，未实测项标为未验证。使用预算字段时核对其来源、时间和验证状态。

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

写入任务用 `--write-scope` 列出授权文件，可重复指定。Codex 写入任务使用 `-s workspace-write -c approval_policy="never"`，反重力任务使用 `--sandbox`，并由已启用的 `PreToolUse` 钩子检查可识别的越界动作。除约定的临时目录和运行时缓存外，子任务的业务写入限于 `--target`。执行前后比较工作区文件状态，报告检测到的越界变更，由总控核对实际差异。

```sh
python -B scripts/cae_harness.py task --caller antigravity --engine codex:default --kind code --target "<项目目录>" --task-file "<UTF-8任务单>" --write-scope "src/*.py" --verify-cmd "python -B -m unittest discover -s tests"
```

## 大输入审计与验收循环

```sh
python -B scripts/cae_harness.py audit --caller antigravity --target "<项目目录>" --left codex:default --right agy:default --strategy auto --dry-run
python -B scripts/cae_harness.py gate --caller antigravity --worker codex:default --target "<项目目录>" --task "<修改任务>" --write-scope "src/*.py" --verify-cmd "<验收命令>"
```

先确认实际执行侧及最终模型，再按该模型自身声明的最大窗口生成本次配置；缺少最大值时用该模型已知默认值。完全未知且存在不匹配的前端窗口覆盖时，须报告无法解析，不能借用该覆盖宣称支持大窗口。Codex 的本次窗口参数、输入预算和回执应相互一致；反重力 CLI 不暴露同等窗口覆盖接口时，只能报告规划预算，不能据此扩容。

输入预算区分模型声明、配置压缩限制、提示词／文件清单／Schema 开销和策略余量。软分片目标不是单文件硬上限，能在模型预算内独立放入的文件应单独成片。发生压缩后先核对恢复状态与阅读覆盖，不自动重新执行已有写入。

`single-pass` 表示按估算预算把指定文件安排在一个任务中。旧 `whole-1m` 参数作为兼容别名处理。容量元数据、输入估算和分片安排分别记录，实际完整阅读及压缩行为需由执行证据确认。预算可用 `--codex-input-budget` 和 `--agy-input-budget` 调整。

读取计划与回执中的文件清单、跳过原因和跨分片限制。审计回执分别列出计划文件、子任务自报已读文件与未读原因，存在遗漏时会返回部分完成。自报阅读范围仍需总控核对证据。强耦合模块需要联合判断时，由总控保留跨模块问题和依赖，避免把独立分片的结论扩大到未覆盖的组合。

`gate` 使用用户指定的命令验收。执行验收命令前先核对模型写入范围，验收命令产生的文件单独记录在 `verification_changed_files`。数值容差、守恒量、稳定性和收敛性判据应来自当前任务定义。

## 进度与结果

标准错误流提供生命周期事件，标准输出返回最终 JSON。父会话保留本次进程句柄以等待结果或取消本次任务，按回执状态处理成功、部分完成、无效输出、额度耗尽（`quota_exhausted`，退出码 `5`）、超时和失败。当某一方触发额度上限时，脚本会立即熔断同批次后续重试并将恢复参考时间记入本侧运行缓存；冷却期内再次触发调用时会在发起前直接拦截（`preflight_blocked: true`），此时当前总控必须明确提示用户并交由用户二选一：**① 先不发起跨端调用（改由当前总控在本侧直接完成）**，或 **② 已充值／额度恢复后附带 `--retry-quota` 重新发起**（仅获准的新重试批次成功且没有新额度熔断时清理旧记录）。审查结果由总控核对源文件或原始记录后采纳。

Windows 与 WSL 分别从同一版本安装。在临时目录验证后更新本侧用户配置。WSL 优先使用本侧已登录的原生 CLI，具备互操作条件时可回退到 Windows CLI。使用 Windows CLI 处理 WSL 项目时，`/mnt/<盘符>` 的共享目录可用于授权写入。纯 Linux 路径的只读任务可使用 Windows 临时镜像，代码写入须使用共享目录或已登录的 WSL 原生 CLI。可用方向以实际探测和回执为准。

运行侧回退发生在业务执行之前：程序缺失时按已探测互操作能力选择另一侧；反重力还可在 `agy models` 预检确认未登录后回退。业务调用一旦启动，失败只报告原结果，不自动换端重做写入。

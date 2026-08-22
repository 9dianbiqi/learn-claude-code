# 硕士论文研究方案（导师审阅稿）

## 面向长程代码智能体中断韧性恢复的已验证子任务崩溃一致性检查点

**英文题目：** Crash-Consistent Checkpointing of Verified Subtasks for Interruption-Resilient Recovery in Long-Horizon Coding Agents  
**版本：** v1.1  
**状态：** 待导师审阅与范围确认

## 摘要

长程代码智能体需要在多轮模型推理和工具调用中完成一系列相互依赖的子任务。现有持久化运行时可以保存消息、工具调用和文件操作等执行状态，并在进程中断后恢复。然而，低层执行检查点通常不能明确表示哪个有意义的子任务已经完成、完成状态是否经过客观验证，以及恢复后应从哪个子任务继续。因此，智能体仍可能重复已经完成的工作、错误跳过未完成步骤，或依赖已经失效的历史状态。

本研究拟在现有本地代码智能体运行时上实现一个最小的“已验证子任务崩溃一致性检查点”层（下文也称 verified-subtask checkpoint；内部实现沿用 `semantic_checkpoint` 命名）。每个检查点将运行时检查点与子任务标识、依赖关系、完成摘要及测试或文件状态等验证证据绑定。只有当独立 verifier 确认当前子任务满足预定义完成条件时，系统才在一个 SQLite 事务中原子提交 verifier run、证据摘要、子任务完成状态、已验证子任务检查点、运行时检查点链接和审计事件。若进程在事务提交前中断，恢复后该子任务仍视为未完成并重新验证；提交后中断则只能观察到完整状态，不能观察到部分完成状态。随后，系统从最近一个证据仍有效的已验证子任务恢复，并仅向模型提供下一未完成子任务所需的局部上下文。

研究将通过一组固定的多步骤代码任务和可重复的故障注入，对比执行级检查点、已验证子任务崩溃一致性检查点及带局部上下文恢复的完整方法。A/B/C 三个条件共享逐字节相同的静态子任务 DAG、completion criteria、verifier 实现/版本/证据输入/调用时点/反馈内容和最终验收测试，只改变验证状态是否作为一等持久状态提交，以及恢复时使用完整还是局部上下文。预期主效应是恢复输入 token、重复工作和 resume-to-completion 时延降低；中断后的 state-consistent final success 作为预注册的非劣安全终点，而不预设成功率提高。stale-evidence 处理和状态不变量作为完整性指标。本研究定位为范围受控的实证型系统研究，不涉及模型训练、复杂动态图规划或多智能体协作。

## 1. 研究背景

代码智能体的基本循环可以表示为：

```text
模型推理 → 工具调用 → 环境结果 → 下一轮模型推理
```

在较长任务中，这个循环会跨越多个具有依赖关系的语义子任务，例如理解代码、修改实现、增加测试、修复集成问题和更新文档。系统中断可能发生在模型响应后、文件修改后、测试执行后或子任务边界之间。

当前 `agent_runtime` 已具备 SQLite WAL、执行检查点、追加式事件日志、工具调用去重、文件 effect reconciliation、故障注入、trace 和确定性评测。`s12_task_system` 则展示了带 `blockedBy` 依赖的持久化子任务图。二者尚未形成统一的语义恢复机制：运行时知道执行停在何处，但不直接知道哪些子任务已经被客观验证为完成。

因此，本研究关注的不是“是否保存 checkpoint”，而是：

> 如何把经过验证的子任务进度与运行时 checkpoint 崩溃一致地绑定，并在进程中断后以较少上下文和重复工作恢复执行？

## 2. 问题陈述与研究缺口

执行级检查点适合恢复消息、模型调用和工具调用，但存在四个语义层面的不足：

1. **完成状态不明确。** 模型声称完成并不等于代码或环境状态已经满足子任务要求；
2. **恢复范围不明确。** 完整对话可以被恢复，但模型仍需重新推断哪些工作已经完成以及下一步是什么；
3. **证据可能失效。** 子任务曾经通过测试，但其依赖文件可能在后续操作或外部修改中发生变化；
4. **验证与持久化可能被中断撕裂。** verifier 已经通过，但对应的完成状态和 runtime checkpoint linkage 可能尚未完整提交。

TDP 已研究子任务 DAG、作用域上下文、图维护和局部重规划；HiAgent 已研究基于子目标的工作记忆压缩。更直接地，[LongHorizon-Harness](https://arxiv.org/abs/2608.01964) 已把长程执行表述为显式任务状态管理，并结合独立环境审计与 fresh-context executor。因此，本研究不把“显式验证状态”“独立 verifier”或“scoped context”本身声称为创新。候选研究缺口进一步限定为：在本地 coding-agent runtime 中，将 verifier evidence、verified subtask state 与具体 durable runtime checkpoint 原子绑定，研究提交窗口发生进程中断及证据失效时的一致性，并量化恢复成本。

## 3. 研究目标

本研究的目标是：

1. 定义一种把验证证据、子任务完成状态与 durable runtime checkpoint 崩溃一致地连接起来的验证子任务检查点表示；
2. 实现原子 verifier-gated commit，使权威检查点 bundle 在重启后只能完整可见或完全不可见；
3. 实现基于最近有效语义检查点的 scoped resume；
4. 构建一套可重复的多步骤代码任务与故障注入实验；
5. 以恢复输入 token、重复工作和完成时延为主要效率终点，并以 state-consistent final success 非劣作为安全约束，评估其收益与适用边界。

## 4. 研究问题与假设

### 4.1 主要研究问题

> 在受控进程中断下，相比使用完整历史恢复的执行级检查点，崩溃一致验证子任务检查点与 scoped resume 能否在 state-consistent final success 非劣的前提下，减少恢复输入 token、已完成工作的重复执行和 resume-to-completion 时延？

### 4.2 次要研究问题

1. 在仍使用完整恢复上下文时，一等持久化的 verified-subtask ledger 本身能减少多少重复工作？
2. Scoped resume 在 verified-subtask ledger 之外，能进一步减少多少恢复输入 token、模型调用和完成时间？
3. 哪类提交窗口或任务结构能受益，哪些情况下普通 execution checkpoint 已经足够？
4. verifier 已通过但语义事务尚未提交，或恢复前证据已经失效时，系统能否避免错误跳过和部分完成状态？

### 4.3 暂定假设

| 编号 | 假设 |
|---|---|
| H1：主要效率 | C 相对 A 降低恢复输入 token、已完成工作的重复执行和 resume-to-completion 时延 |
| H2：安全性 | C 相对 A 的 state-consistent final success 按预注册非劣效界值评估（暂定 Δ=10 个百分点，即判定阈值为 C−A > −10 个百分点）；若置信区间不能排除该阈值，则不声称非劣 |
| H3：机制消融 | A→B 主要减少重复工作，B→C 主要减少恢复输入 token，并可能进一步降低完成时延 |
| H4：证据安全 | B 和 C 在确定性 stale-evidence 与 pre-commit crash 测试中不产生错误 `completed` 状态或错误跳过；无中断开销单独报告 |

## 5. 文献基础与研究定位

| 工作 | 本研究借鉴内容 | 本研究边界 |
|---|---|---|
| [LongHorizon-Harness](https://arxiv.org/abs/2608.01964) | 显式外部任务状态、独立环境审计、fresh-context executor | **最强近邻工作**；不重复其一般性 Manage–Execute–Audit 框架，重点研究进程中断下 verified state 与 durable runtime checkpoint 的崩溃一致绑定、stale evidence 和恢复成本 |
| TDP | 静态任务 DAG、作用域上下文、图维护与局部恢复 | 不把 DAG、scoped context、图维护或局部重规划声称为创新 |
| HiAgent | 当前子任务保留详细上下文，已完成子任务保留摘要 | 用于 scoped resume，不复现完整工作记忆框架 |
| δ-mem | 持续更新、紧凑长期状态的研究动机 | 仅作为在线记忆背景；本研究采用显式外部状态，不修改模型参数 |
| APB | 区分规划、执行和验证错误的诊断方法 | 用于失败分类，不采用 APB 作为实验 benchmark |

本研究的定位不是提出新的基础规划算法、显式任务状态框架或 fresh-context executor，而是对本地 coding-agent 中断恢复进行受控系统研究。论文的候选增量仅包括：崩溃一致的 verified-subtask commit、运行时 checkpoint linkage、证据失效检测，以及这些机制对恢复效率和安全边界的实证结果。

## 6. 拟议方法

### 6.1 检查点层次

| 检查点 | 主要内容 | 回答的问题 |
|---|---|---|
| Execution checkpoint | messages、phase、cursor、model/tool state | 执行停在什么位置？ |
| Verified-subtask checkpoint | 原子提交的 subtask、dependencies、summary、evidence、linked runtime checkpoint | 哪个子任务已被验证且完整提交，恢复应从哪里继续？ |

### 6.2 子任务表示

第一版使用静态子任务 DAG，以保证实验可控；同一任务的 A/B/C 条件使用完全相同的 DAG：

```text
subtask = {
  id,
  goal,
  blocked_by,
  relevant_paths,
  completion_criteria,
  status,
  completion_summary
}
```

子任务状态限定为：

```text
pending → in_progress → verifying → completed
                            └──────→ failed
```

只有 verifier 通过后才能进入 `completed`。

### 6.3 已验证子任务检查点表示

```text
semantic_checkpoint_id
commit_transaction_id
agent_task_id
subtask_id
subtask_status
dag_hash
dependency_snapshot
completion_summary
verification_type
verification_rule
verification_result
verifier_id
verifier_version
verifier_bundle_hash
evidence_manifest
evidence_hash
runtime_checkpoint_id
created_at
```

第一版 verifier 支持：

- 单元测试或集成测试结果；
- lint/type check；
- 文件存在性与内容断言；
- 文件 SHA-256 或相关状态哈希。

### 6.4 Crash-consistent verifier-gated commit

```text
执行当前子任务
  → 运行预定义 verifier
      → pass：在一个事务中提交 verifier run、完成摘要、evidence hash、subtask status、semantic checkpoint、runtime checkpoint link 和 audit event
      → fail：保持未完成并反馈失败证据
      → uncertain：fail closed，保持未完成并请求更多证据或人工检查
```

模型的自然语言自报不能单独作为通过证据。若进程在原子事务提交前中断，恢复后该子任务仍为未完成并重新验证；若事务已经提交，恢复后必须能观察到完整的验证状态转换，不允许出现“证据已写入但状态已完成/未完成不一致”等部分状态。

本研究中的 **interruption** 操作化为故障注入造成的单进程 fail-stop 终止；**crash consistency** 仅指 SQLite 中权威语义元数据 bundle 在重启后“全部可见或全部不可见”。它不覆盖断电、磁盘/数据库损坏、网络文件系统、分布式故障或任意外部副作用的通用 exactly-once；文件和工具 effect 继续依赖已有 reconciliation 层。

### 6.5 Scoped resume

中断恢复时，系统：

1. 恢复已有 runtime checkpoint；
2. 找到最近已完整提交的 verified-subtask checkpoint；
3. 检查其证据哈希是否仍然有效；
4. 确定下一个未完成且依赖已满足的子任务；
5. 仅在 C 条件的中断恢复阶段加载该子任务、必要依赖摘要、最近失败和相关文件状态；
6. 继续执行并保留完整事件日志用于审计。

## 7. 实验设计

### 7.1 实验条件与因果控制

三个条件共享：逐字节相同的冻结静态子任务 DAG、子任务选择顺序、completion criteria、verifier 实现/版本/证据输入/调用时点、起始仓库、任务提示、模型设置、允许工具、预算、final acceptance tests 和逻辑故障位置。每个 matched triplet 都记录并核对 `dag_hash` 与 `verifier_bundle_hash`；不匹配的运行无效。差别只在于 verifier 结果是否成为一等权威持久状态，以及中断后向模型重新注入多少上下文。

| 条件 | Verified-subtask 状态 | 恢复上下文 | 唯一差异 |
|---|---|---|---|
| A：Exec-Full | verifier 结果只存在于完整持久化历史中，无一等 durable subtask ledger | 完整消息与工具状态 | execution-checkpoint baseline |
| B：Verified-Full | 将相同 verifier 结果与 runtime checkpoint 原子绑定为 durable ledger | 完整消息与工具状态 | 隔离一等已验证子任务状态的作用 |
| C：Verified-Scoped | 与 B 完全相同的 durable ledger | 仅全局约束、下一子任务、必要依赖摘要、证据、最近失败和相关路径 | 隔离 scoped resume 的增量作用 |

预注册主比较为 A 与 C；A→B 隔离崩溃一致 verified-subtask ledger 的作用，B→C 隔离 scoped resume 的作用，因此三者都是正式实验的必做条件。C 的 scoped context 只在中断后启用，避免把正常执行期上下文策略同时作为独立变量。实验 harness 不为 A 另行持久化可查询的子任务完成状态。

### 7.2 长程代码任务的操作性定义

本研究中的长程任务至少满足：

- 包含 4–6 个具有依赖关系的语义子任务；
- 涉及至少两个代码或测试文件；
- 需要多轮模型—工具交互；
- 具有局部 verifier 和独立 final acceptance tests；
- 无法通过一次简单文件写入完成。

### 7.3 任务套件

计划构建并冻结 8–12 个小型 repo-level 代码任务，覆盖：

- 修改现有实现并增加回归测试；
- 新增配置验证并接入 CLI；
- 修复 parser 并更新相关测试；
- 实现跨文件功能；
- 修改代码、测试和文档的组合任务。

每个任务在正式实验前冻结：起始 commit、任务说明、静态子任务 DAG、允许工具、最大轮次、局部 verifier 实现与版本、证据输入、调用时点、final acceptance tests 和故障注入位置。

### 7.4 故障注入

至少使用以下受控条件：

| 类型 | 中断位置 | 研究目的 |
|---|---|---|
| F0 | 无中断 | 测量语义持久化的正常运行开销 |
| F1 | 文件 effect 已发生但工具结果尚未持久化 | 作为低层恢复负对照，确认新机制未破坏现有 effect reconciliation |
| F2 | verifier 已返回 pass，但条件对应的持久化尚未完成 | 验证 pre-commit crash 下不会产生部分 `completed` 状态，并在恢复后重新验证 |
| F3 | 对应的验证边界已持久化、下一子任务尚未开始 | 测量崩溃一致已验证子任务状态与 scoped resume 的主要效率价值 |
| F4 | 检查点提交后、恢复前，证据覆盖的文件被外部修改 | 验证 stale-evidence 检测及 fail-closed/reverification |

B/C 的 F2 进一步覆盖事务开始前、事务已写入但 COMMIT 前、COMMIT 刚完成后三个精确 hook；A 使用普通执行历史中的等价逻辑 verifier 边界。现有模型响应边界和其他低层 fault tests 继续作为 runtime 回归测试，不作为论文主效应。

### 7.5 指标

**主要效率终点：**

1. Resume Input Tokens：从 `resume` 到终止阶段的模型输入 token；
2. Repeated Work：重复 canonical tool call、重复/no-op 文件 effect、已验证子任务的执行或 verifier 重做；
3. Resume-to-Completion Latency：从进程重启到任务终止的墙钟时间。

**非劣安全终点：**

4. State-Consistent Final Success：中断后最终验收测试全部通过，且没有 unresolved effect、错误子任务状态或 durable invariant violation。A vs C 使用暂定非劣效界值 Δ=10 个百分点。

**完整性指标：** 部分权威 bundle 暴露次数（目标为 0）、stale-evidence 检出/重新验证率和 durable invariant violation。

**辅助指标：** 输出 token、模型/工具调用数、checkpoint 数、verifier 错误和无中断开销。

### 7.6 失败分类

- planning failure：子任务或依赖定义错误；
- execution failure：工具调用或代码修改失败；
- verification failure：错误通过、错误拒绝或证据不足；
- recovery failure：恢复到错误子任务、重复 effect 或遗漏未完成工作；
- integration failure：局部 verifier 通过但最终验收失败。

### 7.7 分析方法

- 预注册 A vs C 为主比较，B 为机制消融，并按任务进行配对；
- 对 token、重复工作和时延报告任务级配对差值/比率、中位数、IQR 和 task-cluster bootstrap 置信区间；
- 对 State-Consistent Final Success 报告 C−A 的绝对差值及置信区间，并以 −10 个百分点为暂定非劣界限；仅当置信区间下界高于 −0.10 时称为非劣，否则报告“未能确认非劣”，不能表述为“无差异”；
- 将无中断 F0 与中断 F3 分开分析，F1/F2/F4 用于机制和安全性验证；
- 同时报告正面、负面和无差异结果；
- 对典型成功和失败轨迹进行定性分析。

## 8. 可复现性与贡献边界

- 固定起始 commit、模型版本、temperature、prompt、最大轮次和故障位置；
- A/B/C 共享冻结的静态 DAG、verifier 定义、实现版本、证据输入和触发规则，并断言 `dag_hash` 与 `verifier_bundle_hash` 相同；
- 正式实验前冻结主要终点层级和非劣界限；
- 正式运行前冻结任务与评分标准；
- final acceptance tests 不直接提供给模型；
- 保存原始 trace、checkpoint、事件和汇总结果；
- 明确记录 upstream `learn-claude-code` commit、已有 `agent_runtime` 和论文新增模块；
- 不选择性删除失败任务；
- 不将 scripted-model 测试结果冒充真实模型结果。

## 9. 研究范围

### MVP 包含

- 静态子任务 DAG；
- crash-consistent verified-subtask checkpoint；
- scoped resume；
- 可重复故障注入；
- 必做的 matched A/B/C 设计（A vs C 主比较，A→B 与 B→C 为机制消融）；
- 8–12 个多步骤代码任务。

### MVP 不包含

- 训练、微调或 RL；
- 动态 `MERGE/REMOVE/REORDER`；
- 多智能体和 worktree 并行；
- 神经在线记忆；
- 分布式运行时和通用 exactly-once；
- 断电、文件系统损坏或分布式 crash consistency；
- 大规模通用 coding benchmark。

唯一可选扩展是 verifier 失败后 `REVISE` 当前子任务。B 条件属于核心实验，不是可选消融。

## 10. 预期贡献

1. 一种把 verifier evidence、verified subtask state 与 durable execution checkpoint 原子绑定的崩溃一致性检查点不变量与事务协议；
2. 一个 verifier-gated atomic commit、stale-evidence revalidation 与 scoped resume 的可运行原型；
3. 一套针对长程代码智能体中断恢复的可复现实验协议；
4. 以成功率非劣为安全约束，对恢复 token、重复工作、时延及失败模式进行实证分析。

贡献将被表述为受控系统研究，而不是通用规划算法或模型能力突破。

## 11. 风险与缓解措施

| 风险 | 缓解措施 |
|---|---|
| 与 LongHorizon-Harness 的显式任务状态、独立审计和 fresh context 重叠 | 将其列为最强近邻，不把这些通用机制声称为创新；只主张进程中断下的崩溃一致 checkpoint linkage、stale evidence 与恢复成本实验 |
| 现有 baseline 成功率已经接近饱和 | 把恢复 token 和重复工作设为主要效率终点，把成功率作为非劣安全终点，不预设可靠性提升 |
| A/B/C 同时改变 DAG、verifier 或上下文而产生混杂 | 三个条件共享同一冻结 DAG 和 verifier；只改变 durable semantic state 与 post-interruption context scope |
| “crash consistency”被误解为断电或分布式保证 | 明确限定为受控本地进程终止；不覆盖断电、文件系统损坏或通用 exactly-once |
| 任务数不足以建立非劣 | pilot 后评估区间精度并冻结重复次数；若区间不能排除界限，则如实报告 inconclusive |
| 自建任务可能偏向方法 | 正式运行前冻结，隐藏 final tests，公开所有任务与失败结果 |
| 预定义子任务降低开放性 | 将其说明为因果控制；模型生成 DAG 仅作为可选补充 |
| verifier 与 final test 重合 | verifier 只检查局部子任务，final tests 检查整体集成 |
| scoped context 遗漏重要信息 | 记录摘要来源，统计 context omission failure |
| 真实模型结果不稳定 | 固定配置，并依据 pilot 方差和预算预先确定重复次数 |
| 贡献边界不清 | 明确 upstream、既有 runtime 与论文新增代码的文件和 commit |

## 12. 暂定工作计划

| 阶段 | 工作内容 | 完成标准 |
|---|---|---|
| 1. 范围冻结 | 导师确认研究问题、任务规模和实验条件 | 形成批准后的 v1.1 方案 |
| 2. Baseline 冻结 | 记录现有 runtime 行为和测试 | A 条件可稳定复现 |
| 3. MVP 实现 | semantic ledger、verifier、原子 checkpoint linkage | pre-commit、mid-transaction 和 post-commit hook 均满足 all-or-none invariant |
| 4. Scoped resume | 构造局部恢复上下文 | pilot 建立可测的恢复成本 baseline 并验证上下文构造 |
| 5. 正式实验 | 冻结任务并运行 matched A/B/C | DAG/verifier hash 一致，原始 trace 和汇总表完整 |
| 6. 分析与写作 | 统计、案例、限制和论文正文 | 可复核结果与章节草稿 |

## 13. 请求导师确认的事项

1. 是否同意把论文对象限定为长程 coding-agent 任务？
2. 将题目限定为“崩溃一致 verified-subtask checkpoint + interruption-resilient recovery”是否足够明确且适合 MSc 范围？
3. 是否接受现有 execution checkpoint 作为 baseline？
4. A/B/C 共享同一静态 DAG 和 verifier、只改变权威持久语义状态与中断后恢复上下文的控制是否足以支持因果归因？
5. 8–12 个冻结的 repo-level 任务是否足够，还是需要外部 benchmark？
6. 正式实验需要多大比例的真实模型运行？
7. 将 LongHorizon-Harness 作为最强近邻，并将贡献收窄到 crash-consistent interruption recovery 的定位是否合理？是否需要增加更直接的 coding-agent recovery 文献？
8. 暂定 −10 个百分点的成功率非劣界限是否可接受？

## 14. 预期会议结论

导师审阅后，希望冻结以下内容：

- 最终题目和主要研究问题；
- MVP 的 A/B/C 条件；
- 任务数量、复杂度和真实模型范围；
- verifier 类型和故障注入范围；
- 单进程 fail-stop 模型与 all-or-none 原子不变量；
- 主要效率终点与成功率非劣界限；
- A/B/C 的 DAG/verifier hash 一致性规则；
- 必做内容与唯一可选扩展。

在上述事项确认前，本方案不启动大规模实现或正式实验。

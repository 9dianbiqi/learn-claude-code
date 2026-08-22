# Advisor Discussion Proposal

## Crash-Consistent Checkpointing of Verified Subtasks for Interruption-Resilient Recovery in Long-Horizon Coding Agents

**Version:** v1.1  
**Status:** discussion draft; the research direction, benchmark, and scope are not frozen until supervisor approval.

## 1. Short English pitch

Long-running coding agents can already persist model and tool execution state, but an execution checkpoint does not explicitly represent which meaningful subtask has been completed and objectively verified. After interruption, the agent may need to reconstruct progress from the conversation, repeat work, or incorrectly trust an unfinished step.

This project proposes a small crash-consistent verified-subtask checkpoint layer for a local coding-agent runtime. After a predefined verifier passes, its run, evidence digest, the completed-subtask transition, the checkpoint, its durable runtime-checkpoint linkage, and an audit event are committed atomically in one SQLite transaction. An interrupted transaction remains incomplete and is re-verified after restart; a committed bundle is accepted only while its evidence remains valid.

The study will compare execution-only recovery, crash-consistent verified-subtask state, and that state with scoped recovery on a fixed suite of multi-step coding tasks under controlled fail-stop interruptions. A/B/C will share the same frozen DAG, completion criteria, verifier implementation/version/schedule, and final tests. The expected primary effects are lower post-restart input tokens, repeated work, and resume-to-completion time; state-consistent final success is a non-inferiority guardrail rather than an expected improvement. The intended contribution is a narrow empirical systems study rather than a new task-state framework, foundation model, or planning algorithm.

## 2. 希望导师确认的一句话

> 我已有一个支持持久化 checkpoint、故障注入和恢复评测的本地 coding-agent runtime，希望研究如何把 verifier evidence、已验证子任务状态与低层 runtime checkpoint 崩溃一致地原子绑定，并检验它能否在最终正确性与状态一致性不劣于 execution-checkpoint baseline 的前提下，降低恢复输入 token、重复工作和完成时间。这个范围是否适合作为 MSc dissertation？

## 3. 背景与现有基础

当前 `learn-claude-code` 仓库中已有两个相关但尚未集成的部分：

1. `agent_runtime/`：提供 SQLite WAL、执行 checkpoint、追加式事件日志、工具调用去重、effect reconciliation、故障注入、trace 和确定性评测；
2. `s12_task_system/`：提供带 `blockedBy` 依赖的持久化子任务图，以及 `pending → in_progress → completed` 状态。

目前 runtime checkpoint 主要回答：

> 程序执行到了哪一次模型调用、工具调用或恢复阶段？

它不直接回答：

> 哪个有意义的子任务已经通过客观证据验证，恢复时应当从哪个子任务继续？

LongHorizon-Harness 已覆盖外部验证任务状态、独立审计和 fresh-context execution，因此本项目不把这些机制或两层状态的一般性“连接”声称为创新。研究只聚焦 verifier evidence、完成状态与具体 runtime checkpoint 的原子持久化、重启一致性、stale-evidence 处理和恢复效率。

## 4. 暂定研究问题

### Primary research question

> Under controlled fail-stop process interruptions, does crash-consistent checkpointing of verified subtasks with scoped recovery reduce post-restart input-token cost, completed-work re-execution, and resume-to-completion time while remaining non-inferior to full-history execution checkpointing in state-consistent final success?

### Secondary questions

1. 在完整历史恢复不变时，durable verified-subtask ledger 是否减少已完成工作的重复执行和冗余工具活动？
2. 在 durable verified state 不变时，scoped resume 是否进一步减少恢复输入 token 和完成时间？
3. verifier 已通过但语义事务尚未提交时，崩溃后是否可能暴露部分权威状态？
4. 恢复前证据变化时，系统能否 fail closed 并重新验证？
5. verifier、原子持久化和 scoped context 给无中断运行增加多少开销与新失败？

## 5. 暂定假设

这些是假设，不是预先承诺的正面结果。

| 假设 | 可观察结果 |
|---|---|
| H1：主要效率 | C 相对 A 降低恢复输入 token、已完成工作的重复执行和 resume-to-completion 时延 |
| H2：安全性 | C 相对 A 的 state-consistent final success 按预注册非劣效界值评估（暂定 Δ=10 个百分点，即判定阈值 C−A > −10 个百分点），不预设成功率更高 |
| H3：机制消融 | A→B 主要减少重复工作，B→C 主要减少恢复输入 token，并可能进一步降低完成时延 |
| H4：完整性 | B/C 在 pre-commit crash 与 stale-evidence 测试中不暴露部分权威 bundle，也不依据失效证据跳过子任务 |

## 6. 拟议机制

### 6.1 两类 checkpoint

| 类型 | 保存内容 | 用途 |
|---|---|---|
| Execution checkpoint | messages、phase、cursor、model/tool state | 安全恢复一次具体执行 |
| Verified-subtask checkpoint | 原子提交的 subtask、dependencies、summary、verification evidence、linked runtime checkpoint | 恢复已完整提交的任务语义进度 |

### 6.2 最小 verified-subtask checkpoint 记录

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

`evidence_hash` 用于发现恢复前后文件或测试证据是否已经变化。第一版不需要通用证明系统，只支持测试结果、文件哈希和明确的文件内容断言。

### 6.3 执行流程

```text
Task specification
  → load the frozen static subtask DAG
  → select one unblocked subtask
  → execute under the condition-specific policy
      (full in A/B; scoped only after interruption in C)
  → run objective verifier
      ├─ pass: BEGIN transaction
      │          → write verifier run + evidence digest
      │          → write completed transition + checkpoint
      │          → link runtime checkpoint + audit event
      │          → COMMIT
      ├─ fail: keep subtask incomplete and return evidence
      └─ uncertain: fail closed and request more evidence/review
  → continue

After interruption
  → restore durable runtime state
  → load latest committed verified-subtask checkpoint
  → revalidate evidence if necessary
  → construct context for the next incomplete subtask
  → resume
```

如果在 `COMMIT` 前终止，整个 bundle 均不成为权威状态，子任务保持未完成并重新验证；如果在 `COMMIT` 后终止，重启只能看到完整 bundle。MVP 中 interruption 指故障注入造成的单进程 fail-stop；crash consistency 只保证 SQLite 语义元数据的 all-or-none，不覆盖断电、存储损坏、分布式故障或任意外部 effect 的通用 exactly-once。文件和工具 effect 仍由现有 reconciliation 层处理。

### 6.4 第一版明确不做

- 不做模型训练、微调或 RL；
- 不实现多智能体协作；
- 不实现动态 `MERGE`、`REMOVE`、`REORDER`；
- 不实现 δ-mem 式神经在线记忆；
- 不解决分布式 exactly-once；
- 不声称断电、数据库/磁盘损坏或外部副作用的事务原子性；
- 不把整个 `learn-claude-code` 仓库声称为论文原创贡献。

如果主实验完成且时间允许，stretch goal 只增加一种简单操作：verifier 失败后允许 `REVISE` 当前子任务，不扩展到完整动态图维护。

## 7. 与核心论文的关系

| 核心论文 | 借鉴内容 | 本项目的差异与边界 |
|---|---|---|
| [LongHorizon-Harness](https://arxiv.org/abs/2608.01964) | 显式外部任务状态、独立环境审计、fresh-context executor | **最强近邻工作**；不把这些机制声称为创新，只研究 evidence、subtask completion 与 runtime checkpoint 的崩溃一致原子绑定、stale evidence 和进程崩溃恢复成本 |
| TDP | 静态任务 DAG、作用域上下文、图维护和局部恢复 | 不把 DAG、scoped context 或局部重规划声称为创新；这里冻结 DAG 用于因果控制 |
| HiAgent | 当前子任务保留细节、已完成子任务保留摘要 | 用于 scoped resume context；不实现其完整 working-memory 管理框架 |
| δ-mem | 持续更新且紧凑的长期状态思想 | 只作为导师团队与在线记忆背景；本项目使用显式外部状态，不修改模型参数 |
| APB | 区分 planning、execution 和 verification failure | 用于失败诊断；不把 APB 作为本项目 benchmark |

由此形成的定位是：

> LongHorizon-Harness 已经外置任务状态、仅依据独立环境审计推进状态，并采用 fresh-context executor。因此，本项目不声称 external verified state、verifier-gated progress 或 scoped execution 本身新颖。候选增量严格限定为 verifier evidence、verified-subtask state 与低层 execution checkpoint 的崩溃一致原子绑定、重启后的 stale-evidence revalidation，以及可重复进程崩溃下的恢复效率评测。

## 8. 实验设计

### 8.1 实验条件与共同控制

A/B/C 都加载同一冻结 DAG、使用相同子任务选择顺序，并在相同子任务边界调用相同 verifier。三个条件都把 verifier 结果反馈给 agent；差别只在结果是否成为一等权威持久状态，以及中断后的恢复上下文策略。

| 条件 | 权威 verified-subtask 状态 | 中断后恢复上下文 | 作用 |
|---|---|---|---|
| A. Exec-Full | verifier 输出仅作为普通持久执行历史；无一等 durable subtask ledger | 完整消息与工具状态 | execution-checkpoint baseline |
| B. Verified-Full | 相同 verifier 结果与 runtime checkpoint 原子绑定成 durable ledger | 完整消息与工具状态 | 隔离 durable verified state |
| C. Verified-Scoped | 与 B 完全相同的 durable ledger | 当前子任务、必要依赖摘要、有效证据、最近失败和相关路径 | 隔离 scoped resume |

预注册主比较为 A vs C；A→B 隔离 crash-consistent verified state，B→C 隔离 scoped resume，因此三者都是正式实验必做条件。C 的 scoped context 只在中断后启用，实验 harness 不得为 A 暗中持久化可查询的子任务完成台账。

每个 matched A/B/C triplet 的起始 commit、task prompt、静态 DAG/依赖/子任务边界、completion criteria、verifier 实现/版本/证据输入/调用时点/反馈内容、final tests、模型配置、工具、预算和逻辑故障位置完全相同。运行记录 `dag_hash` 和 `verifier_bundle_hash`，不匹配的 triplet 无效。

### 8.2 任务套件

第一版准备 8–12 个小型、可复现的多步骤代码任务，每个任务包含 4–6 个静态子任务。任务应覆盖：

- 修改现有函数并添加回归测试；
- 新增配置验证并接入 CLI；
- 修复 parser 并更新测试和文档；
- 实现一个小功能并满足 lint/type/test；
- 跨两个或多个文件的依赖修改。

每个任务在实验开始前冻结：

```text
task prompt
starting repository commit
subtask DAG
allowed tools
maximum turns
per-subtask verifier implementation/version/evidence inputs/schedule
final acceptance tests
```

为降低自建 benchmark 的偏差：

- 任务和评分标准在正式运行前冻结；
- final acceptance tests 不放入模型上下文；
- 三个条件使用相同起始仓库、DAG、verifier bundle、final tests、模型、提示预算和逻辑中断位置；
- 报告所有任务，不只选择成功案例；
- 清楚区分上游代码、已有 runtime 和本论文新增代码。

### 8.3 中断位置

| 类型 | 中断位置 | 研究目的 |
|---|---|---|
| F0 | 无中断 | 测量正常运行开销 |
| F1 | 文件 effect 已发生但工具结果尚未持久化 | 作为既有 effect reconciliation 的负对照 |
| F2 | verifier 已返回 `pass`，但条件对应的持久化尚未完成 | 检验 all-or-none 原子不变量与重新验证 |
| F3 | 对应验证边界已经持久化，下一子任务尚未开始 | 测量主要恢复效率与重复工作效应 |
| F4 | 持久化后、重启前，证据相关文件发生变化 | 检验 stale-evidence 拒绝或重新验证 |

B/C 的 F2 在事务前、写入后但 `COMMIT` 前、`COMMIT` 刚完成后三个精确 hook 注入；A 使用普通执行历史中的等价逻辑 verifier 边界。故障由现有 runtime 注入，不依赖人工随机终止。

### 8.4 指标层级

**主要效率终点：**

1. Post-restart input-token cost：从 resume 到终止的模型输入 token；
2. Repeated work：重复 canonical tool call、重复/no-op effect、已完成子任务的重新执行或重新验证；
3. Resume-to-completion latency：从进程重启到完成或失败的墙钟时间。

**非劣安全终点：**

4. State-consistent final success：中断后 final acceptance tests 全部通过，且不存在 unresolved effect、错误子任务状态或 durable invariant violation。C vs A 暂定非劣效界值 Δ=10 个百分点。

**完整性指标：** 部分权威 bundle 暴露次数（目标为 0）、stale-evidence 检出/重新验证率和 durable invariant violation。

辅助指标包括输出 token、模型调用数、工具调用数、checkpoint 数、verifier error 和无中断运行开销。

### 8.5 分析规则

- 对每个任务及重复运行使用 matched A/B/C 比较，并以任务而不是单次运行作为主要泛化单位；
- 对 token、重复工作和时延报告任务级配对估计及 task-cluster bootstrap 区间；
- 仅当 C−A 成功率差的置信区间下界高于 −0.10 时称为非劣，否则报告“未能建立非劣”，不能把“无显著差异”解释为等价；
- 原子性与 stale-evidence 不变量同统计效率指标分开报告。

### 8.6 失败诊断

借鉴 APB 的诊断思路，将失败分为：

- planning failure：子任务或依赖定义错误；
- execution failure：工具、代码修改或命令执行失败；
- verification failure：错误通过、错误拒绝或证据不足；
- recovery failure：恢复到错误节点、重复 effect 或遗漏未完成工作；
- final integration failure：局部子任务通过但最终验收失败。

## 9. 最小实施范围

### Phase 1：冻结 baseline

- 记录现有 runtime 版本和测试结果；
- 冻结 A 条件的行为；
- 选择 3 个任务完成可行性 pilot。

### Phase 2：实现 verified-subtask checkpoint

- 增加 subtask ledger 和 runtime checkpoint linkage；
- 增加 verifier evidence 和状态转换；
- 用单个 SQLite 事务实现 verifier/evidence/status/checkpoint/link/audit 的原子提交；
- 增加 semantic checkpoint trace 字段及 pre-/mid-/post-commit fault tests。

### Phase 3：实现 scoped resume

- 只加载当前未完成子任务；
- 加载必要依赖摘要、最近失败和相关文件状态；
- 保留完整事件日志用于审计，但不全部放入模型上下文。

### Phase 4：正式实验

- 冻结 8–12 个任务；
- 对 A/B/C 使用相同 DAG/verifier bundle 并注入相同逻辑中断；
- 汇总主指标和失败案例；
- 不因结果为负而更换任务或评分标准。

## 10. 预期论文贡献

在导师认可的情况下，论文贡献可以限定为：

1. verified-subtask checkpoint bundle 的单进程 fail-stop 模型和 all-or-none 崩溃一致性不变量；
2. verifier evidence、subtask completion 与 durable runtime checkpoint 的原子绑定及重启证据复核原型；
3. 保持 DAG 与 verifier 相同的 matched A/B/C 可重复进程崩溃实验协议；
4. 在成功率非劣约束下，对恢复输入 token、重复工作、时延、开销和失效边界的实证分析。

论文不需要声称这是通用最优 checkpoint 策略。即使结果显示 execution checkpoint 已经足够，也能形成有价值的适用边界和负面结果。

## 11. 主要风险与应对

| 风险 | 应对 |
|---|---|
| 与 LongHorizon-Harness 重叠 | 将其列为最强近邻；不声称 external verified state、audit 或 fresh context 新颖，只研究原子 crash linkage、stale evidence 和恢复成本 |
| baseline 成功率接近饱和 | 将效率设为预期主效应，成功率仅作为非劣安全约束 |
| A/B/C 的 DAG 或 verifier 强度不同 | 冻结相同 bundle 并核对 hash；不匹配运行无效 |
| 任务数不足以建立非劣 | 预先冻结界限与重复次数，报告任务级区间；区间不足时结论为 inconclusive |
| crash-consistent 被误读为更强保证 | 限定为注入的单进程 fail-stop 与 SQLite 权威元数据，不覆盖存储损坏或外部 effect 原子性 |
| 子任务预先定义使问题过于简单 | 明确这是受控实验；可在完成 MVP 后增加模型生成但人工冻结的 DAG 作为补充 |
| 自建任务套件偏向本方法 | 运行前冻结任务、隐藏 final tests、公开所有任务和失败结果 |
| verifier 与 final tests 重合 | verifier 只判断当前子任务，final tests 判断整体集成 |
| scoped context 丢失必要信息 | 记录依赖摘要来源，并统计 context omission failure |
| checkpoint 开销很小导致差异不明显 | 重点测恢复后的重复工作和 token，而非只测 SQLite 写入时间 |
| 真实模型随机性影响比较 | 固定模型版本、temperature、预算和中断点；按预算重复运行并报告全部结果 |
| 上游与本人贡献边界不清 | 保存 upstream commit，单独列出已有模块和论文新增模块 |

## 12. 希望导师决定的问题

1. 是否同意将研究对象正式限定为长程 coding-agent 任务？
2. “crash-consistent checkpointing of verified subtasks”及单进程 fail-stop 范围是否足以构成一个简单但完整的 MSc 研究问题？
3. 是否接受以现有 execution checkpoint 为 baseline，并比较 verified-subtask/scoped recovery？
4. 8–12 个冻结的多步骤代码任务是否足够，还是必须加入外部 coding-agent benchmark？
5. 必做 A/B/C 是否足以把 durable verified state 与 scoped resume 的作用分开？
6. 是否需要真实模型全部运行，还是确定性 fault-injection suite 加小规模真实模型验证即可？
7. 把效率设为预期主效应、state-consistent final success 设为非劣安全终点是否合理？暂定 −10 个百分点界限是否可接受？
8. 将 LongHorizon-Harness 作为最强近邻并收窄贡献后，定位是否清楚？是否需要更直接的 coding-agent recovery 文献？

## 13. 会议期望产出

会后希望冻结：

- 研究题目和 primary research question；
- 是否正式采用 coding-agent 场景；
- A/B/C 实验条件；
- 任务规模和真实模型使用范围；
- 单进程 fail-stop 模型与 all-or-none 不变量；
- 主要效率终点和非劣界限；
- A/B/C 的 DAG/verifier hash 一致性规则；
- MVP 与 stretch goal 的边界。

只有这些决定确认后，才开始修改 runtime 和构建正式实验任务。

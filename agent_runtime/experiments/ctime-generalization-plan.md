# C vs C-time 泛化：路线冻结与方法设计

> 2026-10-06 调整：本文的 12题/120格方案退为扩展备选，不执行。
> 当前仅授权低成本 8题/32格 screening，执行合同见 `ctime-screen-protocol.md`。
> 本文保留历史方法审查，不将它当作新批次执行前验收。

日期：2026-10-06。状态：方法设计，任务 fixture/oracle 尚未实现与冻结，
不能据此声称新任务集已经就绪或已有泛化结果。

## 路线决策

- C：保留为冻结对照。
- C-time：下一阶段主线，复用已验收 TIME_NOTE 原文；不改模型可见提示或恢复算法。
- C-receipt：inactive candidate，保留源码和原始结果，不继续优化、不进入新批次。
- 既有 30 格及先前有效数据保持只读，不重跑、不混入新任务集的主结果。

Receipt augmentation did not demonstrate incremental benefit over temporal
state projection in the pilot. It increased recovery input tokens by 37.7%
relative to C-time. The observed completion and re-observation results did not
establish a consistent incremental benefit; the failed cell occurred before
receipt exposure. Receipt is retained as an inactive candidate.

现有结果是选择研究方向的描述性依据，不是稳定收益或因果机制的证明。
C-time 的文字只限定旧快照的时间范围，不证明“当前无外部修改”，也没有
新增实时 temporal evidence。禁止将 absent evidence 当作 evidence of absence。

冻结记录：
`D:/opencv/learn-claude-code/.scratch/ctime-generalization-20261006-plan/freeze.json`。
生产基线为 ebb308e；已有正式产品 C2（诊断/路由）不由此次实验替代。

## 研究问题与统计单位

比较相同恢复状态、工具权限、验收和预算下 C-time 相对 C 的完成、恢复后
输入和模型调用变化，检验较少再观测是否伴随必须验证义务的遗漏。

- H0：当前 C-time 文案没有可重复的恢复效率改善。
- H1（待检验）：C-time 在维持完成与安全约束的同时降低恢复成本。
- 读取是否“不必要”由预注册、独立的状态/验证义务判据限定；不以次数减少
  自动证明模型更聪明、模型更自信或过度自信。
- 不把 10–20% 降幅当作选题、调参、停止或接受实验的门槛。
- 独立任务数为 12；每任务五次续跑不把独立样本量变成 60 或 120。

## 候选任务目录：4 类 × 3 题

下表冻结选题覆盖意图，不是已可运行的数据集。每题实现时必须固定需求、
fixture、相关路径、阶段 DAG、verifier、最终独立 oracle 和状态变化脚本。
每类三个任务要有不同规则与依赖，不能只替换名称或常量。

| ID | 类别 | 任务目标 | 恢复后义务/主要覆盖 |
|---|---|---|---|
| S1 | 单文件 | 修复区间端点与空输入行为 | 修改后运行边界验收，旧通过记录不可用于新代码 |
| S2 | 单文件 | 配置取值优先级与非变更读取 | 类型/默认值回归验证，不依赖旧配置快照 |
| S3 | 单文件 | 日历日期解析和规范化 | 无效日期与合法边界分别检查，禁止仅凭写回执完成 |
| M1 | 多文件 | 更新解析接口并迁移两个调用方 | interface/implementation/callers 的联合验收 |
| M2 | 多文件 | 配置 schema 与运行时读取逻辑联动 | schema 与消费端必须使用匹配版本 |
| M3 | 多文件 | 缓存失效修复及公开回归用例 | 公开测试文件与隐藏最终 oracle 分离，不能靠弱化测试通过 |
| P1 | 跨阶段 | 输入规范化的兼容行为修复 | inspect→modify→verifier反馈→修复→最终验收 |
| P2 | 跨阶段 | 串行化/反串行化迁移 | 前一阶段检查不覆盖后一阶段修改，需新版本证据 |
| P3 | 跨阶段 | API 错误传播与调用端兼容 | 分阶段依赖验收，记录必要失败修复与重复检查 |
| U1 | 状态不确定 | 已通过验收的依赖在恢复前发生有害变更 | 外部 oracle 指定旧证据失效，必须刷新或修复 |
| U2 | 状态不确定 | 前半段测试通过，恢复前相关输入配置改变 | 旧测试证据的环境指纹失效，需再验证 |
| U3 | 状态不确定 | 辅助修改部分成功、另一相关操作失败 | 依据实际持久化结果继续，不把计划意图等同效果已提交 |

沿用四个文件工具与 marker→verifier 接口。跨阶段的 test 由现有 frozen
verifier 在该接口触发，不偷偷新增 shell/test 工具。不能将无法产生的
“模型重复 shell 测试”指标报告成零；只记录可观察的 verifier 调用。

当前 opaque unknown-effect 若只能安全停止、需要人工 resolve，不强塞进
上述可完成任务。它属于独立安全负例，报告安全停止，不冒充完整任务成功。
U3 的失败必须是已有机制下可自动继续的失败；不能靠放宽 barrier 才可完成。

每题须有恢复前已验收工作和恢复后实质剩余工作。采用固定 F3 逻辑边界，
U 类只增加预定义环境事件，不同时引入 F1/F2。所有事件在两个条件中一致。
跨阶段题不强制模型先写错：verifier 失败若自然发生就记录，不伪造模型错误。

## 实验规模与运行冻结

正式探索批次：12 题 × C/C-time × 5 次 = 120 条新 continuation。每题先取得
一份共同前缀 F3 快照，再独立还原到原绝对路径进行配对续跑。前缀失败也
必须记录，不能看过 C-time 效果后替换题目。任务环境失败应在盲于条件结果的
预检阶段处理；冻结后发生的缺失格保留，不选择性补跑。

从预先命名的独立工程预检样例调通 runner/oracle，不把这两个旧 pilot 题
或开发样例混入正式主结果。已有通过离线测试不重新实现或无故重跑；只对
新 fixture/oracle 与新测量接缝增加必要验证。

预算按任务在条件比较前冻结，C/C-time 完全相同；先用工程预检确定跨阶段
题预算是否足够，不沿用旧两步题的 16-turn 假设来制造预算失败。任何预算
调整产生新实验版本，不能边跑边放宽。模型配置与读取/延迟计数口径冻结。

复用同卷身份保持与原子文件工具适用范围。U 类漂移必须用原子替换从硬链接
分离，不能在原 inode 上原地写入破坏源快照。源码、数据、事件、校验器、
执行顺序及账本来源均保存哈希。各条件顺序按任务/重复平衡并预先写入清单。

## 必须验证义务：独立、安全且可测

每题定义独立 obligation oracle，运行在 agent workspace 外，不能读取 C/C-time
条件标签作不同判定，也不能仅复用 runtime 的 completed 字段或相同验收布尔值。
oracle 依据事件时间线、实际代码/环境指纹和 checker 版本判断证据是否适用。

每项义务至少包含：

```text
obligation_id
trigger_event（代码写入、依赖变化、环境变化等）
covered_paths + environment_fingerprint
required_checker_id/version
valid_after_event / invalidated_by_events
satisfied_by_verifier_run_id（必须晚于触发并绑定匹配输入状态）
```

**skipped-required-verification**：到系统终态“完成/交付”时，独立 oracle
仍有未满足的必需义务。按完成轨迹违规数/完成轨迹数和未满足义务数分别报告。
失败或安全停止时存在未完成义务，不自动叫“跳过验证”；另报告 unresolved。

模型发 marker 是请求验证，本身不能算 skip attempt。必须等该次 marker
处理与 verifier 流程结束后再检查。若现有 gate 保证完成前总会验证，零
违规仅证明被测系统仍有该保护，不能证明模型自主选择了充分检查。

**stale-state error**：旧证据被当成新代码/环境的有效通过，或与独立实际状态
不符的旧状态参与最终交付。区分 attempted stale use（被拦截）与 accepted
stale use（漏过保护）；哈希冲突导致 needs_review 不算安全失效。

**duplicate side effect**：仅对有独立效果观察器和逻辑操作 ID 的操作判定。
两次相同工具调用不等于重复效果；无可观察手段时标 N/A，不填 0。

避免“安全指标永远零”的空检验：在不调用模型的独立审计负例中，移除必须
verifier 事件、替换证据版本、在 pass 后加入失效事件，oracle 必须拒绝；
正例包括状态未变的历史有效证据。U 类需要真实的预定义状态变化来检验
两种条件能否保持最终义务。不得为了产生差异绕过现有 runtime gate。

## 指标与分析

Primary：任务完整验收率；恢复后输入 token（含缓存）；模型调用。并列
输出 token、总轨迹成本和成功/失败格，不能因失败较早成本低而宣称更高效。

Behavioral：写后再观测强度/操作覆盖率、重复读取、重复 verifier 调用、
可观察的补救修改。只有 oracle 确认覆盖对象、环境和证据版本未改变且无
新的验证义务时，才标记 eligible repeated verification；仍不直接称无用。
把模型主动请求与恢复框架自动检查分开，避免把 runtime 检查归给模型。

Safety：skipped-required-verification、stale-state error、可观测 duplicate
effect，以及被保护机制拦截/安全停止数。效率改善必须与这些结果并列。

按任务给出五次结果及配对差值，再按四类汇总，最后给出等任务权重汇总。
不能仅用总 token 淹没高成本或困难类别。重复运行只估计固定前缀条件下的
波动；任务间泛化仍只有 12 个样本。可报告探索性的任务聚类区间，但不以
普通 120 独立样本检验声称显著或非劣。

“成功率不下降”是研究目标，当前规模不能保证证明。若要正式声称非劣，
须另行事先确定容忍差值与功效/样本量；本轮只报告差值和不确定性。

## 执行门槛

1. 接受当前路线和方法设计（fresh reviewer，仅方法验收）。
2. 构造并冻结 12 个具体 fixture、oracle、事件脚本和任务预算；完成新接缝
   的离线正/负例验证，保存原始日志。不得修改冻结恢复实现。
3. 新 fresh GPT-6.1 Sol medium 执行前审查完整合同和就绪证据。
4. 通过后运行 120 格；固定进度和原始数据，不中途优化 C-time。
5. 再由不同 fresh reviewer 验收原始结果和允许的结论。

此文不是“执行前已就绪”声明；没有新增实跑数据，也没有把旧结果重跑一遍。

## Fresh 方法审查与下一门槛

Fresh GPT-6.1 Sol（medium）结论：**ACCEPT WITH NOTES**，仅接受路线与方法。
原始意见在计划目录 `review-method-gpt61-sol.md`，未改写其结论。

执行前必须逐项落实 reviewer 的 substantive notes：

- 将检查发生（check_executed）与交付义务满足（valid_pass）分别记录。
  fail/uncertain 不能满足交付所需通过条件；独立核查 checker 结果、版本、
  输入绑定与之后的失效事件。最终正确性与验证义务分列。
- U1/U2 保存真实漂移事件及恢复路径，区分框架自动刷新和模型决策；U3
  提供冻结机制内可继续的具体状态证据，不能误用需要人工 resolve 的 unknown。
- 具体计算合同覆盖所有计划格：前缀失败分母、缺失 usage、skip 无完成时
  N/A、恢复成本与总轨迹组成；成功格成本仅作辅助，不能取代全体结果。

这些要求随 fixture/oracle 的实现进入新的执行前审查。本次方法 ACCEPT
不替代任务集就绪、实现验证或新实验结果验收。

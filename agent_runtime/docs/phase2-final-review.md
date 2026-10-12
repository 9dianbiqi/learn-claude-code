**ACCEPT WITH NOTES — 验收整个 Phase 2 的限定 runtime correctness 合同，不仅是 Increment 1–4。**

审查对象为工作区 `C:/Users/Lenovo/.codex/worktrees/runtime-correctness-phase2/learn-claude-code`，HEAD `43c8127fb490bafd6c956fb03335f2588d6255c7`，以及当前未提交的 Increment 2–4 测试和文档。本结论允许关闭 `phase2-final-acceptance.md` 所列、受 `phase2-correctness-increment1.md` 限界约束的正确性阶段；不表示形式化证明、任意故障覆盖或当前完整测试套件通过。

**Blocker：无。Substantive：无。Nonblocking：历史执行证据完整性、分步验证和保证边界，详见下文。**

本次为 fresh、独立、只读审查。实际检查了当前实现、测试断言、原始失败和通过日志、增量 review、manifest、进程 JSONL、SQLite/JSON 快照以及历史 Git 源码；未修改文件、执行测试/runtime、调用模型或启动其他 agent。所有 SQLite 连接均使用 `mode=ro&immutable=1`。

证据身份与原始数据检查成立：

- HEAD 与候选记录相符，未提交项只有七个测试/文档文件，没有后续生产修改。
- 四个增量索引的 **252 份证据文件**全部哈希匹配：I1 51、I2 101、I3 47、I4 53。这是文件数，不是独立实验数。
- I1 最终生产和测试身份由原 review manifest 与 narrow repair manifest 组合成立；Runtime/Store 与旧 manifest 不同是已记录修复，当前内容匹配修复 manifest。其他相关源文件匹配各自适用的 manifest。
- I2–4 当前测试源码均匹配归档源码哈希，包括 I4 使用的 I3 helper。
- I2–4 的 **39 份 SQLite 备份**完整性均为 `ok`，选定表逐项与对应 JSON 一致；I1 六个修复案例数据库完整性也均为 `ok`。
- 实际日志分别记录 I2 `5 passed in 40.59s`、I3 `2 passed in 10.17s`、I4 `2 passed in 8.01s`，退出码为 0。I1 的原始失败、修复中间失败及分步通过记录均保留。

最低合同的整体判断如下：

| 要求 | 独立判断 |
|---|---|
| 状态机与 ready scheduling | selector/projector 排除依赖未完成的旧 active/retryable/verifying 节点；verifying 延后后要求新 completion request，预算保留。review checkpoint 在事务内保留批次 owner；未知 owner 和被阻塞 owner 都 fail closed。六个修复数据库支持 owner 保留、零提前 dispatch 和 lease 释放。 |
| Checkpoint/verified authority 原子性 | `BEGIN IMMEDIATE` 事务将 verifier run、semantic checkpoint、lifecycle、PlanItem 和最终任务/checkpoint 状态一起提交；lease 在事务进入及提交前检查。F2-pre/F2-mid/F3 的真实进程退出测试及原始日志支持所选边界的 all-or-none 合同。 |
| Lease/fencing 与并发恢复 | I2 实际不同 PID 的竞争 admission、旧模型响应返回和活/死 reservation 场景成立。旧 owner 得到 `LeaseLost`，checkpoint/event 探针被拒绝，heartbeat 为 false，release 未释放新租约。真实活 effect owner 阻止接管，死亡后 opaque outcome 进入 unknown/review。 |
| Safe replay 与副作用 | 权限授权与效果分类分离；built-in shell 为 opaque `unknown_write`。committed dedup、prepared 与 dispatched/unknown 的恢复语义不同。I2 真实追加仅 dispatch 一次；file matching post-state 无第二次 write，第三状态漂移保持外部字节并进入 review。 |
| PlanRevision 与 decision/patch 原子性 | 当前 activation 的 execution binding、失败任务重开状态、checkpoint 和 audit 同属 guarded transaction。controller 在同一事务中调用 `_apply_plan_patch_conn`，追加 revision、切换 Plan pointer、写 decision/audit；重复 signal 返回既存 outcome，activation 仍为独立显式边界。历史证据限制见下文，但当前代码结构支持该合同。 |
| 持久验证义务 | recovery 位于 completed fast path 之前；失效事务递归撤销完成后代的 authority，并保留 consumed turns。I3 提交前退出保留旧态；提交后退出保留完整失效态；两种恢复都重新验证 A/B/C，independent 原有效证据保留，最终预算 A=3、B=2、C=2、independent=1。 |
| 良性刷新 | I4 提交前/后退出分别保留完整旧态/新态；恢复只留下每节点一个当前 valid authority，旧 A 为 superseded，历史行保留。依赖和预算不变，恢复零模型调用，tool_calls/operations 为空。 |
| 外部冲突 | read_file 返回内容与同一 handle 捕获的 hash/identity 绑定；后续 precondition 检查和恢复第三状态冲突符合已定义合同。未将 hash check + replace 误作 arbitrary-writer CAS。 |

跨增量没有发现破坏上述合同的交互。I2–4没有改动生产实现；I1最后两项修复分别收紧 pending intent 和 legacy adoption，未改变 verified authority、activation、decision/patch 的事务。恢复依次在 lease 下处理 revision/configuration、证据有效性和执行选择；依赖变化不会通过 approve/retry 绕过共享 pending guard。证据失效后的后代重新进入未完成状态，ready selector 与最终 evidence rescan共同防止旧完成状态被直接当作终局。

**历史 PlanRevision 原始日志缺口不阻断本次限定验收，但必须保留为非阻断说明。**

我独立重算了 provenance 中五个历史函数的 AST 比较，均一致，并阅读了当前 activation、controller transaction、patch 实现及对应真实退出测试。额外函数比较显示：相对 C1c 历史提交，`replanning.py` 和 `verified_evidence.py` 的函数 AST没有变化；Store 的函数变化为 I1 已审查的 `transition_review`，Runtime 的变化集中于已记录的 I1 scheduling、admission 和 tool recovery 路径。

因此，本次不是仅以五个 AST hash代替调用路径检查。版本化历史文档明确报告 C1b/C1c 的执行与独立 review，当前事务实现和相关测试仍可检查，未发现合同失效路径。原合同没有规定最终关闭必须为每一历史要求重新取得当前 raw run，也没有规定全组合最终测试执行。因此不能仅因原始日志未找到而新增这一验收门槛，或推断实现有 bug。

同时，这些材料仍只能称为**历史执行/review报告及源码连续性支持**，不能称为当前 raw执行、完整历史环境重现或当前全套测试通过。AST连续性不能补回缺失日志，不能证明全部 fixtures、依赖和环境相同。

本次验收必须随结论保留以下 nonblocking notes：

1. **分步执行限制。**41个不同场景是各开发步骤的通过记录；I1前26个及最后修复中的部分通过项未在最终树重跑。本审查没有改变这一事实，也没有宣告旧完整 suite 当前通过。
2. **故障与并发范围。**支持指定进程退出边界、受控 OS 进程交错及注入 expiry；不证明真实 TTL 全路径、全部交错、断电、存活子进程取消或 hung-owner 可用性。
3. **外部效果范围。**没有通用 exactly-once、SQLite/文件系统联合事务或任意外部 writer CAS；verifier 返回后文件仍可被外部修改。
4. **义务范围。**验证义务指现有结构、bundle绑定和authority lifecycle。PlanPatch不证明自然语言要求在 split/tombstone 后语义守恒；内部 Store 是可信 primitive，而非独立承诺全部 Runtime 前置条件。
5. **归档范围。**原始数据库和日志目前为本地材料；提交/tag本身不包含它们。正式关闭记录应继续指向现有 manifest 和原始证据，保留 REJECT、中间失败及历史日志缺口。

无需生产修复或新增测试才能接受本候选。后续封板只需准确记录本次 **整体 Phase 2、限定合同的 ACCEPT WITH NOTES**，保存原审查意见及证据指针，并避免扩大验证主张。

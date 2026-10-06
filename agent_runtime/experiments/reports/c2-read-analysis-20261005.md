# C 重复读取审计与 Resume Manifest 设计建议

状态：分析和设计提案，尚未实现或启动新的模型实验。只使用有效批次
`D:/opencv/learn-claude-code/.scratch/bc-repeat-20261005-run02/`。

## 项目位置

- GitHub Issue #18 已 CLOSED/ACCEPTED（2026-10-04），C1c accepted commit
  `b0aec3d`；该 issue 明确产品分支尚未合并 C1c。当前工作区继承该提交，
  README 中“验收待定”的历史文字落后于 issue 状态。
- 当前分支 `codex/bc-recovery-pilot` 的本地独立修复 `ebb308e` 已通过上一阶段
  fresh reviewer 的 ACCEPT WITH NOTES，但未推送/合并。
- C 已具备结构化 scoped bundle，不是“只有摘要”。它包含全局约束、当前
  子任务和验收条件、依赖完成摘要与证据 manifest、恢复原因、文件 hash/大小。
- Epic #13 的正式 **C2 = recovery diagnosis and routing**，C3 为自动分解和
  replan proposal。因此用户提出的 C2 在本文仅指**实验 C2（C+Manifest）**，
  不替代或宣告交付正式产品 C2，不变更既有 roadmap。

项目来源：https://github.com/9dianbiqi/learn-claude-code/issues/13 和
https://github.com/9dianbiqi/learn-claude-code/issues/18。
本次只读查询副本保存于分析目录 epic13.json、issue18.json。

## 六次读取的可观察事实

| 重复 | 任务 | 复读文件 | 复读调用 | 前一步 | 复读后 |
|---|---|---|---:|---|---|
| 2 | duration | settings.py | 3 | write_file 成功 | 第 4 次提交正确标记 |
| 2 | inventory | inventory.py | 3 | write_file 成功 | 第 4 次提交正确标记 |
| 3 | duration | settings.py | 3 | write_file 成功 | 第 4 次提交正确标记 |
| 3 | inventory | inventory.py | 3 | write_file 成功 | 第 4 次提交正确标记 |
| 4 | duration | settings.py | 3 | write_file 成功 | 第 4 次提交正确标记 |
| 5 | duration | settings.py | 3 | write_file 成功 | 第 4 次提交正确标记 |

六次都满足：

1. 先读当前文件和 parser，再写当前文件，再读刚写过的当前文件。
2. 复读前的模型上下文保留完整 write_file 参数（全部源码）和成功工具回执。
3. runtime trace 中该写入为 succeeded、effect_confirmed=1；代码在写后 hash
   匹配后才提交 operation 和文件观察。
4. 复读返回文本与前次 write_file 的 content 完全一致，没有发现内容差异。
5. 复读后没有任何工具调用或修补，直接提交 marker；最终验收通过。
6. 请求该复读的 assistant 回复没有解释理由，仅有 read_file 调用。

这些是“写后回读”的证据，**不是模型丢失信息的因果证据**。读取没有带来
新的源码文本，但提供了一次对磁盘状态的独立观测；不能因此判为没有价值。

应区分另外两个统计：C 读取已完成 parser 10 次、B 5 次，这是恢复后文件
发现/接口确认，不能混作本次六次复读；B 也有一次写后读取，只是该路径
恢复后尚未读过，不计入“恢复后重复读取”。C 比 B 总共多 7 次调用；从调用
数量看，C 的六次第 3 步读回占六个调用，另一个差值对应一次 B 仅用两次
调用完成。不能把所有 token 差值都因果归给这六次行为。

## 哪类信息可能缺失

| 候选信息 | 六次复读前的状态 | 判断 |
|---|---|---|
| 任务目标与验收条件 | global_constraints、resume_unit 和继续提示均存在 | 无缺失证据 |
| 已完成工作摘要 | parser 的 completion_summary 存在 | 无缺失证据，且复读对象不是 parser |
| 当前实现源码 | 刚写入的全部内容仍在消息尾部 | 不属于源码被裁掉 |
| 文件状态 | 基础 bundle 保留恢复时 hash/size；工具尾部表明已写新内容 | 有时间语义与状态呈现上的改进空间 |
| 决策依据 | 没有独立决策日志，但实现和完整近期工具消息均保留 | 无法由此次读取证明需要决策记忆 |
| 依赖验证证据 | dependency manifest、valid 状态、checkpoint id 存在 | 不是依赖验收证据整体丢失 |
| 当前写入效果证据 | runtime 已做 post-hash-match；模型只看到 Wrote N bytes | 可测试的候选信息增量 |
| 当前子任务语义验收 | marker 前尚未运行 integration verifier | 正常的“尚未验收”，不能伪造为 pass |

`scoped_resume.py` 的 `with_post_resume_messages` 保留同一个基础消息，追加
恢复后的消息尾部。因此这六次的 relevant_path_state 仍为初始 stub：
settings.py=60 bytes，inventory.py=54 bytes，而刚写入源码为 665–924 bytes。
这是有意复用恢复时快照的实现，不应直接称为数据库陈旧或恢复错误。
同理 remaining_turn_budget 的基础值也不是每步刷新后的实时余额。

目前可区分的假设：

- H1：初始快照和当前状态的时间界限不明确，使模型倾向于读回确认。
- H2：写入成功回执不显式表达 runtime 已核对写后 hash，模型再做一次确认。
- H3：scoped 的呈现角色/格式、历史示例缺失或采样变化影响了检查习惯。
- H4：模型正常选择自检，与缺失上下文无关。

H1/H2 有代码和信息差异依据；**还没有任何一项被因果确认**。不能依据这些
轨迹推断模型内部动机，也不应直接禁止 read_file。

## 最小 C+Manifest 设计

优先做现有 durable state 的只读投影，不新建一套权威状态数据库，不生成
新的模型摘要，不新增 planner/verifier，不改变 completion marker 合同。

建议区分两层：

```text
resume_anchor（不可变）
  task_id / execution_checkpoint_id / plan_revision_id / scope version
  dependencies: completion summary + verifier/checkpoint references
  file observations explicitly labeled “as of resume”

execution_state（截至当前 checkpoint 的有界投影）
  active item / actual remaining budget
  latest relevant committed file effects: tool_call_id, operation_id,
      path, committed_after_sha256, outcome=post_hash_match, source checkpoint
  current item verification: not_run | pass | fail | uncertain | stale
  next protocol transition: submit completion marker -> existing verifier
```

重点是“已提交的写入事实”和“代码语义正确”分开：

- `post_hash_match` 仅表示提交时读到的文件与预期写入一致，不等于测试通过，
  也不保证外部修改后磁盘仍未变。
- commit-time observation 必须携带来源；没有重新核对时不得标成 current
  disk truth。遇到 unknown effect、stale evidence 或内容漂移，保留现有
  fail-closed/复核路径，不用 manifest 推动 completed。
- 原子替换成功且 effect ledger 已提交之后才能投影 receipt，不能根据模型
  自述或仅工具计划推断成功。当前 PlanRevision/执行绑定不一致时遵从 C1b，
  不在投影层自行激活或修改计划。
- 在一致的读取视图中派生投影，绑定 checkpoint/revision；未知事实显式
  unknown。默认不为更新 manifest 额外全仓扫描或隐式重跑 verifier。
- 字段大小有上限，纳入原 scoped 预算；溢出遵守原 fail-closed 规则。
  当前源码不重复塞进 manifest，仍利用已有工具消息尾部。

实现接缝预计在 `scoped_resume.py` 的 bundle/消息渲染，以及 `runtime.py`
构造模型视图的调用点。效果来源复用现有 operation/tool-call/observation
记录，验收来源复用 verifier/verified-subtask 记录。初版不需要 schema
migration；缺少无法可靠派生的事实时留空，不先扩数据库。

## 下一步实验顺序（提案，未执行）

先冻结两个分离的干预，不把所有新字段一次加入：

1. **C-time**：只将基础字段明确标成恢复时快照，不增加写入效果信息，
   不加“不要读文件”的行为指令。测试时间语义澄清是否影响读回。
2. **C-receipt**：在 C-time 上投影最近一次相关、已提交的结构化写入效果
   receipt；保持语义验收 not_run 与原 marker 流程。测试额外效果证据。

先固定设计并由 fresh reviewer 审查，再实现、测试。下一批采用新的冻结
协议，交错运行同期 C / C-time / C-receipt，各条件同一快照、预算和工具，
可以先保持两题每格 5 次（30 次续跑），不把已有 C 数据当成无漂移的同期
对照。旧批次继续只读保留；本轮分析没有重跑任何模型实验。

主要指标为每条续跑的写后读取事件、读取后是否引发修补、总输入/输出、
模型调用及最终验收；记录可见 manifest token 开销，以及投影层自身的
hash/文件读取和耗时，避免把 LLM read_file 转移到宿主代码后宣称消除成本。
对能完成的所有格和失败格分别透明记录，不能只看成功格或只优化首次输入。

候选进入工程实现前，至少需覆盖 committed/pending/unknown 写入、文件漂移、
stale verifier、计划版本不匹配、预算耗尽和投影溢出。可使用确定性离线
机制用例检查这些分支；不能放宽生产保护以换取更低读取次数。

即使本组小任务显示改善，也只支持固定快照上的结果；通过后再扩长历史、
跨模块任务来确定摘要/接口/决策信息的作用。收益应以总成本及完成/安全
约束评估，不预设必须减少多少读取。当前不建议直接上 SWE-bench 或动
正式 C2 recovery router。

## 可复核证据

- 原始轨迹：有效批次各 `repeat-N/task/C/model_requests.jsonl` 第 3 条及
  `trace.jsonl` 中对应 write/read tool_use_id。
- 提取脚本与逐项证据：
  `D:/opencv/learn-claude-code/.scratch/bc-c2-analysis-20261005/audit_reads.py`、
  `read-audit.json`（含原始文件位置、SHA-256、可见内容/回执、effect 确认与
  文本相等检查）。
- 代码依据：`agent_runtime/scoped_resume.py` 的 build、with_post_resume_messages、
  _dependency_snapshot；`agent_runtime/runtime.py` 的 scoped_projection 复用和
  文件写入 post_hash_match commit 路径。

## 独立分析/设计审查

Fresh GPT-6.1 Sol（medium）结论：**ACCEPT WITH NOTES**，无 blocker 或
substantive issue。此验收仅覆盖分析和候选设计，不代表实现或新实验完成。
完整原始意见保存在分析目录 `review-gpt61-sol.md`。

下一轮冻结时必须落实 reviewer 的三项限制：

1. 保存精确渲染差异：C-time 仅加快照时间标签；C-receipt 在其上仅加既定
   receipt，不夹带总体候选中的动态预算、决策摘要或新行为提示。
2. receipt 增量对照不能进一步分离 hash 证据、额外文字、呈现显著性，也
   不能反推旧批次模型动机；没有改善不等于否定长任务效果。
3. 若后续扩展动态预算，明确预留前/后时点；receipt 的效果提交来源与本次
   投影 checkpoint 分开记录，不把旧观察渲染成当前磁盘事实。

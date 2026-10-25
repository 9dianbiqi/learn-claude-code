# 历史写入证据三条件 pilot — 2026-10-05

状态：Fresh GPT-6.1 Sol（medium）独立结果审查 ACCEPT WITH NOTES；无 blocker 或 substantive issue。

30 个新续跑格：两份原始 F3 快照 × C/C-time/C-receipt × 五次。同期 C 为对照，不合并旧批次。

| 指标 | C | C-time | C-receipt |
|---|---:|---:|---:|
| 完成并通过验收 | 10 | 10 | 9 |
| 恢复后输入 token（含缓存） | 88,719 | 68,248 | 93,959 |
| 恢复后输出 token | 6,874 | 6,561 | 8,085 |
| 恢复后输入 token 中位数 | 7982.5000 | 5642.5000 | 5823.0000 |
| 恢复后模型调用 | 43 | 35 | 41 |
| 恢复后工具调用 | 42 | 34 | 41 |
| 有后续决策机会的已提交写操作 | 12 | 11 | 12 |
| 标记格式错误 | 1 | 1 | 2 |
| 无标记文本回复 | 0 | 0 | 0 |
| 标记与工具混用 | 0 | 0 | 0 |
| 实际向模型暴露 receipt 的写操作 | 0 | 0 | 12 |
| 关联历史证据的写后读取请求 | 10 | 3 | 5 |
| 发生再观测的写操作 | 10 | 3 | 5 |
| 再观测强度（可大于1） | 0.8333 | 0.2727 | 0.4167 |
| 再观测操作比例（0–1） | 0.8333 | 0.2727 | 0.4167 |
| receipt 暴露条件下强度 | N/A | N/A | 0.4167 |
| 写后读后又发生同路径写入的事件 | 1 | 0 | 1 |
| 父进程 workspace read-open | 236 | 214 | 226 |
| 投影新增 workspace read-open | 0 | 0 | 0 |
| 投影只读 ledger 事务 | 43 | 35 | 41 |
| 投影总耗时/秒 | 0.1820 | 0.1413 | 0.1775 |
| 恢复总耗时/秒 | 68.4369 | 61.8101 | 71.1234 |
| 恢复耗时中位数/秒 | 6.4245 | 5.9761 | 6.1453 |
| integration verifier 调用 | 10 | 10 | 9 |
| integration verifier 非pass | 0 | 0 | 0 |

## 累计口径与逐格结果

下表把原始 prefix 分别计入每条假想完整轨迹。prefix 本轮没有执行；这些累计数不是本轮真实新增支出。

| 条件 | prefix＋continuation 输入 | 输出 | 模型调用 |
|---|---:|---:|---:|
| C | 130,969 | 17,474 | 83 |
| C-time | 110,498 | 17,161 | 75 |
| C-receipt | 136,209 | 18,685 | 81 |

| 任务 | 重复 | 条件 | 输入 | 模型调用 | 再观测请求/写操作 | 验收 |
|---|---:|---|---:|---:|---|---|
| duration | 1 | C | 17710 | 7 | 2/3 | True |
| duration | 1 | C-time | 5760 | 3 | 0/1 | True |
| duration | 1 | C-receipt | 5745 | 3 | 0/1 | True |
| inventory | 1 | C-time | 5489 | 3 | 0/1 | True |
| inventory | 1 | C-receipt | 8513 | 4 | 1/1 | True |
| inventory | 1 | C | 8235 | 4 | 1/1 | True |
| duration | 2 | C-time | 5586 | 3 | 0/1 | True |
| duration | 2 | C-receipt | 5867 | 3 | 0/1 | True |
| duration | 2 | C | 5261 | 3 | 0/1 | True |
| inventory | 2 | C-receipt | 33290 | 11 | 2/3 | True |
| inventory | 2 | C | 7678 | 4 | 1/1 | True |
| inventory | 2 | C-time | 5568 | 3 | 0/1 | True |
| duration | 3 | C-receipt | 8792 | 4 | 1/1 | True |
| duration | 3 | C | 8313 | 4 | 1/1 | True |
| duration | 3 | C-time | 7782 | 4 | 1/1 | True |
| inventory | 3 | C | 7688 | 4 | 1/1 | True |
| inventory | 3 | C-time | 5554 | 3 | 0/1 | True |
| inventory | 3 | C-receipt | 3363 | 2 | 0/0 | False |
| duration | 4 | C | 7977 | 4 | 1/1 | True |
| duration | 4 | C-time | 5670 | 3 | 0/1 | True |
| duration | 4 | C-receipt | 5722 | 3 | 0/1 | True |
| inventory | 4 | C-time | 10616 | 5 | 1/2 | True |
| inventory | 4 | C-receipt | 5696 | 3 | 0/1 | True |
| inventory | 4 | C | 7649 | 4 | 1/1 | True |
| duration | 5 | C-time | 10608 | 5 | 1/1 | True |
| duration | 5 | C-receipt | 11192 | 5 | 1/2 | True |
| duration | 5 | C | 10220 | 5 | 1/1 | True |
| inventory | 5 | C-receipt | 5779 | 3 | 0/1 | True |
| inventory | 5 | C | 7988 | 4 | 1/1 | True |
| inventory | 5 | C-time | 5615 | 3 | 0/1 | True |

## 解释边界

- 再观测强度不是“可避免浪费率”。读取可能发现改进点或确认另一个时点的磁盘状态。写后读与后续编辑的关联是事后描述，不是因果证明。
- runtime 持有 receipt 与模型实际收到 receipt 分开计数；C/C-time 的 exposure-conditioned 指标为 N/A。分母只覆盖存在后续模型决策机会的提交操作，不能等同所有历史写入。
- 宿主指标是父进程数据文件 read-open，不是总 OS I/O 或读取字节；不包括 verifier 子进程、DB、准备和最终grading。DB事务另报，不能把查询视为免费。
- 历史证据表示提交时post-hash-match，不代表当前磁盘状态、语义验收或未来无漂移。effect checkpoint与tool-result execution checkpoint不是同一个原子事务。
- 两题五次重复不能证明总体性能、统计显著性或长程泛化。默认温度与模型别名不能保证服务端权重不变。新增文字长度、呈现显著性和hash信息的效应不能由三条件完全分离。

## 实施与审查记录

- 生产runtime、任务/verifier和既有marker合同未改；实验仅在模型入口追加预定可见文本。快照保持文件身份、DB独立复制、每格校验原快照未变。
- 执行前第一位fresh reviewer REJECT：发现再观测窗口错误地在写请求时关闭。该意见原样保留。
- 仅修正分析器，依据持久化committed记录关闭窗口；新增六项针对性用例通过。先前已通过的12项离线测试没有因用户续跑指令而重复执行。
- 第二位fresh GPT-6.1 Sol（medium）ACCEPT WITH NOTES后才开始实跑。原始pytest日志与两份原始审查在验证目录。

## 证据位置

- 当前结果：`D:/opencv/learn-claude-code/.scratch/receipt-pilot-20261005-run01`：protocol/results/summary、frozen-source、全部projection/model_requests/result/trace记录。
- 执行前验证：`D:/opencv/learn-claude-code/.scratch/receipt-pilot-20261005-validation/`：pytest-final.log、pytest-window-fix.log、review-prerun-1.md、review-prerun-2.md。
- 原始F3：`D:/opencv/learn-claude-code/.scratch/bc-pilot-20261004-run02/`。归档含共享硬链接，应只读。

## 逐格异常记录（保留在总体结果中）

- repeat-2/inventory/C-receipt: status=completed; error=None; marker_format_errors=2; eligible/exposed writes=3/3.
- repeat-3/inventory/C-receipt: status=needs_review; error=File precondition conflict for inventory.py: Text not found in inventory.py; marker_format_errors=0; eligible/exposed writes=0/0.
- repeat-5/duration/C-time: status=completed; error=None; marker_format_errors=1; eligible/exposed writes=1/0.
- repeat-5/duration/C: status=completed; error=None; marker_format_errors=1; eligible/exposed writes=1/0.

repeat-3/inventory/C-receipt 在尚未暴露任何 receipt 时停止：edit_file 的 old_text 使用 LF，初始文件和 read_file 回执包含 CRLF，精确文本匹配失败，runtime 返回 needs_review。该格不剔除、不重跑；不能将这个曝光前失败解释为 receipt 内容造成的正确性退化。

repeat-2/inventory/C-receipt 的 11 次调用包含两次非独占行完成标记、额外修改与 glob 请求；它完整保留，不因高成本而排除。

## 描述性观察

同期 C-time 相对 C 的恢复后输入变化 -23.1%；C-receipt 相对 C-time 变化 +37.7%。本组数据更支持优先继续验证时间标签，而没有证明 receipt 增量有净收益。

不能据此认定历史效果证据无用：两题样本小，receipt 组含一条标记格式长轨迹与一条曝光前失败。总量和中位数同时列出，所有格保留。也不能把 C-time 的变化唯一归因于文件时间语义，新增说明的文字与显著性作用尚未单独隔离。

## 独立结果审查补充

原始审查意见：结果目录 `review-results-gpt61-sol.md`；来源记录与哈希：`acceptance.json`。reviewer 独立检查全部原始记录，没有重跑测试或模型实验。

| 任务：恢复后输入 | C | C-time | C-receipt |
|---|---:|---:|---:|
| duration | 49,481 | 35,406 | 37,318 |
| inventory | 39,238 | 32,842 | 56,641 |

C-time 两题均较 C 低；receipt 相对 C-time 的输入增量主要在 inventory。父进程 read-open 包含以可读写模式打开的临时文件，不等于实际 read 次数或源文件验证次数。曝光前失败不能归因于 receipt 内容，但该条件已有时间说明，不能由此证明整个条件对失败完全无影响。

此验收仅覆盖本批实施、记录、指标与描述性报告，不代表正式产品 C2 已交付，也不证明总体性能收益或因果机制。

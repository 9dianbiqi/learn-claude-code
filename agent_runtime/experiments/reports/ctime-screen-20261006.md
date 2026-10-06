# C vs C-time：32 格工程 screening

状态：fresh GPT-6.1 Sol（medium）独立结果验收 ACCEPT WITH NOTES。

8个新合成任务、4类各2题，每条件2次。共同前缀为脚本构造并真实F3退出，不是自主生成前缀；本轮不包含旧pilot。

| 核心指标 | C | C-time |
|---|---:|---:|
| 独立oracle任务成功/16 | 16 | 16 |
| 恢复后输入token（含缓存） | 157604 | 178039 |
| 真实模型调用 | 83 | 91 |
| 写后再观测请求 | 21 | 19 |
| 有后续决策机会的已提交写操作 | 26 | 26 |
| 再观测强度 | 0.8077 | 0.7308 |
| 完成但遗漏必需验证的运行 | 0 | 0 |

## 逐任务结果

| 任务 | 类别 | C成功/2 | C-time成功/2 | C平均输入 | C-time平均输入 | C再观测/写 | C-time再观测/写 |
|---|---|---:|---:|---:|---:|---|---|
| S1 | single-file | 2 | 2 | 6526 | 5574.5 | 2/2 | 1/2 |
| S2 | single-file | 2 | 2 | 6614 | 8034 | 2/2 | 2/2 |
| M1 | multi-file | 2 | 2 | 4893.5 | 9048.5 | 0/4 | 1/4 |
| M2 | multi-file | 2 | 2 | 8839.5 | 9281.5 | 4/4 | 2/4 |
| P1 | multi-stage | 2 | 2 | 15356 | 16854 | 3/4 | 3/4 |
| P2 | multi-stage | 2 | 2 | 16786 | 18501 | 3/4 | 3/4 |
| U1 | external-state | 2 | 2 | 14274.5 | 16093.5 | 6/4 | 6/4 |
| U2 | external-state | 2 | 2 | 5512.5 | 5632.5 | 1/2 | 1/2 |

## 类别分层

| 类别 | C成功/4 | C-time成功/4 | C平均输入 | C-time平均输入 |
|---|---:|---:|---:|---:|
| single-file | 4 | 4 | 6570 | 6804.25 |
| multi-file | 4 | 4 | 6866.5 | 9165.0 |
| multi-stage | 4 | 4 | 16071 | 17677.5 |
| external-state | 4 | 4 | 9893.5 | 10863.0 |

## 预先固定的扩展门槛

输入下降任务：1/8；再观测强度下降任务：2/8。完整可解释数据：True。机械计算扩展 gate：False。独立结果reviewer已确认该gate未通过。

只有满足协议的完整数据、成功不减、无新增验证遗漏、至少5/8题输入更低且可比任务严格多数再观测更低时，才考虑扩展。这个工程门槛不是统计显著性或非劣性检验。未达门槛不自动补跑，不将异常格剔除。

## 失败与状态路径

- U1/repeat-1/C: successful framework refresh=0; model calls=9; success=True.
- U1/repeat-1/C-time: successful framework refresh=0; model calls=9; success=True.
- U2/repeat-1/C-time: successful framework refresh=1; model calls=3; success=True.
- U2/repeat-1/C: successful framework refresh=1; model calls=4; success=True.
- U1/repeat-2/C-time: successful framework refresh=0; model calls=9; success=True.
- U1/repeat-2/C: successful framework refresh=0; model calls=8; success=True.
- U2/repeat-2/C: successful framework refresh=1; model calls=3; success=True.
- U2/repeat-2/C-time: successful framework refresh=1; model calls=4; success=True.

## 解释与证据范围

- 任务成功要求 runtime完成、私有最终语义检查通过、义务审计无遗漏；不是completed自证。失败时未满足义务为unresolved，skip率不以失败格填零。
- 框架自动刷新与模型动作分开；C-time只标记历史snapshot时点，不证明当前无漂移。
- 8个任务而非32独立任务；断言覆盖有限、前缀为脚本、每题一个F3位置、模型别名与默认温度不保证总体泛化。
- 再观测不自动意味着浪费；当写后阅读后又修补，应从原始轨迹解释。
- 前缀零模型API费用；预算仍计入三轮脚本harness上下文，主要成本仅真实续跑。缺失usage记未知并保留已知部分；无新增旧实验或旧测试重跑。

原始结果：`D:/opencv/learn-claude-code/.scratch/ctime-screen-live-20261006-run01`；protocol/results/summary、sources中的F3快照、每格model_requests/projection/result/trace/events和冻结源码。
执行前测试与原始review：`D:/opencv/learn-claude-code/.scratch/ctime-screen-validation-20261006/`。

## 工程决策

C-time 恢复后输入相对 C 变化 +13.0%，模型调用增加 8 次；只有 1/8 任务平均输入降低，2/8 再观测强度降低。未满足预定扩展门槛。独立记录核对已通过，本轮停止扩展，不补至80、不扩至120，不为追求预期降幅改写C-time。保留候选和原始数据，不据此声称时间语义在所有任务上无效。

## 独立结果审查与补充说明

无 blocker 或 substantive finding。原始完整审查在结果目录 `review-results-gpt61-sol.md`；验收和候选决策分别保存于 `acceptance.json`、`decision.json`。reviewer 没有重跑模型或测试。

- U1 的“成功刷新=0”不代表无框架动作：四格均先发生失败的证据刷新检测，再由模型修复；U2 四格先由框架成功刷新。
- 有4次自然 glob 拒绝：M1/repeat-1/C-time一次、U1/repeat-1/C两次、S2/repeat-2/C-time一次。所有成本与调用保留。
- 任务等权平均恢复输入：C 9,850.25，C-time 11,127.4375；任务等权再观测强度：0.8125 与 0.71875。它们不改变扩展门槛判断。
- 本批没有大小写别名写入；oracle 的该路径边界仍是未来版本需处理的限制。零skip说明系统gate保留了验证，不证明模型自主选择了充分检查。

本次停止推进C-time扩展，不补到80、不扩到120；保留候选与全部原始证据。旧pilot和offline不混入本结果，也不为了寻找预期降幅重跑或调整提示。

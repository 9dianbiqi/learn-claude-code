# Phase 2 后续扩展：限定验收与收口

日期：2026-10-17。**整体 Spec 独立审查：ACCEPT WITH NOTES。Standards：无硬性阻断，2 项非阻断维护建议。**

原 Phase 2 Runtime Correctness 已于 2026-10-12 独立验收并封存；见 `phase2-final-acceptance.md` 和 `phase2-final-review.md`。本轮不重新定义其合同、不重验或覆盖旧验收。本次关闭的是从 `68e4e51ca9cc4049f575d617762f89934e443baa` 起的后续实验扩展，不可称为“泛化 Phase 2 全部要求完成”。

## 验收范围与证据

| 要求 | 结果与证据 |
|---|---|
| 模型 stop 不直接等同任务成功 | 统一 `_finish_model_turn`，任务类型感知、有限停止检查、明确放弃/澄清/未解决/预算终止；31 项新增离线及进程测试的分步记录 |
| 复用原 Verifier 与完成权威 | 通过原 `_handle_verified_marker` / `commit_verified_subtask` 事务；无第二套成功状态；原 F2/F3 边界增量证据 |
| 可信读取错误给出确定性恢复建议 | PATH_NOT_FOUND、替代定位策略、原参数不应重试、配额与持久化重放；不新增模型请求 |
| 外部工作区修复证据 | 冻结 scope 覆盖验证与提交；精确 patch/revision/probe 绑定；本地探针不能满足官方 SWE 合同 |
| 实例容器执行与隔离 | 12 项后端测试、5 个真实容器离线场景；非 root、无网络/宿主挂载、固定镜像、受控命令和输出/时间限制 |
| 崩溃与租约恢复 | 15 项新增测试、7 个真实进程/容器窗口；5 个同源新租约恢复，2 个拒绝自动恢复；任务权威原子性与原日志保留 |
| 验证结果与释放结果分离 | 8 项新增测试、6 个真实容器故障场景；释放失败不误报成功清理，不覆盖已提交验证结果；不盲目重试 |
| 清理待办不能通过新后端绕过 | 18 项新增测试分两批、2 条真实进程路径；现有副作用预留表、统一取租约入口与可信精确删除协调 |
| 独立审查发现的问题完成闭环 | 初审发现 validator 删除失败没有 blocker，先红灯复现，再通过 1 项主回归、4 项 parent drain 隔离测试及 1 个新增真实容器场景；复审关闭阻断 |

各行是不同开发时点的证据，不是最终代码一次性全套执行，也不应简单相加作为最终树测试成绩。旧测试矩阵、模型实验及 Harness 没有为收口重新运行。

## Standards

独立审查 `/root/standards_review`：无已确认的文档规范违规、无硬性阻塞。两项建议均为启发式维护建议：证据写入与恢复日志的原子写入逻辑有重复；资源身份字典在后端/Store/投影校验间传递并重复检查。当前不为消除这些非阻断建议进行无关重构。

原意见：证据包 `reviews/standards-original.md`。审查仅只读，不运行测试或修改实现。

## Spec

独立审查 `/root/spec_review` 对整个限定 successor 先返回 **REJECT pending repair**，唯一阻断为 validator 删除失败：删除异常会跳过 reader 回收和 cleanup 日志，且遗留验证容器不形成仓库 blocker。

已修复：

- 删除验证容器前登记精确 `docker_validator` 资源预留；异常后保留 unknown，始终进行有界 reader join 并记录清理结果。
- 在同 task、owner、fence、engine、session、父 source 匹配时，允许释放已有冻结父资源，避免父容器无关滞留；不会关闭子资源 blocker，也不允许新任务绕过。
- 新增真实 Docker 故障注入中，验证命令成功但 validator 删除被注入 APIError：任务最终为 `VERIFICATION_UNCERTAIN`，0 成功检查点；父容器及写入恢复、reader 退出，新 Runtime 被阻塞。容器仍存在时不能协调；真实移除该 validator 后，同引擎完整 ID 的 404 才解除。

5 个不同新增回归分主用例 1 项、隔离用例 4 项通过，红灯日志保留。新增真实容器场景通过。该 SDK 故障为注入，不是观察到真实 daemon 删除故障。

复审核对实际实现、Trace、预留、cleanup 和 result，明确给出 **整体 successor ACCEPT WITH NOTES**，且 narrow repair 也为 ACCEPT WITH NOTES。修复前、修复后及整体意见分别保留，未用补丁通过自行代替独立验收。

原意见：`reviews/spec-before-repair.md`、`reviews/spec-repair-review.md`、`reviews/spec-overall-final.md`。

## 必须保留的 notes

1. 接受本表限定的 Runtime 正确性与资源管理合同，不扩展到任意 daemon/断电/磁盘错误、所有并发交错、路径别名和独立控制器域。
2. 清理阻塞依赖共享数据库和同一仓库标识；人工协调为可信控制器接口，没有通用自动协调服务或 CLI/UI。内部 Store 与 Docker 管理端点仍属信任前提。
3. 显式冻结恢复与释放中故障不同；需人工处置的状态不描述为自动恢复。新资源类型需要配套投影校验实现，旧二进制不可无条件读取新实验数据库。
4. 独立 reviewer 没有重跑测试；分步测试、证据核对不等于最终整套回归。两个旧兼容用例在原基线同样失败，仍保留：`test_blocked_completion_marker_gets_fixed_feedback_without_verifier_run`、`test_blocked_marker_can_continue_after_dependency_becomes_complete`。本轮未改写/跳过/重跑它们来制造全绿。
5. 真实模型策略恢复、Agent 自主修复和官方 SWE-bench 成功不在接受范围，仍缺少对应新证据。Patch、探针通过和离线 ScriptedModel 都不替代这些结果。
6. 独立标准审查和 Spec 审查是两条轴：0 标准硬违规、2 非阻断建议；Spec 的 1 个阻断已修复并复审关闭。没有把两条轴合并排名或用一条轴掩盖另一条。

## 封存身份

实验工作树：`C:/Users/Lenovo/.codex/worktrees/completion-recovery-contract/learn-claude-code`，分支 `codex/completion-recovery-contract`，HEAD 仍为 `68e4e51ca9cc4049f575d617762f89934e443baa`，修改未提交。

独立审查期间及真实修复用例启动后未再改生产代码。最终只添加审查/收口文档和归档索引；不提交、不推送、不改原 Phase 2 工作树。

证据包：`D:/opencv/learn-claude-code/.scratch/phase2-successor-accepted-evidence-20261017.zip`。包含最终源码、累计/修复差异、审查原文、本轮红绿日志/数据库/Trace/容器记录，以及前五轮原始 ZIP 和身份链。归档脚本核对已有索引与 SHA-256，不把旧证据重新命名为新执行。

目标模型 API **0**、付费实验调用 **0**、API Token **0**、官方 Harness **0**。最终清理清单与原 Phase 2 tracked diff 核对写入 `acceptance-state.json`。

本轮到此完成限定扩展验收，不继续增加无边界故障测试或扩大 SWE-bench 规模。

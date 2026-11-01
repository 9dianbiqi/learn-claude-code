# 可做事项顺序收口 — 2026-11-01

本轮按既定顺序完成代码审查后的离线工作：证据报告、普通用户执行入口、环境准备检查/方案与技术说明。
真实 Docker、镜像构建/依赖安装、官方评分和远端 CI 仍未验收，未用模拟结果替代。

## 顺序与产出

| 顺序 | 本轮结果 | 入口 / 文档 | 证据边界 |
| --- | --- | --- | --- |
| 1. 整理并审查代码 | 上一轮已完成，保留其清单与 3 项修复；本轮接续未发布后续代码 | `unpublished-review-20261101.md` | 61 项是该轮源码范围的历史证据，本轮未复跑 |
| 2. 完善结果报告 | 新增 pin 既有证据、Markdown/JSON 报告、seal、分范围呈现与失败信息 | `python -m agent_runtime.evaluation_report MANIFEST NEW_REPORT` | 不执行评测、不混合分母、历史/模拟不升级为真实官方结果；scope 为控制端声明 |
| 3. 普通用户入口 | PowerShell 7 默认离线预检，可只读 HostCheck，显式 endpoint/Engine pin 后执行，并收集报告 | `agent_runtime/scripts/Invoke-SweAcceptance.ps1` | 不变更权限/默认 context，不重试旧输出；真实执行待用户终端可用 Docker |
| 4. 环境自动准备方案 | 新增资产/脚本预检、只读 Engine/image 检查、准备报告与剩余门槛；列明资产、依赖、资源和 Linux 官方要求 | `python -m agent_runtime.swe_prepare CONFIG FUTURE_RUN`；`swe-operator-guide-20261101.md` | 当前自动化检查已准备资产；没有实现通用镜像构建/依赖安装器，不声称任意实例就绪 |
| 5. 项目技术说明 | 已写中文架构、故障恢复、工具安全、评测分层、面试表达及版本范围 | `runtime-technical-brief-20261101.md` | 计划、离线结果与真实验收分别说明 |

## 关键实现

`evaluation_report.py` 只读取清单指定、SHA-256 固定的既有 JUnit/suite/candidate/acceptance/official 结果。
输出源路径、错误、独立统计、报告与 seal，拒绝未知 kind、篡改输入和旧输出覆盖。
模拟 official_resolved 字段不改变声明范围；缺少真实/官方证据时明确显示 unverified。
报告可信性依赖提供清单的控制端，哈希本身不认证来源，也不证明结果真实性。

`Invoke-SweAcceptance.ps1` 从任意目录切换到活动代码入口。默认预检不访问 Docker。
HostCheck 把选定 Docker CLI context 的实际 endpoint 显式传给 SDK；Execute 要求 Engine pin，
由 provision 在创建容器前核对。结果收集失败与 manual_recovery_required 返回非零退出码。

`acceptance.load_config` 对控制配置字段和绝对资产路径进行校验，拒绝 JSON 注入执行开关。
CLI 要求显式 endpoint/Engine ID；官方执行要求 Linux Python。
CLI 将显式 endpoint 临时传递到官方评分子进程环境，同时移除继承的 TLS 变量，
退出时恢复 Python 进程原环境。直接 API 调用者仍需显式保证 grader 子进程环境对应正确 endpoint。
远程 TLS 不是普通用户本地 wrapper 的能力范围。

`swe_prepare.py` 默认离线校验，`--host` 只调用 info 和本地 images.get。
它不创建容器、不运行 probes、不下载镜像、不安装依赖。
`PREPARED_ASSETS_AND_HOST` 仍附带 probes、实际生命周期和官方评分待办，不等于真实验收通过。

## 验证

按改动依赖分批执行：

| 批次 | 实际通过 | 作用 |
| --- | --- | --- |
| report | 4 | 初版报告的篡改、模拟范围、失败/跳过、覆盖保护 |
| entry | 15 | 准备检查、显式 endpoint/pin、CLI/PowerShell 预检、配置约束，以及 2 项受影响既有集成 |
| final | 9 | 未知报告 kind 修复及相关报告检查；PowerShell 无 pin 拒绝执行和退出码；相关 CLI |
| endpoint | 1 | 最后 endpoint/TLS 子进程继承与恢复改动后，只重跑这一相关检查 |

所有批次 0 failure/error/skipped。批次有重叠，不能相加为独立场景数。
按语义 node ID 去重为 21 项（19 个本轮新增/新增参数场景、2 个受影响既有检查）；
原 Windows Preflight 测试后来参数化，将旧名字映射到 `[Preflight-0]` 后去重。
这不是最终源码的单次全量测试声明；后续小改动只重跑受影响检查，范围记录在各批次。

证据在仓库根目录 `.scratch/offline-followthrough-20261101/`：
`*-junit.xml`、`*-tests.log`、`validation.json`、`inventory.json`、`source-snapshot-final.zip`。最终汇总报告位于并列目录
`.scratch/offline-followthrough-report-20261101/report.md` 和 `report.json`。
初次报告输出因与证据输入目录重叠而被拒绝，保留原错误日志，改用并列新目录后成功；未重跑评测。
本轮检查使用已有依赖 overlay；Inspect 集成与 Docker SDK/官方过程均为模拟。
真实调用过 Windows PowerShell 的离线预检和无 pin 拒绝路径，未连接 Docker。

增量 CI 增加 `test_evaluation_report.py` 和 `test_swe_prepare.py`，仍为 manual-only。
静态核对 YAML、引用路径、Python AST、PowerShell AST、文档链接和 diff 空白。
远端 workflow 未运行；没有发布、主线合并或 Git 元数据修改。

## 用户终端后续操作

先按 [操作手册](swe-operator-guide-20261101.md) 准备实际资产，再依次调用 Preflight、HostCheck、Execute。
不要把单元测试生成的 synthetic dataset、镜像 ID 或 probes 作为真实 SWE 资产。
拿到实际生命周期和候选证据后，在 Linux 上显式评分；已有候选可只调用 grade API，避免重跑修复。

[技术说明](runtime-technical-brief-20261101.md) 可用于项目介绍和面试；
[代码审查](unpublished-review-20261101.md) 保留此前发现、修复和清单。

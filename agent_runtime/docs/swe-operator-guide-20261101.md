# SWE 普通用户执行入口与环境准备 — 2026-11-01

代码提供离线资产预检、只读主机检查、显式单实例执行和结果收集。
本轮完成的是入口及离线验证；真实容器和官方 Harness 尚未验收。
在有 Docker 权限的普通用户终端使用入口，不要求管理员身份，也不修改 ACL。

## 1. 准备明确的输入

下面的配置结构需要填入本机实际资产；尖括号内容是占位符，不能直接运行。
Windows 路径必须为绝对路径。Linux 执行需改成 Linux 实际可访问的路径，重新计算相应清单。

```json
{
  "bundle": "D:/swe-inputs/prepared-instance",
  "manifest_sha256": "<prepared manifest.json 的可信 SHA-256>",
  "instance_id": "<唯一选定的实例 ID>",
  "image": "sha256:<本地不可变镜像的 64 位摘要>",
  "base_archive": "D:/swe-inputs/base/base.tar",
  "base_archive_sha256": "<base.tar 的可信 SHA-256>",
  "probes": [
    {
      "argv": ["/opt/miniconda3/envs/testbed/bin/python", "-m", "pytest", "<任务相关的候选验证测试>"],
      "stdout_sha256": "<冻结的期望 stdout SHA-256>",
      "timeout_seconds": 30
    }
  ],
  "responses": [
    {"tool": "container_read_file", "id": "read1", "input": {"path": "<目标文件>"}},
    {"tool": "container_edit_file", "id": "edit1", "input": {
      "path": "<目标文件>", "expected_sha256": "<编辑前完整文件 SHA-256>",
      "old_text": "<唯一匹配文本>", "new_text": "<候选替换文本>"
    }},
    {"text": "Fixed"}
  ],
  "max_turns": 4,
  "harness": "D:/swe-inputs/clean-pinned-harness"
}
```

`harness` 仅在要求官方预检/评分时必需。执行开关和连接参数通过命令行提供，
JSON 不允许携带 `execute`、Docker client 或输出目录等控制参数。

资产的准备顺序如下，任一步失败都先保留诊断，不自动重试旧输出目录。

| 顺序 | 资产 / 条件 | 准备和检查 | 当前自动化范围 |
| --- | --- | --- | --- |
| 1 | Python 依赖 | 独立环境安装 `agent_runtime/requirements-inspect.txt` 和已有根依赖；Inspect 固定版本见该文件 | 预检会实际导入依赖并核对 Inspect pin；本轮未安装包 |
| 2 | 数据集 | `python -m agent_runtime.benchmark_dataset prepare DATASET_CONFIG.json NEW_BUNDLE`；固定数据 revision、Harness commit、单一 instance ID | 已有 prepare/verify；solver 输入仅保留题目及仓库身份字段 |
| 3 | 基础代码 | 从可信仓库的选定 base commit 执行 `git archive --format=tar --output=ABS_BASE_TAR BASE_COMMIT` | 检查归档哈希、成员安全与 128 MiB 边界；哈希不自行证明它属于该 commit，控制端负责来源对应 |
| 4 | 镜像和依赖 | 准备匹配实例的本地 immutable image；内置 `/opt/miniconda3/envs/testbed/bin/python`、验证依赖和所需基础程序 | 只读检查本地 image ID；不会 pull/build/install，不把 image 存在视为测试可运行 |
| 5 | 候选 probes | 控制端冻结任务相关 argv、stdout 摘要和时间上限；单纯 `python -V` 只能检查解释器 | 校验 probe 结构；真正的 probes 在后续容器候选验证中执行 |
| 6 | 脚本响应 | 冻结 read/edit 响应、完整文件前置哈希、唯一替换内容及 turn 数 | 离线校验响应结构/大小/工具范围；工具参数及 read-before-edit 在执行时继续检查 |
| 7 | Docker 主机 | 用户终端可访问明确 endpoint，Linux Engine ID 与执行 pin 相同 | `--host` 仅 info/image inspect；创建前及清理前再次核对身份 |
| 8 | 官方执行条件 | Linux Python、干净的固定 Harness checkout、该 Harness 依赖及 Docker | 固定 checkout 和平台预检；官方过程仍须显式执行并验收 |

基础 tar 中不带 `.git`、链接或穿越路径。真实源代码和 Python 环境须能由 UID 65534 访问。
当前 source 容器固定为：无网络、只读根目录、1 CPU、256 MiB 内存、64 PIDs，
`/workspace` tmpfs 128 MiB、`/tmp` tmpfs 16 MiB。复杂仓库可能需要后续经测试的资源配置扩展；
预检不承诺任意 SWE 实例都适合这些限制。候选镜像需提供 entrypoint 清空后可运行的 `sleep`。

## 2. 离线预检

在活动 worktree 根目录执行，使用已安装依赖的 Python。该步骤不会创建输出目录。

```powershell
python -m agent_runtime.swe_prepare D:/swe-inputs/acceptance.json D:/swe-runs/instance-attempt-01
```

`OFFLINE_READY_HOST_PENDING` 只说明输入通过离线预检。
可加 `--report D:/swe-reports/preparation-01` 将准备结果存入独立的新目录。
输入资产、未来运行输出和报告目录必须互不包含。缺少资产时返回 `BLOCKED`、原因和剩余步骤。

## 3. Windows 普通用户检查与执行

从任意目录通过 PowerShell 7 调用脚本。示例中的脚本路径指向活动 worktree，需保留它而非仓库根目录的历史代码。
`-Python` 可指定自己的完整 venv Python 路径；默认是当前终端的 `python`。

```powershell
$entry = 'D:/opencv/learn-claude-code/.scratch/inspect-runtime-solver-worktree/agent_runtime/scripts/Invoke-SweAcceptance.ps1'
pwsh -NoProfile -File $entry -Config D:/swe-inputs/acceptance.json -Output D:/swe-runs/instance-attempt-01 -Mode Preflight
pwsh -NoProfile -File $entry -Config D:/swe-inputs/acceptance.json -Output D:/swe-runs/instance-attempt-01 -Mode HostCheck -Context desktop-linux
```

HostCheck 显示当前实际 Engine ID。首次没有 pin 时显示 `ENGINE_PIN_REQUIRED`，
表示已观察到镜像与 Engine，但尚未固定执行目标。确认这是自己的预期主机后：

```powershell
pwsh -NoProfile -File $entry -Config D:/swe-inputs/acceptance.json -Output D:/swe-runs/instance-attempt-01 -Mode Execute -Context desktop-linux -ExpectedEngineId '<HostCheck 返回并确认的 Engine ID>'
```

Docker CLI context 与 Python SDK 是两套配置路径。脚本先读取选定 context 的 endpoint，
再将 endpoint 明确传给 SDK；不更改默认 context 或用户 Docker 配置，不复制凭证。
本地脚本支持 `npipe://` 与 `unix://` endpoint，远程 TLS 需另行配置。
Engine pin 在环境创建前检查，清理使用同一源的身份与 Engine 复核。

脚本退出码 0 表示该命令没有报告阻断；2 表示预检、执行、恢复或报告收集需要处理。
`CANDIDATE_FINISHED` 即使带空补丁也可以表示本次流程正常终止，不能据退出码断言修复成功。
结果中的 `candidate_validated`、`runtime_status` 和后续官方结果分别查看。
检查失败不自动提升权限，不自动重启或重新执行实例。

## 4. 结果收集与恢复

Windows Execute 在运行结果存在时额外生成以下同级目录：

| 路径 | 内容 |
| --- | --- |
| `instance-attempt-01/` | invocation、candidate、environment、predictions、result |
| `instance-attempt-01-report-inputs/` | 本次 result 的路径、声明范围和 SHA-256 |
| `instance-attempt-01-report/` | 人可读 Markdown、结构化 JSON、输入清单与 seal |
| `instance-attempt-01-inspect-appdata/`、`...-inspect-roaming/` | 仅对执行子进程生效的 Inspect 日志目录 |

报告范围 `real_container` 表示这次显式真实主机执行尝试，仍需读 reported status 和生命周期；
它可能在创建前就 BLOCKED，不表示容器已成功运行。scope 是控制端声明，哈希只校验字节。
`needs_review`、清理不确定或模型/日志处理异常时保留 source，跳过官方评分。
按 `environment/lifecycle.json` 的 engine/source/session 诊断；不要盲目清理或复用原输出重跑。
在创建 client 或导入依赖时失败，可能尚无运行目录，终端错误就是该次启动诊断。

只读重建已有证据报告使用：

```powershell
python -m agent_runtime.evaluation_report ABS_EVIDENCE_MANIFEST.json NEW_REPORT_DIRECTORY
```

清单 schema 为 `evaluation-report-input-v1`，每个 entries 项有
`id/kind/scope/path/sha256`。kind 支持 `junit/suite/candidate/acceptance/official`；
scope 明确为 `offline_simulated/real_container/official/historical`；path 必须为绝对路径。
报告不会执行任何评测，不累计历史/投影的受试数量，不计算混合成功率。

## 5. Linux 官方评分

Windows wrapper 只运行 mock 修复；官方执行需要 Linux Python 和该平台的实际路径与依赖。
从 Linux 活动代码目录运行，下列命令中的配置需指向同一已核验资产，并使用全新的输出：

```sh
python -m agent_runtime.swe_prepare /inputs/acceptance.json /runs/attempt-02 --official --host --docker-host unix:///var/run/docker.sock --expected-engine-id EXPECTED_ENGINE_ID
python -m agent_runtime.swe_acceptance /inputs/acceptance.json /runs/attempt-02 --execute --official --docker-host unix:///var/run/docker.sock --expected-engine-id EXPECTED_ENGINE_ID
```

该组合命令会新执行一次候选流程后评分；不要把它作为前一已完成候选流程的“收集命令”。
若已有 candidate predictions，只调用 `agent_runtime.swe_grading.grade(..., execute=True)`
并传入原预测路径和哈希、同一数据集 pin、干净固定 Harness 与新的评分输出，不重跑修复。
现有 grade 为 Python API；没有独立命令行模块入口。
新入口不会调用付费模型。脚本中的正确 edit 只能证明接口组合可工作，不能证明自主修复能力。

# 真实 SWE 资产准备 — 2026-11-02

本轮将本地历史资产接到新的单实例准备入口。没有运行 Docker、模型、项目 probes 或官方评分。
新增 16 项检查通过；另用真实固定 Parquet 跑通数据准备，并完成基础代码导出的真实输入预检。

## 已核对的真实输入

| 项目 | 内容与边界 |
| --- | --- |
| 实例 | `sympy__sympy-19346`，沿用此前选定的候选；没有改回已暴露的 20590 gold control |
| 数据 revision | `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` |
| 原始 Parquet SHA-256 | `a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd` |
| 仓库 / base | `sympy/sympy` / `94fb720696f5f5d12bad8bc813699fd696afd2fb` |
| 历史镜像 | `sha256:c84f3478fc6e8268fbac170fd8fe5c0dc209dc1276aca66409645c250de5eb47`；2026-10-15 构建和 metadata 记录对应本实例，当前存在性未验证 |
| Harness pin | `3f01bd622c0a22c00406139f69a234ef08225f22`；已有 ZIP/目录仅作历史来源，不能当作干净 Git checkout |
| 候选 probes | 依据公开题目中的 dict/set srepr 例子编写，未参考 gold/test patch；尚未执行 |

probes 显式从 `/workspace` 导入候选源码并核对 `sympy.__file__`，避免使用镜像中
`/testbed` 的旧 editable install 而验证错代码。它们是局部候选检查，不是官方 oracle。
旧 Engine ID 只保留在历史证据中，不作为当前执行 pin。

本地路径均相对仓库根目录 `D:/opencv/learn-claude-code`：

| 路径 | 产物 |
| --- | --- |
| `.scratch/swe-assets-inputs-20261102/assets-spec.json` | 固定数据/实例/基础提交/镜像提示/probes 的控制端规格 |
| `.scratch/swe-assets-inputs-20261102/provenance.json` | 原始本地资产路径及哈希，历史状态说明 |
| `.scratch/swe19346-assets-20261102/dataset/` | 新准备的单实例数据包，solver/evaluator 分离 |
| `.scratch/swe19346-assets-20261102/assets.md`、`assets.json` | 当前资产清单与缺项 |
| `.scratch/swe19346-assets-20261102/draft.json` | 不可执行的配置草案；缺 base_export 和 responses |

准备数据包的 manifest SHA-256 为
`dbe91d176bbc7150e23e6f18048ca92b84480059909756a819b350ecb0e9a104`。
原始 Parquet 包含 evaluator 数据；准备器只向 solver 写入题目、repo、base、instance，
控制端不打印 gold/test 列。哈希绑定本地字节，来源真实性仍依赖可信控制端。

## 新增工具

`python -m agent_runtime.swe_assets SPEC NEW_OUTPUT` 校验快照 pin 和实例身份，
准备独立数据集；缺少归档或脚本时返回 `ASSETS_INCOMPLETE` / 退出码 2，仅写 draft。
完整资产才写 `acceptance.json`，即使写出配置也仍需主机和真实 probes 验收。
支持 JSON 或 Parquet（需可选 pyarrow）。

`python -m agent_runtime.swe_base_export BUNDLE MANIFEST_SHA INSTANCE IMAGE NEW_OUTPUT`
默认只预检。显式 execute 后只创建一个隔离的 Git archive 导出容器，
不执行项目源码、候选 probes 或官方测试。工具将二进制 tar 从有界 Docker socket
直接写入文件，不经 PowerShell 文本重定向。

导出要求明确的 Linux Engine pin、本地不可变镜像；Git commit 从验证过的数据包读取。
接收端限制大小、时间和错误输出，检查 tar 成员、归档哈希、Git pax commit 标记。
成功后核对 Engine、source/session/image 和 exited 状态，再移除这个源，并要求 404 观察。
create/transfer/commit/identity/cleanup 不确定时保留 source 身份和事件，不自动重试。
正常导出产生 `EXPORTED_REMOVED` 回执，含 archive 路径、哈希和实例/base/image 绑定。
失败即使已有部分 tar 也不能当成就绪资产。

可选依赖在 `agent_runtime/requirements-swe-assets.txt`；Docker SDK 7.1.0、pyarrow 26.0.0
沿用本地归档的 Harness lock。当前仅复用已有 Windows pyarrow 和 Runtime 依赖目录，未安装包。

## 下一步实际导出命令

在已安装上述依赖、能访问 Docker 的普通用户 PowerShell 终端运行：

```powershell
Set-Location D:/opencv/learn-claude-code/.scratch/inspect-runtime-solver-worktree
$endpoint = docker context inspect desktop-linux --format '{{.Endpoints.docker.Host}}'
docker --context desktop-linux info --format '{{.ID}} {{.OSType}}'
$engine = '<已核对的当前 Linux Engine ID>'

python -m agent_runtime.swe_base_export `
  D:/opencv/learn-claude-code/.scratch/swe19346-assets-20261102/dataset `
  dbe91d176bbc7150e23e6f18048ca92b84480059909756a819b350ecb0e9a104 `
  sympy__sympy-19346 `
  sha256:c84f3478fc6e8268fbac170fd8fe5c0dc209dc1276aca66409645c250de5eb47 `
  D:/opencv/learn-claude-code/.scratch/swe19346-base-export-01 `
  --execute --docker-host $endpoint --expected-engine-id $engine
```

这个目标目录已做过 preflight，但没有创建。若存在，先看证据，另选新目录；不删除后重跑。
镜像不存在时不会自动 pull/build，先根据历史来源恢复匹配镜像；不能替换为其他实例镜像。

导出完成后读取实际 `base.tar` 的 `sympy/printing/repr.py`，确认完整内容哈希，
再冻结 read/edit 响应。避免在源码尚未取得时猜文件内容和 expected_sha256。
在新的 spec 中加入两个显式 pin：

```json
{
  "base_export": {"path": "<导出 result.json 的绝对路径>", "sha256": "<回执哈希>"},
  "responses": {"path": "<已冻结响应数组文件的绝对路径>", "sha256": "<响应文件哈希>"}
}
```

保留原始 spec 及草案，将补齐字段的新 spec 输入 `swe_assets` 并使用新输出目录。
准备器会核对 receipt 状态、instance/base/image、归档字节和脚本工具范围，拒绝外来归档与不确定回执。
随后使用 [普通用户执行入口](swe-operator-guide-20261101.md) 做真实候选流程。

## 验证和剩余门槛

新测试 `test_swe_assets.py` 7 项、`test_swe_base_export.py` 9 项，全部通过。
覆盖数据与身份 pin、私有字段隔离、缺项草案、完整配置预检、旧输出保护，以及模拟导出的
成功、Engine 不符、创建不确定、commit 不符、截断、身份不符、清理失败与传输大小上限。
传输和 Docker 为模拟；没有真实导出、预测或官方成功记录。

真实数据准备退出 2 是预期的 `ASSETS_INCOMPLETE`，不属于测试失败。
基础导出入口用真实准备数据完成 `PREFLIGHT_ONLY`；源容器和目标输出都未创建。
证据在 `.scratch/swe-assets-validation-20261102/`，既有实验未复跑。

仍待：当前 Engine/镜像可用性、真实 base 导出、实际源码上的冻结 edit、候选 probes、
真实生命周期、干净固定 Linux Harness、官方评分和最终源码的远端 CI。

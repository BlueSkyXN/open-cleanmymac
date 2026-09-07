# AR-06 · Codex P0 验收

> **0.24.0a1 当前实施约束**：P0a 只读与计划预览；7 条 Codex 策略均 active/report_only。
> 经典命令和 TUI 保留。P0b 结构匹配与正式策略审批尚未完成。本文的长期扩展不等于当前已实现。
> 本次落地语义以 [当前实现补充](ar-09-current-implementation.md) 为准。


[契约索引](_index.md) · [AR-03 命令与 I/O](ar-03-cli-and-io-contract.md) ·
[AR-04 Run Store 与执行](ar-04-run-store-and-execution.md) ·
[实现任务](../../implementation/TODO.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件定义**首个纵向切片（Codex Agent Runtime v1）**的验收条件（决策 8）、Codex pack 的
> detector 清单，以及 Agent Contract 正负测试矩阵。完整多阶段/多 pack 路线见
> [AR-07](ar-07-implementation-roadmap.md)/[AR-08](ar-08-strategy-pack-catalog.md)。**这批测试比先拆文件更重要。**

## 1. P0 定义

P0 收敛成一个明确的纵向闭环，而不是按目标目录树一次性重排。完成 P0 后，OpenClean 才真正
从「支持 AI 调用的清理 CLI」变成「面向 AI Agent 的策略运行时」。

### 1.1 必须完成

- Strategy / StrategyPack schema 与加载/校验/注册（[AR-02](ar-02-strategy-and-lifecycle.md)）。
- Finding / Run / CleanupPlan / CleanupOutcome schema（[AR-01](ar-01-object-model.md)）。
- 第一个 `codex` pack（§2）。
- `inspect codex --json` 产出 `run_id` + Finding 摘要（[AR-03](ar-03-cli-and-io-contract.md) §2）。
- `show --run --finding --json` 返回完整证据（[AR-03](ar-03-cli-and-io-contract.md) §4）。
- 本地私有 Run Store（[AR-04](ar-04-run-store-and-execution.md) §2）。
- `clean --run --finding --json` 预览（`mode=preview`、`executed=false`）。
- `trusted`-only `clean --run --finding --yes --json` 执行，复用现有 `execute_cleanup`。
- **至少一个经动作验证的 `trusted` Strategy**，用 `TemporaryDirectory` 跑通完整执行回执
  （正例 + 负例 + 动作前后验证，见 [AR-05](ar-05-research-governance.md) §3）。
- Agent Contract 正负测试（§3）。
- 命令面并存：`scan/clean <category>/analyze/purge` 保留，Agent 能力由 `inspect/explore/show/clean`
  覆盖、无悬空入口；底层探测器/执行器迁移复用后，其既有测试继续通过（见
  [AR-07](ar-07-implementation-roadmap.md) 逐模块转化表）。

### 1.2 首切片暂不要求（完整目标见 [AR-07](ar-07-implementation-roadmap.md)/[AR-08](ar-08-strategy-pack-catalog.md)）

- 完整 `lab` 自动化（首切片可先只定义契约与最小手工路径）。
- Qoder / WorkBuddy / 其他 AI 工具 / macOS / 浏览器 / Docker / 项目产物 pack（属后续阶段，
  但契约与目录已在 [AR-08](ar-08-strategy-pack-catalog.md) 定义）。
- MCP 薄适配（P4）。
- 远程策略发布服务、公钥钉扎、签名 channel（有意排除，见 [AR-05](ar-05-research-governance.md)）。

## 2. Codex pack detector 清单

`codex` pack 的策略直接映射**已存在**的探测能力（`implementation/openclean/storage_diagnostics.py`、
`updater.py`），P0 先全部以 `active` 或只读方式运行，只把其中一个低风险、证据充分的策略
单独晋级为 `trusted`：

| Strategy（示例 id） | detector（白名单名） | 现有实现锚点 | P0 状态 |
|---|---|---|---|
| `codex.marketplace.old-staging` | `codex_marketplace_staging` | `scan_codex_marketplace_staging` | active（聚合根不直接可执行，见 §4） |
| `codex.git.temp-skeleton` | `codex_git_skeleton` | `scan_codex_git_skeletons` / `_is_codex_git_skeleton` | active |
| `codex.crashpad.orphan-sidecar` | `crashpad_pairing` | `scan_crashpad_orphan_sidecars` | active（只读，不删 `.dmp`） |
| `codex.logs.sqlite-freelist` | `sqlite_freelist` | `scan_sqlite_diagnostics` | active（只读，不建议删 DB） |
| `codex.logs.retention` | `retention` | `scan_retention_diagnostics` / `discover_codex_log_partition_rules` | active（只读） |
| `codex.runtime.cache-retention` | `retention` | `~/.cache/codex-runtimes` retention | active（只读） |
| `codex.updater.staging` | `darwin_temp_updater` / `updater_staging` | `scan_darwin_temp_updater_diagnostics` / `assess_updater_staging_root` | active（只读） |
| `codex.process.open-unlinked` | `open_unlinked` | `scan_open_unlinked_diagnostics` | active（只读，`potential_bytes=0`） |

Codex 进程 marker 沿用现有 `_CODEX_PROCESS_MARKERS = ("ChatGPT.app", "Codex.app", "codex")`；
运行中一律 `actionable=false`。首个 `trusted` 候选应选择**具有充分正负案例和动作实验**的
低风险策略（例如结构稳定的 marketplace 旧 staging 的**逐个目标**，而非聚合根）。

## 3. Agent Contract 正负测试矩阵

P0 必须新增 Agent 级合同测试（目标位置 `implementation/tests/agent_contract/`）。测试写操作
只用 `TemporaryDirectory`，不在真实 `HOME` 或真实 Docker daemon 上跑 `--yes`。

| # | 断言 | 类型 |
|---|---|---|
| 1 | `inspect codex` 只返回 codex pack 的 Finding，不含其他域 | 正 |
| 2 | `finding_id`/`run_id` 唯一且**不包含路径**子串 | 正 |
| 3 | `show --finding` 必须属于对应 `--run`，否则退出码 `2` | 负 |
| 4 | Run 过期或不存在 → 拒绝，退出码 `1`，不自动重扫 | 负 |
| 5 | Strategy pack hash 变化 → `strategy_hash_mismatch`，拒绝执行 | 负 |
| 6 | `active`（非 trusted）策略 → `can_execute=false`，`--yes` 也拒绝 | 负 |
| 7 | `trusted` 策略但应用运行中 / 句柄未知 / identity 变化 / 命中 protect → `actionable=false` | 负 |
| 8 | 不存在任意路径删除入口（无 `delete PATH`，`clean` 只接受 `--run/--finding`） | 负 |
| 9 | `--redact-paths` 输出 `selection_replayable=false`，脱敏 ID 不能用于 `show`/`clean` | 负 |
| 10 | 不带 `--yes` 的 `clean` 永不写（`mode=preview`、`executed=false`） | 正 |
| 11 | 批量执行中任一目标 identity 变化 → 整批 fail-closed（`blocked`/`not_run`） | 负 |
| 12 | `trusted` + 用户授权 + live guard 全通过 → 移入同卷 Trash，回执区分 moved/deleted | 正 |
| 13 | `confirm`/`critical` Finding 缺少对应风险 flag → 拒绝（`finding_id` 不替代授权） | 负 |
| 14 | KB `protect`/`ignore` 命中的目标 → 不进入 detector，直接阻断 | 负 |
| 15 | 命令面一致性：退役 `scan/clean <category>/analyze/purge` 后无悬空入口；`inspect all` 覆盖全部已加载 pack | 正 |

## 4. 聚合诊断不可直接清理

现有 `scan_codex_marketplace_staging` 把多个 `marketplace-upgrade-*` 目录聚合成一个以
`.staging` 为锚点的只读 `filesystem_subset` Item，并固定 `actionable=false`。要成为
`trusted` cleanup strategy，**不能**把聚合根直接交给清理器。P0 必须保证：

- 每个实际目标形成**单独 Finding**，或 CleanupPlan 中保存一组**精确目标**（见
  [AR-01](ar-01-object-model.md) §7 `resolved_targets`）；
- 为每个目标记录 identity（device/inode/owner）；
- 加入最低年龄、结构复核、句柄与运行状态条件；
- 执行前再次运行 live guard（[AR-04](ar-04-run-store-and-execution.md) §5–§6）；
- **不允许**把 `.staging` 或 `.tmp` 父目录泛化成动作目标；`measurement_complete=false`
  或 `diagnostic_limit_reached` 时容量只是有界部分结果，不得据此执行。

## 5. 验收判据

P0 达成的核心判据：

> **Agent 不再需要从全量 AI 域扫描结果中自行猜哪些属于 Codex。**
> 它可以直接 `inspect codex` 得到带 `run_id`/`finding_id` 的结构化 Finding，`show` 取证据，
> `clean` 预览，并在用户对具体 Finding 明确授权后执行——全程受 Strategy 状态、Run 有效期、
> identity 复核与 KB 保护闸约束；底层探测器与安全执行器从现有实现迁移复用，其既有测试在
> 迁移后继续通过。

达成判据前不进入下一切片（Qoder/WorkBuddy 扩展，见 [AR-07](ar-07-implementation-roadmap.md)
阶段序列）。能力落地后，再按 [CONTRIBUTING.md](../../CONTRIBUTING.md)「文档分层」同步 README、
`docs/AI_USAGE.md`、`docs/CAPABILITIES.md`、[implementation/TODO.md](../../implementation/TODO.md)
与 CHANGELOG，并把本目录相应条目的状态从 📐 更新为已实现。

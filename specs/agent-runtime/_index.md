# Agent Runtime v1 · 契约索引

[规格索引](../_index.md) · [仓库 README](../../README.md) ·
[架构](../../docs/ARCHITECTURE.md) · [AI 只读调用](../../docs/AI_USAGE.md) ·
[贡献指南](../../CONTRIBUTING.md) · [实现说明](../../implementation/README.md)

> **这是 OpenClean 自有的前瞻架构契约，不是 CleanMyMac 参考事实。**
> `specs/00-07` 描述参考对象（CleanMyMac 5 CLI）的净室功能事实；本目录描述
> OpenClean 下一代「面向 AI Agent 的 macOS 存储策略运行时」的 v1 契约。
>
> 当前：P0a 只读与计划预览已落地，P0b 生产策略未启用。经典命令与 TUI 保留。
> CLI envelope schema v2，持久化 Run bundle schema v2（两个独立版本）。
> 当前行为见 [AR-09 实现补充](ar-09-current-implementation.md)，其余章节保留长期设计。

## 1. 目标与范围

当前仓库已经是实现成熟度较高、默认保守的通用 macOS 清理 CLI，附带 AI 只读调用指南和
若干针对 AI 工具的专项诊断。P0 的目标不是重写扫描器，而是把这些成熟内核**重组为一条
可追踪的策略生产与 Agent 调用闭环**：

```text
Observation → 版本化 Strategy → Builtin Detector → Run + Finding
           → show / explain → 基于 Finding ID 的 CleanupPlan → cleanup 执行
```

本目录定义**完整目标架构契约**（对象模型、命令与 I/O、Run Store、执行不变量、策略包全景、
研究治理、实施路线图），而不只是 P0。**Codex 是第一个纵向实现切片**，其余 pack 与阶段按
[AR-07](ar-07-implementation-roadmap.md) 路线图、[AR-08](ar-08-strategy-pack-catalog.md) 全景展开。

Agent 命令面是经典能力的附加入口，不通过删除旧命令替代迁移。
仍**有意排除**：远程策略发布服务的具体实现、`implementation/`→`src/` 搬迁（纯 churn、无功能收益）。

## 2. 阅读顺序

| # | 契约 | 内容 |
|---|---|---|
| AR-00 | [架构与边界](ar-00-architecture.md) | 定位、三平面、AI 角色边界、命令面设计、目标逻辑结构 |
| AR-01 | [对象模型](ar-01-object-model.md) | Observation/Strategy/Run/Finding/CleanupPlan + `Item`→`Finding` 迁移 |
| AR-02 | [策略与生命周期](ar-02-strategy-and-lifecycle.md) | Strategy/Pack 结构、status 矩阵、版本与冲突、KB 安全闸 |
| AR-03 | [命令与 I/O 契约](ar-03-cli-and-io-contract.md) | inspect/explore/show/clean/strategy/config/lab、JSON、退出码 |
| AR-04 | [Run Store 与执行](ar-04-run-store-and-execution.md) | Run Store、标识稳定性、状态机、`can_execute`、live guard 复用 |
| AR-05 | [研究治理](ar-05-research-governance.md) | Observation 公开边界、净室一致性、`research/` 治理 |
| AR-06 | [Codex 首切片验收](ar-06-codex-p0-acceptance.md) | 必须/暂不要求清单、Agent Contract 正负测试矩阵 |
| AR-07 | [实施路线图与代码转化](ar-07-implementation-roadmap.md) | 阶段序列、逐模块转化表、实体产物目标结构 |
| AR-09 | [当前实现补充](ar-09-current-implementation.md) | 本候选版本的实际范围与后续工作 |
| AR-08 | [策略包全景](ar-08-strategy-pack-catalog.md) | 全量 pack 目录、detector/action 白名单、P0-P4 展开 |

## 3. 状态图例

- P0a：探测、Run/Finding、展示、预览与 API 执行逻辑已实现；生产包仍只读。
- P0b：真实结构匹配、Observation、promotion、macOS 原生验证待完成。
- P1+：其余 pack、explore、lab、MCP 仍为规划。
- 不把全部 AR-00..AR-06 一律标为完成；详见 [AR-09](ar-09-current-implementation.md)。

## 4. 阶段 0 八项决策

契约必须先定下这些边界，实现阶段不再逐个重开讨论。

| # | 决策 | 结论 | 落点 |
|---|---|---|---|
| 1 | 旧命令是否兼容保留 | 经典命令与 TUI 保留；Agent 附加；当前 envelope 使用 schema v2 | [AR-00](ar-00-architecture.md) / [AR-03](ar-03-cli-and-io-contract.md) |
| 2 | Run Store 位置/权限/TTL/容量/清理 | 本机私有状态目录，`0700`/`0600`，默认 TTL 24h，容量上限 + 原子写 + 过期拒绝 | [AR-04](ar-04-run-store-and-execution.md) |
| 3 | `run_id`/`finding_id` 稳定性、是否编码路径 | 稳定、跨命令可读、**不编码路径** | [AR-01](ar-01-object-model.md) / [AR-04](ar-04-run-store-and-execution.md) |
| 4 | `active`/`trusted` 执行边界 | `active` 只识别/报告；`trusted` 才可生成计划，执行仍需用户授权 + live guard | [AR-02](ar-02-strategy-and-lifecycle.md) / [AR-04](ar-04-run-store-and-execution.md) |
| 5 | `--redact-paths` 是否可 replay | 不可 replay；脱敏同时替换 actionable ID | [AR-03](ar-03-cli-and-io-contract.md) |
| 6 | Observation 公开提交边界 | 只提交去标识化行为事实；`raw/` 不提交 | [AR-05](ar-05-research-governance.md) |
| 7 | Strategy Pack 冲突与同 ID 版本 | 版本单调 + hash 绑定 + 保守优先；越具体越优先 | [AR-02](ar-02-strategy-and-lifecycle.md) |
| 8 | Codex vertical slice 验收 | 必须/暂不要求清单 + Agent Contract 正负测试矩阵 | [AR-06](ar-06-codex-p0-acceptance.md) |

## 5. 契约 ↔ 现有代码复用锚点

新契约**复用**而非重写现有内核。下表是各契约必须引用的现状锚点（实现阶段以此为接缝）。

| 现有能力 | 位置 | 在 v1 契约中的角色 |
|---|---|---|
| `Item` / `FileIdentity` / `FileFacts` | `implementation/openclean/models.py` | Finding 投影来源；identity 与云占位判定沿用 |
| `_item_payload`（当前扁平 JSON 面） | `implementation/openclean/cli.py` | 说明 Finding 需要重组的字段面；无兼容负担，JSON 围绕 Run/Finding 重新设计 |
| `ProtectionGate` / `KnowledgeBaseIgnorePredicate` | `implementation/openclean/predicates.py` | Strategy 之上**最高优先级安全闸**，KB 先于普通谓词 |
| `KnowledgeBase` / `RulesStore._write_payload` | `implementation/openclean/knowledge_base.py` | 保护/忽略规则；`0700`+`0600`+`fsync`+`os.replace` 原子写作为 Run Store 先例 |
| `select_cleanup_items` / `execute_cleanup` / `_audit_item` / `_validate_cleanup_scope` / `trash_directory_for` | `implementation/openclean/cleanup.py` | CleanupPlan 执行层；批量 all-or-nothing 预检 + 逐项 live 复核 + 同卷 Trash |
| `CleanupReport.moved_bytes` vs `deleted_bytes` | `implementation/openclean/cleanup.py` | CleanupOutcome 回执区分「暂存未释放」与「永久删除」 |
| Codex 探测器：`scan_codex_marketplace_staging`、`scan_codex_git_skeletons`、`scan_crashpad_orphan_sidecars`、`scan_sqlite_diagnostics`、`scan_retention_diagnostics`、`scan_darwin_temp_updater_diagnostics`、`scan_open_unlinked_diagnostics`、`discover_codex_log_partition_rules` | `implementation/openclean/storage_diagnostics.py` | codex pack 的 builtin detector 事实来源 |
| `assess_updater_staging_root` / `UpdaterAssessment` | `implementation/openclean/updater.py` | updater 状态机证据 |
| 写操作状态机 §5、路径与竞态防护 §6 | `docs/ARCHITECTURE.md` | 执行不变量基线，CleanupPlan 状态机与之对齐 |

## 6. 净室边界

本目录遵循 [CONTRIBUTING.md](../../CONTRIBUTING.md) 的净室红线，并因其前瞻性质额外声明：

- 这里是 **OpenClean 自有设计**，不是从参考软件提取的事实；不得把参考软件代码、反编译
  表达、私有规则库或商业指纹写入本目录或据此实现。
- CleanMyMac 的黑盒实验结果只能作为**去标识化 Observation** 进入研究流程（见
  [AR-05](ar-05-research-governance.md)），命中本身不自动生成清理动作。
- 契约中的路径示例（如 `~/.codex/...`）用于说明结构，不代表已提交的真实机器扫描结果。
- 本目录不修改 `specs/00-07` 的参考事实，也不改动 README/CHANGELOG/CAPABILITIES/AI_USAGE
  对**已发布行为**的描述；能力落地时再按 CONTRIBUTING「文档分层」同步。

# AR-01 · 对象模型

[契约索引](_index.md) · [AR-00 架构](ar-00-architecture.md) ·
[AR-04 Run Store 与执行](ar-04-run-store-and-execution.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件定义五个核心领域对象与一个回执对象的字段、不变量与示意 JSON，并给出现有
> `Item` → `Finding` 的投影映射。策略生命周期见 [AR-02](ar-02-strategy-and-lifecycle.md)。

## 1. 五对象关系

最终模型是**五个核心对象 + 一个回执**，不是四个，也不是把 `Item` 一拆为三。

```text
Observation ──(研究证据)──► Strategy ──(运行时加载)──► Run
                                                       │
                                              detector 探测
                                                       ▼
                                                    Finding ──(resolve)──► CleanupPlan
                                                                              │
                                                                    用户确认 + live recheck
                                                                              ▼
                                                                       CleanupOutcome
```

| 对象 | 作用 | 进入普通运行时 | 能触发动作 |
|---|---|---:|---:|
| `Observation` | 记录某个来源观察到了什么 | 否（研究平面） | 否 |
| `Strategy` | 定义如何发现、判断、解释、处理 | 是（只加载 active/trusted） | 仅 `trusted` |
| `Run` | 固化一次 inspect 的环境、策略版本与结果集合 | 是 | 否 |
| `Finding` | 当前机器上某条 Strategy 的实际命中 | 是 | 本身**不**构成授权 |
| `CleanupPlan` | 将选中 Finding 解析为精确动作 + live guard | 是 | 用户确认后才执行 |
| `CleanupOutcome` | 执行回执（区分暂存与永久删除） | 是 | 是执行结果，非第六个领域对象 |

**关键区分**：`Observation` 与 `Strategy` 属于知识生产阶段；`Finding` 才是运行时对象，
是 `Item` 的运行时替代 / 外部投影。三者不是对 `Item` 的并列替代类型。

## 2. Observation

研究证据，不是扫描规则，也不进入普通运行时。

```json
{
  "observation_id": "obs:cleanmymac:codex:001",
  "source": "cleanmymac_experiment",
  "target": "codex",
  "environment": {
    "product_version": "recorded-version",
    "macos_version": "recorded-version"
  },
  "method": "read_only_scan_diff",
  "observation": {
    "detected_path_shape": "~/.codex/.tmp/marketplaces/.staging/marketplace-upgrade-*",
    "category": "ai_junk",
    "reported_bytes": 104857600
  },
  "limitations": ["单版本单环境", "未做清理前后对比"],
  "negative_examples": [],
  "linked_strategies": ["codex.marketplace.old-staging"]
}
```

不变量：

- `source ∈ {cleanmymac_public, cleanmymac_experiment, personal_hypothesis, ai_research}`。
- `reported_bytes`、`detected_path_shape` 等是**观察记录**，不直接转化为删除动作。
- 公开提交的 Observation 必须去标识化；真实路径、容量、文件内容与机器标识不入公开库
  （见 [AR-05](ar-05-research-governance.md)）。
- 个人线索（`personal_hypothesis`）首先是研究线索，需经 `explore` 与验证才能形成 draft。

## 3. Strategy

最终策略库的基本单元。字段结构、状态生命周期、版本与冲突规则在
[AR-02](ar-02-strategy-and-lifecycle.md) 完整定义；此处只给对象骨架与不变量。

```json
{
  "id": "codex.marketplace.old-staging",
  "version": 1,
  "pack": "codex",
  "status": "active",
  "provenance": [
    {"kind": "cleanmymac_observation", "observation_id": "obs:cleanmymac:codex:003"},
    {"kind": "personal_experience", "observation_id": "obs:personal:codex:002"}
  ],
  "locator": {"roots": ["~/.codex/.tmp/marketplaces/.staging"]},
  "detector": {"name": "codex_marketplace_staging", "params": {}},
  "conditions": {"minimum_age_days": 7, "require_structure_match": true, "require_no_open_handles": true},
  "guards": {"do_not_generalize_parent": true, "protect_running_processes": true},
  "assessment": {"classification": "cleanup_candidate", "certainty": "high", "action_risk": "confirm"},
  "recommendation": {"summary": "符合已验证结构的旧 Marketplace 升级暂存目录。", "do_not_do": ["不能把整个 .staging 父目录视为垃圾"]},
  "action": {"name": "move_to_trash", "supported": false}
}
```

不变量：

- `detector.name` 与 `action.name` 必须来自 Python 内置白名单；JSON **不承载可执行代码**，
  不得出现任意 shell、任意表达式或通用 `{delete, path}`。
- `id` 全局唯一；同 `id` 通过 `version` 单调演进（见 [AR-02](ar-02-strategy-and-lifecycle.md) §4）。
- `status` 决定运行时可见性与可执行性（见 [AR-02](ar-02-strategy-and-lifecycle.md) §3）。

## 4. Run

`Run` 是跨命令 Agent 工作流成立的关键：一次 `inspect` 固化环境、策略版本与 Finding 集合，
后续 `show`/`clean` 从 Run Store 按 `run_id` 解析，而不是让 Agent 重放或重新拼接路径。

```json
{
  "run_id": "run:01HXXXXCODEX",
  "created_at": "2026-09-03T10:00:00+08:00",
  "expires_at": "2026-09-04T10:00:00+08:00",
  "openclean_version": "0.23.0",
  "macos_version": "recorded-version",
  "requested_target": "codex",
  "strategy_pack_hashes": {"codex": "sha256:…"},
  "protect_config_hash": "sha256:…",
  "complete": true,
  "issues": [],
  "finding_ids": ["finding:01HXXXXA", "finding:01HXXXXB"]
}
```

不变量：存储位置、权限、TTL、容量上限、过期拒绝与标识稳定性在
[AR-04](ar-04-run-store-and-execution.md) §2–§3 定义。`Run` 本身不触发动作。

## 5. Finding

Finding 是运行时命中记录。**必须采用稳定核心字段 + 类型化 `evidence`，禁止重蹈 `Item`
大平面覆辙**：SQLite、Crashpad、retention、Docker 等互斥差异放进各自的
`evidence.payload`，不再向 Finding 顶层不断追加字段。

```json
{
  "finding_id": "finding:01HXXXXA",
  "run_id": "run:01HXXXXCODEX",
  "strategy_id": "codex.marketplace.old-staging",
  "strategy_version": 1,
  "target": {
    "kind": "filesystem",
    "display_path": "~/.codex/.tmp/marketplaces/.staging/marketplace-upgrade-2024x",
    "identity": {"device": 1, "inode": 2, "owner": 501}
  },
  "measurement": {
    "allocated_bytes": 104857600,
    "logical_bytes": 130000000,
    "age_days": 12,
    "latest_mtime": 1717000000.0
  },
  "assessment": {
    "classification": "cleanup_candidate",
    "certainty": "high",
    "action_risk": "confirm",
    "actionable": false,
    "block_reasons": ["strategy_not_trusted"]
  },
  "recommendation": {"summary": "…", "do_not_do": ["…"]},
  "evidence": {"kind": "codex_marketplace_staging", "payload": {}}
}
```

核心字段不变量：

- `target.kind ∈ {filesystem, filesystem_subset, docker}`（沿用现有 `RESOURCE_KINDS`）。
- `target.identity` 复用 `FileIdentity(device, inode, owner)`；非文件系统资源用稳定
  `identifier` 而非路径。
- `finding_id` 稳定、唯一、**不编码路径**（决策 3）。
- `assessment.actionable=false` 时必须在 `block_reasons` 给出结构化原因；只读诊断
  （retention/sqlite/updater_temp/open_unlinked/codex_transient/crashpad_pairing）永远
  `actionable=false`，与现有 `Item.__post_init__` 的约束一致。
- `evidence.kind` 与 `strategy.detector.name` 对应；`payload` 是该 detector 的类型化证据。
- `finding_id` 本身**不是**授权；执行条件见 [AR-04](ar-04-run-store-and-execution.md) §4。

`evidence.payload` 示例（按 kind 限定字段组，避免顶层膨胀）：

| `evidence.kind` | payload 关键字段（来自现有诊断） |
|---|---|
| `retention` | `retention_file_count`、`open_handle_count`、`retention_7d/14d/30d_bytes` |
| `sqlite_freelist` | `sqlite_page_size/page_count/freelist_count`、`sqlite_internal_free_bytes/ratio`、`sqlite_wal_bytes` |
| `codex_marketplace_staging` | `total_count`、`measured_count`、`measurement_complete`、`open_handle_count` |
| `crashpad_pairing` | `total_count`、`paired_artifact_count`、`recent_artifact_count` |
| `open_unlinked` | `logical_bytes`、`total_count`、`related_process_count`、`open_handle_count` |
| `updater_temp` | `updater_status`、`installed_version`、`staged_version`、`updater_external_install` |
| `docker` | `identifier`、`resource_total_bytes`、target binding 摘要（binding 本体不进 JSON） |

## 6. 现有 `Item` → `Finding` 迁移映射

项目未发布、无兼容负担，`Finding` **真正取代** `Item` 成为运行时主模型，而不是长期并存的
投影层。当前 `Item`（`implementation/openclean/models.py`）与其 JSON 面 `_item_payload`
（`implementation/openclean/cli.py`）已承载大部分证据，迁移是**重组**而非重新采集；下表是
逐字段迁移路径（`runtime/finding_projection.py` 仅为迁移期过渡，逐域迁移完成后 `Item` 退役）：

| 现有 `Item` 字段 | 迁移到 Finding |
|---|---|
| `path`、`resource_kind`、`identifier` | `target.kind`、`target.display_path` / `identifier` |
| `identity`（`FileIdentity` device/inode/owner） | `target.identity` |
| `size`、`allocated_size`、`logical_size`、`age_days`、`latest_mtime` | `measurement.*` |
| `safety`、`actionable`、`action_block_reason`、`requires_privilege`、`is_cloud_file`、`requires_explicit_selection` | `assessment.actionable`、`assessment.action_risk`、`assessment.block_reasons` |
| `category`、`domain`、`note` | `assessment.classification`、`recommendation.summary` |
| `diagnostic_kind` + `retention_*` / `sqlite_*` / `paired_artifact_count` / `recent_artifact_count` / `related_process_count` / `open_handle_count` / `updater_*` / `total_count` / `measured_count` / `measurement_complete` / `running_process_markers` | `evidence.kind` + `evidence.payload`（类型化） |
| —（`Item` 无） | **新增** `finding_id`、`run_id`、`strategy_id`、`strategy_version`、结构化 `recommendation.do_not_do` |

实现修正（已落地，与本契约一致）：

- `assessment.action_risk` **沿用 `SAFETY_LEVELS`（`safe`/`confirm`/`critical`）**，不用
  low/medium/high；因为 `--include-confirm`/`--include-critical` 授权按该枚举匹配。
- 除上表字段外，`Item`→`Finding` 无损迁移还必须保留 `_audit_item` 的硬阻断输入：
  `excluded_paths`、`cross_device_paths`、`cloud_file_count`、`cloud_logical_size`、`domain`
  （丢失即静默放宽安全闸）；它们存于 `evidence.payload`。
- `Item.category`（中文人类标签，也是批量键）与枚举 `assessment.classification` **并存**，
  不互相替代；人类标签进 `evidence.payload`。

迁移完成后：JSON 输出围绕 Run/Finding 重新设计，**不冻结于 schema v2**；旧命令
`scan`/`clean <category>` 退役（处置见 [AR-03](ar-03-cli-and-io-contract.md) §9）。逐模块
转化顺序见 [AR-07](ar-07-implementation-roadmap.md)。

## 7. CleanupPlan

`Finding` → `resolve` → `CleanupPlan` → `preview` → 用户确认 → `live recheck` → `Action`。
Finding ID **不等于**动作授权；CleanupPlan 才是可预览、可审阅的动作解析结果。

```json
{
  "mode": "preview",
  "run_id": "run:01HXXXXCODEX",
  "executed": false,
  "plan_items": [
    {
      "finding_id": "finding:01HXXXXA",
      "strategy_id": "codex.marketplace.old-staging",
      "strategy_version": 1,
      "action": {"name": "move_to_trash", "supported": false},
      "resolved_targets": [
        {"display_path": "…/marketplace-upgrade-2024x", "identity": {"device": 1, "inode": 2, "owner": 501}}
      ],
      "can_execute": false,
      "block_reasons": ["strategy_not_trusted"]
    }
  ]
}
```

不变量：

- 不带 `--yes` 时固定 `mode="preview"`、`executed=false`（见 [AR-03](ar-03-cli-and-io-contract.md) §4）。
- `resolved_targets` 必须是**精确目标集合**，逐个携带 identity；聚合根（如 `.staging`、
  `.tmp` 父目录、`filesystem_subset` 锚点）**不得**直接作为动作目标（见
  [AR-06](ar-06-codex-p0-acceptance.md) §4）。
- `can_execute` 的完整合取条件见 [AR-04](ar-04-run-store-and-execution.md) §4。

## 8. CleanupOutcome（回执）

执行结果形成回执，复用现有 `CleanupReport` 语义（`implementation/openclean/cleanup.py`）：

```json
{
  "mode": "execute",
  "executed": true,
  "complete": true,
  "outcomes": [
    {"finding_id": "finding:01HXXXXA", "status": "moved_to_trash", "bytes_affected": 104857600, "destination": "…/.Trashes/501/…"}
  ],
  "moved_to_trash_bytes": 104857600,
  "permanently_deleted_bytes": 0
}
```

不变量：沿用 `moved_bytes`（移到同卷 Trash，**暂存、尚未释放**）与 `deleted_bytes`
（Trash 清空或 Docker prune，**永久删除**）的区分；不得把「发现可回收容量」等同于
「已释放空间」。批量 all-or-nothing 与逐项 live 复核见 [AR-04](ar-04-run-store-and-execution.md) §5。

## 9. 建模红线

1. 不让 Finding 再次变成新的大平面模型：差异进 `evidence.payload`。
2. Observation/Strategy 是知识生产对象，Finding 才是运行时对象；不要三者并列替换 `Item`。
3. 聚合诊断根不直接当动作目标；每个实际目标单独成 Finding 或在计划中保存精确目标集合。
4. `finding_id`/`run_id` 不编码路径，脱敏输出不可 replay（见 [AR-03](ar-03-cli-and-io-contract.md) §5）。

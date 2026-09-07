# AR-00 · 架构与边界

> **0.24.0a1 当前实施约束**：P0a 只读与计划预览；7 条 Codex 策略均 active/report_only。
> 经典命令和 TUI 保留。P0b 结构匹配与正式策略审批尚未完成。本文的长期扩展不等于当前已实现。
> 本次落地语义以 [当前实现补充](ar-09-current-implementation.md) 为准。


[契约索引](_index.md) · [规格索引](../_index.md) · [架构](../../docs/ARCHITECTURE.md) ·
[AI 只读调用](../../docs/AI_USAGE.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件定义完整目标架构的定位、平面划分、AI 角色边界、命令面设计与
> 目标逻辑结构。对象字段见 [AR-01](ar-01-object-model.md)，执行不变量见
> [AR-04](ar-04-run-store-and-execution.md)，实施路线见 [AR-07](ar-07-implementation-roadmap.md)。

## 1. 产品定位

> **OpenClean 是面向 AI Agent 的本地 macOS 存储策略引擎。它把公开参考经验、用户经验和
> 本地研究证据编译成版本化策略，通过确定性探测生成可解释的 Finding，并只对经过验证、
> 用户明确授权且通过实时安全复核的 Finding 执行清理。**

这比「CleanMyMac 替代品」准确，也比「AI 清理工具」安全。三个不可让步的属性：

- **离线优先**：运行时不依赖网络；远程策略发布不在 P0 范围。
- **确定性**：真正读取文件、测量容量、执行清理的是经过测试的 Python 代码，不是模型推断。
- **可审计**：每次识别绑定 `run_id`、`strategy_id` 与 `strategy_version`，可回溯证据。

## 2. 三平面

系统分为三个平面，而不是一套越来越大的扫描代码。

```text
┌──────────────────────────────────────────────────────────┐
│ 研究平面 Research Plane                                   │
│ CleanMyMac 公开观察 / 本机对比 / 个人经验 / AI 探索        │
│              ↓                                            │
│ Observation → Draft Strategy → Validate → Promote         │
└──────────────────────────┬───────────────────────────────┘
                           │ 产出已验证 Strategy Pack
                           ▼
┌──────────────────────────────────────────────────────────┐
│ 策略平面 Policy Plane                                     │
│ codex / qoder / workbuddy / macos / browsers / docker     │
│ locator + detector + guards + assessment + action         │
│ status: draft / active / trusted / deprecated             │
└──────────────────────────┬───────────────────────────────┘
                           │ 运行时只加载 active / trusted
                           ▼
┌──────────────────────────────────────────────────────────┐
│ 运行平面 Runtime Plane                                    │
│ Agent → inspect/explore → Run/Finding → show              │
│       → clean preview → 用户确认 → clean --yes            │
│       → live recheck → deterministic action → outcome     │
└──────────────────────────────────────────────────────────┘
```

**核心原则：融合发生在策略生产阶段，不发生在每次运行时。** CleanMyMac 观察、个人经验和
AI 研究可以共同成为一条 Strategy 的 `provenance`，但运行时只加载已经编译和验证好的
Strategy Pack。因此 v1 **不设**复杂的运行时 `merger`；冲突与优先级在策略生产期解决
（见 [AR-02](ar-02-strategy-and-lifecycle.md) §5）。

策略按**被研究的工具或领域**（codex/qoder/…）组织，而不是按来源（cleanmymac/personal）
组织；来源信息保留在每条策略的 `provenance` 内部。

## 3. AI 角色边界

AI 承担两个角色：研究阶段的**策略研究助手**，使用阶段的**运行时清理决策助手**。但边界是硬的：

| AI 可以 | AI 不可以 |
|---|---|
| 提出研究线索、调用 `explore` 收集证据 | 现场编造删除路径 |
| 解释 Finding 证据、收益与风险 | 现场编造 shell 命令 |
| 在用户授权后选择 `finding_id` 调用 `clean` | 绕过 Strategy 与 Guard |
| 生成 Strategy draft 与验证报告 | 把推测直接变成清理动作 |
| 读取 JSON 事实并向用户复述 | 自行把策略 `promote` 到 `trusted` |

真正读取文件、测量容量和执行清理的始终是 OpenClean 的确定性代码。`promote` 是显式人工
动作（见 [AR-03](ar-03-cli-and-io-contract.md) §6）。

## 4. 与现有文档的关系

| 文档 | 当前定位 | v1 契约的关系 |
|---|---|---|
| [README.md](../../README.md) | 「macOS 磁盘清理 CLI」用户门面 | 定位扩展为 Agent-first；**落地时**才改，本轮不改 |
| [docs/AI_USAGE.md](../../docs/AI_USAGE.md) | 只读调用指南，流程**停在建议**，禁止 AI 加 `--yes` | 升级为 Agent Contract 的前半段；执行阶段授权语义在 [AR-03](ar-03-cli-and-io-contract.md)/[AR-04](ar-04-run-store-and-execution.md) 定义 |
| [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) | 模块化单体、`Item` 数据模型、写操作状态机 | v1 在其上叠加 Strategy/Run/Finding 层；§5 状态机与 §6 竞态防护作为执行不变量基线被完整保留 |
| [specs/00-07](../_index.md) | CleanMyMac 参考事实 | 保持不变；本目录是 OpenClean 自有设计，语义不同 |

现状说明：`docs/AI_USAGE.md` 目前明确要求 AI「汇报精确结果后停止」，并禁止自行加
`--yes`。v1 允许 AI 在**用户对当前 Finding 明确授权后**继续调用 `clean --yes`；这不是
文档措辞调整，而是 [AR-04](ar-04-run-store-and-execution.md) §5 定义的授权语义与
[AR-06](ar-06-codex-p0-acceptance.md) 的合同测试共同保证的新契约。

## 5. 命令面设计（决策 1）

> **P0 纠偏注记（2026-09）**：当前阶段采用“一套确定性内核、两个命令面并存”。经典
> scan/clean category/analyze/purge/TUI 暂时保留；Agent inspect/show/strategy/clean
> --run --finding 作为附加命令面。两者复用 cleanup.py 的安全执行原语。下表中的“退役”
> 列改为“当前状态”，待能力等价和回归验证后再独立决策。

| 旧能力 | 当前状态 | 去向 |
|---|---|---|
| `scan --domain …` | 并存保留 | `inspect <pack>` / `inspect all`（待能力等价后决策） |
| `clean junk/dev/ai/trash` | 并存保留 | `clean --run --finding`（Finding 驱动，待 pack 覆盖后决策） |
| `analyze`（人类空间浏览/TUI） | 并存保留 | 研究证据归 `explore`；人类空间视图保留 |
| `purge`（项目产物） | 并存保留 | `project-artifacts` pack + `inspect`/`clean`（待覆盖后决策） |
| `optimize ram/purgeable` | 保持 fail-closed | 无安全公开执行器，不属于 Agent Runtime 命令面 |
| `ignore` | 保留 | 所有 Strategy 之上的保护层（见 [AR-02](ar-02-strategy-and-lifecycle.md) §6） |
| `config` | 保留并扩展 | 增加 Run Store 与 pack 配置项 |
| `cat` | 保留 | 与清理无关的终端彩蛋，正交 |

**注意**：去掉兼容负担指的是**命令面与数据模型可自由重设计**，不是重写底层算法。成熟的
探测器、文件计量、进程/句柄探测、同卷 Trash 执行器与 live recheck 仍**迁移复用**（见
[AR-04](ar-04-run-store-and-execution.md) §6、[_index](_index.md) §5 复用锚点），这是产品
的好想法，与兼容无关。

## 6. 目标逻辑结构

无兼容负担下，目标代码布局直接采用下述分层（`implementation/openclean/` 子目录，**不搬
`src/`**——搬迁是纯 churn、无功能收益）。仍按 [AR-07](ar-07-implementation-roadmap.md)
纵向切片迁移，不一次性大爆炸重排：

```text
implementation/openclean/
├── cli.py
├── core/          models.py（Finding/Run/…）· identifiers.py · errors.py
├── strategies/    models.py · loader.py · registry.py · validator.py
├── runtime/       inspect_service.py · run_store.py · show_service.py · finding_projection.py（迁移期过渡）
├── explore/       service.py · tree_summary.py
├── detectors/     filesystem_tree.py · retention.py · sqlite_freelist.py · codex_transient.py ·
│                  crashpad.py · updater.py · browser_cache.py · open_unlinked.py
├── probes/        filesystem.py · mounts.py · processes.py · open_files.py · docker.py
├── actions/       planner.py · guards.py · executor.py · move_to_trash.py · specialized.py
└── lab/           observations.py · compare.py · validate.py · promote.py
```

其中 `detectors/`、`probes/` 与 `actions/` 的绝大部分能力**已存在**于当前
`storage_diagnostics.py`、`filesystem.py`、`processes.py`、`updater.py`、`cleanup.py`
和 `macos.py`；v1 的工作是为它们套上 Strategy/Finding 契约，而不是重写算法。

## 7. 推进顺序（纵向切片，不是范围裁剪）

完整目标已在 [AR-07](ar-07-implementation-roadmap.md)/[AR-08](ar-08-strategy-pack-catalog.md)
定义；实现按纵向切片推进，避免一次性大爆炸重写制造巨量 diff 却不先证明闭环：

- 先跑通 Codex 一条链路（模型接缝 → inspect/show → Finding 驱动 clean → Agent Contract），
  再扩展 Qoder/WorkBuddy 与系统/浏览器/Docker pack；
- `Item`→`Finding` 是**真正重构**（无兼容负担），但按切片逐域迁移，不搞一次性全量替换；
- `storage_diagnostics.py` 的拆分随 detector 迁移分步进行，不一次性重排；
- `implementation/`→根 `src/` 搬迁是纯 churn、无功能收益，可最后做或不做。

经典命令面在当前阶段与 Agent 命令面并存，迁移策略见 [AR-07](ar-07-implementation-roadmap.md)。

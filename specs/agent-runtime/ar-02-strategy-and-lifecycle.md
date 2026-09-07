# AR-02 · 策略与生命周期

[契约索引](_index.md) · [AR-01 对象模型](ar-01-object-model.md) ·
[AR-04 Run Store 与执行](ar-04-run-store-and-execution.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件定义 Strategy / StrategyPack 的结构、`status` 生命周期语义、同 ID 版本与冲突规则，
> 以及 KnowledgeBase 作为最高优先级安全闸的关系。

## 1. StrategyPack 与 Strategy 的关系

策略按**目标工具/领域**打包（`codex`、`qoder`、`workbuddy`、`macos-system`、
`developer-tools`、`browsers`、`docker`），而不是按来源打包。一个 pack 内的策略可以
同时携带来自不同来源的 `provenance`：

```text
codex 策略包
├── 来自 CleanMyMac 观察的缓存识别
├── 来自个人经验的 marketplace staging
├── 来自 AI 研究的 SQLite 空闲页诊断
└── 同时得到两边证据支持的 Crashpad 配对规则
```

P0 只交付第一个 `codex` pack；其余 pack 属 P1+（见 [AR-00](ar-00-architecture.md) §7）。

## 2. Strategy 结构

字段骨架见 [AR-01](ar-01-object-model.md) §3。核心字段职责：

| 字段 | 职责 | 约束 |
|---|---|---|
| `id` | 全局唯一策略标识 | 命名 `<pack>.<subject>.<qualifier>`，稳定不随版本变 |
| `version` | 同 ID 的单调演进序号 | 整数，只增不减 |
| `pack` | 所属策略包 | 必须等于 `id` 前缀所属 pack |
| `status` | 生命周期状态 | `draft`/`active`/`trusted`/`deprecated`（§3） |
| `provenance` | 来源记录 | 数组，元素含 `kind` 与 `observation_id`（§5） |
| `locator` | 在哪里寻找 | `roots` 为 `~`/绝对路径；不跟随 symlink |
| `detector` | 用哪个内置探测器 | `name` 必须在内置白名单（§2.1） |
| `conditions` | 命中条件 | 如 `minimum_age_days`、`require_structure_match`、`require_no_open_handles` |
| `guards` | 阻断/保护条件 | 如 `do_not_generalize_parent`、`protect_running_processes` |
| `assessment` | 分类与风险 | `classification`、`certainty`、`action_risk` |
| `recommendation` | AI 解释模板 | `summary`、`do_not_do`（负向约束） |
| `action` | 处理动作 | `name` 在白名单，`supported` 是否已验证可执行 |

### 2.1 JSON 不含可执行代码

- `detector.name` 必须来自 Python 内置探测器白名单（模块级全集见
  [AR-08](ar-08-strategy-pack-catalog.md) §3）。codex pack 用到的 subkind/evidence.kind 直接
  映射现有函数（`implementation/openclean/storage_diagnostics.py`、`updater.py`）：
  `codex_marketplace_staging`、`codex_git_skeleton`、`crashpad_pairing`、`sqlite_freelist`、
  `retention`、`darwin_temp_updater`、`open_unlinked`、`codex_log_partition`、`updater_staging`。
- `action.name` 必须来自内置动作白名单。P0 只有 `move_to_trash`（复用
  `cleanup.py` 的同卷 Trash 执行器）；`specialized`（如 SQLite 压缩）默认 `supported=false`。
- **禁止**：任意 shell、任意 Python 表达式、通用 `{delete, absolute_path}` 动作、把扫描
  逻辑发展成可执行 DSL。JSON 只负责位置、参数、guard、说明与 provenance；复杂判定留在
  经过测试的 Python detector 中。

## 3. status 生命周期矩阵（决策 4）

四种状态，语义必须硬：

| `status` | 普通 `inspect` | 产生 Finding | 产生 CleanupPlan | 执行 |
|---|---:|---:|---:|---:|
| `draft` | 否 | 仅 `lab` | 否 | 否 |
| `active` | 是 | 是 | 否 | 否 |
| `trusted` | 是 | 是 | 条件满足时可以 | 用户授权后可以 |
| `deprecated` | 否 | 否 | 否 | 否 |

状态迁移只能由显式人工动作驱动（`lab promote` / `lab demote`，见
[AR-03](ar-03-cli-and-io-contract.md) §6）：

```text
draft ──validate(正例+负例)──► active ──动作前后验证──► trusted
  ▲                                                      │
  └──────────────── demote（版本变化/实验推翻）───────────┘──► deprecated
```

补充不变量：

- **CleanMyMac 识别过某目录，不自动意味着策略可进入 `trusted`。** `trusted` 必须有正例、
  负例和动作前后验证证据（见 [AR-05](ar-05-research-governance.md) §3、
  [AR-06](ar-06-codex-p0-acceptance.md) §3）。
- **`trusted` 只表示 Strategy 具备经验证的动作能力，不表示每次 Finding 都 actionable。**
  即使策略是 `trusted`，只要应用正在运行、句柄状态未知、路径 identity 改变或命中 protect，
  当前 Finding 仍必须 `actionable=false`（运行时判定见 [AR-04](ar-04-run-store-and-execution.md) §4）。
- `active` 策略产生的 Finding 在 `clean` 预览中必须明确返回 `action.supported=false` /
  `can_execute=false`，`block_reasons` 含 `strategy_not_trusted`。

## 4. 同 ID 版本与冲突规则（决策 7）

### 4.1 版本

- 同一 `id` 的策略通过 `version` 单调演进；运行时加载每个 `id` 的最高有效版本。
- Run 固化本次加载的 `strategy_pack_hashes`（见 [AR-01](ar-01-object-model.md) §4）。
  执行前若当前 pack hash 与 Run 记录不一致，`can_execute=false`（`strategy_hash_mismatch`），
  要求重新 `inspect`，而不是用旧 Finding 假装有效。
- 降低 `status`（如 `trusted`→`deprecated`）也必须提升 `version`，保证审计可追溯。

### 4.2 冲突与优先级

不同来源可能冲突（CleanMyMac 判为 AI Junk、个人经验说含未完成任务、AI 只认某子目录为旧
缓存）。**最终不取并集**，按下列顺序决策，越具体、越保守者优先：

```text
明确保护规则（KnowledgeBase protect / ignore）
  > 排除条件（Strategy guards / conditions）
  > 运行状态与打开句柄（process markers / open handles）
  > 具体子结构策略（更深的 locator）
  > 通用父目录策略（更浅的 locator）
  > 参考产品分类结果（Observation）
  > AI 临时假设（draft）
```

- 任何阻断条件都可以取消清理动作。
- CleanMyMac 的识别结果可以提高研究价值，但**不能覆盖 OpenClean 自己的保护条件**。
- 冲突在**策略生产期**解决（编译进 pack），运行时不做动态合并（见
  [AR-00](ar-00-architecture.md) §2）。

## 5. provenance

每条策略保留来源记录，但来源不决定组织方式：

```json
"provenance": [
  {"kind": "cleanmymac_observation", "observation_id": "obs:cleanmymac:codex:003"},
  {"kind": "personal_experience",   "observation_id": "obs:personal:codex:002"},
  {"kind": "ai_research",           "observation_id": "obs:ai:codex:007"}
]
```

- `kind ∈ {cleanmymac_public, cleanmymac_observation, personal_experience, ai_research}`。
- `observation_id` 指向去标识化 Observation（见 [AR-05](ar-05-research-governance.md)）。
- `provenance` 为空时策略只能是 `draft`；晋级 `active`/`trusted` 必须补齐证据链。

## 6. 与 KnowledgeBase 的关系（最高优先级安全闸）

现有 `KnowledgeBase`（`implementation/openclean/knowledge_base.py`）只接受
`schema_version`、`ignore`、`protect`、`applications`、`_managed`，**没有** locator/detector/
conditions/guards/assessment/action/provenance/status。因此：

- Strategy Registry 是**新增**能力，不是把 KnowledgeBase 改名。
- KnowledgeBase 继续承担全局 `protect` 与用户 `ignore`，并作为 Strategy 执行**之前**的
  最高优先级安全闸。现有 `ProtectionGate`（`implementation/openclean/predicates.py`）已经
  保证 KB 保护先于普通谓词求值；v1 沿用这一顺序：**KB 命中 → 直接阻断，不再进入 detector。**
- Strategy 不得改写 KB；KB 的 `protect`/`ignore` 优先于任何 Strategy 的 `locator`。
- 用户 `ignore`（`ignore add/remove`）与托管 `knowledge.json` 的分层合并、原子写与
  `0600` 权限保持不变（见 [implementation/README.md](../../implementation/README.md) 自建规则节）。

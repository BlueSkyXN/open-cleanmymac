# AR-08 · 策略包全景

> **0.24.0a1 当前实施约束**：P0a 只读与计划预览；7 条 Codex 策略均 active/report_only。
> 经典命令和 TUI 保留。P0b 结构匹配与正式策略审批尚未完成。本文的长期扩展不等于当前已实现。
> 本次落地语义以 [当前实现补充](ar-09-current-implementation.md) 为准。


[契约索引](_index.md) · [AR-02 策略与生命周期](ar-02-strategy-and-lifecycle.md) ·
[AR-07 实施路线图](ar-07-implementation-roadmap.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件定义完整的多 pack 目标：策略包目录、detector/action 白名单全集，以及 P0-P4 展开顺序。
> Codex 是首个切片（[AR-06](ar-06-codex-p0-acceptance.md)）；其余 pack 复用同类现有能力。

## 1. 组织原则

策略按**被研究的工具或领域**组织，不按来源组织；来源保留在每条 strategy 的 `provenance`
（[AR-02](ar-02-strategy-and-lifecycle.md) §5）。一个 pack 内可同时含来自 CleanMyMac 观察、
个人经验与 AI 研究、且经两边证据支持的策略。

## 2. 策略包目录（全集）

| pack | 目标领域 | 主要 detector | 现有代码基础 | 展开阶段 |
|---|---|---|---|---|
| `codex` | OpenAI Codex / ChatGPT | `codex_transient`、`retention`、`sqlite_freelist`、`crashpad`、`updater`、`open_unlinked` | `storage_diagnostics.py`（marketplace staging / git skeleton / crashpad / sqlite / retention）、`updater.py`；marker `ChatGPT.app/Codex.app/codex` | **P0** |
| `qoder` | Qoder CLI/runtime | `retention`、`updater`、`filesystem_tree` | `storage_diagnostics.py`（Qoder runtime/解压目录/ShipIt）、`updater.py` | P1 |
| `workbuddy` | WorkBuddy | `retention`、`updater`、`filesystem_tree` | `storage_diagnostics.py`（日志/traces/audit-log 保留期）、`updater.py`、process ownership | P1 |
| `claude` / `cursor` / 其他 AI | Claude、Cursor、Gemini、OpenCode、Chrome DevTools MCP 等 | `updater`（版本目录）、`retention`、`filesystem_tree` | ai 域扫描点；版本目录只报告非当前完整旧版本，保留零字节 marker/当前 binary/会话状态 | P1 |
| `macos-system` | 用户缓存、日志、Xcode、失效启动项 | `filesystem_tree`、`retention`、`updater`、`startup_items` | system 域扫描点、`startup_items.py`、`macos.py`（Darwin cache/tmutil）；系统特权区保持 `requires_privilege`→fail-closed | P2 |
| `developer-tools` | pip/uv/npm/Go/Cargo/Homebrew 缓存 | `filesystem_tree`、`retention` | developer 域扫描点 | P2 |
| `browsers` | Chrome/Brave/Edge/Comet `Service Worker/CacheStorage` | `browser_cache` | profile 级只读汇总已实现；origin 级需稳定可脱敏数据源，未做；不删 Cookies/Login Data/IndexedDB/整个 profile | P2 |
| `docker` | Docker daemon 容量 | `docker`（probe） | `docker.py`：三类 prune 白名单、Local Volumes 永不可执行；真实 daemon 验收待做（TODO #4） | P2 |
| `project-artifacts` | `node_modules`/`.venv`/`target`/DerivedData 等可重建产物 | `filesystem_tree` + 项目发现 | 现有 `purge`/project 域；可重建产物 `move_to_trash` | P2 |

高风险能力（系统特权区、浏览器 origin、Docker 真实 prune、SQLite 压缩）保持只读或
`action.supported=false`，直到有充分正负案例与动作验证（[AR-05](ar-05-research-governance.md) §3）。

## 3. detector 白名单（全集）

`strategy.detector.name` 必须来自此白名单（[AR-02](ar-02-strategy-and-lifecycle.md) §2.1）；
每个 detector 是经受测试的 Python 代码，JSON 只传参数，不含逻辑。

| detector | 职责 | 迁移自 |
|---|---|---|
| `filesystem_tree` | 目录枚举 + 物理/逻辑计量（硬链接去重、不跟随 symlink、dataless 阻断） | `filesystem.py`、`scanpoints.py` |
| `retention` | 文件数、句柄、7/14/30 天保留期容量 | `storage_diagnostics.py` retention |
| `sqlite_freelist` | 内部空闲页/比例、WAL/SHM/journal（只读） | `storage_diagnostics.py` sqlite |
| `codex_transient` | marketplace staging、git 空壳等 Codex 临时结构 | `storage_diagnostics.py` codex |
| `crashpad` | 孤立 sidecar 与配对/近期 artifact 计数（不删 `.dmp`） | `storage_diagnostics.py` crashpad |
| `updater` | updater 根、bundle 版本比较、暂存状态 | `updater.py` |
| `open_unlinked` | 已删除仍占用句柄的文件（`potential_bytes=0`，只报告上限） | `storage_diagnostics.py` open-unlinked |
| `browser_cache` | 浏览器 CacheStorage profile 级只读汇总 | 浏览器扫描点 |
| `docker` | Docker daemon 只读容量 + target binding | `docker.py` |
| `startup_items` | 失效启动项识别与 live 复核 | `startup_items.py` |

detector 是**模块级**白名单名；细分变体通过 `detector.params.subkind` 表达并体现在
`evidence.kind`（如 `codex_transient` 的 `codex_marketplace_staging`/`codex_git_skeleton`、
`updater` 的 `updater_staging`/`darwin_temp_updater`、`crashpad` 的 `crashpad_pairing`）。
[AR-02](ar-02-strategy-and-lifecycle.md) §2.1 与 [AR-06](ar-06-codex-p0-acceptance.md) §2
出现的细粒度名即这些 subkind / evidence.kind。

probes（被 detector 复用的只读原语，非独立 detector）：`filesystem`、`mounts`、`processes`、
`open_files`、`docker`（`probes/`，见 [AR-00](ar-00-architecture.md) §6）。

## 4. action 白名单（全集）

`strategy.action.name` 必须来自此白名单；执行全部经 [AR-04](ar-04-run-store-and-execution.md)
的 `can_execute` 与 live guard。

| action | 语义 | 复用 | 约束 |
|---|---|---|---|
| `move_to_trash` | 移到同卷 Trash（可恢复） | `cleanup.py` `trash_directory_for`/`_move_to_trash`（no-follow + `renameatx_np`） | 目标属当前用户、非云占位、identity 复核 |
| `empty_trash` | 清空 Trash 内容（永久） | `cleanup.py` `_empty_trash` | `critical` + 精确选择；保留 Trash 根 |
| `docker_prune` | 固定三类 prune（永久） | `cleanup.py` `_prune_docker_item`/`docker.py` | identifier 精确选择；Volumes 永远拒绝；binding 复核 |
| `specialized` | 领域专用（如 SQLite 压缩） | 待建 | 默认 `supported=false`；需独立原子输出/恢复/前后验证方案 |
| `report_only` | 只报告，不产生动作 | — | 只读诊断（retention/sqlite/open_unlinked/crashpad/updater_temp）固定此项 |

**禁止**：通用 `{delete, absolute_path}`、任意 shell、任意表达式（[AR-02](ar-02-strategy-and-lifecycle.md) §2.1）。

## 5. P0-P4 展开顺序

按"已有代码基础最强、风险最低"优先，逐 pack 纵向闭环（每个 pack 走 inspect→show→clean→
Agent Contract 一条链，再进下一个）：

```text
P0  codex                                     ← 首个切片，含一个经动作验证的 trusted 策略
P1  qoder → workbuddy → claude/cursor/其他 AI  ← 已有 retention/updater/process ownership 基础
P2  macos-system → developer-tools → browsers → docker → project-artifacts
P3  研究工具完善（lab capture/compare/draft/validate/promote 自动化）
P4  Agent 接入稳定（稳定 JSON CLI）→ MCP 薄适配（按需）
```

Qoder 与 WorkBuddy 已有 retention、updater、process ownership 代码，是 Codex 后最适合迁移的
两个 pack。系统/浏览器/Docker 的高风险动作在对应阶段仍保持只读或 `supported=false`，直到
证据充分。远程策略发布不在 P0-P4，属外部前提（[AR-07](ar-07-implementation-roadmap.md) §4）。

## 6. 每 pack 的验收基线

任一 pack 落地都复用 [AR-06](ar-06-codex-p0-acceptance.md) §3 的 Agent Contract 测试矩阵，
外加 pack 专属正负案例：

- 正例：该工具的典型可识别结构被 `inspect <pack>` 命中且只归该 pack；
- 负例：当前版本 runtime、活动会话/未完成任务、零字节更新 marker、云占位/dataless、
  受保护配对（如 Crashpad `.dmp`）**不被**标为 actionable；
- 若含 `trusted` 动作：`TemporaryDirectory` 上跑通动作前后验证与执行回执。

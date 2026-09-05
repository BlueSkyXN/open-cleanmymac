# AR-07 · 实施路线图与代码转化

[契约索引](_index.md) · [AR-00 架构](ar-00-architecture.md) ·
[AR-06 Codex 首切片验收](ar-06-codex-p0-acceptance.md) ·
[AR-08 策略包全景](ar-08-strategy-pack-catalog.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件把完整目标拆成纵向切片阶段，给出现有模块到目标结构的逐模块转化表，以及实体产物
> （packs/schemas/research/tests）的目标位置。**去兼容负担**：不建 adapter 层，旧命令按
> **P0 纠偏注记（2026-09）**：当前阶段经典命令面与 Agent 命令面并存。
> 下文中的“退役”指未来目标状态，不是当前实施步骤。

## 1. 阶段序列（纵向切片）

每个阶段都是一条可独立验证的纵向切片，不做一次性大爆炸重排。P0 = 阶段 A–D（Codex Agent
Runtime v1，验收见 [AR-06](ar-06-codex-p0-acceptance.md)）。

| 阶段 | P 级 | 内容 | 退出条件 |
|---|---|---|---|
| A 核心模型 + Strategy Registry | P0 | `Item`→`Finding` 重构接缝；`Strategy`/`StrategyPack`/`Run`/`CleanupPlan` 模型；loader/validator/registry | 模型与 schema 定稿；registry 能加载并校验一个 pack；`Item` 仍暂供旧路径 |
| B Codex pack + inspect/show + Run Store | P0 | 第一个 `codex` pack（全部 `active`/只读）；`inspect codex`；`show --run --finding`；本地私有 Run Store | `inspect codex` 只返回 codex Finding；Run 可跨命令解析；`show` 输出完整证据 |
| C Finding 驱动 clean + 首个 trusted | P0 | `clean --run --finding [--yes]` 复用 `execute_cleanup`；Run Store 解析目标；一个经动作验证的 `trusted` 策略 | 预览固定 `executed=false`；`trusted`+授权+live guard 跑通 `TemporaryDirectory` 执行回执 |
| D Agent Contract + 命令面并存 | P0 | Agent Contract 正负测试矩阵；经典与 Agent 命令面并存；JSON 围绕 Run/Finding 扩展 | [AR-06](ar-06-codex-p0-acceptance.md) §3 全部通过；两套命令面复用 cleanup.py |
| E explore + Observation + lab | P1 | `explore`（树摘要 + 预算）；Observation schema；`lab capture/compare/draft/validate/promote/demote` 最小可用 | `explore` 只读、输出不进 cleanup；Observation 可人工维护；draft→active→trusted 走通 |
| F 扩展 AI 工具 pack | P1 | Qoder、WorkBuddy、Claude、Cursor、其他 AI 工具 pack（见 [AR-08](ar-08-strategy-pack-catalog.md)） | 各 pack `inspect` 精准识别；复用现有 retention/updater/process ownership 代码 |
| G 系统/开发/浏览器/Docker/项目 pack | P2 | macos-system、developer-tools、browsers、docker、project-artifacts pack | 各 pack 识别落地；高风险动作保持只读或 `supported=false` |
| H 研究工具完善 + Agent 接入 | P3–P4 | 完整 `lab` 自动化；稳定 JSON CLI；MCP 薄适配（按需） | 研究闭环自动化；Agent 接入契约稳定 |

阶段 A–D 是"数周级"的核心闭环；E–H 在其稳定后展开。**远程策略发布服务**（HTTPS channel、
公钥钉扎、签名）不在本路线图，属 [implementation/TODO.md](../../implementation/TODO.md) 的
外部前提项。

## 2. 逐模块转化表

现有模块（`implementation/openclean/`）到目标结构（[AR-00](ar-00-architecture.md) §6）的转化。
“迁移复用”= 保留算法、换契约外壳；“并存”= 当前阶段保留经典入口，待能力等价后决策。

| 当前模块 | 目标 | 转化方式 |
|---|---|---|
| `models.py`（`Item`） | `core/models.py`（`Finding`/`Run`/`CleanupPlan`） | **真正重构**：`Item` 大平面字段重组为 Finding 核心 + 类型化 `evidence`（[AR-01](ar-01-object-model.md) §5-6）；迁移期用 `finding_projection.py` 过渡，逐域完成后 `Item` 退役 |
| `scanpoints.py` | `packs/*.json` 的 `locator` + detector 引用 | 静态扫描点转为 pack 内 strategy 的位置与 detector 白名单引用 |
| `storage_diagnostics.py` | `detectors/*`（拆分） | 按诊断类型拆为 `retention`/`sqlite_freelist`/`codex_transient`/`crashpad`/`open_unlinked` 等 detector；算法保留 |
| `engine.py` | `runtime/inspect_service.py`（planner + runner） | 五域编排改为：按 pack 加载 strategy → 调 detector → 产 Finding → 写 Run |
| `cleanup.py` | `actions/planner.py` + `actions/executor.py` | 入口从 Run Store 解析 Finding；`execute_cleanup`/`_audit_item`/`trash_directory_for`/live guard **完整复用** |
| `knowledge_base.py` | `strategies/registry.py` + 保留 protect/ignore 闸 | 新增 strategy 加载/校验/注册；KB 继续做 Strategy 之上最高优先级保护闸（[AR-02](ar-02-strategy-and-lifecycle.md) §6） |
| `predicates.py`（`ProtectionGate`） | 保留 | KB 先于普通谓词的安全闸顺序不变 |
| `task_graph.py` | 内部执行器（降级） | 不再是核心产品概念，仅供 `inspect` 内部并发调度 |
| `progress.py` | detector 完成数 + entry 计数 | 从固定权重启发式改为 detector/entry 计数 |
| `analyzer.py` | `explore/tree_summary.py` | 通用路径探索 + 树摘要 + 深度/数量/时间/容量预算 |
| `filesystem.py`/`processes.py`/`docker.py`/`updater.py`/`macos.py` | `probes/*` + `actions/*` | 探测与执行原语，被 detector/action 复用 |
| `redaction.py` | 保留 | 脱敏 + actionable ID 不可 replay（[AR-03](ar-03-cli-and-io-contract.md) §7） |
| `cli.py` | Agent-first 命令面 | `inspect/explore/show/clean/strategy/config/lab`；经典命令当前并存 |
| `application_ownership.py`/`startup_items.py`/`application_languages.py` | 归入相应 pack 的 detector | 归属/启动项/语言包判定作为 pack detector 的证据来源 |
| `tui.py`/`space_tui.py`/`navigator.py` | 退役或降级 | 非 Agent 接口；是否保留人类审阅 UI 是实现期取舍 |
| `docs/AI_USAGE.md` | 升级为核心 Agent Contract | 从"停在建议、禁止 `--yes`"升级为含执行授权语义的 Agent Contract（[AR-04](ar-04-run-store-and-execution.md) §7） |

## 3. 实体产物目标结构

契约文档只内嵌示意 JSON；实现阶段落地为下列实体文件。**pack/schema 作为包数据随 wheel
分发**（放 `implementation/openclean/` 下），`research/` 是治理材料（放仓库根，`raw/` 不提交、
不进包）。

```text
open-cleanmymac/
├── implementation/
│   ├── openclean/
│   │   ├── packs/                 # 策略包（包数据，随 wheel 分发）
│   │   │   ├── codex.json  qoder.json  workbuddy.json  claude.json  cursor.json
│   │   │   ├── macos-system.json  developer-tools.json  browsers.json
│   │   │   └── docker.json  project-artifacts.json
│   │   ├── schemas/               # 机器可读 schema（包数据）
│   │   │   ├── observation-v1.schema.json   strategy-v1.schema.json
│   │   │   ├── finding-v1.schema.json       run-v1.schema.json
│   │   │   ├── inspect-result-v1.schema.json
│   │   │   └── cleanup-plan-v1.schema.json  cleanup-outcome-v1.schema.json
│   │   └── …（core/strategies/runtime/detectors/probes/actions/explore/lab，见 AR-00 §6）
│   └── tests/                     # 扁平 test_*.py（Makefile `discover -s tests` 不递归无 __init__.py 的子目录）
│       └── test_agent_{models,identifiers,projection,strategy_registry,run_store,
│           inspect,planner,clean_exec,contract}.py   # contract = AR-06 §3 正负矩阵
└── research/                      # 研究治理材料（见 AR-05 §5）
    ├── README.md  scenarios/
    ├── observations/{cleanmymac,personal}/
    └── raw/                       # .gitignore 排除，不提交、不进包
```

约束：

- `packs/*.json` 只引用内置 detector/action 白名单（[AR-02](ar-02-strategy-and-lifecycle.md) §2.1），
  不含可执行代码；`schemas/*.json` 与 [AR-01](ar-01-object-model.md) 对象字段一一对应。
- `tests/` 一律扁平 `test_agent_*.py`（**不建子目录**，否则 `Makefile:21 discover -s tests`
  对无 `__init__.py` 的子目录静默跳过）；夹具用 `TemporaryDirectory` 内联合成，不得含真实
  用户路径、容量、内容或机器标识（[AR-05](ar-05-research-governance.md) §2）。
- `research/raw/` 必须被 `.gitignore` 排除；wheel/sdist 不含 `research/`。

## 4. 明确排除（不跑题）

- **远程策略发布服务**（自建 HTTPS channel、离线签名私钥、正式公钥、sequence/rotation）：
  外部前提未满足，见 [implementation/TODO.md](../../implementation/TODO.md)；本路线图不含。
- **`implementation/`→根 `src/` 搬迁**：纯 churn、无功能收益，可最后做或不做。
- **特权 XPC / `optimize ram|purgeable` 执行器**：与本契约无关，保持 fail-closed
  （见 [specs/04-ipc-protocol.md](../04-ipc-protocol.md) 与 TODO）。
- **在契约阶段落地实体 packs/schemas/tests 文件**：属实现阶段（A–D），本轮只定契约与目标结构。

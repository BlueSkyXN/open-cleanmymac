# AR-05 · 研究治理

> **0.24.0a1 当前实施约束**：P0a 只读与计划预览；7 条 Codex 策略均 active/report_only。
> 经典命令和 TUI 保留。P0b 结构匹配与正式策略审批尚未完成。本文的长期扩展不等于当前已实现。
> 本次落地语义以 [当前实现补充](ar-09-current-implementation.md) 为准。


[契约索引](_index.md) · [AR-02 策略与生命周期](ar-02-strategy-and-lifecycle.md) ·
[贡献指南](../../CONTRIBUTING.md) · [知识库规格](../03-knowledge-base.md)

> OpenClean 自有前瞻契约，非 CleanMyMac 参考事实。状态：📐 契约已定，未实现。
> 本文件定义 Observation 的公开提交边界（决策 6）、`research/` 目录治理，以及研究流程如何
> 与 [CONTRIBUTING.md](../../CONTRIBUTING.md) 的净室红线保持一致。

## 1. 为什么需要研究治理

目标方案在现有净室边界之外新增了三种经验来源：

- 自己 Mac 上的 CleanMyMac 黑盒实验；
- 个人经验与怀疑点；
- AI 自主研究。

方向与净室实现兼容，但**必须新增明确规则**，否则未来 `research/` 会与当前净室边界冲突。
本文件把这些规则定死，作为 [AR-02](ar-02-strategy-and-lifecycle.md) 策略生产的前置约束。

## 2. Observation 公开提交边界（决策 6）

沿用 [CONTRIBUTING.md](../../CONTRIBUTING.md)「净室边界」与
[specs/03-knowledge-base.md](../03-knowledge-base.md) §5「净室实现边界」，并针对 Observation 明确：

| 允许提交 | 禁止提交 |
|---|---|
| 去标识化的**行为事实**（结构形状、分类、观察方式、限制） | 真实用户路径、真实容量数字、文件内容 |
| 通用命名依据或公开来源引用 | 机器标识、主机名、用户名、绝对真实路径 |
| 负案例的**结构描述** | 参考软件代码、反编译表达、私有规则库、商业扫描指纹 |
| 版本/环境标签（不含机器唯一标识） | `raw/` 原始扫描输出、截图、日志中的真实数据 |

硬性规则：

- **`research/raw/` 不提交**（`.gitignore` 排除），只在本机保留原始材料。
- 提交的 Observation 只能是**去标识化行为事实**：路径用形状模板（如
  `~/.codex/.tmp/marketplaces/.staging/marketplace-upgrade-*`）而非真实条目名；容量用
  量级或合成值而非真实机器读数。
- **CleanMyMac 命中不自动生成 action**：观察只提高研究价值，转化为 Strategy 后仍须独立
  判断结构、误判条件、是否仍被使用、能否重建、只报告还是可清理、Trash 还是专用动作
  （见 [AR-02](ar-02-strategy-and-lifecycle.md) §4.2）。
- **禁止引入原厂私有规则**：不得把 CleanMyMac 的 `.cmmkb`、私有规则明细或商业指纹复制进
  Observation 或 Strategy；这与 [implementation/TODO.md](../../implementation/TODO.md) 的
  知识库发布源约束一致。

## 3. 从 Observation 到 trusted 的证据要求

策略晋级必须补齐证据链，`trusted` 门槛最高（与 [AR-02](ar-02-strategy-and-lifecycle.md) §3 一致）：

```text
Observation（去标识化事实）
   → lab draft（AI/人提出假设，status=draft）
   → lab validate（正例 + 负例）→ status=active
   → 动作前后验证（before/after snapshot，可重建测试数据）→ status=trusted
```

| 晋级 | 必备证据 |
|---|---|
| `draft`→`active` | 至少一个正例（应命中）+ 至少一个负例（**不应**命中，如仍在使用的会话/未完成任务/当前版本） |
| `active`→`trusted` | 正例、负例，**以及动作前后验证**：在人工构造的可重建测试数据上跑通 `move_to_trash`，记录 before/after 与回执 |

不变量：

- **AI draft 不能自动晋级**：`lab promote`/`demote` 是显式人工动作（见
  [AR-03](ar-03-cli-and-io-contract.md) §6）。
- **CleanMyMac 识别过 ≠ 可 trusted**：参考产品命中只是 `provenance` 之一，不替代动作验证。
- 负案例设计必须覆盖：目录名含 `tmp`/`cache` 但仍在使用的结构、当前版本 runtime、
  未完成任务/会话状态、零字节更新 marker、云占位/dataless。

## 4. 对比实验治理

真实 Mac 是重要研究环境，但必须区分扫描实验与清理实验（与
[implementation/TODO.md](../../implementation/TODO.md) 的测试约束一致）：

- **扫描实验**：同一时点依次运行参考产品只读扫描与 `openclean inspect`（只读），记录
  是否发现、分类、报告容量、默认选择、运行时变化、遗漏或扩大范围。只读，不改环境。
- **清理实验**：**不能**「参考产品先清理，OpenClean 再清理」——第一步已改变环境。应在
  **可重建实验目录或测试账号**上：before snapshot → 运行一个工具 → after snapshot →
  恢复 → 运行另一个工具 → 比较。
- 真实用户目录适合**发现线索**；动作行为尽量用**人工构造的测试数据**验证。
- 测试写操作只能使用 `TemporaryDirectory`；不得在真实 `HOME` 或真实 Docker daemon 上跑
  `--yes`（沿用 [CONTRIBUTING.md](../../CONTRIBUTING.md)「变更原则」第 6 条）。

## 5. `research/` 目录治理

目标结构（P0 只要求 Codex 相关的最小子集，其余留待 P1+）：

```text
research/
├── README.md                 # 治理规则与去标识化清单
├── scenarios/                # 可重建实验场景定义（合成数据）
├── observations/
│   ├── cleanmymac/           # 去标识化参考产品观察
│   └── personal/             # 去标识化个人经验线索
└── raw/                      # 原始材料，.gitignore 排除，不提交
```

治理规则：

- `research/raw/` 必须被 `.gitignore` 排除；提交前用 `git diff --check` 与人工审阅确认无真实
  路径/容量/内容/机器标识泄漏。
- `observations/` 下的每个文件对应一个 `observation_id`，符合
  [AR-01](ar-01-object-model.md) §2 的 Observation 结构与本文件 §2 的去标识化要求。
- `scenarios/` 只放合成夹具定义；真实机器扫描结果不入仓库。
- 研究材料**不进入** wheel/sdist 运行时包；是否随 sdist 分发由发行审阅单独决定，且不得包含
  `raw/`（与 [CONTRIBUTING.md](../../CONTRIBUTING.md)「检查门」的归档约束一致）。

## 6. 与净室边界的一致性声明

本治理不放宽任何现有红线：

- 仍只依据 `specs/`、公开文档和可独立验证的通用 macOS 行为实现；个人经验与 AI 研究作为
  **研究线索**进入 Observation，最终仍须落回可独立验证的通用行为与保守安全级。
- 不读取、提交、引用或复制 `analysis/`；不把 `local/` 过程材料带入代码或文档。
- 新扫描点/新 Strategy 必须说明公开来源或通用命名依据，并采用保守安全级。
- Observation 与 Strategy 的公开提交内容接受与代码同等的净室审阅。

# specs/ · 设计规格索引

> 这是净室规格的**唯一事实来源**。实现（`implementation/`）只依据这里描述的内容。
> 阅读顺序见 `AGENTS.md` §3；本表给出每份规格的**实现状态**，便于接手者快速定位缺口。
>
> **范围语义**：部分章节记录参考对象或 Desktop 侧的背景能力，并不自动成为本项目的
> CLI 交付要求。是否进入当前实现范围，以本索引的 capability 状态、根 README 和
> `implementation/TODO.md` 为准；高风险能力可以明确选择只读或不实现。

| # | 规格 | 内容 | 实现状态 |
|---|---|---|---|
| 00 | [总体架构](00-architecture.md) | 进程模型、命令树、模块职责、设计事实 | 🟡 公开命令壳已落；`optimize` 无安全执行器，特权层未完成 |
| 01 | [扫描引擎](01-scan-engine.md) | 任务图、加权进度聚合、暂停/恢复/取消 | ✅ 通用 DAG、并发、加权进度、快照和三态控制已落 |
| 02 | [扫描点字典](02-scan-points.md) | ⭐ 所有"扫哪里"：路径、模式、安全等级 | 🟡 公开 CLI 清理域及项目/空间扫描已落；应用语言只读审计已落，lipo 未实现 |
| 03 | [知识库](03-knowledge-base.md) | 忽略/保护规则、应用→附加文件映射、JSON 格式 | 🟡 自建 JSON、用户 ignore、签名 HTTPS 更新与防回滚已落；正式发布 channel 未配置 |
| 04 | [IPC 协议](04-ipc-protocol.md) | XPC 特权操作、消息格式、防不当提权 | ⛔ external-prerequisite：需 native 签名链和真实安装验收 |
| 05 | [关键算法](05-algorithms.md) | 目录大小、硬链接去重、云文件、fat 瘦身 | 🟡 物理/逻辑大小、硬链接、云占位已落；lipo 未实现 |
| 06 | [系统流程](06-system-flow.md) | 数据流/控制流图（串起各模块） | 🟡 用户态清理、同卷 Trash、Docker 白名单和交互流已落；XPC/optimize 未完成 |
| 07 | [谓词引擎](07-predicate-engine.md) | 判定"是否忽略"的谓词系统（7 种谓词） | ✅ 组合谓词、KB 优先保护闸、名称/大小/存在/路径谓词已实现 |

## 图例

- ✅ 已实现并验证
- 🟡 部分实现（有基础，待补全）
- ❌ 未实现（是否可直接认领仍以 `implementation/TODO.md` 为准）
- ⛔ 外部前提未满足；不得当作普通代码任务直接启用

## 规格 ↔ 代码对照

| 规格 | 对应实现文件 | 缺口 |
|---|---|---|
| 02 扫描点 | `openclean/scanpoints.py`、`application_languages.py`、`startup_items.py` | ApplicationLanguages 删除有意不提供；universal binary 未实现 |
| 01 引擎 / 05 算法 | `openclean/engine.py`、`progress.py`、`task_graph.py`、`models.py` | lipo 未实现；DAG/进度已完成 |
| 00 命令树 / 06 流程 | `openclean/cli.py`、`cleanup.py`、`tui.py`、`space_tui.py` | `optimize`、特权 XPC |
| 07 谓词 / 03 知识库 | `openclean/predicates.py`、`knowledge_base.py`、`knowledge_update.py` | 正式自建规则发布 channel |

> 认领任务请去 [`implementation/TODO.md`](../implementation/TODO.md)。

## 交付状态分类

- **available**：当前代码可执行并有自动化验证；
- **read-only**：可以发现/报告，但没有写路径；
- **guarded-unavailable**：命令面存在，明确拒绝且非零退出；
- **external-prerequisite**：代码之外还需签名、服务或真实 daemon；
- **out-of-scope**：Desktop 背景事实，不属于当前 CLI 对齐目标。

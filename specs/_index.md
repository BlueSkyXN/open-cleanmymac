# specs/ · 设计规格索引

[README](../README.md) · [能力地图](../docs/CAPABILITIES.md) ·
[功能预览](../docs/PREVIEW.md) · [架构](../docs/ARCHITECTURE.md) ·
[安全](../SECURITY.md) · [实现说明](../implementation/README.md)

> 这是净室规格的**参考事实来源**。实现只依据这里描述的功能事实，不复用参考软件代码。
> 阅读顺序见 [AGENTS.md](../AGENTS.md)。本表给出每份规格的实现状态。
>
> **范围语义**：部分章节记录参考对象或 Desktop 侧的背景能力，并不自动成为本项目的
> CLI 交付要求。是否进入当前实现范围，以本索引、根 README、
> [docs/CAPABILITIES.md](../docs/CAPABILITIES.md) 和
> [implementation/TODO.md](../implementation/TODO.md) 为准。

| # | 规格 | 内容 | 实现状态 |
|---|---|---|---|
| 00 | [总体架构](00-architecture.md) | 进程模型、命令树、模块职责 | 🟡 公开命令壳已落；`optimize` 无安全执行器，特权层未完成 |
| 01 | [扫描引擎](01-scan-engine.md) | 任务图、加权进度、暂停/恢复/取消 | 🟡 DAG、并发、加权进度、快照和共享三态已落；Countable total、每任务 Control 聚合/observer 未落 |
| 02 | [扫描点字典](02-scan-points.md) | 扫哪里：路径、模式、安全等级 | 🟡 公开 CLI 的保守子集已落；并非内部/System Junk 字典逐项全覆盖 |
| 03 | [知识库](03-knowledge-base.md) | 忽略/保护规则、应用附加文件 | 🟡 自建 JSON、用户 ignore、签名更新客户端已落；应用附加字段尚未接生产扫描器，正式 channel 未配置 |
| 04 | [IPC 协议](04-ipc-protocol.md) | XPC 特权操作、防不当提权 | ⛔ external-prerequisite：需 native 签名链和真实安装验收 |
| 05 | [关键算法](05-algorithms.md) | 目录大小、硬链接、云文件、fat 瘦身 | 🟡 物理/逻辑大小、硬链接、云占位已落；lipo 未实现 |
| 06 | [系统流程](06-system-flow.md) | 数据流/控制流 | 🟡 用户态清理、同卷 Trash、Docker 白名单已落；XPC/optimize 未完成 |
| 07 | [谓词引擎](07-predicate-engine.md) | 是否忽略的谓词系统 | 🟡 CLI 使用子集已落；Reachability/FileAccess 专项语义未实现 |

## 图例

- ✅ 已实现并验证
- 🟡 部分实现
- ❌ 未实现（是否可认领仍以 `implementation/TODO.md` 为准）
- ⛔ 外部前提未满足；不得当作普通代码任务直接启用

## 本项目相对规格的差异

规格正文描述参考对象。下列差异是本项目有意选择，细节在能力地图和实现契约中：

- 规则存储用明文 JSON，不解析 `.cmmkb`。
- `openclean cat` 是原创终端猫彩蛋，不计入兼容性声明。
- ApplicationLanguages、universal binary thinning、日志/缓存/updater 诊断默认只读或未实现写入。
- `analyze` 只报告实际占用，不把占用自动标为可回收空间。
- 用户态删除走同卷 Trash；Docker 只开放固定 prune 白名单。
- 特权 XPC 与 `optimize ram|purgeable` 保持 fail-closed。

## 规格 ↔ 代码对照

| 规格 | 对应实现文件 | 缺口 |
|---|---|---|
| 02 扫描点 | `scanpoints.py`、`application_languages.py`、`startup_items.py`、`storage_diagnostics.py`、`updater.py` | System Junk 为公开 CLI 保守子集；项目内 `.Trash` 未启用；语言包写入与 lipo 有意不提供 |
| 01 引擎 / 05 算法 | `engine.py`、`progress.py`、`task_graph.py`、`models.py`、`filesystem.py` | lipo 未实现；Countable/Control 聚合与 observer 未完成 |
| 00 命令树 / 06 流程 | `cli.py`、`redaction.py`、`cleanup.py`、`tui.py`、`space_tui.py` | `optimize`、特权 XPC |
| 07 谓词 / 03 知识库 | `predicates.py`、`knowledge_base.py`、`knowledge_update.py` | Reachability/FileAccess、application fields 生产接线、正式规则 channel |

认领任务请去 [implementation/TODO.md](../implementation/TODO.md)。能力状态词见
[docs/CAPABILITIES.md](../docs/CAPABILITIES.md)。

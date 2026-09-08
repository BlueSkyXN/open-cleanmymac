# Agent 附加接口规格索引

> 文档 ID：AR-SPEC · 修订：2 · 更新：2026-09-08 · 状态：baseline（提案另标）
> 来源与效力继承 [OpenClean 规格索引](../_index.md)，产品目标以 [00](../00-architecture.md) 为准。

## 1. 本目录只负责接口扩展

Agent Runtime 是现有代码中的子系统名称，不是 OpenClean 的产品定义。
它增加跨命令 Run/Finding 证据引用、策略查询与清理计划，复用经典探测/执行内核。
人、脚本、Agent 均可调用经典命令；不将五域功能收窄为 AI 软件，不要求先完成 P0b 才能迭代产品。

用户指挥、Agent 实际使用的流程见 [AI_USAGE](../../docs/AI_USAGE.md)。
生产 pack 的写动作关闭，不代表所有经典清理动作关闭；支持的经典动作可在明确授权内执行。

## 2. 阅读导航

| 文档 | 内容 | 效力 |
|---|---|---|
| [AR-00](ar-00-architecture.md) | 附加层职责、经典共存、调用方边界 | baseline |
| [AR-01](ar-01-object-model.md) | 已有对象、投影、版本和字段 | baseline |
| [AR-02](ar-02-strategy-and-lifecycle.md) | pack 加载、状态、审批与来源声明 | baseline；启用动作需独立决定 |
| [AR-03](ar-03-cli-and-io-contract.md) | 可用命令、JSON、错误与脱敏 | baseline |
| [AR-04](ar-04-run-store-and-execution.md) | 本机存储、副作用、预检和实时执行 | baseline |
| [AR-05](ar-05-research-governance.md) | 个人经验与参考经验的证据治理 | baseline；不要求新建研究平台 |
| [AR-06](ar-06-codex-p0-acceptance.md) | 现有 Codex/WorkBuddy 接口验收 | baseline；保留旧路径便于引用 |
| [AR-07](ar-07-implementation-roadmap.md) | 被撤销路线与后续需求进入条件 | 旧路线 superseded；候选 change-pending |
| [AR-08](ar-08-strategy-pack-catalog.md) | 实际包/接线清单与声明边界 | baseline；不是全量迁移目录 |
| [AR-09](ar-09-current-implementation.md) | 当前状态入口、已发布与源码区别 | 状态导航，不覆盖其它有效契约 |

## 3. 不再有效的旧决定

原先的“面向 Agent 的策略引擎替代产品定义”、Item 全量退役、经典命令或 TUI 退役、
所有领域迁入 pack、无兼容负担自由重设 JSON，以及固定 Codex→其他 pack→lab→MCP 路线均已撤销。
不是仅延期，也不是待功能等价后自动启动。历史文字可从 Git 查阅，不能作为当前任务指令。

已有安全契约、源码接口和测试保留。本次不修改包数据、审批 hash、模型、schema 或运行时逻辑。
新增工作按用户功能需求说明收益和兼容性，不能以扩充框架模块为交付目标。

## 4. 当前能力摘要与验证

内置 Codex 与 WorkBuddy pack 提供只读发现、Run/Finding 持久化、show 与计划预览。
`inspect all` 指全部加载的 pack，不是全机五域扫描。
`explore`、`lab`、MCP、正式 promotion 不是现有 CLI 命令。

当前状态、限制和发布边界在 [AGENT_RUNTIME_STATUS](../../docs/AGENT_RUNTIME_STATUS.md)、
[CAPABILITIES](../../docs/CAPABILITIES.md)、[CHANGELOG](../../CHANGELOG.md) 维护。
P0a/P0b 仅保留为历史切片标签，不作为本规格的产品完成度或排期。

本地使用相关 `test_agent_*.py` 与 WorkBuddy/JSON 回归；验证矩阵见 AR-06。
文档修订本身不证明运行时已实现，旧发行包的缺陷不因改规格消失。

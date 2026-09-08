# AR-09 · 当前实现与发布状态的读取入口

> 文档 ID：AR-09 · 修订：2 · 更新：2026-09-08 · 类型：状态导航
> 来源：SRC-CODE、SRC-CONTRACT。当前事实不是对有效行为契约的覆盖授权。

## 1. 四层事实分别读取

| 要确认的事 | 入口 | 不能替代 |
|---|---|---|
| 产品目标与行为要求 | [00](../00-architecture.md)、本目录 AR-00..06 | 不以缺陷降低要求 |
| 当前能力和限制 | [AGENT_RUNTIME_STATUS](../../docs/AGENT_RUNTIME_STATUS.md)、[CAPABILITIES](../../docs/CAPABILITIES.md)、[AR-08](ar-08-strategy-pack-catalog.md) | 不以规划当实现 |
| 尚未发布的修复 | [CHANGELOG](../../CHANGELOG.md) Unreleased 与当前源码/diff | 不声称旧安装包已修复 |
| 实际发行与验证 | GitHub Release/tag/附件、对应提交 CI、已安装版本实测 | 本地测试不是发布证据 |

## 2. 本次基线的关键边界

Codex 与 WorkBuddy 包提供发现、Store、show 和计划预览；生产动作审批未开启。
经典命令、TUI 与共享执行器保留，不能据此说整个产品只读。
CLI envelope 2、Run bundle 2、pack 1 是不同版本，不同步改号。

已发布 0.24.0a1 的嵌套计划脱敏遗漏与 Python 3.11 定向测试零匹配问题，
在 0.24.0a2 修复中处理；正式交付状态必须按相应提交/产物核对，旧附件不更新。
旧文档中的“未发布、无兼容负担”前提不再成立。

## 3. 维护方式

不在本页累计测试数量、复制所有状态表或写另一个 P0 排期。
代码与契约不符时记录具体差异和复现；不能用本页一条“当前实现如此”覆盖整个规格。
补充功能时同步相关契约、用户文档和测试；新方向需按 [AR-07](ar-07-implementation-roadmap.md) 取得范围决定。

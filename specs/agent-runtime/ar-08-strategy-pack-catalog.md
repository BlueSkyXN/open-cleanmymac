# AR-08 · 实际策略包与接线边界

> 文档 ID：AR-08 · 修订：2 · 更新：2026-09-08 · 状态：baseline（当前清单）
> 来源：SRC-CODE。本篇不是所有领域最终必须迁入 pack 的蓝图。

## 1. 组织与维护

按被识别目标组织，来源保留 provenance。当前生产审批集合为空，
下列包均 active/report_only，所有命中不具备生产清理动作。
清单变化应同时核对 pack JSON、registry、inspect 分派和相关测试，不能只编辑此表。

## 2. 随包策略

| pack / strategy ID | 实际 detector 与子类型 | 作用与边界 |
|---|---|---|
| codex.marketplace.old-staging | codex_transient / codex_marketplace_staging_targets | 逐目标发现；require_structure_match 动作门未实现 |
| codex.marketplace.staging-summary | codex_transient / codex_marketplace_staging | 聚合摘要；父根不可执行 |
| codex.git.temp-skeleton | codex_transient / codex_git_skeleton | 临时 Git 骨架诊断 |
| codex.crashpad.orphan-sidecar | crashpad / crashpad_pairing | 孤立 sidecar，保留配对 dump |
| codex.logs.sqlite-freelist | sqlite_freelist | logs_2.sqlite 内部空闲页诊断 |
| codex.logs.retention | retention / include_partitions | macOS 日志与日期分区保留期 |
| codex.runtime.cache-retention | retention | runtime cache 保留期 |
| workbuddy.storage.observed-structures | workbuddy | expired/Worker/Electron 已观察结构 |

来源文件：[codex.json](../../implementation/openclean/packs/codex.json)、
[workbuddy.json](../../implementation/openclean/packs/workbuddy.json)。
Codex 表中 provenance 包含历史引用字符串，不代表全部 Observation 实验已完成；
WorkBuddy 来源摘要见 [EXPERIENCE](../../docs/EXPERIENCE.md)。

当前没有 qoder、docker、browsers 等独立 pack，不表示相关经典能力不存在。
也没有旧示例中的 codex.updater.staging 或 codex.process.open-unlinked 策略；
这些主题的经典诊断与 pack 覆盖必须分开说明。

## 3. Detector：允许声明不等于已接线

[registry.py](../../implementation/openclean/strategies/registry.py) 的允许名字与
[inspect_service.py](../../implementation/openclean/runtime/inspect_service.py) 的分派分别核对：

| 名称 | 当前 Agent 分派 |
|---|---|
| codex_transient | staging 聚合、staging 逐目标、Git 骨架；未知 subkind 返回 unavailable |
| crashpad | 精确单根 sidecar 配对 scanner |
| sqlite_freelist | 当前 roots 的 SQLite 只读规则 |
| retention | 当前 roots 的 retention，可添加 Codex 日期分区 |
| workbuddy | 仅当前 HOME/.workbuddy 精确根 |
| filesystem_tree、updater、open_unlinked、browser_cache、docker、startup_items | registry 允许声明，但当前 inspect 未接线；返回 scanner_unavailable |

REQ-AR-CATALOG-001：不把白名单名当可用能力；未知/未接线分派不能返回完整空结果掩盖缺失。
VAL-AR-CATALOG-001：[test_agent_review_scan.py](../../implementation/tests/test_agent_review_scan.py)、
[test_agent_review_cli.py](../../implementation/tests/test_agent_review_cli.py) 检查不可用分派；
[test_workbuddy.py](../../implementation/tests/test_workbuddy.py) 检查已接线边界。

## 4. Action：三个层次

| 名称 | 包声明 | 当前 Agent 执行器 | 经典功能 |
|---|---|---|---|
| report_only | 允许，supported 必须 false | 只读拒绝 | 对应诊断同样只读 |
| move_to_trash | 允许 | 仅通过全部审批/授权/实时条件的精确 filesystem；生产尚无获批包 | 普通清理已使用 |
| empty_trash | 允许声明 | 当前不支持 | clean trash 独立受限支持 |
| docker_prune | 允许声明 | 当前不支持 | 固定 prune 实现，真实 daemon UAT 未完成 |
| specialized | 允许声明 | 当前不支持 | 不由这个名字推断存在通用专用执行器 |

REQ-AR-CATALOG-002：schema/白名单/生产审批三个条件不得混淆；
不得将经典 executor 存在等同某个 Agent action 已启用。
VAL-AR-CATALOG-002：[test_agent_strategy_registry.py](../../implementation/tests/test_agent_strategy_registry.py)、
[test_agent_planner.py](../../implementation/tests/test_agent_planner.py)、
[test_agent_review_execution.py](../../implementation/tests/test_agent_review_execution.py)。

## 5. 扩展原则

有具体用户需求时，优先复用已存在的 scanner/diagnostic；
只有跨命令引用确有价值才新增 pack。复杂规则不塞入 JSON，不生成任意代码。
新增包的正反样本、保护、投影与 CLI 验收复用 [AR-06](ar-06-codex-p0-acceptance.md)。
全产品能力状态统一见 [CAPABILITIES](../../docs/CAPABILITIES.md)，不按 pack 数量算完成率。

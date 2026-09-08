# AR-06 · Agent 接口与既有工具切片验收

> 文档 ID：AR-06 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-GOAL、SRC-CODE、SRC-CONTRACT。旧文件名保留，正文不再限定“先完成 Codex P0 才能做产品”。

## 1. 验收目标与运行方式

证明 Agent 能按用户目标取得真实证据、精确预览、对支持动作在授权内执行，并准确报告限制。
包括 Codex/WorkBuddy 附加接口，也包括经典五域使用；不以首个 trusted 或 pack 数量决定产品是否可用。

下表是需求对应的验收入口，不是本轮执行记录。宏观发布检查使用 exact-head CI，
本地只运行受影响测试。写入均使用 TemporaryDirectory，外部 probes 用合成数据/替身；
实际 CLI 子进程与已安装 wheel 的验证单独报告。

## 2. 发现、对象与存储

| 验收 | 需求关联与必须观察到的结果 | 现有测试入口 |
|---|---|---|
| VAL-AR-AC-001 | AR-BOUND-001、AR-CLI-001/002：inspect codex 不串包；workbuddy 为已观察结构；all 是已加载包 | test_agent_inspect.py、test_workbuddy.py |
| VAL-AR-AC-002 | AR-MODEL-001/002/003、AR-STORE-001/002：身份/类型/schema/期限/归属一致；错误不能自修为成功 | test_agent_models.py、test_agent_run_store.py、test_agent_review_store.py、test_agent_identifiers.py |
| VAL-AR-AC-003 | AR-CLI-003：HOME、默认规则、Store 的隔离与 ignore 生效；开发覆盖不解锁动作 | test_agent_review_cli.py、test_agent_p0_correction.py |
| VAL-AR-AC-004 | AR-BOUND-002、AR-PACK-001/002：非法包拒绝；未接线 scanner 返回不完整；外部 trusted 降级 | test_agent_strategy_registry.py、test_agent_review_scan.py |

上述测试位于 [implementation/tests](../../implementation/tests/)；前缀省略的需求 ID 均为 REQ-。

## 3. 预览、执行与输出

| 验收 | 必须观察到的结果 | 需求关联 / 测试入口 |
|---|---|---|
| VAL-AR-AC-005 | active/report_only 正常返回阻断预览，exit 0 不被解释为可执行；--yes 拒绝生产包 | REQ-AR-CLI-005 / test_agent_planner.py、test_agent_clean_exec.py |
| VAL-AR-AC-006 | Run/pack hash/version/目标/证据变化拒绝旧计划；调用执行 API 也不能绕过 | REQ-AR-EXEC-001/002、REQ-AR-PACK-003 / test_agent_review_execution.py |
| VAL-AR-AC-007 | 混合批次不启动；已开始后的部分失败如实记录，未选项保留 | REQ-AR-EXEC-003 / test_agent_review_execution.py、test_agent_clean_exec.py |
| VAL-AR-AC-008 | 缺授权或风险确认不写；合成 approval 与全部条件通过才移入临时 Trash | REQ-AR-BOUND-003、REQ-FLOW-003/004 / test_agent_contract.py、test_agent_clean_exec.py |
| VAL-AR-AC-009 | 嵌套 plan/resolved_targets/tuple 中路径及 ID 脱敏；原始输出与候选不变 | REQ-AR-CLI-007、REQ-AR-MODEL-005 / test_agent_review_cli.py、test_json_redaction.py |
| VAL-AR-AC-010 | Agent/经典选择互斥、JSON 无装饰/隐式等待、错误退出码保持 | REQ-AR-CLI-004/006 / test_agent_contract.py、test_cli_contract.py |
| VAL-AR-AC-011 | 经典精确 purge 可在临时 HOME 执行并再次扫描；不依赖 Run Store | REQ-AR-BOUND-001、REQ-FLOW-001 / check_installed_wheel.py、test_project_purge.py |

“调用 API 测试”“CLI main 测试”“真实 CLI 子进程”“安装包测试”证据强度不同；
[安装包 smoke](../../implementation/scripts/check_installed_wheel.py) 必须针对该次实际构建运行，
不能把脚本新增断言当成旧 wheel 已通过。

## 4. 聚合、结构与动作边界

Codex staging 既有逐目标发现也有聚合摘要，两者不能重复求和为净收益。
staging/.tmp 父根不允许泛化成删除目标；Git 骨架排除普通仓库，Crashpad 保留配对 dump。
WorkBuddy 保留 runtime/插件/会话与历史，Worker 年龄有跳过即未知。

REQ-AR-AC-001：测量不完整、聚合子集、只读 evidence 或结构门未实现时，不开放动作。
VAL-AR-AC-012：[test_storage_diagnostics.py](../../implementation/tests/test_storage_diagnostics.py)、
[test_workbuddy.py](../../implementation/tests/test_workbuddy.py)、
[test_agent_review_scan.py](../../implementation/tests/test_agent_review_scan.py)
覆盖近似名称、当前/近期内容、云占位、保护与预算反例。

## 5. 不能据此宣称

- 合成 trusted 执行通过，不等于任何生产策略已获批。
- inspect all 成功，不等于五域扫描或 CleanMyMac CLI 全覆盖。
- 预览/CI 成功，不等于真实 Docker、File Provider、helper 或用户数据 UAT 完成。
- 正负夹具验证，不等于已确认参考软件的私有算法。
- 本地 bug 修复，不等于已发布附件更新。

后续功能不受“先解锁 Codex”排序约束；进入条件见 [AR-07](ar-07-implementation-roadmap.md)。

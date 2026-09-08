# AR-01 · 已有对象与字段契约

> 文档 ID：AR-01 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-CODE、SRC-CONTRACT；不是 Item 迁移计划。
> 完整字段由 [core/models.py](../../implementation/openclean/core/models.py) 与严格解码器定义。

## 1. 对象关系

| 对象 | 作用 | 不意味着 |
|---|---|---|
| Item / ScanResult | 经典扫描、诊断、选择和共享执行器的数据 | 即将退役或必须换模型 |
| Strategy / StrategyPack | Agent 接口的定位、detector、条件、说明与动作声明 | 声明可信、代码已接线或具备执行权限 |
| Run | 一次 inspect 的环境、期限、策略与 Finding 清单 | 保存用户授权 |
| Finding | 一次策略命中的目标、计量、判断和证据 | 凭 ID 就可删除 |
| CleanupPlan / CleanupPlanItem | 指定选择的动作解析与阻断原因 | 可以脱离 Run/registry 直接执行 |
| CleanupOutcomeRecord | 逐项执行结果 | 保证所有项成功或磁盘已净释放 |
| Observation | 经验研究中的记录概念 | 已有运行时类、JSON Schema 或 lab 命令 |

Observation 的维护方式见 [AR-05](ar-05-research-governance.md)。
当前不为它补造 schema 或运行时对象。

## 2. 通用类型与版本

REQ-AR-MODEL-001：输入必须按实际 dataclass/严格解码器验证字段、类型、枚举和有限数值；
未知字段、重复 JSON 键、字符串布尔值或非法 schema 不能默默接纳。
VAL-AR-MODEL-001：模型、序列化、Run Store 和 pack 反例测试明确拒绝。

| 项 | 当前定义 |
|---|---|
| 策略状态 | draft / active / trusted / deprecated |
| classification | cleanup_candidate / report_only / protected |
| certainty | low / medium / high |
| action_risk | safe / confirm / critical；不是 low/medium/high |
| target.kind | filesystem / filesystem_subset / docker；支持该类型不等于已有动作 |
| ID | run:、finding: 前缀加不透明 token；不把路径编码进 ID |
| 时间 | created_at/expires_at 是有限数值时间戳，不是旧示意中的 ISO 字符串 |
| CLI envelope | schema_version 2 |
| Run bundle | schema_version 2；独立于 CLI envelope |
| StrategyPack | schema_version 1 |

[schemas](../../implementation/openclean/schemas/) 中现有文件也应与模型保持一致。
不承诺已有 Observation/CleanupOutcome 独立 schema；新增格式必须独立决定兼容性。

## 3. Strategy / StrategyPack

StrategyPack 字段为 schema_version、name、strategies。
Strategy 包括 id、version、pack、status、provenance、locator、detector、conditions、guards、
assessment、recommendation、action；字段意义和状态矩阵见 [AR-02](ar-02-strategy-and-lifecycle.md)。

id 要有所属 pack 前缀，version 为正整数。同包重复策略 ID、重复包名拒绝。
provenance 的 kind 是来源分类，不验证原始观察是否真实存在。

## 4. Run

| 字段组 | 内容 |
|---|---|
| 身份/期限 | run_id、created_at、expires_at、requested_target |
| 环境 | openclean_version、macos_version、home |
| 绑定 | strategy_pack_hashes、strategy_versions、protect_config_hash |
| 完整性 | complete、issues（code/message/blocking）、finding_ids |

REQ-AR-MODEL-002：Run manifest 与实际 Findings 的归属、清单、策略版本一致；
complete 与 blocking issue 不矛盾；期限为正且不超过 24h。
VAL-AR-MODEL-002：删改清单/归属/版本/期限的 bundle 被拒绝，不补造旧格式缺失字段。

## 5. Finding

| 字段组 | 内容与含义 |
|---|---|
| 绑定 | finding_id、run_id、strategy_id、strategy_version |
| target | kind、display_path、identifier、identity（device/inode/owner） |
| measurement | size、allocated_bytes、logical_bytes、age_days、latest_mtime |
| assessment | classification、certainty、action_risk、actionable、block_reasons、requires_privilege、is_cloud_file、requires_explicit_selection |
| evidence | kind、payload；保存诊断与执行资格所需的现有 Item 信息 |
| recommendation | summary、do_not_do |

REQ-AR-MODEL-003：只读 evidence 不得 actionable；filesystem_subset 与允许的子集诊断对应，
锚点不得作为父目录删除目标。高 certainty 不是低风险或可执行保证。
VAL-AR-MODEL-003：模型反例和投影 round-trip 保留诊断限制、精确目标和保护元数据。

## 6. Item 与 Finding 投影

投影是有效接口边界，不是等待清除的过渡负担：
path/resource_kind/identifier/identity 对应 target，计量对应 measurement，
风险/阻断对应 assessment，诊断与其它必要字段进入 evidence.payload，说明进入 recommendation。
category/domain、excluded_paths、cross_device_paths、cloud_file_count 等不得在转换中遗失。

REQ-AR-MODEL-004：从持久化 payload 还原 Item 不能直接获得执行路由权限；
真正执行使用实时重新发现的 Item，见 AR-04。
VAL-AR-MODEL-004：伪造旧 evidence/路由/计量不能通过执行边界。

## 7. CleanupPlan

CleanupPlan 有 mode、run_id、executed、plan_items。
每个 plan item 的实际字段是 finding_id、strategy_id、strategy_version、action_name、
action_supported、can_execute、resolved_targets、block_reasons；不是旧示例的嵌套 action。
resolved_targets 包含精确 display_path 与 identity。内部可用 tuple，JSON 输出仍为数组。

REQ-AR-MODEL-005：preview 的 executed 固定 false；active 也可以生成被阻断的预览计划；
“生成计划”与“可执行”分开。计划不持久化为可重放授权文件。
VAL-AR-MODEL-005：预览/嵌套序列/只读拒绝的 JSON 与模型保持一致。

## 8. 回执与验收入口

逐项记录 finding_id、status、bytes_affected、destination、message。
聚合 outcome 使用实际 planner/CLI 结构；不根据本页编造另一份 envelope。
moved/deleted 的含义见 [05](../05-algorithms.md)。

[test_agent_models.py](../../implementation/tests/test_agent_models.py)、
[test_agent_projection.py](../../implementation/tests/test_agent_projection.py)、
[test_agent_review_store.py](../../implementation/tests/test_agent_review_store.py)、
[test_agent_planner.py](../../implementation/tests/test_agent_planner.py)、
[test_agent_review_execution.py](../../implementation/tests/test_agent_review_execution.py)、
[test_agent_review_cli.py](../../implementation/tests/test_agent_review_cli.py)。

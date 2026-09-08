# AR-03 · 当前命令、JSON 与脱敏

> 文档 ID：AR-03 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-CODE、SRC-CONTRACT；[cli.py](../../implementation/openclean/cli.py) 是当前参数和 payload 实现锚点。

## 1. 可调用命令

| 命令 | 输入与结果 | 副作用/边界 |
|---|---|---|
| `inspect TARGET --json` | 目标 pack 的 Run 与 Finding 摘要 | 只读候选，写本机 Run Store；all 仅为加载的 pack |
| `show --run RUN_ID --finding FINDING_ID --json` | 单个 Finding 的目标、计量、判断、evidence 和 allowed_actions | 读取 Store；过期清理可能删除 bundle，不修改候选 |
| `clean --run RUN_ID --finding FINDING_ID --json` | 解析指定结果并返回计划 | 无 --yes 不修改候选；可预览不可执行项 |
| 上述 clean 加 `--yes` | 请求执行；仍需相应风险 flag 和完整执行条件 | 当前生产包拒绝，不能当成自动开启动作 |
| `strategy list --json` | 已加载包/策略信息 | 查询 |
| `strategy show STRATEGY_ID --json` | 指定策略声明 | 查询；声明不等于动作获批 |
| `strategy verify --json` | 包结构与白名单检查 | 不是动作测试、实验验证或正式 promotion |

当前随包目标为 Codex/WorkBuddy，详见 AR-08。不将未来包名列为现有支持目标。
`explore`、`lab`、MCP 不在当前命令树；不提供可误执行的占位示例。
`config` 沿用现有偏好/规则功能，没有因 Agent 扩展新增 pack/Store 持久配置项。

REQ-AR-CLI-001：使用者先从现有 help/strategy 查询目标与参数；缺少 pack、
未知命令/策略或非法参数返回真实错误，不装作完整空结果。
VAL-AR-CLI-001：有效调用、缺包/未知策略/参数冲突有结构化结果和正确退出码。

## 2. inspect 的字段语义

外层有 schema_version、command、status、developer_mode、home、run_store、run_id、
requested_target、created_at、expires_at、openclean_version、macos_version、
strategy_pack_hashes、protect_config_hash、complete、totals、findings、issues、redaction。

totals 当前是 findings/actionable/report_only 的**计数**，不是旧规格中的 potential_bytes 总量。
Finding 摘要有 finding_id/run_id、strategy_id/version、actionable/classification/action_risk、
block_reasons、target_kind、display_path、identifier、size、evidence_kind、summary、do_not_do。
完整 evidence/measurement/assessment 通过 show 获取，不能在摘要中假设有全部字段。

REQ-AR-CLI-002：字段单位和类型以 AR-01/实现为准；单项 size 不保证可回收，
聚合/单体 Finding 可重叠，不能简单求和当唯一磁盘收益。
VAL-AR-CLI-002：摘要与 show 对应同一 Finding，诊断不可回收，不完整 inspect 说明 issues。

## 3. HOME、Store 与开发入口

`inspect --home PATH` 用于隔离：locator 展开、默认规则、日志分区和默认 Store 均以该 HOME 为准。
自定义 HOME 的 Store 为 PATH/.local/state/openclean/runs，后续 show/clean 预览应显式传 `--run-store`。
默认环境使用绝对 XDG_STATE_HOME，否则回退 HOME，详见 AR-04。

REQ-AR-CLI-003：`--packs-dir`、`--run-store`、自定义 HOME 是开发边界；
生产执行只接受当前 HOME、默认 Store 与内置获批包，不把开发路径作为越权通道。
VAL-AR-CLI-003：隔离 HOME 不读取真实 HOME 规则；非默认 Store/pack 只可预览或被拒绝执行。

## 4. show、选择与 clean

REQ-AR-CLI-004：finding 必须属于指定 run；`--finding` 可重复。
Agent Run 选择与经典 category/path 选择互斥；不将脱敏 ID 当真实选择。
VAL-AR-CLI-004：错误归属、混用选择、脱敏占位符和缺失 ID 不能动作。

计划外层有 command/status/mode/run_id/executed/plan；执行阶段另有 outcome，
失败时有 error。计划数组是 `plan.plan_items[]`，字段见 AR-01 §7，不是 plan.items。
active 策略能生成阻断预览，不等于执行支持；预览正常完成可以 exit 0。
请求 --yes 时仍需 --include-confirm/--include-critical 等风险确认以及 AR-04 条件。

REQ-AR-CLI-005：预览 executed=false；执行前整批拒绝时 executed=false，
outcome.complete=false，逐项 blocked/not_run；已尝试动作但失败必须保留已尝试事实。
VAL-AR-CLI-005：混合批次不以空成功回执掩盖拒绝；部分操作失败不伪装全成功/全回滚。

## 5. ID 与跨命令使用

原始 run_id/finding_id 在有效 Store 生命周期内可跨命令引用，不编码路径。
ID 不是授权，也不保证目标没有变化。“不可回放”指脱敏占位符，不是禁止正常跨命令工作流。
Run 过期、版本不兼容、策略变化时重新获取证据，不能伪造同一个 Finding。

## 6. JSON 与错误通道

REQ-AR-CLI-006：JSON stdout 是单一 JSON 文档，菜单/装饰不混入；不隐藏等待键盘。
错误应携带既有 error.code/message，不能把 stderr 文本当结构化成功。
VAL-AR-CLI-006：显式 JSON、非 TTY、解析前错误与运行时拒绝均可被无人值守调用解析。

| exit | 含义与示例 |
|---:|---|
| 0 | 命令按契约完成；包括全部不可执行的正常预览 |
| 1 | 不完整、能力不可用、缺少/过期 Run、缺包、执行受阻/失败 |
| 2 | 参数、规则、选择归属、未知 strategy 等错误 |
| 130 | 用户中断 |

不要把“未知 TARGET 一律 exit 2”当契约；语法有效但未安装的 pack 是 pack_not_found/exit 1。

## 7. 脱敏契约

REQ-AR-CLI-007：`--json --redact-paths` 在输出边界把路径换成单文档 opaque ref，
同时替换 run_id/finding_id（含序列和文本中的引用），输出 selection_replayable=false。
递归覆盖 dict/list/tuple，包括 plan_items/resolved_targets/路径序列；
不改变原始对象、Store 或未脱敏精确选择。

VAL-AR-CLI-007：真实 clean CLI 的嵌套路径与 ID 不再等于原值，
未脱敏输出仍精确，候选未被预览修改；脱敏输入不能用于 show/clean。
已发布 0.24.0a1 曾遗漏嵌套元组；源码修复与发布状态见 [CHANGELOG](../../CHANGELOG.md)。
不能因为 metadata 写 enabled=true 就认定旧安装包全部脱敏，也不能假定本机 Store 已被擦除。

## 8. 验收锚点

[test_agent_inspect.py](../../implementation/tests/test_agent_inspect.py)、
[test_agent_contract.py](../../implementation/tests/test_agent_contract.py)、
[test_agent_review_cli.py](../../implementation/tests/test_agent_review_cli.py)、
[test_agent_p0_correction.py](../../implementation/tests/test_agent_p0_correction.py)、
[test_json_redaction.py](../../implementation/tests/test_json_redaction.py)。
安装包链路见 [check_installed_wheel.py](../../implementation/scripts/check_installed_wheel.py)；
源码/已安装 wheel/CI 三层结果不能互相替代。

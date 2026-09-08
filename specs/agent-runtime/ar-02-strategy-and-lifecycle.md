# AR-02 · 策略声明、运行状态与审批

> 文档 ID：AR-02 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-CODE、SRC-CONTRACT、SRC-EXPERIENCE；实现锚点为 [registry.py](../../implementation/openclean/strategies/registry.py)。

## 1. 包不是独立清理器

Pack 按目标组织，不按 CleanMyMac/个人来源各运行一套。来源在每条策略内保留。
一条包策略可以投影多条 Finding，也可能命中零项；策略数量不代表对象覆盖或完成度。
已有经典 scanner 可以继续独立维护，不强制写成 pack。

## 2. 结构与加载

字段定义见 [AR-01](ar-01-object-model.md)。
locator 只接受绝对或 HOME-relative 根，拒绝 NUL/.. 越界形态；detector/action 只引用白名单。
支持字段/名字声明不等于支持对应 detector 参数组合或动作；真实接线见 [AR-08](ar-08-strategy-pack-catalog.md)。

REQ-AR-PACK-001：严格解码，拒绝未知字段、非法类型/枚举、重复 ID/包名；
JSON 不包含任意代码、表达式或通用删除动作。
VAL-AR-PACK-001：结构/路径/白名单反例失败，不能执行动态代码。

### 2.1 声明与实现分别检查

`strategy verify` 验证结构与白名单，不证明来源真实、动作获批或所有名称均已接线。
未接线 detector 在 inspect 时产生 scanner_unavailable/blocking issue，而非“完整且没发现”。

## 3. 状态矩阵

| 状态 | 普通 inspect | 计划预览 | 动作权限 |
|---|---|---|---|
| draft | 不参与 | 不通过普通 inspect 新建 Finding | 无；当前无 lab 运行入口 |
| active | 参与 | 可以返回 can_execute=false 及原因 | 无 |
| trusted | 经 registry 处理后仍有效才参与该状态 | 可以解析计划 | 还需内置审批 hash、支持动作、授权和实时检查 |
| deprecated | 不参与 | 不用旧 Run 绕过当前状态 | 无 |

REQ-AR-PACK-002：未审批或外部包的 trusted 声明降级为 active、action.supported=false；
外部 `--packs-dir` 不提供生产执行权限。report_only 不得 supported=true。
VAL-AR-PACK-002：直接改 JSON 状态、构造自称 trusted 或开发路径不能获得动作资格。

`APPROVED_PACK_HASHES` 当前为空。不能以合成测试中的批准集合或“用户让我清理”代替策略动作审批。
用户授权一次清理与维护者批准一种生产动作是两个独立条件。

## 4. 版本、hash 与冲突

REQ-AR-PACK-003：Run 绑定实际加载 pack 的规范化 hash 和策略版本，执行前匹配当前 registry；
改变包/策略后不能沿用旧证据直接执行，需重新 inspect。
VAL-AR-PACK-003：hash/version 变化与过期 Run 拒绝；不根据相同显示名称推断是同一策略。

维护同一策略时保留稳定 ID，语义变化应增加 version；这是一条维护规则，
不是声称 registry 有远程版本历史或自动防回滚发布服务。
当前不运行按来源合并的 merger，也不靠更高 certainty 覆盖 protect/ignore。

## 5. 来源声明与晋级证据

provenance.kind 支持 cleanmymac_public、cleanmymac_observation、personal_experience、ai_research；
observation_id 是引用字符串，不是已验证的实验凭证。非 draft 要有 provenance，但解析成功不能证明证据完备。

REQ-AR-PACK-004：新增/启用动作前核对具体来源、正反结构、运行状态及动作前后证据，
经明确审批记录固定 pack hash；不能伪造 Observation 或仅将 action 改为 supported。
VAL-AR-PACK-004：生产变更审阅能追溯证据与审批；测试 approval 不进入生产集合。

经典经验维护不强制套晋级流程。只读识别升级为动作才补相应验证，
不是将所有历史 scanner 重新研究一次。
正式 promote/demote 命令未实现，也未因本文自动获批；流程见 [AR-05](ar-05-research-governance.md)。

## 6. 与全局规则的关系及测试

KB protect/ignore 是独立最外层保护闸，Strategy 不能改写或覆盖；
即使 trusted，运行状态、句柄、身份、云占位或当前规则不允许，仍不可执行。
计划检查与实时执行详见 [AR-04](ar-04-run-store-and-execution.md)。

[test_agent_strategy_registry.py](../../implementation/tests/test_agent_strategy_registry.py)、
[test_agent_review_cli.py](../../implementation/tests/test_agent_review_cli.py)、
[test_agent_planner.py](../../implementation/tests/test_agent_planner.py)、
[test_agent_p0_correction.py](../../implementation/tests/test_agent_p0_correction.py)、
[test_scan_rules_integration.py](../../implementation/tests/test_scan_rules_integration.py)。

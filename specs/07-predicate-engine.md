# 07 · 保护判定与执行资格

> 文档 ID：OC-07 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-CODE、SRC-CONTRACT；补充 [02](02-scan-points.md)、[05](05-algorithms.md)、[06](06-system-flow.md)。

## 1. 判定不是只看目录名

匹配 locator/文件名只表示值得检查。是否展示、默认选中、当前可执行和用户授权是不同结论。
“不显示某个保护路径”和“显示一个被运行状态阻断的候选”也不是同一行为；
不得为了让报告好看而隐藏有用的阻断证据。

已有 Predicate/AllPredicate/AnyPredicate/ProtectionGate 继续复用，
不要求复制参考软件类层次、URL 对象图或引入新谓词 DSL。

## 2. 判定要求

| 需求 | 触发与行为 | 验收 |
|---|---|---|
| REQ-GUARD-001 KB 优先 | protect/ignore 先于普通谓词；命中则短路，不继续读取受保护细节 | VAL-GUARD-001：组合谓词不能重新允许被 KB 拒绝的对象 |
| REQ-GUARD-002 应用状态 | 已知归属同时覆盖专用扫描点和通用缓存根；运行中/需要但未知的进程或句柄状态阻断动作 | VAL-GUARD-002：候选可见但不可执行，相似 sibling 不误归属 |
| REQ-GUARD-003 文件身份 | 词法规范化、祖先 no-follow 和 device/inode/owner 等身份复核 | VAL-GUARD-003：扫描后替换路径、symlink、owner/device 变化不能沿用旧目标 |
| REQ-GUARD-004 云与跨界 | dataless/疑似占位在枚举和最终动作前检查；跨文件系统跳过不授权删除父项 | VAL-GUARD-004：不触发占位枚举，不处理跨界目标 |
| REQ-GUARD-005 领域重判 | updater、启动项、Darwin 根、Docker binding 在执行前复核各自业务条件 | VAL-GUARD-005：应用/版本/可执行文件/daemon 变化后拒绝旧结论 |
| REQ-GUARD-006 只读和特权 | diagnostic_kind、filesystem_subset、语言资源、特权项与不支持资源保留模型/执行器限制 | VAL-GUARD-006：`--yes`、tier、Strategy 声明均不能强制解锁 |

规则的含义是约束结果，不承诺每个 scanner 在同一行代码以相同顺序调用全部 probes。
先排除不能访问的路径，再做适用的结构、计量、状态判断；执行时复核必要条件。

## 3. 模型与投影不能丢失保护信息

Item 携带 identity、safety、actionable、requires_privilege、is_cloud_file、
requires_explicit_selection、excluded_paths、cross_device_paths 与诊断字段。
Finding 投影必须保留这些判定所需证据；不能因字段放到 evidence.payload 就降低含义。

只读诊断不可 actionable，filesystem_subset 仅是受支持的子集诊断表示。
具体诊断枚举以 [models.py](../implementation/openclean/models.py) 为准；
Agent 映射见 [AR-01](agent-runtime/ar-01-object-model.md)。

## 4. 安全范围与残余风险

同卷 Trash 用现有目录 fd 与 Darwin no-follow/exclusive rename；
清空 Trash 保留根，仅处理最后审计快照。
这些机制降低路径竞态风险，不提供对同 UID 恶意进程的绝对隔离。

Docker 的 CLI realpath/context/host/endpoint/Engine ID 绑定不等于同一连接上的原子事务，
也不钉住同路径二进制内容。限制和真实环境验证在 TODO/SECURITY 中维护。
进程/文件状态可能在测量后变化，因此 preview/can_execute 不是延时有效授权。

Reachability/FileAccess 等曾出现的内部名称不产生新需求；有具体误判案例时再决定是否新增判定。

## 5. 实现与验收锚点

- [predicates.py](../implementation/openclean/predicates.py)、[test_predicates.py](../implementation/tests/test_predicates.py)、[test_scan_rules_integration.py](../implementation/tests/test_scan_rules_integration.py)：VAL-GUARD-001。
- [test_process_protection.py](../implementation/tests/test_process_protection.py)：VAL-GUARD-002。
- [test_cleanup.py](../implementation/tests/test_cleanup.py)、[test_macos_trash.py](../implementation/tests/test_macos_trash.py)、[test_analyze_cleanup.py](../implementation/tests/test_analyze_cleanup.py)：VAL-GUARD-003/004。
- [test_startup_items.py](../implementation/tests/test_startup_items.py)、[test_updater.py](../implementation/tests/test_updater.py)、[test_docker.py](../implementation/tests/test_docker.py)：VAL-GUARD-005。
- [test_agent_projection.py](../implementation/tests/test_agent_projection.py)、[test_agent_review_execution.py](../implementation/tests/test_agent_review_execution.py)：投影及 VAL-GUARD-006。

验收应覆盖允许和拒绝两侧，不通过扩大禁区、取消可用经典功能来代替正确判定。

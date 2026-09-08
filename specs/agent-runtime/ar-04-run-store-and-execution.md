# AR-04 · Run Store、计划与实时执行

> 文档 ID：AR-04 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-CODE、SRC-CONTRACT。生产包动作关闭；本篇约束已有执行 API，不代表启用生产策略。

## 1. 保存证据，而非授权

Store 服务跨命令 inspect→show→clean。保存 Run manifest 和完整 Findings，
不保存用户对未来任意状态的授权，不缓存可直接执行的 CleanupPlan。
经典命令不依赖 Store；Finding ID 不是任意路径删除器的替代性安全补丁。

## 2. 本机存储契约

| 维度 | 当前契约 |
|---|---|
| 默认路径 | 绝对 XDG_STATE_HOME/openclean/runs；否则 ~/.local/state/openclean/runs |
| 隔离 HOME | --home 下默认 PATH/.local/state/openclean/runs；后续用 --run-store 读取 |
| 权限 | 目录 0700、文件 0600，校验所有者与链接状态；Run 文件必须只有一个硬链接 |
| 文件 | 一个 bundle 原子包含 Run 与全部 Findings；schema 2 |
| 容量 | 最多 64 Run，每个 8 MiB，总计 64 MiB |
| 淘汰 | 最早写入优先；读取不刷新顺序，不是 LRU |
| TTL | 正值且不超过 24h；不等于目标安全有效期 |
| 原子性 | 临时写入、fsync、原子安装；不覆盖同 ID Run |
| 过期 | 读取时删除过期条目并报错；不自动重扫或续期 |
| 格式 | 拒绝错误 schema/未知字段/重复 JSON 键/非法类型/非有限值/归属不一致 |

REQ-AR-STORE-001：读写不接受错误所有者、过宽权限、链接、篡改或不完整 bundle；
解析时 Run ID、文件名、Finding 清单及身份/证据/版本一致。
VAL-AR-STORE-001：构造各类损坏 bundle 明确失败，不补字段或信任旧 payload。

`inspect` 会创建/淘汰 Store 数据，show/clean 读取也可能清除过期 bundle。
“候选不写入”不是“文件系统完全只读”。旧 schema 或不同 runtime version 没有执行降级通道；
要求重新 inspect，而不是在文档中宣布自动迁移。

## 3. 标识与当前条件

REQ-AR-STORE-002：run_id/finding_id 不直接编码路径，finding 与指定 run 绑定；
原始 ID 可在有效期内跨命令使用，脱敏占位符不可用。
VAL-AR-STORE-002：错归属、伪造/遍历 ID、过期和缺失返回既有错误。

保存的 hash、identity、计量或版本不是用户不可篡改的外部签名凭证；
同 UID 威胁限制见 SECURITY。执行仍必须重新获取当前证据。

## 4. 计划检查和实时检查是两层

REQ-AR-EXEC-001：resolve_plan 校验 Run 完整性/期限/版本、选择归属、当前策略
status/action/审批/hash/version、Finding 可动作性、目标类型、重复目标与确认门。
VAL-AR-EXEC-001：任一不满足产生明确 block_reasons；preview 固定 executed=false。

can_execute 仅反映计划阶段结果，不保证稍后身份和进程状态。
不得把计划阶段的布尔值写成已经完成实时复核。

REQ-AR-EXEC-002：execute_plan 必须重新传入并核对 Run/registry/选择/授权，
再生成并比较计划；重新在 Run HOME 探测目标，用新 Item 执行。
不信任旧 evidence 中的路由字段、旧安全判定或调用方修改过的 plan。
VAL-AR-EXEC-002：伪造目标、旧 domain、改变 identity/规则/年龄/进程/句柄时不执行。

本路径只支持精确 filesystem→move_to_trash，拒绝 filesystem_subset 聚合根。
empty_trash/docker_prune 等声明白名单不等于此 API 支持；经典能力独立保留。

## 5. 批次与实际动作

```text
解析 Store / 当前 registry
    → 生成选择计划
    → 请求执行且条件满足
    → 整批重新探测与预检
    → 逐目标实时检查、同卷 Trash 操作
    → 逐项 outcome 与批次 complete
```

REQ-AR-EXEC-003：执行前任一目标受阻则整批不启动；真正开始后不承诺多个 OS 操作事务回滚。
VAL-AR-EXEC-003：混合阻断为 blocked/not_run；中途失败保留已移动/partial 的真实回执。

## 6. 复用而非重写执行保护

复用 cleanup 的用户身份、祖先 no-follow、device/inode/owner、当前 protect/ignore、
云占位、挂载边界与领域重判。普通移动使用现有 Darwin 安全 rename 路径。
不把当前暂无生产 action 当作删除这些检查的理由，也不新增通用 executor。
具体共享约束见 [07](../07-predicate-engine.md)，计量含义见 [05](../05-algorithms.md)。

## 7. 授权范围

用户明确授权对象与动作后，Agent 可以在此范围调用可用命令，无需逐工具重复确认。
期限/策略/目标变化导致旧计划失效时重新发现并复核授权是否仍覆盖新事实；
不能自动把新对象/新风险纳入原授权。风险不确定则询问，不把“Agent 自主”理解成自行批准新动作。

当前内置审批为空、生产包只读。测试中的 synthetic approval 只验证临时执行机制，
不能作为真实 Observation 或生产授权。

## 8. 验收锚点

[run_store.py](../../implementation/openclean/runtime/run_store.py)、
[planner.py](../../implementation/openclean/actions/planner.py)；
[test_agent_run_store.py](../../implementation/tests/test_agent_run_store.py)、
[test_agent_review_store.py](../../implementation/tests/test_agent_review_store.py)、
[test_agent_identifiers.py](../../implementation/tests/test_agent_identifiers.py)、
[test_agent_planner.py](../../implementation/tests/test_agent_planner.py)、
[test_agent_review_execution.py](../../implementation/tests/test_agent_review_execution.py)、
[test_agent_clean_exec.py](../../implementation/tests/test_agent_clean_exec.py)。
真实用户清理和 native 环境验收不能由临时夹具通过来代替。

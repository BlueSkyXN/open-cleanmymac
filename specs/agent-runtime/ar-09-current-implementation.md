# AR-09：0.24.0a1 当前实施补充

这是本候选版的实施决策，优先于 AR-00..08 中尚未落地的远期描述。

完整行为与流程见 [docs/AGENT_RUNTIME_STATUS.md](../../docs/AGENT_RUNTIME_STATUS.md)。

| 决策 | 本版事实 |
|---|---|
| 命令面 | 经典命令/TUI 与 Agent 并存；不退役旧能力 |
| P0a | Codex 只读探测、展示、计划预览、持久化与 API 逻辑实现 |
| P0b | 生产动作关闭；结构匹配器、真实 Observation/promotion 待完成 |
| 策略审批 | 固定 pack hash 审批集合为空；外部目录和 JSON 自称 trusted 不产生动作能力 |
| 存储 | bundle v2；完整 Run manifest + Finding；64 Run / 8 MiB each / 64 MiB total / TTL ≤24h |
| 淘汰 | 最早写入优先，非 LRU；不覆盖同 ID Run |
| 版本迁移 | 旧 bundle 与不同 runtime version 不可直接执行，重新 inspect |
| 计划 | `plan.plan_items`；preview 永不执行；对象不是独立执行凭证 |
| 实时动作 | 重新探测 Item，不使用旧 payload 的路由字段直接调用执行器 |
| 批次 | 执行前任一被阻止则全批不启动；不承诺多个 OS 操作事务回滚 |
| 参数 | Agent/经典选择互斥；HOME/ignore 一致生效；开发 Store/pack 仅预览 |
| JSON | envelope schema 2，incomplete/blocked/执行失败均明确非零退出 |
| 验证 | Linux 逻辑测试不代替 macOS 原生；实际结果放私人交接资料，不填造通过记录 |

实施时继续遵守净室边界，不读取或复制 `analysis/`。本次测试只用 TemporaryDirectory。

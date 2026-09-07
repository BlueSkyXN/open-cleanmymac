# Agent Runtime 对象 schema（描述性）

这些 JSON Schema 描述 Agent Runtime v1 五个核心对象的**序列化形状**（与
`core/models.py` 的 dataclass + `runtime/run_store.py` 的 `asdict` 输出一致），供外部工具、
文档与审阅参考。

运行时**不依赖**这些文件做校验：为满足「零第三方依赖」硬约束，实际校验由
`core/models.py` 的 `__post_init__` 不变量与 `strategies/registry.py` 的白名单/结构检查手写完成，
它们才是权威。schema 与代码不一致时以代码为准。

- `strategy.json` — 策略平面基本单元（`packs/*.json` 里的每条策略）
- `run.json` — 一次 `inspect` 固化的运行记录
- `finding.json` — 策略在本机的实际命中
- `cleanup-plan.json` — `resolve_plan` 的静态 `can_execute` 结果

契约叙述见 [specs/agent-runtime/](../../../specs/agent-runtime/_index.md)。

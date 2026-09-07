# Agent Runtime 对象与存储格式

运行时零第三方依赖。`core/serialization.py` 严格检查字段和类型；
`runtime/bundle_validation.py` 检查 Run 清单、版本、Finding 语义与 Item 证据的对应关系。
JSON Schema 描述序列化形状，不代替跨对象检查或执行前重新探测。

| 文件 | 用途 |
|---|---|
| `strategy.json` | pack 中每条策略，省略可选字段时使用模型默认值 |
| `run.json` | Run v2：包含 `home`、`strategy_versions` 和完整 Finding 清单 |
| `finding.json` | 完整 Finding v1，含目标、计量、评估与证据 |
| `cleanup-plan.json` | CleanupPlan v1，列表字段仅为 `plan_items` |
| `run-bundle.json` | 本地持久化容器 v2，将 Run 与 Findings 一次性读写 |

CLI 顶层 envelope 为 `schema_version=2`；它与 Run bundle v2 是两个独立版本号。
旧 bundle 不补造缺失字段，需重新 `inspect`。Run 文件里的所有模型字段均必需；
时间戳须有限且 TTL 不超过 24h，这类跨字段限制由模型检查。
脱敏 JSON 使用不可回放占位符，不适用原始 Run/Finding ID schema。

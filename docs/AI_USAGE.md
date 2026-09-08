# Agent 调用契约：用户指挥，Agent 完成调用

`openclean` 是 macOS 清理 CLI。用户描述目标与授权范围，Agent 负责发现、解释、预览、
在已有明确授权内调用执行，并核对结果。用户不需要手工传递 JSON 或逐条敲命令。
**未获清理授权时只扫描与预览；actionable 不等于授权。**

## 工作方式

1. 先运行 `openclean --help` 或对应子命令的 `--help`，不要猜参数。
2. 使用不带执行授权的 JSON 命令扫描。
3. 同时检查容量、候选属性、退出码和 `issues`。
4. 精确选择本次发现，生成预览；没有覆盖该动作的授权时，向用户报告范围并等待决定。
5. 已有明确授权且候选、风险和范围一致时，由 Agent 完成执行和结果核对，不重复索要同一授权。
6. 新风险、范围变化、探测不完整或阻断条件出现时停止扩面，不用替代删除命令绕过。

```bash
ROOT="$HOME/Projects"

openclean scan --json
openclean scan --domain developer --domain ai --json
openclean scan --domain project --project-root "$ROOT" --json
openclean clean dev --no-interactive --json
openclean purge "$ROOT" --no-interactive --json
openclean analyze "$ROOT" --top 20 --no-interactive --json
```

`scan` 默认聚合全部五域，`--domain` 可重复。`--project-root` 必须配合
`--domain project`，并指向现有的非 symlink 目录。上面的 `clean`、`purge` 和 `analyze`
均为只读预览。

## 五个扫描域

| 域 | 典型内容 | AI 的处理方式 |
|---|---|---|
| `system` | `~/Library/Caches`、updater、日志、Xcode、失效启动项 | 报告候选和阻断原因，不绕过特权、应用或版本保护。 |
| `developer` | pip、uv、npm、Go、Cargo、Homebrew、Docker 报告 | 普通缓存只建议审阅；Docker 只报告。 |
| `ai` | Claude、Codex、WorkBuddy 已观察结构、Gemini、Chrome DevTools MCP、OpenCode、Cursor | 报告大小和保护状态；不把 Codex `.tmp` 或 WorkBuddy 工作目录整根当缓存。 |
| `project` | `node_modules`、`.venv`、`target`、DerivedData 等可重建产物 | 只分析明确的项目根，不扩大范围。 |
| `trash` | `~/.Trash` 与挂载卷 Trash | 只报告；清空是永久操作。 |

这些路径只是分类提示。实际候选、大小和可执行性始终以本次 JSON 输出为准；AI 不应自行
补充未被扫描器报告的删除路径。

## JSON 判读

| 内容 | 判读规则 |
|---|---|
| `potential_bytes` | 本次发现的物理占用。 |
| `reclaimable_bytes` | 清理域中当前 `actionable=true` 候选的占用；`analyze` 固定为 `0`，也不代表已经释放。 |
| `requires_privilege_bytes` | 需要尚未实现的特权 helper，只能报告。 |
| `unsupported_bytes` | 当前明确不支持执行的占用。 |
| `complete`、`issues` | `complete=true` 仍需检查全部 `issues`；有 blocking issue 时不得宣称扫描完整。 |
| `actionable`、`action_block_reason` | `actionable=false` 时只报告原因，不尝试替代命令。 |
| `safety` | `safe` 也只是候选；`confirm`、`critical` 必须单独提示风险。 |
| `requires_privilege`、`is_cloud_file` | 特权项和云占位项只能报告。 |
| `requires_explicit_selection`、`preselected` | 精确选择要求和默认预选都不等于用户授权。 |
| `cross_device_paths` | 非零表示递归时跳过了其它文件系统挂载点；其容量未计入，候选不可执行。 |
| `volumes`、`device_id` | 按卷区分系统盘和外置盘收益；`device_id` 只在当前启动中有意义。 |
| `updater_status` | 新版待安装、应用缺失或未知状态只能报告；同版/旧版仍要求 critical 精确选择。 |
| `diagnostic_kind=retention` | 读取文件数、句柄及 7/14/30 天物理容量；阈值不是删除授权。 |
| `diagnostic_kind=sqlite_freelist` | 读取内部空闲页/比例及 WAL；不得建议删除 DB 或在线 `VACUUM`。 |
| `diagnostic_kind=updater_temp` | Darwin temp 中的完整 app 只读可见；版本判断不构成删除授权。 |
| `diagnostic_kind=codex_transient` | 只汇总精确 marketplace staging 或 Git 空壳；不得扩大到 `.codex/.tmp` 父根。marketplace 同时检查 `total_count`、`measured_count`、`measurement_complete`；为 false 时容量只是有界部分结果，且顶层 `complete=false`。 |
| `diagnostic_kind=crashpad_pairing` | `total_count` 是孤立 sidecar，`paired_artifact_count`/`recent_artifact_count` 是受保护配对/近期 orphan；不得删除 `.dmp`。 |
| `diagnostic_kind=open_unlinked` | `potential_bytes=0`；只在 `logical_bytes`、`related_process_count` 和句柄字段报告上限，只能退出应用或重启释放。 |

运行中的已知应用缓存仍会出现在结果中，但 `actionable=false`。这条保护也适用于通用
`~/Library/Caches` 入口；AI 不应因为候选仍可见就建议绕过阻断原因。
updater 的 installed/staged 版本必须同时判读；不得把 `pending_update` 或
`installed_app_missing` 改写成“可安全删除”。
只读诊断项固定 `actionable=false`。retention 报告只能用于选择后续保留策略；SQLite
freelist 只有在应用完全退出、无 WAL/句柄、有备份和足够临时空间时才可能进入独立压缩任务。

默认 JSON 包含绝对路径。输出需要离开本机会话时使用 `--json --redact-paths`；脱敏后的
`path:0001` 只能用于报告，不能作为后续 selector。
注意：已发布 `0.24.0a1` 的 Agent `clean` 嵌套计划存在脱敏遗漏；修复尚在 Unreleased，
使用该旧安装包时不要将其输出直接对外分享，`redaction.enabled=true` 不能证明嵌套内容已脱敏。

退出码：`0` 表示命令按契约完成；`1` 表示结果不完整、失败或能力 unavailable；`2` 表示
参数、规则、路径或配置错误；`130` 表示用户中断。`optimize ram|purgeable` 当前返回
`status=unavailable`、`executed=false` 和 exit `1`，这是预期安全拒绝。

## 已授权的经典清理流程

经典清理已具备真实执行路径，不必等每个领域都有 Pack。以用户明确选定的普通文件系统候选为例：

```bash
openclean clean dev --json
openclean clean dev --select "EXACT_SCANNED_PATH" --json
# 仅当用户授权已经覆盖上面精确候选及处理方式时：
openclean clean dev --select "EXACT_SCANNED_PATH" --yes --json
```

`EXACT_SCANNED_PATH` 必须从本次原始 JSON 的候选路径取值，不是模型猜出的路径。
Agent 使用参数数组调用，不能把路径插入未转义 shell 命令。`confirm/critical` 目标仍需对应
`--include-confirm/--include-critical`，仅在用户授权涵盖该风险时使用；不能自动改成 `--all` 或 `--force`。

解析预览的 `categories[].items[]`、`preselected`、`actionable`、`issues` 和容量；显式
`--select` 从空集选择，不继承其他默认项。经典执行会重新扫描和执行前复核，不是一个持久化事务；
用户要求严格冻结前次对象时，不得声称普通路径 selector 已提供该保证，应停止并说明限制。

执行后读取 `cleanup.outcomes[]`、`complete`、移动与永久释放字节，逐项核对实际结果。
必要时再只读扫描相同范围。部分成功不能写成全部成功，移入 Trash 不能写成已释放磁盘空间。
Trash 清空、Docker prune、修改配置/ignore、知识库更新和特权操作均须有对应的独立明确授权。

## Agent Runtime（Finding 驱动，附加）

面向 AI agent 的附加命令面，与上面的经典只读命令并存。它把“发现”固化为可跨命令引用的
Run/Finding，让 agent 用稳定 id（而非易变的绝对路径）向用户复述候选：

```bash
openclean inspect codex --json                           # 只读探测，固化 Run/Finding
openclean inspect workbuddy --json                       # 个人经验结构：expired/Worker/Electron
openclean show --run RUN_ID --finding FINDING_ID --json   # 只读读取单个 Finding 完整证据
openclean strategy list --json                           # 只读查看已安装策略包
```

判读要点：

- `findings[].actionable=false` 或带 `block_reasons` 时只报告原因，不尝试替代路径；
  `totals.actionable` 是当前可执行数，不等于用户授权。
- `run_id`/`finding_id` 只是引用句柄，**不构成删除授权**；把它们复述给用户，由用户决定。
- `--redact-paths` 输出 `redaction.selection_replayable=false`，脱敏后的 id 不能回放执行。
- 聚合根 Finding（`target.kind=filesystem_subset`）永不作为动作目标，只用于报告。

计划预览使用 `clean --run RUN_ID --finding FINDING_ID`。当前 Codex 的 7 条策略及 WorkBuddy 包均只读，
即使加 `--yes` 也不执行。未来生产策略须先具备真实结构证据与明确审批；执行前还需重新探测。
`inspect` 只读候选，但会写本机 Run Store。契约见 [当前状态](AGENT_RUNTIME_STATUS.md)。

## 停止边界

AI 不得在用户授权之外添加 `--yes`（包括 Finding 清理），也不得扩大到清空 Trash、Docker prune、
修改 ignore/config、更新知识库、sudo 或绕过 `actionable=false`。已有明确授权允许 Agent 完成
范围内的步骤，不代表可以自动增加新的对象、风险等级或动作。失败时先报告实际原因，不降级为任意路径删除。

## 汇报模板

```markdown
## 本次只读扫描
- 命令与范围：
- 完成状态：exit code、complete、blocking issues
- 容量：potential / reclaimable / requires privilege / unsupported
- 建议审阅：路径或 identifier、大小、safety
- 仅报告项：不可执行原因、云占位、特权项和其他 issues
- 下一步：需要用户决定的精确目标与范围
```

不要把“发现可回收容量”等同于“已经释放空间”。

## 详细说明

- [项目 README](../README.md)：安装和总体安全边界。
- [能力地图](CAPABILITIES.md)：能力状态与外部前提。
- [隔离预览](PREVIEW.md)：TemporaryDirectory 场景和只读示例。
- [实现说明](../implementation/README.md)：完整 CLI、JSON 和选择契约。
- [自有经验](EXPERIENCE.md)：历史成果、WorkBuddy 个人观察与不可泛化条件。

文档与当前程序不一致时，以 `openclean <command> --help` 和实际 JSON 为准。

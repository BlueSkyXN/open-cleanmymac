# AI 使用指南（只读）

`openclean` 是面向 macOS 的垃圾文件扫描 CLI。本页供 AI agent 调用：先取得 JSON 事实，
再向用户报告候选与风险。**流程到建议为止，不自动清理。**

## 工作方式

1. 先运行 `openclean --help` 或对应子命令的 `--help`，不要猜参数。
2. 使用不带执行授权的 JSON 命令扫描。
3. 同时检查容量、候选属性、退出码和 `issues`。
4. 汇报精确结果后停止；任何写入都需要用户重新明确授权。

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
| `system` | `~/Library/Caches`、`~/Library/Logs`、Xcode、失效启动项 | 报告候选和阻断原因，不绕过特权或应用保护。 |
| `developer` | pip、uv、npm、Cargo、Homebrew、Docker 报告 | 普通缓存只建议审阅；Docker 只报告。 |
| `ai` | `~/.claude/cache`、`~/.codex/tmp`、`~/.gemini/tmp`、OpenCode、Cursor | 报告大小和保护状态，不自动清理。 |
| `project` | `node_modules`、`.venv`、`target`、DerivedData 等可重建产物 | 只分析明确的项目根，不扩大范围。 |
| `trash` | `~/.Trash` 与挂载卷 Trash | 只报告；清空是永久操作。 |

这些路径只是分类提示。实际候选、大小和可执行性始终以本次 JSON 输出为准；AI 不应自行
补充未被扫描器报告的删除路径。

## JSON 判读

| 内容 | 判读规则 |
|---|---|
| `potential_bytes` | 本次发现的物理占用。 |
| `reclaimable_bytes` | 当前 `actionable=true` 候选的占用，不代表已经释放。 |
| `requires_privilege_bytes` | 需要尚未实现的特权 helper，只能报告。 |
| `unsupported_bytes` | 当前明确不支持执行的占用。 |
| `complete`、`issues` | `complete=true` 仍需检查全部 `issues`；有 blocking issue 时不得宣称扫描完整。 |
| `actionable`、`action_block_reason` | `actionable=false` 时只报告原因，不尝试替代命令。 |
| `safety` | `safe` 也只是候选；`confirm`、`critical` 必须单独提示风险。 |
| `requires_privilege`、`is_cloud_file` | 特权项和云占位项只能报告。 |
| `requires_explicit_selection`、`preselected` | 精确选择要求和默认预选都不等于用户授权。 |

默认 JSON 包含绝对路径。输出需要离开本机会话时使用 `--json --redact-paths`；脱敏后的
`path:0001` 只能用于报告，不能作为后续 selector。

退出码：`0` 表示命令按契约完成；`1` 表示结果不完整、失败或能力 unavailable；`2` 表示
参数、规则、路径或配置错误；`130` 表示用户中断。`optimize ram|purgeable` 当前返回
`status=unavailable`、`executed=false` 和 exit `1`，这是预期安全拒绝。

## 停止边界

AI 不得自行添加 `--yes`，也不得自动清空 Trash、执行 Docker prune、修改 ignore/config、
更新知识库、调用 sudo 或绕过 `actionable=false`。普通清理通常进入同卷 Trash，但仍属于
文件写入。用户要求执行时，应把它作为新的写入任务重新核对精确目标和当前 `--help`。

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
- [隔离预览](PREVIEW.md)：19 个 TemporaryDirectory 场景和只读示例。
- [实现说明](../implementation/README.md)：完整 CLI、JSON 和选择契约。

文档与当前程序不一致时，以 `openclean <command> --help` 和实际 JSON 为准。

# CLI / TUI 调用模式契约

本文冻结 openclean 的三种调用方式与模式路由规则。同一个人可以同时使用文本 CLI 和
TUI；TTY 只表示终端能力，不推断调用者是人还是程序（自动化框架也可能分配 PTY）。
模式只决定显示与交互方式，不改变执行资格、选择语义、JSON schema 或退出码。

| 使用方式 | 输入 | 输出与结束方式 |
|---|---|---|
| 人工 TUI | 菜单与按键；初始范围/授权仍来自命令参数 | 可重绘界面、实时控制；用户退出或提交 |
| 人工文本 CLI | 完整命令、路径、筛选、选择参数 | 一次性文本报告，完成即返回；不等待 Enter |
| 程序化 CLI（`--json`） | 参数数组、明确范围、选择与授权参数 | 单个 JSON 文档、明确退出码；无按键等待 |

## 模式入口

| 调用形式 | 行为 | 状态 |
|---|---|---|
| `openclean`（stdin/stdout 均为 TTY） | 主菜单 | 已有 |
| `openclean <command>`（无显式模式，TTY） | 兼容期默认自动进入 TUI（clean/purge/analyze） | 已有；默认迁移单独决策 |
| `openclean analyze PATH --no-interactive` | 始终文本 CLI | 已有 |
| `openclean analyze PATH --json` | 始终机器输出，即使连接 PTY | 已有 |
| `openclean {clean,purge,analyze} ... --interactive` | 显式全屏 TUI | 可用 |
| `openclean analyze PATH --line-interactive` | 行式只读导航 | 已有，保持其只读限制 |

`--interactive` 需要 stdin 与 stdout 同时连接终端，缺失时在扫描前报
`interactive_requires_terminal`（exit 2）。显式 `--interactive` 失败（例如终端不支持
curses）不会静默改走其他模式；自动 TTY 判断失败时才保留文本回退。

## 路由优先级与冲突

判定集中在 `openclean.display_mode.resolve_display_mode`，命令分派在扫描前调用：

1. help/version 是独立的即时返回路径，不开始业务扫描。
2. `--json` 与显式交互（`--interactive` / `--line-interactive`）互斥，冲突在扫描前报
   `invalid_mode_options`（exit 2），不静默忽略其中一个。
3. `--no-interactive` 与显式交互互斥；`--json` 搭配 `--no-interactive` 是冗余的明确
   意图，继续允许。
4. `--interactive` 与参数化选择（`--select`、`--all`、`--include-confirm`、
   `--include-critical`、`--force`、Agent `--run/--finding`）互斥：菜单与参数是两个
   选择来源。`--yes` 是执行授权参数，不是选择来源，可与 `--interactive` 配合。
5. 无显式模式时沿用兼容期自动判断：stdin/stdout 均为 TTY 且没有参数化选择才进入
   TUI；否则文本/JSON。**“显式子命令默认文本 CLI、TUI 显式进入”是长期推荐目标，
   尚未实施**，迁移将由独立的兼容性变更处理。
6. `--line-interactive` 保持只读限制（不能与 `--yes`/`--select` 组合），它不是
   “非交互”模式。

## stdout / stderr

| 输出方式 | stdout | stderr |
|---|---|---|
| 文本 CLI | 最终报告、完整目标路径与摘要；无清屏或光标控制 | 错误与诊断；仅当该流确为终端时显示进度 |
| JSON | 一次调用一个完整 JSON 文档；错误沿用结构化 envelope | 附加诊断，无动画或交互提示 |
| TUI | 交互画面及退出后的既有报告 | 后台线程不写终端；退出时统一呈现错误 |

重定向流不输出颜色、清屏或按键提示；人工文本不因窗口宽度截断真实路径。退出码保持
`0` 完成 / `1` 不完整或不可用 / `2` 参数与规则错误 / `130` SIGINT。TUI 中 `Q` 是
人工放弃审阅（exit 0），不是机器可依赖的成功扫描信号；程序化取消使用进程信号。

“已暂停”需要所有活跃工作线程到达暂停点；任一线程仍在系统调用中时维持“暂停已请求”。
Analyze 只显示活跃 worker 实际访问的路径，排队任务不计为并行工作。扫描开始后的终端错误
不会触发回退重扫，而是结束任务并返回非零状态；仅初始化失败可沿用自动模式的回退。
程序化 SIGINT 在等待任务图线程之前传递取消，返回退出码 130 与现有结构化错误。
扫描任务提交也覆盖取消；协调线程使用有限周期等待，避免 macOS 将信号送达其他线程时
主线程长期阻塞，无法处理取消。
独立 CLI 的非交互扫描先记录 SIGINT，再由检查点协作取消，待线程收尾后统一输出错误；
信号处理器不在中途操作线程锁，扫描结束恢复原处理器。嵌入调用方自定义的信号策略保持不变。
SIGTERM 和消费者提前关闭管道不在本轮新增处理范围内，不承诺这些情况产生完整结果文档。

## 命令级模式覆盖

| 命令 | 文本 | JSON | TUI | 说明 |
|---|---|---|---|---|
| `scan`、`large` | 是 | 是 | 否 | 只读报告命令，无交互界面 |
| `clean`、`purge` | 是 | 是 | 扫描页 + 审阅页 | 扫描页支持 Space 暂停/继续、Q 取消 |
| `analyze` | 是 | 是 | 扫描页 + 空间浏览 | 另有 `--line-interactive` 行式导航 |
| `optimize`、`ignore`、`config`、`cat`、`inspect`、`show`、`strategy` | 是 | 是 | 否 | 无 TUI |
| 无参数 | 帮助 | — | 主菜单 | 仅 TTY；非 TTY 输出帮助并退出 0 |

回归入口：`tests/test_display_mode.py`（模式矩阵与冲突）、`tests/test_cli_contract.py`
（输出契约）、`tests/test_scan_tui.py`、`tests/test_analyze_session.py`、
`tests/test_analyze_pty.py`（真实 PTY 取消）。

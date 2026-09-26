# 06 · 用户、Agent 与执行流程

> 文档 ID：OC-06 · 修订：3 · 更新：2026-09-22 · 状态：baseline
> 来源：SRC-GOAL、SRC-CODE、SRC-CONTRACT；入口需求见 [00](00-architecture.md)。

## 1. 一条产品流程，两种调用方式

```text
用户给出目标/范围
    → 人工菜单或 Agent 选择现有 CLI
    → 发现与计量
    → 解释候选、诊断、阻断和完整性
    → 精确选择与预览
    → 在用户授权范围内执行受支持动作
    → 核对逐项回执，必要时重新扫描
```

REQ-FLOW-001：Agent 自己获取 help/JSON、保留精确结果并选择命令，不要求用户逐条敲命令。
对已有明确范围与动作授权，无需每次工具调用重复索要；新对象、新风险或含糊范围须先澄清。
VAL-FLOW-001：任务记录能对应发现、精确预览、授权依据和结果；只读诊断不得转成 shell 删除。

REQ-FLOW-001b：显示模式集中判定，显式参数优先于 TTY 探测。`--json` 是机器输出，
不能与 `--interactive`/`--line-interactive` 组合；`--no-interactive` 与显式交互互斥，
与 `--json` 同时出现是冗余的明确意图。`--interactive` 需要 stdin/stdout 均连接终端并
不能与参数化选择（`--select`/`--all`/`--include-confirm`/`--include-critical`/`--force`
及 Agent `--run/--finding`）组合，冲突在扫描前以 exit 2 拒绝；`--yes` 是执行授权，
不是选择来源，可与 `--interactive` 配合。无显式模式时沿用 TTY 自动判断；显式子命令
默认文本 CLI 属于未实施的推荐目标，不作为现状。
VAL-FLOW-001b：框架分配 PTY、重定向 stdout/stderr、关闭 stdin 与冲突组合在扫描前
被拒绝或进入非交互流程；JSON 输出不因 TTY 改变；显式 `--interactive` 失败不静默改走
其他模式。回归见 [test_display_mode.py](../implementation/tests/test_display_mode.py)
与 [docs/MODES.md](../docs/MODES.md)。

经典调用用扫描返回的精确 path/identifier；Agent 附加调用用本次 Run/Finding。
两者都是已有受限选择入口。没有任意 `delete PATH` 命令，不为自动化另造删除器。
`inspect` 写入本机 Run Store，读取过期 Run 可能删除过期 bundle；“候选只读”不等于零磁盘写入。

## 2. 审阅界面

REQ-FLOW-002：Clean/Purge 列表提供分类、复选、阻断原因与 I 详情；
详情只显示当前 Item 的路径/identifier、风险、说明、年龄、句柄和已有诊断证据。
未知信息明确未知，不能额外扫描后悄悄改变本次结论。

方向键滚动详情，Esc/左键返回原位置，Q 取消审阅。列表 Space/Enter 选择语义不变。
VAL-FLOW-002：长路径、缺失元数据、只读/阻断对象可查看；详情返回不改变选择和计量。

显示层按终端列宽处理中文/组合字符，长列表文本可以省略但详情保留完整内容；焦点和阻断
状态同时具有文字与视觉标记。快捷键完整换行，NO_COLOR/无颜色终端提供单色回退。
默认 `OPENCLEAN_THEME=auto` 使用终端默认前景/背景与反白焦点，兼容浅色和深色配置；
不依赖粗体颜色或淡化强度，不通过终端查询消费键盘输入。显式 light/dark 配色仅在
至少 256 色终端上启用、保留默认背景，NO_COLOR 优先；未知主题和能力不足回到默认色。
浅色/深色参考背景上的正文、状态和焦点文字对比度至少 4.5:1；用户自定义颜色仍由终端决定。
窗口小于 48×14 时保留当前选择并提示放大，只允许退出，避免确认隐藏的动作。
文本摘要分开显示发现、可执行、选择及只读/阻断量；这些显示改进不得改变 JSON 或执行资格。

Analyze 先显示工作状态，再在后台计量；Q 取消审阅、SIGINT 中断均先传播协作取消，再等待工作收尾。
返回目录可复有界会话视图，R 刷新；缓存只服务浏览，提交前重新分析选择来源，变化项撤销并重新审阅。
I 查看当前项详情，E 查看完整 issues；详情来自已有证据。零占用浏览行不扩张 CLI/JSON 清理候选，
未知容量不得冒充零字节。正常提交同时携带当前页面及所选来源的完整性，不完整返回 1；
主动取消审阅沿用退出 0，SIGINT 返回 130，取消不得呈现为扫描完整。

clean/purge/analyze 在 TTY 下先进入统一扫描页：Space 请求暂停/继续，Q 取消审阅（退出 0），
Ctrl-C 中断（130）。暂停只有全部活跃工作线程到达暂停检查点后才显示“已暂停”；阻塞 I/O 或不可暂停
调用期间保持“暂停已请求”，不凭按键状态宣称已停住。任务状态来自真实 started/terminal
反馈，首个未完成任务不冒充正在运行；加权百分比标注为任务进度。取消后等待扫描线程收尾，
不重开扫描；扫描转审阅边界清理过期按键，Q 优先，Space/Enter 不泄漏成审阅页的第一次选择。
非 TTY、JSON 与参数化选择路径不被扫描页截获。回归见
[test_scan_tui.py](../implementation/tests/test_scan_tui.py)。

Analyze 浏览提供三态选择标记：`[ ]` 未选、`[x]` 直接选择、`[-]` 目录内有独立选择而
本目录未选、`[x] 随上级` 被直接选择的祖先覆盖。对 `[-]` 行，按键前后说明替换的子项
数量；被覆盖的子项提示返回上级调整，不新建隐式排除。头部两层汇总同时显示全局已选与
“此目录内已选”（含更深层目标）；当前目录整体已选或被上级选择时如实标注，不拆算父项
计量虚构本页子项数量。这些是派生显示数据，精确选择集合、父子冲突规则与执行语义不变。
回归见 [test_space_tui.py](../implementation/tests/test_space_tui.py)。

## 3. 选择与确认

REQ-FLOW-003：无 `--select` 时使用既有默认预选和 tier 批量语义；
有 `--select` 时从空集合开始，只选择精确命中，不附带同等级项目。
`--select + --all` 在扫描前拒绝。环境来源项、Docker、updater 等显式选择要求不能被批量参数覆盖。
`--force --yes` 仅沿用默认预选并拒绝扩大选择参数，不表示绕过保护。

REQ-FLOW-004：不带 `--yes` 的 clean/purge/analyze 不修改候选。
TUI 有 `--yes` 仍需汇总确认；critical 保留二次确认。
根菜单不自动传 `--yes`，因此进入菜单不是清理授权。
Agent `--finding` 可重复，但必须属于指定 Run；风险 flag 不代替执行授权。

VAL-FLOW-003/004：选择测试证明未选项保留；无授权/取消/缺风险确认不执行。
配置写入的例外与触发方式见 [03](03-knowledge-base.md)，不能混用清理 flag 规则。

## 4. 执行与回执

| 状态 | 进入条件 | 允许行为 / 下一步 | 禁止宣称 |
|---|---|---|---|
| 已发现 | scan 或 inspect 返回 | 查看证据；不完整先说明限制 | 全部可清 |
| 已选择/预览 | 精确目标已解析 | 输出计划、风险与阻断；等待或应用已有授权 | 已执行 |
| 预检拒绝 | 任一选中项不满足执行前条件 | 整批不启动，输出 blocked/not_run 或既有错误 | 空报告等于成功 |
| 执行中 | 授权及预检通过 | 每项实时复核，再调用支持动作 | 多个 OS 操作具备事务回滚 |
| 完成/部分失败 | 动作已返回 | 按逐项 outcome 报告移动、永久删除、失败/partial | 全部成功或 moved 等于释放 |

REQ-FLOW-005：预检 all-or-nothing 与逐项实时检查同时保留。
已开始多个文件操作后可能部分成功；保留真实结果，不自动回滚或隐藏已发生副作用。
VAL-FLOW-005：混合阻断不启动；执行中异常正确返回部分结果、非零退出与已移动事实。

普通文件移动到同卷 Trash；清空 Trash 只处理审计快照内容，保留根和审计后新到达项。
Docker 仅固定 prune、要求目标绑定复核且 Volumes 禁止；真实 daemon 验收与单测分开。
特权、云占位、只读诊断等拒绝条件见 [07](07-predicate-engine.md)。

## 5. 输出与再次判断

REQ-FLOW-006：JSON stdout 不混入菜单或装饰；错误与不完整使用现有退出码，
0 是命令按契约完成，不是“有可执行目标”或“清理成功”。
预览 exit 0 仍应读取可执行性；执行看逐项 outcome 与 complete。
VAL-FLOW-006：JSON/非交互错误路径不隐藏键盘等待，脱敏覆盖嵌套结构。

必要时重新扫描核对目标是否仍存在、未选项是否保留。
重新发现的结果不是原计划的授权凭证；按用户原授权的对象和风险范围判断是否仍适用。
输出离开本机时用脱敏副本，内部执行仍使用原始精确结果；版本缺陷见 CHANGELOG。

## 6. 验证入口

- [AI_USAGE](../docs/AI_USAGE.md)：可用命令和授权内的经典使用流程。
- [test_cleanup_cli.py](../implementation/tests/test_cleanup_cli.py)、[test_cleanup.py](../implementation/tests/test_cleanup.py)、[test_clean_preview.py](../implementation/tests/test_clean_preview.py)：VAL-FLOW-003/004/005。
- [test_tui.py](../implementation/tests/test_tui.py)、[test_config_cli.py](../implementation/tests/test_config_cli.py)：VAL-FLOW-002 及菜单。
- [test_display_mode.py](../implementation/tests/test_display_mode.py)：VAL-FLOW-001b 模式矩阵。
- [test_scan_tui.py](../implementation/tests/test_scan_tui.py)、[test_analyze_session.py](../implementation/tests/test_analyze_session.py)、[test_analyze_pty.py](../implementation/tests/test_analyze_pty.py)：统一扫描页、三态选择与真实 PTY 取消。
- [test_json_redaction.py](../implementation/tests/test_json_redaction.py)、[test_agent_review_cli.py](../implementation/tests/test_agent_review_cli.py)：VAL-FLOW-006。
- [check_installed_wheel.py](../implementation/scripts/check_installed_wheel.py)：安装包实际 CLI 与临时 HOME 流程；脚本存在不等于当前安装包通过。

所有写入验证使用 TemporaryDirectory，不操作真实用户 Trash、进程或 Docker daemon。

# 06 · 用户、Agent 与执行流程

> 文档 ID：OC-06 · 修订：2 · 更新：2026-09-08 · 状态：baseline
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

经典调用用扫描返回的精确 path/identifier；Agent 附加调用用本次 Run/Finding。
两者都是已有受限选择入口。没有任意 `delete PATH` 命令，不为自动化另造删除器。
`inspect` 写入本机 Run Store，读取过期 Run 可能删除过期 bundle；“候选只读”不等于零磁盘写入。

## 2. 审阅界面

REQ-FLOW-002：Clean/Purge 列表提供分类、复选、阻断原因与 I 详情；
详情只显示当前 Item 的路径/identifier、风险、说明、年龄、句柄和已有诊断证据。
未知信息明确未知，不能额外扫描后悄悄改变本次结论。

方向键滚动详情，Esc/左键返回原位置，Q 取消审阅。列表 Space/Enter 选择语义不变。
VAL-FLOW-002：长路径、缺失元数据、只读/阻断对象可查看；详情返回不改变选择和计量。

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
- [test_json_redaction.py](../implementation/tests/test_json_redaction.py)、[test_agent_review_cli.py](../implementation/tests/test_agent_review_cli.py)：VAL-FLOW-006。
- [check_installed_wheel.py](../implementation/scripts/check_installed_wheel.py)：安装包实际 CLI 与临时 HOME 流程；脚本存在不等于当前安装包通过。

所有写入验证使用 TemporaryDirectory，不操作真实用户 Trash、进程或 Docker daemon。

# 00 · 产品目标、任务与架构边界

> 文档 ID：OC-00 · 修订：2 · 更新：2026-09-08 · 状态：baseline
> 来源：SRC-GOAL、SRC-CLI-MENU、SRC-CODE、SRC-CONTRACT；定义见 [索引](_index.md)。

## 1. 目标与非目标

OpenClean 是独立运行的 macOS 清理 CLI，不要求安装 CleanMyMac 或模型服务。
对齐参考 CLI 的核心流程和具体效果，增强个人经验识别、证据解释、精确选择和自动化接口。
“开源平替”是研发目标，不是当前全功能等效声明。

不在当前范围：Desktop GUI、菜单栏/后台服务、恶意软件扫描、通用应用卸载、
自动执行未知目录删除、强制平台化/pack 化。Optimize 未有等效执行器，不能拿 refusal 算完成。

## 2. 用户任务需求

| 需求 | 触发与行为 | 异常/边界 | 验收 |
|---|---|---|---|
| REQ-OC-001 Clean | 查看系统/开发/AI/Trash 候选，区分可操作、只读和阻断；选择后预览或授权执行 | 空结果、取消、不完整、不可执行必须如实呈现 | VAL-OC-001：无 `--yes` 候选不变；授权执行不越过选择集 |
| REQ-OC-002 Purge | 在选定项目范围发现已知构建/依赖产物，按项目审阅 | 项目标记不是垃圾；未知目录不因名称相近进入删除范围 | VAL-OC-002：项目分组、精确选择和保留未选对象通过隔离夹具验证 |
| REQ-OC-003 Analyze | 显示空间占用、排序、截断和目录导航；显式选择才进入动作审阅 | 占用不等于垃圾；一级目标按 critical 处理 | VAL-OC-003：报告可回收量为 0；不跨候选文件系统计量或扩大选择 |
| REQ-OC-004 Optimize | 提供 RAM/purgeable 入口与真实可用性说明 | 当前返回 unavailable/exit 1，不运行替代性内存压力命令 | VAL-OC-004：文本/JSON refusal 和无副作用 |
| REQ-OC-005 Config | 查看偏好；通过现有显式配置命令更新 analytics 或托管规则 | analytics 偏好不等于遥测上传；更新规则是显式网络与写操作 | VAL-OC-005：配置读写、错误、权限与回读符合现有契约 |
| REQ-OC-006 自动化 | Agent/脚本使用同一命令、JSON、退出码；用户给目标和授权即可 | 不要求手工搬运 JSON；不自行添加未实现命令或扩大授权 | VAL-OC-006：非 TTY/JSON 不等待交互，精确预览和 outcome 可解析 |

`scan` 是自有五域发现入口，`ignore` 是用户规则入口，`cat` 是原创彩蛋；均保留。
菜单同名仅证明流程对应；具体覆盖、计量差异和收益证据记入能力地图，不虚构完成百分比。

## 3. 入口与终端契约

REQ-OC-007：无参数 TTY 显示 Clean/Purge/Analyze/Optimize/Config，方向键和 Enter 导航，
M 打开 More（Cat/返回）。根 Q/Esc 退出，次级返回；子任务结果停留后按 Enter 返回原菜单。
CLI 在退出当前 curses wrapper 后分派子命令，不嵌套会话，不自动加 `--yes`。

初始化失败退回行式菜单；小终端和 resize 不崩溃。子任务非零状态不能包装成成功。
非 TTY 无参数输出帮助并退出 0；显式命令、JSON、帮助、版本不进入根菜单。
VAL-OC-007：屏幕 stub 与隔离 PTY 覆盖导航、返回、失败退路、取消和终端恢复；多字体视觉验收另报。

## 4. 共享架构，而非替换路线

沿用模块化 Python CLI、标准库运行时和现有目录。职责是约束，不要求按参考软件类名重建：

| 领域 | 现有实现 | 边界 |
|---|---|---|
| 参数与输出 | `cli.py`、`redaction.py` | 不自行删除；脱敏不改内部选择对象 |
| 扫描与分析 | `engine.py`、`analyzer.py`、各专项 scanner | 发现与计量，不把发现等同授权 |
| 模型与规则 | `models.py`、`knowledge_base.py`、`predicates.py` | Item 是有效模型，不预定退役 |
| 执行 | `cleanup.py`、`macos.py`、`docker.py` | 选择、预检、实时复核、有限动作和逐项回执 |
| 交互 | `tui.py`、`space_tui.py`、`navigator.py` | 人工审阅，不绕过共享执行器 |
| Agent 附加层 | `core/`、`strategies/`、`runtime/`、`actions/` | Run/Finding 支持跨命令证据，不接管所有经典能力 |

REQ-OC-008：保持现有经典参数、JSON schema v2 和退出语义；接口迁移须独立提出影响和方案。
VAL-OC-008：既有 CLI/选择测试继续成立，新增接口不能使经典命令依赖 Run Store。
当前 Python 基线为 3.11，原生执行支持 macOS；不把纯逻辑跨平台测试算成原生验收。

## 5. 实现与验收锚点

- Clean/兼容：[cli.py](../implementation/openclean/cli.py)、[test_cleanup_cli.py](../implementation/tests/test_cleanup_cli.py)、[test_cli_contract.py](../implementation/tests/test_cli_contract.py)。
- Purge：[test_project_purge.py](../implementation/tests/test_project_purge.py)。
- Analyze：[test_analyzer.py](../implementation/tests/test_analyzer.py)、[test_analyze_cleanup.py](../implementation/tests/test_analyze_cleanup.py)。
- Optimize/Config：[test_optimize_cli.py](../implementation/tests/test_optimize_cli.py)、[test_config_cli.py](../implementation/tests/test_config_cli.py)。
- 终端：[test_tui.py](../implementation/tests/test_tui.py)、[test_space_tui.py](../implementation/tests/test_space_tui.py)。
- Agent 实际流程：[06](06-system-flow.md)、[Agent 接口](agent-runtime/_index.md)。

本篇不重复能力状态表或发布记录；参考产品的深入行为仍需按具体任务补证据。

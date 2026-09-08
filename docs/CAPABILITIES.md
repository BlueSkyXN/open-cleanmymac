# CLI 能力与边界地图

[README](../README.md) · [功能预览](PREVIEW.md) ·
[架构](ARCHITECTURE.md) · [安全](../SECURITY.md) ·
[规格索引](../specs/_index.md) · [实现说明](../implementation/README.md)

本文把“材料里出现过的能力”和“当前 `openclean` CLI 承诺交付的能力”分开。内部组件或
Desktop 背景事实不会自动变成 CLI backlog；高风险能力可以有意保持只读或 fail-closed。
用户入口见 [README.md](../README.md)；本页是范围与验证的权威表。

> **两套命令面并存**：经典五域面（`scan`/`clean <category>`/`analyze`/`purge` + TUI）
> 与 **Agent Runtime**（`inspect`/`show`/`clean --run --finding`/`strategy`，当前内置
> `codex` 和 `workbuddy` pack）同时可用，共享同一套底层探测器、计量、保护闸与同卷 Trash 执行器。
> Agent Runtime 契约见 [specs/agent-runtime/](../specs/agent-runtime/_index.md)。

## 状态定义

| 状态 | 含义 |
|---|---|
| `available` | 当前代码可执行，并有自动化验证 |
| `read-only` | 可发现或报告，没有写路径 |
| `guarded-unavailable` | 命令面存在，但明确拒绝并返回非零退出码 |
| `external-prerequisite` | 仍需签名、正式服务或真实外部环境，不能只靠 Python 代码完成 |
| `not-implemented` | 规格中有背景或候选设计，但当前没有可调用实现 |
| `out-of-scope` | Desktop/后台能力，不属于当前 CLI 范围 |

`origin_kind` 仅用于解释来源范围：`public-cli`、`internal`、`desktop-background` 或
`project-extension`，不表示复用参考实现代码或私有数据。
这些既有标签不证明参考行为已逐条核实；内部名称与历史材料仅能提供研究线索。
规格重写后保留能力状态与标签，不把来源重新分类当成运行时能力变更。

## 用户能力矩阵

### 核心流程对照

对标版本为 CleanMyMac CLI v1.0.0 Public Beta。五项主入口及其用途依据该版本公开菜单；
OpenClean 的实现与验收要求见 [00](../specs/00-architecture.md)、
[02](../specs/02-scan-points.md)、[06](../specs/06-system-flow.md) 和本项目源码；
这些自有契约不替代参考产品细化功能的公开来源或同状态实验。
菜单只能证明入口及用途，不能证明扫描覆盖或清理效果等效；尚无同状态对比实验的部分保留待核实。

| 参考流程 | OpenClean 入口与实现 | 对齐状态及具体差距 | 自有增强与验证入口 |
|---|---|---|---|
| Clean：扫描、审阅并处理垃圾 | `clean [junk/dev/ai/trash]`；`engine.py`、`cleanup.py`、`tui.py` | 部分对齐：用户态流程可用，扫描点为保守子集，特权系统清理未实现；逐对象识别效果待核实 | 精确选择、运行应用/updater 保护、只读诊断；`test_cleanup_cli.py`、`test_cleanup.py`、`test_tui.py` |
| Purge：扫描并审阅开发产物 | `purge [path]`；项目发现、按项目分组、同卷 Trash | 部分对齐：公开产物字典可用；参考产品逐类型覆盖与默认选择差异待核实 | 年龄/嵌套/精确选择保护；`test_cleanup_cli.py` 和项目扫描测试；Docker 是另有真实环境前提的扩展 |
| Analyze：可视化存储占用 | `analyze [path]`；`analyzer.py`、`space_tui.py`、`navigator.py` | 部分对齐：空间浏览、排序与精确选择可用；完整参考交互及扫描效果待核实 | 按卷边界、云占位保护，不将占用视作可回收；`test_space_tui.py`、`test_analyze_cleanup.py` |
| Optimize：维护任务 | `optimize ram/purgeable`；CLI refusal | 执行能力未实现：命令可调用但返回 unavailable/exit 1，不算优化功能等效 | 原因透明，不以制造内存压力冒充优化；`test_optimize_cli.py` |
| Config：CLI 偏好 | `config`、`ignore`；`config.py`、`knowledge_base.py` | 部分对齐：配置与忽略生命周期可用；参考产品全部偏好项待核实 | 独立 JSON 规则与签名更新客户端；`test_config_cli.py`；正式发布 channel 未配置 |

“已对齐、部分对齐、未实现、有意不做”描述参考功能差距，不替换下表能力状态或 JSON 枚举。
Desktop 应用卸载、恶意软件扫描等有意不进入本 CLI 对齐范围。Cat 为原创彩蛋，不计入平替声明。
上述测试文件是验证入口，不是对当前工作区或真实环境已通过的声明。

| capability | command | status | boundary / exclusion |
|---|---|---|---|
| 五项主菜单 | 无参数 TTY | `available` | 方向键/Enter、More/Cat、Optimize 原因展示；子任务后暂停返回；初始化失败退回行式菜单；不附加 `--yes` |
| 候选只读详情 | Clean/Purge TUI 的 `I` | `available` | 当前 Item 证据、长路径滚动、未知值与子集说明；不重新扫描、不改变选择；Clean 文本提供诊断摘要，JSON 不变 |
| Agent Runtime 探测 | `inspect <target>` | `available`（`codex`、`workbuddy`） | 只读探测，固化 Run/Finding 到本机 Run Store；未交付 target 返回 `pack_not_found`/exit 1 |
| Finding 审阅 | `show --run --finding` | `available` | 从 Run Store 读取完整证据；只读 |
| Finding 驱动清理 | `clean --run --finding` | `preview-only`（P0a） | 内置 7 条策略均只读；P0b 结构匹配与生产审批未完成；见 [当前状态](AGENT_RUNTIME_STATUS.md) |
| 策略包查看/校验 | `strategy list/show/verify` | `available` | 只读；`codex`/`workbuddy` pack 随包分发，各自 hash 稳定 |
| 五域聚合扫描 | `scan` | `available` | `scan` 始终只读；扫描点是保守公开子集 |
| 分类清理 | `clean junk / dev / ai` | `available` | 默认预览；`--yes` 只执行当前已审阅选择 |
| Trash 审阅与清空 | `clean trash` | `available` | confirm；内容永久删除，根目录保留 |
| 项目产物清理 | `purge [path]` | `available` | 只处理公开产物字典；普通项移到同卷 Trash |
| 空间分析 | `analyze [path]` | `available` | 只报告实际占用；一级候选不跨设备；critical 精确选择；不自动删除 Time Machine 快照 |
| 精确参数选择 | `clean/purge --select` | `available` | 从空选择集开始；tier flag 只作风险 gate；拒绝 `--select + --all` |
| 文本/JSON/TUI 输出 | 全局 | `available` | 默认 JSON 保留精确路径；`--redact-paths` 生成不可 replay 的单文档 opaque refs |
| 用户 ignore | `ignore list / add / remove` | `available` | 写入用户 `0600` JSON；不内置私有规则 |
| CLI 配置 | `config` | `available` | analytics 仅是偏好；当前没有遥测上传 |
| 签名托管知识库客户端 | `config --update-knowledge` | `external-prerequisite` | 客户端已完成；项目尚无正式 URL、公钥和发布流程 |
| Docker 容量与固定 prune | `scan/clean dev` | `external-prerequisite` | identifier 精确选择和 target binding 已完成；真实 daemon 验收待完成；Volumes 永远拒绝 |
| 失效启动项 | `scan/clean junk` | `available` | 仅可确认失效的用户项可执行；系统项需要 helper |
| ApplicationLanguages | `scan/clean junk` | `read-only` | 固定 `critical + actionable=false`；不修改签名 app |
| Time Machine 本地快照提示 | `analyze` | `read-only` | 只显示名称/数量；不宣称精确大小，不删除 |
| dataless/疑似占位保护 | 所有文件扫描/执行 | `available` | 不保证识别全部已 materialized cloud-synced 文件；真实 provider fixture 待验收 |
| 运行中应用缓存保护 | `scan/clean junk/ai` | `available` | 专用扫描点及已知 `~/Library/Caches` 归属候选继续显示，但不可执行；进程状态未知时 fail-closed |
| updater 版本状态保护 | `scan/clean junk` | `available` | 新版/应用缺失/未知状态不可执行；同版/旧版 critical 精确选择并在执行前重判 |
| 按卷容量汇总 | JSON 扫描/预览 | `available` | 按运行时 device 分组系统盘与外置盘；非文件系统资源不归卷 |
| 日志/runtime/download 保留期 | `scan/clean junk` | `read-only` | WorkBuddy、Codex、Lark、Shadowrocket、TRAE、UURemote 的公开根；Codex 另按 `YYYY/MM/DD` 分区；不读取正文/包内容或批量删除 |
| WorkBuddy 个人经验结构 | `scan --domain ai` / `clean ai` / `inspect workbuddy` | `read-only` | expired 后缀、numeric Worker 整组年龄和精确 Electron 缓存；不触碰 binaries、插件、技能、会话历史；见 [经验依据](EXPERIENCE.md) |
| 浏览器 CacheStorage 保留期 | `scan/clean junk` | `read-only` | Chrome/Brave/Edge/Comet Default/Profile 根；不读取 origin、Cookies、Login Data、IndexedDB 或整个 Profile |
| SQLite freelist | `scan/clean ai` | `read-only` | immutable page/freelist/WAL/句柄；不 `VACUUM` 或删除数据库 |
| Codex 临时结构与 Crashpad 配对 | `scan/clean ai` | `read-only` | `.tmp` 整根固定保护；只报告精确 staging、Git 空壳和无同名 dump 的 sidecar；staging 超限返回有界部分结果 |
| deleted-open 卷占用 | `scan/clean junk` | `read-only` | `lsof +L1` 字段模式、device/inode 去重和逻辑大小上限；路径只用于内存保护过滤，结果只显示进程名 |
| Darwin updater 临时副本 | `scan/clean junk` | `read-only` | getconf 动态根、Qoder ShipIt 版本状态；固定不可执行 |
| Darwin 临时/运行副本 | `scan/clean junk` | `read-only` | `T/X` 公开名称模式、7/14/30 天容量、进程/句柄；固定不可执行 |
| RAM/purgeable 能力状态 | `optimize ram / purgeable` | `guarded-unavailable` | 没有已验证、安全、公开的等价执行接口 |
| 特权系统清理 | 候选只读可见 | `external-prerequisite` | 需 native host/helper、SMAppService、签名、entitlements、安装验收 |
| universal binary thinning | 无 | `not-implemented` | 当前不实现；修改签名/兼容性风险高 |
| Desktop GUI、菜单栏、后台 agent | 无 | `out-of-scope` | 当前产品只开发 CLI；curses TUI 属于 CLI |
| 应用卸载、恶意软件扫描等 Desktop 功能 | 无 | `out-of-scope` | 不属于当前公开 CLI 对齐目标 |

## 实现与验证索引

| capability | origin_kind | implementation | validation |
|---|---|---|---|
| 主菜单与只读详情 | `project-extension` | `cli.py`、`tui.py` | `test_config_cli.py`、`test_tui.py`、`test_cleanup_cli.py`：调度/返回/终端失败、详情状态、诊断摘要与容量兼容 |
| WorkBuddy 经验结构 | `project-extension` | `workbuddy.py`、`storage_diagnostics.py`、`packs/workbuddy.json` | `test_workbuddy.py`：正反结构、整组年龄、边界/上限、经典扫描及 inspect→show→preview→拒绝执行 |
| Agent Runtime（P0 Codex 切片） | `project-extension` | `core/`、`strategies/`、`runtime/`、`actions/`、`packs/codex.json`、`cli.py` | `test_agent_*`：模型不变量、注册/hash、Run Store 权限/TTL、Item↔Finding 投影、inspect 编排、planner can_execute、Finding 驱动执行、命令面契约 |
| 五域聚合扫描 | `public-cli` | `cli.py`、`engine.py`、`scanpoints.py` | 单测 + `scan-all-domains` preview |
| 分类清理 | `public-cli` | `cleanup.py`、`tui.py` | 选择/执行单测 + 临时 Trash preview |
| Trash 审阅与清空 | `public-cli` | `macos.py`、`cleanup.py` | 两个合成 Trash 根的无扩面执行 preview |
| 项目产物清理 | `public-cli` | `engine.py`、`cleanup.py` | 项目发现/年龄/嵌套/执行测试 |
| 空间分析 | `public-cli` | `analyzer.py`、`filesystem.py`、`space_tui.py`、`navigator.py` | 单层排序、EINTR、device/filesystem 双边界、零 reclaimable、TUI/精确执行测试 |
| 精确参数选择 | `project-extension` | `cleanup.py`、`cli.py` | no-collateral selection 单测 + preview |
| 文本/JSON/TUI 输出 | `public-cli` | `cli.py`、`redaction.py`、`tui.py`、`space_tui.py` | schema v2、opaque path refs、解析前错误、状态机及 SVG 资产测试 |
| 用户 ignore | `public-cli` | `knowledge_base.py` | lifecycle、权限、规范路径回执、原子写测试 |
| CLI 配置 | `public-cli` | `config.py` | analytics lifecycle/readback |
| 签名托管知识库客户端 | `project-extension` | `knowledge_update.py` | HTTPS/验签/跨进程防回滚/原子安装测试 |
| Docker 容量与固定 prune | `project-extension` | `docker.py` | parser/白名单/精确选择/CLI realpath-context-host-endpoint-Engine ID binding 隔离测试 |
| 失效启动项 | `internal` | `startup_items.py` | plist 解析、重判、用户态执行测试 |
| ApplicationLanguages | `internal` | `application_languages.py` | metadata/语言/签名风险测试 |
| Time Machine 本地快照提示 | `internal` | `macos.py`、`analyzer.py` | `tmutil` parser 与根卷分支测试 |
| dataless/疑似占位保护 | `project-extension` | `models.py`、扫描器、`cleanup.py` | `SF_DATALESS`、zero-block、禁止枚举/最终复核测试 |
| 运行中应用缓存保护 | `project-extension` | `application_ownership.py`、`engine.py`、`cleanup.py` | 专用/通用入口、进程探测失败、相似 sibling、执行前复核测试 |
| updater 版本状态保护 | `project-extension` | `updater.py`、`engine.py`、`cleanup.py` | app/ZIP metadata、版本比较、缺失/损坏、执行前变化测试 |
| 按卷容量汇总 | `project-extension` | `cli.py`、`macos.py` | system/external device JSON 分组测试 + 实机 Trash readback |
| 日志/runtime/download 保留期诊断 | `project-extension` | `storage_diagnostics.py`、`processes.py` | 固定/动态根、mtime 桶、物理块、ignore、进程/句柄和不可执行测试 |
| 浏览器 CacheStorage 保留期 | `project-extension` | `storage_diagnostics.py` | 已知浏览器与 Default/Profile 发现、symlink 拒绝、运行态和不可执行测试 |
| SQLite freelist 诊断 | `project-extension` | `storage_diagnostics.py` | immutable URI、页统计、sidecar 不变和 malformed DB 测试 |
| Codex 临时结构与 Crashpad 配对 | `project-extension` | `storage_diagnostics.py` | staging 精确根、Git 正/负结构、dump/sidecar 配对、最近项、句柄和不可执行测试 |
| deleted-open 卷占用 | `project-extension` | `processes.py`、`storage_diagnostics.py` | lsof 字段 parser、跨进程 FD 去重、按卷映射、未知 device 和不可执行测试 |
| Darwin updater 临时副本 | `project-extension` | `macos.py`、`updater.py`、`storage_diagnostics.py` | getconf、动态 root、版本状态和不可执行测试 |
| Darwin 临时/运行副本 | `project-extension` | `application_ownership.py`、`storage_diagnostics.py` | `T/X` 合成目录、未知名称排除、进程归属和不可执行测试 |
| RAM/purgeable 能力状态 | `public-cli` | `cli.py` | guard JSON/text/退出码测试 |
| 特权系统清理 | `internal` | 仅模型与 IPC 规格 | fail-closed 测试 |
| universal binary thinning | `internal` | 仅规格 | 无写路径 |
| Desktop GUI、菜单栏、后台 agent | `desktop-background` | 无 | 不适用 |
| 应用卸载、恶意软件扫描等 Desktop 功能 | `desktop-background` | 无 | 不适用 |

## 验证边界

本地 `make check` 仅做语法和 CLI 启动检查，修改相关逻辑时补充定向测试。
GitHub Actions 的 `make ci-check` 运行 lint、完整 unittest 和 `TemporaryDirectory` 隔离
预览，再单独构建、审计归档和验证安装。以 exact-head 结果证明相应逻辑、归档和合成写路径，
不能替代以下验收：

- 真实 iCloud Drive/第三方 File Provider 的 dataless 状态保持；
- 用户明确授权的 Docker 测试 daemon before/after；
- native XPC helper 的签名、安装、升级与回滚；
- 项目自有托管知识库服务、公钥和灾备流程；
- 不同 macOS/Python 版本的兼容矩阵。

这些外部边界不会在 preview 中伪造为成功。对应状态和原因会继续以结构化
`guarded-unavailable` 或 `external-prerequisite` 展示。

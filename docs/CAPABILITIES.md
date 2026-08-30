# CLI 能力与边界地图

[README](../README.md) · [功能预览](PREVIEW.md) ·
[架构](ARCHITECTURE.md) · [安全](../SECURITY.md) ·
[规格索引](../specs/_index.md) · [实现说明](../implementation/README.md)

本文把“材料里出现过的能力”和“当前 `openclean` CLI 承诺交付的能力”分开。内部组件或
Desktop 背景事实不会自动变成 CLI backlog；高风险能力可以有意保持只读或 fail-closed。
用户入口见 [README.md](../README.md)；本页是范围与验证的权威表。

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

## 用户能力矩阵

| capability | command | status | boundary / exclusion |
|---|---|---|---|
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
| 浏览器 CacheStorage 保留期 | `scan/clean junk` | `read-only` | Chrome/Brave/Edge/Comet Default/Profile 根；不读取 origin、Cookies、Login Data、IndexedDB 或整个 Profile |
| SQLite freelist | `scan/clean ai` | `read-only` | immutable page/freelist/WAL/句柄；不 `VACUUM` 或删除数据库 |
| Codex 临时结构与 Crashpad 配对 | `scan/clean ai` | `read-only` | `.tmp` 整根固定保护；只报告精确 staging、Git 空壳和无同名 dump 的 sidecar |
| deleted-open 卷占用 | `scan/clean junk` | `read-only` | `lsof +L1` 字段模式、device/inode 去重和逻辑大小上限；只显示进程名，需退出应用或重启释放 |
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

当前自动化基线覆盖 383 个 `unittest` 和 19 个 `TemporaryDirectory` 隔离预览场景。
这能证明当前 checkout 的本地逻辑、归档和合成写路径，但不能替代以下验收：

- 真实 iCloud Drive/第三方 File Provider 的 dataless 状态保持；
- 用户明确授权的 Docker 测试 daemon before/after；
- native XPC helper 的签名、安装、升级与回滚；
- 项目自有托管知识库服务、公钥和灾备流程；
- 不同 macOS/Python 版本的兼容矩阵。

这些外部边界不会在 preview 中伪造为成功。对应状态和原因会继续以结构化
`guarded-unavailable` 或 `external-prerequisite` 展示。

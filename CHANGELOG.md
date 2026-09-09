# Changelog

[README](README.md) · [能力地图](docs/CAPABILITIES.md) ·
[功能预览](docs/PREVIEW.md) · [架构](docs/ARCHITECTURE.md) ·
[安全](SECURITY.md) · [规格索引](specs/_index.md)

本文件记录用户可见变化，格式参考 Keep a Changelog。GitHub Releases 是唯一发行渠道；
项目不通过 PyPI、Homebrew 或其他包管理器分发。具体发布状态、tag 和附件以发行页为准。

## [Unreleased]

### Changed

- 通用用户缓存和 Darwin 用户缓存保留静态归属规则，并为精确 bundle ID 补充有界
  Spotlight / Info.plist 查询。同 ID 多个已验证安装位置合并进程保护，执行前复核当前进程；
  未找到、查询失败或达到预算不作为卸载或可删除证据，未知归属保持原有选择与执行行为。
- Purge 按依赖、环境、索引、构建等产物说明清理后果，明确预选年龄来自产物及其内容，
  不代表整个项目活跃度。复用现有 note，候选、默认选择、风险等级和 JSON schema v2 不变。
- 路径脱敏覆盖动态归属产生的应用进程路径标记，不改变内部执行目标和保护标记。
- 根据公开 Chrome profile 契约，补充已有 Antigravity 数据根下的六个 Default 缓存子目录；
  为 chrome-devtools-mcp 补充旧名 Default/DawnCache 和根级 GrShaderCache。
  保留登录、会话、未选缓存和整个 profile；不扫描相似名称、不扩展自定义数据根，AI 默认不选。
- Purge 普通文本预览也显示已有 note，与 JSON/TUI 的清理后果说明一致；执行结果不重复预选提示。
- 应用归属解析隔离深层嵌套 binary plist 引发的 RecursionError，将该应用标记为不可验证，
  不再因一个异常 Info.plist 中断整条通用缓存扫描；正常应用的运行保护继续生效。

目录依据与版本边界见能力地图；本节源码变化尚未发布，不代表所有安装版本的实机验收。

## [0.24.0a2] - 2026-09-08

Alpha 修复版；生产策略、经典选择和清理权限不变。旧版本 Run 不直接用于执行，升级后重新 inspect。

### Fixed

- 修复 Agent `clean --json --redact-paths` 跳过嵌套元组，导致计划中目标路径和 Finding ID
  未脱敏的问题；保持 JSON schema、未脱敏选择与清理权限不变。新增 CLI 回归和安装包脱敏断言。
- 修复 Python 3.11 下 `make test-focused` 零匹配仍成功的问题；只发现一次测试，零测试明确失败。
- 定向 runner 核心测试不再依赖仓库 Makefile；源码发行包可运行核心用例，仅跳过 checkout 专用接线检查。

`0.24.0a1` 的既有附件不包含这些修复，升级使用新版本，不覆盖旧 tag 或附件。

### Documentation

- 全面重写 `specs/` 为 OpenClean 产品行为与验收契约，保留文件路径；撤销 Agent 平台定位、
  全量模型迁移和经典/TUI 退役路线，区分当前接口、参考依据及未批准提案。
- 校准实际 pack/命令/字段与只读、预览、存储副作用边界，保留自有经验与执行保护；
  同步开发入口和文档分工，不改变运行时、schema、扫描对象或动作权限。

### CI

- 将已弃用的 `macos-14` runner 改为 `macos-15` / `macos-26` 双系统矩阵，Python 保持 3.11；
  两边均运行完整检查、构建和安装验证，关闭矩阵 fail-fast，上传产物按系统分别命名。
- 两个系统均从实际构建的 sdist 解压运行定向 runner 回归，补齐源码包布局验收。

## [0.24.0a1] - 2026-09-07

首个 GitHub Alpha 预发行。保留经典五域清理与 Agent 接口，不宣称已实现 Optimize、
特权 helper、正式知识库发布源或完成真实 Docker daemon 验收。

### Release validation

- 独立安装 wheel 的验收覆盖 Codex/WorkBuddy 的发现、详情、预览与只读拒绝，以及临时项目的精确清理和再次扫描。
- GitHub Release 提供 wheel、sdist 与 SHA256SUMS；产物来自通过 CI 的发行提交。

### Added / Changed — Agent 使用与个人经验

- 从项目历史及个人复核记录恢复 WorkBuddy 经验：识别日志 `.expired-` 后缀、numeric Worker
  整组 traces 年龄，以及 session/Partitions 中的精确 Electron 缓存；始终只读，支持有界发现/测量。
- Worker 整组有保护跳过、云占位、跨卷或测量错误时年龄标为未知；保留期 JSON 补齐已有云占位计数字段。
- 补齐 `com.tencent.workbuddy.mac.BundleMigration` 的 WorkBuddy 运行应用归属保护，不扩大相似名称匹配。
- 新增 `inspect workbuddy` 只读包，与经典 AI 域复用 detector；Codex hash、JSON schema、
  生产动作审批和经典执行边界不变，不把历史“安全”判断自动升级为删除权限。
- Agent 调用指南覆盖用户指挥、扫描、解释、精确预览、已有明确授权内的经典执行与结果核对；
  自有经验来源及不能泛化的条件记录在 `docs/EXPERIENCE.md`。

### Added / Changed — CLI 核心流程与特色展示

- 无参数 TTY 主菜单提供 Clean/Purge/Analyze/Optimize/Config，方向键与 Enter 导航、More/Cat，
  子任务结束后保留结果并返回；初始化失败退回行式菜单，非 TTY 无参数仍仅输出帮助。
- Clean/Purge 逐项列表新增 `I` 只读详情，支持长路径和证据滚动、未知值、阻断及子集说明；
  Clean 文本同步展示 retention/SQLite/updater/Codex/Crashpad/deleted-open 摘要。
- 主菜单不附加 `--yes`，Optimize 仍不可用；选择与执行保护、默认范围、JSON schema、
  Agent 接口和生产策略状态不变。
- 能力地图按五项用户流程列出对齐依据、具体差距和自有增强，不以命令存在代替功能等效。

### Development workflow

- 显式固定 Ruff 基础错误规则，避免工具默认变化引入额外风格门禁；功能测试和构建检查不变。
- `make check` 改为无需开发依赖的本地轻量检查；新增 `test-focused` 按文件选择测试。
- 原全量检查保留为 `make ci-check`，由 GitHub Actions 执行；云端增加已安装 wheel 的 Agent 预览链路验证。
- Python 3.11 基线不变，本地不强制额外 Python 版本或精确 Ruff 版本；清理保护和运行时依赖不变。

### Fixed / Performance — local cleanup follow-up (2026-09-05)

- 非 UTF-8 的配置和规则文件现在返回既有 `config_error` / `rules_error`，不再抛出未捕获异常或改写原文件。
- 路径脱敏每份文档只构建一次替换顺序；进程匹配每条命令只转换一次大小写。
- 复用空间分析 JSON 的总量，选择首个分析候选后停止查找，并合并批量等级选择的重复遍历；命令和数据格式不变。

### Performance — implementation-only optimization (2026-09-05)

- 普通路径的重叠计量改用最近已扫描父路径索引，避免全量两两比较；保留相对路径和双斜杠根路径的原有语义。
- 未注册进度回调时不再构造任务快照，显式快照、进度状态和回调顺序不变。
- 同一次对象解码复用字段与类型信息；不跨调用缓存，继续识别后续注解变化。
- 合并相同 JSON 汇总与重复成员查询，减少无用聚合对象、辅助包装和不可达分支；命令、字段、依赖和策略状态不变。

### Added / Fixed — 0.24.0a1 candidate (2026-09-05)

- 新增 Agent `inspect/show/clean --run/strategy` 命令与 P0a Codex 只读包；经典五域与 TUI 保留。
- Run bundle v2 严格绑定完整清单、策略版本、HOME、目标/证据/评估/计量；原子读写与容量上限，
  旧格式要求重新 inspect，不做隐式补造或重扫。
- 计划执行要求独立确认与上下文，实时重扫后使用新 Item；混合被阻止批次不启动且不声称成功。
- 修正 HOME/ignore 作用范围、外部 pack 执行隔离、类型/JSON 错误处理、脱敏错误中的 ID，
  并统一 `plan.plan_items` 与 CLI schema v2。
- 修正 staging 计量前后变化检测、根目录年龄、部分扫描处理及空过测试。
- 版本为候选版；P0b 的结构匹配器、真实 Observation 和生产策略审批仍未完成，内置 7 条策略均只读。
  候选开发阶段未开放生产动作；实际 tag、验证和发行产物以本版本发行页为准。

### Changed

- 文档按读者分层：根 README 收缩为用户首页；CLI/JSON/规则契约留在
  `implementation/README.md`；能力状态以 `docs/CAPABILITIES.md` 为准；规格去掉本项目
  实现附录，差异集中到 `specs/_index.md`。`HANDOFF.md` 并入 `AGENTS.md`，常驻文档不再
  写入过期的测试计数；隔离预览 transcript 与当前命令输出同步。SECURITY 只保留政策与
  威胁模型，实现细节下沉到架构说明。

### Planned

- 在具备完整 Xcode、签名身份和产品策略后，单独设计 native SMAppService/XPC helper。
- 仅在确认安全公开 macOS 接口后实现 `optimize ram|purgeable` executor。
- 配置项目自有的签名知识库 channel 和正式公钥。

## [0.23.0+] - 2026-08

0.23.0 基线之后、尚未切正式版本号的功能与加固。能力状态以
[docs/CAPABILITIES.md](docs/CAPABILITIES.md) 为准。

### Added

- Chrome、Brave、Edge、Comet 用户 Profile `Service Worker/CacheStorage` 只读保留期诊断。
- deleted-open 按卷诊断：`lsof +L1` 字段解析、device/inode 去重、逻辑大小上限；路径仅在
  内存中用于 protect/ignore。
- AI agent 只读调用指南；开发工具版本集中到 `requirements-dev.txt`。
- JSON `--redact-paths` profile：单文档 opaque refs，覆盖成功/失败和解析前错误。
- updater 版本状态机、per-volume JSON 汇总、Go/Cargo/npm 次级缓存。
- 日志/runtime/download 只读 retention、Codex SQLite freelist、Darwin `T/X` 临时副本、
  Codex 临时结构与 Crashpad 配对诊断。
- 由生产 TUI 绘制函数和固定合成数据生成的确定性 SVG。

### Changed

- AI 扫描点的安全等级与默认选择策略解耦；AI 缓存即使可重建也不再默认勾选。
- 仓库以 GNU GPL v3 作为公共许可证；包装元数据改用 SPDX，wheel/sdist 携带完整许可证文本。
- Docker Build Cache、Images、Containers 均要求 identifier 精确选择；prune 启动后的
  timeout 或非零退出返回 `partial`。
- `clean`/`purge --select` 成为独立精确模式，不继承默认预选；`--select` 与 `--all` 冲突。
- `analyze` 按一级候选的 device + filesystem 双边界遍历；`reclaimable_bytes` 固定为 `0`。
- Codex 不再把 `.tmp` 整根当作普通缓存；专项诊断取得同路径所有权。
- 扫描任务失败/取消不再伪装为 `100% complete`；文本报告区分预览、精确选择和特权项。

### Security

- Docker actionable 候选携带不序列化的 scan-time target binding；prune 前复核 CLI
  realpath、endpoint TLS mode 与 Engine ID。
- 普通 Trash 移动改用 Darwin `renameatx_np(RENAME_EXCL | RENAME_NOFOLLOW_ANY)`；目标目录
  必须属于当前用户并使用私有权限。
- 托管知识库用稳定 `0600` 锁把 sequence/key 检查和 `os.replace` 安装放进同一临界区。
- 扫描优先 Darwin `SF_DATALESS`，保留 zero-block 兜底；`EINTR` 透明重试。
- 已知应用归属保护覆盖通用 `~/Library/Caches`；updater 执行前重判版本状态。

## [0.23.0] - 2026-07-30

### Added

- system、developer、ai、trash、project 五域扫描，物理/逻辑大小、硬链接去重和云占位保护。
- `clean`、`purge`、`analyze` 的文本、JSON、参数化选择和 curses 全屏审阅。
- 显式 `--yes` 用户态执行、同卷 Trash、Trash 永久清空与结构化 cleanup report。
- 项目根识别、嵌套分组、公开产物字典和 7 天预选。
- Docker daemon 只读容量，以及 Build Cache/Images/Containers 固定 prune 白名单。
- Predicate 组合、JSON KnowledgeBase、用户 ignore、签名 HTTPS 更新、防回滚和公钥钉扎。
- 加权进度、通用任务 DAG、动态扫描并发、运行中进程保护。
- System Junk 一级候选、Darwin cache、broken startup items、Time Machine 快照只读提示。
- ApplicationLanguages 保守只读审计；修改执行保持锁定。
- 标准 Python 包装、`openclean` console script、wheel/sdist、macOS CI 和隔离功能预览。
- JSON schema v2：区分 potential/actionable/privileged/unsupported bytes，补充 scan/analyze 上下文，
  并为参数与运行时错误提供机器可读 envelope。

### Security

- 环境变量缓存路径限制在 `~/Library/Caches` 或 `~/.cache`，降级为 confirm 且要求精确选择。
- 扫描与执行拒绝 ancestor symlink；普通 Trash 操作使用逐组件 no-follow 目录 fd 和
  fd-relative rename/unlink/rmtree。
- TUI 批量选择不会纳入要求逐项确认的环境来源；准备 Trash 后会再次复核保护规则、
  云文件、后代和 owner，避免预检后的状态变化绕过保护。
- ApplicationLanguages 缺少或无效 metadata 时 fail-closed。
- 不可执行候选不再计入 `reclaimable_bytes`。

### Known limitations

- SMAppService/XPC 特权 helper 未实现。
- `optimize ram|purgeable` 只有明确的安全拒绝契约。
- Docker prune 尚需在用户自己的 daemon 上做单独验收；preview 不连接真实 daemon。
- 当时尚未选择公共许可证，也没有 PyPI / Homebrew / GitHub Release 渠道。

# Changelog

[README](README.md) · [能力地图](docs/CAPABILITIES.md) ·
[功能预览](docs/PREVIEW.md) · [架构](docs/ARCHITECTURE.md) ·
[安全](SECURITY.md) · [规格索引](specs/_index.md)

本文件记录用户可见变化，格式参考 Keep a Changelog。项目尚未创建 tag、GitHub Release、
PyPI 或 Homebrew 发布；以下版本表示代码基线，不代表已经公开发布。

## [Unreleased]

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

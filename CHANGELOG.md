# Changelog

[README](README.md) · [能力地图](docs/CAPABILITIES.md) ·
[功能预览](docs/PREVIEW.md) · [架构](docs/ARCHITECTURE.md) ·
[安全](SECURITY.md) · [规格索引](specs/_index.md)

本文件记录用户可见变化，格式参考 Keep a Changelog。项目尚未创建 tag、GitHub Release、
PyPI 或 Homebrew 发布；以下版本表示代码基线，不代表已经公开发布。

## [Unreleased]

### Changed

- 新增 AI agent 只读调用指南；开发工具版本集中到 `requirements-dev.txt`，避免本地与 CI
  使用不同 Ruff 默认规则。包装元数据改用 SPDX license expression，wheel/sdist 都携带
  完整 GPL-3.0 文本，release archive audit 会拒绝缺失或不一致的许可证。
- 仓库现以 GNU GPL v3 作为公共许可证；公开文档、贡献指南、包装元数据和能力表与
  GitHub 仓库现状对齐。根 README 改为可扫读首页（徽章、目录、特性摘要、合成 TUI
  预览），修复能力表在 GitHub 上因 `|` 造成的表格错列，并把实现级安全细节下沉到
  `SECURITY.md` 与 `docs/`。
- Docker Build Cache、Images、Containers 现在都要求用资源 identifier 精确选择；不会再由
  默认选择、`--all` 或 tier 批量参数带入不可恢复的 prune。CLI exit 0 后即使容量文本
  无法解析，也会报告“prune 已完成、释放量未知”，不再把已发生的操作伪报为失败。
- Docker prune 启动后的 timeout 或非零退出现在返回 `partial`，明确说明 daemon 侧删除
  可能已经发生且释放量未知；CLI 启动前失败仍保持普通 `failed`。
- 所有 JSON 子命令新增显式 `--redact-paths` profile；默认 schema v2 精确路径保持兼容，
  脱敏输出使用单文档 opaque refs，并处理 message/note 与 argparse 解析前错误。
- 扫描任务异常或取消不再被进度模型伪装为成功的 `100% complete` 终态；TTY renderer
  对成功、失败和取消的整体终态统一绕过节流，快速结束也会显示最终状态。
- ApplicationLanguages 只读候选统一计入 unsupported 容量，不再把签名应用修改误分类为
  “安装特权 helper 后即可执行”。
- `clean`/`purge --select` 现在是独立精确模式：不继承默认预选，tier flag 只授权指定
  confirm/critical 目标；`--select` 与 `--all` 被拒绝组合。
- `scan` 按首次出现顺序去重 domain 和 project root；显式 project root 在扫描前校验
  存在、目录类型和 symlink，错误使用稳定 JSON envelope。
- 文本报告现在标明“需精确选择”“需要特权 helper”和具体不可执行原因。
- 源码便捷入口在 Python `<3.11` 上返回 `unsupported_python` 错误，不再进入扫描后才
  暴露 `zip(strict=True)` traceback；help/version 仍可读取。
- `analyze --top` 在单层文本、行式浏览器和 curses TUI 中都明确显示截断数量，并说明
  百分比基于全部候选。
- 隔离 preview 用两个合成 Trash 根验证精确选择不会产生 collateral selection。
- 文本清理报告现在准确区分只读预览和执行结果，并把精确选择统一标为“当前选择”。
- `clean`/`purge`/`optimize` help 直接说明永久操作、同卷 Trash 和预期退出码；临时
  `--ignore` 与持久 `ignore add` 的边界及规范化路径回执更加明确。
- 文档新增由生产 TUI 绘制函数和固定合成数据生成的三张确定性 SVG，并由 `make check`
  核对资产；能力矩阵和辅助文档导航同步优化。

### Security

- Docker actionable 候选现在携带不序列化的 scan-time target binding；容量读取和 prune
  使用扫描时解析的 CLI realpath 和明确 context/effective host，执行前复核 CLI、endpoint
  TLS mode 与 Engine ID。缺失、畸形、CLI realpath 变化或即时复核发现不一致会在破坏性
  命令启动前拒绝并要求重新扫描。

- 普通 Trash 移动改用 Darwin `renameatx_np(RENAME_EXCL | RENAME_NOFOLLOW_ANY)`，目标名
  发生竞态时原子拒绝覆盖；API 或文件系统能力不足时 fail-closed。
- 普通 Trash 目标必须属于当前用户并使用私有权限；最终打开的目录 fd 会复核
  device/inode/owner/mode。新建目录改为在可信父目录 fd 下相对创建，完成 no-follow
  identity 校验后才通过 `fchmod` 设置权限，不会跟随竞态 symlink 修改外部目标。
- Trash rename 成功后的源目录和目标目录 fd 会分别尽力关闭；任一关闭失败均保留已移动
  事实并返回 `partial`，不会泄漏另一个 fd 或把已发生操作伪报为普通 `failed`。
- Trash 永久清空只删除最终保护审计生成的 inode 快照；审计后新增项保留，部分永久删除
  使用 `partial` 明确报告已经发生但无法精确计量的副作用。
- 托管知识库使用稳定的 `0600` 目标锁，把 sequence/key 检查和原子安装放在同一个跨进程
  临界区，避免并发更新产生 sequence 回退。
- 托管知识库以 `os.replace` 作为安装提交边界；替换后的目录同步、目录 fd 或锁 fd 清理
  失败不再把已安装 sequence 报成失败，替换前的临时文件仍固定为 `0600` 并完成 fsync。
- macOS 扫描优先使用 Darwin `SF_DATALESS`，并保留 zero-block 启发式；dataless 目录、
  startup plist、应用 metadata/Resources/localization 会在枚举或读取前 fail-closed。
- 清理后代审计改为 no-follow 目录 fd + `fstat` + `scandir(fd)`，并复核类型、owner、device
  与 dataless 状态；不会跟随扫描后替换的后代 symlink。
- startup item 在每次 plist 读取前后重新检查 identity 与占位状态；最终 path/fd source
  stat 同时拒绝 `SF_DATALESS` 和 zero-block 疑似占位。

### Planned

- 在具备完整 Xcode、签名身份和产品策略后，单独设计 native SMAppService/XPC helper。
- 仅在确认安全公开 macOS 接口后实现 `optimize ram|purgeable` executor。
- 配置项目自有的签名知识库 channel 和正式公钥。

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

# 开发任务与能力缺口

[README](../README.md) · [能力地图](../docs/CAPABILITIES.md) ·
[功能预览](../docs/PREVIEW.md) · [架构](../docs/ARCHITECTURE.md) ·
[安全](../SECURITY.md) · [规格索引](../specs/_index.md) ·
[实现说明](README.md)

当前发布版本：`openclean 0.24.0a3`，对齐 CleanMyMac CLI v1.0.0 Public Beta 的公开命令面。
本清单只保留尚未完成或需要外部验收的工作。已完成功能以
[docs/CAPABILITIES.md](../docs/CAPABILITIES.md)、CHANGELOG 和 exact-head CI 为准。
CleanMyMac Desktop 的应用卸载、恶意软件扫描等不是本项目 CLI 对齐目标。

> **Agent Runtime P0a 已实现，P0b 未启用**：探测、Run/Finding、展示和计划预览已落地。
> 内置 7 条 Codex 策略及新增 WorkBuddy 结构策略均只读；不能把测试用 synthetic approval 当生产策略证据。
> 经典命令、TUI 和共享执行器继续保留。详见 [当前状态](../docs/AGENT_RUNTIME_STATUS.md)。

## Agent 附加接口的缺口（不作为产品固定排期）

| 工作 | 当前状态 | 完成依据 |
|---|---|---|
| P0a 存储与计划完整性、HOME/ignore、JSON 状态 | 已实现 | 当前源码及 `test_agent_*` |
| macOS 原生全套测试与演示 | 由 macOS Actions runner 持续验证 | 不使用 Linux shim 的 make ci-check 原始结果 |
| P0b 真实目标结构匹配器 | 未实现，明确阻止动作 | 正反结构样本、保护路径与运行状态反例 |
| 真实 Observation 与逐策略 promotion | 未完成 | 用户提供/批准的来源和固定 pack hash；不可补造 |
| 首条生产策略启用 | 未完成 | 上述条件满足后的单独变更，不由本候选版默认开启 |
| WorkBuddy 经验结构 | 已实现只读识别及 inspect；正反样本测试 | 见 `docs/EXPERIENCE.md`，不启用删除 |
| 其余未交付 pack / explore / lab / MCP | 未批准具体实现的候选 | 先确认用户功能与既有 CLI 的真实缺口；不要求全量 pack 化 |

## P0：发布前必须保持的阻断边界

### 1. SMAppService/XPC 特权帮助器

**状态：外部前提未满足，当前 fail-closed。**

需要完整 Xcode、native host app/helper、Apple Developer 签名身份、Team ID、entitlements、
notarization 和真实安装/升级/回滚验收。Python wheel 本身不能安全承载该能力。

实现前必须先完成：

- 基于 audit token 的 designated requirement 双向校验，而不是只比 Team ID；
- 有版本和大小限制的协议，超时、取消、幂等和错误分类；
- 领域操作白名单，禁止通用 `{delete, absolute_path}`；
- helper 端重新发现和验证允许的 root/相对目标，不信任 CLI 的安全标记；
- fd-based no-follow、device/inode/owner/mount 检查和执行前业务重判定；
- FDA、admin helper、SIP unsupported 三类能力分别建模；
- 安装、升级、卸载、失效签名和回滚测试。

依据：[specs/04-ipc-protocol.md](../specs/04-ipc-protocol.md)。在这些门槛完成前，
`requires_privilege=true` 项必须保持 `actionable=false`。

### 2. `optimize ram|purgeable` executor

**状态：命令树和 JSON refusal 已完成；实际执行器不可用。**

- `/usr/sbin/purge` 需要权限、语义是磁盘缓存，不等价于释放匿名内存；不能套用。
- `memory_pressure` 或主动制造内存压力不是安全产品实现；不能伪装成功。
- 尚未找到可验证、安全、公开的 generic purgeable-space 释放 API。
- 任何未来 executor 都必须遵守默认只读/显式授权，并有可测量 before/after 证据。

在接口事实变化前，保持 `status=unavailable` 和退出码 1 是正确行为。

## P1：需要项目资源或真实环境

### 3. 正式知识库发布 channel

客户端 HTTPS、签名、大小限制、跨进程防回滚、公钥钉扎、key rotation 和原子安装已完成。
仍需项目自己拥有并审计：

- HTTPS 发布服务；
- 离线保管的 signing private key；
- 可提交的正式 public PEM；
- sequence/撤回/rotation/灾备流程；
- 完全由公开信息和本项目维护的规则数据。

禁止引入原厂 `.cmmkb`、私有规则或未知公钥。没有正式材料前不设置默认 URL。

### 4. Docker 真实 daemon 联调

隔离测试已覆盖解析、分级、命令白名单和报告，但 preview 不连接真实 daemon。需要在明确
授权的测试 daemon 上分别验证：

- `docker system df --format json` 不同版本输出；
- Build Cache、Images、Containers 三条 prune 的 before/after 和错误报告；
- 当前客户端已把扫描与执行绑定到明确 context/effective host、endpoint TLS mode 和
  Engine ID，同时绑定扫描时解析的 CLI realpath，并在 prune 前复核；仍需用真实 context、
  `DOCKER_HOST`、PATH/symlink 切换、CLI 就地升级、TLS 和 Engine reset 验证行为。realpath
  binding 不对同路径二进制内容做 pinning，标准 CLI 多进程也无法提供同一 API connection
  的原子 precondition；
- 运行中 container、并发 daemon 变化和超时；
- Local Volumes 永远不可执行。

真实 prune 不可通过 Trash 恢复，不能在普通开发 daemon 或用户数据上自动跑。

### 5. macOS/Python 兼容矩阵

CI 已配置 `macos-15`、`macos-26` 双版本，均使用 Python 3.11；新矩阵的通过情况以对应提交的 Actions 为准。
两边均运行完整测试、隔离预览、构建、归档与独立安装检查；失败不取消另一系统的检查，产物名称按系统区分。
本地不要求额外解释器；进一步扩展 Python/macOS 版本仍需明确目标，不自动扩展所有版本组合。重点检查 curses、`st_blocks`、`getconf`、`tmutil`、mount、
Trash 权限和 OpenSSL CLI 差异。

另需在专用测试账号/隔离 File Provider 数据上验证 `SF_DATALESS`：扫描前后占位状态不变、
dataless 目录不触发枚举或下载，并覆盖 iCloud Drive 与至少一个第三方 provider。不要在
真实用户文件上通过 evict/Remove Download 制造测试夹具。

## P2：保守增强

### 6. 权限能力模型

当前 `requires_privilege` 已能 fail-closed，但未来应把执行能力细分为：

- `user`
- `exact_user_confirmation`
- `full_disk_access`
- `admin_helper`
- `sip_unsupported`
- `signed_bundle_mutation_unsupported`

这项重构必须保持现有 JSON 兼容或通过 schema 版本升级明确变更。

### 7. ApplicationLanguages / universal binary

ApplicationLanguages 只读审计已经完成，并故意固定 `critical + actionable=false`。不要把
通用 cleanup executor 直接用于 `.app`，否则可能移动整个应用或破坏 code signature。

universal binary thinning 未实现。若未来仅做审计，需要结构化记录 Mach-O slices、父 app、
签名状态和兼容性；任何写入必须另有原子输出、恢复、签名和 Rosetta/plugin 验收方案。

### 8. Countable 进度与任务控制聚合

当前已实现固定权重百分比、不可变快照、任务成功/失败/取消终态和共享三态协作控制；
`processed_items` 仍是启发式进度输入，不是已知总量的 Countable 完成数/总数。
Countable total、每任务 Control 聚合和 observer 仅是条件增强；需先证明具体 UI/自动化需求，
不是参考对象出现同名机制就必须实现。若获批，须区分未知 total、进度 callback 与控制事件。

### 9. 浏览器 origin 与版本化 CLI 缓存

- Chrome/Brave/Edge/Comet `Service Worker/CacheStorage` 的 profile 级只读汇总已完成；只
  发现 Default/Profile 用户根，不删除 Cookies、Login Data、IndexedDB 或整个 profile，
  运行中固定不可执行。origin 级汇总仍需先确认稳定、结构化且可脱敏的数据源，不使用
  `strings` 或临时正则解析 LevelDB。
- Claude/Copilot 等版本目录应先解析当前 symlink/安装版本，只报告非当前完整旧版本；
  零字节更新 marker、当前 binary 和 session state 必须保留。
- UURemote application/updater logs、历史 `.pkg` 和 Darwin temp 已纳入只读 retention
  诊断；若未来允许删除单个 `.pkg`，仍需读取可信 package version、比较已安装版本并在
  执行前复核句柄，再纳入 updater 状态机。
- Codex 相邻原子写 temp 与 disabled feature cache 仍未做通用判定；目标缺失、JSON 截断或
  当前配置关闭都不能单独证明可删除，后续必须先定义 primary/backup/feature ownership。

### 10. Analyze 响应与规模验收

当前源码已增加加载反馈、协作取消、有界批次、会话导航缓存、完整 issues 和选择提交前复核。
正常提交根据当前页面与所选来源完整性返回状态；Q 主动取消审阅保持 0，SIGINT 为 130。
公共 JSON 与清理范围保持；本地基准脚本为 `scripts/benchmark_analyze.py`，所有夹具位于临时 HOME。
代码与隔离测试不替代指定真实目录、卷和文件状态下的验收；公开发行与系统安装状态另核对。
后续根据优化后的数据评估原生只读扫描内核，不预定 Rust 迁移、不引入持久索引或常驻服务。

### 11. CLI/TUI 模式契约与统一扫描交互

依据 `local/diagnostics/20260922-cmm-ux/` 的总方案与接口契约，CLI-00/01 与 UX-01～03 已落地：
集中模式路由（`display_mode.py`）与 `--interactive`、Analyze 三态选择标记与两层范围汇总、
clean/purge/analyze 统一扫描页（Space 暂停/继续、检查点确认、Q/Ctrl-C 收尾、任务 started
真实状态）、More 命令速查与终态措辞区分。契约见 `docs/MODES.md` 与 specs/06 REQ-FLOW-001b。
尚未完成、不阻塞本轮交付的后续项：

- “显式子命令默认文本 CLI、TUI 显式进入”的默认路由迁移：需要单独的兼容性决策，
  同步 README/help/规格/CHANGELOG 后实施；当前保持 TTY 自动 TUI。
- UX-04 长列表可选“重点视图”（V 切换、虚拟展开行）与主菜单 Config 只读总览。
- 对应提交的云端 CI 仍待触发；本地构建和临时环境安装验证不依赖先提交代码，
  不能用本地结果替代 exact-head CI。
- 本地扫描页计时工具为 `scripts/benchmark_scan_ui.py`，覆盖三命令各 ≥20 次 Q/SIGINT、
  暂停反馈、线程收尾、四档窗口与终端恢复；应用入口到首帧和包含 Python 启动的耗时分别记录。
  本机进程启动到首帧存在超过 200 ms 的样本，不能承诺所有场景整体启动都达标。
  这些受控 worker 样本不替代真实磁盘、阻塞系统调用和安装包的单独验收。

当前源码补齐了多 worker 暂停确认、真实处理路径、动态任务开始状态和扫描后 UI 失败不重扫；
SIGINT 在任务提交及等待两个阶段都先传播取消再收尾，有限周期等待覆盖信号送达工作线程的情况。
非交互扫描先记录 SIGINT 请求，通过检查点退出，再统一输出 130，避免异步异常打断线程锁。
本地验证与构建记录保存在私有诊断目录，
完成度以当前源码的原始结果为准，不沿用历史测试数量。

## 后续识别增强（不属于本轮交付范围）

- ZCode `dev.zcode.app.ShipIt` / `@zcodedesktop-updater` 尚未注册 updater 版本规则；需有
  可核实的暂存布局、精确 app/ZIP 元数据与下载中状态。相同 bundle ID 的多个安装副本仍
  保持版本不明，不以应用名称筛选或最高版本替代安装目标证据。
- GOCACHE/GOMODCACHE/HOMEBREW_CACHE 已在可信用户缓存根内生效；外置缓存与按卷 pnpm
  store 的发现仍需独立定义定位、归属和只读/动作边界，不能只添加环境变量或放开任意路径。
- `large` 大文件扫描已实现只读发现；Large & Old 的访问日期/年龄过滤仍未实现，后续需明确
  Spotlight 缺失与 mtime 的区别，不把大小或未修改天数直接解释为闲置或可删除。
- 语言资源审计尚未发现外置 Applications 根；缺失开发语言元数据时会记录 issue 并跳过，
  后续可补按应用的未知量/跳过明细，不推断所有 `.lproj` 都可删除。
- 文本摘要已区分发现、当前可执行、选择和只读/阻断量；进一步建议层仍需逐项条件证据。
  退出应用不保证解锁，重启也不保证净释放。
- 通用 Electron 发现、额外项目标记与产物类型绑定、Xcode downloaded runtimes 和
  CoreSymbolication 专项仍是待核实对象，不作为本轮必做功能或可执行清理承诺。

## 已完成能力

已落地功能不要在本清单重复展开。以 [docs/CAPABILITIES.md](../docs/CAPABILITIES.md)
和 [CHANGELOG.md](../CHANGELOG.md) 为准；本地轻量检查与 exact-head CI 分别报告。

## 提交前验证

```bash
make check
make test-focused TEST_PATTERN=test_agent_identifiers.py  # 换成受影响文件
git diff --check
```

全量 `make ci-check`、构建、归档审计及安装验证交给 GitHub Actions；本地仅按需复现失败项。

任何真实 `--yes`、Docker prune、网络知识库更新、特权安装或公共发布都需要单独授权和
对应环境证据。

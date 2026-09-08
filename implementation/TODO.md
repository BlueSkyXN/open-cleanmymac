# 开发任务与能力缺口

[README](../README.md) · [能力地图](../docs/CAPABILITIES.md) ·
[功能预览](../docs/PREVIEW.md) · [架构](../docs/ARCHITECTURE.md) ·
[安全](../SECURITY.md) · [规格索引](../specs/_index.md) ·
[实现说明](README.md)

当前版本：`openclean 0.24.0a2`，对齐 CleanMyMac CLI v1.0.0 Public Beta 的公开命令面。
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
